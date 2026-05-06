# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Typed convenience wrapper around CC: Tweaked's `inventory` generic
peripheral.

The describe-driven `Peripheral` proxy already exposes every method
the host advertises (size/list/getItemDetail/getItemLimit/pushItems/
pullItems), but inventories are common enough that a typed
[`Inventory`][] wrapper pays for itself: it converts the raw
`{count, name, nbt}` dicts into [`InventoryItem`][] dataclasses, gives
you Pythonic iteration (`for slot, item in inv`), search helpers, and
predicate-based bulk-move primitives.

Use `Inventory.wrap(machine, name)` to build one — it auto-detects
whether the underlying peripheral really is an inventory and falls
back to a clear error otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterator

if TYPE_CHECKING:
    from .machine import Machine
    from .peripheral import Peripheral


@dataclass(frozen=True, slots=True)
class InventoryItem:
    """One slot's worth of items. `nbt` is None for stacks without
    custom NBT (the common case); when present it's an opaque hash
    string the host computes — useful only as an equality key for
    distinguishing visually-identical-but-mechanically-different
    items (enchanted books, durability variants, etc.)."""

    name: str
    count: int
    nbt: str | None = None
    raw: dict[str, Any] | None = None
    """The original host dict, kept so callers that need fields the
    typed dataclass doesn't surface (mod-specific extensions, future
    additions) can still get to them without a re-fetch."""


