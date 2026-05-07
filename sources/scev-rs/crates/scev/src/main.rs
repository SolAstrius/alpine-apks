// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! `scev` — guest-side CLI for the Scalar Evolution host RPC.
//!
//! Subcommand-compatible with the Zig binary it replaces and with
//! `scev-py` from `sources/py-scev`. Default transport is the system
//! daemon at `/run/scevd.sock`; falls back to direct serial when
//! the daemon isn't running. Override with `--endpoint` or
//! `SCEV_ENDPOINT`.

mod args;
mod client;
mod print;
mod transport;

use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use clap::{Parser, Subcommand};
use scev_wire::{methods, Value};

use crate::client::{CallError, Client};
use crate::transport::{connect, Endpoint};

#[derive(Parser, Debug)]
#[command(version, about = "Scalar Evolution guest RPC CLI")]
struct Cli {
    /// Connection URI: `unix:///path`, `tcp://host:port`,
    /// `serial:///dev/ttyS1`. If omitted, prefers the daemon socket
    /// at /run/scevd.sock and falls back to /dev/ttyS1.
    #[arg(long, env = "SCEV_ENDPOINT")]
    endpoint: Option<String>,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Liveness check.
    Ping,
    /// Server-side log at slf4j level (trace|debug|info|warn|error).
    Log {
        level: String,
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        msg: Vec<String>,
    },
    /// Machine environment info.
    #[command(name = "self")]
    SelfInfo,
    /// List peripherals (CC's peripheral.getNames).
    List,
    /// List peripherals by type.
    Find { peripheral_type: String },
    /// Print a peripheral's type(s).
    Type { peer: String },
    /// List a peripheral's methods.
    Methods { peer: String },
    /// Fuzzy-search method names across peripherals.
    #[command(name = "methods-like")]
    MethodsLike { needle: String },
    /// Reflection-derived signatures, grouped by class.
    Describe {
        peer: String,
        method: Option<String>,
    },
    /// Call a peripheral method.
    Call {
        peripheral: String,
        method: String,
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Inject a CC event (typed args like `call`).
    Queue {
        event: String,
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Subscribe and print events.
    Events { count: Option<i64> },
    /// Observed event-argument shapes; `clear` resets.
    Schema { event: Option<String> },
    /// Dispatch-trace control (on|off|status|dump|clear). Default: dump.
    Trace { sub: Option<String> },
    /// Send-and-listen on a modem in one connection.
    #[command(name = "modem-call")]
    ModemCall {
        modem: String,
        target_ch: i64,
        reply_ch: i64,
        message: String,
        #[arg(default_value_t = 1)]
        count: i64,
        #[arg(default_value_t = 5000)]
        timeout_ms: i64,
    },
    /// Ordered batch dispatch — items are JSON `[[method, [args...]], ...]`.
    /// Reads from --items flag or stdin if absent. Result prints one
    /// JSON envelope per line.
    Batch {
        /// JSON array of `[method, [args...]]` pairs. If omitted, read
        /// from stdin.
        #[arg(long)]
        items: Option<String>,
        /// Stop dispatching after the first item that errors. Items
        /// after the failure surface as `skipped` envelopes.
        #[arg(long)]
        stop_on_error: bool,
    },
    /// Parallel batch dispatch — same JSON shape as `batch`. Cross-peripheral
    /// items run concurrently on the host.
    #[command(name = "batch-par")]
    BatchPar {
        #[arg(long)]
        items: Option<String>,
    },
    /// Subscribe to a server-side event-name allow-list. Empty list
    /// resubscribes to wildcard (every event).
    Subscribe {
        #[arg(trailing_var_arg = true)]
        names: Vec<String>,
    },
    /// Drop event names from the server-side filter. Empty list drops
    /// the entire filter (no events).
    Unsubscribe {
        #[arg(trailing_var_arg = true)]
        names: Vec<String>,
    },
    /// Cancel an in-flight request by id (best-effort).
    Cancel { id: u64 },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .context("build tokio runtime")?;
    let exit = runtime.block_on(run(cli));
    match exit {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("scev: {e:#}");
            std::process::exit(1);
        }
    }
}

async fn run(cli: Cli) -> Result<i32> {
    let endpoint = match cli.endpoint {
        Some(s) => Endpoint::parse(&s)?,
        None => Endpoint::discover()?,
    };
    let (read, write) = connect(&endpoint).await?;
    let client = Client::start(read, write);

    // Capability handshake — best-effort, fills in defaults if the
    // host is too old (or down) to answer `self`. Subsequent gating
    // (subscribe-by-name, batch, cancel-on-timeout) reads from the
    // stashed Caps without further round-trips.
    let _ = client.handshake().await;

    match cli.cmd {
        Cmd::Ping => cmd_ping(&client).await,
        Cmd::Log { level, msg } => cmd_log(&client, &level, &msg.join(" ")).await,
        Cmd::SelfInfo => cmd_self(&client).await,
        Cmd::List => cmd_list(&client).await,
        Cmd::Find { peripheral_type } => cmd_find(&client, &peripheral_type).await,
        Cmd::Type { peer } => cmd_type(&client, &peer).await,
        Cmd::Methods { peer } => cmd_methods(&client, &peer).await,
        Cmd::MethodsLike { needle } => cmd_methods_like(&client, &needle).await,
        Cmd::Describe { peer, method } => cmd_describe(&client, &peer, method.as_deref()).await,
        Cmd::Call { peripheral, method, args } => {
            cmd_call(&client, &peripheral, &method, &args).await
        }
        Cmd::Queue { event, args } => cmd_queue(&client, &event, &args).await,
        Cmd::Events { count } => cmd_events(&client, count).await,
        Cmd::Schema { event } => cmd_schema(&client, event.as_deref()).await,
        Cmd::Trace { sub } => cmd_trace(&client, sub.as_deref().unwrap_or("dump")).await,
        Cmd::ModemCall {
            modem,
            target_ch,
            reply_ch,
            message,
            count,
            timeout_ms,
        } => cmd_modem_call(&client, &modem, target_ch, reply_ch, &message, count, timeout_ms).await,
        Cmd::Batch { items, stop_on_error } => cmd_batch(&client, items.as_deref(), stop_on_error, false).await,
        Cmd::BatchPar { items } => cmd_batch(&client, items.as_deref(), false, true).await,
        Cmd::Subscribe { names } => cmd_subscribe(&client, &names, false).await,
        Cmd::Unsubscribe { names } => cmd_subscribe(&client, &names, true).await,
        Cmd::Cancel { id } => cmd_cancel(&client, id).await,
    }
}

// ---------------------------------------------------------- helpers

fn empty() -> Value {
    Value::Array(vec![])
}

fn args(values: Vec<Value>) -> Value {
    Value::Array(values)
}

async fn call(client: &Client, method: &str, args: Value, ms: u64) -> Result<Value> {
    let timeout = Duration::from_millis(ms);
    client.call(method, args, timeout).await.map_err(|e| match e {
        CallError::Rpc(info) => anyhow!("rpc returned error [{}]: {}", info.code, info.message),
        CallError::Timeout => anyhow!("rpc timed out"),
        CallError::Disconnected => anyhow!("rpc client disconnected"),
        CallError::Io(s) => anyhow!("io: {s}"),
    })
}

// ---------------------------------------------------------- subcommands

async fn cmd_ping(client: &Client) -> Result<i32> {
    let v = call(client, methods::PING, empty(), 3000).await?;
    args::dump_json(&v);
    Ok(0)
}

async fn cmd_log(client: &Client, level: &str, msg: &str) -> Result<i32> {
    call(
        client,
        methods::LOG,
        args(vec![Value::String(level.into()), Value::String(msg.into())]),
        3000,
    )
    .await?;
    println!("null");
    Ok(0)
}

async fn cmd_self(client: &Client) -> Result<i32> {
    let v = call(client, methods::SELF_, empty(), 3000).await?;
    args::dump_json(&v);
    Ok(0)
}

async fn cmd_list(client: &Client) -> Result<i32> {
    let v = call(client, methods::LIST, empty(), 5000).await?;
    let entries = args::parse_peripheral_list(&v)?;
    for (peer, types) in entries {
        println!("{peer}  {}", types.join("+"));
    }
    Ok(0)
}

async fn cmd_find(client: &Client, want: &str) -> Result<i32> {
    let v = call(client, methods::LIST, empty(), 5000).await?;
    let entries = args::parse_peripheral_list(&v)?;
    let mut hits = 0;
    for (peer, types) in entries {
        if types.iter().any(|t| t == want) {
            println!("{peer}");
            hits += 1;
        }
    }
    // Match the Zig CLI's exit-code convention: 2 if no matches.
    Ok(if hits == 0 { 2 } else { 0 })
}

async fn cmd_type(client: &Client, peer: &str) -> Result<i32> {
    let v = call(
        client,
        methods::TYPE,
        args(vec![Value::String(peer.into())]),
        5000,
    )
    .await?;
    let Value::Map(map) = v else {
        bail!("expected map, got {v:?}");
    };
    for (k, val) in &map {
        if k.as_str() == Some("types") {
            if let Some(arr) = val.as_array() {
                for t in arr {
                    if let Some(s) = t.as_str() {
                        println!("{s}");
                    }
                }
            }
        }
    }
    Ok(0)
}

async fn cmd_methods(client: &Client, peer: &str) -> Result<i32> {
    let v = call(
        client,
        methods::METHODS,
        args(vec![Value::String(peer.into())]),
        5000,
    )
    .await?;
    for name in args::as_string_array(&v)? {
        println!("{name}");
    }
    Ok(0)
}

async fn cmd_methods_like(client: &Client, needle: &str) -> Result<i32> {
    let list = call(client, methods::LIST, empty(), 5000).await?;
    let entries = args::parse_peripheral_list(&list)?;
    let mut hits = 0;
    for (peer, _types) in entries {
        let resp = match call(
            client,
            methods::METHODS,
            args(vec![Value::String(peer.clone().into())]),
            5000,
        )
        .await
        {
            Ok(v) => v,
            Err(_) => continue, // host can't introspect this peripheral; skip
        };
        let Ok(names) = args::as_string_array(&resp) else { continue };
        for name in names {
            if name.contains(needle) {
                println!("{peer}:{name}");
                hits += 1;
            }
        }
    }
    Ok(if hits == 0 { 2 } else { 0 })
}

async fn cmd_describe(client: &Client, peer: &str, method: Option<&str>) -> Result<i32> {
    let mut a = vec![Value::String(peer.into())];
    if let Some(m) = method {
        a.push(Value::String(m.into()));
    }
    let v = call(client, methods::DESCRIBE, args(a), 10_000).await?;
    print::print_describe(&v);
    Ok(0)
}

async fn cmd_call(client: &Client, peer: &str, method: &str, tokens: &[String]) -> Result<i32> {
    let mut a = vec![Value::String(peer.into()), Value::String(method.into())];
    for t in tokens {
        a.push(args::parse_typed(t)?);
    }
    let v = call(client, methods::CALL, args(a), 15_000).await?;
    args::dump_json(&v);
    Ok(0)
}

async fn cmd_queue(client: &Client, event: &str, tokens: &[String]) -> Result<i32> {
    let mut a = vec![Value::String(event.into())];
    for t in tokens {
        a.push(args::parse_typed(t)?);
    }
    call(client, methods::QUEUE_EVENT, args(a), 5000).await?;
    println!("null");
    Ok(0)
}

async fn cmd_events(client: &Client, count: Option<i64>) -> Result<i32> {
    // Best-effort subscribe — wildcard, since the user said "show me
    // everything". With `event_subscriptions` capable hosts, an empty
    // names list means the wildcard fast path; older hosts no-op.
    let _ = client.subscribe(&[]).await;
    let mut rx = client.events();
    let mut printed = 0i64;
    let max = count.unwrap_or(-1);
    loop {
        match rx.recv().await {
            Ok(ev) => {
                let json = serde_json::to_string(&args::msgpack_to_json(&ev.args))?;
                println!("{} {}", ev.name, json);
                printed += 1;
                if max > 0 && printed >= max {
                    return Ok(0);
                }
            }
            Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                eprintln!("scev: events: dropped {n} events (consumer too slow)");
            }
            Err(_) => return Ok(0),
        }
    }
}

