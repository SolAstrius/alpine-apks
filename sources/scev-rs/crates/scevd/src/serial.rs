// SPDX-License-Identifier: MPL-2.0
//
// Serial-port plumbing: open /dev/ttyS1 in raw mode, do the
// tcflush + leading-0x00 ritual that clears any cooked-mode echo trash
// the host's framer accumulated before we showed up, then split into
// FramedRead/FramedWrite halves and run them as separate tasks.
//
// Why two tasks (not Framed-everything-in-one): the dispatcher needs
// to be able to push outbound frames *while* an inbound read is
// blocking. Splitting the AsyncRead+AsyncWrite halves lets us own each
// direction in its own task and drive both via mpsc channels.

use std::path::Path;

use anyhow::{Context, Result};
use futures::{SinkExt, StreamExt};
use scev_wire::{Frame, FrameCodec};
use tokio::io::AsyncWriteExt;
use tokio::sync::mpsc;
use tokio_serial::SerialPortBuilderExt;
use tokio_util::codec::{FramedRead, FramedWrite};
use tracing::{debug, error, info, warn};

const BAUD: u32 = 115200;
const SERIAL_TX_QUEUE: usize = 256;
const SERIAL_RX_QUEUE: usize = 256;

/// Open the serial port and start the reader/writer tasks.
///
/// Returns:
///   * `Receiver<Frame>` — every host-to-guest frame, decoded.
///     Dispatcher owns this end.
///   * `Sender<Frame>`  — outbound frames; the writer task drains it.
pub async fn open(path: &Path) -> Result<(mpsc::Receiver<Frame>, mpsc::Sender<Frame>)> {
    let mut port = tokio_serial::new(path.to_string_lossy(), BAUD)
        .data_bits(tokio_serial::DataBits::Eight)
        .parity(tokio_serial::Parity::None)
        .stop_bits(tokio_serial::StopBits::One)
        .flow_control(tokio_serial::FlowControl::None)
        .open_native_async()
        .with_context(|| format!("open {}", path.display()))?;

    // First-run hygiene: the host's framer may hold echo trash from before
    // the kernel's cooked-mode default got switched off. A bare 0x00 byte
    // forces it to terminate whatever it had accumulated, decode the junk
    // (which fails harmlessly), and reset cleanly. Same ritual as
    // sources/scev/src/rpc.zig and sources/py-scev/src/scev/_rpc.py.
    if let Err(e) = port.write_all(&[0u8]).await {
        warn!(error = %e, "first-run flush byte failed; continuing");
    }
    info!(path = %path.display(), baud = BAUD, "serial port open");

    let (read_half, write_half) = tokio::io::split(port);
    let mut reader = FramedRead::new(read_half, FrameCodec::new());
    let mut writer = FramedWrite::new(write_half, FrameCodec::new());

    let (in_tx, in_rx) = mpsc::channel::<Frame>(SERIAL_RX_QUEUE);
    let (out_tx, mut out_rx) = mpsc::channel::<Frame>(SERIAL_TX_QUEUE);

    // Reader: parse decoded msgpack bodies into Frames and push to dispatcher.
    tokio::spawn(async move {
        while let Some(item) = reader.next().await {
            match item {
                Ok(payload) => match Frame::from_bytes(&payload) {
                    Ok(frame) => {
                        if in_tx.send(frame).await.is_err() {
                            debug!("dispatcher gone, serial reader exiting");
                            break;
                        }
                    }
                    Err(e) => warn!(error = %e, "drop unparseable host frame"),
                },
                Err(e) => {
                    error!(error = %e, "serial read error, terminating reader");
                    break;
                }
            }
        }
    });

    // Writer: encode Frames to msgpack, push through the COBS Encoder.
    tokio::spawn(async move {
        while let Some(frame) = out_rx.recv().await {
            let bytes = match frame.to_bytes() {
                Ok(b) => b,
                Err(e) => {
                    error!(error = %e, "encode outbound frame failed; dropping");
                    continue;
                }
            };
            if let Err(e) = writer.send(bytes).await {
                error!(error = %e, "serial write failed, terminating writer");
                break;
            }
        }
    });

    Ok((in_rx, out_tx))
}
