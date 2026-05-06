# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""Peripheral proxies with describe-driven introspection.

Each peripheral the host exposes gets a dynamically-synthesized Python
class — one method per `@LuaFunction`, with a real `inspect.Signature`
derived from the host's reflective metadata. That means:

  * `dir(p)` returns the live method list (tab completion just works).
  * `help(p.someMethod)` shows the signature with parameter names,
    types, optional markers, enum constraints, return shape, and the
    [mainThread]/[unsafe] flags.
  * `inspect.signature(p.someMethod)` returns a real `Signature`, so
    IDEs, tooling, and runtime introspection all see typed params.
  * Method aliases the host advertises become extra attributes on the
    same impl, so `p.alias()` and `p.canonicalName()` are equivalent.

The class is cached per-peripheral-type-tuple on the owning Machine —
five inventories of the same kind share one synthesized class, only
the bound `_name` differs.
"""

from __future__ import annotations

import inspect
import keyword
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .machine import Machine


# Best-effort Lua-luaType → Python annotation mapping. Lua's "number"
# covers both ints and floats — we annotate as `float` since CC's
# numeric arguments are commonly fractional (durability, redstone
# strengths up to 15 are int but inventory slot indices are also int);
# `int` would underclaim the real domain. Users don't see strict
# type-checking from this anyway, only IDE/REPL hints.
_LUA_TO_PY: dict[str, type | object] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "table": dict,
    "function": Any,
    "any": Any,
    "nil": type(None),
}


def _lua_to_py(param: dict) -> Any:
    base = _LUA_TO_PY.get(param.get("luaType", "any"), Any)
    enum = param.get("enumValues") or []
    if enum:
        # Build a Literal[a, b, c, ...] from the host-advertised allowed
        # values. Falls back to the base type if the values can't be
        # made hashable (e.g., not all strings).
        try:
            return Literal[tuple(enum)]  # type: ignore[misc, valid-type]
        except TypeError:
            return base
    return base


def _ret_to_py(ret: str) -> Any:
    if ret == "none":
        return type(None)
    # "one" / "many" / "dynamic" / "value" all collapse to Any — we'd
    # need return-shape descriptors to do better.
    return Any


def _ident(s: str | None, fallback: str) -> str:
    """Coerce a host-provided parameter name to a valid Python
    identifier. Empty/None/bad names fall back, Python keywords get a
    trailing underscore (PEP 8 idiom)."""
    if not s or not s.isidentifier():
        return fallback
    if keyword.iskeyword(s):
        return s + "_"
    return s


def _render_doc(sig: dict) -> str:
    """Build the docstring shown by `help(method)`. Mirrors the
    structured printer in `main.zig:printSignatureRow` so both clients
    surface the same information at the same level of detail."""
    parts: list[str] = []
    parts.append(f"{sig['name']}(")
    bits: list[str] = []
    for i, p in enumerate(sig.get("params", [])):
        nm = _ident(p.get("name"), f"arg{i}")
        t = p.get("luaType", "any")
        opt = "?" if p.get("optional") else ""
        enum = p.get("enumValues")
        s = f"{nm}: {t}{opt}"
        if enum:
            s += " ∈ {" + "|".join(enum) + "}"
        bits.append(s)
    parts.append(", ".join(bits))
    parts.append(")")
    ret = sig.get("return", "value")
    if ret == "none":
        parts.append(" -> nil")
    elif ret == "many":
        parts.append(" -> value, ...")
    elif ret == "dynamic":
        parts.append(" -> dynamic")
    else:
        parts.append(" -> value")
    flags = []
    if sig.get("mainThread"):
        flags.append("mainThread")
    if sig.get("unsafe"):
        flags.append("unsafe")
    if flags:
        parts.append("  [" + ", ".join(flags) + "]")
    aliases = sig.get("aliases") or []
    if aliases:
        parts.append("\n\nAliases: " + ", ".join(aliases))
    return "".join(parts)


def _make_method(sig: dict) -> Any:
    """Synthesize a bound-method-shaped function with a real Signature
    and docstring derived from one host @LuaFunction descriptor.

    The returned callable is meant to be installed on a class via
    setattr — Python's descriptor protocol handles the `self` bind."""
    params = sig.get("params", [])
    used_names: set[str] = {"self"}
    inspect_params: list[inspect.Parameter] = [
        inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ]
    for i, p in enumerate(params):
        nm = _ident(p.get("name"), f"arg{i}")
        # Avoid clobbering self / clobbering an earlier sibling param.
        while nm in used_names:
            nm = f"{nm}_"
        used_names.add(nm)
        default = None if p.get("optional") else inspect.Parameter.empty
        inspect_params.append(
            inspect.Parameter(
                nm,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=_lua_to_py(p),
            )
        )
    sigobj = inspect.Signature(
        inspect_params,
        return_annotation=_ret_to_py(sig.get("return", "value")),
    )
    method_name = sig["name"]

    def fn(self: Peripheral, *args: Any, **kwargs: Any) -> Any:
        bound = sigobj.bind(self, *args, **kwargs)
        bound.apply_defaults()
        # Strip self; drop trailing Nones the user didn't pass — Lua
        # treats them as `nil`/missing and the host's reflective
        # converter would coerce them back to nil anyway, so we save
        # bytes on the wire and avoid surprising peripherals that
        # check arg count.
        positional = list(bound.arguments.values())[1:]
        while positional and positional[-1] is None:
            positional.pop()
        return self._machine._client.call(  # noqa: SLF001
            "call", [self._name, method_name, *positional], timeout=15.0
        )

    fn.__name__ = method_name
    fn.__qualname__ = method_name
    fn.__signature__ = sigobj  # type: ignore[attr-defined]
    fn.__doc__ = _render_doc(sig)
    return fn


