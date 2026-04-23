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
//! Ported from an earlier C implementation; same wire protocol and flag surface.

const std = @import("std");
const mpack = @import("mpack.zig");
const rpc = @import("rpc.zig");

const SCEV_VERSION = "0.1-zig";

pub fn main(init: std.process.Init.Minimal) u8 {
    return runMain(init) catch |e| {
        std.debug.print("scev: unhandled error: {s}\n", .{@errorName(e)});
        return 1;
    };
}

fn runMain(init: std.process.Init.Minimal) !u8 {
    // POSIX Iterator.init needs no allocator and no deinit — the args
    // vector is already laid out in process memory by the loader.
    var args_it = std.process.Args.Iterator.init(init.args);
    _ = args_it.next() orelse return exitUsage();

    const cmd = args_it.next() orelse return exitUsage();

    // Slurp the rest of argv into a slice so subcommands can index it.
    // We link libc, so the C allocator is the cheapest real allocator.
    const alloc = std.heap.c_allocator;
    var rest: std.ArrayList([]const u8) = .empty;
    defer rest.deinit(alloc);
    while (args_it.next()) |a| try rest.append(alloc, a);

    if (std.mem.eql(u8, cmd, "--help") or std.mem.eql(u8, cmd, "-h")) {
        printUsage();
        return 0;
    }

    // std.c.getenv returns ?[*:0]const u8; span() gives us the slice.
    const serial_env = std.c.getenv("SCEV_SERIAL");
    const serial_path: ?[]const u8 = if (serial_env) |p| std.mem.span(p) else null;
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
    if (std.mem.eql(u8, cmd, "describe")) return try cmdDescribe(&client, rest.items);
    if (std.mem.eql(u8, cmd, "schema")) return try cmdSchema(&client, rest.items);
    if (std.mem.eql(u8, cmd, "trace")) return try cmdTrace(&client, rest.items);
    if (std.mem.eql(u8, cmd, "self")) return try cmdSelf(&client);
    if (std.mem.eql(u8, cmd, "find")) return try cmdFind(&client, rest.items);
    if (std.mem.eql(u8, cmd, "methods-like")) return try cmdMethodsLike(&client, rest.items);

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
    var msg_buf: std.ArrayList(u8) = .empty;
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

/// `scev describe <peer> [method]` — reflection-derived signatures
/// for every `@LuaFunction` on a peripheral, grouped by declaring
/// class. Pretty-printed, not raw JSON: this is the primary "no docs
/// available" discovery surface.
///
/// Second argument narrows to a single method.
fn cmdDescribe(c: *rpc.Client, rest: [][]const u8) !u8 {
    if (rest.len < 1) {
        std.debug.print("usage: scev describe <peer> [method]\n", .{});
        return 64;
    }
    var ctx = ArgsCtx{ .toks = rest };
    const gen = struct {
        fn emit(e: *mpack.Encoder, user: ?*anyopaque) anyerror!void {
            const c2: *ArgsCtx = @ptrCast(@alignCast(user.?));
            try e.arrayHeader(@intCast(c2.toks.len));
            for (c2.toks) |tok| try e.str(tok);
        }
    };
    const resp = c.call(rpc.METHOD_DESCRIBE, gen.emit, @ptrCast(&ctx), 10000) catch |e| {
        std.debug.print("scev: rpc error: {s}\n", .{@errorName(e)});
        return 1;
    };
    if (resp.is_error) {
        std.debug.print("scev: rpc returned error: {s}\n", .{resp.bytes});
        return 2;
    }
    // The response is a MsgValue.Map — parse it and pretty-print.
    var dec = mpack.Decoder.init(resp.bytes);
    printDescribe(&dec) catch {
        std.debug.print("scev: malformed describe response\n", .{});
        return 1;
    };
    return 0;
}

/// `scev schema [event | clear]` — observed event argument shapes.
/// No arg → all events. `clear` resets the learner. Otherwise filters
/// to one event.
fn cmdSchema(c: *rpc.Client, rest: [][]const u8) !u8 {
    var ctx = ArgsCtx{ .toks = rest };
    const gen = struct {
        fn emit(e: *mpack.Encoder, user: ?*anyopaque) anyerror!void {
            const c2: *ArgsCtx = @ptrCast(@alignCast(user.?));
            try e.arrayHeader(@intCast(c2.toks.len));
            for (c2.toks) |tok| try e.str(tok);
        }
    };
    const resp = c.call(rpc.METHOD_SCHEMA, gen.emit, @ptrCast(&ctx), 5000) catch |e| {
        std.debug.print("scev: rpc error: {s}\n", .{@errorName(e)});
        return 1;
    };
    if (resp.is_error) {
        std.debug.print("scev: rpc returned error: {s}\n", .{resp.bytes});
        return 2;
    }
    // Single-event responses are a map; multi-event is an array of
    // maps. Peek the first byte's type to decide.
    var dec = mpack.Decoder.init(resp.bytes);
    const k = dec.peek() catch {
        std.debug.print("scev: malformed schema response\n", .{});
        return 1;
    };
    switch (k) {
        .map => printSchemaEntry(&dec) catch return 1,
        .array => {
            const n = dec.readArrayHeader() catch return 1;
            var i: u32 = 0;
            while (i < n) : (i += 1) {
                printSchemaEntry(&dec) catch return 1;
            }
        },
        .nil => {}, // `clear` returns NIL — nothing to print
        else => printValue(&dec, 0),
    }
    return 0;
}

/// `scev trace [on|off|dump|clear|status]` — dispatch-trace control.
/// Default subcommand is `dump`.
fn cmdTrace(c: *rpc.Client, rest: [][]const u8) !u8 {
    var ctx = ArgsCtx{ .toks = rest };
    const gen = struct {
        fn emit(e: *mpack.Encoder, user: ?*anyopaque) anyerror!void {
            const c2: *ArgsCtx = @ptrCast(@alignCast(user.?));
            try e.arrayHeader(@intCast(c2.toks.len));
            for (c2.toks) |tok| try e.str(tok);
        }
    };
    const resp = c.call(rpc.METHOD_TRACE, gen.emit, @ptrCast(&ctx), 5000) catch |e| {
        std.debug.print("scev: rpc error: {s}\n", .{@errorName(e)});
        return 1;
    };
    if (resp.is_error) {
        std.debug.print("scev: rpc returned error: {s}\n", .{resp.bytes});
        return 2;
    }
    var dec = mpack.Decoder.init(resp.bytes);
    const k = dec.peek() catch return 1;
    switch (k) {
        .array => {
            const n = dec.readArrayHeader() catch return 1;
            var i: u32 = 0;
            while (i < n) : (i += 1) {
                printTraceEntry(&dec) catch return 1;
            }
        },
        else => {
            printValue(&dec, 0);
            std.debug.print("\n", .{});
        },
    }
    return 0;
}

/// `scev self` — machine environment info. Does not leak host mod
/// identity or world location by design.
fn cmdSelf(c: *rpc.Client) !u8 {
    return runCall(c, rpc.METHOD_SELF, null, null, 3000);
}

/// `scev find <type>` — client-side filter over `list`. Prints
/// peripheral names whose type set contains the requested type.
fn cmdFind(c: *rpc.Client, rest: [][]const u8) !u8 {
    if (rest.len < 1) {
        std.debug.print("usage: scev find <type>\n", .{});
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
    var hits: u32 = 0;
    while (i < n) : (i += 1) {
        const m = dec.readMapHeader() catch return 1;
        var peer: []const u8 = "";
        var matched = false;
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
                    if (std.mem.eql(u8, t, want)) matched = true;
                }
            } else {
                dec.skip() catch return 1;
            }
        }
        if (matched) {
            std.debug.print("{s}\n", .{peer});
            hits += 1;
        }
    }
    return if (hits == 0) 2 else 0;
}

