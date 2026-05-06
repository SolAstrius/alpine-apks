# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""CC event values returned by `Machine.events()`.

Kept deliberately small — the host emits arbitrary `(name, args[])`
tuples and the Python side just rehouses them as a structured value
that's pleasant in the REPL and supports structural pattern matching:

    for ev in m.events():
        match ev:
            case Event("modem_message", [side, ch, reply, msg, dist]):
                ...
            case Event("timer", [tid]):
                ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    args: list[Any] = field(default_factory=list)
    # Enables `match Event("foo", [a, b]):` pattern matching.
    __match_args__ = ("name", "args")

    def __repr__(self) -> str:
        return f"Event({self.name!r}, {self.args!r})"
