# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Bridge from real graphics frameworks (Pillow, matplotlib, anything that
can spit out an RGB ndarray) to scev's monitor-buffer pipeline.

The hand-rolled approach — pick palette colors, draw rectangles via
`blit`, render a 5×7 bitmap font for big text — gets you off the
ground but it's tedious for anything beyond a single dashboard. With
Pillow you write:

    import scev
    from scev.buffer import Buffer
    from scev.gui import paint, new_canvas
    from PIL import ImageDraw, ImageFont

    with scev.connect() as m:
        mon = m["monitor_5"]
        buf = Buffer(*mon.getSize(), color=mon.isColor())
        img = new_canvas(buf)                       # full-screen PIL Image
        d = ImageDraw.Draw(img)
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size=18)
        d.rectangle((0, 0, img.width, 30), fill=(20, 60, 120))
        d.text((6, 4), "Hello, monitor", font=font, fill="white")
        paint(buf, img)
        buf.flush(m, "monitor_5")

…and you get a real anti-aliased font, real shape drawing, real image
compositing — all running through the same sextant + batch-blit
pipeline as before, no new RPC machinery. Same trick works for
matplotlib (`fig.canvas.draw(); paint_array(buf, fig.canvas.buffer_rgba())`)
or any other library that hands back pixels.

What you give up vs. blitting palette colours directly: every cell
goes through the sextant's 2-color quantiser, so very fine
fg-on-bg-on-fg patterns (e.g. anti-aliased text on a busy background)
will see some banding inside each 2×3 cell. For most UIs that's not
visible; for sharp 1-bit pixel art at sub-cell resolution the
hand-rolled path can still be marginally crisper."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from .sextant import draw_image_np

if TYPE_CHECKING:
    from PIL import Image as PILImage  # noqa: N811

    from .buffer import Buffer


def _to_rgb_array(image: Any) -> np.ndarray:
    """Coerce a PIL Image or numpy-like into an `(H, W, 3)` uint8 array."""
    arr = np.asarray(image)
    if arr.ndim == 3 and arr.shape[2] >= 3:
        return arr[:, :, :3].astype(np.uint8, copy=False)
    if hasattr(image, "convert"):
        return np.asarray(image.convert("RGB"))
    raise TypeError(f"can't convert {type(image).__name__} to RGB array")


def paint(buf: "Buffer", image: Any, x: int = 1, y: int = 1) -> None:
    """Paint a PIL Image (or any HxWx3 RGB array) onto `buf` at cell `(x, y)`.

    Image dimensions must satisfy `width % 2 == 0` and `height % 3 == 0` —
    every 2×3 sub-pixel block becomes one character cell. Use
    `new_canvas(buf)` to get a correctly-sized blank canvas, or
    `pad_to_sextant(arr)` if you have an arbitrary array."""
    draw_image_np(buf, x, y, _to_rgb_array(image))


def new_canvas(buf: "Buffer", fill: tuple[int, int, int] = (0, 0, 0)) -> "PILImage.Image":
    """Return a fresh full-screen PIL Image sized to the buffer's pixel grid.

    Lazy-imports Pillow so scev's core remains optional-dep-free."""
    from PIL import Image  # type: ignore[import-not-found]

    return Image.new("RGB", (buf.sizeX * 2, buf.sizeY * 3), fill)


def pad_to_sextant(arr: np.ndarray, fill: int = 0) -> np.ndarray:
    """Pad an RGB array along bottom/right so dims are sextant-legal.

    Width is rounded up to a multiple of 2, height to a multiple of 3.
    No-op when both dims already align."""
    h, w = arr.shape[:2]
    py = (3 - h % 3) % 3
    px = (2 - w % 2) % 2
    if not (py or px):
        return arr
    return np.pad(arr, ((0, py), (0, px), (0, 0)), mode="constant", constant_values=fill)


__all__ = ["paint", "new_canvas", "pad_to_sextant"]
