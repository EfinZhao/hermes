# tools

Command-line scripts for building and checking the messaging contract in `messaging/`, and for validating what the firmware sends: a fake device and a receiver.
The tools import `messaging/msgcodec.py` and nothing from `firmware/`. Run them from the repo root with `uv run`.

| Tool | What it does | Writes |
|---|---|---|
| `import_can_csv.py` | Adds CAN signals from DBC files to the registry as channels | `messaging/registry.yaml` |
| `gen_registry.py` | Checks the registry's append-only rules, writes the firmware header and a snapshot | `firmware/main/registry_gen.h`, `messaging/registry_archive/registry_vN.json` |
| `make_golden.py` | Writes the golden packets, the byte-exact answer key for every implementation | `messaging/golden/*.json` |
| `fake_car.py` | Pretends to be the device and sends DATA packets over UDP | no files; it only sends UDP |
| `receiver/` | Receives a device's packets over UDP, checks them (CRC, gaps, repeats, reboots) and decodes the values | no files; it prints values, events and a summary |

## Typical workflow

1. Add a device or signal: edit `import_can_config.yaml`, run `import_can_csv.py`.
2. Edit `messaging/registry.yaml` by hand if needed (real `rate_hz`, `priority`, `live`, `status`).
3. Run `gen_registry.py --bump`, then commit `registry.yaml` and `registry_gen.h` together.
4. After any change to `msgcodec.py`, run `make_golden.py` and commit the golden files with the change.
5. Run `receiver/receiver.py` on the laptop to check what the device sends. With no hardware, feed it from `fake_car.py`.

`gen_registry.py --check` and `make_golden.py --check` change nothing and fail if the committed files are stale, so they can run in CI.

---

## import_can_csv.py

Creates or extends `messaging/registry.yaml` from CAN definition files (DBCs).
Which devices, signals, ids and names to use is set in `import_can_config.yaml` (see the comments at the top of that file); nothing car-specific lives in the script.

- No registry (or an empty one): starts from `messaging/registry_base.yaml`, then adds channels.
- Registry exists: adds only signals it does not have yet. Existing channels are never changed.
- Only signals with a unit are imported, unless listed in `keep_unitless`; switches and status codes are skipped.
- Signals listed in `active` are imported as `status: active`, all others as `planned`.
- Each device gets its own block of channel ids starting at `first_id`. Units whose CAN ids depend on a setting (E888 base id) use `base_id` + `offsets`.
- The DBCs are read rather than the CSVs in `messaging/data/`, because the CSVs lack multiplexing information.
- `rate_hz` and `priority` are placeholders; set the real values in `registry.yaml`.

