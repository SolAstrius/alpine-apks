# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""British-spelling alias of [scev.colors][]. Provided so existing CC
code that uses `colours.grey` / `colours.lightGrey` translates
character-for-character without renaming. The two modules share the
same constants and helpers — this is just a re-export."""

from __future__ import annotations

from .colors import *  # noqa: F401, F403
from .colors import __all__  # noqa: F401
