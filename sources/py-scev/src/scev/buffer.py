# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.
#
# Port of Lyqyd's CC "Buffer" library (MIT, (c) 2013 Lyqyd) to Python,
# adapted for the scev RPC transport. Original at:
#   https://github.com/Lyqyd/Buffer
# Same buffer-of-strings layout (text/textColor/backColor as parallel
# strings indexed 1..sizeY), same write/scroll/setBounds semantics; the
# only structural change is that `flush` builds a `Machine.batch(...)`
# of `setCursorPos`+`blit` pairs instead of issuing them through the CC
# `term` global.

"""Off-screen character-grid buffer for monitor (and term) drawing.

A `Buffer` is a pure in-memory representation of what you'd like a
monitor to look like — text plus per-cell foreground and background
colour. You draw into it the same way you'd draw into a CC term
redirect (`write`, `blit`, `setCursorPos`, `clear`, `scroll`, the
colour setters). Then `flush` ships the result to a real monitor via
scev's batched RPC:

    import scev
    from scev.buffer import Buffer

    with scev.connect() as m:
        mon = m["monitor_0"]
        w, h = mon.getSize()
        buf = Buffer(w, h, color=mon.isColor())
        buf.setBackgroundColor(scev.colors.BLACK)
        buf.clear()
        buf.setCursorPos(2, 2)
        buf.setTextColor(scev.colors.LIME)
        buf.write("hello, monitor")
        prev = buf.flush(m, "monitor_0")          # full draw, returns snapshot
        # ...
        buf.setCursorPos(2, 4)
        buf.write("frame 2")
        prev = buf.flush(m, "monitor_0", prev)    # only changed rows go on the wire

Why a buffer instead of `monitor.write`/`blit` directly: every CC term
call is one RPC round-trip. A 50×19 monitor is 950 cells; redrawing it
cell-by-cell is painful. The buffer collapses a frame into one batched
RPC and (with `prev`) only re-blits the rows that changed.

`flush` uses the *ordered* `batch` — `setCursorPos` and `blit` mutate
shared cursor state on the peripheral, so they cannot be reordered.
For independent reads/writes across peripherals, use `Machine.batch_par`
directly."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from . import colors as _colors

if TYPE_CHECKING:
    from .machine import Machine
    from .peripheral import Peripheral


# Single hex char `0`-`f` per cell, matching CC's `blit` argument shape
# and the wire format. We store rows as `str` rather than `bytearray`
# because every concat in `_do_write` stays in `str` land and msgpack
# wants `str` for blit args anyway.
BlitChar = str


@dataclass(slots=True)
class Buffer:
    """Off-screen terminal buffer. State only — no I/O until `flush`.

    `color=False` mimics CC's monochrome monitors / advanced-vs-basic
    distinction: only black (`0x8000`) and white (`0x1`) are accepted
    by the colour setters; anything else is silently ignored, matching
    the Lua original's `nil, "Colour not supported"` branch (the return
    value is dropped in practice — CC scripts never check it).

    Coordinates are 1-indexed throughout, matching CC. The cursor may
    sit outside the visible area; writes there are clipped or skipped
    in the same way Lyqyd's library handles them so existing code that
    relies on those edge cases keeps working."""

    sizeX: int = 51
    sizeY: int = 19
    color: bool = True
    xOffset: int = 0
    yOffset: int = 0

    cursorX: int = 1
    cursorY: int = 1
    cursorBlink: bool = False
    curTextColor: BlitChar = "0"  # white
    curBackColor: BlitChar = "f"  # black

    text: list[str] = field(default_factory=list)
    textColor: list[str] = field(default_factory=list)
    backColor: list[str] = field(default_factory=list)

    minX: int = 1
    maxX: int = 0
    minY: int = 1
    maxY: int = 0

    def __post_init__(self) -> None:
        if self.maxX == 0:
            self.maxX = self.sizeX
        if self.maxY == 0:
            self.maxY = self.sizeY
        self.clear()

    # ------------------------------------------------------------------
    # core write — port of Lyqyd's `doWrite` with identical clip/skip
    # semantics (off-top/bottom skips entirely; off-left clips the
    # leading chars; off-right is naturally truncated by the line
    # length the host accepts).
    # ------------------------------------------------------------------

    def _do_write(self, text: str, tc: str, bc: str) -> None:
        pos = self.cursorX
        if self.cursorY > self.sizeY or self.cursorY < 1:
            self.cursorX = pos + len(text)
            return

        if pos + len(text) <= 1:
            self.cursorX = pos + len(text)
            return
        elif pos < 1:
            # Drop the off-screen prefix. Lua: `len = abs(cursorX) + 2`,
            # then string.sub(text, len) keeps from index `len` on; in
            # Python that's `text[len-1:]`.
            n = abs(self.cursorX) + 2
            wt, wtc, wbc = text[n - 1 :], tc[n - 1 :], bc[n - 1 :]
            self.cursorX = 1
        elif pos > self.sizeX:
            self.cursorX = pos + len(text)
            return
        else:
            wt, wtc, wbc = text, tc, bc

        # Splice into the row. Lua uses 1-indexed inclusive substring;
        # the equivalent in Python is `s[a-1:b]`. preStop=cursorX-1
        # gives the prefix end; postStart=cursorX+len(wt) the postfix
        # start. When cursorX==1 the prefix is empty.
        line_t = self.text[self.cursorY - 1]
        line_tc = self.textColor[self.cursorY - 1]
        line_bc = self.backColor[self.cursorY - 1]

        cx = self.cursorX
        prefix_end = cx - 1               # inclusive (Lua), exclusive in Py slice
        postfix_start = cx + len(wt) - 1  # 0-indexed start in Py

        self.text[self.cursorY - 1] = line_t[:prefix_end] + wt + line_t[postfix_start:]
        self.textColor[self.cursorY - 1] = (
            line_tc[:prefix_end] + wtc + line_tc[postfix_start:]
        )
        self.backColor[self.cursorY - 1] = (
            line_bc[:prefix_end] + wbc + line_bc[postfix_start:]
        )
        self.cursorX = pos + len(text)

    # ------------------------------------------------------------------
    # term-redirect surface (CC names + snake_case aliases)
    # ------------------------------------------------------------------

    def write(self, text: Any) -> None:
        s = str(text)
        self._do_write(s, self.curTextColor * len(s), self.curBackColor * len(s))

    def blit(self, text: str, textColor: str, backColor: str) -> None:
        if len(textColor) != len(text) or len(backColor) != len(text):
            raise ValueError("blit arguments must be the same length")
        self._do_write(text, textColor, backColor)

    def clear(self) -> None:
        blank = " " * self.sizeX
        ftc = self.curTextColor * self.sizeX
        fbc = self.curBackColor * self.sizeX
        self.text = [blank] * self.sizeY
        self.textColor = [ftc] * self.sizeY
        self.backColor = [fbc] * self.sizeY

    def clearLine(self) -> None:
        i = self.cursorY - 1
        if 0 <= i < self.sizeY:
            self.text[i] = " " * self.sizeX
            self.textColor[i] = self.curTextColor * self.sizeX
            self.backColor[i] = self.curBackColor * self.sizeX

    def getCursorPos(self) -> tuple[int, int]:
        return self.cursorX, self.cursorY

    def setCursorPos(self, x: float | None, y: float | None) -> None:
        if x is not None:
            self.cursorX = int(x)
        if y is not None:
            self.cursorY = int(y)

    def setCursorBlink(self, b: bool) -> None:
        self.cursorBlink = bool(b)

    def getSize(self) -> tuple[int, int]:
        return self.sizeX, self.sizeY

    def scroll(self, n: int = 1) -> None:
        n = int(n)
        if n == 0:
            return
        blank = " " * self.sizeX
        ftc = self.curTextColor * self.sizeX
        fbc = self.curBackColor * self.sizeX
        if n > 0:
            for i in range(self.sizeY - n):
                self.text[i] = self.text[i + n]
                self.textColor[i] = self.textColor[i + n]
                self.backColor[i] = self.backColor[i + n]
            for i in range(self.sizeY - n, self.sizeY):
                self.text[i] = blank
                self.textColor[i] = ftc
                self.backColor[i] = fbc
        else:
            k = -n
            for i in range(self.sizeY - 1, k - 1, -1):
                self.text[i] = self.text[i - k]
                self.textColor[i] = self.textColor[i - k]
                self.backColor[i] = self.backColor[i - k]
            for i in range(k):
                self.text[i] = blank
                self.textColor[i] = ftc
                self.backColor[i] = fbc

    # ----- colours ---------------------------------------------------

    def getTextColor(self) -> int:
        return _colors.fromBlit(self.curTextColor)

    getTextColour = getTextColor

    def setTextColor(self, clr: int) -> None:
        if not (1 <= clr <= 0x8000):
            return
        if not self.color and clr not in (_colors.WHITE, _colors.BLACK):
            return
        self.curTextColor = _colors.toBlit(clr)

    setTextColour = setTextColor

    def getBackgroundColor(self) -> int:
        return _colors.fromBlit(self.curBackColor)

    getBackgroundColour = getBackgroundColor

    def setBackgroundColor(self, clr: int) -> None:
        if not (1 <= clr <= 0x8000):
            return
        if not self.color and clr not in (_colors.WHITE, _colors.BLACK):
            return
        self.curBackColor = _colors.toBlit(clr)

    setBackgroundColour = setBackgroundColor

    def isColor(self) -> bool:
        return self.color is True

    isColour = isColor

    # ----- compositing ----------------------------------------------

    def render(self, other: "Buffer") -> None:
        """Copy `other`'s row contents into this buffer, sizeY rows.
        Mirrors Lyqyd's `redirect.render` — caller is responsible for
        making sure dimensions match."""
        n = min(self.sizeY, other.sizeY)
        for i in range(n):
            self.text[i] = other.text[i]
            self.textColor[i] = other.textColor[i]
            self.backColor[i] = other.backColor[i]

    def setBounds(self, x_min: int, y_min: int, x_max: int, y_max: int) -> None:
        """Restrict which rows/cols `flush` actually pushes to the
        target. Useful when one monitor hosts several independent
        widgets that each own a subregion."""
        self.minX, self.minY = int(x_min), int(y_min)
        self.maxX, self.maxY = int(x_max), int(y_max)

    # ------------------------------------------------------------------
    # flush — the part the Lua original delegated to `term.blit` per
    # row. Here we batch all the (setCursorPos, blit) pairs into a
    # single ordered RPC.
    # ------------------------------------------------------------------

    def flush(
        self,
        machine: "Machine",
        peer: "str | Peripheral",
        current: "Buffer | None" = None,
        *,
        timeout: float = 60.0,
    ) -> "Buffer":
        """Render the buffer to `peer` (peer name or Peripheral proxy)
        on `machine`. If `current` is supplied, only rows that differ
        from it are re-blitted, and the new state is mirrored back into
        `current` so the next call diffs against it. If `current` is
        None, every row in the bounds is sent and a fresh snapshot is
        returned that you can pass back next frame.

        Returns the snapshot used for diffing — pass it as `current`
        on the next call.

        Uses `Machine.batch` (ordered) because `setCursorPos`+`blit`
        share cursor state on the peripheral and must run sequentially
        host-side."""
        peer_name = peer if isinstance(peer, str) else peer.name

        if current is None:
            current = Buffer(self.sizeX, self.sizeY, color=self.color)
            # Force every in-bounds row to be considered dirty by giving
            # `current` a sentinel row that can't equal real content.
            sentinel = "\x00" * self.sizeX
            for i in range(self.sizeY):
                current.text[i] = sentinel

        items: list[tuple[str, list[Any]]] = []
        for y in range(self.minY, self.maxY + 1):
            i = y - 1
            if (
                self.text[i] == current.text[i]
                and self.textColor[i] == current.textColor[i]
                and self.backColor[i] == current.backColor[i]
            ):
                continue
            items.append(("call", [peer_name, "setCursorPos", self.minX + self.xOffset, y + self.yOffset]))
            items.append(
                (
                    "call",
                    [
                        peer_name,
                        "blit",
                        self.text[i][self.minX - 1 : self.maxX],
                        self.textColor[i][self.minX - 1 : self.maxX],
                        self.backColor[i][self.minX - 1 : self.maxX],
                    ],
                )
            )
            current.text[i] = self.text[i]
            current.textColor[i] = self.textColor[i]
            current.backColor[i] = self.backColor[i]

        # Cursor + colour epilogue, matching Lyqyd's `draw` tail.
        items.append(
            ("call", [peer_name, "setCursorPos", self.cursorX + self.xOffset, self.cursorY + self.yOffset])
        )
        items.append(("call", [peer_name, "setTextColor", _colors.fromBlit(self.curTextColor)]))
        items.append(("call", [peer_name, "setBackgroundColor", _colors.fromBlit(self.curBackColor)]))
        items.append(("call", [peer_name, "setCursorBlink", self.cursorBlink]))

        if items:
            machine.batch(items, timeout=timeout)
        return current


def new(
    sizeX: int = 51,
    sizeY: int = 19,
    color: bool = True,
    xOffset: int = 0,
    yOffset: int = 0,
) -> Buffer:
    """Lyqyd-compatible factory — `buffer.new(w, h, color, xOff, yOff)`
    returns a fresh `Buffer` with the given dimensions. Provided so
    code translated line-by-line from Lua reads naturally."""
    return Buffer(
        sizeX=sizeX, sizeY=sizeY, color=color, xOffset=xOffset, yOffset=yOffset
    )


__all__ = ["Buffer", "new"]
