// SPDX-License-Identifier: MPL-2.0
//
// COBS framing — wraps the `cobs` crate. Wire shape is `<encoded> 0x00`
// (trailing 0x00 is the frame delimiter; no 0x00 may appear inside the
// encoded body). Conformance corpus is mirrored from
// sources/scev/src/cobs.zig + sources/py-scev/tests/test_cobs.py — any
// regression here should fail those too.

use thiserror::Error;

/// Max plaintext frame size we'll accumulate or send. Mirrors the host's
/// `ScevRpcManager.MAX_FRAME_BYTES`; clients that want to adapt per-host
/// can read the authoritative value from the `self` RPC's
/// `frame_max_bytes` field. This constant is the upper bound the codec
/// is willing to allocate for, sized to accommodate `describe`/`schema`
/// payloads and rich event args without splitting frames.
pub const MAX_FRAME: usize = 65536;

#[derive(Debug, Error)]
pub enum FrameCodecError {
    #[error("frame larger than MAX_FRAME ({MAX_FRAME})")]
    TooLarge,
    #[error("corrupt cobs frame")]
    Corrupt,
}

/// Encode `payload` as a COBS frame, including the trailing 0x00 delimiter.
pub fn encode_frame(payload: &[u8]) -> Result<Vec<u8>, FrameCodecError> {
    if payload.len() > MAX_FRAME {
        return Err(FrameCodecError::TooLarge);
    }
    let mut out = vec![0u8; cobs::max_encoding_length(payload.len())];
    let n = cobs::encode(payload, &mut out);
    out.truncate(n);
    out.push(0);
    Ok(out)
}

/// Decode a COBS frame body — bytes between two 0x00 delimiters, the
/// trailing delimiter already stripped by the reader.
pub fn decode_frame(body: &[u8]) -> Result<Vec<u8>, FrameCodecError> {
    // The `cobs` crate tolerates a 0x00 inside the body (treats it as
    // an early end-of-frame). The Zig + Python ports treat it as a
    // hard "framer lost sync" error and drop the frame; mirror that
    // here so all three implementations agree on rejection rules.
    if body.contains(&0) {
        return Err(FrameCodecError::Corrupt);
    }
    let mut out = vec![0u8; body.len()];
    let n = cobs::decode(body, &mut out).map_err(|_| FrameCodecError::Corrupt)?;
    out.truncate(n);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_basic() {
        let msg = b"hello";
        let enc = encode_frame(msg).unwrap();
        assert_eq!(*enc.last().unwrap(), 0);
        let dec = decode_frame(&enc[..enc.len() - 1]).unwrap();
        assert_eq!(dec, msg);
    }

    #[test]
    fn round_trip_preserves_zeros() {
        let msg = [0x01u8, 0x00, 0x02, 0x00, 0x03];
        let enc = encode_frame(&msg).unwrap();
        // No 0x00 should appear inside the encoded body.
        assert!(enc[..enc.len() - 1].iter().all(|&b| b != 0));
        let dec = decode_frame(&enc[..enc.len() - 1]).unwrap();
        assert_eq!(dec, msg);
    }

    #[test]
    fn decode_rejects_truncated_chunk() {
        // Code byte says 4 bytes but only 2 follow.
        let bad = [0x05u8, b'a', b'b'];
        assert!(matches!(decode_frame(&bad), Err(FrameCodecError::Corrupt)));
    }

    #[test]
    fn decode_rejects_zero_code_byte() {
        // 0x00 inside a frame means the framer lost sync.
        let bad = [0x02u8, b'a', 0x00, b'b'];
        assert!(matches!(decode_frame(&bad), Err(FrameCodecError::Corrupt)));
    }

    #[test]
    fn run_of_254_uses_ff_code() {
        let payload: Vec<u8> = (0..254).map(|i| ((i % 255) as u8) + 1).collect();
        let enc = encode_frame(&payload).unwrap();
        let dec = decode_frame(&enc[..enc.len() - 1]).unwrap();
        assert_eq!(dec, payload);
    }

    #[test]
    fn rejects_oversize() {
        let big = vec![0x42u8; MAX_FRAME + 1];
        assert!(matches!(encode_frame(&big), Err(FrameCodecError::TooLarge)));
    }
}