async fn cmd_schema(client: &Client, event: Option<&str>) -> Result<i32> {
    let v = match event {
        Some(s) => {
            call(
                client,
                methods::SCHEMA,
                args(vec![Value::String(s.into())]),
                5000,
            )
            .await?
        }
        None => call(client, methods::SCHEMA, empty(), 5000).await?,
    };
    print::print_schema(&v);
    Ok(0)
}

async fn cmd_trace(client: &Client, sub: &str) -> Result<i32> {
    if !matches!(sub, "on" | "off" | "status" | "dump" | "clear") {
        bail!("trace: bad subcommand {sub:?}");
    }
    let v = call(
        client,
        methods::TRACE,
        args(vec![Value::String(sub.into())]),
        5000,
    )
    .await?;
    if sub == "dump" {
        print::print_trace(&v);
    } else {
        args::dump_json(&v);
    }
    Ok(0)
}

async fn cmd_modem_call(
    client: &Client,
    modem: &str,
    target_ch: i64,
    reply_ch: i64,
    message: &str,
    count: i64,
    timeout_ms: i64,
) -> Result<i32> {
    // Subscribe to events BEFORE issuing the call so we don't race the
    // host's reply. With the daemon, events broadcast continuously to
    // every client, so this is purely a local broadcast::Receiver.
    // Narrow the server-side filter to modem traffic so unrelated
    // events aren't carried over the wire — this is the whole point
    // of the `event_subscriptions` capability.
    let _ = client.subscribe(&["modem_message", "rednet_message"]).await;
    let mut events = client.events();

    // open + transmit. With the daemon, `call` round-trips through the
    // dispatcher; events arrive in parallel via the broadcast channel.
    call(
        client,
        methods::CALL,
        args(vec![
            Value::String(modem.into()),
            Value::String("open".into()),
            Value::Integer(reply_ch.into()),
        ]),
        5000,
    )
    .await?;
    call(
        client,
        methods::CALL,
        args(vec![
            Value::String(modem.into()),
            Value::String("transmit".into()),
            Value::Integer(target_ch.into()),
            Value::Integer(reply_ch.into()),
            Value::String(message.into()),
        ]),
        5000,
    )
    .await?;

    let deadline = tokio::time::Instant::now() + Duration::from_millis(timeout_ms as u64);
    let mut printed = 0i64;
    while printed < count {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Ok(if printed > 0 { 0 } else { 124 });
        }
        match tokio::time::timeout(remaining, events.recv()).await {
            Ok(Ok(ev)) => {
                let json = serde_json::to_string(&args::msgpack_to_json(&ev.args))?;
                println!("{} {}", ev.name, json);
                printed += 1;
            }
            Ok(Err(tokio::sync::broadcast::error::RecvError::Lagged(_))) => continue,
            Ok(Err(_)) => return Ok(if printed > 0 { 0 } else { 1 }),
            Err(_) => return Ok(if printed > 0 { 0 } else { 124 }),
        }
    }
    Ok(0)
}

