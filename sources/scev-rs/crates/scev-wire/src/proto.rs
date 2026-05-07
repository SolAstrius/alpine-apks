// SPDX-License-Identifier: MPL-2.0
//
// Protocol constants — kept identical to `sources/scev/src/rpc.zig` and
// `sources/py-scev/src/scev/_rpc.py` so all three clients can talk to
// the same host. Treat this file as the source of truth on the Rust side.

/// Tag values prefixed onto every frame.
#[repr(i64)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tag {
    Request = 0,
    Response = 1,
    Event = 2,
    /// Sent by the host in lieu of a Response whose encoded form would
    /// exceed the wire frame cap. Wire shape:
    /// `[3, response_id, stream_id, total_size]`. The receiver fetches
    /// `total_size` bytes via `read_chunk(stream_id, ...)` calls and
    /// decodes the assembled buffer as a regular Response.
    Chunked = 3,
}

impl Tag {
    pub fn from_i64(v: i64) -> Option<Self> {
        match v {
            0 => Some(Tag::Request),
            1 => Some(Tag::Response),
            2 => Some(Tag::Event),
            3 => Some(Tag::Chunked),
            _ => None,
        }
    }
}

/// Method names — the host dispatches on these. Kept as `&str` constants
/// rather than an enum because new methods land on the host side without
/// guest releases (e.g. introspection methods added during development).
pub mod methods {
    pub const PING: &str = "ping";
    pub const LOG: &str = "log";
    pub const LIST: &str = "list";
    pub const METHODS: &str = "methods";
    pub const CALL: &str = "call";
    pub const QUEUE_EVENT: &str = "queue_event";
    pub const SUBSCRIBE: &str = "subscribe";
    pub const UNSUBSCRIBE: &str = "unsubscribe";
    pub const DESCRIBE: &str = "describe";
    pub const SCHEMA: &str = "schema";
    pub const TYPE: &str = "type";
    pub const TRACE: &str = "trace";
    pub const SELF_: &str = "self";
    /// Pull a slice of a chunked response cached on the host. Args:
    /// `(stream_id: int, offset: int, max_len: int) -> bin`.
    pub const READ_CHUNK: &str = "read_chunk";
    /// Discard a chunked response early. Args: `(stream_id: int) -> bool`.
    pub const DISCARD_CHUNK: &str = "discard_chunk";
    /// Ordered batch dispatch — runs N `(method, args)` pairs serially
    /// on the host in one request, returning per-item `[err, result]`
    /// envelopes in input order. Args:
    /// `(items: array<[method, args]>, opts?: { stop_on_error: bool })
    /// -> array<[err_or_nil, result_or_nil]>`.
    pub const BATCH: &str = "batch";
}

/// Stable string codes the host emits in the structured error map.
/// Mirrors `lekkit.scev.core.rpc.RpcErrors`. Treat unknown codes as
/// [GENERIC] for branching purposes; always show [`ErrorInfo::message`]
/// to the user regardless.
pub mod errors {
    pub const GENERIC: &str = "rpc_error";
    pub const BAD_ARGS: &str = "bad_args";
    pub const NO_SUCH_METHOD: &str = "no_such_method";
    pub const NO_SUCH_PEER: &str = "no_such_peer";
    pub const LUA_ERROR: &str = "lua_error";
    pub const RUNTIME_ERROR: &str = "runtime_error";
    pub const INTERNAL_ERROR: &str = "internal_error";
    pub const NOT_INSTALLED: &str = "not_installed";
    pub const UNSUPPORTED: &str = "unsupported";
    pub const FRAME_TOO_LARGE: &str = "frame_too_large";
    /// Item slot in a `batch` response that didn't run because an
    /// earlier item errored and `stop_on_error` was set.
    pub const SKIPPED: &str = "skipped";
}
