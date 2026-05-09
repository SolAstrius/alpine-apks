# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""2×3 sub-pixel ("sextant") encoding for monitor pixel rendering.

CC: Tweaked's font carries 32 special glyphs at codepoints `\\x80`–`\\x9F`
("teletext mosaic chars") that each draw a 2-wide × 3-tall grid of
sub-pixels inside one character cell. Combined with `blit`'s per-cell
foreground/background colors, a single character cell paints 6
sub-pixels with 2 colors. A 29×19 monitor at default scale becomes
effectively a 58×57 *pixel* grid with per-cell 2-color choice.

Encoding details
----------------

The 32 glyphs encode the 5 *movable* sub-pixels (top-left, top-right,
mid-left, mid-right, bottom-left); the bottom-right sub-pixel is
always rendered in the cell's background colour. So a 6-bit pattern
(bit 0 = TL, 1 = TR, 2 = ML, 3 = MR, 4 = BL, 5 = BR) splits into two
cases:

* bit 5 = 0 (BR off):  char = 0x80 | (mask & 0x1F),
                       fg = on-color, bg = off-color.
* bit 5 = 1 (BR on):   char = 0x80 | (~mask & 0x1F),
                       fg = off-color, bg = on-color.

Two-color quantisation
----------------------

Real input pixels rarely come as just two colors per tile. We snap
each of the 6 pixels to its nearest palette slot, then enumerate the
*distinct* slots that came up (at most 6) and try each ordered pair
as the (on-color, off-color) candidate. For each candidate pair the
optimal mask is greedy — each sub-pixel goes to whichever palette
slot it's closer to. The pair with the lowest summed squared-RGB
error wins. ~15 pairs × 12 distance computations per tile, ~10 ms
for a 29×19 monitor in pure Python.

This is a heuristic — the globally optimal 2-color choice could in
principle be a palette slot that none of the 6 pixels' nearest
matches landed on. In practice that almost never happens for natural
images: the optimum is dominated by the colors actually present.

Public surface:

* [`CC_PALETTE`][]: default 16-entry RGB palette CC ships with.
* [`encode_tile`][]: one tile -> (char, fg_hex, bg_hex).
* [`draw_image`][]: paint an `H × W` RGB array into a `Buffer`,
  cell-aligned at `(x, y)`.

Usage:

    import scev
    from scev.buffer import Buffer
    from scev.sextant import draw_image

    image = [[(r, g, b) for x in range(W)] for y in range(H)]   # H must be 3·rows, W must be 2·cols
    with scev.connect() as m:
        mon = m["monitor_1"]
        cw, ch = mon.getSize()
        buf = Buffer(cw, ch, color=mon.isColor())
        buf.setBackgroundColor(scev.colors.BLACK); buf.clear()
        draw_image(buf, 1, 1, image)
        buf.flush(m, "monitor_1")
