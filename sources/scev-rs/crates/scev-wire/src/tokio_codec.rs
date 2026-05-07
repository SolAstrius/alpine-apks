// SPDX-License-Identifier: MPL-2.0
//
// tokio-util Encoder/Decoder for the COBS-delimited frame stream. Lets
// daemon serial + UDS + TCP code use `Framed::new(io, FrameCodec::new())`
// uniformly — the same Frame type rides on every transport.
//
// Decoder behaviour mirrors the rpc.zig loop:
//   * Find the next 0x00 in the buffer.
//   * Empty body (back-to-back delimiters) → silently skip; the host
//     and daemon both write a leading 0x00 on first run to flush the
//     other side's framer, so we have to tolerate that.
//   * COBS decode failure → drop the frame, scan on. The reader stays
//     synced because the delimiter byte was already consumed.
//   * Buffer past MAX_FRAME with no delimiter → reset (host is
//     confused or we lost sync). Surface an error so the caller can
//     decide whether to retry.

use bytes::{Buf, BytesMut};
use thiserror::Error;
use tokio_util::codec::{Decoder, Encoder};

use crate::codec::{decode_frame, encode_frame, FrameCodecError, MAX_FRAME};

#[derive(Debug, Error)]
pub enum CodecError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("frame: {0}")]
    Frame(#[from] FrameCodecError),
}

#[derive(Default)]
pub struct FrameCodec;

impl FrameCodec {
    pub fn new() -> Self {
        Self
    }
}

impl Decoder for FrameCodec {
    /// One decoded msgpack body (post-COBS). The caller wraps it into a
    /// typed `Frame` via `Frame::from_bytes`.
    type Item = Vec<u8>;
    type Error = CodecError;

    fn decode(&mut self, src: &mut BytesMut) -> Result<Option<Self::Item>, Self::Error> {
        loop {
            match src.iter().position(|&b| b == 0) {
                Some(idx) => {
                    let body = src.split_to(idx);
                    src.advance(1); // consume the delimiter
                    if body.is_empty() {
                        // Stray delimiter (first-run flush byte) — keep scanning.
                        continue;
                    }
                    match decode_frame(&body) {
                        Ok(payload) => return Ok(Some(payload)),
                        Err(_) => {
                            // Drop bad frame, scan on. Don't surface it —
                            // a single bit error on a serial line shouldn't
                            // tear the connection.
                            continue;
                        }
                    }
                }
                None => {
                    if src.len() > MAX_FRAME {
                        src.clear();
                        return Err(CodecError::Frame(FrameCodecError::TooLarge));
                    }
                    return Ok(None);
                }
            }
        }
    }
}

impl Encoder<Vec<u8>> for FrameCodec {
    type Error = CodecError;

    fn encode(&mut self, item: Vec<u8>, dst: &mut BytesMut) -> Result<(), Self::Error> {
        let frame = encode_frame(&item)?;
        dst.extend_from_slice(&frame);
        Ok(())
    }
}

// Convenience: encode a borrowed slice without taking ownership.
impl<'a> Encoder<&'a [u8]> for FrameCodec {
    type Error = CodecError;

    fn encode(&mut self, item: &'a [u8], dst: &mut BytesMut) -> Result<(), Self::Error> {
        let frame = encode_frame(item)?;
        dst.extend_from_slice(&frame);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::BufMut;

    #[test]
    fn decoder_reassembles_split_frame() {
        let mut codec = FrameCodec::new();
        let mut buf = BytesMut::new();
        let frame = encode_frame(b"hello world").unwrap();
        // Feed half, then the rest.
        let mid = frame.len() / 2;
        buf.put_slice(&frame[..mid]);
        assert!(codec.decode(&mut buf).unwrap().is_none());
        buf.put_slice(&frame[mid..]);
        let out = codec.decode(&mut buf).unwrap().unwrap();
        assert_eq!(out, b"hello world");
    }

    #[test]
    fn decoder_yields_two_frames_in_one_buffer() {
        let mut codec = FrameCodec::new();
        let mut buf = BytesMut::new();
        buf.put_slice(&encode_frame(b"a").unwrap());
        buf.put_slice(&encode_frame(b"bb").unwrap());
        assert_eq!(codec.decode(&mut buf).unwrap().unwrap(), b"a");
        assert_eq!(codec.decode(&mut buf).unwrap().unwrap(), b"bb");
        assert!(codec.decode(&mut buf).unwrap().is_none());
    }

    #[test]
    fn decoder_skips_stray_zero() {
        let mut codec = FrameCodec::new();
        let mut buf = BytesMut::new();
        buf.put_u8(0); // stray flush byte
        buf.put_slice(&encode_frame(b"x").unwrap());
        assert_eq!(codec.decode(&mut buf).unwrap().unwrap(), b"x");
    }

    #[test]
    fn decoder_resyncs_after_corrupt_frame() {
        let mut codec = FrameCodec::new();
        let mut buf = BytesMut::new();
        // Garbage that won't decode (code byte says 5 but only 1 follows),
        // followed by a valid frame.
        buf.put_u8(0x05);
        buf.put_u8(b'a');
        buf.put_u8(0); // delimiter — drops the bad frame
        buf.put_slice(&encode_frame(b"good").unwrap());
        assert_eq!(codec.decode(&mut buf).unwrap().unwrap(), b"good");
    }
}
