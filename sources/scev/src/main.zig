// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! scev — guest-side CLI for the Scalar Evolution host RPC.
//!
//! Subcommands:
//!   scev ping                        — liveness; prints "pong" on success
//!   scev log <level> <msg>           — server-side log at slf4j level
//!   scev call <periph> <method> ...  — CC: Tweaked peripheral call
//!   scev events [count]              — subscribe and print events
//!
//! Typed arg prefixes for `call` (bare tokens default to string):
//!   s:hello   string
//!   i:42      int
//!   f:3.14    double
//!   b:true    bool
//!   n:        nil
//!
//! Zig port of the original C implementation at ../scev-guest/.
//! Same wire protocol, same flag surface.

const std = @import("std");
const mpack = @import("mpack.zig");
const rpc = @import("rpc.zig");

const SCEV_VERSION = "0.1-zig";

pub fn main() u8 {
    return runMain() catch |e| {
        std.debug.print("scev: unhandled error: {s}\n", .{@errorName(e)});
        return 1;
    };
}

fn runMain() !u8 {
    var args_it = std.process.args();
    defer args_it.deinit();
    _ = args_it.next() orelse return exitUsage();

    const cmd = args_it.next() orelse return exitUsage();

    // Slurp the rest of argv into a slice so subcommands can index it.
    var rest = std.ArrayListUnmanaged([]const u8){};
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const alloc = gpa.allocator();
    defer rest.deinit(alloc);
    while (args_it.next()) |a| try rest.append(alloc, a);

    if (std.mem.eql(u8, cmd, "--help") or std.mem.eql(u8, cmd, "-h")) {
        printUsage();
        return 0;
    }

    const serial_path: ?[]const u8 = std.posix.getenv("SCEV_SERIAL");
    var client = rpc.Client.open(serial_path) catch |e| {
        std.debug.print("scev: cannot open {s}: {s}\n", .{
            serial_path orelse "/dev/ttyS1", @errorName(e),
        });
        return 74; // EX_IOERR
    };
    defer client.close();

    if (std.mem.eql(u8, cmd, "ping")) return try cmdPing(&client);
    if (std.mem.eql(u8, cmd, "log"))  return try cmdLog(&client, rest.items, alloc);
    if (std.mem.eql(u8, cmd, "call")) return try cmdCall(&client, rest.items);
    if (std.mem.eql(u8, cmd, "events")) return try cmdEvents(&client, rest.items);
    if (std.mem.eql(u8, cmd, "list")) return try cmdList(&client);
    if (std.mem.eql(u8, cmd, "methods")) return try cmdMethods(&client, rest.items);
    if (std.mem.eql(u8, cmd, "type")) return try cmdType(&client, rest.items);

    std.debug.print("scev: unknown subcommand '{s}'\n", .{cmd});
    printUsage();
    return 64;
}

// ---------------- Subcommands ----------------

fn cmdPing(c: *rpc.Client) !u8 {
    return runCall(c, rpc.METHOD_PING, null, null, 3000);
}

fn cmdLog(c: *rpc.Client, rest: [][]const u8, alloc: std.mem.Allocator) !u8 {
    if (rest.len < 2) {
        std.debug.print("usage: scev log <level> <msg...>\n", .{});
        return 64;
    }
    const level = rest[0];
    // Join all remaining tokens with single spaces for ergonomic CLI.
    var msg_buf = std.ArrayListUnmanaged(u8){};
    defer msg_buf.deinit(alloc);
    for (rest[1..], 0..) |tok, i| {
        if (i != 0) try msg_buf.append(alloc, ' ');
        try msg_buf.appendSlice(alloc, tok);
    }
    const msg = msg_buf.items;

    const Ctx = struct { lvl: []const u8, m: []const u8 };
    var ctx = Ctx{ .lvl = level, .m = msg };
    const gen = struct {
        fn emit(e: *mpack.Encoder, user: ?*anyopaque) anyerror!void {
            const c2: *Ctx = @ptrCast(@alignCast(user.?));
            try e.arrayHeader(2);
            try e.str(c2.lvl);
            try e.str(c2.m);
        }
    };
    return runCall(c, rpc.METHOD_LOG, gen.emit, @ptrCast(&ctx), 3000);
}

