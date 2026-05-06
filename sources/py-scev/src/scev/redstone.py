# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Typed wrapper around CC: Tweaked's `redstone_relay` peripheral.

The relay exposes the same surface as CC's local `redstone` API, but
reachable through a wired-modem network — which is the only way for
guest Python (running outside the CC OS) to touch redstone at all.
We model the six computer sides as a [`Side`][] enum-ish container of
strings so callers don't have to memorise `"top"|"bottom"|"left"|
"right"|"front"|"back"`.

Three signal flavours, matching CC:

  * **Binary** (`output`/`input`): on/off booleans
  * **Analog** (`analog_output`/`analog_input`): 0..15 strength levels
  * **Bundled** (`bundled_output`/`bundled_input`): 16-channel colour
    bitmask, designed to interop with mods like Project: Red

`scev.colors.combine` / `scev.colors.subtract` / `scev.colors.test`
are the canonical helpers for building/parsing bundled bitmasks — see
the `colors` module."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .machine import Machine
    from .peripheral import Peripheral


class Side:
    """Side names CC accepts on every redstone method. Provided as
    class attributes so editor autocomplete fills them in, while still
    being plain strings for direct comparison and JSON-friendliness."""

    TOP: Final[str] = "top"
    BOTTOM: Final[str] = "bottom"
    LEFT: Final[str] = "left"
    RIGHT: Final[str] = "right"
    FRONT: Final[str] = "front"
    BACK: Final[str] = "back"

    ALL: Final[tuple[str, ...]] = ("top", "bottom", "left", "right", "front", "back")


class RedstoneRelay:
    """Pythonic wrapper around the `redstone_relay` peripheral.

    Constructed via `RedstoneRelay.wrap(machine, name)` — the wrap
    step verifies the peripheral really is a relay before returning.

        with scev.connect() as m:
            relay = scev.RedstoneRelay.wrap(m, "redstone_relay_0")
            relay.set_output("top", True)
            print(relay.input("back"))                  # bool
            print(relay.analog_input("back"))           # 0..15

            # Bundled cable I/O — bitmasks of colour bits
            from scev import colors
            relay.set_bundled_output("front",
                colors.combine(colors.RED, colors.WHITE))

            # Block until the inputs change, then re-read
            for ev in m.events(filter="redstone"):
                ...
    """

    def __init__(self, peripheral: "Peripheral") -> None:
        self._p = peripheral

    @classmethod
    def wrap(cls, machine: "Machine", name: str) -> "RedstoneRelay":
        p = machine[name]
        if "redstone_relay" not in p.types:
            raise TypeError(
                f"peripheral {name!r} is not a redstone_relay "
                f"(types: {'+'.join(p.types) or '?'})"
            )
        return cls(p)

    @property
    def peripheral(self) -> "Peripheral":
        """Escape hatch to the introspected proxy."""
        return self._p

    @property
    def name(self) -> str:
        return self._p.name

    # ------------------------------------------------------------ binary

    def set_output(self, side: str, on: bool) -> None:
        """On = full strength (15); off = 0. For finer control use
        [`set_analog_output`][]."""
        self._p._call("setOutput", str(side), bool(on))  # noqa: SLF001

    def output(self, side: str) -> bool:
        return bool(self._p._call("getOutput", str(side)))  # noqa: SLF001

    def input(self, side: str) -> bool:
        return bool(self._p._call("getInput", str(side)))  # noqa: SLF001

    # ------------------------------------------------------------ analog

    def set_analog_output(self, side: str, value: int) -> None:
        """Strength 0..15. Host raises if the value is out of range —
        we don't pre-validate to avoid drifting from the host's rules."""
        self._p._call("setAnalogOutput", str(side), int(value))  # noqa: SLF001

    def analog_output(self, side: str) -> int:
        return int(self._p._call("getAnalogOutput", str(side)))  # noqa: SLF001

    def analog_input(self, side: str) -> int:
        return int(self._p._call("getAnalogInput", str(side)))  # noqa: SLF001

    # British-spelling aliases — kept as bound methods so users coming
    # from Lua code where `setAnalogueOutput` is the canonical name
    # don't have to translate identifiers.
    set_analogue_output = set_analog_output
    analogue_output = analog_output
    analogue_input = analog_input

    # ------------------------------------------------------------ bundled

    def set_bundled_output(self, side: str, mask: int) -> None:
        """Set the colour bitmask the bundled cable on `side` will
        emit. Build masks with `colors.combine(...)`; clear bits with
        `colors.subtract(...)`."""
        self._p._call("setBundledOutput", str(side), int(mask))  # noqa: SLF001

    def bundled_output(self, side: str) -> int:
        return int(self._p._call("getBundledOutput", str(side)))  # noqa: SLF001

    def bundled_input(self, side: str) -> int:
        return int(self._p._call("getBundledInput", str(side)))  # noqa: SLF001

    def test_bundled_input(self, side: str, mask: int) -> bool:
        """True iff every bit in `mask` is set on the bundled input.
        Use `colors.combine(RED, WHITE)` to build the mask."""
        return bool(
            self._p._call("testBundledInput", str(side), int(mask))  # noqa: SLF001
        )

    # ------------------------------------------------------------ repr

    def __repr__(self) -> str:
        return f"<RedstoneRelay {self._p.name}>"


def find_relays(machine: "Machine") -> list[RedstoneRelay]:
    """Wrap every attached redstone_relay. Useful when a base has
    multiple relays scattered across a wired network — iterate them
    all to gate logic on cumulative state."""
    return [RedstoneRelay(p) for p in machine.find("redstone_relay")]


__all__ = ["RedstoneRelay", "Side", "find_relays"]
