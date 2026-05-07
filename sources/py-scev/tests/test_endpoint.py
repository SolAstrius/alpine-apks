# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Endpoint parsing + transport-shape tests.

The end-to-end UDS tests stand up a real socket pair and a tiny mock
host (just echoes the right shape of frame) to verify Client connects
and round-trips a frame. Doesn't go through scevd — that's covered by
the Rust workspace's dispatcher tests."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading

import msgpack
import pytest

from scev import _cobs, _endpoint, _rpc


# ---------------------------------------------------------------- parse


def test_parse_unix() -> None:
    ep = _endpoint.parse("unix:///run/scevd.sock")
    assert ep.kind == "unix" and ep.path == "/run/scevd.sock"


def test_parse_tcp() -> None:
    ep = _endpoint.parse("tcp://10.0.0.5:5151")
    assert ep.kind == "tcp"
    assert ep.host == "10.0.0.5"
    assert ep.port == 5151


def test_parse_serial() -> None:
    ep = _endpoint.parse("serial:///dev/ttyS3")
    assert ep.kind == "serial" and ep.path == "/dev/ttyS3"


def test_parse_bare_dev_path_routes_to_serial() -> None:
    ep = _endpoint.parse("/dev/ttyUSB0")
    assert ep.kind == "serial"


def test_parse_bare_socket_path_routes_to_unix() -> None:
    ep = _endpoint.parse("/tmp/x.sock")
    assert ep.kind == "unix"


def test_parse_host_port_shorthand() -> None:
    ep = _endpoint.parse("foo.example:9999")
    assert ep.kind == "tcp"
    assert ep.host == "foo.example"
    assert ep.port == 9999


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        _endpoint.parse("not-a-thing")


# ---------------------------------------------------------------- discover


def test_discover_uses_env_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SCEV_ENDPOINT", "tcp://1.2.3.4:5151")
    ep = _endpoint.discover()
    assert ep.kind == "tcp" and ep.host == "1.2.3.4"


def test_discover_falls_back_to_serial(monkeypatch) -> None:
    monkeypatch.delenv("SCEV_ENDPOINT", raising=False)
    monkeypatch.delenv("SCEV_SERIAL", raising=False)
    # Force the default-socket path to NOT exist by routing it
    # through a non-existent location via a fresh DEFAULT_SOCKET.
    monkeypatch.setattr(_endpoint, "DEFAULT_SOCKET", "/tmp/nonexistent-scevd-socket-xyz")
    ep = _endpoint.discover()
    assert ep.kind == "serial"
    assert ep.path == "/dev/ttyS1"


def test_discover_picks_socket_when_present(tmp_path, monkeypatch) -> None:
    sock_path = tmp_path / "scevd.sock"
    sock_path.write_bytes(b"")
    monkeypatch.delenv("SCEV_ENDPOINT", raising=False)
    monkeypatch.setattr(_endpoint, "DEFAULT_SOCKET", str(sock_path))
    ep = _endpoint.discover()
    assert ep.kind == "unix"
    assert ep.path == str(sock_path)


# ---------------------------------------------------------------- end-to-end UDS


class _MockHost(threading.Thread):
    """Trivial UDS server that accepts one connection, reads one COBS-
    delimited frame, decodes it as a msgpack request, and writes back
    a Response frame mirroring the id with `result=str("pong")`."""

    def __init__(self, sock_path: str) -> None:
        super().__init__(daemon=True)
        self._sock_path = sock_path
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(sock_path)
        self._listener.listen(1)

    def run(self) -> None:
        conn, _ = self._listener.accept()
        try:
            # Read until we see a 0x00 delimiter.
            buf = bytearray()
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf.extend(chunk)
                if 0 in buf:
                    break
            idx = buf.index(0)
            body = bytes(buf[:idx])
            payload = _cobs.decode(body)
            req = msgpack.unpackb(payload, raw=False)
            assert req[0] == _rpc.TAG_REQUEST
            rid = req[1]
            resp = msgpack.packb([_rpc.TAG_RESPONSE, rid, None, "pong"])
            conn.sendall(_cobs.encode(resp))
        finally:
            conn.close()
            self._listener.close()


def test_sync_client_round_trip_over_unix() -> None:
    with tempfile.TemporaryDirectory() as td:
        sock_path = os.path.join(td, "test.sock")
        host = _MockHost(sock_path)
        host.start()
        try:
            client = _rpc.Client.open(f"unix://{sock_path}")
            try:
                result = client.call(_rpc.METHOD_PING, timeout=2.0)
                assert result == "pong"
            finally:
                client.close()
        finally:
            host.join(timeout=2.0)


def test_async_client_round_trip_over_unix() -> None:
    async def run() -> None:
        from scev.aio import AsyncClient

        with tempfile.TemporaryDirectory() as td:
            sock_path = os.path.join(td, "test.sock")
            host = _MockHost(sock_path)
            host.start()
            try:
                client = await AsyncClient.open(f"unix://{sock_path}")
                try:
                    result = await client.call(_rpc.METHOD_PING, timeout=2.0)
                    assert result == "pong"
                finally:
                    await client.close()
            finally:
                host.join(timeout=2.0)

    asyncio.run(run())
