// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! Frame codec + protocol constants for the scev guest RPC.
//!
//! Wire layout (after COBS decode) is a msgpack array, tagged by the
//! first element:
//!
//!   `[TAG_REQUEST,  id, method,  args]` — guest → host
//!   `[TAG_RESPONSE, id, err|nil, result]` — host → guest
//!   `[TAG_EVENT,        name,    args]`   — host → guest, async
//!
//! The same frame format rides on:
//!   * `/dev/ttyS1` — what the host emulator speaks directly
//!   * `/run/scevd.sock` — the daemon's per-client multiplex; daemon
//!     rewrites correlation ids so multiple clients can share one
//!     serial fd without clobbering each other's call/response pairs.
//!
//! The byte-for-byte equivalence with the existing Zig + Python
//! implementations is the conformance contract; the cobs/mpack tests
//! in those trees are mirrored here.

#![deny(rust_2018_idioms)]

pub mod codec;
pub mod frame;
pub mod proto;
pub mod tokio_codec;

pub use codec::{decode_frame, encode_frame, FrameCodecError, MAX_FRAME};
pub use frame::{Frame, RpcError};
pub use proto::{methods, Tag};
pub use tokio_codec::{CodecError, FrameCodec};

// Re-export rmpv so downstream crates can build/inspect Values without
// pulling rmpv into their own Cargo.toml. Keeps the dep graph centralized.
pub use rmpv::{self, Value};
