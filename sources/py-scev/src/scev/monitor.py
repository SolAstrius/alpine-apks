# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Typed wrapper around CC: Tweaked's `monitor` peripheral.

Monitors are terminal redirects — same write/scroll/setCursorPos/
clear/setTextColor/blit surface as CC's `term` API, plus the few
monitor-specific bits (setTextScale/getTextScale/isColor). The
introspected proxy already exposes everything; this wrapper is a
thin ergonomic layer on top:

  * Real Python types on color args (accepts `int` or our `colors.*`
    constants — same value, just imported by name)
  * Pythonic batch helpers: `write_at(x, y, text)`, `print(*lines)`,
    `clear_lines(top, bottom)`
  * `with monitor.preserve(): …` context manager that snapshots
    cursor pos + text/bg colours on entry and restores on exit, so
    drawing one widget doesn't bleed into the next
  * `Size` namedtuple-style return for `get_size()` so destructuring
    is idiomatic: `w, h = mon.size`

The `_call`-passthrough escape hatch (`monitor.peripheral`) is always
available if you need a CC method we haven't surfaced yet.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Iterator

if TYPE_CHECKING:
    from .machine import Machine
    from .peripheral import Peripheral


@dataclass(frozen=True, slots=True)
class Size:
    """Terminal dimensions in character cells. Iterable so
    `w, h = mon.size` works, and a real dataclass so callers can pass
    it around without losing labels."""

    width: int
    height: int

    def __iter__(self) -> Iterator[int]:
        yield self.width
        yield self.height


@dataclass(frozen=True, slots=True)
class Cursor:
    x: int
    y: int

    def __iter__(self) -> Iterator[int]:
        yield self.x
        yield self.y


