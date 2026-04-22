// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! MessagePack encoder/decoder, restricted to the subset the scev RPC
//! protocol uses: nil, bool, int (signed 64-bit carry-all), float64,
//! string (UTF-8), bin, array, map. Matches the Java-side
//! `lekkit.scev.rpc.MsgPack` + the C `mpack.c` byte-for-byte on the
//! wire.
//!
//! Unsupported tags are reported via [`DecodeError.Unsupported`] at
//! peek/read time rather than silently panicking; the dispatcher side
//! turns them into RPC error responses.

const std = @import("std");
const mem = std.mem;

pub const Kind = enum {
    nil,
    bool_,
    int,
    uint,
    f32_,
    f64_,
    str,
    bin,
    array,
    map,
    eof,
};

pub const DecodeError = error{
    Truncated,     // ran off the end of the buffer
    Unsupported,   // tag we don't grok (ext, timestamp, …)
    BadType,       // peek said X but read_Y was called
    Overflow,      // uint won't fit in i64
};

pub const EncodeError = error{BufferTooSmall};

// ============================================================
// Encoder
// ============================================================

pub const Encoder = struct {
    buf: []u8,
    len: usize = 0,

    pub fn init(buf: []u8) Encoder {
        return .{ .buf = buf };
    }

    fn writeByte(self: *Encoder, b: u8) EncodeError!void {
        if (self.len >= self.buf.len) return EncodeError.BufferTooSmall;
        self.buf[self.len] = b;
        self.len += 1;
    }

    fn writeBytes(self: *Encoder, bytes: []const u8) EncodeError!void {
        if (self.len + bytes.len > self.buf.len) return EncodeError.BufferTooSmall;
        @memcpy(self.buf[self.len .. self.len + bytes.len], bytes);
        self.len += bytes.len;
    }

    fn writeU16(self: *Encoder, v: u16) EncodeError!void {
        const b = [2]u8{ @intCast(v >> 8), @truncate(v) };
        try self.writeBytes(&b);
    }

    fn writeU32(self: *Encoder, v: u32) EncodeError!void {
        const b = [4]u8{ @intCast(v >> 24), @intCast((v >> 16) & 0xFF), @intCast((v >> 8) & 0xFF), @truncate(v) };
        try self.writeBytes(&b);
    }

    fn writeU64(self: *Encoder, v: u64) EncodeError!void {
        var b: [8]u8 = undefined;
        mem.writeInt(u64, &b, v, .big);
        try self.writeBytes(&b);
    }

    pub fn nil(self: *Encoder) EncodeError!void {
        try self.writeByte(0xC0);
    }

    pub fn boolean(self: *Encoder, v: bool) EncodeError!void {
        try self.writeByte(if (v) 0xC3 else 0xC2);
    }

    pub fn int(self: *Encoder, v: i64) EncodeError!void {
        // Positive path: fixint (0..127), uint8, uint16, uint32, uint64.
        // Negative path: negative fixint (-32..-1), int8, int16, int32, int64.
        if (v >= 0) {
            const uv: u64 = @intCast(v);
            if (uv <= 0x7F) return self.writeByte(@intCast(uv));
            if (uv <= 0xFF) {
                try self.writeByte(0xCC);
                return self.writeByte(@intCast(uv));
            }
            if (uv <= 0xFFFF) {
                try self.writeByte(0xCD);
                return self.writeU16(@intCast(uv));
            }
            if (uv <= 0xFFFFFFFF) {
                try self.writeByte(0xCE);
                return self.writeU32(@intCast(uv));
            }
            try self.writeByte(0xCF);
            return self.writeU64(uv);
        } else {
            if (v >= -32) {
                // negative fixint = 0xE0 | (v & 0x1F), where v is
                // already in the [-32, -1] range. Store raw 6-bit
                // two's-complement.
                const raw: u8 = @bitCast(@as(i8, @intCast(v)));
                return self.writeByte(raw);
            }
            if (v >= -128) {
                try self.writeByte(0xD0);
                return self.writeByte(@bitCast(@as(i8, @intCast(v))));
            }
            if (v >= -32768) {
                try self.writeByte(0xD1);
                return self.writeU16(@bitCast(@as(i16, @intCast(v))));
            }
            if (v >= -2147483648) {
                try self.writeByte(0xD2);
                return self.writeU32(@bitCast(@as(i32, @intCast(v))));
            }
            try self.writeByte(0xD3);
            return self.writeU64(@bitCast(v));
        }
    }

    pub fn f64_(self: *Encoder, v: f64) EncodeError!void {
        try self.writeByte(0xCB);
        try self.writeU64(@bitCast(v));
    }

    pub fn str(self: *Encoder, s: []const u8) EncodeError!void {
        const n = s.len;
        if (n <= 31) {
            try self.writeByte(0xA0 | @as(u8, @intCast(n)));
        } else if (n <= 0xFF) {
            try self.writeByte(0xD9);
            try self.writeByte(@intCast(n));
        } else if (n <= 0xFFFF) {
            try self.writeByte(0xDA);
            try self.writeU16(@intCast(n));
        } else if (n <= 0xFFFFFFFF) {
            try self.writeByte(0xDB);
            try self.writeU32(@intCast(n));
        } else {
            return EncodeError.BufferTooSmall; // > 4 GiB string, not supported
        }
        try self.writeBytes(s);
    }

    pub fn bin(self: *Encoder, b: []const u8) EncodeError!void {
        const n = b.len;
        if (n <= 0xFF) {
            try self.writeByte(0xC4);
            try self.writeByte(@intCast(n));
        } else if (n <= 0xFFFF) {
            try self.writeByte(0xC5);
            try self.writeU16(@intCast(n));
        } else if (n <= 0xFFFFFFFF) {
            try self.writeByte(0xC6);
            try self.writeU32(@intCast(n));
        } else {
            return EncodeError.BufferTooSmall;
        }
        try self.writeBytes(b);
    }

    pub fn arrayHeader(self: *Encoder, n: u32) EncodeError!void {
        if (n <= 15) {
            try self.writeByte(0x90 | @as(u8, @intCast(n)));
        } else if (n <= 0xFFFF) {
            try self.writeByte(0xDC);
            try self.writeU16(@intCast(n));
        } else {
            try self.writeByte(0xDD);
            try self.writeU32(n);
        }
    }

    pub fn mapHeader(self: *Encoder, n: u32) EncodeError!void {
        if (n <= 15) {
            try self.writeByte(0x80 | @as(u8, @intCast(n)));
        } else if (n <= 0xFFFF) {
            try self.writeByte(0xDE);
            try self.writeU16(@intCast(n));
        } else {
            try self.writeByte(0xDF);
            try self.writeU32(n);
        }
    }
};

