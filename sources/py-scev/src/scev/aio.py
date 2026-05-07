# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Async (asyncio) variant of the scev client.

Same wire protocol, same describe-driven introspection, but the
transport runs on `asyncio.get_event_loop().add_reader` so multiple
in-flight calls and an event consumer can coexist without threads:

    async def main():
        async with scev.AsyncMachine() as m:
            # Concurrent calls — both responses route by id, no
            # serialization on our side. The host still processes them
            # one at a time, but the asyncio side doesn't block.
            pong, info, peers = await asyncio.gather(
                m.ping(),
                m.self_info(),
                m.list_peripherals(),
            )

            # Introspected peripheral, async methods
            chest = await m['minecraft:chest_0']
            items = await chest.list()

            # Event stream
            async for ev in m.events():
                match ev:
                    case ModemMessage(message=msg): print(msg)

The async API is *not* a sync wrapper around threads — it's a parallel
implementation sharing only the cobs/msgpack codecs and the
peripheral-class synthesis with the sync side. Sync and async
Machines can coexist in the same process on different fds.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import termios
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import msgpack

from . import _cobs, _rpc
from .events import Event
from .machine import MachineInfo
from .peripheral import (
    Peripheral,
    _build_peripheral_class,
    _make_method,
)


MAX_FRAME = _rpc.MAX_FRAME


# --------------------------------------------------------------- AsyncClient


