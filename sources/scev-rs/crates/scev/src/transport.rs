// SPDX-License-Identifier: MPL-2.0
//
// Endpoint URI parsing + connect. Three schemes:
//
//   unix:///run/scevd.sock     UNIX socket (the daemon)
//   tcp://host:port            TCP socket (the daemon, exposed remotely)
//   serial:///dev/ttyS1        Direct serial — bypass the daemon
//
// Selection order if no `--endpoint` is given:
//   1. SCEV_ENDPOINT env var
//   2. /run/scevd.sock if it exists
//   3. SCEV_SERIAL env var or /dev/ttyS1
//
// The CLI ALWAYS prefers the daemon when it's reachable: only the
// daemon can deliver events that arrived between client invocations,
// which is the original "events between processes get lost" problem.

use std::path::PathBuf;

use anyhow::{anyhow, bail, Context, Result};
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpStream, UnixStream};
use tokio_serial::SerialPortBuilderExt;

const DEFAULT_SOCKET: &str = "/run/scevd.sock";
const DEFAULT_SERIAL: &str = "/dev/ttyS1";

#[derive(Debug, Clone)]
pub enum Endpoint {
    Unix(PathBuf),
    Tcp(String),
    Serial(PathBuf),
}

impl Endpoint {
    pub fn parse(raw: &str) -> Result<Self> {
        if let Some(rest) = raw.strip_prefix("unix://") {
            Ok(Endpoint::Unix(PathBuf::from(rest)))
        } else if let Some(rest) = raw.strip_prefix("tcp://") {
            Ok(Endpoint::Tcp(rest.to_string()))
        } else if let Some(rest) = raw.strip_prefix("serial://") {
            Ok(Endpoint::Serial(PathBuf::from(rest)))
        } else if raw.starts_with('/') {
            // Bare absolute path: heuristic — if it lives under /dev,
            // assume serial; otherwise UNIX socket.
            if raw.starts_with("/dev/") {
                Ok(Endpoint::Serial(PathBuf::from(raw)))
            } else {
                Ok(Endpoint::Unix(PathBuf::from(raw)))
            }
        } else if raw.contains(':') {
            // host:port shorthand → tcp
            Ok(Endpoint::Tcp(raw.to_string()))
        } else {
            Err(anyhow!("can't parse endpoint {raw:?}"))
        }
    }

    pub fn discover() -> Result<Self> {
        if let Ok(s) = std::env::var("SCEV_ENDPOINT") {
            if !s.is_empty() {
                return Self::parse(&s);
            }
        }
        // Daemon is always preferred when its socket exists.
        if std::fs::metadata(DEFAULT_SOCKET).is_ok() {
            return Ok(Endpoint::Unix(PathBuf::from(DEFAULT_SOCKET)));
        }
        let serial = std::env::var("SCEV_SERIAL").unwrap_or_else(|_| DEFAULT_SERIAL.to_string());
        Ok(Endpoint::Serial(PathBuf::from(serial)))
    }
}

pub type BoxRead = std::pin::Pin<Box<dyn AsyncRead + Send + Unpin>>;
pub type BoxWrite = std::pin::Pin<Box<dyn AsyncWrite + Send + Unpin>>;

pub async fn connect(ep: &Endpoint) -> Result<(BoxRead, BoxWrite)> {
    match ep {
        Endpoint::Unix(path) => {
            let s = UnixStream::connect(path)
                .await
                .with_context(|| format!("connect unix:{}", path.display()))?;
            let (r, w) = s.into_split();
            Ok((Box::pin(r), Box::pin(w)))
        }
        Endpoint::Tcp(addr) => {
            let s = TcpStream::connect(addr)
                .await
                .with_context(|| format!("connect tcp://{addr}"))?;
            // Disable Nagle so request frames go out immediately — the
            // call/response loop is latency-sensitive.
            s.set_nodelay(true).ok();
            let (r, w) = s.into_split();
            Ok((Box::pin(r), Box::pin(w)))
        }
        Endpoint::Serial(path) => {
            let mut port = tokio_serial::new(path.to_string_lossy(), 115_200)
                .data_bits(tokio_serial::DataBits::Eight)
                .parity(tokio_serial::Parity::None)
                .stop_bits(tokio_serial::StopBits::One)
                .flow_control(tokio_serial::FlowControl::None)
                .open_native_async()
                .with_context(|| format!("open serial {}", path.display()))?;
            // Same first-run ritual as the Zig + Python clients: drop a
            // single 0x00 byte so the host's framer terminates any
            // accumulated cooked-mode echo trash and resets cleanly.
            if let Err(e) = port.write_all(&[0u8]).await {
                bail!("first-run flush failed: {e}");
            }
            let (r, w) = tokio::io::split(port);
            Ok((Box::pin(r), Box::pin(w)))
        }
    }
}
