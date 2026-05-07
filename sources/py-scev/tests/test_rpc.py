# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Unit tests for the wired-up capability surface: Caps parsing,
batch envelope decode, and the integration shape over a mock UDS
host that handles `self`, `subscribe(names)`, `cancel`, `batch`,
`discard_chunk`."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading
import time
from typing import Any

import msgpack
import pytest

from scev import _cobs, _rpc


# ---------------------------------------------------------------- pure unit


def test_caps_parse_full_shape() -> None:
    v = {
        "id": 1,
        "protocol_version": 1,
        "capabilities": {
            "batch": True,
            "cancel": True,
            "event_subscriptions": False,
        },
        "limits": {"frame_max_bytes": 65536},
    }
    c = _rpc._parse_caps(v)
    assert c.protocol_version == 1
    assert c.has("batch")
    assert c.has("cancel")
    assert not c.has("event_subscriptions")
    assert c.frame_max_bytes == 65536


def test_caps_parse_legacy_host_returns_empty() -> None:
    """No protocol_version/capabilities keys → empty Caps so every
    gating site falls back to the unconditional path."""
    v = {"id": 42}
    c = _rpc._parse_caps(v)
    assert c.protocol_version == 0
    assert not c.has("batch")
    assert c.frame_max_bytes == 0


def test_caps_parse_garbage_doesnt_crash() -> None:
    """Anything we don't recognise becomes a default Caps — no
    exceptions for forward-compat keys."""
    assert _rpc._parse_caps(None) == _rpc.Caps()
    assert _rpc._parse_caps([1, 2, 3]) == _rpc.Caps()
    assert _rpc._parse_caps("nope") == _rpc.Caps()


# ---------------------------------------------------------------- batch envelope


def test_batch_envelope_mixed() -> None:
    raw = [
        [None, "ok"],
        [{"code": _rpc.ERR_BAD_ARGS, "message": "missing arg"}, None],
        [{"code": _rpc.ERR_SKIPPED, "message": "skipped"}, None],
    ]
    parsed = _rpc._parse_batch_envelope(raw)
    assert len(parsed) == 3
    assert parsed[0][0] is None and parsed[0][1] == "ok"
    e1 = parsed[1][0]
    assert e1 is not None and e1.code == _rpc.ERR_BAD_ARGS
    assert e1.message == "missing arg"
    e2 = parsed[2][0]
    assert e2 is not None and e2.code == _rpc.ERR_SKIPPED


def test_batch_envelope_legacy_string_err() -> None:
    """Pre-structured-error hosts emitted bare strings; the parser
    wraps them as ERR_GENERIC so the call site doesn't have to
    branch."""
    raw = [["boom", None]]
    parsed = _rpc._parse_batch_envelope(raw)
    err = parsed[0][0]
    assert err is not None and err.code == _rpc.ERR_GENERIC
    assert err.message == "boom"


def test_batch_envelope_garbage_per_item() -> None:
    """Each malformed item surfaces as a generic RpcError instead of
    blowing up the whole list — protects against a buggy host
    leaking partial state."""
    raw = [None, [1], [None, "ok"]]
    parsed = _rpc._parse_batch_envelope(raw)
    assert parsed[0][0] is not None
    assert parsed[1][0] is not None
    assert parsed[2][0] is None and parsed[2][1] == "ok"


# ---------------------------------------------------------------- mock host


