# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""CC event values returned by `Machine.events()` and `pull_event()`.

scev does *not* run inside the CC operating system — we're a Linux
guest talking to a CC computer over a serial RPC. So we only model
events the host can usefully forward to us:

  * **Networked** — modem_message, rednet_message
  * **Peripheral hot-plug** — peripheral, peripheral_detach
  * **Peripheral-driven** — monitor_touch, monitor_resize, disk,
    disk_eject, speaker_audio_empty
  * **Redstone** — fires whenever a redstone_relay (or the host
    computer's own redstone inputs) change; gating polls on this
    event is the canonical "wait for an input change" pattern
  * **Turtle** — turtle_inventory

Local CC events that don't make sense outside the CC OS — keyboard,
mouse, terminal, scheduler timers/alarms, settings, task_complete,
computer_command, terminate — are deliberately *not* parsed. If the
host ever forwards one anyway it'll surface as a generic Event(name,
args) and pattern-match on the name still works.

Adding a new known event:
    1. Define a new dataclass with `NAME = "..."`,
       `__match_args__ = (...)`, and a `from_args(args)` classmethod.
    2. Decorate with `@_register`.
    3. That's it — `Event.parse(name, args)` will route to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class Event:
    """Base / fallback. Carries `name` + raw `args` for any host event
    we don't have a structured class for. Concrete subclasses add
    typed fields and richer `__match_args__` while keeping the same
    `name`/`args` for round-trip compatibility."""

    name: str
    args: list[Any] = field(default_factory=list)
    __match_args__ = ("name", "args")

    NAME: ClassVar[str] = ""  # overridden by subclasses

    @classmethod
    def parse(cls, name: str, args: list[Any]) -> "Event":
        """Factory: return the appropriate subclass instance for
        `name` if one is registered, otherwise a generic Event."""
        builder = _REGISTRY.get(name)
        if builder is not None:
            try:
                return builder.from_args(args)
            except Exception:
                # Schema mismatch — degrade gracefully to the generic
                # Event so the caller still gets the raw args.
                pass
        return Event(name=name, args=list(args))


# Registry of name → subclass that knows how to parse it.
_REGISTRY: dict[str, type[Event]] = {}


def _register(cls: type[Event]) -> type[Event]:
    if not cls.NAME:
        raise TypeError(f"{cls.__name__}.NAME must be a non-empty string")
    _REGISTRY[cls.NAME] = cls
    return cls


def _pad(args: list[Any], n: int) -> list[Any]:
    """Pad `args` to length `n` with None — host emits fewer values
    than we expect for some events (e.g., distance on ender modems)."""
    if len(args) >= n:
        return args
    return list(args) + [None] * (n - len(args))


# ----------------------------------------------------------- networked


@_register
@dataclass(frozen=True, slots=True)
class ModemMessage(Event):
    """Queued when a modem receives a message on an open channel.
    Distance is None for ender modems (cross-dimensional)."""

    NAME: ClassVar[str] = "modem_message"
    side: str = ""
    channel: int = 0
    reply_channel: int = 0
    message: Any = None
    distance: float | None = None
    __match_args__ = ("side", "channel", "reply_channel", "message", "distance")

    @classmethod
    def from_args(cls, args: list[Any]) -> "ModemMessage":
        a = _pad(args, 5)
        return cls(
            name=cls.NAME,
            args=list(args),
            side=str(a[0]) if a[0] is not None else "",
            channel=int(a[1]) if a[1] is not None else 0,
            reply_channel=int(a[2]) if a[2] is not None else 0,
            message=a[3],
            distance=float(a[4]) if a[4] is not None else None,
        )


@_register
@dataclass(frozen=True, slots=True)
class RednetMessage(Event):
    """Queued by the rednet shim — reply_channel decoded back to a
    sender computer id."""

    NAME: ClassVar[str] = "rednet_message"
    sender: int = 0
    message: Any = None
    protocol: str | None = None
    __match_args__ = ("sender", "message", "protocol")

    @classmethod
    def from_args(cls, args: list[Any]) -> "RednetMessage":
        a = _pad(args, 3)
        return cls(
            name=cls.NAME,
            args=list(args),
            sender=int(a[0]) if a[0] is not None else 0,
            message=a[1],
            protocol=None if a[2] is None else str(a[2]),
        )


# ----------------------------------------------------------- peripheral hot-plug


@_register
@dataclass(frozen=True, slots=True)
class PeripheralAttach(Event):
    """A peripheral was wrapped (placed adjacent or attached via cable)."""

    NAME: ClassVar[str] = "peripheral"
    side: str = ""
    __match_args__ = ("side",)

    @classmethod
    def from_args(cls, args: list[Any]) -> "PeripheralAttach":
        a = _pad(args, 1)
        return cls(name=cls.NAME, args=list(args), side=str(a[0]) if a[0] is not None else "")