/// `scev methods-like <substring>` — fuzzy method search across all
/// peripherals. Prints `peer:method` for each match. Issues one
/// `list` + one `methods` per peripheral — fine for dozens, not for
/// hundreds.
fn cmdMethodsLike(c: *rpc.Client, rest: [][]const u8) !u8 {
    if (rest.len < 1) {
        std.debug.print("usage: scev methods-like <substring>\n", .{});
        return 64;
    }
    const needle = rest[0];
    const list_resp = c.call(rpc.METHOD_LIST, null, null, 5000) catch |e| {
        std.debug.print("scev: rpc error: {s}\n", .{@errorName(e)});
        return 1;
    };
    if (list_resp.is_error) {
        std.debug.print("scev: rpc returned error: {s}\n", .{list_resp.bytes});
        return 2;
    }
    // Collect peer names first (we reuse the decoder buffer per
    // methods call).
    var peers_buf: [64][]const u8 = undefined;
    var peers_n: usize = 0;
    var list_dec = mpack.Decoder.init(list_resp.bytes);
    const ln = list_dec.readArrayHeader() catch return 1;
    var li: u32 = 0;
    while (li < ln and peers_n < peers_buf.len) : (li += 1) {
        const m = list_dec.readMapHeader() catch return 1;
        var j: u32 = 0;
        while (j < m) : (j += 1) {
            const key = list_dec.readStr() catch return 1;
            if (std.mem.eql(u8, key, "peer")) {
                const p = list_dec.readStr() catch return 1;
                peers_buf[peers_n] = p;
                peers_n += 1;
            } else {
                list_dec.skip() catch return 1;
            }
        }
    }
    var hits: u32 = 0;
    var pi: usize = 0;
    while (pi < peers_n) : (pi += 1) {
        const peer = peers_buf[pi];
        var single = [_][]const u8{peer};
        var ctx = ArgsCtx{ .toks = &single };
        const gen = struct {
            fn emit(e: *mpack.Encoder, user: ?*anyopaque) anyerror!void {
                const c2: *ArgsCtx = @ptrCast(@alignCast(user.?));
                try e.arrayHeader(1);
                try e.str(c2.toks[0]);
            }
        };
        const resp = c.call(rpc.METHOD_METHODS, gen.emit, @ptrCast(&ctx), 5000) catch continue;
        if (resp.is_error) continue;
        var mdec = mpack.Decoder.init(resp.bytes);
        const mn = mdec.readArrayHeader() catch continue;
        var mi: u32 = 0;
        while (mi < mn) : (mi += 1) {
            const name = mdec.readStr() catch break;
            if (std.mem.indexOf(u8, name, needle) != null) {
                std.debug.print("{s}:{s}\n", .{ peer, name });
                hits += 1;
            }
        }
    }
    return if (hits == 0) 2 else 0;
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

// ---------------- Structured printers ----------------

/// Pretty-print a `describe` response map. Two shapes:
///   - full: { peer, type, types, class, groups: { class: [sig, ...] } }
///   - narrow: { peer, type, types, class, method: sig }
fn printDescribe(d: *mpack.Decoder) anyerror!void {
    const m = try d.readMapHeader();
    var peer: []const u8 = "?";
    var type_s: []const u8 = "?";
    var class_s: []const u8 = "?";
    // We can't random-access a MsgValue map through this streaming
    // decoder, so we collect into local state during one pass and
    // defer group-printing until we've read the header keys. Stash
    // the groups/method payload offset and re-decode from there if
    // seen — but mpack.Decoder doesn't expose seeking, so we print
    // inline once we hit them. The header keys come first in our
    // own encoder's output, so in practice "peer/type/types/class"
    // arrive before "groups"/"method". Defensive: tolerate any
    // order by printing a short header when we hit groups/method.
    var printed_header = false;
    var k: u32 = 0;
    while (k < m) : (k += 1) {
        const key = try d.readStr();
        if (std.mem.eql(u8, key, "peer")) {
            peer = try d.readStr();
        } else if (std.mem.eql(u8, key, "type")) {
            type_s = try d.readStr();
        } else if (std.mem.eql(u8, key, "types")) {
            // swallow
            const tn = try d.readArrayHeader();
            var ti: u32 = 0;
            while (ti < tn) : (ti += 1) _ = try d.readStr();
        } else if (std.mem.eql(u8, key, "class")) {
            class_s = try d.readStr();
        } else if (std.mem.eql(u8, key, "dynamic")) {
            _ = try d.readBool();
            // Remote peripheral — flat methods list follows.
        } else if (std.mem.eql(u8, key, "methods")) {
            // Remote peripheral fallback: just a name list.
            if (!printed_header) {
                std.debug.print("{s} ({s}) [remote]\n", .{ peer, type_s });
                printed_header = true;
            }
            const mn = try d.readArrayHeader();
            var mi: u32 = 0;
            while (mi < mn) : (mi += 1) {
                const name = try d.readStr();
                std.debug.print("  {s}(...)  [unknown signature — remote peripheral]\n", .{name});
            }
        } else if (std.mem.eql(u8, key, "groups")) {
            if (!printed_header) {
                std.debug.print("{s} ({s})\n  class: {s}\n", .{ peer, type_s, class_s });
                printed_header = true;
            }
            const gn = try d.readMapHeader();
            var gi: u32 = 0;
            while (gi < gn) : (gi += 1) {
                const group = try d.readStr();
                std.debug.print("  [{s}]\n", .{group});
                const arr_n = try d.readArrayHeader();
                var ai: u32 = 0;
                while (ai < arr_n) : (ai += 1) try printSignatureRow(d);
            }
        } else if (std.mem.eql(u8, key, "method")) {
            if (!printed_header) {
                std.debug.print("{s} ({s})\n  class: {s}\n", .{ peer, type_s, class_s });
                printed_header = true;
            }
            try printSignatureRow(d);
        } else {
            try d.skip();
        }
    }
}

/// One signature row: `name(p0: type?, p1: type ∈ {a|b}) -> shape [mainThread]`.
/// Re-derives the row from the structured params/return fields so the
/// formatting stays consistent even if the host tweaks its
/// signatureString template.
fn printSignatureRow(d: *mpack.Decoder) anyerror!void {
    const m = try d.readMapHeader();
    var name: []const u8 = "?";
    var aliases_buf: [4][]const u8 = undefined;
    var aliases_n: usize = 0;
    // Stash params while reading; print after the closing paren.
    const Param = struct {
        luaType: []const u8 = "?",
        optional: bool = false,
        hasEnum: bool = false,
        enum_buf: [64]u8 = undefined,
        enum_len: usize = 0,
    };
    var params: [16]Param = undefined;
    var params_n: usize = 0;
    var return_s: []const u8 = "one";
    var main_thread = false;
    var unsafe_flag = false;

    var k: u32 = 0;
    while (k < m) : (k += 1) {
        const key = try d.readStr();
        if (std.mem.eql(u8, key, "name")) {
            name = try d.readStr();
        } else if (std.mem.eql(u8, key, "aliases")) {
            const an = try d.readArrayHeader();
            var ai: u32 = 0;
            while (ai < an) : (ai += 1) {
                const a = try d.readStr();
                if (aliases_n < aliases_buf.len) {
                    aliases_buf[aliases_n] = a;
                    aliases_n += 1;
                }
            }
        } else if (std.mem.eql(u8, key, "params")) {
            const pn = try d.readArrayHeader();
            var pi: u32 = 0;
            while (pi < pn) : (pi += 1) {
                var p: Param = .{};
                const pm = try d.readMapHeader();
                var pk: u32 = 0;
                while (pk < pm) : (pk += 1) {
                    const pkey = try d.readStr();
                    if (std.mem.eql(u8, pkey, "luaType")) {
                        p.luaType = try d.readStr();
                    } else if (std.mem.eql(u8, pkey, "optional")) {
                        p.optional = try d.readBool();
                    } else if (std.mem.eql(u8, pkey, "enumValues")) {
                        p.hasEnum = true;
                        const en = try d.readArrayHeader();
                        var ei: u32 = 0;
                        while (ei < en) : (ei += 1) {
                            const v = try d.readStr();
                            if (p.enum_len + v.len + 1 < p.enum_buf.len) {
                                if (ei != 0) {
                                    p.enum_buf[p.enum_len] = '|';
                                    p.enum_len += 1;
                                }
                                @memcpy(p.enum_buf[p.enum_len .. p.enum_len + v.len], v);
                                p.enum_len += v.len;
                            } else {
                                try d.skip();
                            }
                        }
                    } else {
                        try d.skip();
                    }
                }
                if (params_n < params.len) {
                    params[params_n] = p;
                    params_n += 1;
                }
            }
        } else if (std.mem.eql(u8, key, "return")) {
            return_s = try d.readStr();
        } else if (std.mem.eql(u8, key, "mainThread")) {
            main_thread = try d.readBool();
        } else if (std.mem.eql(u8, key, "unsafe")) {
            unsafe_flag = try d.readBool();
        } else {
            try d.skip();
        }
    }

    std.debug.print("    {s}(", .{name});
    var pi: usize = 0;
    while (pi < params_n) : (pi += 1) {
        if (pi != 0) std.debug.print(", ", .{});
        const p = params[pi];
        const opt_marker: []const u8 = if (p.optional) "?" else "";
        if (p.hasEnum and p.enum_len > 0) {
            std.debug.print("arg{d}: {s}{s} ∈ {{{s}}}", .{ pi, p.luaType, opt_marker, p.enum_buf[0..p.enum_len] });
        } else {
            std.debug.print("arg{d}: {s}{s}", .{ pi, p.luaType, opt_marker });
        }
    }
    std.debug.print("): ", .{});
    if (std.mem.eql(u8, return_s, "none")) {
        std.debug.print("nil", .{});
    } else if (std.mem.eql(u8, return_s, "many")) {
        std.debug.print("value, ...", .{});
    } else if (std.mem.eql(u8, return_s, "dynamic")) {
        std.debug.print("dynamic", .{});
    } else {
        std.debug.print("value", .{});
    }
    if (main_thread) std.debug.print("  [mainThread]", .{});
    if (unsafe_flag) std.debug.print("  [unsafe]", .{});
    if (aliases_n > 0) {
        std.debug.print("  aliases:", .{});
        var ai: usize = 0;
        while (ai < aliases_n) : (ai += 1) std.debug.print(" {s}", .{aliases_buf[ai]});
    }
    std.debug.print("\n", .{});
}

/// One schema entry: `name  N observations\n  shape  count\n...`
fn printSchemaEntry(d: *mpack.Decoder) anyerror!void {
    const m = try d.readMapHeader();
    var name: []const u8 = "?";
    var observations: i64 = 0;
    var shape_buf: [32]struct { key: []const u8, count: i64 } = undefined;
    var shape_n: usize = 0;
    var k: u32 = 0;
    while (k < m) : (k += 1) {
        const key = try d.readStr();
        if (std.mem.eql(u8, key, "name")) {
            name = try d.readStr();
        } else if (std.mem.eql(u8, key, "observations")) {
            observations = try d.readInt();
        } else if (std.mem.eql(u8, key, "shapes")) {
            const sn = try d.readMapHeader();
            var si: u32 = 0;
            while (si < sn) : (si += 1) {
                const sk = try d.readStr();
                const sv = try d.readInt();
                if (shape_n < shape_buf.len) {
                    shape_buf[shape_n] = .{ .key = sk, .count = sv };
                    shape_n += 1;
                }
            }
        } else {
            try d.skip();
        }
    }
    std.debug.print("{s}  {d} observation(s)\n", .{ name, observations });
    var si: usize = 0;
    while (si < shape_n) : (si += 1) {
        std.debug.print("  {s}  ×{d}\n", .{ shape_buf[si].key, shape_buf[si].count });
    }
}

/// One dispatch-trace row: `[startedAt+us] peer.method(args) → outcome: detail`.
fn printTraceEntry(d: *mpack.Decoder) anyerror!void {
    const m = try d.readMapHeader();
    var peer: []const u8 = "?";
    var method: []const u8 = "?";
    var args_s: []const u8 = "";
    var outcome: []const u8 = "?";
    var detail: []const u8 = "";
    var has_detail = false;
    var started: i64 = 0;
    var dur_us: i64 = 0;
    var k: u32 = 0;
    while (k < m) : (k += 1) {
        const key = try d.readStr();
        if (std.mem.eql(u8, key, "peer")) peer = try d.readStr()
        else if (std.mem.eql(u8, key, "method")) method = try d.readStr()
        else if (std.mem.eql(u8, key, "args")) args_s = try d.readStr()
        else if (std.mem.eql(u8, key, "outcome")) outcome = try d.readStr()
        else if (std.mem.eql(u8, key, "detail")) {
            const pk = try d.peek();
            if (pk == .nil) {
                try d.readNil();
            } else {
                detail = try d.readStr();
                has_detail = true;
            }
        } else if (std.mem.eql(u8, key, "startedAt")) started = try d.readInt()
        else if (std.mem.eql(u8, key, "durationUs")) dur_us = try d.readInt()
        else try d.skip();
    }
    std.debug.print("[{d}+{d}us] {s}.{s}({s}) → {s}", .{ started, dur_us, peer, method, args_s, outcome });
    if (has_detail) std.debug.print(": {s}", .{detail});
    std.debug.print("\n", .{});
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
        \\  self                           machine environment info
        \\  list                           list peripherals (CC's peripheral.getNames)
        \\  find <type>                    list peripherals by type
        \\  type <peripheral>              print a peripheral's type(s) and class
        \\  methods <peripheral>           list a peripheral's methods
        \\  methods-like <substring>       fuzzy-search method names across peripherals
        \\  describe <peer> [method]       reflection-derived signatures, grouped by class
        \\  call <peripheral> <method> ... call a peripheral method
        \\  events [count]                 subscribe and print events
        \\  schema [event|clear]           observed event-argument shapes
        \\  trace [on|off|status|dump|clear]   dispatch-trace control
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
