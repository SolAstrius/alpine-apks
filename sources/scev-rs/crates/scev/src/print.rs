// SPDX-License-Identifier: MPL-2.0
//
// Structured printers for `describe`, `schema`, and `trace` — ported
// from `sources/scev/src/main.zig` (printDescribe / printSchemaEntry /
// printTraceEntry / printSignatureRow). Output format is line-stable
// with the Zig CLI so shell pipelines built on either binary keep
// working.

use scev_wire::Value;

fn vstr<'a>(v: &'a Value) -> &'a str {
    v.as_str().unwrap_or("")
}

fn vbool(v: &Value) -> bool {
    v.as_bool().unwrap_or(false)
}

fn vmap<'a>(v: &'a Value) -> Option<&'a Vec<(Value, Value)>> {
    if let Value::Map(m) = v {
        Some(m)
    } else {
        None
    }
}

fn varr<'a>(v: &'a Value) -> Option<&'a Vec<Value>> {
    v.as_array()
}

fn map_get<'a>(m: &'a [(Value, Value)], key: &str) -> Option<&'a Value> {
    m.iter().find_map(|(k, v)| {
        if k.as_str() == Some(key) {
            Some(v)
        } else {
            None
        }
    })
}

/// `describe` response → pretty per-class signature listing.
pub fn print_describe(v: &Value) {
    let Some(map) = vmap(v) else {
        // Not a map — just dump it.
        crate::args::dump_json(v);
        return;
    };
    let peer = map_get(map, "peer").map(vstr).unwrap_or("?");
    let type_s = map_get(map, "type").map(vstr).unwrap_or("?");
    let class_s = map_get(map, "class").map(vstr).unwrap_or("?");

    // Remote peripheral fallback (no signatures, just method names).
    if let Some(methods) = map_get(map, "methods").and_then(varr) {
        println!("{peer} ({type_s}) [remote]");
        for m in methods {
            if let Some(name) = m.as_str() {
                println!("  {name}(...)  [unknown signature — remote peripheral]");
            }
        }
        return;
    }

    // Header (full or single-method). `groups` and `method` both want it.
    let mut printed_header = false;
    let print_header = |printed: &mut bool| {
        if !*printed {
            println!("{peer} ({type_s})\n  class: {class_s}");
            *printed = true;
        }
    };

    if let Some(groups) = map_get(map, "groups").and_then(vmap) {
        print_header(&mut printed_header);
        for (gname, sigs) in groups {
            if let (Some(name), Some(arr)) = (gname.as_str(), varr(sigs)) {
                println!("  [{name}]");
                for sig in arr {
                    print_signature_row(sig);
                }
            }
        }
    }
    if let Some(method) = map_get(map, "method") {
        print_header(&mut printed_header);
        print_signature_row(method);
    }
}

fn print_signature_row(sig: &Value) {
    let Some(map) = vmap(sig) else { return };
    let name = map_get(map, "name").map(vstr).unwrap_or("?");
    let aliases: Vec<&str> = map_get(map, "aliases")
        .and_then(varr)
        .map(|a| a.iter().filter_map(|e| e.as_str()).collect())
        .unwrap_or_default();

    let mut buf = format!("    {name}(");
    if let Some(params) = map_get(map, "params").and_then(varr) {
        for (i, p) in params.iter().enumerate() {
            if i != 0 {
                buf.push_str(", ");
            }
            let Some(pmap) = vmap(p) else { continue };
            let lua_type = map_get(pmap, "luaType").map(vstr).unwrap_or("?");
            let opt = if map_get(pmap, "optional").map(vbool).unwrap_or(false) {
                "?"
            } else {
                ""
            };
            let enum_vals: Vec<&str> = map_get(pmap, "enumValues")
                .and_then(varr)
                .map(|a| a.iter().filter_map(|e| e.as_str()).collect())
                .unwrap_or_default();
            if enum_vals.is_empty() {
                buf.push_str(&format!("arg{i}: {lua_type}{opt}"));
            } else {
                buf.push_str(&format!(
                    "arg{i}: {lua_type}{opt} ∈ {{{}}}",
                    enum_vals.join("|")
                ));
            }
        }
    }
    buf.push_str("): ");
    let ret = map_get(map, "return").map(vstr).unwrap_or("value");
    match ret {
        "none" => buf.push_str("nil"),
        "many" => buf.push_str("value, ..."),
        "dynamic" => buf.push_str("dynamic"),
        _ => buf.push_str("value"),
    }
    if map_get(map, "mainThread").map(vbool).unwrap_or(false) {
        buf.push_str("  [mainThread]");
    }
    if map_get(map, "unsafe").map(vbool).unwrap_or(false) {
        buf.push_str("  [unsafe]");
    }
    if !aliases.is_empty() {
        buf.push_str("  aliases:");
        for a in &aliases {
            buf.push(' ');
            buf.push_str(a);
        }
    }
    println!("{buf}");
}

/// `schema` response: either one entry (map) or an array of entries.
pub fn print_schema(v: &Value) {
    match v {
        Value::Map(_) => print_schema_entry(v),
        Value::Array(arr) => {
            for e in arr {
                print_schema_entry(e);
            }
        }
        Value::Nil => {} // `clear` returns nil
        _ => crate::args::dump_json(v),
    }
}

fn print_schema_entry(v: &Value) {
    let Some(map) = vmap(v) else { return };
    let name = map_get(map, "name").map(vstr).unwrap_or("?");
    let observations = map_get(map, "observations").and_then(|n| n.as_i64()).unwrap_or(0);
    println!("{name}  {observations} observation(s)");
    if let Some(shapes) = map_get(map, "shapes").and_then(vmap) {
        for (shape, count) in shapes {
            let s = shape.as_str().unwrap_or("?");
            let n = count.as_i64().unwrap_or(0);
            println!("  {s}  ×{n}");
        }
    }
}

/// `trace dump`: one row per dispatch, formatted as the Zig CLI does.
pub fn print_trace(v: &Value) {
    match v {
        Value::Array(arr) => {
            for e in arr {
                print_trace_entry(e);
            }
        }
        other => {
            crate::args::dump_json(other);
        }
    }
}

fn print_trace_entry(v: &Value) {
    let Some(map) = vmap(v) else { return };
    let peer = map_get(map, "peer").map(vstr).unwrap_or("?");
    let method = map_get(map, "method").map(vstr).unwrap_or("?");
    let args = map_get(map, "args").map(vstr).unwrap_or("");
    let outcome = map_get(map, "outcome").map(vstr).unwrap_or("?");
    let started = map_get(map, "startedAt").and_then(|n| n.as_i64()).unwrap_or(0);
    let dur_us = map_get(map, "durationUs").and_then(|n| n.as_i64()).unwrap_or(0);
    let detail = map_get(map, "detail").and_then(|n| n.as_str());
    if let Some(d) = detail {
        println!("[{started}+{dur_us}us] {peer}.{method}({args}) → {outcome}: {d}");
    } else {
        println!("[{started}+{dur_us}us] {peer}.{method}({args}) → {outcome}");
    }
}
