"""Build or extend messaging/registry.yaml from CAN definition files (DBCs).

Usage:  uv run python tools/import_can_csv.py [--dry-run] [--registry PATH] [--base PATH] [--config PATH]

What it does
  * registry.yaml missing or empty: starts from messaging/registry_base.yaml (params, device id,
    your non-CAN channels), then imports the CAN channels of every device in the config.
  * registry.yaml exists: appends only the signals it does not have yet.
  Which signals, their ids and friendly names come from tools/import_can_config.yaml.

The CSVs in messaging/data/ hold the same signals as the DBCs but drop the multiplexer
selector values E888 needs, so the DBCs are the import source.

Rules
  * Only numeric measurements are imported (signals with a unit, plus the config's
    `keep_unitless`). Switches, state enums and version bytes are skipped; they can be
    appended later under new ids.
  * Idempotent and append-only: a signal already in the registry (same CAN address) is skipped,
    existing channels are never touched, ids are never reassigned.
  * New channels are `planned` unless the config marks them active (compiled into the firmware).
  * rate_hz / priority / live are placeholders: the DBC has no cycle times.
  * The text is appended so the comments in registry.yaml survive, and the result is validated
    before anything is written.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import cantools
import yaml

ROOT = Path(__file__).resolve().parent.parent
MSG = ROOT / "messaging"
sys.path.insert(0, str(MSG))
import msgcodec  # noqa: E402

DEFAULT_REGISTRY = MSG / "registry.yaml"
DEFAULT_BASE = MSG / "registry_base.yaml"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "import_can_config.yaml"

UNIT_NAME = {"RPM": "rpm", "C": "c", "%": "pct", "deg": "deg", "kPa": "kpa", "km/h": "kmh",
             "s": "s", "g": "g", "g/s": "gps", "ratio": "ratio", "LA": "lambda",
             "mV": "mv", "V": "v", "Hz": "hz"}
UNIT_OUT = {"RPM": "rpm", "LA": "lambda"}


def snake(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^0-9a-z]+", "_", name.lower())).strip("_")


def auto_name(sig_name: str, unit: str, prefix: str | None) -> str:
    base = snake(sig_name)
    suffix = UNIT_NAME.get(unit, snake(unit)) if unit else ""
    if suffix and not base.endswith("_" + suffix) and base != suffix:
        base += "_" + suffix
    if prefix:
        return f"{prefix}.{base}"
    group, _, rest = base.partition("_")
    return f"{group}.{rest}" if rest else base


def value_range(sig) -> tuple[float, float]:
    lo, hi = sig.minimum, sig.maximum
    if lo is None or hi is None or (lo == 0 and hi == 0):   # DBC range unset: use raw range
        raw_lo, raw_hi = ((-(1 << (sig.length - 1)), (1 << (sig.length - 1)) - 1)
                          if sig.is_signed else (0, (1 << sig.length) - 1))
        lo, hi = raw_lo * sig.scale + sig.offset, raw_hi * sig.scale + sig.offset
    return float(f"{lo:.10g}"), float(f"{hi:.10g}")


def candidates(dev: dict, db):
    """Yield (key, sig, addr, mux) for one device in a stable order. `mux` = (sel_start, sel_len, value)."""
    skip, keep = set(dev.get("skip_messages", [])), set(dev.get("keep_unitless", []))
    if "base_id" not in dev:                                   # fixed ids, as written in the DBC
        for msg in sorted(db.messages, key=lambda m: m.frame_id):
            if msg.frame_id in skip:
                continue
            for sig in msg.signals:
                if sig.unit or sig.name in keep:
                    yield ("id", msg.frame_id, sig.start, sig.length, None), sig, {"can_id": f"0x{msg.frame_id:X}"}, None
        return
    for off in dev["offsets"]:                                 # base + offset
        msg = db.get_message_by_frame_id(dev["base_id"] + off)
        sel = next((s for s in msg.signals if s.is_multiplexer), None)
        for sig in msg.signals:
            if sig.is_multiplexer or not (sig.unit or sig.name in keep):
                continue
            mux = None
            if sig.multiplexer_ids:
                assert sel is not None and sel.byte_order == "big_endian" and not sel.is_signed
                mux = (sel.start, sel.length, sig.multiplexer_ids[0])
            yield ("base", dev["name"], off, sig.start, sig.length, mux), sig, \
                {"can_base": dev["name"], "can_offset": off}, mux


def existing_keys(reg: dict) -> set:
    keys = set()
    for ch in reg["channels"]:
        if ch["source"] != "can":
            continue
        s = ch["src"]
        mux = (s["mux"]["start_bit"], s["mux"]["length"], s["mux"]["value"]) if "mux" in s else None
        if "can_id" in s:
            keys.add(("id", s["can_id"], s["start_bit"], s["length"], mux))
        else:
            keys.add(("base", s["can_base"], s["can_offset"], s["start_bit"], s["length"], mux))
    return keys


def fmt(v: float) -> str:
    return repr(float(v))


def render(chid, name, sig, addr, mux, active, priority=1) -> str:
    lo, hi = value_range(sig)
    unit = UNIT_OUT.get(sig.unit, sig.unit) if sig.unit else "count"
    addr_s = ", ".join(f"{k}: {v}" for k, v in addr.items())
    mux_s = ""
    if mux:
        mux_s = f", mux: {{ start_bit: {mux[0]}, length: {mux[1]}, value: {mux[2]} }}"
    src = (f"{{ {addr_s}, bus: 0, start_bit: {sig.start}, length: {sig.length}, "
           f"byte_order: {sig.byte_order}, signed: {str(sig.is_signed).lower()}, "
           f"scale: {fmt(sig.scale)}, offset: {fmt(sig.offset)}{mux_s} }}")
    return (f"  - id: {chid}\n    name: {name}\n    unit: \"{unit}\"\n    source: can\n"
            f"    rate_hz: 10              # placeholder: DBC has no cycle time\n"
            f"    priority: {priority}\n    live: {str(active).lower()}\n    log: true\n"
            f"    min: {fmt(lo)}\n    max: {fmt(hi)}\n"
            f"    status: {'active' if active else 'planned'}\n    src: {src}\n")


def check_devices(devices: list) -> None:
    names = [d["name"] for d in devices]
    if len(names) != len(set(names)):
        raise SystemExit("config: device names must be unique")
    firsts = [d["first_id"] for d in devices]
    if len(firsts) != len(set(firsts)):
        raise SystemExit("config: devices need different first_id values")
    for d in devices:
        if "base_id" in d and "offsets" not in d:
            raise SystemExit(f"config: device {d['name']} has base_id but no offsets")


def add_can_bases(text: str, reg: dict, devices: list) -> str:
    """Make sure registry.yaml's `can_bases` table has an entry for every base+offset device.
    Existing entries win: the registry is where you record your unit's real base id."""
    have = reg.get("can_bases") or {}
    new = [(d["name"], d["base_id"]) for d in devices if "base_id" in d and d["name"] not in have]
    if not new:
        return text
    lines = "".join(f"  {n}: 0x{b:X}\n" for n, b in new)
    if "can_bases" in reg:
        return re.sub(r"^(can_bases:.*\n)", lambda m: m.group(1) + lines, text, count=1, flags=re.M)
    return re.sub(r"^(device_id: .*\n)",
                  lambda m: m.group(1) + "can_bases:            # CAN base ids of devices addressed by offset (src.can_base). "
                  "Match your unit's configuration.\n" + lines, text, count=1, flags=re.M)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--base", type=Path, default=DEFAULT_BASE)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = ap.parse_args(argv)

    dbs: dict = {}
    cfg = yaml.safe_load(args.config.read_text())
    devices = cfg["devices"]
    check_devices(devices)

    # start from the existing registry, or from the base template when there is none
    text = args.registry.read_text() if args.registry.exists() else ""
    bootstrap = not text.strip()
    if bootstrap:
        text = args.base.read_text()
        print(f"{args.registry} missing or empty: starting from {args.base.name}")
    reg = msgcodec.validate_registry(yaml.safe_load(text), str(args.registry))

    firsts = sorted(d["first_id"] for d in devices)
    have = existing_keys(reg)
    names = {c["name"] for c in reg["channels"]}
    ids = {c["id"] for c in reg["channels"]}
    out = []
    for dev in devices:
        db = dbs.setdefault(dev["dbc"], cantools.database.load_file(str(ROOT / dev["dbc"])))
        pinned, active, friendly = dev.get("pinned", {}), set(dev.get("active", [])), dev.get("names", {})
        lo = dev["first_id"]
        hi = next((f for f in firsts if f > lo), 1 << 30)       # this device's id block is [lo, hi)
        for sig_name, p in pinned.items():
            if p["id"] >= min(firsts):
                raise SystemExit(f"{dev['name']}: pinned id {p['id']} ({sig_name}) must be below the lowest first_id {min(firsts)}")
        next_id = max([i for i in ids if lo <= i < hi], default=lo - 1) + 1
        seen_pinned = set()
        for key, sig, addr, mux in candidates(dev, db):
            pin = pinned.get(sig.name)
            if pin:
                seen_pinned.add(sig.name)
            if key in have:          # already in the registry (possibly under a pinned id)
                continue
            if pin:
                chid, name, is_active, priority = pin["id"], pin["name"], pin.get("active", False), pin.get("priority", 1)
                if chid in ids:
                    raise SystemExit(f"pinned id {chid} ({sig.name}) is already used by another channel")
            else:
                chid, next_id = next_id, next_id + 1
                if chid >= hi:
                    raise SystemExit(f"{dev['name']}: id block [{lo}, {hi}) is full, raise the next device's first_id")
                name = friendly.get(sig.name) or auto_name(sig.name, sig.unit, dev.get("prefix"))
                is_active, priority = sig.name in active, 1
            if name in names:
                raise SystemExit(f"name collision: {name} ({dev['name']}.{sig.name}); set a distinct prefix or names entry")
            names.add(name)
            ids.add(chid)
            out.append(render(chid, name, sig, addr, mux, is_active, priority))
        if (missing := set(pinned) - seen_pinned):
            raise SystemExit(f"{dev['name']}: pinned signals not found in {dev['dbc']}: {sorted(missing)}")

    print(f"{len(out)} channels to add")
    if args.dry_run or not out:
        return 0

    text = re.sub(r"^registry_version: \d+", f"registry_version: {reg['registry_version'] + 1}", text,
                  count=1, flags=re.M)
    text = add_can_bases(text, reg, devices)
    text = text.rstrip("\n") + "\n\n  # --- imported from the DBCs by tools/import_can_csv.py ---\n" + "\n".join(out)
    msgcodec.validate_registry(yaml.safe_load(text), "result")      # never write an invalid registry
    tmp = args.registry.with_suffix(".yaml.tmp")                    # write then rename: no half-written file
    tmp.write_text(text)
    tmp.replace(args.registry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