class _CapsHost(threading.Thread):
    """UDS server that speaks the full RPC surface used by these tests:

    * `self` returns a configurable `{protocol_version, capabilities,
      limits}` map.
    * `subscribe(names)` records the last-seen filter and returns
      `{filter: nil | [name,...]}`.
    * `cancel(id)` records cancelled ids and returns `{cancelled: bool}`.
    * `discard_chunk(id)` records discarded stream ids, returns True.
    * `batch(items)` returns `[[None, "method:args_count"], ...]`.
    * `ping` returns "pong" — used as a generic round-trip probe.
    * (test-only) `slow` blocks until the host's `slow_release` event
      is set, used to drive cancel-on-timeout.
    """

    def __init__(self, sock_path: str, caps: dict[str, Any]) -> None:
        super().__init__(daemon=True)
        self._sock_path = sock_path
        self._caps = caps
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(sock_path)
        self._listener.listen(1)
        self.subscribed: list[list[str]] = []
        self.cancelled: list[int] = []
        self.discarded: list[int] = []
        self.batches_seen: list[list[Any]] = []
        self.slow_release = threading.Event()
        # Stop the accept loop after this many connections — pytest
        # creates one per test so default to 1.
        self._max_conns = 1

    def run(self) -> None:
        for _ in range(self._max_conns):
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            try:
                self._serve(conn)
            finally:
                conn.close()
        self._listener.close()

    def _serve(self, conn: socket.socket) -> None:
        buf = bytearray()
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                idx = buf.find(0)
                if idx < 0:
                    break
                body = bytes(buf[:idx])
                del buf[: idx + 1]
                try:
                    payload = _cobs.decode(body)
                    req = msgpack.unpackb(payload, raw=False, strict_map_key=False)
                except Exception:
                    continue
                if not isinstance(req, list) or len(req) < 4 or req[0] != _rpc.TAG_REQUEST:
                    continue
                rid, method, args = req[1], req[2], req[3]
                resp = self._dispatch(method, list(args or []))
                if resp is _SLOW_NO_REPLY:
                    # Don't write back — drives client-side timeout.
                    continue
                out = msgpack.packb([_rpc.TAG_RESPONSE, rid, None, resp], use_bin_type=True)
                try:
                    conn.sendall(_cobs.encode(out))
                except OSError:
                    return

    def _dispatch(self, method: str, args: list[Any]) -> Any:
        # Renamed away from `_handle` to avoid clashing with
        # threading.Thread's internal `_handle` attribute.
        if method == _rpc.METHOD_SELF:
            return self._caps
        if method == _rpc.METHOD_PING:
            return "pong"
        if method == _rpc.METHOD_SUBSCRIBE:
            names = args[0] if args and isinstance(args[0], list) else []
            self.subscribed.append(list(names))
            return {"filter": list(names) if names else None}
        if method == _rpc.METHOD_UNSUBSCRIBE:
            names = args[0] if args and isinstance(args[0], list) else []
            return {"filter": list(names)}
        if method == _rpc.METHOD_CANCEL:
            self.cancelled.append(int(args[0]))
            return {"cancelled": True}
        if method == _rpc.METHOD_DISCARD_CHUNK:
            self.discarded.append(int(args[0]))
            return True
        if method == _rpc.METHOD_BATCH or method == _rpc.METHOD_BATCH_PAR:
            items = args[0] if args else []
            self.batches_seen.append(items)
            return [
                [None, f"{itm[0]}:{len(itm[1]) if len(itm) > 1 else 0}"]
                for itm in items
            ]
        if method == "slow":
            # Wait until the test releases us; the client will have
            # timed out and (with cancel cap) sent a CANCEL frame in
            # the meantime. We don't reply — the cancelled coroutine
            # produces no Response per the host contract.
            self.slow_release.wait(timeout=5.0)
            return _SLOW_NO_REPLY
        return {"code": _rpc.ERR_NO_SUCH_METHOD, "message": f"unknown {method}"}


_SLOW_NO_REPLY = object()


# ---------------------------------------------------------------- integration


def _spawn_host(caps: dict[str, Any]) -> tuple[_CapsHost, str, "tempfile.TemporaryDirectory[str]"]:
    td = tempfile.TemporaryDirectory()
    sock_path = os.path.join(td.name, "test.sock")
    host = _CapsHost(sock_path, caps)
    host.start()
    # Tiny pause so accept() is ready before the client connects.
    time.sleep(0.05)
    return host, sock_path, td


_FULL_CAPS = {
    "id": 1,
    "protocol_version": 1,
    "capabilities": {
        "batch": True,
        "batch_par": True,
        "cancel": True,
        "event_subscriptions": True,
        "chunked_transfer": True,
    },
    "limits": {"frame_max_bytes": 65536},
}


