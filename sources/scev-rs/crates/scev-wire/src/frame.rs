// SPDX-License-Identifier: MPL-2.0
//
// Typed view over the msgpack array body — daemon code does id rewriting
// against `Frame::Request { id, .. }` rather than poking into rmpv arrays
// by hand. `from_bytes` parses; `to_bytes` re-encodes.

use rmpv::Value;
use thiserror::Error;

use crate::proto::Tag;

#[derive(Debug, Clone)]
pub enum Frame {
    Request {
        id: u64,
        method: String,
        args: Value,
    },
    Response {
        id: u64,
        /// `Some` for an error response (msgpack string), `None` for success.
        err: Option<String>,
        result: Value,
    },
    Event {
        name: String,
        args: Value,
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
                let err = match err_v {
                    Value::Nil => None,
                    Value::String(s) => Some(s.into_str().unwrap_or_default()),
                    other => {
                        return Err(RpcError::TypeMismatch {
                            field: "err",
                            detail: format!("{other:?}"),
                        })
                    }
                };
                let result = it.next().unwrap();
                Ok(Frame::Response { id, err, result })
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
                match err {
                    None => Value::Nil,
                    Some(s) => Value::String(s.clone().into()),
                },
                result.clone(),
            ]),
            Frame::Event { name, args } => Value::Array(vec![
                Value::Integer((Tag::Event as i64).into()),
                Value::String(name.clone().into()),
                args.clone(),
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
    fn round_trip_response_err() {
        let f = Frame::Response {
            id: 1,
            err: Some("nope".into()),
            result: Value::Nil,
        };
        let bytes = f.to_bytes().unwrap();
        match Frame::from_bytes(&bytes).unwrap() {
            Frame::Response { err, .. } => assert_eq!(err.as_deref(), Some("nope")),
            _ => panic!("wrong variant"),
        }
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
