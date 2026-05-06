# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""scev — Python client for the Scalar Evolution guest RPC.

Two parallel surfaces, both built on the same wire codecs (COBS +
msgpack) and the same describe-driven introspection:

    # Synchronous — ergonomic for scripts, REPL, one-shot tools.
    import scev
    with scev.connect() as m:
        chest = m['minecraft:chest_0']
        chest.list()
        for ev in m.events():
            match ev:
                case scev.ModemMessage(message=msg, channel=ch): ...

    # Async — coroutines, concurrent calls, multi-consumer events.
    import asyncio, scev
    async def main():
        async with scev.AsyncMachine() as m:
            chest = await m['minecraft:chest_0']
            await chest.pushItems('minecraft:chest_1', 1)

    asyncio.run(main())

Plus a CC-faithful Rednet wrapper (sync + async variants) for talking
to CC computers as a peer:

    rn = scev.Rednet(m, computer_id=42)
    with rn:
        rn.send(7, "hi", protocol="chat")
        msg = rn.receive(protocol_filter="chat", timeout=5.0)

The serial path defaults to `/dev/ttyS1`; override via the
`SCEV_SERIAL` environment variable or by passing `path=` to
[`connect`][scev.connect] / [`AsyncMachine.open`][scev.aio.AsyncMachine.open].
"""

from __future__ import annotations

# Sync core + sub-modules. `colors` and `colours` are imported as
# attribute submodules so `scev.colors.RED` and the British alias both
# work without polluting the top-level namespace with 16 colour names.
from . import aio, colors, colours
from ._rpc import (
    Client,
    FrameTooLarge,
    ProtocolError,
    RpcError,
    Timeout,
)
from .events import (
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
from .inventory import Inventory, InventoryItem
from .machine import Machine, MachineInfo, PeripheralView, connect
from .monitor import Cursor, Monitor, Size, find_monitors
from .peripheral import Peripheral
from .rednet import (
    CHANNEL_BROADCAST,
    CHANNEL_REPEAT,
    MAX_ID_CHANNELS,
    PROTOCOL_DNS,
    Rednet,
    RednetMessage as RednetReceived,
)
from .redstone import RedstoneRelay, Side, find_relays

# Async re-exports — cheap to expose at the top level since the user
# already opted into the package by importing scev.
from .aio import (
    AsyncClient,
    AsyncMachine,
    AsyncPeripheral,
    AsyncRednet,
)

__version__ = "0.3.1"

__all__ = [
    # version
    "__version__",
    # exceptions
    "FrameTooLarge",
    "ProtocolError",
    "RpcError",
    "Timeout",
    # transport (low level)
    "Client",
    "AsyncClient",
    # sync API
    "Machine",
    "MachineInfo",
    "Peripheral",
    "PeripheralView",
    "connect",
    # async API
    "AsyncMachine",
    "AsyncPeripheral",
    "AsyncRednet",
    # peripheral wrappers
    "Inventory",
    "InventoryItem",
    "Monitor",
    "Cursor",
    "Size",
    "find_monitors",
    "RedstoneRelay",
    "Side",
    "find_relays",
    # rednet
    "Rednet",
    "RednetReceived",
    "CHANNEL_BROADCAST",
    "CHANNEL_REPEAT",
    "MAX_ID_CHANNELS",
    "PROTOCOL_DNS",
    # events (only the networked + peripheral-driven ones — local CC
    # OS events like keyboard/mouse/timer/etc. don't apply when we're
    # a Linux guest talking over a serial RPC)
    "Event",
    "Disk",
    "DiskEject",
    "ModemMessage",
    "MonitorResize",
    "MonitorTouch",
    "PeripheralAttach",
    "PeripheralDetach",
    "RednetMessage",
    "Redstone",
    "SpeakerAudioEmpty",
    "TurtleInventory",
    # submodules
    "aio",
    "colors",
    "colours",
]
