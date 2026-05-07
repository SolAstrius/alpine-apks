# scev — Scalar Evolution guest RPC client (Python)

Python sibling to the Zig `scev` binary. Same wire protocol (COBS +
msgpack over `/dev/ttyS1`) — three implementations (Zig, Kotlin,
Python) are byte-for-byte interoperable. The Python package adds:

- **Describe-driven introspection**: every peripheral method gets a
  real `inspect.Signature` synthesized from the host's reflection
  metadata, so `dir()`, `help()`, and IDE tooltips work out of the
  box.
- **Pattern-matchable events**: known CC event names (`modem_message`,
  `timer`, `key`, …) become typed dataclasses; unknowns fall back to
  `Event(name, args)` — both shapes work in `match`/`case`.
- **CC-faithful Rednet wrapper**: synchronous (open/close/send/
  broadcast/receive) plus an async variant with full service
  discovery (`host`/`unhost`/`lookup`).
- **Sync and async APIs side by side**: pick the one that matches
  your code's shape; same wire, same introspection, same Rednet
  semantics.

## Sync

```python
import scev

with scev.connect() as m:
    m.ping()                              # 'pong'
    info = m.self_info()
    chest = m['minecraft:chest_0']        # describe runs once per type
    chest.list()                          # real method, real signature
    help(chest.pushItems)                 # signature from describe RPC

    # Pattern-match on events
    for ev in m.events(count=10):
        match ev:
            case scev.ModemMessage(side=s, channel=ch, message=msg):
                print(f'{s}@{ch} <- {msg}')
            case scev.Timer(id=t):
                print(f'timer {t} fired')
            case scev.Event(name=n):       # fallback for any unknown name
                print(f'unhandled: {n}')

    # Inject CC events from the guest shell
    m.queue_event('hello_from_python', 'world', 42)

    # Trace a block of work
    with m.trace():
        chest.pushItems('minecraft:chest_1', 1)
    print(m.trace_dump())
```

## Async

```python
import asyncio
import scev

async def main():
    async with scev.AsyncMachine() as m:
        # Concurrent calls — responses route by id, no blocking
        pong, info, peers = await asyncio.gather(
            m.ping(), m.self_info(), m.list_peripherals()
        )

        chest = await m['minecraft:chest_0']
        items = await chest.list()

        async for ev in m.events(count=10):
            match ev:
                case scev.ModemMessage(message=msg): print(msg)

asyncio.run(main())
```

## Rednet

```python
# Sync
rn = scev.Rednet(machine, computer_id=42)
with rn:
    rn.send(target_id=7, message="hi", protocol="chat")
    msg = rn.receive(protocol_filter="chat", timeout=5.0)
    if msg: print(f'from {msg.sender}: {msg.message}')

# Async — adds host/unhost/lookup
async with scev.AsyncRednet(async_machine, computer_id=42) as rn:
    await rn.host("chat", "alice")            # advertise ourselves
    ids = await rn.lookup("chat", timeout=2)  # discover peers
    for peer in ids:
        await rn.send(peer, "hi", protocol="chat")
    async for msg in rn.iter_messages(protocol="chat"):
        print(f'from {msg.sender}: {msg.message}')
```

## CLI (`scev-py`)

Same subcommand surface as the Zig binary (`ping`/`log`/`self`/
`list`/`find`/`type`/`methods`/`methods-like`/`describe`/`call`/
`queue`/`events`/`schema`/`trace`). Argument prefix syntax matches
(`s:hello i:42 b:true`); Python adds `j:<json>` for sending tables:

```sh
scev-py call modem_0 transmit i:15 i:43 j:'{"hello":"world"}'
```

## Transports

`scev` and `AsyncMachine` connect via the system daemon at
`/run/scevd.sock` by default (auto-discovered when the file exists),
falling back to direct serial when no daemon is running. Override
explicitly:

```python
scev.connect("unix:///run/scevd.sock")     # daemon UNIX socket
scev.connect("tcp://10.0.0.5:5151")        # daemon TCP socket (no auth)
scev.connect("serial:///dev/ttyS1")        # direct serial — bypass daemon
```

The CLI accepts the same forms via `--endpoint <URI>` or
`SCEV_ENDPOINT`. Direct-serial mode loses events that arrive between
client invocations; the daemon is the only transport that doesn't.

## Environment

- `SCEV_ENDPOINT` — transport URI (overrides default discovery).
- `SCEV_SERIAL` — direct-serial fallback path (default `/dev/ttyS1`).

## Wire compat

The COBS port is byte-for-byte identical to `sources/scev/src/cobs.zig`
(verified by tests). The first-run flush ritual (`tcflush(TCIOFLUSH)`
plus a leading `0x00` to clear the host's framer) is ported from
`rpc.zig` — see that file for the root-cause writeup of why it's
needed.
