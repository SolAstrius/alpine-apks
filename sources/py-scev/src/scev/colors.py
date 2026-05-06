# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""CC's `colors` module ported to Python.

CC stores 16 colours as single-bit positions in a 16-bit field
(`white=1, orange=2, magenta=4, …, black=32768`). This lets you AND/OR
multiple colours together for things like bundled cables. We provide
both the constants (snake-cased *and* CC-cased aliases so existing CC
docs are copy-pasteable) and the bit-manipulation helpers
(`combine`/`subtract`/`test`), plus the RGB pack/unpack and blit-hex
conversions added in CC 1.81/1.94.

CC also has a British-spelling `colours` module that's a literal alias
of `colors`, except `colours.grey == colors.gray` and
`colours.lightGrey == colors.lightGray`. We re-export
[scev.colours][] as the same module so `from scev.colours import grey`
works.

Default palette values aren't included here — they're only relevant
for `term.setPaletteColour` calls and the host knows the defaults; if
you need them, fetch via `monitor.getPaletteColor(c)`."""

from __future__ import annotations

import math
from typing import Iterable

# ----------------------------------------------------------------- constants

# American-spelled (matches `colors`).
WHITE: int = 0x1
ORANGE: int = 0x2
MAGENTA: int = 0x4
LIGHT_BLUE: int = 0x8
YELLOW: int = 0x10
LIME: int = 0x20
PINK: int = 0x40
GRAY: int = 0x80
LIGHT_GRAY: int = 0x100
CYAN: int = 0x200
PURPLE: int = 0x400
BLUE: int = 0x800
BROWN: int = 0x1000
GREEN: int = 0x2000
RED: int = 0x4000
BLACK: int = 0x8000

# CC-cased aliases — the literal attribute names CC's docs use, so
# `colors.lightBlue` / `colors.lightGray` work for users porting Lua
# code straight across.
white = WHITE
orange = ORANGE
magenta = MAGENTA
lightBlue = LIGHT_BLUE
yellow = YELLOW
lime = LIME
pink = PINK
gray = GRAY
lightGray = LIGHT_GRAY
cyan = CYAN
purple = PURPLE
blue = BLUE
brown = BROWN
green = GREEN
red = RED
black = BLACK

# British-spelling aliases for the two that differ. Both spellings
# coexist in the namespace so users can pick either.
grey = gray
lightGrey = lightGray

# Convenient ordered list — useful for "iterate every colour" loops
# (palette dumps, debug rendering, etc.).
ALL: list[int] = [
    WHITE, ORANGE, MAGENTA, LIGHT_BLUE,
    YELLOW, LIME, PINK, GRAY,
    LIGHT_GRAY, CYAN, PURPLE, BLUE,
    BROWN, GREEN, RED, BLACK,
]

# Reverse map: bit value → blit hex character. CC's palette assigns
# 0=white, 1=orange, 2=magenta, …, f=black, which is `log2(value)`
# rendered in lowercase hex. Cached so toBlit/fromBlit are O(1).
_VALUE_TO_BLIT: dict[int, str] = {v: format(i, "x") for i, v in enumerate(ALL)}
_BLIT_TO_VALUE: dict[str, int] = {ch: v for v, ch in _VALUE_TO_BLIT.items()}


# ----------------------------------------------------------------- helpers


def combine(*colors: int) -> int:
    """Bitwise-OR every argument. Equivalent to CC's `colors.combine`,
    accepting both single colours and pre-combined sets:

        combine(WHITE, MAGENTA, LIGHT_BLUE)  # => 13
    """
    result = 0
    for c in colors:
        result |= int(c)
    return result


def subtract(initial: int, *colors: int) -> int:
    """Remove `colors` from `initial`. Each subtrahend may be a
    single colour or a combined set; bits not present in `initial`
    are left alone:

        subtract(LIME, ORANGE, WHITE)  # => 32 (LIME) — neither was set
    """
    mask = 0
    for c in colors:
        mask |= int(c)
    return initial & ~mask


def test(colors_set: int, color: int) -> bool:
    """True iff every bit in `color` is also set in `colors_set`.
    Useful for asking "is the lightBlue bit set in this bundled-cable
    output?" against an OR-combined value."""
    return (int(colors_set) & int(color)) == int(color)


