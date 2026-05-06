# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""scev — Python client for the Scalar Evolution guest RPC.

Top-level idiom:

    import scev
    with scev.connect() as m:
        m.ping()
        chest = m['minecraft:chest_0']
        for item in chest.list():
            print(item)

The serial path defaults to `/dev/ttyS1`; override via the
`SCEV_SERIAL` environment variable or by passing `path=` to
`connect()`.
"""

from __future__ import annotations

from .events import Event
from .machine import Machine, MachineInfo, PeripheralView, connect
from .peripheral import Peripheral
from ._rpc import (
    Client,
    FrameTooLarge,
    ProtocolError,
    RpcError,
    Timeout,
)

__version__ = "0.1.0"

__all__ = [
    "Client",
    "Event",
    "FrameTooLarge",
    "Machine",
    "MachineInfo",
    "Peripheral",
    "PeripheralView",
    "ProtocolError",
    "RpcError",
    "Timeout",
    "__version__",
    "connect",
]
