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
        [TAG_REQUEST,   id, method,    args]            — guest → host
        [TAG_RESPONSE,  id, err|nil,   result]          — host → guest
        [TAG_EVENT,         name,      args]            — host → guest
        [TAG_CHUNKED,   id, stream_id, total_size]      — host → guest

The chunked-marker stands in for a Response whose encoded form
exceeds MAX_FRAME. Drain via successive `read_chunk(stream_id, ...)`
calls and decode the assembled buffer as a regular Response — handled
transparently inside `call()` so callers don't see the chunking.

The err slot of a Response is either `nil` (success), a structured
map `{code, message}` (current host), or a bare string (legacy /
forward-compat — wrapped as `{code: GENERIC, message: <str>}`). The
`RpcError` exception always carries both `code` and `message`.
"""

from __future__ import annotations

import os
import select
import termios
import time
from typing import Any, Optional

import msgpack

from . import _cobs


# Max plaintext frame size we'll accumulate or send. Mirrors the host's
# `ScevRpcManager.MAX_FRAME_BYTES`; clients that want to adapt per-host
# can read the authoritative value from the `self` RPC's
# `frame_max_bytes` field. This constant is the upper bound the codec is
# willing to allocate for, sized to accommodate `describe`/`schema`
# payloads and rich event args without splitting frames.
MAX_FRAME = 65536

TAG_REQUEST = 0
TAG_RESPONSE = 1
TAG_EVENT = 2
TAG_CHUNKED = 3

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
METHOD_READ_CHUNK = "read_chunk"
METHOD_DISCARD_CHUNK = "discard_chunk"


# Error codes the host may emit in the structured err map. Treat
# unknown codes as ERR_GENERIC for branching purposes; always show
# `RpcError.message` to the user regardless.
ERR_GENERIC = "rpc_error"
ERR_BAD_ARGS = "bad_args"
ERR_NO_SUCH_METHOD = "no_such_method"
ERR_NO_SUCH_PEER = "no_such_peer"
ERR_LUA_ERROR = "lua_error"
ERR_RUNTIME_ERROR = "runtime_error"
ERR_INTERNAL_ERROR = "internal_error"
ERR_NOT_INSTALLED = "not_installed"
ERR_UNSUPPORTED = "unsupported"
ERR_FRAME_TOO_LARGE = "frame_too_large"


class RpcError(Exception):
    """Host returned a structured error response.

    `code` is one of the `ERR_*` constants (or a future-host code we
    don't yet recognise — branch on it but don't assume the set is
    closed). `message` is the human-readable form. The string
    representation is `"<code>: <message>"` so existing logging that
    just `str()`s the exception still surfaces both.
    """

    def __init__(self, message: str, code: str = ERR_GENERIC) -> None:
        super().__init__(f"{code}: {message}" if code != ERR_GENERIC else message)
        self.code = code
        self.message = message


class Timeout(Exception):
    """Deadline elapsed before a matching frame arrived."""


class FrameTooLarge(Exception):
    """A single frame exceeded MAX_FRAME without a delimiter."""


class ProtocolError(Exception):
    """Frame structure was wrong (wrong arity, missing fields, …)."""


def _unwrap_response(err: Any, result: Any) -> Any:
    """Resolve a (err, result) pair from a TAG_RESPONSE frame.

    Returns `result` on success. Raises `RpcError` for any of the err
    shapes the host might emit:
     - `nil` → success (returns result)
     - dict `{code, message}` → structured error
     - bare string → wrapped as `RpcError(str, code=ERR_GENERIC)` for
       legacy / forward-compat with anything still emitting strings.
    Anything else surfaces as `ProtocolError`.
    """
    if err is None:
        return result
    if isinstance(err, dict):
        code = err.get("code") or ERR_GENERIC
        message = err.get("message") or ""
        if not isinstance(code, str):
            code = str(code)
        if not isinstance(message, str):
            message = str(message)
        raise RpcError(message, code=code)
    if isinstance(err, str):
        raise RpcError(err, code=ERR_GENERIC)
    raise ProtocolError(f"unrecognised err slot: {err!r}")


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
        seconds elapse. Transparently drains chunked responses via
        repeated `read_chunk` calls before returning."""
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
            if tag == TAG_CHUNKED:
                # [TAG_CHUNKED, response_id, stream_id, total_size]
                if len(arr) != 4:
                    raise ProtocolError(f"chunked arity {len(arr)}")
                if arr[1] != rid:
                    # Marker for someone else's call — drop. (Shouldn't
                    # happen against the host because call() is
                    # serial, but tolerant.)
                    continue
                return self._drain_chunked(arr[2], arr[3], deadline)
            if tag != TAG_RESPONSE:
                # Event or malformed — keep waiting.
                continue
            if len(arr) != 4:
                raise ProtocolError(f"response arity {len(arr)}")
            if arr[1] != rid:
                # Stale response from a previous call — drop and keep
                # waiting for ours.
                continue
            return _unwrap_response(arr[2], arr[3])

    def _drain_chunked(
        self,
        stream_id: int,
        total_size: int,
        deadline: Optional[float],
    ) -> Any:
        """Pull `total_size` bytes of `stream_id` via repeated
        `read_chunk` calls, decode the assembled buffer as a
        Response, and return the unwrapped result. Raises `RpcError`
        if the host responded with one — same exception shape as a
        non-chunked call, so callers can't tell the difference."""
        buf = bytearray()
        offset = 0
        # Keep slices well under MAX_FRAME so the read_chunk Response
        # (a `bin` payload of slice_len bytes plus msgpack/cobs
        # overhead) always fits in one wire frame.
        slice_size = min(MAX_FRAME // 2, 32 * 1024)
        while offset < total_size:
            want = min(slice_size, total_size - offset)
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            slice_bytes = self.call(
                METHOD_READ_CHUNK,
                [stream_id, offset, want],
                timeout=remaining,
            )
            if not isinstance(slice_bytes, (bytes, bytearray)):
                raise ProtocolError(
                    f"read_chunk returned non-bytes: {type(slice_bytes).__name__}"
                )
            if not slice_bytes:
                raise ProtocolError(
                    f"chunked drain hit EOF at {offset}/{total_size}"
                )
            buf.extend(slice_bytes)
            offset += len(slice_bytes)
        # Assembled bytes are exactly the original Response frame.
        try:
            arr = msgpack.unpackb(bytes(buf), raw=False, strict_map_key=False)
        except Exception as e:
            raise ProtocolError(f"chunked drain: assembled buffer didn't decode: {e}")
        if (
            not isinstance(arr, (list, tuple))
            or len(arr) != 4
            or arr[0] != TAG_RESPONSE
        ):
            raise ProtocolError(f"chunked drain: not a Response: {arr!r}")
        return _unwrap_response(arr[2], arr[3])

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
