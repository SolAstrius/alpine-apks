// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! RPC client over the COBS+msgpack serial transport on
//! `/dev/ttyS1`. Mirrors the Java-side `ScevRpcManager` contract
//! exactly — correlation ids are picked by the guest, the host reflects
//! them back on the matching response, and events arrive async.

const std = @import("std");
const posix = std.posix;
// Zig 0.16 removed posix.close / posix.write (moved to std.Io). For this
// plain-CLI tool we keep things simple and call libc directly for those
// two — we already link musl. Reads can still go through std.posix.read
// because it was retained.
const c = std.c;
const cobs = @import("cobs.zig");
const mpack = @import("mpack.zig");

pub const MAX_FRAME: usize = 8192;

pub const TAG_REQUEST: i64 = 0;
pub const TAG_RESPONSE: i64 = 1;
pub const TAG_EVENT: i64 = 2;

pub const METHOD_PING = "ping";
pub const METHOD_LOG = "log";
pub const METHOD_LIST = "list";
pub const METHOD_METHODS = "methods";
pub const METHOD_CALL = "call";
pub const METHOD_QUEUE_EVENT = "queue_event";
pub const METHOD_SUBSCRIBE = "subscribe";
pub const METHOD_UNSUBSCRIBE = "unsubscribe";
pub const METHOD_DESCRIBE = "describe";
pub const METHOD_SCHEMA = "schema";
pub const METHOD_TYPE = "type";
pub const METHOD_TRACE = "trace";
pub const METHOD_SELF = "self";

pub const Error = error{
    OpenFailed,
    TcSetAttrFailed,
    WriteFailed,
    ReadFailed,
    Eof,
    Timeout,
    FrameTooLarge,
    CorruptFrame,
    ProtocolError,
    EncodeFailed,
} || mpack.DecodeError || cobs.CobsError;

