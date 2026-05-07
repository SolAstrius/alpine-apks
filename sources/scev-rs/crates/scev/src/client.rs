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
// On a client-side timeout we fire a best-effort `cancel(id)` so the
// host's coroutine isn't left running unwatched. On chunked-drain
// abort (any error mid-drain) we fire a best-effort
// `discard_chunk(stream_id)` so the host's slab cache is freed early
// rather than waiting for the TTL.
//
// `Client::handshake` runs once after construction to fetch
// `self.protocol_version` / `self.capabilities` / `self.limits`. The
// rest of the surface (subscribe-by-name, batch, cancel, …) gates on
// the capability flags so older hosts degrade cleanly.
//
// The CLI uses block_on around `call()` for sync-feeling subcommands
// and an async loop for events / modem-call.

use std::collections::{HashMap, HashSet};
use std::io::Cursor;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
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

/// Snapshot of `self.protocol_version` / `self.capabilities` /
/// `self.limits` returned by [`Client::handshake`]. Stored once on the
/// `ClientShared` and read from every gating site.
///
/// Pre-bump hosts (no `protocol_version` field) get
/// `protocol_version = 0`, an empty capability set, and the legacy
/// 8 KiB frame cap; everything that wants opt-in features will see
/// `has("name") == false` and fall back to the unconditional path.
#[derive(Debug, Clone, Default)]
pub struct Caps {
    pub protocol_version: u64,
    pub capabilities: HashSet<String>,
    pub frame_max_bytes: u64,
}

impl Caps {
    pub fn has(&self, name: &str) -> bool {
        self.capabilities.contains(name)
    }
}

/// Inner state shared between `Client` and any drain tasks it spawns.
/// Drain tasks need `next_id`/`pending`/`out_tx` to issue their own
/// `read_chunk` calls; pulling those out into an `Arc<ClientShared>`
/// lets the drainer participate in the normal call/response plumbing
/// without the Client struct having to be `Arc`-shared by callers.
struct ClientShared {
    /// Atomic so fire-and-forget paths (cancel, discard_chunk) can
    /// allocate ids without holding a lock across await — keeps the
    /// resulting fire-and-forget futures Send-able and the sync
    /// callers cheap.
    next_id: AtomicU64,
    pending: Mutex<HashMap<u64, oneshot::Sender<Result<Value, scev_wire::ErrorInfo>>>>,
    out_tx: mpsc::Sender<Frame>,
    /// Set once by [`Client::handshake`]. Empty when handshake hasn't
    /// run (tests, library users that opt out) — every gating site
    /// treats "no caps known" as "feature unavailable".
    caps: OnceLock<Caps>,
}

impl ClientShared {
    fn alloc_id(&self) -> u64 {
        let v = self.next_id.fetch_add(1, Ordering::Relaxed);
        if v == 0 {
            self.next_id.fetch_add(1, Ordering::Relaxed)
        } else {
            v
        }
    }

