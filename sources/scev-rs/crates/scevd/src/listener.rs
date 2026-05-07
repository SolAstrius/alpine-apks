// SPDX-License-Identifier: MPL-2.0
//
// Listener tasks for UNIX-socket and TCP transports. Each accept loop
// allocates a unique ClientId, sets up a per-client mpsc, and spawns
// the per-client task that pumps Frames in both directions.
//
// Both transports speak the same COBS+msgpack frame protocol the
// serial line speaks; the daemon is a transparent multiplexer at the
// frame level (with id rewriting).

use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use anyhow::{Context, Result};
use futures::{SinkExt, StreamExt};
use scev_wire::{Frame, FrameCodec};
use tokio::net::{TcpListener, UnixListener};
use tokio::sync::mpsc;
use tokio_util::codec::{FramedRead, FramedWrite};
use tracing::{debug, error, info, warn};

use crate::dispatcher::{ClientId, DispatcherMsg};

const CLIENT_QUEUE: usize = 256;

static NEXT_CLIENT_ID: AtomicU64 = AtomicU64::new(1);

fn next_client_id() -> ClientId {
    NEXT_CLIENT_ID.fetch_add(1, Ordering::Relaxed)
}

pub async fn spawn_uds(
    path: PathBuf,
    mode: u32,
    dispatcher: mpsc::Sender<DispatcherMsg>,
) -> Result<()> {
    // If a stale socket file exists (we crashed last time), remove it.
    // bind(2) doesn't tolerate an existing socket file.
    if tokio::fs::metadata(&path).await.is_ok() {
        if let Err(e) = tokio::fs::remove_file(&path).await {
            warn!(path = %path.display(), error = %e, "couldn't remove stale socket");
        }
    }
    let listener =
        UnixListener::bind(&path).with_context(|| format!("bind {}", path.display()))?;

    // chmod after bind. Doing it before would race with the bind itself.
    let perms = std::fs::Permissions::from_mode(mode);
    if let Err(e) = std::fs::set_permissions(&path, perms) {
        warn!(path = %path.display(), error = %e, "chmod failed");
    }
    info!(path = %path.display(), mode = format!("{:o}", mode), "UDS listener up");

    tokio::spawn(async move {
        loop {
            match listener.accept().await {
                Ok((stream, _addr)) => {
                    let id = next_client_id();
                    let (read_half, write_half) = stream.into_split();
                    spawn_client(
                        id,
                        FramedRead::new(read_half, FrameCodec::new()),
                        FramedWrite::new(write_half, FrameCodec::new()),
                        dispatcher.clone(),
                    );
                }
                Err(e) => {
                    error!(error = %e, "UDS accept failed");
                    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                }
            }
        }
    });
    Ok(())
}

pub async fn spawn_tcp(addr: String, dispatcher: mpsc::Sender<DispatcherMsg>) -> Result<()> {
    let listener = TcpListener::bind(&addr)
        .await
        .with_context(|| format!("bind {addr}"))?;
    info!(addr = %addr, "TCP listener up (no auth — trust your network)");

    tokio::spawn(async move {
        loop {
            match listener.accept().await {
                Ok((stream, peer)) => {
                    debug!(peer = %peer, "TCP client connected");
                    let id = next_client_id();
                    let (read_half, write_half) = stream.into_split();
                    spawn_client(
                        id,
                        FramedRead::new(read_half, FrameCodec::new()),
                        FramedWrite::new(write_half, FrameCodec::new()),
                        dispatcher.clone(),
                    );
                }
                Err(e) => {
                    error!(error = %e, "TCP accept failed");
                    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                }
            }
        }
    });
    Ok(())
}

/// Per-client task. Generic over the (Read, Write) framed pair so UDS
/// and TCP share the same body.
fn spawn_client<R, W>(
    id: ClientId,
    mut reader: FramedRead<R, FrameCodec>,
    mut writer: FramedWrite<W, FrameCodec>,
    dispatcher: mpsc::Sender<DispatcherMsg>,
) where
    R: tokio::io::AsyncRead + Unpin + Send + 'static,
    W: tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    tokio::spawn(async move {
        // Channel the dispatcher uses to push frames TO this client.
        let (to_client_tx, mut to_client_rx) = mpsc::channel::<Frame>(CLIENT_QUEUE);
        if dispatcher
            .send(DispatcherMsg::ClientConnected {
                id,
                outbound: to_client_tx,
            })
            .await
            .is_err()
        {
            return; // dispatcher gone
        }

        let dispatcher_for_disconnect = dispatcher.clone();
        let outcome = tokio::select! {
            // Inbound from the client → dispatcher
            r = async {
                while let Some(frame) = reader.next().await {
                    let frame = match frame {
                        Ok(payload) => match Frame::from_bytes(&payload) {
                            Ok(f) => f,
                            Err(e) => {
                                warn!(client = id, error = %e, "drop unparseable client frame");
                                continue;
                            }
                        },
                        Err(e) => {
                            debug!(client = id, error = %e, "client read error");
                            return Err(());
                        }
                    };
                    if dispatcher.send(DispatcherMsg::FromClient { id, frame }).await.is_err() {
                        return Err(());
                    }
                }
                Ok::<_, ()>(())
            } => r.err(),
            // Outbound from dispatcher → client
            r = async {
                while let Some(frame) = to_client_rx.recv().await {
                    let bytes = match frame.to_bytes() {
                        Ok(b) => b,
                        Err(e) => {
                            error!(client = id, error = %e, "encode-to-client failed; dropping frame");
                            continue;
                        }
                    };
                    if let Err(e) = writer.send(bytes).await {
                        debug!(client = id, error = %e, "client write failed");
                        return Err(());
                    }
                }
                Ok::<_, ()>(())
            } => r.err(),
        };
        let _ = outcome;
        let _ = dispatcher_for_disconnect
            .send(DispatcherMsg::ClientDisconnected(id))
            .await;
    });
}
