// SPDX-License-Identifier: MPL-2.0
// Copyright (c) 2026 Scalar Evolution contributors.

//! Build script for the Zig port of the scev guest CLI.
//!
//! Default target is the riscv64-linux-musl variant that the Alpine
//! image runs on — a static musl binary drops straight into
//! /usr/local/bin with no dynamic-link surprises. Override with
//! `zig build -Dtarget=<triple>` for local host testing.

const std = @import("std");

pub fn build(b: *std.Build) void {
    // Standard -Dtarget / -Doptimize flags. Defaults: host-native,
    // ReleaseSafe. For the actual guest build the invocation is
    //   zig build -Dtarget=riscv64-linux-musl -Doptimize=ReleaseFast
    // which both the Makefile shim and any CI invoker pass explicitly.
    // We don't override the default to riscv64 because doing so
    // requires registering the flag manually and breaks
    // -Dtarget=native + the standard help surface.
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{
        .preferred_optimize_mode = .ReleaseSafe,
    });

    const exe = b.addExecutable(.{
        .name = "scev",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
            // Static link — musl libc only. The image build copies
            // this single file into /usr/local/bin and never worries
            // about dynamic-loader compat again.
            .link_libc = true,
            .strip = (optimize != .Debug),
        }),
    });
    // Static linking for musl targets: no ld.so on the guest.
    if (target.result.abi == .musl) exe.linkage = .static;

    b.installArtifact(exe);

    // `zig build run -- ping` and friends for host-side smoke tests.
    const run = b.addRunArtifact(exe);
    run.step.dependOn(b.getInstallStep());
    if (b.args) |args| run.addArgs(args);
    b.step("run", "run the scev CLI").dependOn(&run.step);

    // `zig build test` — unit tests for cobs / mpack / cli-arg parsing.
    const tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/tests.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
        }),
    });
    b.step("test", "run unit tests").dependOn(&b.addRunArtifact(tests).step);
}
