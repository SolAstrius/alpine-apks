// SPDX-License-Identifier: MPL-2.0
//
// Typed-arg parser + value helpers. Same `t:value` prefix syntax the
// Zig and Python CLIs use — kept identical so muscle memory transfers
// between the binaries.

use anyhow::{anyhow, Result};
use scev_wire::Value;

/// Parse one positional token into a msgpack Value.
///
/// Prefixes: s/i/f/b/n/j (`j:` is a Python-CLI extension carried over —
/// decodes any JSON value, the only way to send tables/lists from a
/// shell without escaping nightmares).
///
/// Bare tokens (no recognised prefix) become strings — matches the Zig
/// CLI's lenient default so `scev call modem_0 transmit 15 43 hello`
/// works as expected.
pub fn parse_typed(tok: &str) -> Result<Value> {
    if tok.len() >= 2 && tok.as_bytes()[1] == b':' {
        let v = &tok[2..];
        match tok.as_bytes()[0] {
            b's' => return Ok(Value::String(v.into())),
            b'i' => {
                let n: i64 = v.parse().map_err(|_| anyhow!("not an int: {tok}"))?;
                return Ok(Value::Integer(n.into()));
            }
            b'f' => {
                let f: f64 = v.parse().map_err(|_| anyhow!("not a float: {tok}"))?;
                return Ok(Value::F64(f));
            }
            b'b' => {
                let b = matches!(v, "true" | "1");
                return Ok(Value::Boolean(b));
            }
            b'n' => return Ok(Value::Nil),
            b'j' => {
                let json: serde_json::Value =
                    serde_json::from_str(v).map_err(|e| anyhow!("bad JSON in {tok}: {e}"))?;
                return Ok(json_to_msgpack(json));
            }
            _ => {}
        }
    }
    Ok(Value::String(tok.into()))
}

/// Convert a serde_json::Value tree into rmpv::Value. Used for `j:`
/// args; lossless for the JSON shapes CC's Lua tables map to.
pub fn json_to_msgpack(v: serde_json::Value) -> Value {
    match v {
        serde_json::Value::Null => Value::Nil,
        serde_json::Value::Bool(b) => Value::Boolean(b),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Value::Integer(i.into())
            } else if let Some(u) = n.as_u64() {
                Value::Integer(u.into())
            } else {
                Value::F64(n.as_f64().unwrap_or(0.0))
            }
        }
        serde_json::Value::String(s) => Value::String(s.into()),
        serde_json::Value::Array(a) => {
            Value::Array(a.into_iter().map(json_to_msgpack).collect())
        }
        serde_json::Value::Object(m) => Value::Map(
            m.into_iter()
                .map(|(k, v)| (Value::String(k.into()), json_to_msgpack(v)))
                .collect(),
        ),
    }
}

/// Convert rmpv::Value to serde_json::Value for output. Map keys that
/// aren't strings are stringified (Lua tables can have non-string keys
/// — JSON can't, so we coerce).
pub fn msgpack_to_json(v: &Value) -> serde_json::Value {
    use serde_json::{Number, Value as J};
    match v {
        Value::Nil => J::Null,
        Value::Boolean(b) => J::Bool(*b),
        Value::Integer(i) => {
            if let Some(n) = i.as_i64() {
                J::Number(n.into())
            } else if let Some(u) = i.as_u64() {
                J::Number(u.into())
            } else {
                J::String(i.to_string())
            }
        }
        Value::F32(f) => Number::from_f64(*f as f64).map(J::Number).unwrap_or(J::Null),
        Value::F64(f) => Number::from_f64(*f).map(J::Number).unwrap_or(J::Null),
        Value::String(s) => J::String(s.as_str().unwrap_or_default().to_string()),
        Value::Binary(b) => J::String(format!("<bin:{}>", b.len())),
        Value::Array(a) => J::Array(a.iter().map(msgpack_to_json).collect()),
        Value::Map(m) => {
            let mut obj = serde_json::Map::new();
            for (k, val) in m {
                let key = match k {
                    Value::String(s) => s.as_str().unwrap_or_default().to_string(),
                    other => format!("{other:?}"),
                };
                obj.insert(key, msgpack_to_json(val));
            }
            J::Object(obj)
        }
        Value::Ext(_, _) => J::Null,
    }
}

