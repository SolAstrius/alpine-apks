// SPDX-License-Identifier: MPL-2.0
//
// Async RPC client. Sits on top of any (read, write) pair from
// transport::connect. Owns a background task that:
//
//   * reads frames from the wire
//   * routes Response frames to per-id `oneshot::Sender`s registered by
//     `call()`
//   * fans Event frames out to a `broadcast::Sender` so multiple
//     `events()` consumers (or `modem-call`'s listen loop) can each see
//     them.
//
// The CLI uses block_on around `call()` for sync-feeling subcommands
// and an async loop for events / modem-call.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Result};
use futures::{SinkExt, StreamExt};
use scev_wire::{Frame, FrameCodec, Value};
use thiserror::Error;
use tokio::sync::{broadcast, mpsc, oneshot, Mutex};
use tokio_util::codec::{FramedRead, FramedWrite};

use crate::transport::{BoxRead, BoxWrite};

#[derive(Debug, Error)]
pub enum CallError {
    #[error("rpc returned error: {0}")]
    Rpc(String),
    #[error("rpc timed out")]
    Timeout,
    #[error("client disconnected")]
    Disconnected,
    #[error("io: {0}")]
    #[allow(dead_code)]
    Io(String),
}

#[derive(Debug, Clone)]
pub struct EventMsg {
    pub name: String,
    pub args: Value,
}

pub struct Client {
    next_id: Mutex<u64>,
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Value, String>>>>>,
    out_tx: mpsc::Sender<Frame>,
    events_tx: broadcast::Sender<EventMsg>,
    /// Held to keep the background tasks alive until the Client drops.
    _shutdown: mpsc::Sender<()>,
}

impl Client {
    pub fn start(read: BoxRead, write: BoxWrite) -> Self {
        let (out_tx, mut out_rx) = mpsc::channel::<Frame>(64);
        let pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Value, String>>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let (events_tx, _) = broadcast::channel::<EventMsg>(1024);
        let (shutdown_tx, _shutdown_rx) = mpsc::channel::<()>(1);

        // Reader task — owns FramedRead, dispatches by tag.
        {
            let pending = pending.clone();
            let events_tx = events_tx.clone();
            tokio::spawn(async move {
                let mut reader = FramedRead::new(read, FrameCodec::new());
                while let Some(item) = reader.next().await {
                    let payload = match item {
                        Ok(p) => p,
                        Err(_) => break, // wire died
                    };
                    let frame = match Frame::from_bytes(&payload) {
                        Ok(f) => f,
                        Err(_) => continue, // malformed; drop
                    };
                    match frame {
                        Frame::Response { id, err, result } => {
                            let mut pen = pending.lock().await;
                            if let Some(tx) = pen.remove(&id) {
                                let outcome = match err {
                                    Some(e) => Err(e),
                                    None => Ok(result),
                                };
                                let _ = tx.send(outcome);
                            }
                        }
                        Frame::Event { name, args } => {
                            let _ = events_tx.send(EventMsg { name, args });
                        }
                        Frame::Request { .. } => {
                            // Server doesn't send requests; ignore.
                        }
                    }
                }
                // On disconnect: drop pending senders so callers see
                // CallError::Disconnected instead of hanging.
                let mut pen = pending.lock().await;
                pen.clear();
            });
        }

        // Writer task — drains the out_rx mpsc, encodes, writes.
        tokio::spawn(async move {
            let mut writer = FramedWrite::new(write, FrameCodec::new());
            while let Some(frame) = out_rx.recv().await {
                let bytes = match frame.to_bytes() {
                    Ok(b) => b,
                    Err(_) => continue,
                };
                if writer.send(bytes).await.is_err() {
                    break;
                }
            }
        });

        Client {
            next_id: Mutex::new(1),
            pending,
            out_tx,
            events_tx,
            _shutdown: shutdown_tx,
        }
    }

    pub async fn call(
        &self,
        method: &str,
        args: Value,
        timeout: Duration,
    ) -> Result<Value, CallError> {
        let id = {
            let mut n = self.next_id.lock().await;
            let v = *n;
            *n = n.wrapping_add(1).max(1);
            v
        };
        let (tx, rx) = oneshot::channel::<Result<Value, String>>();
        {
            let mut pen = self.pending.lock().await;
            pen.insert(id, tx);
        }
        let frame = Frame::Request {
            id,
            method: method.into(),
            args,
        };
        if self.out_tx.send(frame).await.is_err() {
            self.pending.lock().await.remove(&id);
            return Err(CallError::Disconnected);
        }
        let outcome = match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(Ok(v))) => Ok(v),
            Ok(Ok(Err(e))) => Err(CallError::Rpc(e)),
            Ok(Err(_)) => Err(CallError::Disconnected),
            Err(_) => {
                // Timeout: clean up the pending entry so a late
                // response doesn't leak the slot.
                self.pending.lock().await.remove(&id);
                Err(CallError::Timeout)
            }
        };
        outcome
    }

    pub fn events(&self) -> broadcast::Receiver<EventMsg> {
        self.events_tx.subscribe()
    }

    /// Convenience: send a request without expecting a response. Used
    /// when fire-and-forget would race a follow-up await on events
    /// (modem-call doesn't, in the daemonized world — but the helper
    /// is handy regardless).
    #[allow(dead_code)]
    pub async fn send_oneway(&self, method: &str, args: Value) -> Result<()> {
        let id = {
            let mut n = self.next_id.lock().await;
            let v = *n;
            *n = n.wrapping_add(1).max(1);
            v
        };
        let frame = Frame::Request {
            id,
            method: method.into(),
            args,
        };
        self.out_tx
            .send(frame)
            .await
            .map_err(|_| anyhow!("client disconnected"))
    }
}