async fn cmd_batch(
    client: &Client,
    items_arg: Option<&str>,
    stop_on_error: bool,
    parallel: bool,
) -> Result<i32> {
    let cap = if parallel { "batch_par" } else { "batch" };
    if !client.has_capability(cap) {
        eprintln!(
            "scev: warning: host did not advertise `{cap}` capability — proceeding anyway, \
             but it may surface as no_such_method"
        );
    }
    let raw = match items_arg {
        Some(s) => s.to_string(),
        None => {
            use std::io::Read;
            let mut buf = String::new();
            std::io::stdin()
                .read_to_string(&mut buf)
                .context("read items from stdin")?;
            buf
        }
    };
    let parsed: serde_json::Value =
        serde_json::from_str(&raw).context("parse --items as JSON")?;
    let arr = parsed
        .as_array()
        .ok_or_else(|| anyhow!("--items must be a JSON array"))?;
    let mut items: Vec<(String, Value)> = Vec::with_capacity(arr.len());
    for entry in arr {
        let pair = entry
            .as_array()
            .ok_or_else(|| anyhow!("each item must be a [method, args] pair"))?;
        if pair.is_empty() {
            bail!("item is empty");
        }
        let method = pair[0]
            .as_str()
            .ok_or_else(|| anyhow!("item method must be a string"))?
            .to_string();
        let args_v = if pair.len() > 1 {
            args::json_to_msgpack(pair[1].clone())
        } else {
            Value::Array(vec![])
        };
        items.push((method, args_v));
    }
    let results = if parallel {
        client.batch_par(items).await
    } else {
        client.batch(items, stop_on_error).await
    };
    let results = results.map_err(|e| match e {
        CallError::Rpc(info) => anyhow!("rpc returned error [{}]: {}", info.code, info.message),
        CallError::Timeout => anyhow!("rpc timed out"),
        CallError::Disconnected => anyhow!("rpc client disconnected"),
        CallError::Io(s) => anyhow!("io: {s}"),
    })?;
    // Print one JSON envelope per line: {"err": null|{code,message}, "result": ...}
    let mut any_errored = false;
    for r in &results {
        let envelope = match r {
            Ok(v) => serde_json::json!({
                "err": serde_json::Value::Null,
                "result": args::msgpack_to_json(v),
            }),
            Err(info) => {
                any_errored = true;
                serde_json::json!({
                    "err": {"code": info.code, "message": info.message},
                    "result": serde_json::Value::Null,
                })
            }
        };
        println!("{}", serde_json::to_string(&envelope)?);
    }
    Ok(if any_errored { 2 } else { 0 })
}

async fn cmd_subscribe(client: &Client, names: &[String], unsubscribe: bool) -> Result<i32> {
    let owned: Vec<&str> = names.iter().map(String::as_str).collect();
    let resp = if unsubscribe {
        client.unsubscribe(&owned).await
    } else {
        client.subscribe(&owned).await
    }
    .map_err(|e| match e {
        CallError::Rpc(info) => anyhow!("rpc returned error [{}]: {}", info.code, info.message),
        CallError::Timeout => anyhow!("rpc timed out"),
        CallError::Disconnected => anyhow!("rpc client disconnected"),
        CallError::Io(s) => anyhow!("io: {s}"),
    })?;
    args::dump_json(&resp);
    Ok(0)
}

async fn cmd_cancel(client: &Client, id: u64) -> Result<i32> {
    if !client.has_capability("cancel") {
        eprintln!("scev: warning: host did not advertise `cancel` capability");
    }
    let v = call(
        client,
        methods::CANCEL,
        args(vec![Value::Integer(id.into())]),
        3000,
    )
    .await?;
    args::dump_json(&v);
    Ok(0)
}