/// Print a Value as a single line of JSON. Used by the generic
/// subcommands (ping/log/self/call/queue/schema/trace) where a single
/// machine-readable line is the right output shape.
pub fn dump_json(v: &Value) {
    println!("{}", serde_json::to_string(&msgpack_to_json(v)).unwrap_or_else(|_| "null".into()));
}

/// As above, but for serde_json::Value directly.
#[allow(dead_code)]
pub fn dump_json_serde(v: &serde_json::Value) {
    println!("{}", serde_json::to_string(v).unwrap_or_else(|_| "null".into()));
}

/// Pull a list-of-strings out of a Value, with a clear error if the
/// host returned the wrong shape. Used by `methods` etc. where the
/// host's contract is "array of strings".
pub fn as_string_array(v: &Value) -> Result<Vec<String>> {
    let arr = v
        .as_array()
        .ok_or_else(|| anyhow!("expected array, got {v:?}"))?;
    arr.iter()
        .map(|e| {
            e.as_str()
                .map(|s| s.to_string())
                .ok_or_else(|| anyhow!("array element {e:?} not a string"))
        })
        .collect()
}

/// `[{peer, types: [..]}]` → vec of (peer, types) pairs. Used by `list`,
/// `find`, `methods-like`.
pub fn parse_peripheral_list(v: &Value) -> Result<Vec<(String, Vec<String>)>> {
    let arr = v
        .as_array()
        .ok_or_else(|| anyhow!("expected array, got {v:?}"))?;
    let mut out = Vec::with_capacity(arr.len());
    for entry in arr {
        let map = entry
            .as_map()
            .ok_or_else(|| anyhow!("expected map entry, got {entry:?}"))?;
        let mut peer = String::new();
        let mut types: Vec<String> = vec![];
        for (k, val) in map {
            match k.as_str() {
                Some("peer") => peer = val.as_str().unwrap_or("").to_string(),
                Some("types") => {
                    if let Some(tarr) = val.as_array() {
                        types = tarr
                            .iter()
                            .filter_map(|e| e.as_str().map(str::to_string))
                            .collect();
                    }
                }
                _ => {}
            }
        }
        out.push((peer, types));
    }
    Ok(out)
}

/// Turn a slice of typed-arg tokens into an msgpack array Value.
#[allow(dead_code)]
pub fn typed_args_to_value(tokens: &[String]) -> Result<Value> {
    let mut out = Vec::with_capacity(tokens.len());
    for t in tokens {
        out.push(parse_typed(t)?);
    }
    Ok(Value::Array(out))
}

/// Convenience for subcommands that take exactly one positional arg of
/// any shape. Exists mainly to keep clap parser logic simple.
#[allow(dead_code)]
pub fn one_or_err<T>(opt: Option<T>, what: &str) -> Result<T> {
    opt.ok_or_else(|| anyhow!("missing argument: {what}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typed_string_default() {
        assert!(matches!(parse_typed("hello").unwrap(), Value::String(_)));
    }

    #[test]
    fn typed_int() {
        let v = parse_typed("i:42").unwrap();
        assert_eq!(v.as_i64(), Some(42));
    }

    #[test]
    fn typed_bool_true() {
        let v = parse_typed("b:true").unwrap();
        assert_eq!(v.as_bool(), Some(true));
    }

    #[test]
    fn typed_nil() {
        assert!(matches!(parse_typed("n:").unwrap(), Value::Nil));
    }

    #[test]
    fn typed_json_object() {
        let v = parse_typed("j:{\"k\":1}").unwrap();
        let m = v.as_map().unwrap();
        assert_eq!(m.len(), 1);
    }

    #[test]
    fn bare_string_with_colon_unprefixed_falls_through() {
        // "x:y" — first byte is `x`, not a known prefix, so this is
        // a bare string. (`s:y` would be the typed form.)
        bail_if_unexpected("x:y");
    }

    fn bail_if_unexpected(tok: &str) {
        match parse_typed(tok).unwrap() {
            Value::String(s) => assert_eq!(s.as_str(), Some(tok)),
            other => panic!("unexpected: {other:?}"),
        }
    }
}