/// Client state. Owns the serial fd and a rolling RX accumulator; the
/// latter carries partial frames across read() calls when a frame
/// spans multiple OS reads.
pub const Client = struct {
    fd: posix.fd_t,
    next_id: u64,
    rx: [MAX_FRAME]u8,
    rx_len: usize,
    // Scratch for decode. RX framing tears payload bytes out via COBS
    // and drops them here; stays valid until the next `recvFrame`.
    decoded: [MAX_FRAME]u8,
    // TX scratch: large enough to hold any request + its COBS overhead.
    encoded: [cobs.maxEncodedSize(MAX_FRAME)]u8,

    /// Open the serial device in raw mode. `path` defaults to
    /// "/dev/ttyS1" if null — matches the C impl and the layer the
    /// scev kernel exposes.
    pub fn open(path: ?[]const u8) !Client {
        const p = path orelse "/dev/ttyS1";
        // openZ wants a null-terminated path; stack-buffer it.
        var path_buf: [256]u8 = undefined;
        if (p.len >= path_buf.len) return error.OpenFailed;
        @memcpy(path_buf[0..p.len], p);
        path_buf[p.len] = 0;

        const fd = posix.openatZ(
            posix.AT.FDCWD,
            @ptrCast(&path_buf),
            .{ .ACCMODE = .RDWR, .NOCTTY = true, .CLOEXEC = true },
            0,
        ) catch return error.OpenFailed;
        errdefer _ = c.close(fd);

        try setRaw(fd);

        return .{
            .fd = fd,
            .next_id = 1,
            .rx = undefined,
            .rx_len = 0,
            .decoded = undefined,
            .encoded = undefined,
        };
    }

    pub fn close(self: *Client) void {
        _ = c.close(self.fd);
        self.fd = -1;
    }

    /// Build and send one msgpack+COBS-framed request. The `args_fn`
    /// callback writes one msgpack array into the encoder (the `args`
    /// slot of the request frame). Passing `null` sends an empty args
    /// array.
    pub fn sendRequest(
        self: *Client,
        id: u64,
        method: []const u8,
        args_fn: ?*const fn (*mpack.Encoder, ?*anyopaque) anyerror!void,
        args_user: ?*anyopaque,
    ) !void {
        var tx: [MAX_FRAME]u8 = undefined;
        var enc = mpack.Encoder.init(&tx);

        try enc.arrayHeader(4);
        try enc.int(TAG_REQUEST);
        try enc.int(@intCast(id));
        try enc.str(method);
        if (args_fn) |f| try f(&enc, args_user)
        else try enc.arrayHeader(0);

        try self.sendFrame(tx[0..enc.len]);
    }

    /// One request/response round-trip. Discards any interleaved events
    /// or stale responses and loops until the response with our `id`
    /// arrives or the timeout fires.
    ///
    /// Returns a slice into `self.decoded` that covers the msgpack-
    /// encoded result value (for success) OR the error message (for
    /// error). The caller checks `Response.is_error`.
    pub fn call(
        self: *Client,
        method: []const u8,
        args_fn: ?*const fn (*mpack.Encoder, ?*anyopaque) anyerror!void,
        args_user: ?*anyopaque,
        timeout_ms: i32,
    ) !Response {
        const id = self.next_id;
        self.next_id += 1;
        try self.sendRequest(id, method, args_fn, args_user);

        while (true) {
            const payload = try self.recvFrame(timeout_ms);
            var dec = mpack.Decoder.init(payload);
            const arr_n = dec.readArrayHeader() catch continue;
            if (arr_n < 1) continue;
            const tag = dec.readInt() catch continue;
            if (tag != TAG_RESPONSE or arr_n != 4) {
                // Event or malformed frame — drain the rest and
                // continue waiting for our response.
                var i: u32 = 1;
                while (i < arr_n) : (i += 1) dec.skip() catch break;
                continue;
            }
            const rsp_id = dec.readInt() catch continue;
            if (@as(u64, @intCast(rsp_id)) != id) {
                // Stale response from a previous call — skip.
                dec.skip() catch {};
                dec.skip() catch {};
                continue;
            }

            // err field: nil means success, string means error.
            const err_kind = dec.peek() catch return Error.ProtocolError;
            if (err_kind == .nil) {
                try dec.readNil();
                const body_start = dec.pos;
                return .{
                    .is_error = false,
                    .bytes = payload[body_start..],
                };
            }
            if (err_kind == .str) {
                const err_str = try dec.readStr();
                return .{
                    .is_error = true,
                    .bytes = err_str,
                };
            }
            return Error.ProtocolError;
        }
    }

    /// Block until the next inbound frame arrives. Returns a slice
    /// into `self.decoded`; valid until the next call to any recv-
    /// like method.
    pub fn recvFrame(self: *Client, timeout_ms: i32) ![]const u8 {
        while (true) {
            // Scan the accumulator for the first 0x00 delimiter. If
            // present, try to decode the preceding bytes as a COBS
            // frame; on corrupt frames we drop them and rescan so the
            // caller can recover from a lost byte.
            const delim = std.mem.indexOfScalar(u8, self.rx[0..self.rx_len], 0);
            if (delim) |enc_len| {
                const dec_len_or_err = cobs.decode(self.rx[0..enc_len], &self.decoded);
                self.consumeRx(enc_len + 1);
                const dec_len = dec_len_or_err catch continue; // drop bad frame, scan on
                return self.decoded[0..dec_len];
            }

            // No delimiter yet — need more bytes from the wire.
            if (self.rx_len >= MAX_FRAME) {
                // Frame ran over cap without ever showing a 0x00 — the
                // host is confused or we just lost sync. Reset.
                self.rx_len = 0;
                return Error.FrameTooLarge;
            }
            try self.waitReadable(timeout_ms);
            const n = posix.read(self.fd, self.rx[self.rx_len..]) catch return Error.ReadFailed;
            if (n == 0) return Error.Eof;
            self.rx_len += n;
        }
    }

    fn consumeRx(self: *Client, bytes: usize) void {
        if (self.rx_len > bytes) {
            std.mem.copyForwards(u8, self.rx[0..], self.rx[bytes..self.rx_len]);
            self.rx_len -= bytes;
        } else {
            self.rx_len = 0;
        }
    }

    fn sendFrame(self: *Client, payload: []const u8) !void {
        if (payload.len > MAX_FRAME) return Error.FrameTooLarge;
        const n = cobs.encode(payload, &self.encoded);
        var off: usize = 0;
        while (off < n) {
            const rc = c.write(self.fd, self.encoded[off..].ptr, n - off);
            if (rc < 0) return Error.WriteFailed;
            if (rc == 0) return Error.WriteFailed;
            off += @intCast(rc);
        }
    }

    fn waitReadable(self: *Client, timeout_ms: i32) !void {
        var pfd = [_]posix.pollfd{.{
            .fd = self.fd,
            .events = posix.POLL.IN,
            .revents = 0,
        }};
        const ready = posix.poll(&pfd, timeout_ms) catch return Error.ReadFailed;
        if (ready == 0) return Error.Timeout;
    }
};

pub const Response = struct {
    is_error: bool,
    /// When `is_error == false`: the raw msgpack bytes of the result
    /// value — caller runs a Decoder over them.
    /// When `is_error == true`: the error message bytes (UTF-8).
    bytes: []const u8,
};

/// Put the fd in raw 115200 8N1 mode — matches the scev NS16550A
/// emulator's baud/format expectations. Raw-mode strips tty line-
/// discipline so we see every byte the host wrote.
fn setRaw(fd: posix.fd_t) !void {
    var t = posix.tcgetattr(fd) catch return Error.TcSetAttrFailed;

    // Manual cfmakeraw — Zig std doesn't expose the BSD helper, and
    // the termios flag fields are platform-typed bit-sets so we zero
    // them by overwriting with defaults.
    t.iflag = @bitCast(@as(u32, 0));
    t.oflag = @bitCast(@as(u32, 0));
    t.lflag = @bitCast(@as(u32, 0));
    t.cflag.CSIZE = .CS8;
    t.cflag.PARENB = false;
    t.cflag.CREAD = true;
    t.cc[@intFromEnum(posix.V.MIN)] = 0;
    t.cc[@intFromEnum(posix.V.TIME)] = 0;

    posix.tcsetattr(fd, .NOW, t) catch return Error.TcSetAttrFailed;
}
