# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Top-level handle for one scev guest's RPC channel.

Wraps the low-level [Client][scev._rpc.Client] with ergonomic accessors
for every host method, plus a Mapping interface over peripherals so
indexing returns a fully-introspected proxy:

    >>> m = scev.connect()
    >>> m.ping()
    'pong'
    >>> chest = m['minecraft:chest_0']      # describe runs once per type
    >>> chest.list()                         # real method call, real types
    [{'count': 64, 'name': 'minecraft:cobblestone'}, ...]
    >>> for ev in m.events():
    ...     match ev:
    ...         case Event('peripheral', [name]): ...
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import _rpc
from .events import Event
from .peripheral import Peripheral, _build_peripheral_class

# Sentinel for "no filter" — None already means "no timeout" in
# pull_event so we need a distinct value for the filter parameter.
_NO_FILTER = object()


@dataclass(frozen=True)
class MachineInfo:
    """Read-only view of the `self` RPC response. Field set is whatever
    the host emits (uuid, hostname, …) — exposed both via attribute
    access for the documented keys and via `.raw` for the full dict."""

    raw: dict[str, Any]

    @property
    def uuid(self) -> str:
        return str(self.raw.get("uuid", ""))

    @property
    def hostname(self) -> str | None:
        v = self.raw.get("hostname")
        return None if v is None else str(v)

    def __getattr__(self, item: str) -> Any:
        # Late-bound passthrough so any future host-added field is
        # accessible as machine_info.someField without a release.
        try:
            return self.raw[item]
        except KeyError as e:
            raise AttributeError(item) from e