# ----------------------------------------------------------------- RGB


def packRGB(r: float, g: float, b: float) -> int:
    """Pack three floats in [0,1] into a 24-bit integer. Mirrors CC's
    `colors.packRGB`; clamps inputs to range so callers don't have to
    pre-clamp their own data."""
    return (
        (_clamp_byte(r) << 16)
        | (_clamp_byte(g) << 8)
        | _clamp_byte(b)
    )


def unpackRGB(rgb: int) -> tuple[float, float, float]:
    """Inverse of [packRGB][]. Returns (r, g, b) each in [0,1]."""
    rgb = int(rgb)
    return (
        ((rgb >> 16) & 0xFF) / 255.0,
        ((rgb >> 8) & 0xFF) / 255.0,
        (rgb & 0xFF) / 255.0,
    )


def rgb8(*args: float) -> int | tuple[float, float, float]:
    """Deprecated polymorphic helper from older CC. Calls `packRGB` if
    given three args, `unpackRGB` if given one. Kept for translation
    fidelity from existing Lua code; new code should call the typed
    pair directly."""
    if len(args) == 1:
        return unpackRGB(int(args[0]))
    if len(args) == 3:
        return packRGB(args[0], args[1], args[2])
    raise TypeError(f"rgb8 takes 1 or 3 arguments ({len(args)} given)")


def _clamp_byte(v: float) -> int:
    """Clamp `v` to [0,1] then quantise to 0..255."""
    if v <= 0.0:
        return 0
    if v >= 1.0:
        return 255
    # CC rounds via floor(v * 255 + 0.5); Python's round() does
    # banker's rounding, so use the explicit formula to match.
    return int(math.floor(v * 255.0 + 0.5))


# ----------------------------------------------------------------- blit


def toBlit(color: int) -> str:
    """Convert a colour bit-value to its single-char blit hex code
    (`'0'..'f'`). Raises ValueError for non-power-of-two or
    out-of-range values — the host's `term.blit` would error on those
    too, so we surface it client-side."""
    ch = _VALUE_TO_BLIT.get(int(color))
    if ch is None:
        raise ValueError(f"not a valid colour value: {color!r}")
    return ch


def fromBlit(hex_char: str) -> int:
    """Inverse of [toBlit][] — accepts a single character `'0'..'f'`
    (case-insensitive) and returns the colour bit-value. Raises
    ValueError on anything else."""
    if not isinstance(hex_char, str) or len(hex_char) != 1:
        raise ValueError(f"expected a single hex char, got {hex_char!r}")
    v = _BLIT_TO_VALUE.get(hex_char.lower())
    if v is None:
        raise ValueError(f"not a valid blit hex char: {hex_char!r}")
    return v


def to_blit_string(colours: Iterable[int]) -> str:
    """Bonus: convert an iterable of colour bit-values into the
    multi-char blit string CC's `term.blit` consumes. Useful for
    constructing colour runs programmatically:

        term.blit(text, to_blit_string([RED]*len(text)), to_blit_string([BLACK]*len(text)))
    """
    return "".join(toBlit(c) for c in colours)


__all__ = [
    # constants
    "ALL",
    "BLACK",
    "BLUE",
    "BROWN",
    "CYAN",
    "GRAY",
    "GREEN",
    "LIGHT_BLUE",
    "LIGHT_GRAY",
    "LIME",
    "MAGENTA",
    "ORANGE",
    "PINK",
    "PURPLE",
    "RED",
    "WHITE",
    "YELLOW",
    # CC-cased aliases
    "black",
    "blue",
    "brown",
    "cyan",
    "gray",
    "green",
    "grey",
    "lightBlue",
    "lightGray",
    "lightGrey",
    "lime",
    "magenta",
    "orange",
    "pink",
    "purple",
    "red",
    "white",
    "yellow",
    # helpers
    "combine",
    "fromBlit",
    "packRGB",
    "rgb8",
    "subtract",
    "test",
    "toBlit",
    "to_blit_string",
    "unpackRGB",
]