    async fn call(
        &self,
        method: &str,
        args: Value,
        timeout: Duration,
    ) -> Result<Value, CallError> {
        let id = self.alloc_id();
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
                // Fire-and-forget cancel: the host's coroutine is
                // still running on its end. Push the request frame
                // straight onto the writer queue (no pending entry
                // registered → response is silently dropped). We use
                // try_send so a full out_tx queue can't make the
                // timeout path itself block.
                if self.has_cap("cancel") {
                    self.fire_and_forget(scev_wire::methods::CANCEL, id);
                }
                Err(CallError::Timeout)
            }
        }
    }

    fn has_cap(&self, name: &str) -> bool {
        self.caps.get().map(|c| c.has(name)).unwrap_or(false)
    }

    /// Send a `[method, [target_id]]` request without waiting for the
    /// response. Used for `cancel(id)` and `discard_chunk(stream_id)`,
    /// both of which are best-effort cleanup. The response (if any)
    /// lands in the reader task and gets dropped because we never
    /// register a pending entry for the synthetic id we allocated.
    fn fire_and_forget(&self, method: &'static str, target_id: u64) {
        let id = self.alloc_id();
        let frame = Frame::Request {
            id,
            method: method.into(),
            args: Value::Array(vec![Value::Integer(target_id.into())]),
        };
        // try_send: if the writer queue is backed up, silently drop.
        // The host will reclaim the resource on its TTL anyway; we
        // tried.
        let _ = self.out_tx.try_send(frame);
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
            next_id: AtomicU64::new(1),
            pending: Mutex::new(HashMap::new()),
            out_tx,
            caps: OnceLock::new(),
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
                                // Best-effort tell the host to free
                                // the slab so we don't pay TTL latency
                                // on cleanup.
                                shared.fire_and_forget(
                                    scev_wire::methods::DISCARD_CHUNK,
                                    stream_id,
                                );
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

    /// Run the `self` round-trip and stash protocol_version /
    /// capabilities / limits.frame_max_bytes for later gating. Idempotent
    /// — second call is a no-op (OnceLock semantics). On any error
    /// (very old host, connection blip, malformed response) install a
    /// safe default Caps so callers can still proceed without
    /// crashing.
    pub async fn handshake(&self) -> Caps {
        if let Some(c) = self.shared.caps.get() {
            return c.clone();
        }
        let caps = match self
            .shared
            .call(scev_wire::methods::SELF_, Value::Array(vec![]), Duration::from_secs(3))
            .await
        {
            Ok(v) => parse_caps(&v),
            Err(_) => Caps::default(),
        };
        // OnceLock::set returns Err if another task beat us; either
        // outcome is fine — both caps values would be derived from a
        // recent self call.
        let _ = self.shared.caps.set(caps.clone());
        caps
    }

    #[allow(dead_code)]
    pub fn caps(&self) -> Caps {
        self.shared.caps.get().cloned().unwrap_or_default()
    }

    pub fn has_capability(&self, name: &str) -> bool {
        self.shared.has_cap(name)
    }

    /// Subscribe to events by name. Empty `names` means wildcard /
    /// pre-handshake-style "send everything". Server-side filtering
    /// only kicks in when the host advertises `event_subscriptions`;
    /// older hosts treat this as a no-op subscribe and we get every
    /// event regardless. Returns the host's `{filter: nil | [name,…]}`
    /// echo so callers can confirm what stuck.
    pub async fn subscribe(&self, names: &[&str]) -> Result<Value, CallError> {
        let args = if names.is_empty() {
            Value::Array(vec![])
        } else {
            Value::Array(vec![Value::Array(
                names
                    .iter()
                    .map(|n| Value::String((*n).into()))
                    .collect(),
            )])
        };
        self.shared
            .call(scev_wire::methods::SUBSCRIBE, args, Duration::from_secs(3))
            .await
    }

    pub async fn unsubscribe(&self, names: &[&str]) -> Result<Value, CallError> {
        let args = if names.is_empty() {
            Value::Array(vec![])
        } else {
            Value::Array(vec![Value::Array(
                names
                    .iter()
                    .map(|n| Value::String((*n).into()))
                    .collect(),
            )])
        };
        self.shared
            .call(scev_wire::methods::UNSUBSCRIBE, args, Duration::from_secs(3))
            .await
    }

    /// Ordered batch dispatch. `items` is `(method, args)` pairs.
    /// Returns one `Result<Value, ErrorInfo>` per input item, in
    /// order. With `stop_on_error = true`, items after the first
    /// error come back as `errors::SKIPPED`.
    pub async fn batch(
        &self,
        items: Vec<(String, Value)>,
        stop_on_error: bool,
    ) -> Result<Vec<Result<Value, scev_wire::ErrorInfo>>, CallError> {
        self.batch_inner(scev_wire::methods::BATCH, items, stop_on_error)
            .await
    }

    /// Parallel batch dispatch — same envelope shape, items run
    /// concurrently on the host. `stop_on_error` doesn't apply
    /// (always runs every item).
    pub async fn batch_par(
        &self,
        items: Vec<(String, Value)>,
    ) -> Result<Vec<Result<Value, scev_wire::ErrorInfo>>, CallError> {
        self.batch_inner(scev_wire::methods::BATCH_PAR, items, false)
            .await
    }

    async fn batch_inner(
        &self,
        method: &'static str,
        items: Vec<(String, Value)>,
        stop_on_error: bool,
    ) -> Result<Vec<Result<Value, scev_wire::ErrorInfo>>, CallError> {
        let item_arr = Value::Array(
            items
                .into_iter()
                .map(|(m, a)| Value::Array(vec![Value::String(m.into()), a]))
                .collect(),
        );
        let mut args = vec![item_arr];
        if method == scev_wire::methods::BATCH && stop_on_error {
            args.push(Value::Map(vec![(
                Value::String("stop_on_error".into()),
                Value::Boolean(true),
            )]));
        }
        // Generous timeout: a fanout of N items can legitimately take
        // up to N * per-item budget on the host. Caller can wrap in
        // their own tokio::time::timeout if they want stricter.
        let v = self
            .shared
            .call(method, Value::Array(args), Duration::from_secs(60))
            .await?;
        Ok(parse_batch_envelope(&v))
    }

    /// Convenience: send a request without expecting a response. Used
    /// when fire-and-forget would race a follow-up await on events
    /// (modem-call doesn't, in the daemonized world — but the helper
    /// is handy regardless).
    #[allow(dead_code)]
    pub async fn send_oneway(&self, method: &str, args: Value) -> Result<()> {
        let id = self.shared.alloc_id();
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

/// Parse the `{protocol_version, capabilities: {flag: bool, …},
/// limits: {frame_max_bytes: int, …}}` shape returned by the host's
/// `self` RPC. Anything missing falls back to the default — pre-bump
/// hosts (no protocol_version, no capabilities, no limits) get a
/// conservative Caps with version 0 and an empty cap set so every
/// gated feature stays disabled.
fn parse_caps(v: &Value) -> Caps {
    let Value::Map(map) = v else {
        return Caps::default();
    };
    let mut out = Caps::default();
    for (k, val) in map {
        let key = match k {
            Value::String(s) => s.as_str().unwrap_or(""),
            _ => continue,
        };
        match key {
            "protocol_version" => {
                if let Some(n) = val.as_u64() {
                    out.protocol_version = n;
                }
            }
            "capabilities" => {
                if let Value::Map(caps) = val {
                    for (ck, cv) in caps {
                        if let (Some(name), Some(true)) = (ck.as_str(), cv.as_bool()) {
                            out.capabilities.insert(name.to_string());
                        }
                    }
                }
            }
            "limits" => {
                if let Value::Map(lim) = val {
                    for (lk, lv) in lim {
                        if lk.as_str() == Some("frame_max_bytes") {
                            if let Some(n) = lv.as_u64() {
                                out.frame_max_bytes = n;
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }
    out
}

/// `batch` / `batch_par` return an array of `[err_or_nil, result]`
/// pairs. Map each pair to a `Result<Value, ErrorInfo>` so callers
/// can iterate naturally. `errors::SKIPPED` entries surface as `Err`
/// — same shape as a real error, with the SKIPPED code so callers can
/// branch on it if they care.
fn parse_batch_envelope(v: &Value) -> Vec<Result<Value, scev_wire::ErrorInfo>> {
    let Some(arr) = v.as_array() else {
        return Vec::new();
    };
    arr.iter()
        .map(|entry| {
            let Some(pair) = entry.as_array() else {
                return Err(scev_wire::ErrorInfo::generic("batch item not an array"));
            };
            if pair.len() < 2 {
                return Err(scev_wire::ErrorInfo::generic("batch item shorter than 2"));
            }
            match &pair[0] {
                Value::Nil => Ok(pair[1].clone()),
                Value::Map(em) => {
                    let mut code = scev_wire::errors::GENERIC.to_string();
                    let mut message = String::new();
                    for (k, val) in em {
                        match k.as_str() {
                            Some("code") => {
                                if let Some(s) = val.as_str() {
                                    code = s.to_string();
                                }
                            }
                            Some("message") => {
                                if let Some(s) = val.as_str() {
                                    message = s.to_string();
                                }
                            }
                            _ => {}
                        }
                    }
                    Err(scev_wire::ErrorInfo::new(code, message))
                }
                Value::String(s) => {
                    Err(scev_wire::ErrorInfo::generic(s.as_str().unwrap_or("")))
                }
                _ => Err(scev_wire::ErrorInfo::generic("batch err slot wrong shape")),
            }
        })
        .collect()
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
/// a clean `CallError::Rpc` rather than hanging — and fires a
/// fire-and-forget `discard_chunk` so the host's slab cache is freed
/// promptly instead of waiting for TTL eviction.
async fn drain_chunked(
    shared: Arc<ClientShared>,
    response_id: u64,
    stream_id: u64,
    total_size: u64,
    original: oneshot::Sender<Result<Value, scev_wire::ErrorInfo>>,
) {
    // Helper closure: send a fire-and-forget discard_chunk before
    // resolving with `err`. Synchronous (try_send) so the resolve
    // happens immediately after.
    let abort = |err: scev_wire::ErrorInfo,
                 original: oneshot::Sender<Result<Value, scev_wire::ErrorInfo>>,
                 shared: Arc<ClientShared>| {
        shared.fire_and_forget(scev_wire::methods::DISCARD_CHUNK, stream_id);
        let _ = original.send(Err(err));
    };

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
            abort(
                scev_wire::ErrorInfo::generic("chunked drain timed out"),
                original,
                shared,
            );
            return;
        }
        let result = match shared
            .call(scev_wire::methods::READ_CHUNK, args, remaining)
            .await
        {
            Ok(v) => v,
            Err(CallError::Rpc(info)) => {
                // Host-side error (NO_SUCH_PEER for an evicted/expired
                // stream, etc.). Don't discard — the stream's already
                // gone from the host's perspective.
                let _ = original.send(Err(info));
                return;
            }
            Err(e) => {
                abort(
                    scev_wire::ErrorInfo::generic(format!("chunked drain failed: {e}")),
                    original,
                    shared,
                );
                return;
            }
        };
        let slice = match result {
            Value::Binary(b) => b,
            other => {
                abort(
                    scev_wire::ErrorInfo::generic(format!(
                        "chunked drain: read_chunk returned non-bin: {other:?}"
                    )),
                    original,
                    shared,
                );
                return;
            }
        };
        if slice.is_empty() {
            // Host says EOF but we haven't filled total_size — short
            // payload. Treat as protocol error.
            abort(
                scev_wire::ErrorInfo::generic(format!(
                    "chunked drain: EOF at {offset}/{total_size}"
                )),
                original,
                shared,
            );
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
            abort(
                scev_wire::ErrorInfo::generic(format!(
                    "chunked drain: assembled buffer didn't decode: {e}"
                )),
                original,
                shared,
            );
            return;
        }
    };
    let assembled = match Frame::from_value(val) {
        Ok(f) => f,
        Err(e) => {
            abort(
                scev_wire::ErrorInfo::generic(format!(
                    "chunked drain: assembled buffer wasn't a Frame: {e}"
                )),
                original,
                shared,
            );
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
            abort(
                scev_wire::ErrorInfo::generic(format!(
                    "chunked drain: assembled non-Response frame: {other:?}"
                )),
                original,
                shared,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_caps_full_shape() {
        let v = Value::Map(vec![
            (
                Value::String("protocol_version".into()),
                Value::Integer(1u64.into()),
            ),
            (
                Value::String("capabilities".into()),
                Value::Map(vec![
                    (Value::String("batch".into()), Value::Boolean(true)),
                    (Value::String("cancel".into()), Value::Boolean(true)),
                    (
                        Value::String("event_subscriptions".into()),
                        Value::Boolean(false),
                    ),
                ]),
            ),
            (
                Value::String("limits".into()),
                Value::Map(vec![(
                    Value::String("frame_max_bytes".into()),
                    Value::Integer(65536u64.into()),
                )]),
            ),
        ]);
        let c = parse_caps(&v);
        assert_eq!(c.protocol_version, 1);
        assert!(c.has("batch"));
        assert!(c.has("cancel"));
        assert!(!c.has("event_subscriptions"));
        assert_eq!(c.frame_max_bytes, 65536);
    }

    #[test]
    fn parse_caps_legacy_host() {
        let v = Value::Map(vec![(
            Value::String("id".into()),
            Value::Integer(42i64.into()),
        )]);
        let c = parse_caps(&v);
        assert_eq!(c.protocol_version, 0);
        assert!(c.capabilities.is_empty());
        assert_eq!(c.frame_max_bytes, 0);
    }

    #[test]
    fn parse_batch_envelope_mixed() {
        let v = Value::Array(vec![
            Value::Array(vec![Value::Nil, Value::String("ok".into())]),
            Value::Array(vec![
                Value::Map(vec![
                    (
                        Value::String("code".into()),
                        Value::String(scev_wire::errors::BAD_ARGS.into()),
                    ),
                    (
                        Value::String("message".into()),
                        Value::String("missing arg".into()),
                    ),
                ]),
                Value::Nil,
            ]),
            Value::Array(vec![
                Value::Map(vec![
                    (
                        Value::String("code".into()),
                        Value::String(scev_wire::errors::SKIPPED.into()),
                    ),
                    (
                        Value::String("message".into()),
                        Value::String("skipped".into()),
                    ),
                ]),
                Value::Nil,
            ]),
        ]);
        let parsed = parse_batch_envelope(&v);
        assert_eq!(parsed.len(), 3);
        assert_eq!(parsed[0].as_ref().unwrap().as_str(), Some("ok"));
        let e1 = parsed[1].as_ref().err().unwrap();
        assert_eq!(e1.code, scev_wire::errors::BAD_ARGS);
        assert_eq!(e1.message, "missing arg");
        let e2 = parsed[2].as_ref().err().unwrap();
        assert_eq!(e2.code, scev_wire::errors::SKIPPED);
    }
}