@_register
@dataclass(frozen=True, slots=True)
class PeripheralDetach(Event):
    """A peripheral was removed."""

    NAME: ClassVar[str] = "peripheral_detach"
    side: str = ""
    __match_args__ = ("side",)

    @classmethod
    def from_args(cls, args: list[Any]) -> "PeripheralDetach":
        a = _pad(args, 1)
        return cls(name=cls.NAME, args=list(args), side=str(a[0]) if a[0] is not None else "")


# ----------------------------------------------------------- monitor


@_register
@dataclass(frozen=True, slots=True)
class MonitorTouch(Event):
    """Right-click on an Advanced Monitor — CC reports it as a touch
    event with character-grid coordinates, not pixel coordinates."""

    NAME: ClassVar[str] = "monitor_touch"
    side: str = ""
    x: int = 0
    y: int = 0
    __match_args__ = ("side", "x", "y")

    @classmethod
    def from_args(cls, args: list[Any]) -> "MonitorTouch":
        a = _pad(args, 3)
        return cls(
            name=cls.NAME,
            args=list(args),
            side=str(a[0]) if a[0] is not None else "",
            x=int(a[1]) if a[1] is not None else 0,
            y=int(a[2]) if a[2] is not None else 0,
        )


@_register
@dataclass(frozen=True, slots=True)
class MonitorResize(Event):
    """A monitor was resized (block added/removed, or setTextScale).
    Side is the network or block-side name; current dimensions are
    fetched via `monitor.getSize()` after this event fires."""

    NAME: ClassVar[str] = "monitor_resize"
    side: str = ""
    __match_args__ = ("side",)

    @classmethod
    def from_args(cls, args: list[Any]) -> "MonitorResize":
        a = _pad(args, 1)
        return cls(name=cls.NAME, args=list(args), side=str(a[0]) if a[0] is not None else "")


# ----------------------------------------------------------- disk drive


@_register
@dataclass(frozen=True, slots=True)
class Disk(Event):
    """A floppy disk was inserted into a disk drive on `side`."""

    NAME: ClassVar[str] = "disk"
    side: str = ""
    __match_args__ = ("side",)

    @classmethod
    def from_args(cls, args: list[Any]) -> "Disk":
        a = _pad(args, 1)
        return cls(name=cls.NAME, args=list(args), side=str(a[0]) if a[0] is not None else "")


@_register
@dataclass(frozen=True, slots=True)
class DiskEject(Event):
    NAME: ClassVar[str] = "disk_eject"
    side: str = ""
    __match_args__ = ("side",)

    @classmethod
    def from_args(cls, args: list[Any]) -> "DiskEject":
        a = _pad(args, 1)
        return cls(name=cls.NAME, args=list(args), side=str(a[0]) if a[0] is not None else "")


# ----------------------------------------------------------- speaker


@_register
@dataclass(frozen=True, slots=True)
class SpeakerAudioEmpty(Event):
    """The speaker peripheral on `side` finished its DFPWM stream and
    can accept more audio."""

    NAME: ClassVar[str] = "speaker_audio_empty"
    side: str = ""
    __match_args__ = ("side",)

    @classmethod
    def from_args(cls, args: list[Any]) -> "SpeakerAudioEmpty":
        a = _pad(args, 1)
        return cls(name=cls.NAME, args=list(args), side=str(a[0]) if a[0] is not None else "")


# ----------------------------------------------------------- redstone


@_register
@dataclass(frozen=True, slots=True)
class Redstone(Event):
    """Some redstone input changed — on the host computer's own sides
    or on any networked redstone_relay. Payload is empty by design;
    callers re-poll the relevant peripheral (or the host's redstone
    methods) to read the new value. Use as the canonical "wait for an
    input change" gate instead of polling in a tight loop."""

    NAME: ClassVar[str] = "redstone"
    __match_args__ = ()

    @classmethod
    def from_args(cls, args: list[Any]) -> "Redstone":
        return cls(name=cls.NAME, args=list(args))


# ----------------------------------------------------------- turtle


@_register
@dataclass(frozen=True, slots=True)
class TurtleInventory(Event):
    """A turtle's inventory changed — payload is empty, callers
    re-poll `turtle.getItemDetail` per slot."""

    NAME: ClassVar[str] = "turtle_inventory"
    __match_args__ = ()

    @classmethod
    def from_args(cls, args: list[Any]) -> "TurtleInventory":
        return cls(name=cls.NAME, args=list(args))


# Public list of well-known event types — handy for filtering and
# documentation. `Event.parse` is the routing entry point.
__all__ = [
    "Disk",
    "DiskEject",
    "Event",
    "ModemMessage",
    "MonitorResize",
    "MonitorTouch",
    "PeripheralAttach",
    "PeripheralDetach",
    "RednetMessage",
    "Redstone",
    "SpeakerAudioEmpty",
    "TurtleInventory",
]
