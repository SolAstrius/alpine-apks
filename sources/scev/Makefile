# scev — guest-side RPC CLI, Zig port.
#
# Zig's own `zig build` is the canonical driver; this Makefile is a
# thin shim. The one default worth pinning by hand: the riscv64-linux-
# musl guest target, so `make` without args gives you the binary the
# scev-alpine image wants.
#
# Usage:
#   make        → build/scev (riscv64 static, stripped)
#   make host   → build/scev-host for the host (native)
#   make test   → host-native unit tests
#   make clean

ZIG      ?= zig
BUILDDIR ?= build
TARGET   ?= riscv64-linux-musl
# Zig 0.15 moved the optimize-mode CLI from -Doptimize=Release<X> to
# --release=<mode>. `fast` maps to ReleaseFast in standardOptimizeOption.
RELEASE  ?= fast

.PHONY: all host test clean

all: $(BUILDDIR)/scev

$(BUILDDIR)/scev:
	mkdir -p $(BUILDDIR)
	$(ZIG) build -Dtarget=$(TARGET) --release=$(RELEASE) --prefix $(BUILDDIR)/zig-prefix
	cp $(BUILDDIR)/zig-prefix/bin/scev $(BUILDDIR)/scev

host:
	mkdir -p $(BUILDDIR)
	$(ZIG) build -Dtarget=native --release=$(RELEASE) --prefix $(BUILDDIR)/zig-prefix-host
	cp $(BUILDDIR)/zig-prefix-host/bin/scev $(BUILDDIR)/scev-host

test:
	$(ZIG) build test -Dtarget=native

clean:
	rm -rf $(BUILDDIR) zig-out .zig-cache
