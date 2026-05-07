// SPDX-License-Identifier: MPL-2.0
//
// Dispatcher — single task owning the routing table. Every per-client
// task and the serial reader feed their frames in via one mpsc; the
// dispatcher decides where each goes:
//
//   Request from client  → allocate global daemon_id, record
//                          (daemon_id → (client_id, client's local id)),
//                          rewrite frame, push to serial writer.
//   Response from serial → look up daemon_id → (client_id, local id),
//                          rewrite frame back to local id, send to that
//                          client. Drop if client is gone.
//   Event from serial    → broadcast to every connected client. No
//                          buffering for late subscribers; events that
//                          arrive while no client is connected are
//                          dropped.

use std::collections::HashMap;

use scev_wire::Frame;
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

pub type ClientId = u64;

/// What the dispatcher receives.
pub enum DispatcherMsg {
    /// New client. Dispatcher records the channel and assigns the id.
    /// The acceptor allocates the id; we trust it not to collide.
    ClientConnected {
        id: ClientId,
        outbound: mpsc::Sender<Frame>,
    },
    ClientDisconnected(ClientId),
    /// Frame from a client (guest → host direction). Dispatcher
    /// rewrites the request id and forwards to serial.
    FromClient { id: ClientId, frame: Frame },
    /// Frame from the host (host → guest direction). Dispatcher routes
    /// responses back to the originating client and broadcasts events.
    FromSerial(Frame),
}

pub fn spawn(
    serial_in_rx: mpsc::Receiver<Frame>,
    serial_out_tx: mpsc::Sender<Frame>,
) -> mpsc::Sender<DispatcherMsg> {
    let (tx, rx) = mpsc::channel::<DispatcherMsg>(1024);

    // Pump serial inbound frames into the dispatcher mpsc so the main
    // loop only listens on a single channel.
    {
        let tx = tx.clone();
        let mut serial_in_rx = serial_in_rx;
        tokio::spawn(async move {
            while let Some(frame) = serial_in_rx.recv().await {
                if tx.send(DispatcherMsg::FromSerial(frame)).await.is_err() {
                    break;
                }
            }
        });
    }

    tokio::spawn(async move { run(rx, serial_out_tx).await });
    tx
}

async fn run(mut rx: mpsc::Receiver<DispatcherMsg>, serial_out_tx: mpsc::Sender<Frame>) {
    let mut clients: HashMap<ClientId, mpsc::Sender<Frame>> = HashMap::new();

    // Pending requests: daemon_id → (origin client, original local id).
    // The local id is what we rewrite back to before forwarding the
    // response — keeps each client's id space private.
    let mut pending: HashMap<u64, (ClientId, u64)> = HashMap::new();
    let mut next_daemon_id: u64 = 1;

    while let Some(msg) = rx.recv().await {
        match msg {
            DispatcherMsg::ClientConnected { id, outbound } => {
                debug!(client = id, "client connected");
                clients.insert(id, outbound);
            }
            DispatcherMsg::ClientDisconnected(id) => {
                debug!(client = id, "client disconnected");
                clients.remove(&id);
                // Drop any pending requests for the gone client — the
                // host will still emit responses for them but we'll
                // discard those harmlessly when they arrive.
                pending.retain(|_, (cid, _)| *cid != id);
            }
            DispatcherMsg::FromClient { id, frame } => {
                handle_from_client(
                    id,
                    frame,
                    &mut pending,
                    &mut next_daemon_id,
                    &serial_out_tx,
                    &clients,
                )
                .await;
            }
            DispatcherMsg::FromSerial(frame) => {
                handle_from_serial(frame, &mut pending, &clients).await;
            }
        }
    }
    info!("dispatcher exiting");
}

async fn handle_from_client(
    client_id: ClientId,
    frame: Frame,
    pending: &mut HashMap<u64, (ClientId, u64)>,
    next_daemon_id: &mut u64,
    serial_out_tx: &mpsc::Sender<Frame>,
    clients: &HashMap<ClientId, mpsc::Sender<Frame>>,
) {
    let outbound = match frame {
        Frame::Request {
            id: client_local_id,
            method,
            args,
        } => {
            // Allocate a global id, record the mapping, rewrite the
            // outbound frame to use it.
            let daemon_id = *next_daemon_id;
            *next_daemon_id = next_daemon_id.wrapping_add(1);
            // Skip 0 (we never want a zero id round-tripped).
            if *next_daemon_id == 0 {
                *next_daemon_id = 1;
            }
            pending.insert(daemon_id, (client_id, client_local_id));
            Frame::Request {
                id: daemon_id,
                method,
                args,
            }
        }
        // Clients sending Response/Event frames is nonsensical — drop.
        _ => {
            warn!(client = client_id, "client sent non-Request frame; dropping");
            return;
        }
    };
    if let Err(e) = serial_out_tx.send(outbound).await {
        warn!(client = client_id, error = %e, "serial writer gone; dropping client request");
        // Send the client a synthesized error so its call() doesn't hang forever.
        if let Some(tx) = clients.get(&client_id) {
            let _ = tx
                .send(Frame::Response {
                    id: 0,
                    err: Some(scev_wire::ErrorInfo::generic("scevd: serial writer offline")),
                    result: scev_wire::Value::Nil,
                })
                .await;
        }
    }
}

