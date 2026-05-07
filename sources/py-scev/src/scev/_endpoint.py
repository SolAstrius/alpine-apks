# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Endpoint URI parsing + auto-discovery — mirrors `scev` (Rust CLI)
exactly so muscle memory and shell scripts transfer between the
binaries.

Three schemes:

    unix:///run/scevd.sock     UNIX socket (the daemon)
    tcp://host:port            TCP socket (daemon, exposed remotely)
    serial:///dev/ttyS1        Direct serial — bypass the daemon

Selection order if no explicit endpoint:
    1. SCEV_ENDPOINT env var
    2. /run/scevd.sock if it exists
    3. SCEV_SERIAL env var or /dev/ttyS1

The CLI ALWAYS prefers the daemon when reachable: only the daemon can
deliver events that arrived between client invocations, which is the
original "events between processes get lost" problem this whole
multiplexer exists to solve.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


DEFAULT_SOCKET = "/run/scevd.sock"
DEFAULT_SERIAL = "/dev/ttyS1"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Discriminated transport handle. `kind` is one of `unix`/`tcp`/
    `serial`. The relevant address fields are populated for the
    matching kind; unused ones are empty/0."""

    kind: str
    path: str = ""        # unix or serial
    host: str = ""        # tcp
    port: int = 0         # tcp

    @property
    def display(self) -> str:
        if self.kind == "tcp":
            return f"tcp://{self.host}:{self.port}"
        if self.kind == "unix":
            return f"unix://{self.path}"
        return f"serial://{self.path}"


def parse(raw: str) -> Endpoint:
    """Parse one of the three URI schemes, or apply the bare-path
    heuristic (`/dev/...` → serial, other absolute path → unix,
    `host:port` → tcp).
    """
    if raw.startswith("unix://"):
        return Endpoint(kind="unix", path=raw[len("unix://"):])
    if raw.startswith("tcp://"):
        host, _, port_s = raw[len("tcp://"):].rpartition(":")
        if not host or not port_s:
            raise ValueError(f"bad tcp endpoint {raw!r} — expected tcp://host:port")
        try:
            port = int(port_s)
        except ValueError as e:
            raise ValueError(f"bad tcp port in {raw!r}: {e}") from None
        return Endpoint(kind="tcp", host=host, port=port)
    if raw.startswith("serial://"):
        return Endpoint(kind="serial", path=raw[len("serial://"):])
    if raw.startswith("/"):
        # Bare absolute path: heuristic. /dev/* → serial, else UDS.
        if raw.startswith("/dev/"):
            return Endpoint(kind="serial", path=raw)
        return Endpoint(kind="unix", path=raw)
    if ":" in raw:
        # host:port shorthand → tcp
        host, _, port_s = raw.rpartition(":")
        try:
            return Endpoint(kind="tcp", host=host, port=int(port_s))
        except ValueError as e:
            raise ValueError(f"bad endpoint {raw!r}: {e}") from None
    raise ValueError(f"can't parse endpoint {raw!r}")


def discover() -> Endpoint:
    """Pick an endpoint from the environment + filesystem.

    Prefers SCEV_ENDPOINT, then /run/scevd.sock, then a serial path
    (SCEV_SERIAL or /dev/ttyS1). The daemon socket beats serial when
    both are present — it's the only transport that doesn't lose events
    between client invocations.
    """
    raw = os.environ.get("SCEV_ENDPOINT")
    if raw:
        return parse(raw)
    if os.path.exists(DEFAULT_SOCKET):
        return Endpoint(kind="unix", path=DEFAULT_SOCKET)
    serial = os.environ.get("SCEV_SERIAL", DEFAULT_SERIAL)
    return Endpoint(kind="serial", path=serial)


def resolve(endpoint: Optional[str | Endpoint] = None) -> Endpoint:
    """Convenience: accept None / str / Endpoint and return an Endpoint.
    Used by `Client.open` / `Machine.__init__` / `AsyncClient.open`
    so callers can pass any of the three forms uniformly."""
    if endpoint is None:
        return discover()
    if isinstance(endpoint, Endpoint):
        return endpoint
    return parse(endpoint)