class Inventory:
    """Pythonic wrapper around an inventory peripheral.

    Iteration yields (slot, item) pairs, skipping empty slots — same
    convention as CC's `pairs(chest.list())` idiom which uses `pairs`
    rather than `ipairs` because the table is sparse.

        with scev.connect() as m:
            inv = scev.Inventory.wrap(m, "minecraft:chest_0")
            for slot, item in inv:
                print(f"slot {slot}: {item.count} x {item.name}")

            # Move 16 items of every cobblestone stack to chest_1
            inv.move_items(
                "minecraft:chest_1",
                where=lambda slot, item: item.name == "minecraft:cobblestone",
                limit_per_slot=16,
            )
    """

    def __init__(self, peripheral: "Peripheral") -> None:
        self._p = peripheral

    @classmethod
    def wrap(cls, machine: "Machine", name: str) -> "Inventory":
        """Resolve `name` and verify it's an inventory before returning.
        Raises TypeError if the peripheral exists but isn't an
        inventory — fail-fast beats silent fallback to a useless
        wrapper."""
        p = machine[name]
        if "inventory" not in p.types:
            raise TypeError(
                f"peripheral {name!r} is not an inventory "
                f"(types: {'+'.join(p.types) or '?'})"
            )
        return cls(p)

    @property
    def peripheral(self) -> "Peripheral":
        """Escape hatch: the underlying introspected proxy. Use this
        if you need to call a method the typed API doesn't expose."""
        return self._p

    @property
    def name(self) -> str:
        return self._p.name

    # -------------------------------------------------- size / contents

    def size(self) -> int:
        """Number of slots in the inventory. Matches CC's `inv.size()`."""
        return int(self._p._call("size"))  # noqa: SLF001

    def slots(self) -> dict[int, InventoryItem]:
        """Slot → InventoryItem map. Empty slots are absent (sparse —
        same as CC). Slots are 1-indexed because CC is 1-indexed; we
        deliberately don't normalise to 0-based to keep the mapping
        between Lua docs and Python code obvious."""
        raw = self._p._call("list") or {}  # noqa: SLF001
        out: dict[int, InventoryItem] = {}
        if isinstance(raw, dict):
            iterable: Iterator[tuple[Any, Any]] = iter(raw.items())
        else:
            # The host might emit a Lua-array-style msgpack array.
            iterable = ((i + 1, v) for i, v in enumerate(raw or []))
        for slot, item in iterable:
            if not isinstance(item, dict):
                continue
            try:
                slot_i = int(slot)
            except (TypeError, ValueError):
                continue
            out[slot_i] = InventoryItem(
                name=str(item.get("name") or ""),
                count=int(item.get("count") or 0),
                nbt=item.get("nbt") if isinstance(item.get("nbt"), str) else None,
                raw=item,
            )
        return out

    def get_item_detail(self, slot: int) -> dict | None:
        """Full per-stack detail (display name, durability, NBT hash,
        enchantments, …). Returns the raw dict because the schema is
        item-mod-specific — wrapping every possible field in a
        dataclass would be a maintenance nightmare. None for empty
        slots."""
        return self._p._call("getItemDetail", int(slot))  # noqa: SLF001

    def get_item_limit(self, slot: int) -> int:
        """Slot capacity. 64 for vanilla, larger for chests/barrels
        from mods that override stack size."""
        return int(self._p._call("getItemLimit", int(slot)))  # noqa: SLF001

    # -------------------------------------------------- transfer

    def push_items(
        self,
        to_name: str,
        from_slot: int,
        limit: int | None = None,
        to_slot: int | None = None,
    ) -> int:
        """Move from this inventory's `from_slot` to `to_name`. Returns
        the actual count transferred (the destination might be partially
        full and accept fewer than `limit`)."""
        # Optional args drop off the end the same way the Zig CLI's
        # arg-trim logic handles them — Lua `nil` defaults match CC.
        args: list[Any] = [str(to_name), int(from_slot)]
        if limit is not None:
            args.append(int(limit))
            if to_slot is not None:
                args.append(int(to_slot))
        elif to_slot is not None:
            # Can't set to_slot without setting limit — but CC accepts
            # explicit nil; mirror by inserting None.
            args.extend([None, int(to_slot)])
        return int(self._p._call("pushItems", *args))  # noqa: SLF001

    def pull_items(
        self,
        from_name: str,
        from_slot: int,
        limit: int | None = None,
        to_slot: int | None = None,
    ) -> int:
        """Inverse of [push_items][]. Pulls from `from_name`'s
        `from_slot` into us."""
        args: list[Any] = [str(from_name), int(from_slot)]
        if limit is not None:
            args.append(int(limit))
            if to_slot is not None:
                args.append(int(to_slot))
        elif to_slot is not None:
            args.extend([None, int(to_slot)])
        return int(self._p._call("pullItems", *args))  # noqa: SLF001

    # -------------------------------------------------- batched helpers

    def find(
        self,
        predicate: Callable[[int, InventoryItem], bool],
    ) -> list[tuple[int, InventoryItem]]:
        """Return every (slot, item) where `predicate` returns true.
        Walks the live inventory once via `list()` — caller owns
        rate-limiting if calling this in a tight loop."""
        return [
            (slot, item)
            for slot, item in self.slots().items()
            if predicate(slot, item)
        ]

    def find_one(
        self,
        predicate: Callable[[int, InventoryItem], bool],
    ) -> tuple[int, InventoryItem] | None:
        for slot, item in self.slots().items():
            if predicate(slot, item):
                return slot, item
        return None

    def find_by_name(self, name: str) -> list[tuple[int, InventoryItem]]:
        """Convenience wrapper: every slot containing items named
        `name` (e.g. `"minecraft:cobblestone"`)."""
        return self.find(lambda _slot, item: item.name == name)

    def total(self, name: str | None = None) -> int:
        """Sum of `count` across all slots, optionally filtered to a
        single item name. Useful for "do I have at least N stones?"
        scripts."""
        items = self.slots()
        if name is None:
            return sum(it.count for it in items.values())
        return sum(it.count for it in items.values() if it.name == name)

    def move_items(
        self,
        to_name: str,
        where: Callable[[int, InventoryItem], bool] | None = None,
        limit_per_slot: int | None = None,
        max_total: int | None = None,
    ) -> int:
        """Bulk-move primitive: walk each matching slot and `pushItems`
        it to `to_name`. Returns the total count moved.

        `where`: predicate filter; None matches every non-empty slot.
        `limit_per_slot`: caps per-call transfer; None means full stack.
        `max_total`: stop after this many items total; None means
            move everything that matches.

        Useful for "drain all my cobblestone into the storage system"
        style scripts where the destination peripheral name is the
        only thing that varies between use sites."""
        moved = 0
        for slot, item in self.slots().items():
            if where is not None and not where(slot, item):
                continue
            remaining = (
                limit_per_slot if limit_per_slot is not None else item.count
            )
            if max_total is not None:
                remaining = min(remaining, max_total - moved)
                if remaining <= 0:
                    break
            n = self.push_items(to_name, slot, limit=remaining)
            moved += n
            if max_total is not None and moved >= max_total:
                break
        return moved

    # -------------------------------------------------- iteration

    def __iter__(self) -> Iterator[tuple[int, InventoryItem]]:
        return iter(self.slots().items())

    def __len__(self) -> int:
        return self.size()

    def __repr__(self) -> str:
        return f"<Inventory {self._p.name}>"


__all__ = ["Inventory", "InventoryItem"]
