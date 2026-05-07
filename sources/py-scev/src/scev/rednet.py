# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""High-level modem helper — CC's `rednet` API in Python.

CC's rednet is a thin convention layered on top of the modem
peripheral: each computer claims a channel equal to its computer id,
also listens on `CHANNEL_BROADCAST`, and exchanges messages wrapped in
a Lua table envelope of shape::

    { nMessageID = N, nRecipient = R, message = ..., sProtocol = P? }

This module talks the same wire format so guest Python code can
participate in a CC rednet network as if it were one of the computers.

The synchronous [Rednet][] class covers open/close/is_open/send/
broadcast/receive/iter_messages — everything that doesn't need a
background listener. For hostname/protocol service discovery
([`host`][scev.aio.AsyncRednet.host]/
[`unhost`][scev.aio.AsyncRednet.unhost]/
[`lookup`][scev.aio.AsyncRednet.lookup]) you want
[`scev.aio.AsyncRednet`][], which has the asyncio dispatcher needed to
run a `dns` responder concurrently with normal `receive()` calls."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from .events import ModemMessage as _ModemMessage
from .events import RednetMessage as _RednetEvent

if TYPE_CHECKING:
    from .machine import Machine
    from .peripheral import Peripheral


# Public constants — names match CC: Tweaked exactly so doc-string
# search and grep behaviour transfer between the two ecosystems.
CHANNEL_BROADCAST: int = 65535
"""The wildcard channel rednet uses for broadcasts."""

CHANNEL_REPEAT: int = 65533
"""The channel CC's rednet repeater listens on / re-emits to."""

MAX_ID_CHANNELS: int = 65500
"""Computers with ids ≥ this wrap around to 0; rednet reserves the
range [MAX_ID_CHANNELS, 65535] for protocol channels."""

# Magic protocol name CC uses for hostname/protocol discovery
# (host/unhost/lookup). Worth pinning here so AsyncRednet can use it
# without circular-importing.
PROTOCOL_DNS: str = "dns"


@dataclass(frozen=True, slots=True)
class RednetMessage:
    """A successfully-received rednet message. Distinct from
    [scev.events.RednetMessage][] (the lower-level event class) so we
    can carry receive-time state without conflating the layers."""

    sender: int
    message: Any
    protocol: str | None = None


def _envelope(
    recipient: int,
    message: Any,
    protocol: str | None,
) -> dict[str, Any]:
    """Build the CC rednet payload table — `nRecipient`/`message`/
    `nMessageID` are mandatory; `sProtocol` is omitted (not nil-set)
    when the caller didn't pass a protocol so the wire matches what
    CC's own rednet emits exactly. The sender id rides in
    modem.transmit's `replyChannel` arg, not in this envelope."""
    out: dict[str, Any] = {
        "nMessageID": secrets.randbits(31),
        "nRecipient": int(recipient),
        "message": message,
    }
    if protocol is not None:
        out["sProtocol"] = protocol
    return out