```
uv run python tools/import_can_csv.py [--dry-run] [--registry PATH] [--base PATH] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Print how many channels would be added; change nothing |
| `--registry` | `messaging/registry.yaml` | Registry to create or extend |
| `--base` | `messaging/registry_base.yaml` | Starting skeleton when there is no registry |
| `--config` | `tools/import_can_config.yaml` | Devices, ids, names and active set |

After importing, run `gen_registry.py --bump`.

## gen_registry.py

Checks `registry.yaml` against its past versions, then writes the firmware header and a snapshot.

1. It checks the append-only rules against every snapshot in `messaging/registry_archive/`:
   - A channel that has ever been active can't be deleted, and its id can't change meaning (unit, source, src).
   - A deprecated channel stays deprecated.
   - Params can't be deleted.
   - `registry_version` goes up by exactly 1 when anything changed, and stays the same when nothing did.

   Planned channels can change freely until they are first made active.
2. It writes a snapshot, `messaging/registry_archive/registry_vN.json`. The archive is local and git-ignored. Each team keeps its own, and the rules only compare against the snapshots in that folder, so with an empty archive there is nothing to check.
3. It writes `firmware/main/registry_gen.h` with the active channels, params and CAN signals. Don't edit this header by hand.

```
uv run python tools/gen_registry.py [--check] [--bump] [--registry PATH] [--archive DIR] [--header PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--bump` | off | Raise `registry_version` in `registry.yaml` by 1 if the registry changed (keeps YAML comments) |
| `--check` | off | Change nothing; fail if a rule is broken or the header is missing or out of date. Needs no snapshot, so it works on a fresh clone |
| `--registry` | `messaging/registry.yaml` | Registry to check |
| `--archive` | `messaging/registry_archive/` | Folder of past snapshots |
| `--header` | `firmware/main/registry_gen.h` | Header to write |

## make_golden.py

Writes the golden packet files in `messaging/golden/`: exact packet bytes (as hex) and the values they must decode to, for every message type, plus CRC-32 test vectors and packets a decoder must reject.
Firmware and receivers are tested against these files and must match them byte for byte.
The script builds every packet with `msgcodec.py`, so the Python reference and the answer key can't disagree. Running it twice gives identical files.
The file format is described in `messaging/golden/README.md`.

```
uv run python tools/make_golden.py [--check] [--out DIR]
```

| Flag | Default | Meaning |
|---|---|---|
| `--check` | off | Change nothing; fail if the committed files are out of date |
| `--out` | `messaging/golden/` | Output folder |

## fake_car.py

Pretends to be the device, so you can test the receiver without a car, a CAN bus or an ESP32.
It follows the same rules as the real LIVE stream:

- It samples every channel that is `status: active` and `live: true` at its registry `rate_hz`. Priority 2 channels send 1 of every `live_decimate` samples.
- It collects samples for one batch window (`batch_window_ms`, 100 ms by default) and turns each window into packets with `msgcodec.Packetizer`. `seq` goes up by 1 per packet, and `base_ms` is the time of the packet's first sample in ms since "boot".
- Values follow a waveform picked by unit (rpm, C, V, %, kPa, km/h). Other units swing between 20% and 80% of the channel's range. A little noise is added, and the wave stays 3% inside the registry min/max so normal data never gets flagged CLAMPED. None of this is real car data.
- Synthetic channels are exact: `counter` sends 0, 1, 2, ... and `sine` sends a clean sine wave.
- Optional network faults (loss, duplicates, reordering) let you test how a receiver handles gaps and duplicates.
- The same `--seed` always gives the same run.

```
uv run python tools/fake_car.py [--host H] [--port P] [--registry PATH] [--duration S] [--loss F]
                                [--reorder F] [--duplicate F] [--speed X] [--seed N]
                                [--start-seq N] [--boot-ms N] [--batch-ms N]
```

| Flag | Default | Meaning |
|---|---|---|
| `--host`, `--port` | `127.0.0.1`, `5005` | Where to send the UDP packets |
| `--registry` | `messaging/registry.yaml` | Which channels to send |
| `--duration` | `10` | Seconds of device time to send |
| `--loss` | `0` | Chance (0 to 1) a packet is dropped, which leaves a gap in `seq` |
| `--reorder` | `0` | Chance a packet is held back and arrives 1 to 3 packets late |
| `--duplicate` | `0` | Chance a packet is sent twice |
| `--speed` | `1` | 1 = real time, 2 = twice as fast, 0 = as fast as possible |
| `--seed` | `0` | Random seed for values and network faults |
| `--start-seq` | `0` | First sequence number (try `4294967290` to watch the u32 counter wrap) |
| `--boot-ms` | `10000` | Device clock at the first sample |
| `--batch-ms` | registry `batch_window_ms` | Batch window length in ms |

Examples:

```
uv run python tools/fake_car.py --duration 5                                   # clean stream, real time
uv run python tools/fake_car.py --duration 3 --loss 0.2 --duplicate 0.2 --speed 0 --seed 1
uv run python tools/fake_car.py --start-seq 4294967290 --duration 2             # seq wraps to 0
```

At the end it prints a summary, e.g. `built 30 packets holding 240 samples; sent 33 (4 dropped, 7 duplicated)`.
To look at the raw bytes without a receiver, run `nc -u -l 5005 | xxd` in another terminal; each packet starts with `48 4d 01 00 14` (magic, format version 1, DATA, device 20).

Known issue: `--reorder` currently has no visible effect with the default registry. The script applies network faults one batch window at a time, and with the default registry each window holds a single packet, so a held-back packet comes out in its original place.

## receiver/

Validates what a device sends, on the laptop. It checks every packet (corrupt ones are dropped and counted by reason), counts lost, late and repeated packets per device, spots reboots, and decodes the values with the registry version each packet names. Full details, flags and limits are in `receiver/README.md`.

```
uv run python tools/receiver/receiver.py [--host H] [--port P] [--registry PATH] [--archive DIR] [--quiet] [--idle-exit S]
```

Run it next to the fake car to see the whole path with no hardware:

```
uv run python tools/receiver/receiver.py                     # terminal 1
uv run python tools/fake_car.py --duration 10 --loss 0.1     # terminal 2
```

## Tests

Each tool has tests in `tests/tools/` (`test_import_can_csv.py`, `test_gen_registry.py`, `test_fake_car.py`, and `receiver/` for the receiver); `make_golden.py` is covered by `tests/golden/test_golden.py`. Run everything with `uv run pytest`.