fn cmdCall(c: *rpc.Client, rest: [][]const u8) !u8 {
    if (rest.len < 2) {
        std.debug.print("usage: scev call <peripheral> <method> [args...]\n", .{});
        return 64;
    }
    var ctx = ArgsCtx{ .toks = rest };
    const gen = struct {
        fn emit(e: *mpack.Encoder, user: ?*anyopaque) anyerror!void {
            const c2: *ArgsCtx = @ptrCast(@alignCast(user.?));
            try e.arrayHeader(@intCast(c2.toks.len));
            for (c2.toks) |tok| try emitTypedArg(e, tok);
        }
    };
    return runCall(c, rpc.METHOD_CALL, gen.emit, @ptrCast(&ctx), 15000);
}

/// `scev list` — CC's peripheral.getNames(), roughly.
///
/// Hits METHOD_LIST which returns an array of `{peer, types}` maps;
/// prints one line per peripheral in `name  type[+extraType...]`
/// form so shell-scripting scev is pleasant.
fn cmdList(c: *rpc.Client) !u8 {
    const resp = c.call(rpc.METHOD_LIST, null, null, 5000) catch |e| {
        std.debug.print("scev: rpc error: {s}\n", .{@errorName(e)});
        return 1;
    };
    if (resp.is_error) {
        std.debug.print("scev: rpc returned error: {s}\n", .{resp.bytes});
        return 2;
    }
    var dec = mpack.Decoder.init(resp.bytes);
    const n = dec.readArrayHeader() catch {
        std.debug.print("scev: malformed list response\n", .{});
        return 1;
    };
    var i: u32 = 0;
    while (i < n) : (i += 1) {
        // Each entry is a map with keys "peer" and "types"; accept
        // them in any order rather than hard-coding the insertion
        // order the host uses today.
        const m = dec.readMapHeader() catch return 1;
        var peer: []const u8 = "?";
        // Buffered printing of types — keep it tiny; nothing past ~8
        // types is plausible for a CC peripheral and we just need
        // "name  t1+t2+t3\n".
        var types_buf: [256]u8 = undefined;
        var types_pos: usize = 0;
        var j: u32 = 0;
        while (j < m) : (j += 1) {
            const key = dec.readStr() catch return 1;
            if (std.mem.eql(u8, key, "peer")) {
                peer = dec.readStr() catch return 1;
            } else if (std.mem.eql(u8, key, "types")) {
                const tn = dec.readArrayHeader() catch return 1;
                var tk: u32 = 0;
                while (tk < tn) : (tk += 1) {
                    const t = dec.readStr() catch return 1;
                    if (tk != 0 and types_pos < types_buf.len) {
                        types_buf[types_pos] = '+';
                        types_pos += 1;
                    }
                    const copy_n = @min(t.len, types_buf.len - types_pos);
                    @memcpy(types_buf[types_pos .. types_pos + copy_n], t[0..copy_n]);
                    types_pos += copy_n;
                }
            } else {
                dec.skip() catch return 1;
            }
        }
        std.debug.print("{s}  {s}\n", .{ peer, types_buf[0..types_pos] });
    }
    return 0;
}

/// `scev methods <peer>` — CC's peripheral.getMethods(name). Prints
/// each method on its own line.
fn cmdMethods(c: *rpc.Client, rest: [][]const u8) !u8 {
    if (rest.len < 1) {
        std.debug.print("usage: scev methods <peer>\n", .{});
        return 64;
    }
    var ctx = ArgsCtx{ .toks = rest };
    const gen = struct {
        fn emit(e: *mpack.Encoder, user: ?*anyopaque) anyerror!void {
            const c2: *ArgsCtx = @ptrCast(@alignCast(user.?));
            try e.arrayHeader(1);
            try e.str(c2.toks[0]);
        }
    };
    const resp = c.call(rpc.METHOD_METHODS, gen.emit, @ptrCast(&ctx), 5000) catch |e| {
        std.debug.print("scev: rpc error: {s}\n", .{@errorName(e)});
        return 1;
    };
    if (resp.is_error) {
        std.debug.print("scev: rpc returned error: {s}\n", .{resp.bytes});
        return 2;
    }
    var dec = mpack.Decoder.init(resp.bytes);
    const n = dec.readArrayHeader() catch return 1;
    var i: u32 = 0;
    while (i < n) : (i += 1) {
        const name = dec.readStr() catch return 1;
        std.debug.print("{s}\n", .{name});
    }
    return 0;
}

