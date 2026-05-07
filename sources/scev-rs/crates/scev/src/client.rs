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
//   * intercepts Chunked markers, spawns a drain task that pulls the
//     full payload via `read_chunk` calls, decodes locally, and
//     resolves the original `oneshot` so the caller's `call()` sees
//     a normal Response — the chunked transport is invisible above
//     this layer.
//
// The CLI uses block_on around `call()` for sync-feeling subcommands
// and an async loop for events / modem-call.

use std::collections::HashMap;
use std::io::Cursor;
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
    /// Host returned a structured error response. Code is one of
    /// [`scev_wire::errors`] (or a future-host code we don't yet know);
    /// message is the human-readable form.
    #[error("rpc {0}")]
    Rpc(scev_wire::ErrorInfo),
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

/// Inner state shared between `Client` and any drain tasks it spawns.
/// Drain tasks need `next_id`/`pending`/`out_tx` to issue their own
/// `read_chunk` calls; pulling those out into an `Arc<ClientShared>`
/// lets the drainer participate in the normal call/response plumbing
/// without the Client struct having to be `Arc`-shared by callers.
struct ClientShared {
    next_id: Mutex<u64>,
    pending: Mutex<HashMap<u64, oneshot::Sender<Result<Value, scev_wire::ErrorInfo>>>>,
    out_tx: mpsc::Sender<Frame>,
}

impl ClientShared {
    async fn alloc_id(&self) -> u64 {
        let mut n = self.next_id.lock().await;
        let v = *n;
        *n = n.wrapping_add(1).max(1);
        v
    }

    async fn call(
        &self,
        method: &str,
        args: Value,
        timeout: Duration,
    ) -> Result<Value, CallError> {
        let id = self.alloc_id().await;
        let (tx, rx) = oneshot::channel::<Result<Value, scev_wire::ErrorInfo>>();
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
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(Ok(v))) => Ok(v),
            Ok(Ok(Err(e))) => Err(CallError::Rpc(e)),
            Ok(Err(_)) => Err(CallError::Disconnected),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err(CallError::Timeout)
            }
        }
    }
}

pub struct Client {
    shared: Arc<ClientShared>,
    events_tx: broadcast::Sender<EventMsg>,
    /// Held to keep the background tasks alive until the Client drops.
    _shutdown: mpsc::Sender<()>,
}

