# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Low-level RPC client over the COBS+msgpack serial transport on
`/dev/ttyS1`.

Mirrors `sources/scev/src/rpc.zig` line-for-line — same correlation-id
scheme, same first-run flush ritual (tcflush + bare 0x00 byte to clear
the host's framer), same MAX_FRAME ceiling, same drop-and-resync
policy on corrupt frames.

Wire format of a frame (after COBS decode):
    msgpack-array of:
        [TAG_REQUEST,  id, method,  args]      — guest → host
        [TAG_RESPONSE, id, err|nil, result]    — host → guest
        [TAG_EVENT,        name,    args]      — host → guest, async
"""

from __future__ import annotations

import os
import select
import termios
import time
from typing import Any, Optional

import msgpack

from . import _cobs


MAX_FRAME = 8192

TAG_REQUEST = 0
TAG_RESPONSE = 1
TAG_EVENT = 2

# Method names — kept identical to the Zig const block in rpc.zig and
# the Kotlin RpcProtocol object so all three clients agree on the
# string. Treat as the source of truth on the Python side.
METHOD_PING = "ping"
METHOD_LOG = "log"
METHOD_LIST = "list"
METHOD_METHODS = "methods"
METHOD_CALL = "call"
METHOD_QUEUE_EVENT = "queue_event"
METHOD_SUBSCRIBE = "subscribe"
METHOD_UNSUBSCRIBE = "unsubscribe"
METHOD_DESCRIBE = "describe"
METHOD_SCHEMA = "schema"
METHOD_TYPE = "type"
METHOD_TRACE = "trace"
METHOD_SELF = "self"


class RpcError(Exception):
    """Host returned an error response (msgpack string in the err slot)."""


class Timeout(Exception):
    """Deadline elapsed before a matching frame arrived."""


class FrameTooLarge(Exception):
    """A single frame exceeded MAX_FRAME without a delimiter."""


class ProtocolError(Exception):
    """Frame structure was wrong (wrong arity, missing fields, …)."""


class Client:
    """Owns the serial fd. Not thread-safe — the host RPC is request/
    response with one in-flight call at a time."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._rx = bytearray()
        self._next_id = 1
        self._set_raw()
        self._flush_first_run()

    @classmethod
    def open(cls, path: str = "/dev/ttyS1") -> "Client":
        # NOCTTY so opening doesn't make this our controlling tty;
        # CLOEXEC so we don't leak the fd into any subprocess.
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            return cls(fd)
        except Exception:
            os.close(fd)
            raise

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ termios

    def _set_raw(self) -> None:
        """cfmakeraw equivalent: clear iflag/oflag/lflag, set CS8 + no
        parity + CREAD on cflag, VMIN=VTIME=0 so reads return whatever
        bytes are available immediately. tcflush at the end drops any
        cruft queued under the previous (cooked) discipline."""
        attrs = termios.tcgetattr(self.fd)
        # attrs = [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
        attrs[0] = 0
        attrs[1] = 0
        attrs[3] = 0
        cflag = attrs[2]
        cflag = (cflag & ~termios.CSIZE) | termios.CS8
        cflag &= ~termios.PARENB
        cflag |= termios.CREAD
        attrs[2] = cflag
        cc = list(attrs[6])
        # cc entries are bytes; termios.VMIN/VTIME are valid indices.
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        attrs[6] = cc
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def _flush_first_run(self) -> None:
        """Send a bare 0x00 delimiter on open. Before the first scev
        invocation the tty was in cooked mode and the host's framer may
        have accumulated echo trash. tcflush above can't recall bytes
        already on the wire — but a 0x00 byte forces the host's
        FrameStream to terminate whatever it had, decode the junk
        (which fails), drop the frame, and reset cleanly. Harmless on a
        clean host. See rpc.zig:open for the full root-cause writeup."""
        try:
            os.write(self.fd, b"\x00")
        except OSError:
            # Ignore — if the write fails, the next sendFrame will
            # surface the real error.
            pass

    # --------------------------------------------------------------- send

    def send_frame(self, payload: bytes) -> None:
        if len(payload) > MAX_FRAME:
            raise FrameTooLarge(len(payload))
        encoded = _cobs.encode(payload)
        view = memoryview(encoded)
        n = 0
        while n < len(view):
            n += os.write(self.fd, view[n:])

    def send_request(self, rid: int, method: str, args: Optional[list] = None) -> None:
        frame = msgpack.packb(
            [TAG_REQUEST, rid, method, list(args) if args else []],
            use_bin_type=True,
        )
        self.send_frame(frame)

    # --------------------------------------------------------------- recv

    def recv_frame(self, deadline: Optional[float]) -> bytes:
        """Return the next decoded frame. `deadline` is an absolute
        time.monotonic() value or None for blocking forever. Corrupt
        frames are dropped silently and we keep scanning."""
        while True:
            idx = self._rx.find(0)
            if idx >= 0:
                frame = bytes(self._rx[:idx])
                del self._rx[: idx + 1]
                try:
                    return _cobs.decode(frame)
                except _cobs.CorruptFrame:
                    continue  # drop bad frame, scan on
            if len(self._rx) >= MAX_FRAME:
                # Frame ran over cap without a delimiter — host is
                # confused or we lost sync. Reset and surface the
                # error so the caller can decide.
                self._rx.clear()
                raise FrameTooLarge(MAX_FRAME)
            self._wait_readable(deadline)
            chunk = os.read(self.fd, MAX_FRAME)
            if not chunk:
                # With VMIN=VTIME=0 raw mode, os.read is allowed to
                # return 0 bytes when no data is currently buffered —
                # this is *not* EOF (a tty has no end-of-file in the
                # usual sense; the remote can pause writing). Loop
                # back to select. The deadline guards against
                # busy-spinning if the host genuinely went away.
                continue
            self._rx.extend(chunk)

    def _wait_readable(self, deadline: Optional[float]) -> None:
        if deadline is None:
            select.select([self.fd], [], [])
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Timeout
        rlist, _, _ = select.select([self.fd], [], [], remaining)
        if not rlist:
            raise Timeout

    # -------------------------------------------------------------- call

    def call(
        self,
        method: str,
        args: Optional[list] = None,
        timeout: Optional[float] = 5.0,
    ) -> Any:
        """One round-trip. Discards interleaved events and stale
        responses until the response with our id arrives or `timeout`
        seconds elapse."""
        rid = self._next_id
        self._next_id += 1
        self.send_request(rid, method, args)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            payload = self.recv_frame(deadline)
            try:
                arr = msgpack.unpackb(payload, raw=False, strict_map_key=False)
            except Exception:
                continue
            if not isinstance(arr, (list, tuple)) or not arr:
                continue
            tag = arr[0]
            if tag != TAG_RESPONSE:
                # Event or malformed — keep waiting.
                continue
            if len(arr) != 4:
                raise ProtocolError(f"response arity {len(arr)}")
            if arr[1] != rid:
                # Stale response from a previous call — drop and keep
                # waiting for ours.
                continue
            err = arr[2]
            result = arr[3]
            if err is None:
                return result
            if isinstance(err, str):
                raise RpcError(err)
            raise ProtocolError(f"non-string err: {err!r}")

    def recv_event(self, timeout: Optional[float] = None) -> tuple[str, list]:
        """Block until the next TAG_EVENT frame and return (name, args).
        Non-event frames (stale responses) are discarded silently."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            payload = self.recv_frame(deadline)
            try:
                arr = msgpack.unpackb(payload, raw=False, strict_map_key=False)
            except Exception:
                continue
            if not isinstance(arr, (list, tuple)) or not arr:
                continue
            if arr[0] == TAG_EVENT and len(arr) >= 3:
                name = arr[1]
                args = arr[2] if isinstance(arr[2], list) else list(arr[2])
                return name, args
            # else: response or malformed — drop
