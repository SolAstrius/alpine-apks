// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! `scevd` — system-wide multiplexer for the scev guest serial RPC.
//!
//! Owns `/dev/ttyS1`. Listens on a UNIX socket (and optionally TCP).
//! Accepts multiple concurrent clients, each speaking the same
//! COBS+msgpack frame protocol the host serial line speaks. Daemon
//! rewrites correlation ids on the fly so clients don't collide on
//! their per-process counters, and broadcasts every host event to
//! every connected client.

mod dispatcher;
mod listener;
mod serial;

use std::path::PathBuf;

use anyhow::Context;
use clap::Parser;
use tokio::signal::unix::{signal, SignalKind};
use tracing::{info, warn};

#[derive(Parser, Debug)]
#[command(version, about = "Scalar Evolution guest RPC multiplexer")]
struct Args {
    /// Serial device the host RPC speaks.
    #[arg(long, default_value = "/dev/ttyS1", env = "SCEVD_SERIAL")]
    serial: PathBuf,

    /// UNIX socket clients connect to. Empty string disables UDS.
    #[arg(long, default_value = "/run/scevd.sock", env = "SCEVD_SOCKET")]
    socket: String,

    /// Optional TCP listen address (e.g. `127.0.0.1:5151`). Disabled by
    /// default — TCP carries no auth, only enable on trusted networks.
    #[arg(long, env = "SCEVD_TCP")]
    tcp: Option<String>,

    /// chmod the UNIX socket to this octal mode (e.g. 0660). Default
    /// is 0666 so any user on the machine can talk to it.
    #[arg(long, default_value = "0666")]
    socket_mode: String,

    /// Log filter (`error`/`warn`/`info`/`debug`/`trace`). Honors
    /// RUST_LOG when this isn't set.
    #[arg(long)]
    log: Option<String>,
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();

    let filter = match (args.log.as_deref(), std::env::var("RUST_LOG").ok()) {
        (Some(s), _) => s.to_string(),
        (None, Some(s)) => s,
        _ => "info".into(),
    };
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::new(filter))
        .with_target(false)
        .init();

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .context("build tokio runtime")?;
    runtime.block_on(run(args))
}

async fn run(args: Args) -> anyhow::Result<()> {
    info!(serial = %args.serial.display(), socket = %args.socket, tcp = ?args.tcp, "scevd starting");

    // 1. Open the serial port + spawn reader/writer tasks. Returns the
    //    inbound frame stream and an outbound sink.
    let (serial_in_rx, serial_out_tx) = serial::open(&args.serial).await?;

    // 2. Boot the dispatcher — owns id rewriting + client roster.
    let dispatcher_tx = dispatcher::spawn(serial_in_rx, serial_out_tx);

    // 3. Spin up listeners. UDS is on by default; TCP is opt-in via flag.
    let socket_mode = u32::from_str_radix(args.socket_mode.trim_start_matches("0o"), 8)
        .context("parse --socket-mode")?;
    if !args.socket.is_empty() {
        let path: PathBuf = args.socket.into();
        listener::spawn_uds(path, socket_mode, dispatcher_tx.clone()).await?;
    }
    if let Some(addr) = args.tcp {
        listener::spawn_tcp(addr, dispatcher_tx.clone()).await?;
    }

    // 4. Block on signals — graceful shutdown drops the dispatcher,
    //    which closes channels, which lets all client tasks unwind.
    wait_for_shutdown().await;
    info!("scevd shutting down");
    Ok(())
}

async fn wait_for_shutdown() {
    let mut sigterm = signal(SignalKind::terminate()).expect("install SIGTERM handler");
    let mut sigint = signal(SignalKind::interrupt()).expect("install SIGINT handler");
    tokio::select! {
        _ = sigterm.recv() => warn!("SIGTERM received"),
        _ = sigint.recv() => warn!("SIGINT received"),
    }
}
