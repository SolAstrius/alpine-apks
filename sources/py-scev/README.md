# scev — Scalar Evolution guest RPC client (Python)

Python sibling to the Zig `scev` binary. Same wire protocol (COBS +
msgpack over `/dev/ttyS1`), same CLI surface, plus a real Python
library with describe-driven introspection.

## Library

```python
import scev

with scev.connect() as m:
    m.ping()                              # 'pong'
    info = m.self_info()
    chest = m['minecraft:chest_0']        # dynamic class per peripheral type
    chest.list()                          # real method, real signature
    help(chest.pushItems)                 # signature from host's describe RPC

    # Pattern-match on events
    for ev in m.events(count=10):
        match ev:
            case scev.Event('modem_message', [side, ch, reply, msg, dist]):
                print(f'modem {side}@{ch} <- {msg}')
            case scev.Event('timer', [tid]):
                ...

    # Inject a CC event from the guest shell
    m.queue_event('hello_from_python', 'world', 42)

    # Trace a block of work
    with m.trace():
        chest.pushItems('minecraft:chest_1', 1)
    print(m.trace_dump())
```

## CLI

`scev` (the console script) accepts the same subcommands as the Zig
binary: `ping`, `log`, `self`, `list`, `find`, `type`, `methods`,
`methods-like`, `describe`, `call`, `queue`, `events`, `schema`,
`trace`. Argument prefix syntax (`s:`/`i:`/`f:`/`b:`/`n:`) matches.

Python-only extension: `j:<json>` for sending tables/lists.

```sh
scev list
scev call modem_0 transmit i:15 i:43 j:'{"hello":"world"}'
scev events 5
```

## Environment

- `SCEV_SERIAL` — override serial path (default `/dev/ttyS1`).
