# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Wire-compat tests for the COBS port. Mirror the Zig test cases in
sources/scev/src/cobs.zig so any regression surfaces in either suite."""

from __future__ import annotations

import pytest

from scev import _cobs


def test_round_trip_basic() -> None:
    msg = b"hello"
    enc = _cobs.encode(msg)
    # Encoded frame ends with a 0x00 delimiter; strip it before decode.
    assert enc[-1] == 0
    dec = _cobs.decode(enc[:-1])
    assert dec == msg


def test_round_trip_preserves_zeros() -> None:
    msg = b"\x01\x00\x02\x00\x03"
    enc = _cobs.encode(msg)
    # No 0x00 should appear inside the encoded body.
    assert b"\x00" not in enc[:-1]
    dec = _cobs.decode(enc[:-1])
    assert dec == msg


def test_decode_rejects_truncated_chunk() -> None:
    # Code byte says "4 bytes in this block" but only 2 follow.
    bad = bytes([0x05, ord("a"), ord("b")])
    with pytest.raises(_cobs.CorruptFrame):
        _cobs.decode(bad)


def test_decode_rejects_zero_code_byte() -> None:
    # 0x00 inside a frame means the framer lost sync.
    bad = bytes([0x02, ord("a"), 0x00, ord("b")])
    with pytest.raises(_cobs.CorruptFrame):
        _cobs.decode(bad)


def test_254_byte_run_uses_ff_code() -> None:
    payload = bytes((i % 255) + 1 for i in range(254))
    enc = _cobs.encode(payload)
    dec = _cobs.decode(enc[:-1])
    assert dec == payload