"""

from __future__ import annotations

from typing import Sequence

from . import colors as _colors
from .buffer import Buffer

# CC's default monitor palette as 24-bit RGB ints, indexed by palette
# slot 0..15 (matching the blit hex-char `0`..`f`). Values pulled from
# CC: Tweaked source (Palette.DEFAULT). They get reassigned at runtime
# if the user calls `setPaletteColor`, but this is the boot-time set.
CC_PALETTE: tuple[tuple[int, int, int], ...] = (
    (0xF0, 0xF0, 0xF0),  # 0  white
    (0xF2, 0xB2, 0x33),  # 1  orange
    (0xE5, 0x7F, 0xD8),  # 2  magenta
    (0x99, 0xB2, 0xF2),  # 3  lightBlue
    (0xDE, 0xDE, 0x6C),  # 4  yellow
    (0x7F, 0xCC, 0x19),  # 5  lime
    (0xF2, 0xB2, 0xCC),  # 6  pink
    (0x4C, 0x4C, 0x4C),  # 7  gray
    (0x99, 0x99, 0x99),  # 8  lightGray
    (0x4C, 0x99, 0xB2),  # 9  cyan
    (0xB2, 0x66, 0xE5),  # a  purple
    (0x33, 0x66, 0xCC),  # b  blue
    (0x7F, 0x66, 0x4C),  # c  brown
    (0x57, 0xA6, 0x4E),  # d  green
    (0xCC, 0x4C, 0x4C),  # e  red
    (0x11, 0x11, 0x11),  # f  black
)

_HEX = "0123456789abcdef"


def _nearest_palette_index(
    rgb: tuple[float, float, float],
    palette: Sequence[tuple[int, int, int]],
) -> int:
    """Return the palette slot with the smallest squared-RGB distance
    to `rgb`. Linear scan across 16 entries — not worth a kd-tree."""
    r, g, b = rgb
    best_i, best_d = 0, float("inf")
    for i, (pr, pg, pb) in enumerate(palette):
        dr, dg, db = r - pr, g - pg, b - pb
        d = dr * dr + dg * dg + db * db
        if d < best_d:
            best_d, best_i = d, i
    return best_i


def encode_tile(
    pixels: Sequence[tuple[int, int, int]],
    palette: Sequence[tuple[int, int, int]] = CC_PALETTE,
) -> tuple[str, str, str]:
    """Encode one 2×3 tile into `(char, fg_hex, bg_hex)`.

    `pixels` is the 6 RGB triples in row-major order — top row first,
    left to right: `[TL, TR, ML, MR, BL, BR]`. Returns a 1-char string
    plus two single hex chars suitable for `blit`."""
    if len(pixels) != 6:
        raise ValueError("encode_tile expects exactly 6 pixels")

    # Snap each pixel to nearest palette slot.
    nearest = [_nearest_palette_index(p, palette) for p in pixels]
    distinct = list(set(nearest))

    if len(distinct) == 1:
        # Uniform tile: blit a space, bg = that color. fg is arbitrary.
        idx = distinct[0]
        return " ", "0", _HEX[idx]

    # Cache squared-RGB distance from each pixel to each candidate
    # palette slot, so the inner loop is just lookups.
    dist: dict[int, list[float]] = {}
    for idx in distinct:
        pr, pg, pb = palette[idx]
        dist[idx] = [
            (px - pr) * (px - pr) + (py - pg) * (py - pg) + (pz - pb) * (pz - pb)
            for (px, py, pz) in pixels
        ]

    best_err = float("inf")
    best_mask = 0
    best_on_idx = distinct[0]
    best_off_idx = distinct[-1]

    # Enumerate ordered pairs (on, off). For each pair, the optimal
    # mask is greedy: each pixel joins whichever slot it's closer to.
    for on_idx in distinct:
        d_on = dist[on_idx]
        for off_idx in distinct:
            if off_idx == on_idx:
                continue
            d_off = dist[off_idx]
            mask = 0
            err = 0.0
            for i in range(6):
                if d_on[i] < d_off[i]:
                    mask |= 1 << i
                    err += d_on[i]
                else:
                    err += d_off[i]
            if err < best_err:
                best_err = err
                best_mask = mask
                best_on_idx = on_idx
                best_off_idx = off_idx

    # Encode mask -> (char, fg, bg). BR (bit 5) controls polarity:
    # if BR is off, encode the other 5 directly; if BR is on, invert
    # the cell — swap fg/bg colors and encode ~mask.
    if best_mask & 0x20:
        char_code = 0x80 | ((~best_mask) & 0x1F)
        fg_idx, bg_idx = best_off_idx, best_on_idx
    else:
        char_code = 0x80 | (best_mask & 0x1F)
        fg_idx, bg_idx = best_on_idx, best_off_idx

    return chr(char_code), _HEX[fg_idx], _HEX[bg_idx]


def draw_image(
    buf: Buffer,
    x: int,
    y: int,
    image: Sequence[Sequence[tuple[int, int, int]]],
    palette: Sequence[tuple[int, int, int]] = CC_PALETTE,
) -> None:
    """Render an `H × W` RGB image into `buf` starting at cell `(x, y)`.

    `H` must be a multiple of 3 and `W` a multiple of 2 — the image
    is split into 2×3 sub-pixel tiles, one per character cell. Tiles
    that fall outside the buffer's bounds are silently skipped (write
    semantics inherited from the underlying `Buffer.blit`).

    Each row of the image is one row of sub-pixels; you get
    `H / 3` cells of vertical and `W / 2` cells of horizontal output."""
    h_pix = len(image)
    w_pix = len(image[0]) if h_pix else 0
    if h_pix % 3 or w_pix % 2:
        raise ValueError(
            f"image dims must be multiples of (3, 2); got ({h_pix}, {w_pix})"
        )

    rows = h_pix // 3
    cols = w_pix // 2

    for ty in range(rows):
        line_chars: list[str] = []
        line_fg: list[str] = []
        line_bg: list[str] = []
        for tx in range(cols):
            px = tx * 2
            py = ty * 3
            tile = (
                image[py][px],     image[py][px + 1],
                image[py + 1][px], image[py + 1][px + 1],
                image[py + 2][px], image[py + 2][px + 1],
            )
            ch, fg, bg = encode_tile(tile, palette)
            line_chars.append(ch)
            line_fg.append(fg)
            line_bg.append(bg)
        buf.setCursorPos(x, y + ty)
        buf.blit("".join(line_chars), "".join(line_fg), "".join(line_bg))


def draw_image_np(
    buf: Buffer,
    x: int,
    y: int,
    image,  # numpy.ndarray (H, W, 3) uint8 — annotated `Any` to keep numpy optional
    palette: Sequence[tuple[int, int, int]] = CC_PALETTE,
) -> None:
    """Vectorised numpy variant of [`draw_image`][]. ~50× faster on a
    typical monitor — the per-pixel palette nearest-neighbour and the
    per-tile mode-2 selection both fold into a handful of numpy ops.

    Requires `numpy`. Quantisation strategy differs slightly from the
    pure-Python path: this uses the *two most frequent palette slots*
    in each tile (snapped pixels' mode-1 and mode-2) rather than
    enumerating every pair. For natural / smooth images that's
    quality-equivalent — the optimal pair almost always is the
    top-2 most-common palette slots."""
    import numpy as np  # type: ignore[import-not-found]

    img = np.asarray(image)
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(f"image must be (H, W, 3); got shape {img.shape}")
    ph, pw, _ = img.shape
    if ph % 3 or pw % 2:
        raise ValueError(
            f"image dims must be multiples of (3, 2); got ({ph}, {pw})"
        )
    img = img.astype(np.int32, copy=False)

    pal = np.asarray(palette, dtype=np.int32)  # (16, 3)
    n_pal = pal.shape[0]

    # Snap every pixel to nearest palette index. Broadcast over palette
    # axis: (ph, pw, 1, 3) - (1, 1, n_pal, 3) → (ph, pw, n_pal, 3).
    diff = img[:, :, None, :] - pal[None, None, :, :]
    sqdist = (diff * diff).sum(-1)            # (ph, pw, n_pal)
    pal_idx = sqdist.argmin(-1)               # (ph, pw)

    rows, cols = ph // 3, pw // 2
    # Reshape (ph, pw) → (rows, 3, cols, 2) → transpose to (rows, cols, 3, 2) → flat (rows, cols, 6)
    pal_t = (
        pal_idx.reshape(rows, 3, cols, 2)
               .transpose(0, 2, 1, 3)
               .reshape(rows, cols, 6)
    )
    img_t = (
        img.reshape(rows, 3, cols, 2, 3)
           .transpose(0, 2, 1, 3, 4)
           .reshape(rows, cols, 6, 3)
    )

    # One-hot encode and sum to get per-tile palette histogram in one shot.
    # `np.put_along_axis` writes 1 at `pal_t[..., None]` along axis=-1 of
    # the zero buffer; summing the `6` axis gives us counts per palette slot.
    onehot = np.zeros((rows, cols, 6, n_pal), dtype=np.int32)
    np.put_along_axis(onehot, pal_t[..., None], 1, axis=-1)
    counts = onehot.sum(axis=-2)              # (rows, cols, n_pal)

    # Top-2 most-frequent palette slots per tile.
    top2 = np.argsort(counts, axis=-1)[..., -2:]  # (rows, cols, 2)
    primary = top2[..., 1]    # mode-1
    secondary = top2[..., 0]  # mode-2

    # Greedy mask: each sub-pixel takes whichever of {primary, secondary}
    # is closer in the *original* RGB space (not in palette-index space).
    primary_rgb = pal[primary]                # (rows, cols, 3)
    secondary_rgb = pal[secondary]
    d_primary = ((img_t - primary_rgb[:, :, None, :]) ** 2).sum(-1)
    d_secondary = ((img_t - secondary_rgb[:, :, None, :]) ** 2).sum(-1)
    on_primary = d_primary <= d_secondary     # (rows, cols, 6) bool

    bits = (1 << np.arange(6, dtype=np.int32))
    mask = (on_primary.astype(np.int32) * bits).sum(-1)  # (rows, cols)

    # Apply BR-polarity inversion (bit 5).
    br_on = (mask & 0x20) != 0
    inv_mask = (~mask) & 0x1F
    fwd_mask = mask & 0x1F
    char_codes = np.where(br_on, 0x80 | inv_mask, 0x80 | fwd_mask).astype(np.int32)
    fg_idx = np.where(br_on, secondary, primary).astype(np.int32)
    bg_idx = np.where(br_on, primary, secondary).astype(np.int32)

    # Per-row blit. The string conversion isn't vectorised, but it's
    # cheap relative to the ops above.
    hex_lut = np.array(list(_HEX), dtype="U1")
    fg_chars = hex_lut[fg_idx]                # (rows, cols)
    bg_chars = hex_lut[bg_idx]
    chars = np.array([chr(c) for c in char_codes.ravel()], dtype="U1").reshape(rows, cols)

    for ty in range(rows):
        buf.setCursorPos(x, y + ty)
        buf.blit(
            "".join(chars[ty]),
            "".join(fg_chars[ty]),
            "".join(bg_chars[ty]),
        )


__all__ = ["CC_PALETTE", "encode_tile", "draw_image", "draw_image_np"]