def test_handshake_populates_caps() -> None:
    host, sock_path, td = _spawn_host(_FULL_CAPS)
    try:
        client = _rpc.Client.open(f"unix://{sock_path}")
        try:
            caps = client.handshake(timeout=2.0)
            assert caps.protocol_version == 1
            assert client.has_capability("batch")
            assert client.has_capability("cancel")
            assert caps.frame_max_bytes == 65536
        finally:
            client.close()
    finally:
        host.join(timeout=2.0)
        td.cleanup()


def test_subscribe_passes_names_when_capable() -> None:
    host, sock_path, td = _spawn_host(_FULL_CAPS)
    try:
        client = _rpc.Client.open(f"unix://{sock_path}")
        try:
            client.handshake(timeout=2.0)
            client.subscribe("modem_message", "rednet_message", timeout=2.0)
            assert host.subscribed[-1] == ["modem_message", "rednet_message"]
        finally:
            client.close()
    finally:
        host.join(timeout=2.0)
        td.cleanup()


def test_batch_envelope_round_trip() -> None:
    host, sock_path, td = _spawn_host(_FULL_CAPS)
    try:
        client = _rpc.Client.open(f"unix://{sock_path}")
        try:
            client.handshake(timeout=2.0)
            results = client.batch(
                [("ping", []), ("self", [])],
                stop_on_error=False,
                timeout=2.0,
            )
            assert len(results) == 2
            assert results[0][0] is None and results[0][1] == "ping:0"
            assert results[1][0] is None and results[1][1] == "self:0"
        finally:
            client.close()
    finally:
        host.join(timeout=2.0)
        td.cleanup()


def test_cancel_fires_on_timeout_when_capable() -> None:
    """When the client-side `call` times out and the host advertises
    `cancel`, a fire-and-forget CANCEL frame should reach the host
    bearing the timed-out request id."""
    host, sock_path, td = _spawn_host(_FULL_CAPS)
    try:
        client = _rpc.Client.open(f"unix://{sock_path}")
        try:
            caps = client.handshake(timeout=2.0)
            assert caps.has("cancel")
            target_rid = client._next_id  # the id that `slow` will use
            with pytest.raises(_rpc.Timeout):
                client.call("slow", timeout=0.3)
            # Release the host's slow handler so the test cleans up.
            host.slow_release.set()
            # Give the cancel frame time to land.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if host.cancelled:
                    break
                time.sleep(0.02)
            assert host.cancelled and host.cancelled[-1] == target_rid
        finally:
            host.slow_release.set()
            client.close()
    finally:
        host.join(timeout=2.0)
        td.cleanup()


def test_cancel_skipped_when_capability_absent() -> None:
    """Legacy host (no `cancel` cap): the timeout still raises but no
    CANCEL frame is sent — protects against `no_such_method` noise on
    older hosts that registered cancel as a stub."""
    legacy_caps = {"id": 1}  # no protocol_version, no capabilities
    host, sock_path, td = _spawn_host(legacy_caps)
    try:
        client = _rpc.Client.open(f"unix://{sock_path}")
        try:
            client.handshake(timeout=2.0)
            assert not client.has_capability("cancel")
            with pytest.raises(_rpc.Timeout):
                client.call("slow", timeout=0.3)
            host.slow_release.set()
            time.sleep(0.2)
            assert host.cancelled == []
        finally:
            host.slow_release.set()
            client.close()
    finally:
        host.join(timeout=2.0)
        td.cleanup()


def test_async_handshake_and_subscribe() -> None:
    async def run() -> None:
        host, sock_path, td = _spawn_host(_FULL_CAPS)
        try:
            from scev.aio import AsyncClient

            client = await AsyncClient.open(f"unix://{sock_path}")
            try:
                await client.handshake(timeout=2.0)
                assert client.has_capability("batch")
                await client.subscribe("modem_message", timeout=2.0)
                # Give the host's accept thread a moment to land it.
                await asyncio.sleep(0.05)
                assert ["modem_message"] in host.subscribed
            finally:
                await client.close()
        finally:
            host.join(timeout=2.0)
            td.cleanup()

    asyncio.run(run())