impl Client {
    pub fn start(read: BoxRead, write: BoxWrite) -> Self {
        let (out_tx, mut out_rx) = mpsc::channel::<Frame>(64);
        let shared = Arc::new(ClientShared {
            next_id: Mutex::new(1),
            pending: Mutex::new(HashMap::new()),
            out_tx,
        });
        let (events_tx, _) = broadcast::channel::<EventMsg>(1024);
        let (shutdown_tx, _shutdown_rx) = mpsc::channel::<()>(1);

        // Reader task — owns FramedRead, dispatches by tag.
        {
            let shared = shared.clone();
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
                            let mut pen = shared.pending.lock().await;
                            if let Some(tx) = pen.remove(&id) {
                                let outcome = match err {
                                    Some(e) => Err(e),
                                    None => Ok(result),
                                };
                                let _ = tx.send(outcome);
                            }
                        }
                        Frame::Chunked {
                            response_id,
                            stream_id,
                            total_size,
                        } => {
                            // Original caller's `oneshot` sender —
                            // remove it from pending and hand it to
                            // the drain task. The drain task issues
                            // its own `read_chunk` calls (registering
                            // fresh pending entries for those) and
                            // resolves the original oneshot when the
                            // assembled bytes decode cleanly.
                            let original = shared.pending.lock().await.remove(&response_id);
                            let Some(original) = original else {
                                // Marker for an unknown id — caller
                                // probably timed out or the daemon
                                // forwarded a stale chunked frame.
                                // Drop silently; the client has no
                                // oneshot to resolve.
                                let _ = stream_id;
                                continue;
                            };
                            let s = shared.clone();
                            tokio::spawn(async move {
                                drain_chunked(s, response_id, stream_id, total_size, original).await;
                            });
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
                let mut pen = shared.pending.lock().await;
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
            shared,
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
        self.shared.call(method, args, timeout).await
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
        let id = self.shared.alloc_id().await;
        let frame = Frame::Request {
            id,
            method: method.into(),
            args,
        };
        self.shared
            .out_tx
            .send(frame)
            .await
            .map_err(|_| anyhow!("client disconnected"))
    }
}

/// Per-chunk slice size — keep well under MAX_FRAME so the host's
/// read_chunk Response (a `bin` payload of `slice_len` bytes plus
/// msgpack/cobs overhead) always fits in one wire frame.
const CHUNK_SLICE: u64 = 32 * 1024;

/// Total time budget for draining one chunked response. Each
/// individual `read_chunk` call has its own short timeout; this is
/// the upper bound we give up at if the host stalls.
const CHUNK_DRAIN_TIMEOUT: Duration = Duration::from_secs(60);

/// Pull `total_size` bytes of stream `stream_id` via successive
/// `read_chunk` calls, decode the assembled buffer as the original
/// Response, and resolve the caller's oneshot. Any failure mid-drain
/// resolves the oneshot with a synthetic ErrorInfo so the caller sees
/// a clean `CallError::Rpc` rather than hanging.
async fn drain_chunked(
    shared: Arc<ClientShared>,
    response_id: u64,
    stream_id: u64,
    total_size: u64,
    original: oneshot::Sender<Result<Value, scev_wire::ErrorInfo>>,
) {
    let mut buf: Vec<u8> = Vec::with_capacity(total_size as usize);
    let mut offset: u64 = 0;
    let deadline = tokio::time::Instant::now() + CHUNK_DRAIN_TIMEOUT;

    while offset < total_size {
        let want = (total_size - offset).min(CHUNK_SLICE);
        let args = Value::Array(vec![
            Value::Integer(stream_id.into()),
            Value::Integer(offset.into()),
            Value::Integer(want.into()),
        ]);
        let remaining = deadline
            .checked_duration_since(tokio::time::Instant::now())
            .unwrap_or(Duration::from_secs(0));
        if remaining.is_zero() {
            let _ = original.send(Err(scev_wire::ErrorInfo::generic(
                "chunked drain timed out",
            )));
            return;
        }
        let result = match shared
            .call(scev_wire::methods::READ_CHUNK, args, remaining)
            .await
        {
            Ok(v) => v,
            Err(CallError::Rpc(info)) => {
                let _ = original.send(Err(info));
                return;
            }
            Err(e) => {
                let _ = original.send(Err(scev_wire::ErrorInfo::generic(format!(
                    "chunked drain failed: {e}"
                ))));
                return;
            }
        };
        let slice = match result {
            Value::Binary(b) => b,
            other => {
                let _ = original.send(Err(scev_wire::ErrorInfo::generic(format!(
                    "chunked drain: read_chunk returned non-bin: {other:?}"
                ))));
                return;
            }
        };
        if slice.is_empty() {
            // Host says EOF but we haven't filled total_size — short
            // payload. Treat as protocol error.
            let _ = original.send(Err(scev_wire::ErrorInfo::generic(format!(
                "chunked drain: EOF at {offset}/{total_size}"
            ))));
            return;
        }
        buf.extend_from_slice(&slice);
        offset += slice.len() as u64;
    }

    // The assembled bytes are exactly the original Response frame.
    let _ = (response_id, stream_id, total_size);
    let mut cur = Cursor::new(buf.as_slice());
    let val = match rmpv::decode::read_value(&mut cur) {
        Ok(v) => v,
        Err(e) => {
            let _ = original.send(Err(scev_wire::ErrorInfo::generic(format!(
                "chunked drain: assembled buffer didn't decode: {e}"
            ))));
            return;
        }
    };
    let assembled = match Frame::from_value(val) {
        Ok(f) => f,
        Err(e) => {
            let _ = original.send(Err(scev_wire::ErrorInfo::generic(format!(
                "chunked drain: assembled buffer wasn't a Frame: {e}"
            ))));
            return;
        }
    };
    match assembled {
        Frame::Response { err, result, .. } => {
            let outcome = match err {
                Some(e) => Err(e),
                None => Ok(result),
            };
            let _ = original.send(outcome);
        }
        other => {
            let _ = original.send(Err(scev_wire::ErrorInfo::generic(format!(
                "chunked drain: assembled non-Response frame: {other:?}"
            ))));
        }
    }
}
