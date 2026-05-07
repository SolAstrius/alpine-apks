# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Scalar Evolution contributors.

"""`scev` console script — feature-parity with the Zig binary.

Argument parsing intentionally avoids argparse subparsers so the
typed-positional-args syntax (`s:hello i:42 b:true`) doesn't clash
with argparse's flag handling. The dispatch is a manual switch keyed
on argv[1].

Output formats match the Zig CLI line-for-line where it matters:
`list`, `methods`, `type`, `find`, `methods-like` are all designed to
be shell-pipeable, so we keep the same one-record-per-line shape."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from . import __version__
from . import _rpc
from .machine import Machine

# Bumped independently from the package version so users grepping the
# banner can tell the Python build apart from the Zig one. Format
# matches the Zig SCEV_VERSION ("X.Y-zig" / "X.Y-py").
CLI_VERSION = f"{__version__}-py"

# Filled in by main() from argv[0] basename so error messages say
# "scev-py: ..." (or whatever the script is invoked as) instead of
# hard-coding "scev" and confusing users running both binaries.
PROG = "scev-py"


# ------------------------------------------------------------ helpers

def _err(msg: str) -> int:
    sys.stderr.write(f"{PROG}: {msg}\n")
    return 1


def _dump_json(v: Any) -> None:
    """Generic value printer — used by ping/log/self/call/queue/schema/
    trace and other commands without dedicated formatting. JSON keeps
    output machine-readable; Lua tables round-trip as JSON objects."""
    sys.stdout.write(json.dumps(v, default=str, ensure_ascii=False))
    sys.stdout.write("\n")


def _parse_typed(tok: str) -> Any:
    """Same prefix syntax as the Zig CLI: `t:value` where t is one of
    s/i/f/b/n. Bare tokens (no prefix or unrecognised prefix) become
    strings so shells that don't quote feel natural.

    Extension over the Zig version: `j:<json>` decodes any JSON value,
    so tables/lists are reachable from the shell:
        scev call modem_0 transmit i:15 i:43 j:'{"hello":"world"}'
    """
    if len(tok) >= 2 and tok[1] == ":":
        v = tok[2:]
        c = tok[0]
        if c == "s":
            return v
        if c == "i":
            return int(v)
        if c == "f":
            return float(v)
        if c == "b":
            return v in ("true", "1")
        if c == "n":
            return None
        if c == "j":
            return json.loads(v)
    return tok


# ------------------------------------------------------------ commands

def _cmd_ping(m: Machine, _rest: list[str]) -> int:
    _dump_json(m.ping())
    return 0


def _cmd_log(m: Machine, rest: list[str]) -> int:
    if len(rest) < 2:
        return _err(f"usage: {PROG} log <level> <msg...>")
    level = rest[0]
    msg = " ".join(rest[1:])
    m.log(level, msg)
    sys.stdout.write("null\n")
    return 0


def _cmd_self(m: Machine, _rest: list[str]) -> int:
    info = m.self_info()
    _dump_json(info.raw)
    return 0


def _cmd_list(m: Machine, _rest: list[str]) -> int:
    """Mirrors the Zig `list` formatter: `peer  type[+type...]`."""
    for entry in m.list_peripherals():
        peer = entry.get("peer", "?")
        types = entry.get("types") or []
        sys.stdout.write(f"{peer}  {'+'.join(types)}\n")
    return 0


def _cmd_methods(m: Machine, rest: list[str]) -> int:
    if len(rest) < 1:
        return _err(f"usage: {PROG} methods <peer>")
    for name in m.methods(rest[0]):
        sys.stdout.write(f"{name}\n")
    return 0


def _cmd_type(m: Machine, rest: list[str]) -> int:
    if len(rest) < 1:
        return _err(f"usage: {PROG} type <peer>")
    info = m.type(rest[0])
    for t in info.get("types") or []:
        sys.stdout.write(f"{t}\n")
    return 0


def _cmd_call(m: Machine, rest: list[str]) -> int:
    if len(rest) < 2:
        return _err(f"usage: {PROG} call <peripheral> <method> [args...]")
    peer, method = rest[0], rest[1]
    args = [_parse_typed(t) for t in rest[2:]]
    _dump_json(m.call(peer, method, *args))
    return 0


def _cmd_queue(m: Machine, rest: list[str]) -> int:
    if len(rest) < 1:
        return _err(f"usage: {PROG} queue <event> [args...]")
    name = rest[0]
    args = [_parse_typed(t) for t in rest[1:]]
    m.queue_event(name, *args)
    sys.stdout.write("null\n")
    return 0


def _cmd_events(m: Machine, rest: list[str]) -> int:
    count: int | None = None
    if len(rest) >= 1:
        try:
            n = int(rest[0])
            count = n if n > 0 else None
        except ValueError:
            return _err(f"events: bad count '{rest[0]}'")
    for ev in m.events(count=count):
        sys.stdout.write(ev.name)
        sys.stdout.write(" ")
        sys.stdout.write(json.dumps(ev.args, default=str, ensure_ascii=False))
        sys.stdout.write("\n")
        sys.stdout.flush()
    return 0


def _cmd_schema(m: Machine, rest: list[str]) -> int:
    if rest and rest[0] == "clear":
        m.schema_clear()
        return 0
    event = rest[0] if rest else None
    _dump_json(m.schema(event))
    return 0


def _cmd_trace(m: Machine, rest: list[str]) -> int:
    sub = rest[0] if rest else "dump"
    fn_map = {
        "on": m.trace_on,
        "off": m.trace_off,
        "dump": m.trace_dump,
        "clear": m.trace_clear,
        "status": m.trace_status,
    }
    if sub not in fn_map:
        return _err(f"trace: bad subcommand '{sub}'")
    _dump_json(fn_map[sub]())
    return 0


def _cmd_describe(m: Machine, rest: list[str]) -> int:
    if len(rest) < 1:
        return _err(f"usage: {PROG} describe <peer> [method]")
    peer = rest[0]
    method = rest[1] if len(rest) >= 2 else None
    _dump_json(m.describe(peer, method))
    return 0


def _cmd_find(m: Machine, rest: list[str]) -> int:
    if len(rest) < 1:
        return _err(f"usage: {PROG} find <type>")
    want = rest[0]
    hits = 0
    for entry in m.list_peripherals():
        if want in (entry.get("types") or []):
            sys.stdout.write(f"{entry.get('peer', '?')}\n")
            hits += 1
    return 0 if hits else 2


def _cmd_methods_like(m: Machine, rest: list[str]) -> int:
    if len(rest) < 1:
        return _err(f"usage: {PROG} methods-like <substring>")
    needle = rest[0]
    hits = 0
    for entry in m.list_peripherals():
        peer = entry.get("peer")
        if not isinstance(peer, str):
            continue
        try:
            for name in m.methods(peer):
                if needle in name:
                    sys.stdout.write(f"{peer}:{name}\n")
                    hits += 1
        except _rpc.RpcError:
            continue
    return 0 if hits else 2


# ---------------------------------------------------------- dispatch

# Tuple of (cmd, handler, oneline-help) — the order doubles as the
# usage banner ordering, so keep it intentional.
_COMMANDS: list[tuple[str, Any, str]] = [
    ("ping", _cmd_ping, "liveness check"),
    ("log", _cmd_log, "log to host (trace|debug|info|warn|error)"),
    ("self", _cmd_self, "machine environment info"),
    ("list", _cmd_list, "list peripherals (CC's peripheral.getNames)"),
    ("find", _cmd_find, "list peripherals by type"),
    ("type", _cmd_type, "print a peripheral's type(s)"),
    ("methods", _cmd_methods, "list a peripheral's methods"),
    ("methods-like", _cmd_methods_like, "fuzzy-search method names across peripherals"),
    ("describe", _cmd_describe, "reflection-derived signatures"),
    ("call", _cmd_call, "call a peripheral method"),
    ("queue", _cmd_queue, "inject a CC event"),
    ("events", _cmd_events, "subscribe and print events"),
    ("schema", _cmd_schema, "observed event-argument shapes"),
    ("trace", _cmd_trace, "dispatch-trace control"),
]


def _print_usage() -> None:
    sys.stdout.write(f"{PROG} {CLI_VERSION}\n\n")
    sys.stdout.write(f"usage: {PROG} <subcommand> [args...]\n\nsubcommands:\n")
    for name, _fn, help_text in _COMMANDS:
        sys.stdout.write(f"  {name:<14} {help_text}\n")
    sys.stdout.write(
        "\nargument types (prefix with 't:' where t is):\n"
        "  s:hello   string (default for bare tokens)\n"
        "  i:42      int\n"
        "  f:3.14    double\n"
        "  b:true    bool\n"
        "  n:        nil\n"
        "  j:<json>  JSON value (object/array — Python-only extension)\n"
        "\nendpoint (--endpoint <URI> or SCEV_ENDPOINT env):\n"
        "  unix:///run/scevd.sock     daemon UNIX socket (default if present)\n"
        "  tcp://host:port            daemon TCP socket\n"
        "  serial:///dev/ttyS1        direct serial (bypass daemon)\n"
        "\nenvironment:\n"
        "  SCEV_ENDPOINT   transport URI (overrides default discovery)\n"
        "  SCEV_SERIAL     direct-serial fallback path (when no daemon)\n"
    )


def main(argv: list[str] | None = None) -> int:
    global PROG
    args = list(sys.argv if argv is None else argv)
    if args:
        # Lock the program name from argv[0] so banners / error messages
        # match however the user invoked us — `scev-py`, `python -m scev.cli`,
        # an absolute path, etc. `python -m scev.cli` shows up as the path
        # to cli.py; map that special case back to a friendly name.
        base = os.path.basename(args[0]) or PROG
        if base.endswith(".py") or base == "__main__.py" or base == "cli.py":
            base = "scev-py"
        PROG = base
    if len(args) < 2 or (len(args) >= 2 and args[1] in ("-h", "--help")):
        _print_usage()
        return 0
    if args[1] in ("-V", "--version"):
        sys.stdout.write(f"{PROG} {CLI_VERSION}\n")
        return 0
    cmd = args[1]
    rest = args[2:]
    handler = next((fn for name, fn, _h in _COMMANDS if name == cmd), None)
    if handler is None:
        sys.stderr.write(f"scev: unknown subcommand '{cmd}'\n")
        _print_usage()
        return 64

    # --endpoint isn't a real flag here (we use a manual argv-walk for
    # the typed positional args); pull it out via env or argv pre-scan.
    endpoint = os.environ.get("SCEV_ENDPOINT")
    if not endpoint and "--endpoint" in args:
        i = args.index("--endpoint")
        if i + 1 < len(args):
            endpoint = args[i + 1]
            del args[i : i + 2]
            rest = args[2:]  # rest may have shifted
    try:
        machine = Machine(endpoint)
    except (OSError, ValueError) as e:
        return _err(f"cannot open {endpoint or 'default endpoint'}: {e}")
    try:
        return handler(machine, rest)
    except _rpc.RpcError as e:
        sys.stderr.write(f"scev: rpc returned error: {e}\n")
        return 2
    except _rpc.Timeout:
        sys.stderr.write("scev: rpc error: Timeout\n")
        return 1
    except (_rpc.FrameTooLarge, _rpc.ProtocolError) as e:
        sys.stderr.write(f"scev: rpc error: {type(e).__name__}\n")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        machine.close()


if __name__ == "__main__":
    raise SystemExit(main())