class Machine:
    """Owns one Client. Sync, single in-flight request — wrap multiple
    Machines if you need parallel RPC channels (one per ttyS device)."""

    def __init__(self, path: str | None = None) -> None:
        path = path or os.environ.get("SCEV_SERIAL", "/dev/ttyS1")
        self._client = _rpc.Client.open(path)
        self._peripheral_cache: dict[str, Peripheral] = {}
        # Class cache keyed by tuple-of-types — five identical
        # peripherals share the synthesized class; only the bound name
        # differs per instance.
        self._class_cache: dict[tuple[str, ...], type[Peripheral]] = {}
        # Single-call describe cache keyed by *peripheral name* — used
        # only for instance construction; most lookups hit class_cache.
        self._describe_cache: dict[str, dict] = {}

    @property
    def client(self) -> _rpc.Client:
        """Escape hatch for callers that need to issue raw RPC calls."""
        return self._client

    def close(self) -> None:
        self._client.close()
        self._peripheral_cache.clear()
        self._class_cache.clear()
        self._describe_cache.clear()

    def __enter__(self) -> "Machine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----------------------------------------------------- thin RPC wrappers

    def ping(self) -> str:
        return self._client.call(_rpc.METHOD_PING, timeout=3.0)

    def log(self, level: str, msg: str) -> None:
        self._client.call(_rpc.METHOD_LOG, [level, msg], timeout=3.0)

    def self_info(self) -> MachineInfo:
        return MachineInfo(self._client.call(_rpc.METHOD_SELF, timeout=3.0))

    def list_peripherals(self) -> list[dict]:
        """Raw `peripheral.getNames()` equivalent: list of
        `{peer, types}` dicts. Prefer `m.peripherals` for Pythonic
        access; this is the unfiltered host response.

        Named `list_peripherals` rather than `list` to avoid shadowing
        the builtin inside type annotations."""
        return self._client.call(_rpc.METHOD_LIST, timeout=5.0)

    def methods(self, peer: str) -> list[str]:
        return self._client.call(_rpc.METHOD_METHODS, [peer], timeout=5.0)

    def type(self, peer: str) -> dict:
        """Dedicated `METHOD_TYPE` (not the client-side `list`+filter
        the legacy CLI used). Returns the full host map:
        `{peer, type, types, class[, remote]}`."""
        return self._client.call(_rpc.METHOD_TYPE, [peer], timeout=5.0)

    def describe(self, peer: str, method: str | None = None) -> dict:
        args = [peer] if method is None else [peer, method]
        return self._client.call(_rpc.METHOD_DESCRIBE, args, timeout=10.0)

    def schema(self, event: str | None = None) -> Any:
        args: list[Any] = [] if event is None else [event]
        return self._client.call(_rpc.METHOD_SCHEMA, args, timeout=5.0)

    def schema_clear(self) -> None:
        self._client.call(_rpc.METHOD_SCHEMA, ["clear"], timeout=5.0)

    def trace_status(self) -> Any:
        return self._client.call(_rpc.METHOD_TRACE, ["status"], timeout=5.0)

    def trace_dump(self) -> Any:
        return self._client.call(_rpc.METHOD_TRACE, ["dump"], timeout=5.0)

    def trace_on(self) -> Any:
        return self._client.call(_rpc.METHOD_TRACE, ["on"], timeout=5.0)

    def trace_off(self) -> Any:
        return self._client.call(_rpc.METHOD_TRACE, ["off"], timeout=5.0)

    def trace_clear(self) -> Any:
        return self._client.call(_rpc.METHOD_TRACE, ["clear"], timeout=5.0)

    @contextmanager
    def trace(self) -> Iterator[Any]:
        """Context manager: turn tracing on at entry, dump (and return
        via the `as` value indirectly via the next attribute) on exit.
        Caller can read the dump after the `with` block via
        `m.trace_dump()` if needed; this just guarantees the on/off
        pair without leaking trace state."""
        self.trace_on()
        try:
            yield self
        finally:
            self.trace_off()

    def queue_event(self, name: str, *args: Any) -> None:
        """Inject a CC event into the host's computer event queue.
        First arg is the event name; the rest are passed straight
        through to msgpack and converted to Lua types by the host."""
        self._client.call(_rpc.METHOD_QUEUE_EVENT, [name, *args], timeout=5.0)

    def call(self, peer: str, method: str, *args: Any, timeout: float = 15.0) -> Any:
        """Raw `peripheral.call`. Most users should index by name and
        invoke methods on the Peripheral proxy instead — that route
        gets you signature checking and tab completion."""
        return self._client.call(
            _rpc.METHOD_CALL, [peer, method, *args], timeout=timeout
        )

    # -------------------------------------------------------------- events

    def events(
        self,
        count: int | None = None,
        *,
        filter: str | tuple[str, ...] | None = None,
        subscribe: bool = True,
        timeout: float | None = None,
    ) -> Iterator[Event]:
        """Generator over inbound CC events. Yields parsed [Event][]
        instances — known event names get structured subclasses
        (ModemMessage, RednetMessage, MonitorTouch, Disk, …) so
        pattern-matching by class works; unknown names fall back to a
        generic `Event(name, args)`.

        `filter`: a name or tuple of names to keep — others are
        silently discarded. None (default) yields everything.

        `count`: stop after N matches; None (default) is unbounded.

        `timeout`: per-event recv timeout in seconds. `Timeout` is
        raised if no frame arrives within the window. Useful for
        non-blocking polling loops; the inner `recv_event` resets
        the deadline on each call.

        With `subscribe=True` (default) the no-op SUBSCRIBE is sent on
        entry so the host can start any per-machine event pump it
        wants. Errors on subscribe are tolerated — the host's default
        handler is a no-op anyway."""
        if subscribe:
            try:
                self._client.call(_rpc.METHOD_SUBSCRIBE, timeout=3.0)
            except _rpc.RpcError:
                pass
        wanted = (
            None
            if filter is None
            else (filter,) if isinstance(filter, str) else tuple(filter)
        )
        i = 0
        while count is None or i < count:
            name, args = self._client.recv_event(timeout=timeout)
            if wanted is not None and name not in wanted:
                continue
            yield Event.parse(name, args)
            i += 1

    def pull_event(
        self,
        filter: str | tuple[str, ...] | None = None,
        *,
        timeout: float | None = None,
    ) -> Event:
        """Single-shot equivalent of CC's `os.pullEvent([filter])`.
        Blocks until a matching event arrives (or `timeout` elapses,
        if set). Equivalent to `next(m.events(count=1, filter=...,
        subscribe=False, timeout=timeout))` but without the subscribe
        round-trip — assume the caller has already entered an
        events()/subscribe context if needed.

        Returns a parsed [Event][] (structured subclass when known)."""
        wanted = (
            None
            if filter is None
            else (filter,) if isinstance(filter, str) else tuple(filter)
        )
        while True:
            name, args = self._client.recv_event(timeout=timeout)
            if wanted is None or name in wanted:
                return Event.parse(name, args)

    # ----------------------------------------------------- Mapping surface

    @property
    def peripherals(self) -> "PeripheralView":
        """Mapping-like view over the live peripheral roster. Re-reads
        on every access so peripheral hot-plug (`peripheral` /
        `peripheral_detach` events) is reflected immediately."""
        return PeripheralView(self)

    def __getitem__(self, name: str) -> Peripheral:
        cached = self._peripheral_cache.get(name)
        if cached is not None:
            return cached
        # Ask the host what kinds of peripheral we're looking at; that
        # both validates existence and gives us the cache key for the
        # synthesized class.
        info = self.type(name)
        types = tuple(info.get("types") or [])
        cls = self._class_cache.get(types)
        if cls is None:
            describe = self.describe(name)
            cls = _build_peripheral_class(types, describe)
            self._class_cache[types] = cls
        p = cls(self, name, info)
        self._peripheral_cache[name] = p
        return p

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return any(entry.get("peer") == name for entry in self.list_peripherals())

    def find(
        self,
        peripheral_type: str,
        filter: Any = None,
    ) -> list[Peripheral]:
        """Equivalent of CC's `peripheral.find(type, filter)`.

        Returns every peripheral whose type set contains
        `peripheral_type`. Optional `filter(name, peripheral)` callable
        narrows further — return False to skip. The callback receives
        the same fully-introspected Peripheral object the index returns,
        so it can call methods on it to decide:

            chests = m.find("inventory", lambda _, p: p.size() > 27)
        """
        out: list[Peripheral] = []
        for entry in self.list_peripherals():
            if peripheral_type not in (entry.get("types") or []):
                continue
            peer_name = entry.get("peer")
            if not isinstance(peer_name, str):
                continue
            p = self[peer_name]
            if filter is not None and not filter(peer_name, p):
                continue
            out.append(p)
        return out

    def find_first(
        self,
        peripheral_type: str,
        filter: Any = None,
    ) -> Peripheral | None:
        """First match of [find][scev.machine.Machine.find], or None.
        Equivalent to `next(iter(m.find(type, filter)), None)` but
        bails out on first hit so we don't pay describe-cost for
        peripherals we won't use."""
        for entry in self.list_peripherals():
            if peripheral_type not in (entry.get("types") or []):
                continue
            peer_name = entry.get("peer")
            if not isinstance(peer_name, str):
                continue
            p = self[peer_name]
            if filter is None or filter(peer_name, p):
                return p
        return None


class PeripheralView(Mapping[str, Peripheral]):
    """Live view; iter/len/contains hit the host every time so
    long-lived programs see hot-plugged peripherals immediately."""

    def __init__(self, machine: Machine) -> None:
        self._m = machine

    def __iter__(self) -> Iterator[str]:
        for entry in self._m.list_peripherals():
            peer = entry.get("peer")
            if isinstance(peer, str):
                yield peer

    def __len__(self) -> int:
        return len(self._m.list_peripherals())

    def __getitem__(self, key: str) -> Peripheral:
        return self._m[key]


def connect(path: str | None = None) -> Machine:
    """Convenience constructor — `with scev.connect() as m: ...`."""
    return Machine(path)
