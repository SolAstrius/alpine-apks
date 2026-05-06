# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Consistent Overhead Byte Stuffing — direct port of cobs.zig.

Wire-compatible byte-for-byte with the Zig and Java implementations so
all three clients can talk to the same host. Frame layout: COBS-encoded
payload followed by a single 0x00 delimiter.
"""

from __future__ import annotations


class CorruptFrame(Exception):
    """Raised when a COBS frame contains a stray 0x00 or an oversized
    code byte. Callers typically drop the frame and resync on the next
    delimiter."""


def max_encoded_size(in_len: int) -> int:
    """Worst-case encoded length: one overhead byte per 254 input
    bytes, plus the leading code byte and trailing 0x00."""
    return in_len + (in_len // 254) + 2


def encode(data: bytes) -> bytes:
    """Encode `data`, returning the COBS frame including the trailing
    0x00 delimiter."""
    out = bytearray(b"\x00")  # placeholder for first code byte
    code_slot = 0
    code = 1
    for b in data:
        if b == 0:
            out[code_slot] = code
            code_slot = len(out)
            out.append(0)  # placeholder for next code byte
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_slot] = code
                code_slot = len(out)
                out.append(0)
                code = 1
    out[code_slot] = code
    out.append(0)  # trailing delimiter
    return bytes(out)


def decode(frame: bytes) -> bytes:
    """Decode a complete COBS frame. The trailing 0x00 delimiter must
    be stripped before calling — the reader does that as part of frame
    splitting."""
    out = bytearray()
    i = 0
    n = len(frame)
    while i < n:
        code = frame[i]
        if code == 0:
            # 0x00 inside a frame means the framer lost sync.
            raise CorruptFrame("zero code byte mid-frame")
        i += 1
        chunk = code - 1
        if i + chunk > n:
            raise CorruptFrame("chunk overruns frame")
        out += frame[i : i + chunk]
        i += chunk
        # Code 0xFF means "254 non-zero bytes, no implicit zero before
        # the next chunk". All other codes imply a zero UNLESS the chunk
        # we just consumed was the final one.
        if code < 0xFF and i < n:
            out.append(0)
    return bytes(out)
