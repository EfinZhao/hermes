# tools/receiver

This tool checks what a device sends. It receives packets from the firmware (or from `tools/fake_car.py`), checks each one, counts gaps, repeats and reboots, and decodes the values with the registry. That shows whether the firmware's output is right from end to end.
The golden packets check single packets byte for byte. The receiver checks the live stream: CRC, seq counting, batching and timing. The bench test (#13) and the demo (#15) run it on the laptop.
It imports only `messaging/msgcodec.py` and the registry files.

| File | What it does |
|---|---|
| `receiver.py` | The front door and the command line (UDP). It checks each packet, tracks seq gaps per device, and passes good packets to the decoder and your callbacks. |
| `decoder.py` | Turns DATA records into `Value`s and RAW_CAN records into `RawFrame`s, using the registry version the packet names. |
| `registry_store.py` | Holds every registry version it can find: the snapshots in `messaging/registry_archive/` plus the current `registry.yaml`. |
| `timebase.py` | Maps device time (ms since boot) to wall-clock time, and unwraps `base_ms` past its u32 limit. |

## Running it

```
uv run python tools/receiver/receiver.py [--host H] [--port P] [--registry PATH] [--archive DIR] [--quiet] [--idle-exit S]
```

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Address to listen on. `0.0.0.0` accepts packets from any interface, so a device on Wi-Fi can reach it |
| `--port` | `5005` | UDP port (the same default as `tools/fake_car.py`) |
| `--registry` | `messaging/registry.yaml` | The current registry |
| `--archive` | `messaging/registry_archive/` | Snapshots of older registry versions |
| `--quiet` | off | Print only events and the final summary, not every value |
| `--idle-exit` | `0` | Stop after this many seconds without a packet (0 = run until Ctrl-C) |

With no hardware, run it next to the fake car:

```
uv run python tools/receiver/receiver.py                               # terminal 1
uv run python tools/fake_car.py --duration 10 --loss 0.1 --duplicate 0.1   # terminal 2
```

Each value prints on one line (`time  device  name  value  unit  quality flags`). Events such as gaps, repeats, reboots and corrupt packets go to stderr with a `!` in front. Ctrl-C prints a summary of every count.

## What happens to each packet

1. A corrupt packet (too short, bad magic, wrong format version, length or CRC mismatch, record count mismatch) is dropped and counted by reason. The receiver never decodes part of one.
2. DATA and RAW_CAN packets are decoded. COMMAND, HEARTBEAT and unknown types are rejected, as the spec requires. CONFIG and ACK packets are counted as ignored, because this receiver doesn't send CONFIG yet.
3. The receiver tracks seq separately for each device id:
   - When seq jumps ahead, the skipped packets count as lost: `(new seq - last seq - 1) mod 2^32`. Going from 4294967295 to 0 is normal.
   - A late packet that fills a gap gets delivered and comes off the lost count. It may have been reordered by the network, or be a BACKFILL packet from the device's ring buffer.
   - A repeat (same seq and same `base_ms`) gets dropped.
   - If device time goes back by more than 2 s on a packet without the BACKFILL flag, the device rebooted, and its seq tracking and timebase start again.
4. Decoding uses the registry version in the packet header. If that version isn't in the store, the DATA packet is dropped and counted; keep the version's snapshot in the archive to decode it. A record whose channel id isn't in its version is skipped and counted.
5. Each value carries `device_ms` (ms since boot) and `wall_time` (Unix seconds). The timebase estimates the clock offset from the packet that arrived with the smallest delay, so wall times run late by about the network's smallest delay. The estimate can rise by at most 1000 ppm. That is enough to follow a slow device clock, but one slow packet can't drag it up.

## Using it from your own code

With `tools/receiver/` and `messaging/` on the Python path:

```python
from receiver import Receiver
from registry_store import RegistryStore

rx = Receiver(RegistryStore.load(), on_value=lambda v: print(v.name, v.value, v.unit, v.wall_time))
rx.feed(packet_bytes)          # call this for every packet your transport delivers
print(rx.stats, rx.corrupt)    # counts: packets, delivered, lost, late, duplicates, reboots, ...
```

The callbacks `on_value(Value)`, `on_raw_can(RawFrame)` and `on_event(kind, device_id, detail)` are all optional. A team that wants its own receiving end, such as a dashboard or storage, can start from `on_value`; building that part is up to each team.

## Limits

- One registry store serves every device, so all devices on one receiver must share a registry.
- The receiver remembers the last 4096 packets from each device (`track`). A repeat older than that can't be recognised and gets delivered again.
- A reboot only shows up when device time jumps back by more than 2 s (`reboot_ms`), so a device that reboots within a few seconds of booting can be missed.
- `replay.py` (reading SD log files back) comes later, once the SD log format exists.

## Tests

The tests are in `tests/tools/receiver/`. Run them with `uv run pytest tests/tools/receiver`.
