# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Event parsing — known names build typed dataclasses, unknown names
fall back to generic Event. Only networked + peripheral-driven events
exist here; local-CC-OS events (key/mouse/timer/etc.) are deliberately
not modelled because they don't apply outside the CC OS."""

from __future__ import annotations

from scev import (
    Disk,
    DiskEject,
    Event,
    ModemMessage,
    MonitorResize,
    MonitorTouch,
    PeripheralAttach,
    PeripheralDetach,
    RednetMessage,
    Redstone,
    SpeakerAudioEmpty,
    TurtleInventory,
)


def test_modem_message_parses() -> None:
    ev = Event.parse("modem_message", ["left", 15, 43, {"hello": "world"}, 8.5])
    assert isinstance(ev, ModemMessage)
    assert ev.side == "left"
    assert ev.channel == 15
    assert ev.reply_channel == 43
    assert ev.message == {"hello": "world"}
    assert ev.distance == 8.5


def test_modem_message_no_distance() -> None:
    """Ender modems omit distance — parser pads with None rather than
    failing. Mirrors how CC's own pullEvent handles cross-dimensional
    transmits."""
    ev = Event.parse("modem_message", ["back", 1, 2, "ping"])
    assert isinstance(ev, ModemMessage)
    assert ev.distance is None


def test_modem_message_class_match() -> None:
    ev = Event.parse("modem_message", ["left", 15, 43, "x", 1.0])
    matched = False
    match ev:
        case ModemMessage(channel=15, message=msg):
            assert msg == "x"
            matched = True
    assert matched


def test_rednet_message() -> None:
    ev = Event.parse("rednet_message", [42, "hello", "chat"])
    assert isinstance(ev, RednetMessage)
    assert (ev.sender, ev.message, ev.protocol) == (42, "hello", "chat")


def test_rednet_message_no_protocol() -> None:
    ev = Event.parse("rednet_message", [7, "hi", None])
    assert isinstance(ev, RednetMessage)
    assert ev.protocol is None


def test_unknown_event_fallback() -> None:
    ev = Event.parse("totally_custom", [1, "two", True])
    assert type(ev) is Event
    assert ev.name == "totally_custom"
    assert ev.args == [1, "two", True]


def test_unknown_event_generic_match() -> None:
    """The generic Event(name, args) pattern works for any event,
    known or not."""
    ev = Event.parse("totally_custom", [1, 2, 3])
    matched = False
    match ev:
        case Event("totally_custom", [a, b, c]):
            assert (a, b, c) == (1, 2, 3)
            matched = True
    assert matched


def test_dropped_event_falls_back() -> None:
    """Local-CC-OS events we deliberately don't model (key, mouse,
    timer, alarm, terminate, etc.) should still arrive intact as
    generic Events — the host might forward them and we should
    surface them rather than crash."""
    for name in ("key", "mouse_click", "timer", "terminate", "char"):
        ev = Event.parse(name, [1, 2])
        assert type(ev) is Event, f"{name} should fall back to generic"
        assert ev.name == name


def test_peripheral_attach() -> None:
    ev = Event.parse("peripheral", ["top"])
    assert isinstance(ev, PeripheralAttach)
    assert ev.side == "top"


def test_peripheral_detach() -> None:
    ev = Event.parse("peripheral_detach", ["bottom"])
    assert isinstance(ev, PeripheralDetach)
    assert ev.side == "bottom"


def test_monitor_touch() -> None:
    ev = Event.parse("monitor_touch", ["top", 5, 10])
    assert isinstance(ev, MonitorTouch)
    assert (ev.side, ev.x, ev.y) == ("top", 5, 10)


def test_monitor_resize() -> None:
    ev = Event.parse("monitor_resize", ["right"])
    assert isinstance(ev, MonitorResize)
    assert ev.side == "right"


def test_disk_events() -> None:
    ev = Event.parse("disk", ["left"])
    assert isinstance(ev, Disk)
    assert ev.side == "left"
    ev2 = Event.parse("disk_eject", ["left"])
    assert isinstance(ev2, DiskEject)
    assert ev2.side == "left"


def test_redstone_event_no_args() -> None:
    """Redstone has no payload — args should be the empty list, no
    crash. Fires for both host computer redstone and any networked
    redstone_relay input changes."""
    ev = Event.parse("redstone", [])
    assert isinstance(ev, Redstone)
    assert ev.args == []


def test_speaker_audio_empty() -> None:
    ev = Event.parse("speaker_audio_empty", ["right"])
    assert isinstance(ev, SpeakerAudioEmpty)
    assert ev.side == "right"


def test_turtle_inventory() -> None:
    ev = Event.parse("turtle_inventory", [])
    assert isinstance(ev, TurtleInventory)


def test_args_round_trip_preserved() -> None:
    """Even on a structured event, the raw `args` are kept so callers
    that want the unprocessed payload (e.g., for logging) can get
    it without re-deriving from the dataclass fields."""
    raw = ["left", 1, 2, "msg", 5.0]
    ev = Event.parse("modem_message", raw)
    assert ev.args == raw


def test_malformed_event_falls_back_gracefully() -> None:
    """If the args don't match the expected shape (wrong types,
    missing required values), the parser falls back to a generic
    Event instead of crashing."""
    ev = Event.parse("modem_message", ["left", object(), 0, "x"])
    assert type(ev) is Event
    assert ev.name == "modem_message"