/// `scev type <peer>` — CC's peripheral.getType(name). Currently runs
/// `scev list` under the hood and filters for the matching name;
/// could be a dedicated RPC later if the cost shows up.
fn cmdType(c: *rpc.Client, rest: [][]const u8) !u8 {
    if (rest.len < 1) {
        std.debug.print("usage: scev type <peer>\n", .{});
        return 64;
    }
    const want = rest[0];
    const resp = c.call(rpc.METHOD_LIST, null, null, 5000) catch |e| {
        std.debug.print("scev: rpc error: {s}\n", .{@errorName(e)});
        return 1;
    };
    if (resp.is_error) {
        std.debug.print("scev: rpc returned error: {s}\n", .{resp.bytes});
        return 2;
    }
    var dec = mpack.Decoder.init(resp.bytes);
    const n = dec.readArrayHeader() catch return 1;
    var i: u32 = 0;
    while (i < n) : (i += 1) {
        const m = dec.readMapHeader() catch return 1;
        var peer: []const u8 = "";
        var matched = false;
        var types_buf: [256]u8 = undefined;
        var types_pos: usize = 0;
        var j: u32 = 0;
        while (j < m) : (j += 1) {
            const key = dec.readStr() catch return 1;
            if (std.mem.eql(u8, key, "peer")) {
                peer = dec.readStr() catch return 1;
                matched = std.mem.eql(u8, peer, want);
            } else if (std.mem.eql(u8, key, "types")) {
                const tn = dec.readArrayHeader() catch return 1;
                var tk: u32 = 0;
                while (tk < tn) : (tk += 1) {
                    const t = dec.readStr() catch return 1;
                    if (tk != 0 and types_pos < types_buf.len) {
                        types_buf[types_pos] = '\n';
                        types_pos += 1;
                    }
                    const copy_n = @min(t.len, types_buf.len - types_pos);
                    @memcpy(types_buf[types_pos .. types_pos + copy_n], t[0..copy_n]);
                    types_pos += copy_n;
                }
            } else {
                dec.skip() catch return 1;
            }
        }
        if (matched) {
            std.debug.print("{s}\n", .{types_buf[0..types_pos]});
            return 0;
        }
    }
    std.debug.print("scev: no such peripheral: {s}\n", .{want});
    return 2;
}

fn cmdEvents(c: *rpc.Client, rest: [][]const u8) !u8 {
    const max_count: i32 = if (rest.len >= 1) std.fmt.parseInt(i32, rest[0], 10) catch -1 else -1;

    // Subscribe first — no-op until the server grows real subscription
    // support, but the plumbing is in place.
    _ = runCall(c, rpc.METHOD_SUBSCRIBE, null, null, 3000) catch {};

    var count: i32 = 0;
    while (true) {
        const frame = c.recvFrame(-1) catch |e| {
            std.debug.print("scev: recv error: {s}\n", .{@errorName(e)});
            return 1;
        };
        var dec = mpack.Decoder.init(frame);
        const arr_n = dec.readArrayHeader() catch continue;
        if (arr_n < 1) continue;
        const tag = dec.readInt() catch continue;
        if (tag != rpc.TAG_EVENT) continue;
        const name = dec.readStr() catch continue;
        var stdout_buf: [128]u8 = undefined;
        _ = std.fmt.bufPrint(&stdout_buf, "{s} ", .{name}) catch {};
        std.debug.print("{s} ", .{name});
        printValue(&dec, 0);
        std.debug.print("\n", .{});
        count += 1;
        if (max_count > 0 and count >= max_count) return 0;
    }
}

const ArgsCtx = struct { toks: [][]const u8 };