def _make_remote_method(name: str) -> Any:
    """Fallback for IDynamicPeripheral remote methods — host gives us a
    name only, no signature. We accept anything and forward."""

    def fn(self: Peripheral, *args: Any) -> Any:
        return self._machine._client.call(  # noqa: SLF001
            "call", [self._name, name, *args], timeout=15.0
        )

    fn.__name__ = name
    fn.__qualname__ = name
    fn.__doc__ = f"{name}(...)  [remote — signature unknown]"
    return fn


def _build_peripheral_class(types: tuple[str, ...], describe: dict) -> type:
    """Walk a `describe` response and produce a concrete Peripheral
    subclass. Three shapes are handled, matching main.zig:printDescribe:

      * full:   describe[`groups`] = {className: [sig, ...]}
      * narrow: describe[`method`] = sig                (single-method)
      * remote: describe[`methods`] = [name, ...]       (no signatures)
    """
    namespace: dict[str, Any] = {}
    seen: set[str] = set()

    groups = describe.get("groups") or {}
    if groups:
        for _group_name, sigs in groups.items():
            for sig in sigs:
                method = _make_method(sig)
                namespace[sig["name"]] = method
                seen.add(sig["name"])
                for alias in sig.get("aliases") or []:
                    if alias and alias not in seen:
                        namespace[alias] = method
                        seen.add(alias)

    method_def = describe.get("method")
    if method_def:
        method = _make_method(method_def)
        namespace[method_def["name"]] = method
        seen.add(method_def["name"])

    methods_flat = describe.get("methods")
    if methods_flat:
        for nm in methods_flat:
            if nm and nm not in seen:
                namespace[nm] = _make_remote_method(nm)
                seen.add(nm)

    pretty = "_".join(types) if types else "any"
    cls = type(f"Peripheral_{pretty}", (Peripheral,), namespace)
    return cls


class Peripheral:
    """Base class. Real instances are dynamically-synthesized subclasses
    that carry one method per `@LuaFunction` advertised by the host.

    Instances are cheap — the heavy lifting (one `describe` round-trip
    per peripheral *type*) happens in `_build_peripheral_class` and the
    result is cached on the Machine."""

    def __init__(self, machine: "Machine", name: str, type_info: dict) -> None:
        self._machine = machine
        self._name = name
        self._type_info = type_info or {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def type(self) -> str | None:
        return self._type_info.get("type")

    @property
    def types(self) -> list[str]:
        return list(self._type_info.get("types") or [])

    @property
    def is_remote(self) -> bool:
        return bool(self._type_info.get("remote"))

    def __repr__(self) -> str:
        kinds = "+".join(self.types) if self.types else "?"
        return f"<Peripheral {self._name} {kinds}>"

    def _call(self, method: str, *args: Any, timeout: float = 15.0) -> Any:
        """Escape hatch: invoke a host method that didn't show up in
        `describe` (e.g., a runtime-added IDynamicPeripheral method).
        Most users should just call the bound attribute."""
        return self._machine._client.call(  # noqa: SLF001
            "call", [self._name, method, *args], timeout=timeout
        )
