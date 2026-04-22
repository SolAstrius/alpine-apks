// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! Consistent Overhead Byte Stuffing (COBS) — the framing layer
//! between guest and host. Every frame on the wire ends with a 0x00
//! byte; COBS rewrites the payload so no 0x00 appears inside,
//! reserving it exclusively as the delimiter.
//!
//! The canonical reference is Cheshire & Baker (1997), but the code
//! below matches the Java implementation in
//! `lekkit/scev/rpc/Cobs.java` byte-for-byte so the wire protocol
//! stays stable.

const std = @import("std");

pub const CobsError = error{CorruptFrame};

/// Worst-case output size: every 254 bytes of input picks up one
/// overhead byte, plus the leading code byte and trailing 0x00. Sized
/// to always fit; callers pass a slice of at least this length.
pub inline fn maxEncodedSize(in_len: usize) usize {
    // (in_len + 254) / 254 overhead bytes + 1 trailing zero.
    return in_len + (in_len / 254) + 2;
}

/// Encode `in` into `out`, returning the length written (including
/// the trailing 0x00 delimiter). `out` must have at least
/// `maxEncodedSize(in.len)` bytes of room.
pub fn encode(in: []const u8, out: []u8) usize {
    var oi: usize = 1;            // index of next data write
    var code_slot: usize = 0;     // index of the block's code byte
    var code: u8 = 1;             // run length of the current block

    for (in) |b| {
        if (b == 0) {
            out[code_slot] = code;
            code_slot = oi;
            oi += 1;
            code = 1;
        } else {
            out[oi] = b;
            oi += 1;
            code += 1;
            if (code == 0xFF) {
                out[code_slot] = code;
                code_slot = oi;
                oi += 1;
                code = 1;
            }
        }
    }
    out[code_slot] = code;
    out[oi] = 0;
    oi += 1;
    return oi;
}

/// Decode `in` (a complete COBS-encoded frame, WITHOUT the trailing
/// 0x00 delimiter — strip it at the reader) into `out`. Returns the
/// decoded length, or a [`CobsError.CorruptFrame`] if the frame
/// contains a stray 0x00 or an out-of-bounds code byte.
pub fn decode(in: []const u8, out: []u8) CobsError!usize {
    var oi: usize = 0;
    var i: usize = 0;
    while (i < in.len) {
        const code = in[i];
        if (code == 0) return CobsError.CorruptFrame;
        i += 1;
        const chunk = @as(usize, code) - 1;
        if (i + chunk > in.len) return CobsError.CorruptFrame;
        @memcpy(out[oi .. oi + chunk], in[i .. i + chunk]);
        oi += chunk;
        i += chunk;
        // Code 0xFF means "254 non-zero bytes, no implicit zero
        // between this chunk and the next". All other codes imply a
        // zero UNLESS we just consumed the final chunk.
        if (code < 0xFF and i < in.len) {
            out[oi] = 0;
            oi += 1;
        }
    }
    return oi;
}

test "encode/decode round-trip: basic" {
    var enc_buf: [32]u8 = undefined;
    var dec_buf: [32]u8 = undefined;
    const msg = "hello";
    const enc_len = encode(msg, &enc_buf);
    // Strip trailing 0x00 delimiter before passing to decode.
    const dec_len = try decode(enc_buf[0 .. enc_len - 1], &dec_buf);
    try std.testing.expectEqualStrings(msg, dec_buf[0..dec_len]);
}

test "encode round-trip preserves zeros in payload" {
    var enc_buf: [64]u8 = undefined;
    var dec_buf: [64]u8 = undefined;
    const msg = [_]u8{ 0x01, 0x00, 0x02, 0x00, 0x03 };
    const enc_len = encode(&msg, &enc_buf);
    // No 0x00 should appear inside the encoded body.
    for (enc_buf[0 .. enc_len - 1]) |b| try std.testing.expect(b != 0);
    const dec_len = try decode(enc_buf[0 .. enc_len - 1], &dec_buf);
    try std.testing.expectEqualSlices(u8, &msg, dec_buf[0..dec_len]);
}

test "decode rejects truncated chunk" {
    var dec_buf: [8]u8 = undefined;
    // Code byte says "4 bytes in this block" but only 2 data bytes
    // follow — overruns the buffer. Must surface as CorruptFrame
    // rather than read past the end.
    const bad = [_]u8{ 0x05, 'a', 'b' };
    try std.testing.expectError(CobsError.CorruptFrame, decode(&bad, &dec_buf));
}

test "decode rejects zero code byte" {
    var dec_buf: [8]u8 = undefined;
    // 0x00 as a code byte is the delimiter — it should never appear
    // INSIDE a frame (the reader strips trailing delimiters before
    // calling decode). A 0 code mid-frame means the framer lost sync.
    const bad = [_]u8{ 0x02, 'a', 0x00, 'b' };
    try std.testing.expectError(CobsError.CorruptFrame, decode(&bad, &dec_buf));
}

test "254-byte run uses 0xFF code without implicit trailing zero" {
    // Exactly 254 non-zero bytes — encodes as one 0xFF-coded block.
    var in: [254]u8 = undefined;
    for (&in, 0..) |*p, i| p.* = @intCast((i % 255) + 1);
    var enc_buf: [maxEncodedSize(254)]u8 = undefined;
    var dec_buf: [254]u8 = undefined;
    const enc_len = encode(&in, &enc_buf);
    const dec_len = try decode(enc_buf[0 .. enc_len - 1], &dec_buf);
    try std.testing.expectEqualSlices(u8, &in, dec_buf[0..dec_len]);
}