class AsyncClient:
    """Owns the serial fd, dispatches inbound frames to per-id
    response futures and to event consumers. Single-reader-task design
    — only one coroutine ever calls os.read on the fd, so there's no
    contention on the rx accumulator."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._rx = bytearray()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # Each AsyncEventStream registers a queue here; the dispatcher
        # fans each TAG_EVENT frame out to all of them. Using bounded
        # queues so a runaway consumer can't OOM the host — overflow
        # drops the oldest event silently, with a warning.
        self._event_queues: list[asyncio.Queue[tuple[str, list]]] = []
        self._loop = asyncio.get_running_loop()
        self._readable = asyncio.Event()
        self._closed = False
        # Configure the tty before we start reading from it — same
        # ritual as the sync Client (cfmakeraw, tcflush, leading 0x00).
        self._set_raw()
        self._flush_first_run()
        # Non-blocking fd; we'll wake on readable via add_reader.
        os.set_blocking(fd, False)
        self._loop.add_reader(fd, self._on_readable)
        self._reader_task = self._loop.create_task(self._reader_loop())

    @classmethod
    async def open(cls, path: str = "/dev/ttyS1") -> "AsyncClient":
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            return cls(fd)
        except Exception:
            os.close(fd)
            raise

    def _set_raw(self) -> None:
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[3] = 0
        cflag = attrs[2]
        cflag = (cflag & ~termios.CSIZE) | termios.CS8
        cflag &= ~termios.PARENB
        cflag |= termios.CREAD
        attrs[2] = cflag
        cc = list(attrs[6])
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        attrs[6] = cc
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def _flush_first_run(self) -> None:
        try:
            os.write(self.fd, b"\x00")
        except OSError:
            pass

    # ----------------------------------------------------------- close

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.remove_reader(self.fd)
        except Exception:
            pass
        self._reader_task.cancel()
        try:
            await self._reader_task
        except (asyncio.CancelledError, Exception):
            pass
        # Cancel any in-flight calls so awaiters fail loudly instead
        # of hanging forever after a close.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(_rpc.ProtocolError("client closed"))
        self._pending.clear()
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    # ----------------------------------------------------------- reader

    def _on_readable(self) -> None:
        self._readable.set()

    async def _reader_loop(self) -> None:
        """Single coroutine that owns os.read, drains the rx buffer,
        decodes frames, and routes each to the right consumer."""
        while not self._closed:
            await self._readable.wait()
            self._readable.clear()
            # Drain everything available; we may have been woken with
            # multiple chunks queued.
            while True:
                try:
                    chunk = os.read(self.fd, MAX_FRAME)
                except BlockingIOError:
                    break
                except OSError:
                    return
                if not chunk:
                    # With VMIN=VTIME=0 raw mode, a 0-byte read is
                    # "no data buffered right now", not end-of-file.
                    # break out of the inner drain loop and re-arm
                    # the readable event — there's nothing to do
                    # until more data arrives. NOT a fatal condition.
                    break
                self._rx.extend(chunk)
                if len(self._rx) >= MAX_FRAME and 0 not in self._rx:
                    # Frame ran over cap with no delimiter — resync
                    # by dropping the buffer and letting the next 0x00
                    # start a fresh frame.
                    self._rx.clear()
                # Pull every complete frame currently in the buffer.
                while True:
                    idx = self._rx.find(0)
                    if idx < 0:
                        break
                    frame = bytes(self._rx[:idx])
                    del self._rx[: idx + 1]
                    try:
                        payload = _cobs.decode(frame)
                    except _cobs.CorruptFrame:
                        continue
                    self._dispatch(payload)

    def _dispatch(self, payload: bytes) -> None:
        try:
            arr = msgpack.unpackb(payload, raw=False, strict_map_key=False)
        except Exception:
            return
        if not isinstance(arr, (list, tuple)) or not arr:
            return
        tag = arr[0]
        if tag == _rpc.TAG_RESPONSE and len(arr) == 4:
            rid = arr[1]
            fut = self._pending.pop(rid, None)
            if fut is None or fut.done():
                return  # stale response
            err, result = arr[2], arr[3]
            try:
                resolved = _rpc._unwrap_response(err, result)
            except _rpc.RpcError as e:
                fut.set_exception(e)
            except _rpc.ProtocolError as e:
                fut.set_exception(e)
            else:
                fut.set_result(resolved)
        elif tag == _rpc.TAG_CHUNKED and len(arr) == 4:
            rid = arr[1]
            stream_id = arr[2]
            total_size = arr[3]
            fut = self._pending.pop(rid, None)
            if fut is None or fut.done():
                return  # caller already gave up
            # Spawn a drain task — uses the existing call() machinery
            # to issue read_chunk requests; resolves `fut` when the
            # assembled bytes decode cleanly.
            self._loop.create_task(self._drain_chunked(fut, stream_id, total_size))
        elif tag == _rpc.TAG_EVENT and len(arr) >= 3:
            name = arr[1]
            args = list(arr[2]) if isinstance(arr[2], (list, tuple)) else []
            for q in self._event_queues:
                try:
                    q.put_nowait((name, args))
                except asyncio.QueueFull:
                    # Drop oldest, push newest — back-pressure isn't
                    # something we can apply to the host.
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait((name, args))
                    except asyncio.QueueFull:
                        pass

    # ----------------------------------------------------------- send

    async def _send_frame(self, payload: bytes) -> None:
        if len(payload) > MAX_FRAME:
            raise _rpc.FrameTooLarge(len(payload))
        encoded = _cobs.encode(payload)
        # The fd is non-blocking; loop on partial writes via the
        # event loop's write-ready signal so we don't busy-wait.
        view = memoryview(encoded)
        n = 0
        while n < len(view):
            try:
                n += os.write(self.fd, view[n:])
            except BlockingIOError:
                fut: asyncio.Future[None] = self._loop.create_future()

                def _cb(fut=fut) -> None:
                    if not fut.done():
                        fut.set_result(None)

                self._loop.add_writer(self.fd, _cb)
                try:
                    await fut
                finally:
                    try:
                        self._loop.remove_writer(self.fd)
                    except Exception:
                        pass

    async def call(
        self,
        method: str,
        args: list | None = None,
        timeout: float | None = 5.0,
    ) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = self._loop.create_future()
        self._pending[rid] = fut
        frame = msgpack.packb(
            [_rpc.TAG_REQUEST, rid, method, list(args) if args else []],
            use_bin_type=True,
        )
        try:
            await self._send_frame(frame)
        except Exception:
            self._pending.pop(rid, None)
            raise
        if timeout is None:
            return await fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise _rpc.Timeout from e

    # ----------------------------------------------------------- chunked

    async def _drain_chunked(
        self,
        original: asyncio.Future,
        stream_id: int,
        total_size: int,
    ) -> None:
        """Pull `total_size` bytes of `stream_id` via repeated
        `read_chunk` calls and resolve `original` with the decoded
        Response. Runs in its own task so the dispatch loop is free
        to keep handling other frames (the read_chunk Responses
        included). On any failure mid-drain, resolves `original` with
        a synthetic `RpcError` so the caller's `await` doesn't hang."""
        buf = bytearray()
        offset = 0
        slice_size = min(MAX_FRAME // 2, 32 * 1024)
        try:
            while offset < total_size:
                want = min(slice_size, total_size - offset)
                slice_bytes = await self.call(
                    _rpc.METHOD_READ_CHUNK,
                    [stream_id, offset, want],
                    timeout=None,
                )
                if not isinstance(slice_bytes, (bytes, bytearray)):
                    raise _rpc.ProtocolError(
                        f"read_chunk returned non-bytes: {type(slice_bytes).__name__}"
                    )
                if not slice_bytes:
                    raise _rpc.ProtocolError(
                        f"chunked drain hit EOF at {offset}/{total_size}"
                    )
                buf.extend(slice_bytes)
                offset += len(slice_bytes)
        except _rpc.RpcError as e:
            if not original.done():
                original.set_exception(e)
            return
        except Exception as e:  # noqa: BLE001 — surface anything as a clean error
            if not original.done():
                original.set_exception(
                    _rpc.RpcError(f"chunked drain failed: {e}", code=_rpc.ERR_GENERIC)
                )
            return

        # Assembled bytes are exactly the original Response frame.
        try:
            arr = msgpack.unpackb(bytes(buf), raw=False, strict_map_key=False)
        except Exception as e:
            if not original.done():
                original.set_exception(
                    _rpc.ProtocolError(f"assembled buffer didn't decode: {e}")
                )
            return
        if (
            not isinstance(arr, (list, tuple))
            or len(arr) != 4
            or arr[0] != _rpc.TAG_RESPONSE
        ):
            if not original.done():
                original.set_exception(
                    _rpc.ProtocolError(f"chunked drain: not a Response: {arr!r}")
                )
            return
        try:
            resolved = _rpc._unwrap_response(arr[2], arr[3])
        except _rpc.RpcError as e:
            if not original.done():
                original.set_exception(e)
            return
        except _rpc.ProtocolError as e:
            if not original.done():
                original.set_exception(e)
            return
        if not original.done():
            original.set_result(resolved)

    # ----------------------------------------------------------- events

    def subscribe_events(self, max_buffer: int = 1024) -> asyncio.Queue:
        q: asyncio.Queue[tuple[str, list]] = asyncio.Queue(max_buffer)
        self._event_queues.append(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue) -> None:
        try:
            self._event_queues.remove(q)
        except ValueError:
            pass


# --------------------------------------------------------------- async wrappers


def _make_async_method(sig: dict) -> Any:
    """Async counterpart to peripheral._make_method — same signature
    synthesis, but the body awaits a `call` coroutine."""
    params = sig.get("params", [])
    used: set[str] = {"self"}
    inspect_params: list[inspect.Parameter] = [
        inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ]
    for i, p in enumerate(params):
        from .peripheral import _ident, _lua_to_py

        nm = _ident(p.get("name"), f"arg{i}")
        while nm in used:
            nm = f"{nm}_"
        used.add(nm)
        default = None if p.get("optional") else inspect.Parameter.empty
        inspect_params.append(
            inspect.Parameter(
                nm,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=_lua_to_py(p),
            )
        )
    from .peripheral import _ret_to_py, _render_doc

    sigobj = inspect.Signature(
        inspect_params, return_annotation=_ret_to_py(sig.get("return", "value"))
    )
    method_name = sig["name"]

    async def fn(self: AsyncPeripheral, *args: Any, **kwargs: Any) -> Any:
        bound = sigobj.bind(self, *args, **kwargs)
        bound.apply_defaults()
        positional = list(bound.arguments.values())[1:]
        while positional and positional[-1] is None:
            positional.pop()
        return await self._machine._client.call(  # noqa: SLF001
            "call", [self._name, method_name, *positional], timeout=15.0
        )

    fn.__name__ = method_name
    fn.__qualname__ = method_name
    fn.__signature__ = sigobj  # type: ignore[attr-defined]
    fn.__doc__ = _render_doc(sig)
    return fn


def _build_async_peripheral_class(types: tuple[str, ...], describe: dict) -> type:
    """Mirror of peripheral._build_peripheral_class for async methods.
    Handles the same four shapes (groups / dynamicMethods / method /
    methods) — see the sync version for what each one means."""
    namespace: dict[str, Any] = {}
    seen: set[str] = set()

    def _install_remote(nm: str) -> None:
        async def _remote(self: AsyncPeripheral, *args: Any, _nm=nm) -> Any:
            return await self._machine._client.call(  # noqa: SLF001
                "call", [self._name, _nm, *args], timeout=15.0
            )

        _remote.__name__ = nm
        _remote.__qualname__ = nm
        _remote.__doc__ = f"{nm}(...)  [remote — signature unknown]"
        namespace[nm] = _remote
        seen.add(nm)

    groups = describe.get("groups") or {}
    for _group, sigs in groups.items():
        for sig in sigs:
            method = _make_async_method(sig)
            namespace[sig["name"]] = method
            seen.add(sig["name"])
            for alias in sig.get("aliases") or []:
                if alias and alias not in seen:
                    namespace[alias] = method
                    seen.add(alias)

    # IDynamicPeripheral — name list, no signatures. Same shape as
    # remote-modem peripherals below; different host key.
    for nm in describe.get("dynamicMethods") or []:
        if nm and nm not in seen:
            _install_remote(nm)

    method_def = describe.get("method")
    if method_def:
        method = _make_async_method(method_def)
        namespace[method_def["name"]] = method
        seen.add(method_def["name"])

    for nm in describe.get("methods") or []:
        if nm and nm not in seen:
            _install_remote(nm)

    pretty = "_".join(types).replace(":", "_") if types else "any"
    return type(f"AsyncPeripheral_{pretty}", (AsyncPeripheral,), namespace)


class AsyncPeripheral:
    """Async sibling of Peripheral. Methods are coroutines."""

    def __init__(self, machine: "AsyncMachine", name: str, type_info: dict) -> None:
        self._machine = machine
        self._name = name
        self._type_info = type_info or {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def type(self) -> str | None:
        return self._type_info.get("type")

    @property
    def types(self) -> list[str]:
        return list(self._type_info.get("types") or [])

    @property
    def is_remote(self) -> bool:
        return bool(self._type_info.get("remote"))

    def __repr__(self) -> str:
        kinds = "+".join(self.types) if self.types else "?"
        return f"<AsyncPeripheral {self._name} {kinds}>"

    async def _call(self, method: str, *args: Any, timeout: float = 15.0) -> Any:
        return await self._machine._client.call(  # noqa: SLF001
            "call", [self._name, method, *args], timeout=timeout
        )


class AsyncMachine:
    """Async sibling of Machine. Same RPC surface, but every wrapper is
    a coroutine and `events()` / `pull_event()` are awaitables /
    async iterators.

    Instantiate via `await AsyncMachine.open(...)` or
    `async with AsyncMachine() as m:` (the latter awaits open() for
    you). Constructing directly with the bare `AsyncMachine(path)`
    syntax is *not* supported — opening the serial fd is async."""

    def __init__(self, _client: AsyncClient, _path: str | None) -> None:
        self._client = _client
        self._path = _path
        self._peripheral_cache: dict[str, AsyncPeripheral] = {}
        self._class_cache: dict[tuple[str, ...], type[AsyncPeripheral]] = {}

    @classmethod
    async def open(cls, path: str | None = None) -> "AsyncMachine":
        target = path or os.environ.get("SCEV_SERIAL", "/dev/ttyS1")
        client = await AsyncClient.open(target)
        return cls(client, target)

    async def close(self) -> None:
        await self._client.close()
        self._peripheral_cache.clear()
        self._class_cache.clear()

    async def __aenter__(self) -> "AsyncMachine":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    @property
    def client(self) -> AsyncClient:
        return self._client

    # ----------------------------------------------------- thin RPC wrappers

    async def ping(self) -> str:
        return await self._client.call(_rpc.METHOD_PING, timeout=3.0)

    async def log(self, level: str, msg: str) -> None:
        await self._client.call(_rpc.METHOD_LOG, [level, msg], timeout=3.0)

    async def self_info(self) -> MachineInfo:
        return MachineInfo(await self._client.call(_rpc.METHOD_SELF, timeout=3.0))

    async def list_peripherals(self) -> list[dict]:
        return await self._client.call(_rpc.METHOD_LIST, timeout=5.0)

    async def methods(self, peer: str) -> list[str]:
        return await self._client.call(_rpc.METHOD_METHODS, [peer], timeout=5.0)

    async def type(self, peer: str) -> dict:
        return await self._client.call(_rpc.METHOD_TYPE, [peer], timeout=5.0)

    async def describe(self, peer: str, method: str | None = None) -> dict:
        args = [peer] if method is None else [peer, method]
        return await self._client.call(_rpc.METHOD_DESCRIBE, args, timeout=10.0)

    async def schema(self, event: str | None = None) -> Any:
        args: list[Any] = [] if event is None else [event]
        return await self._client.call(_rpc.METHOD_SCHEMA, args, timeout=5.0)

    async def trace_status(self) -> Any:
        return await self._client.call(_rpc.METHOD_TRACE, ["status"], timeout=5.0)

    async def trace_dump(self) -> Any:
        return await self._client.call(_rpc.METHOD_TRACE, ["dump"], timeout=5.0)

    async def trace_on(self) -> Any:
        return await self._client.call(_rpc.METHOD_TRACE, ["on"], timeout=5.0)

    async def trace_off(self) -> Any:
        return await self._client.call(_rpc.METHOD_TRACE, ["off"], timeout=5.0)

    async def trace_clear(self) -> Any:
        return await self._client.call(_rpc.METHOD_TRACE, ["clear"], timeout=5.0)

    @asynccontextmanager
    async def trace(self) -> AsyncIterator["AsyncMachine"]:
        await self.trace_on()
        try:
            yield self
        finally:
            await self.trace_off()

    async def queue_event(self, name: str, *args: Any) -> None:
        await self._client.call(_rpc.METHOD_QUEUE_EVENT, [name, *args], timeout=5.0)

    async def call(
        self,
        peer: str,
        method: str,
        *args: Any,
        timeout: float = 15.0,
    ) -> Any:
        return await self._client.call(
            _rpc.METHOD_CALL, [peer, method, *args], timeout=timeout
        )

    # ----------------------------------------------------- peripheral access

    async def __getitem__(self, name: str) -> AsyncPeripheral:
        return await self.get(name)

    async def get(self, name: str) -> AsyncPeripheral:
        cached = self._peripheral_cache.get(name)
        if cached is not None:
            return cached
        info = await self.type(name)
        types = tuple(info.get("types") or [])
        cls = self._class_cache.get(types)
        if cls is None:
            describe = await self.describe(name)
            cls = _build_async_peripheral_class(types, describe)
            self._class_cache[types] = cls  # type: ignore[assignment]
        p = cls(self, name, info)
        self._peripheral_cache[name] = p
        return p

    async def find(
        self,
        peripheral_type: str,
        filter: Any = None,
    ) -> list[AsyncPeripheral]:
        out: list[AsyncPeripheral] = []
        for entry in await self.list_peripherals():
            if peripheral_type not in (entry.get("types") or []):
                continue
            peer_name = entry.get("peer")
            if not isinstance(peer_name, str):
                continue
            p = await self.get(peer_name)
            if filter is not None:
                ok = filter(peer_name, p)
                if inspect.isawaitable(ok):
                    ok = await ok
                if not ok:
                    continue
            out.append(p)
        return out

    async def find_first(
        self,
        peripheral_type: str,
        filter: Any = None,
    ) -> AsyncPeripheral | None:
        for entry in await self.list_peripherals():
            if peripheral_type not in (entry.get("types") or []):
                continue
            peer_name = entry.get("peer")
            if not isinstance(peer_name, str):
                continue
            p = await self.get(peer_name)
            if filter is None:
                return p
            ok = filter(peer_name, p)
            if inspect.isawaitable(ok):
                ok = await ok
            if ok:
                return p
        return None

    # ----------------------------------------------------- events

    async def events(
        self,
        count: int | None = None,
        *,
        filter: str | tuple[str, ...] | None = None,
        subscribe: bool = True,
        max_buffer: int = 1024,
    ) -> AsyncIterator[Event]:
        """Async generator over inbound events. Yields parsed Event
        instances. Multiple `events()` iterators on the same machine
        all see every event (the dispatcher fans out to per-iterator
        queues). `max_buffer` bounds each queue — overflow drops the
        oldest event."""
        if subscribe:
            try:
                await self._client.call(_rpc.METHOD_SUBSCRIBE, timeout=3.0)
            except _rpc.RpcError:
                pass
        wanted = (
            None
            if filter is None
            else (filter,) if isinstance(filter, str) else tuple(filter)
        )
        q = self._client.subscribe_events(max_buffer=max_buffer)
        try:
            i = 0
            while count is None or i < count:
                name, args = await q.get()
                if wanted is not None and name not in wanted:
                    continue
                yield Event.parse(name, args)
                i += 1
        finally:
            self._client.unsubscribe_events(q)

    async def pull_event(
        self,
        filter: str | tuple[str, ...] | None = None,
        *,
        timeout: float | None = None,
    ) -> Event:
        """Single-shot. Returns one parsed Event matching `filter` (or
        any event if filter is None). Raises asyncio.TimeoutError on
        timeout."""
        wanted = (
            None
            if filter is None
            else (filter,) if isinstance(filter, str) else tuple(filter)
        )
        q = self._client.subscribe_events()
        try:
            while True:
                if timeout is None:
                    name, args = await q.get()
                else:
                    name, args = await asyncio.wait_for(q.get(), timeout)
                if wanted is None or name in wanted:
                    return Event.parse(name, args)
        finally:
            self._client.unsubscribe_events(q)


async def connect(path: str | None = None) -> AsyncMachine:
    """Convenience constructor — `m = await scev.aio.connect()`."""
    return await AsyncMachine.open(path)


# ============================================================== AsyncRednet


class AsyncRednet:
    """Asyncio-native CC rednet client. Same wire format as the sync
    [scev.rednet.Rednet][] class, plus full hostname/protocol service
    discovery (`host` / `unhost` / `lookup`) — those need a background
    `dns` responder running concurrently with normal receive() calls,
    which is exactly what asyncio's dispatcher gives us for free.

    Usage:

        async with AsyncMachine() as m, AsyncRednet(m, computer_id=42) as rn:
            await rn.host("chat", "alice")           # advertise ourselves
            ids = await rn.lookup("chat", timeout=2) # find peers
            for peer in ids:
                await rn.send(peer, "hi", protocol="chat")

            async for msg in rn.iter_messages(protocol="chat"):
                print(f"from {msg.sender}: {msg.message}")
    """

    def __init__(
        self,
        machine: "AsyncMachine",
        computer_id: int,
        modem: "str | AsyncPeripheral | None" = None,
    ) -> None:
        from .rednet import MAX_ID_CHANNELS

        if computer_id < 0 or computer_id >= MAX_ID_CHANNELS:
            raise ValueError(
                f"computer_id {computer_id} out of range [0,{MAX_ID_CHANNELS})"
            )
        self._machine = machine
        self._id = int(computer_id)
        self._modem: "AsyncPeripheral | None" = None
        # Modem can be passed as a string or pre-resolved Peripheral; if
        # neither, `open()` will auto-discover. Lazy resolution lets us
        # construct AsyncRednet outside an async context.
        self._modem_arg = modem
        # Hosted protocols → set of hostnames. Background dns task
        # responds to lookups for any (protocol, hostname) we hold.
        self._hosted: dict[str, set[str]] = {}
        self._dns_task: asyncio.Task | None = None
        self._opened = False

    # --------------------------------------------------------- properties

    @property
    def computer_id(self) -> int:
        return self._id

    @property
    def modem(self) -> "AsyncPeripheral | None":
        return self._modem

    # ------------------------------------------------------------- open

    async def open(self) -> None:
        from .rednet import CHANNEL_BROADCAST

        if self._modem is None:
            arg = self._modem_arg
            if arg is None:
                picked = await self._machine.find_first("modem")
                if picked is None:
                    raise RuntimeError("no modem peripheral attached")
                self._modem = picked
            elif isinstance(arg, str):
                self._modem = await self._machine.get(arg)
            else:
                self._modem = arg

        await self._modem._call("open", self._id)  # noqa: SLF001
        await self._modem._call("open", CHANNEL_BROADCAST)  # noqa: SLF001
        self._opened = True

    async def close(self) -> None:
        from .rednet import CHANNEL_BROADCAST

        # Tear down host responder before closing the modem so any
        # in-flight dns response can complete normally.
        await self._stop_dns()
        if self._modem is not None:
            for ch in (self._id, CHANNEL_BROADCAST):
                try:
                    await self._modem._call("close", ch)  # noqa: SLF001
                except Exception:
                    pass
        self._opened = False

    async def is_open(self) -> bool:
        from .rednet import CHANNEL_BROADCAST

        if self._modem is None:
            return False
        try:
            a = bool(await self._modem._call("isOpen", self._id))  # noqa: SLF001
            b = bool(await self._modem._call("isOpen", CHANNEL_BROADCAST))  # noqa: SLF001
            return a and b
        except Exception:
            return False

    async def __aenter__(self) -> "AsyncRednet":
        await self.open()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # --------------------------------------------------------- send / broadcast

    async def send(
        self,
        recipient: int,
        message: Any,
        protocol: str | None = None,
    ) -> bool:
        from .rednet import _envelope

        if self._modem is None:
            return False
        try:
            await self._modem._call(  # noqa: SLF001
                "transmit",
                int(recipient),
                self._id,
                _envelope(recipient, message, protocol),
            )
            return True
        except Exception:
            return False

    async def broadcast(
        self,
        message: Any,
        protocol: str | None = None,
    ) -> bool:
        from .rednet import CHANNEL_BROADCAST, _envelope

        if self._modem is None:
            return False
        try:
            await self._modem._call(  # noqa: SLF001
                "transmit",
                CHANNEL_BROADCAST,
                self._id,
                _envelope(CHANNEL_BROADCAST, message, protocol),
            )
            return True
        except Exception:
            return False

    # --------------------------------------------------------- receive

    async def receive(
        self,
        protocol_filter: str | None = None,
        timeout: float | None = None,
    ) -> "RednetMessageT | None":
        async for msg in self.iter_messages(protocol=protocol_filter, timeout=timeout):
            return msg
        return None

    async def iter_messages(
        self,
        protocol: str | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator["RednetMessageT"]:
        from .rednet import RednetMessage as RednetMessageS

        try:
            await self._machine._client.call("subscribe", timeout=3.0)  # noqa: SLF001
        except Exception:
            pass

        async for ev in self._machine.events(
            filter=("rednet_message", "modem_message"),
            subscribe=False,
        ):
            triple = self._normalize(ev)
            if triple is None:
                continue
            sender, message, proto = triple
            if sender == self._id:
                continue
            if protocol is not None and proto != protocol:
                continue
            yield RednetMessageS(sender=sender, message=message, protocol=proto)

    def _normalize(self, ev: Event) -> tuple[int, Any, str | None] | None:
        from .events import ModemMessage as MM
        from .events import RednetMessage as RM

        from .rednet import CHANNEL_BROADCAST

        if isinstance(ev, RM):
            return ev.sender, ev.message, ev.protocol
        if isinstance(ev, MM):
            if ev.channel != self._id and ev.channel != CHANNEL_BROADCAST:
                return None
            payload = ev.message
            if not isinstance(payload, dict):
                return None
            if payload.get("nRecipient") not in (self._id, CHANNEL_BROADCAST):
                return None
            proto = payload.get("sProtocol")
            return (
                int(ev.reply_channel),
                payload.get("message"),
                proto if isinstance(proto, str) else None,
            )
        return None

    # --------------------------------------------------------- host/lookup

    async def host(self, protocol: str, hostname: str) -> None:
        """Advertise ourselves as hosting `protocol` under `hostname`.
        Idempotent for the same (protocol, hostname); raises if the
        hostname is the reserved string `localhost` (CC reserves it
        for self-references)."""
        if hostname == "localhost":
            raise ValueError("hostname 'localhost' is reserved")
        self._hosted.setdefault(protocol, set()).add(hostname)
        if self._dns_task is None:
            self._dns_task = asyncio.create_task(self._dns_loop())

    async def unhost(self, protocol: str) -> None:
        """Stop hosting `protocol` (all hostnames). If we're not
        hosting anything anymore, the dns responder is also torn
        down."""
        self._hosted.pop(protocol, None)
        if not self._hosted:
            await self._stop_dns()

    async def lookup(
        self,
        protocol: str,
        hostname: str | None = None,
        timeout: float = 2.0,
    ) -> list[int] | int | None:
        """Broadcast a dns query for `protocol`/`hostname` and collect
        responses for `timeout` seconds.

        Return shape mirrors CC: when `hostname` is given, returns a
        single int (the responder id) or None; when only `protocol`
        is given, returns a list of all responder ids.

        Local hosts are reported too — we answer our own queries to
        match CC's behaviour where a hosting computer's lookup
        includes itself."""
        from .rednet import CHANNEL_BROADCAST, PROTOCOL_DNS, _envelope

        if self._modem is None:
            await self.open()

        # Self-include: if we host this protocol/hostname, our id is
        # part of the result set.
        local_ids: set[int] = set()
        if protocol in self._hosted:
            if hostname is None or hostname in self._hosted[protocol]:
                local_ids.add(self._id)

        # Broadcast the lookup query.
        query = {"sType": "lookup", "sProtocol": protocol, "sHostname": hostname}
        try:
            await self._modem._call(  # noqa: SLF001
                "transmit",
                CHANNEL_BROADCAST,
                self._id,
                _envelope(CHANNEL_BROADCAST, query, PROTOCOL_DNS),
            )
        except Exception:
            pass

        # Collect responses for `timeout` seconds, then return.
        # Responses come on protocol "dns" with message = the responder
        # id; CC also accepts a hostname-shaped response, so we accept
        # either an int payload or a dict with sHostname matching.
        deadline = asyncio.get_running_loop().time() + timeout
        found: set[int] = set(local_ids)
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                ev = await asyncio.wait_for(
                    self._machine.pull_event(filter="modem_message"),
                    remaining,
                )
                triple = self._normalize(ev)
                if triple is None:
                    continue
                sender, message, proto = triple
                if proto != PROTOCOL_DNS:
                    continue
                # `message` is the responder id (int) or a dict with
                # sHostname → match against the requested hostname.
                if isinstance(message, int):
                    found.add(int(message))
                elif isinstance(message, dict):
                    rh = message.get("sHostname")
                    rp = message.get("sProtocol")
                    if rp == protocol and (hostname is None or rh == hostname):
                        found.add(int(sender))
        except asyncio.TimeoutError:
            pass

        if hostname is not None:
            # CC contract: return a single id or None.
            return next(iter(found), None)
        return sorted(found)

    async def _dns_loop(self) -> None:
        """Background task that watches inbound modem messages and
        responds to dns lookups for any protocols/hostnames we host.
        Runs until close() / unhost() drains _hosted, or the task is
        cancelled."""
        from .rednet import PROTOCOL_DNS, _envelope

        try:
            async for ev in self._machine.events(
                filter=("modem_message",), subscribe=False
            ):
                if not self._hosted:
                    return
                triple = self._normalize(ev)
                if triple is None:
                    continue
                sender, message, proto = triple
                if proto != PROTOCOL_DNS:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("sType") != "lookup":
                    continue
                want_proto = message.get("sProtocol")
                want_host = message.get("sHostname")
                if not isinstance(want_proto, str):
                    continue
                hosts = self._hosted.get(want_proto)
                if not hosts:
                    continue
                if want_host is not None and want_host not in hosts:
                    continue
                # Respond with our id back to the sender's id channel.
                # Payload shape: just the id, per CC's rednet impl.
                if self._modem is None:
                    continue
                try:
                    await self._modem._call(  # noqa: SLF001
                        "transmit",
                        int(sender),
                        self._id,
                        _envelope(sender, self._id, PROTOCOL_DNS),
                    )
                except Exception:
                    pass
        except asyncio.CancelledError:
            return

    async def _stop_dns(self) -> None:
        if self._dns_task is None:
            return
        task = self._dns_task
        self._dns_task = None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# Forward-declare for the receive() return type hint above.
from .rednet import RednetMessage as RednetMessageT  # noqa: E402


__all__ = [
    "AsyncClient",
    "AsyncMachine",
    "AsyncPeripheral",
    "AsyncRednet",
    "connect",
]