class Monitor:
    """Pythonic façade over a `monitor` peripheral.

    Constructed via `Monitor.wrap(machine, name)` — the wrap step
    verifies the underlying peripheral really is a monitor and gives
    you a typed object regardless of which monitor variant it is.
    """

    def __init__(self, peripheral: "Peripheral") -> None:
        self._p = peripheral

    @classmethod
    def wrap(cls, machine: "Machine", name: str) -> "Monitor":
        p = machine[name]
        if "monitor" not in p.types:
            raise TypeError(
                f"peripheral {name!r} is not a monitor "
                f"(types: {'+'.join(p.types) or '?'})"
            )
        return cls(p)

    @property
    def peripheral(self) -> "Peripheral":
        return self._p

    @property
    def name(self) -> str:
        return self._p.name

    # --------------------------------------------------- text / cursor

    def write(self, text: str) -> None:
        """Write at the current cursor position; cursor advances. No
        wrapping — same semantics as CC's `monitor.write`. For
        multi-line output use [`print`][]."""
        self._p._call("write", str(text))  # noqa: SLF001

    def print(self, *lines: Any) -> None:
        """Pythonic newline-separated write. Each arg is `str(...)`'d
        and rendered on its own line, with the cursor advancing
        between lines (and wrapping x → 1)."""
        cur = self.cursor
        x = cur.x
        for i, line in enumerate(lines):
            if i > 0:
                self._p._call("setCursorPos", 1, cur.y + i)  # noqa: SLF001
            else:
                self._p._call("setCursorPos", x, cur.y)  # noqa: SLF001
            self._p._call("write", str(line))  # noqa: SLF001

    def write_at(self, x: int, y: int, text: str) -> None:
        """Position cursor and write in one call — the pattern that
        comes up most when drawing widgets."""
        self._p._call("setCursorPos", int(x), int(y))  # noqa: SLF001
        self._p._call("write", str(text))  # noqa: SLF001

    def scroll(self, lines: int) -> None:
        self._p._call("scroll", int(lines))  # noqa: SLF001

    @property
    def cursor(self) -> Cursor:
        x, y = self._p._call("getCursorPos")  # noqa: SLF001
        return Cursor(int(x), int(y))

    @cursor.setter
    def cursor(self, pos: tuple[int, int] | Cursor) -> None:
        if isinstance(pos, Cursor):
            x, y = pos.x, pos.y
        else:
            x, y = pos
        self._p._call("setCursorPos", int(x), int(y))  # noqa: SLF001

    @property
    def cursor_blink(self) -> bool:
        return bool(self._p._call("getCursorBlink"))  # noqa: SLF001

    @cursor_blink.setter
    def cursor_blink(self, on: bool) -> None:
        self._p._call("setCursorBlink", bool(on))  # noqa: SLF001

    @property
    def size(self) -> Size:
        w, h = self._p._call("getSize")  # noqa: SLF001
        return Size(int(w), int(h))

    # --------------------------------------------------- text scale

    @property
    def text_scale(self) -> float:
        return float(self._p._call("getTextScale"))  # noqa: SLF001

    @text_scale.setter
    def text_scale(self, scale: float) -> None:
        # CC accepts multiples of 0.5 in [0.5, 5]. Don't validate
        # here — the host raises a clear error and we'd just be
        # duplicating its rules.
        self._p._call("setTextScale", float(scale))  # noqa: SLF001

    # --------------------------------------------------- clear

    def clear(self) -> None:
        """Fill the entire screen with the current background colour."""
        self._p._call("clear")  # noqa: SLF001

    def clear_line(self) -> None:
        """Clear the current cursor row only."""
        self._p._call("clearLine")  # noqa: SLF001

    def clear_lines(self, top: int, bottom: int | None = None) -> None:
        """Clear rows `top..bottom` (inclusive). Useful for status-
        bar-style updates without redrawing the whole screen. If
        `bottom` is None, clears just `top`."""
        if bottom is None:
            bottom = top
        cur = self.cursor
        try:
            for y in range(int(top), int(bottom) + 1):
                self._p._call("setCursorPos", 1, y)  # noqa: SLF001
                self._p._call("clearLine")  # noqa: SLF001
        finally:
            self._p._call("setCursorPos", cur.x, cur.y)  # noqa: SLF001

    # --------------------------------------------------- colour

    @property
    def is_color(self) -> bool:
        """True iff this is an Advanced Monitor (colour-capable).
        Basic monitors render colour writes as grayscale."""
        return bool(self._p._call("isColor"))  # noqa: SLF001

    is_colour = is_color

    @property
    def text_color(self) -> int:
        return int(self._p._call("getTextColor"))  # noqa: SLF001

    @text_color.setter
    def text_color(self, value: int) -> None:
        self._p._call("setTextColor", int(value))  # noqa: SLF001

    @property
    def background_color(self) -> int:
        return int(self._p._call("getBackgroundColor"))  # noqa: SLF001

    @background_color.setter
    def background_color(self, value: int) -> None:
        self._p._call("setBackgroundColor", int(value))  # noqa: SLF001

    # British-spelling aliases.
    text_colour = text_color
    background_colour = background_color

    # --------------------------------------------------- blit

    def blit(self, text: str, fg: str, bg: str) -> None:
        """Coloured-character write. `fg` and `bg` are blit-hex
        strings the same length as `text` (one char per character).
        See [`scev.colors.toBlit`][] / [`scev.colors.to_blit_string`][]
        to build them programmatically."""
        if len(fg) != len(text) or len(bg) != len(text):
            raise ValueError(
                f"blit length mismatch: text={len(text)} fg={len(fg)} bg={len(bg)}"
            )
        self._p._call("blit", str(text), str(fg), str(bg))  # noqa: SLF001

    def blit_at(self, x: int, y: int, text: str, fg: str, bg: str) -> None:
        self._p._call("setCursorPos", int(x), int(y))  # noqa: SLF001
        self.blit(text, fg, bg)

    # --------------------------------------------------- palette

    def get_palette(self, colour: int) -> tuple[float, float, float]:
        r, g, b = self._p._call("getPaletteColor", int(colour))  # noqa: SLF001
        return float(r), float(g), float(b)

    def set_palette(
        self,
        colour: int,
        rgb_or_r: int | float,
        g: float | None = None,
        b: float | None = None,
    ) -> None:
        """Set the palette entry for `colour`. Two call shapes mirror
        CC: a single 24-bit packed int, or three floats in [0, 1]."""
        if g is None and b is None:
            self._p._call("setPaletteColor", int(colour), int(rgb_or_r))  # noqa: SLF001
        else:
            self._p._call(  # noqa: SLF001
                "setPaletteColor",
                int(colour),
                float(rgb_or_r),
                float(g if g is not None else 0.0),
                float(b if b is not None else 0.0),
            )

    # --------------------------------------------------- state preserve

    @contextmanager
    def preserve(self) -> Iterator["Monitor"]:
        """Context manager that snapshots cursor + text/bg colours
        on entry and restores them on exit. Lets one drawing function
        (a status bar update, a one-shot popup) leave the monitor's
        state untouched for the surrounding caller."""
        cur = self.cursor
        fg = self.text_color
        bg = self.background_color
        blink = self.cursor_blink
        try:
            yield self
        finally:
            self.text_color = fg
            self.background_color = bg
            self._p._call("setCursorPos", cur.x, cur.y)  # noqa: SLF001
            self.cursor_blink = blink

    # --------------------------------------------------- repr

    def __repr__(self) -> str:
        try:
            s = self.size
            return f"<Monitor {self._p.name} {s.width}×{s.height}>"
        except Exception:
            return f"<Monitor {self._p.name}>"


def find_monitors(machine: "Machine") -> list[Monitor]:
    """Convenience: wrap every attached monitor in a Monitor object."""
    return [Monitor(p) for p in machine.find("monitor")]


__all__ = ["Cursor", "Monitor", "Size", "find_monitors"]
