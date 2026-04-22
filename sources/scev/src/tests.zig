// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! Aggregator for unit tests. `zig build test` compiles this file and
//! runs every `test` block it pulls in via `@import`. Module-level
//! tests live next to the code they exercise.

const std = @import("std");

comptime {
    _ = @import("cobs.zig");
    _ = @import("mpack.zig");
}

test "root module reachable" {
    // Sanity: `zig build test` found and ran something.
    try std.testing.expect(true);
}