// ============================================================
// Decoder
// ============================================================

pub const Decoder = struct {
    buf: []const u8,
    pos: usize = 0,

    pub fn init(buf: []const u8) Decoder {
        return .{ .buf = buf };
    }

    pub fn remaining(self: *const Decoder) usize {
        return self.buf.len - self.pos;
    }

    fn peekByte(self: *const Decoder) DecodeError!u8 {
        if (self.pos >= self.buf.len) return DecodeError.Truncated;
        return self.buf[self.pos];
    }

    fn takeByte(self: *Decoder) DecodeError!u8 {
        const b = try self.peekByte();
        self.pos += 1;
        return b;
    }

    fn takeBytes(self: *Decoder, n: usize) DecodeError![]const u8 {
        if (self.pos + n > self.buf.len) return DecodeError.Truncated;
        const s = self.buf[self.pos .. self.pos + n];
        self.pos += n;
        return s;
    }

    fn takeU16(self: *Decoder) DecodeError!u16 {
        const b = try self.takeBytes(2);
        return mem.readInt(u16, b[0..2], .big);
    }

    fn takeU32(self: *Decoder) DecodeError!u32 {
        const b = try self.takeBytes(4);
        return mem.readInt(u32, b[0..4], .big);
    }

    fn takeU64(self: *Decoder) DecodeError!u64 {
        const b = try self.takeBytes(8);
        return mem.readInt(u64, b[0..8], .big);
    }

    pub fn peek(self: *const Decoder) DecodeError!Kind {
        if (self.pos >= self.buf.len) return .eof;
        const b = self.buf[self.pos];
        if (b <= 0x7F) return .uint;                       // positive fixint
        if (b >= 0xE0) return .int;                        // negative fixint
        if (b & 0xE0 == 0xA0) return .str;                 // fixstr
        if (b & 0xF0 == 0x90) return .array;               // fixarray
        if (b & 0xF0 == 0x80) return .map;                 // fixmap
        return switch (b) {
            0xC0 => .nil,
            0xC2, 0xC3 => .bool_,
            0xC4, 0xC5, 0xC6 => .bin,
            0xCA => .f32_,
            0xCB => .f64_,
            0xCC, 0xCD, 0xCE, 0xCF => .uint,
            0xD0, 0xD1, 0xD2, 0xD3 => .int,
            0xD9, 0xDA, 0xDB => .str,
            0xDC, 0xDD => .array,
            0xDE, 0xDF => .map,
            else => DecodeError.Unsupported,
        };
    }

    pub fn readNil(self: *Decoder) DecodeError!void {
        const b = try self.takeByte();
        if (b != 0xC0) return DecodeError.BadType;
    }

    pub fn readBool(self: *Decoder) DecodeError!bool {
        const b = try self.takeByte();
        return switch (b) {
            0xC2 => false,
            0xC3 => true,
            else => DecodeError.BadType,
        };
    }

    /// Carries uint and int under one roof — uint values beyond i64
    /// range become [`DecodeError.Overflow`].
    pub fn readInt(self: *Decoder) DecodeError!i64 {
        const b = try self.takeByte();
        if (b <= 0x7F) return @intCast(b);
        if (b >= 0xE0) return @as(i8, @bitCast(b));
        return switch (b) {
            0xCC => @intCast(try self.takeByte()),
            0xCD => @intCast(try self.takeU16()),
            0xCE => @intCast(try self.takeU32()),
            0xCF => blk: {
                const v = try self.takeU64();
                if (v > @as(u64, std.math.maxInt(i64))) return DecodeError.Overflow;
                break :blk @intCast(v);
            },
            0xD0 => @as(i8, @bitCast(try self.takeByte())),
            0xD1 => @as(i16, @bitCast(try self.takeU16())),
            0xD2 => @as(i32, @bitCast(try self.takeU32())),
            0xD3 => @as(i64, @bitCast(try self.takeU64())),
            else => DecodeError.BadType,
        };
    }

    pub fn readF64(self: *Decoder) DecodeError!f64 {
        const b = try self.takeByte();
        return switch (b) {
            0xCA => {
                const u: u32 = try self.takeU32();
                const f: f32 = @bitCast(u);
                return @as(f64, f);
            },
            0xCB => @bitCast(try self.takeU64()),
            else => DecodeError.BadType,
        };
    }

    /// Returns a slice INTO the decoder's buffer — valid until the
    /// decoder is re-initialised. No copy.
    pub fn readStr(self: *Decoder) DecodeError![]const u8 {
        const b = try self.takeByte();
        const n: usize = switch (b) {
            0xA0...0xBF => @as(usize, b & 0x1F),
            0xD9 => @intCast(try self.takeByte()),
            0xDA => @intCast(try self.takeU16()),
            0xDB => @intCast(try self.takeU32()),
            else => return DecodeError.BadType,
        };
        return self.takeBytes(n);
    }

    pub fn readBin(self: *Decoder) DecodeError![]const u8 {
        const b = try self.takeByte();
        const n: usize = switch (b) {
            0xC4 => @intCast(try self.takeByte()),
            0xC5 => @intCast(try self.takeU16()),
            0xC6 => @intCast(try self.takeU32()),
            else => return DecodeError.BadType,
        };
        return self.takeBytes(n);
    }

    /// Returns the element count; caller reads N values.
    pub fn readArrayHeader(self: *Decoder) DecodeError!u32 {
        const b = try self.takeByte();
        return switch (b) {
            0x90...0x9F => @as(u32, b & 0x0F),
            0xDC => @intCast(try self.takeU16()),
            0xDD => try self.takeU32(),
            else => DecodeError.BadType,
        };
    }

    pub fn readMapHeader(self: *Decoder) DecodeError!u32 {
        const b = try self.takeByte();
        return switch (b) {
            0x80...0x8F => @as(u32, b & 0x0F),
            0xDE => @intCast(try self.takeU16()),
            0xDF => try self.takeU32(),
            else => DecodeError.BadType,
        };
    }

    /// Skip the next value recursively (arrays/maps include their
    /// payloads). Used by callers that peek a value they don't care
    /// about.
    pub fn skip(self: *Decoder) DecodeError!void {
        const k = try self.peek();
        switch (k) {
            .nil => try self.readNil(),
            .bool_ => _ = try self.readBool(),
            .int, .uint => _ = try self.readInt(),
            .f32_, .f64_ => _ = try self.readF64(),
            .str => _ = try self.readStr(),
            .bin => _ = try self.readBin(),
            .array => {
                const n = try self.readArrayHeader();
                var i: u32 = 0;
                while (i < n) : (i += 1) try self.skip();
            },
            .map => {
                const n = try self.readMapHeader();
                var i: u32 = 0;
                while (i < n) : (i += 1) {
                    try self.skip(); // key
                    try self.skip(); // value
                }
            },
            .eof => return DecodeError.Truncated,
        }
    }
};

