// SPDX-License-Identifier: MPL-2.0
//
// Typed view over the msgpack array body — daemon code does id rewriting
// against `Frame::Request { id, .. }` rather than poking into rmpv arrays
// by hand. `from_bytes` parses; `to_bytes` re-encodes.

use rmpv::Value;
use thiserror::Error;

use crate::proto::{errors, Tag};

/// Structured error payload carried in a [`Frame::Response::err`] slot.
/// `code` is one of [`crate::proto::errors`]; `message` is human-readable.
/// Decoded from either a `{code, message}` map (current host) or a bare
/// string (legacy/forward-compat — wrapped as `{GENERIC, message}`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorInfo {
    pub code: String,
    pub message: String,
}

impl ErrorInfo {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }

    /// Convenience: construct from a bare message with the GENERIC code.
    pub fn generic(message: impl Into<String>) -> Self {
        Self::new(errors::GENERIC, message)
    }
}

impl std::fmt::Display for ErrorInfo {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

#[derive(Debug, Clone)]
pub enum Frame {
    Request {
        id: u64,
        method: String,
        args: Value,
    },
    Response {
        id: u64,
        /// `Some` for an error response, `None` for success.
        err: Option<ErrorInfo>,
        result: Value,
    },
    Event {
        name: String,
        args: Value,
    },
    /// Marker frame the host sends in lieu of a Response whose encoded
    /// form exceeds the wire cap. Drain via successive `read_chunk`
    /// calls (see [`crate::proto::methods::READ_CHUNK`]) to assemble
    /// `total_size` bytes, then decode the buffer as a regular Response.
    Chunked {
        response_id: u64,
        stream_id: u64,
        total_size: u64,
    },
}

#[derive(Debug, Error)]
pub enum RpcError {
    #[error("frame is not a top-level array")]
    NotArray,
    #[error("frame is empty")]
    Empty,
    #[error("unknown tag {0}")]
    UnknownTag(i64),
    #[error("wrong arity for tag {tag:?}: expected {expected}, got {actual}")]
    WrongArity {
        tag: Tag,
        expected: usize,
        actual: usize,
    },
    #[error("type mismatch in field {field}: {detail}")]
    TypeMismatch { field: &'static str, detail: String },
    #[error("msgpack decode failed: {0}")]
    Decode(#[from] rmpv::decode::Error),
    #[error("msgpack encode failed: {0}")]
    Encode(#[from] rmpv::encode::Error),
}

impl Frame {
    /// Parse a decoded msgpack body (post-COBS) into a typed Frame.
    pub fn from_bytes(payload: &[u8]) -> Result<Self, RpcError> {
        let mut cur = std::io::Cursor::new(payload);
        let val = rmpv::decode::read_value(&mut cur)?;
        Self::from_value(val)
    }

    pub fn from_value(val: Value) -> Result<Self, RpcError> {
        let arr = match val {
            Value::Array(a) => a,
            _ => return Err(RpcError::NotArray),
        };
        if arr.is_empty() {
            return Err(RpcError::Empty);
        }
        let tag_raw = arr[0]
            .as_i64()
            .ok_or(RpcError::TypeMismatch {
                field: "tag",
                detail: "not an integer".into(),
            })?;
        let tag = Tag::from_i64(tag_raw).ok_or(RpcError::UnknownTag(tag_raw))?;

        match tag {
            Tag::Request => {
                if arr.len() != 4 {
                    return Err(RpcError::WrongArity {
                        tag,
                        expected: 4,
                        actual: arr.len(),
                    });
                }
                let mut it = arr.into_iter();
                let _ = it.next();
                let id = pull_u64(it.next().unwrap(), "id")?;
                let method = pull_string(it.next().unwrap(), "method")?;
                let args = it.next().unwrap();
                Ok(Frame::Request { id, method, args })
            }
            Tag::Response => {
                if arr.len() != 4 {
                    return Err(RpcError::WrongArity {
                        tag,
                        expected: 4,
                        actual: arr.len(),
                    });
                }
                let mut it = arr.into_iter();
                let _ = it.next();
                let id = pull_u64(it.next().unwrap(), "id")?;
                let err_v = it.next().unwrap();
                let err = decode_err_slot(err_v)?;
                let result = it.next().unwrap();
                Ok(Frame::Response { id, err, result })
            }
            Tag::Chunked => {
                if arr.len() != 4 {
                    return Err(RpcError::WrongArity {
                        tag,
                        expected: 4,
                        actual: arr.len(),
                    });
                }
                let mut it = arr.into_iter();
                let _ = it.next();
                let response_id = pull_u64(it.next().unwrap(), "response_id")?;
                let stream_id = pull_u64(it.next().unwrap(), "stream_id")?;
                let total_size = pull_u64(it.next().unwrap(), "total_size")?;
                Ok(Frame::Chunked {
                    response_id,
                    stream_id,
                    total_size,
                })
            }
            Tag::Event => {
                if arr.len() != 3 {
                    return Err(RpcError::WrongArity {
                        tag,
                        expected: 3,
                        actual: arr.len(),
                    });
                }
                let mut it = arr.into_iter();
                let _ = it.next();
                let name = pull_string(it.next().unwrap(), "name")?;
                let args = it.next().unwrap();
                Ok(Frame::Event { name, args })
            }
        }
    }

    pub fn to_value(&self) -> Value {
        match self {
            Frame::Request { id, method, args } => Value::Array(vec![
                Value::Integer((Tag::Request as i64).into()),
                Value::Integer((*id).into()),
                Value::String(method.clone().into()),
                args.clone(),
            ]),
            Frame::Response { id, err, result } => Value::Array(vec![
                Value::Integer((Tag::Response as i64).into()),
                Value::Integer((*id).into()),
                encode_err_slot(err.as_ref()),
                result.clone(),
            ]),
            Frame::Event { name, args } => Value::Array(vec![
                Value::Integer((Tag::Event as i64).into()),
                Value::String(name.clone().into()),
                args.clone(),
            ]),
            Frame::Chunked {
                response_id,
                stream_id,
                total_size,
            } => Value::Array(vec![
                Value::Integer((Tag::Chunked as i64).into()),
                Value::Integer((*response_id).into()),
                Value::Integer((*stream_id).into()),
                Value::Integer((*total_size).into()),
            ]),
        }
    }

    pub fn to_bytes(&self) -> Result<Vec<u8>, RpcError> {
        let val = self.to_value();
        let mut buf = Vec::new();
        rmpv::encode::write_value(&mut buf, &val)?;
        Ok(buf)
    }
}

/// Decode the err slot of a Response frame. Accepts:
///  * `Value::Nil` → success (no error)
///  * `Value::Map { code, message }` → structured error (current host)
///  * `Value::String` → bare message (legacy / forward-compat —
///    wrapped as `{ code: GENERIC, message: <str> }`)
///
/// Anything else is a type mismatch.
fn decode_err_slot(v: Value) -> Result<Option<ErrorInfo>, RpcError> {
    match v {
        Value::Nil => Ok(None),
        Value::String(s) => Ok(Some(ErrorInfo::generic(
            s.into_str().unwrap_or_default(),
        ))),
        Value::Map(entries) => {
            let mut code: Option<String> = None;
            let mut message: Option<String> = None;
            for (k, v) in entries {
                let key = match k {
                    Value::String(s) => s.into_str().unwrap_or_default(),
                    _ => continue,
                };
                match key.as_str() {
                    "code" => {
                        if let Value::String(s) = v {
                            code = Some(s.into_str().unwrap_or_default());
                        }
                    }
                    "message" => {
                        if let Value::String(s) = v {
                            message = Some(s.into_str().unwrap_or_default());
                        }
                    }
                    _ => { /* ignore unknown keys for forward-compat */ }
                }
            }
            Ok(Some(ErrorInfo {
                code: code.unwrap_or_else(|| errors::GENERIC.into()),
                message: message.unwrap_or_default(),
            }))
        }
        other => Err(RpcError::TypeMismatch {
            field: "err",
            detail: format!("{other:?}"),
        }),
    }
}

fn encode_err_slot(err: Option<&ErrorInfo>) -> Value {
    match err {
        None => Value::Nil,
        Some(info) => Value::Map(vec![
            (
                Value::String("code".into()),
                Value::String(info.code.clone().into()),
            ),
            (
                Value::String("message".into()),
                Value::String(info.message.clone().into()),
            ),
        ]),
    }
}

fn pull_u64(v: Value, field: &'static str) -> Result<u64, RpcError> {
    v.as_u64().ok_or_else(|| RpcError::TypeMismatch {
        field,
        detail: format!("{v:?} not a u64"),
    })
}

fn pull_string(v: Value, field: &'static str) -> Result<String, RpcError> {
    match v {
        Value::String(s) => Ok(s.into_str().unwrap_or_default()),
        other => Err(RpcError::TypeMismatch {
            field,
            detail: format!("{other:?} not a string"),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_request() {
        let f = Frame::Request {
            id: 7,
            method: "ping".into(),
            args: Value::Array(vec![]),
        };
        let bytes = f.to_bytes().unwrap();
        match Frame::from_bytes(&bytes).unwrap() {
            Frame::Request { id, method, args } => {
                assert_eq!(id, 7);
                assert_eq!(method, "ping");
                assert!(matches!(args, Value::Array(ref a) if a.is_empty()));
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn round_trip_response_ok() {
        let f = Frame::Response {
            id: 1,
            err: None,
            result: Value::String("pong".into()),
        };
        let bytes = f.to_bytes().unwrap();
        match Frame::from_bytes(&bytes).unwrap() {
            Frame::Response { id, err, result } => {
                assert_eq!(id, 1);
                assert!(err.is_none());
                assert_eq!(result.as_str(), Some("pong"));
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn round_trip_response_err_structured() {
        let f = Frame::Response {
            id: 1,
            err: Some(ErrorInfo::new(errors::BAD_ARGS, "missing peer")),
            result: Value::Nil,
        };
        let bytes = f.to_bytes().unwrap();
        match Frame::from_bytes(&bytes).unwrap() {
            Frame::Response { err, .. } => {
                let info = err.expect("error present");
                assert_eq!(info.code, errors::BAD_ARGS);
                assert_eq!(info.message, "missing peer");
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn decodes_legacy_string_err_as_generic() {
        // Hand-encode `[1, 7, "boom", nil]` and verify the decoder
        // accepts the bare-string err slot for back-compat.
        let val = Value::Array(vec![
            Value::Integer(1.into()),
            Value::Integer(7u64.into()),
            Value::String("boom".into()),
            Value::Nil,
        ]);
        let mut buf = Vec::new();
        rmpv::encode::write_value(&mut buf, &val).unwrap();
        match Frame::from_bytes(&buf).unwrap() {
            Frame::Response { id, err, .. } => {
                assert_eq!(id, 7);
                let info = err.expect("legacy string promotes to ErrorInfo");
                assert_eq!(info.code, errors::GENERIC);
                assert_eq!(info.message, "boom");
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn round_trip_chunked() {
        let f = Frame::Chunked {
            response_id: 42,
            stream_id: 7,
            total_size: 100_000,
        };
        let bytes = f.to_bytes().unwrap();
        match Frame::from_bytes(&bytes).unwrap() {
            Frame::Chunked {
                response_id,
                stream_id,
                total_size,
            } => {
                assert_eq!(response_id, 42);
                assert_eq!(stream_id, 7);
                assert_eq!(total_size, 100_000);
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn ignores_unknown_keys_in_err_map_for_forward_compat() {
        // Host adds a `detail` field later; current decoder should
        // still extract code+message and ignore the rest.
        let val = Value::Array(vec![
            Value::Integer(1.into()),
            Value::Integer(1u64.into()),
            Value::Map(vec![
                (
                    Value::String("code".into()),
                    Value::String("future_code".into()),
                ),
                (
                    Value::String("message".into()),
                    Value::String("future error".into()),
                ),
                (
                    Value::String("detail".into()),
                    Value::Array(vec![Value::Integer(1.into())]),
                ),
            ]),
            Value::Nil,
        ]);
        let mut buf = Vec::new();
        rmpv::encode::write_value(&mut buf, &val).unwrap();
        let info = match Frame::from_bytes(&buf).unwrap() {
            Frame::Response { err, .. } => err.expect("error present"),
            _ => panic!(),
        };
        assert_eq!(info.code, "future_code");
        assert_eq!(info.message, "future error");
    }

    #[test]
    fn round_trip_event() {
        let f = Frame::Event {
            name: "modem_message".into(),
            args: Value::Array(vec![Value::String("left".into()), Value::Integer(15.into())]),
        };
        let bytes = f.to_bytes().unwrap();
        match Frame::from_bytes(&bytes).unwrap() {
            Frame::Event { name, args } => {
                assert_eq!(name, "modem_message");
                let arr = args.as_array().unwrap();
                assert_eq!(arr[0].as_str(), Some("left"));
                assert_eq!(arr[1].as_i64(), Some(15));
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn rejects_unknown_tag() {
        let bad = Value::Array(vec![Value::Integer(99.into())]);
        let mut buf = Vec::new();
        rmpv::encode::write_value(&mut buf, &bad).unwrap();
        assert!(matches!(
            Frame::from_bytes(&buf),
            Err(RpcError::UnknownTag(99))
        ));
    }
}
