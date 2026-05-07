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
}

impl Tag {
    pub fn from_i64(v: i64) -> Option<Self> {
        match v {
            0 => Some(Tag::Request),
            1 => Some(Tag::Response),
            2 => Some(Tag::Event),
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
}