// ============================================================
// Tests
// ============================================================

test "encode+decode small positive int" {
    var buf: [16]u8 = undefined;
    var enc = Encoder.init(&buf);
    try enc.int(42);
    try std.testing.expectEqual(@as(usize, 1), enc.len);
    var dec = Decoder.init(buf[0..enc.len]);
    try std.testing.expectEqual(@as(i64, 42), try dec.readInt());
}

test "encode+decode negative int32" {
    var buf: [16]u8 = undefined;
    var enc = Encoder.init(&buf);
    try enc.int(-1000000);
    var dec = Decoder.init(buf[0..enc.len]);
    try std.testing.expectEqual(@as(i64, -1000000), try dec.readInt());
}

test "encode+decode string" {
    var buf: [64]u8 = undefined;
    var enc = Encoder.init(&buf);
    try enc.str("hello world");
    var dec = Decoder.init(buf[0..enc.len]);
    try std.testing.expectEqualStrings("hello world", try dec.readStr());
}

test "encode+decode f64" {
    var buf: [16]u8 = undefined;
    var enc = Encoder.init(&buf);
    try enc.f64_(3.14159);
    var dec = Decoder.init(buf[0..enc.len]);
    try std.testing.expectApproxEqAbs(@as(f64, 3.14159), try dec.readF64(), 1e-9);
}

test "array of mixed types" {
    var buf: [64]u8 = undefined;
    var enc = Encoder.init(&buf);
    try enc.arrayHeader(3);
    try enc.int(1);
    try enc.str("two");
    try enc.boolean(true);
    var dec = Decoder.init(buf[0..enc.len]);
    try std.testing.expectEqual(@as(u32, 3), try dec.readArrayHeader());
    try std.testing.expectEqual(@as(i64, 1), try dec.readInt());
    try std.testing.expectEqualStrings("two", try dec.readStr());
    try std.testing.expectEqual(true, try dec.readBool());
}

test "skip nested array" {
    var buf: [64]u8 = undefined;
    var enc = Encoder.init(&buf);
    try enc.arrayHeader(2);
    try enc.arrayHeader(2); // nested
    try enc.int(1);
    try enc.int(2);
    try enc.str("after");
    var dec = Decoder.init(buf[0..enc.len]);
    _ = try dec.readArrayHeader();
    try dec.skip(); // skip nested [1,2]
    try std.testing.expectEqualStrings("after", try dec.readStr());
}