fn emitTypedArg(e: *mpack.Encoder, tok: []const u8) !void {
    // Typed prefix: two-char "t:" where t is {s,i,f,b,n}. Bare tokens
    // (no prefix or unknown type) encode as strings — matches the C
    // CLI for ergonomic round-trips from shells.
    if (tok.len >= 2 and tok[1] == ':') {
        const v = tok[2..];
        switch (tok[0]) {
            's' => return e.str(v),
            'i' => {
                const n = try std.fmt.parseInt(i64, v, 10);
                return e.int(n);
            },
            'f' => {
                const f = try std.fmt.parseFloat(f64, v);
                return e.f64_(f);
            },
            'b' => {
                const truthy = std.mem.eql(u8, v, "true") or std.mem.eql(u8, v, "1");
                return e.boolean(truthy);
            },
            'n' => return e.nil(),
            else => {},
        }
    }
    try e.str(tok);
}

// ---------------- Result printer ----------------

fn printValue(d: *mpack.Decoder, depth: u32) void {
    if (depth > 64) {
        std.debug.print("<nested too deep>", .{});
        return;
    }
    const k = d.peek() catch {
        std.debug.print("<err>", .{});
        return;
    };
    switch (k) {
        .nil => {
            d.readNil() catch {};
            std.debug.print("nil", .{});
        },
        .bool_ => {
            const b = d.readBool() catch false;
            std.debug.print("{s}", .{if (b) "true" else "false"});
        },
        .int, .uint => {
            const n = d.readInt() catch 0;
            std.debug.print("{d}", .{n});
        },
        .f32_, .f64_ => {
            const f = d.readF64() catch 0.0;
            std.debug.print("{d}", .{f});
        },
        .str => {
            const s = d.readStr() catch "";
            std.debug.print("\"{s}\"", .{s});
        },
        .bin => {
            const b = d.readBin() catch "";
            std.debug.print("<bin:{d}>", .{b.len});
        },
        .array => {
            const n = d.readArrayHeader() catch 0;
            std.debug.print("[", .{});
            var i: u32 = 0;
            while (i < n) : (i += 1) {
                if (i != 0) std.debug.print(", ", .{});
                printValue(d, depth + 1);
            }
            std.debug.print("]", .{});
        },
        .map => {
            const n = d.readMapHeader() catch 0;
            std.debug.print("{{", .{});
            var i: u32 = 0;
            while (i < n) : (i += 1) {
                if (i != 0) std.debug.print(", ", .{});
                printValue(d, depth + 1);
                std.debug.print(": ", .{});
                printValue(d, depth + 1);
            }
            std.debug.print("}}", .{});
        },
        .eof => std.debug.print("<eof>", .{}),
    }
}

// ---------------- Request driver ----------------

fn runCall(
    c: *rpc.Client,
    method: []const u8,
    args_fn: ?*const fn (*mpack.Encoder, ?*anyopaque) anyerror!void,
    args_user: ?*anyopaque,
    timeout_ms: i32,
) !u8 {
    const resp = c.call(method, args_fn, args_user, timeout_ms) catch |e| {
        std.debug.print("scev: rpc error: {s}\n", .{@errorName(e)});
        return 1;
    };
    if (resp.is_error) {
        std.debug.print("scev: rpc returned error: {s}\n", .{resp.bytes});
        return 2;
    }
    var dec = mpack.Decoder.init(resp.bytes);
    printValue(&dec, 0);
    std.debug.print("\n", .{});
    return 0;
}

// ---------------- Usage ----------------

fn printUsage() void {
    std.debug.print(
        \\scev {s}
        \\
        \\usage: scev <subcommand> [args...]
        \\
        \\subcommands:
        \\  ping                           liveness check
        \\  log <level> <msg>              log to host (trace|debug|info|warn|error)
        \\  list                           list peripherals (CC's peripheral.getNames)
        \\  type <peripheral>              print a peripheral's type(s)
        \\  methods <peripheral>           list a peripheral's methods
        \\  call <peripheral> <method> ... call a peripheral method
        \\  events [count]                 subscribe and print events
        \\
        \\argument types (prefix with 't:' where t is):
        \\  s:hello   string (default for bare tokens)
        \\  i:42      int
        \\  f:3.14    double
        \\  b:true    bool
        \\  n:        nil
        \\
        \\environment:
        \\  SCEV_SERIAL   override serial device (default /dev/ttyS1)
        \\
    ,
        .{SCEV_VERSION},
    );
}

fn exitUsage() !u8 {
    printUsage();
    return 64;
}