class Rednet:
    """Synchronous CC-rednet client over scev.

    Owns a modem proxy + a computer id. open/close/send/broadcast/
    receive/iter_messages mirror CC's `rednet.*` semantics. Multiple
    Rednet instances on the same Machine can coexist so long as they
    use distinct computer_ids — they share the same underlying event
    stream but filter on addressee.

    For service discovery (`host` / `lookup`) you want
    [scev.aio.AsyncRednet][] — a background `dns` responder needs the
    async dispatcher to run alongside user `receive()` calls.
    """

    def __init__(
        self,
        machine: "Machine",
        computer_id: int,
        modem: "str | Peripheral | None" = None,
    ) -> None:
        if computer_id < 0 or computer_id >= MAX_ID_CHANNELS:
            raise ValueError(
                f"computer_id {computer_id} out of range [0,{MAX_ID_CHANNELS})"
            )
        self._machine = machine
        self._id = int(computer_id)
        if modem is None:
            picked = machine.find_first("modem")
            if picked is None:
                raise RuntimeError("no modem peripheral attached")
            self._modem = picked
        elif isinstance(modem, str):
            self._modem = machine[modem]
        else:
            self._modem = modem

    # ------------------------------------------------------------ properties

    @property
    def computer_id(self) -> int:
        return self._id

    @property
    def modem(self) -> "Peripheral":
        return self._modem

    # ------------------------------------------------------------ open/close

    def open(self) -> None:
        """Open the modem on this computer's id channel *and*
        CHANNEL_BROADCAST so we can receive both direct messages and
        broadcasts. Mirrors CC's `rednet.open`."""
        self._modem._call("open", self._id)
        self._modem._call("open", CHANNEL_BROADCAST)

    def close(self) -> None:
        """Close both channels we opened. Idempotent — already-closed
        channels surface as harmless RpcErrors that we swallow."""
        for ch in (self._id, CHANNEL_BROADCAST):
            try:
                self._modem._call("close", ch)
            except Exception:
                pass

    def is_open(self) -> bool:
        """True iff *both* the id channel and the broadcast channel
        are open — partial open isn't usable, so we report that case
        as False."""
        try:
            return bool(
                self._modem._call("isOpen", self._id)
            ) and bool(self._modem._call("isOpen", CHANNEL_BROADCAST))
        except Exception:
            return False

    def __enter__(self) -> "Rednet":
        self.open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------ send

    def send(
        self,
        recipient: int,
        message: Any,
        protocol: str | None = None,
    ) -> bool:
        """Send `message` to the computer with id `recipient`. Returns
        True iff the modem accepted the transmit; False if it didn't
        (the modem method is fire-and-forget so a True doesn't
        guarantee delivery — same as CC).

        `protocol`, if given, is carried in the rednet envelope and
        used by `receive(protocol_filter=...)` on the other end to
        ignore traffic for other apps."""
        try:
            self._modem._call(
                "transmit",
                int(recipient),
                self._id,
                _envelope(recipient, message, protocol),
            )
            return True
        except Exception:
            return False

    def broadcast(
        self,
        message: Any,
        protocol: str | None = None,
    ) -> bool:
        """Send to every listening computer (channel 65535).
        Equivalent to `rednet.broadcast`."""
        try:
            self._modem._call(
                "transmit",
                CHANNEL_BROADCAST,
                self._id,
                _envelope(CHANNEL_BROADCAST, message, protocol),
            )
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ receive

    def receive(
        self,
        protocol_filter: str | None = None,
        timeout: float | None = None,
    ) -> RednetMessage | None:
        """Wait for the next rednet message addressed to us (or
        broadcast). Returns None on timeout, the parsed message
        otherwise.

        `protocol_filter` discards messages without that protocol tag.
        Argument order matches CC's `rednet.receive(protocol_filter,
        timeout)` so positional invocations transfer cleanly."""
        try:
            for msg in self.iter_messages(protocol=protocol_filter, timeout=timeout):
                return msg
        except Exception:
            # Timeout in pull_event surfaces here when timeout is set;
            # CC's contract is to return nil/None rather than raise.
            return None
        return None

    def iter_messages(
        self,
        protocol: str | None = None,
        timeout: float | None = None,
    ) -> Iterator[RednetMessage]:
        """Stream rednet messages until `timeout` elapses between
        events (or forever if timeout is None). Filters by protocol
        if given. Discards messages we sent ourselves (sender ==
        our id)."""
        try:
            # Narrow the server-side filter to the two events rednet
            # actually cares about — saves bandwidth on hosts
            # advertising `event_subscriptions`. Legacy hosts treat
            # the names as advisory and forward everything anyway, so
            # the client-side `filter=` tuple in pull_event still
            # protects us.
            if self._machine._client.has_capability("event_subscriptions"):  # noqa: SLF001
                self._machine._client.subscribe(  # noqa: SLF001
                    "rednet_message", "modem_message"
                )
            else:
                self._machine._client.subscribe()  # noqa: SLF001
        except Exception:
            pass

        while True:
            ev = self._machine.pull_event(
                filter=("rednet_message", "modem_message"),
                timeout=timeout,
            )
            triple = self._normalize(ev)
            if triple is None:
                continue
            sender, message, proto = triple
            if sender == self._id:
                continue
            if protocol is not None and proto != protocol:
                continue
            yield RednetMessage(sender=sender, message=message, protocol=proto)

    def _normalize(self, ev: Any) -> tuple[int, Any, str | None] | None:
        """Reduce both rednet_message and modem_message variants to
        (sender, message, protocol) or None if not for us.

        Single source of truth for the addressing rules:
          * channel must be our id or CHANNEL_BROADCAST
          * payload must be a dict shaped like the rednet envelope
          * sender id comes from the modem's reply_channel, NOT from
            anywhere inside the payload (CC stores it that way)
        """
        if isinstance(ev, _RednetEvent):
            return ev.sender, ev.message, ev.protocol
        if isinstance(ev, _ModemMessage):
            if ev.channel != self._id and ev.channel != CHANNEL_BROADCAST:
                return None
            payload = ev.message
            if not isinstance(payload, dict):
                return None
            recipient = payload.get("nRecipient")
            if recipient not in (self._id, CHANNEL_BROADCAST):
                return None
            proto = payload.get("sProtocol")
            return (
                int(ev.reply_channel),
                payload.get("message"),
                proto if isinstance(proto, str) else None,
            )
        return None