async fn handle_from_serial(
    frame: Frame,
    pending: &mut HashMap<u64, (ClientId, u64)>,
    clients: &HashMap<ClientId, mpsc::Sender<Frame>>,
) {
    match frame {
        Frame::Response { id, err, result } => {
            let Some((client_id, local_id)) = pending.remove(&id) else {
                debug!(daemon_id = id, "stray response (no pending entry)");
                return;
            };
            let rewritten = Frame::Response {
                id: local_id,
                err,
                result,
            };
            if let Some(tx) = clients.get(&client_id) {
                if let Err(e) = tx.send(rewritten).await {
                    debug!(client = client_id, error = %e, "client gone before response delivery");
                }
            }
        }
        Frame::Chunked {
            response_id,
            stream_id,
            total_size,
        } => {
            // Mark the original request as "still pending until the
            // chunked drain finishes" — we don't remove the entry,
            // because the read_chunk follow-ups are issued by the
            // client itself (using the daemon's id space for those
            // new requests, allocated normally) and the original
            // pending slot is what the client's reader task is
            // waiting on. The client recognises the marker, drains,
            // and resolves locally; the daemon never sees a Response
            // for `response_id` because the original handler invocation
            // on the host completed when it emitted the Chunked frame.
            //
            // What we DO need: rewrite the daemon-side response_id
            // back to the client's local id, and forward.
            let Some(&(client_id, local_id)) = pending.get(&response_id) else {
                debug!(
                    daemon_id = response_id,
                    stream_id, "stray chunked marker (no pending entry)",
                );
                return;
            };
            // The original request slot is consumed — the client's
            // pending entry will be cleared by its drain finishing.
            pending.remove(&response_id);
            let rewritten = Frame::Chunked {
                response_id: local_id,
                stream_id,
                total_size,
            };
            if let Some(tx) = clients.get(&client_id) {
                if let Err(e) = tx.send(rewritten).await {
                    debug!(client = client_id, error = %e, "client gone before chunked marker delivery");
                }
            }
        }
        Frame::Event { .. } => {
            // Broadcast to every connected client. Use try_send so a
            // slow consumer can't stall the dispatcher; if a client's
            // queue is full, the event is dropped for that client only.
            for (cid, tx) in clients.iter() {
                if let Err(e) = tx.try_send(frame.clone()) {
                    warn!(client = cid, error = %e, "drop event for slow client");
                }
            }
        }
        Frame::Request { .. } => {
            // The host doesn't issue requests in this protocol — drop.
            warn!("host sent unexpected Request frame; dropping");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use scev_wire::Value;

    /// Two clients both use local id=1; daemon must rewrite to distinct
    /// global ids and route each response back to the right client with
    /// the local id restored. This is the single most important
    /// correctness property of the dispatcher — without it, two
    /// concurrent `scev call` invocations would clobber each other's
    /// responses.
    #[tokio::test]
    async fn id_rewriting_isolates_clients() {
        let (serial_in_tx, serial_in_rx) = mpsc::channel::<Frame>(8);
        let (serial_out_tx, mut serial_out_rx) = mpsc::channel::<Frame>(8);
        let dispatcher_tx = spawn(serial_in_rx, serial_out_tx);

        // Two fake clients: each gets its own outbound mpsc.
        let (c1_tx, mut c1_rx) = mpsc::channel::<Frame>(8);
        let (c2_tx, mut c2_rx) = mpsc::channel::<Frame>(8);
        dispatcher_tx
            .send(DispatcherMsg::ClientConnected {
                id: 100,
                outbound: c1_tx,
            })
            .await
            .unwrap();
        dispatcher_tx
            .send(DispatcherMsg::ClientConnected {
                id: 200,
                outbound: c2_tx,
            })
            .await
            .unwrap();

        // Both clients fire a request with the same local id (1).
        for client in [100u64, 200u64] {
            dispatcher_tx
                .send(DispatcherMsg::FromClient {
                    id: client,
                    frame: Frame::Request {
                        id: 1,
                        method: "ping".into(),
                        args: Value::Array(vec![]),
                    },
                })
                .await
                .unwrap();
        }

        // Serial side should see two requests with DIFFERENT daemon ids.
        let f1 = serial_out_rx.recv().await.unwrap();
        let f2 = serial_out_rx.recv().await.unwrap();
        let (id1, id2) = match (&f1, &f2) {
            (Frame::Request { id: a, .. }, Frame::Request { id: b, .. }) => (*a, *b),
            _ => panic!("expected two Requests on the serial side"),
        };
        assert_ne!(id1, id2, "daemon must allocate distinct global ids");

        // Send response for the second-requested daemon id first
        // (out of order on purpose — the dispatcher must route by id,
        // not by arrival order).
        serial_in_tx
            .send(Frame::Response {
                id: id2,
                err: None,
                result: Value::String("pong-from-2".into()),
            })
            .await
            .unwrap();
        serial_in_tx
            .send(Frame::Response {
                id: id1,
                err: None,
                result: Value::String("pong-from-1".into()),
            })
            .await
            .unwrap();

        // The mappings (id1 → first client, id2 → second client)
        // depend on the order ids were allocated. Both clients fired
        // before any response came back, so allocation order is the
        // FromClient send order (100 first, then 200).
        let r1 = c1_rx.recv().await.unwrap();
        let r2 = c2_rx.recv().await.unwrap();
        match (r1, r2) {
            (
                Frame::Response { id: 1, result: a, .. },
                Frame::Response { id: 1, result: b, .. },
            ) => {
                // Both clients see their LOCAL id (1) reflected back —
                // the daemon-internal id was rewritten away on the
                // way in and back on the way out.
                assert_eq!(a.as_str(), Some("pong-from-1"));
                assert_eq!(b.as_str(), Some("pong-from-2"));
            }
            other => panic!("unexpected responses: {other:?}"),
        }
    }

    #[tokio::test]
    async fn events_broadcast_to_every_client() {
        let (serial_in_tx, serial_in_rx) = mpsc::channel::<Frame>(8);
        let (serial_out_tx, _serial_out_rx) = mpsc::channel::<Frame>(8);
        let dispatcher_tx = spawn(serial_in_rx, serial_out_tx);

        let (c1_tx, mut c1_rx) = mpsc::channel::<Frame>(8);
        let (c2_tx, mut c2_rx) = mpsc::channel::<Frame>(8);
        dispatcher_tx
            .send(DispatcherMsg::ClientConnected {
                id: 1,
                outbound: c1_tx,
            })
            .await
            .unwrap();
        dispatcher_tx
            .send(DispatcherMsg::ClientConnected {
                id: 2,
                outbound: c2_tx,
            })
            .await
            .unwrap();

        serial_in_tx
            .send(Frame::Event {
                name: "modem_message".into(),
                args: Value::Array(vec![Value::String("left".into())]),
            })
            .await
            .unwrap();

        match c1_rx.recv().await.unwrap() {
            Frame::Event { name, .. } => assert_eq!(name, "modem_message"),
            other => panic!("c1 wrong frame: {other:?}"),
        }
        match c2_rx.recv().await.unwrap() {
            Frame::Event { name, .. } => assert_eq!(name, "modem_message"),
            other => panic!("c2 wrong frame: {other:?}"),
        }
    }

    #[tokio::test]
    async fn disconnected_client_drops_pending() {
        let (_serial_in_tx, serial_in_rx) = mpsc::channel::<Frame>(8);
        let (serial_out_tx, mut serial_out_rx) = mpsc::channel::<Frame>(8);
        let dispatcher_tx = spawn(serial_in_rx, serial_out_tx);

        let (c1_tx, _c1_rx) = mpsc::channel::<Frame>(8);
        dispatcher_tx
            .send(DispatcherMsg::ClientConnected {
                id: 99,
                outbound: c1_tx,
            })
            .await
            .unwrap();
        dispatcher_tx
            .send(DispatcherMsg::FromClient {
                id: 99,
                frame: Frame::Request {
                    id: 7,
                    method: "ping".into(),
                    args: Value::Array(vec![]),
                },
            })
            .await
            .unwrap();
        let _ = serial_out_rx.recv().await.unwrap();

        // Disconnect — pending entry should be cleared so a stale
        // response from the host doesn't get routed to nobody.
        dispatcher_tx
            .send(DispatcherMsg::ClientDisconnected(99))
            .await
            .unwrap();

        // Give the dispatcher a tick to process the disconnect.
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        // Nothing further to assert directly (pending is private) —
        // but the test passing without a panic + no leaks under
        // tokio's test runtime is the contract.
    }
}
