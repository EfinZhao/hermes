"""Build or extend messaging/registry.yaml from CAN definition files (DBCs).

Usage:  uv run python tools/import_can_csv.py [--dry-run] [--registry PATH] [--base PATH] [--config PATH]

What it does
  * registry.yaml missing or empty: starts from messaging/registry_base.yaml (params, device id,
    your non-CAN channels), then imports the CAN channels.
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


def auto_name(sig_name: str, unit: str, e888: bool) -> str:
    base = snake(sig_name)
    suffix = UNIT_NAME.get(unit, snake(unit)) if unit else ""
    if suffix and not base.endswith("_" + suffix) and base != suffix:
        base += "_" + suffix
    if e888:
        return "e888." + base
    group, _, rest = base.partition("_")
    return f"{group}.{rest}" if rest else base


def value_range(sig) -> tuple[float, float]:
    lo, hi = sig.minimum, sig.maximum
    if lo is None or hi is None or (lo == 0 and hi == 0):   # DBC range unset: use raw range
        raw_lo, raw_hi = ((-(1 << (sig.length - 1)), (1 << (sig.length - 1)) - 1)
                          if sig.is_signed else (0, (1 << sig.length) - 1))
        lo, hi = raw_lo * sig.scale + sig.offset, raw_hi * sig.scale + sig.offset
    return float(f"{lo:.10g}"), float(f"{hi:.10g}")


def candidates(cfg: dict, m130, e888):
    """Yield (key, sig, addr, is_e888, mux) in a stable order. `mux` = (sel_start, sel_len, value)."""
    skip, keep = set(cfg["skip_messages"]), set(cfg["keep_unitless"])
    for msg in sorted(m130.messages, key=lambda m: m.frame_id):
        if msg.frame_id in skip:
            continue
        for sig in msg.signals:
            if sig.unit or sig.name in keep:
                yield ("id", msg.frame_id, sig.start, sig.length, None), sig, \
                    {"can_id": f"0x{msg.frame_id:X}"}, False, None
    for off in cfg["e888_offsets"]:
        msg = e888.get_message_by_frame_id(cfg["e888_base_id"] + off)
        sel = next((s for s in msg.signals if s.is_multiplexer), None)
        for sig in msg.signals:
            if sig.is_multiplexer or not sig.unit:
                continue
            mux = None
            if sig.multiplexer_ids:
                assert sel is not None and sel.byte_order == "big_endian" and not sel.is_signed
                mux = (sel.start, sel.length, sig.multiplexer_ids[0])
            yield ("off", off, sig.start, sig.length, mux), sig, {"can_offset": off}, True, mux


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
            keys.add(("off", s["can_offset"], s["start_bit"], s["length"], mux))
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--base", type=Path, default=DEFAULT_BASE)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text())
    m130 = cantools.database.load_file(str(ROOT / cfg["dbc"]["m130"]))
    e888 = cantools.database.load_file(str(ROOT / cfg["dbc"]["e888"]))

    # start from the existing registry, or from the base template when there is none
    text = args.registry.read_text() if args.registry.exists() else ""
    bootstrap = not text.strip()
    if bootstrap:
        text = args.base.read_text()
        print(f"{args.registry} missing or empty: starting from {args.base.name}")
    reg = msgcodec.validate_registry(yaml.safe_load(text), str(args.registry))

    pinned, active = cfg["pinned"], set(cfg["active"])
    first_m130, first_e888 = cfg["first_id"]["m130"], cfg["first_id"]["e888"]
    for sig_name, p in pinned.items():
        if p["id"] >= min(first_m130, first_e888):
            raise SystemExit(f"pinned id {p['id']} ({sig_name}) must be below first_id {min(first_m130, first_e888)}")

    have = existing_keys(reg)
    names = {c["name"] for c in reg["channels"]}
    ids = {c["id"] for c in reg["channels"]}
    next_id = {False: max([i for i in ids if first_m130 <= i < first_e888], default=first_m130 - 1) + 1,
               True: max([i for i in ids if i >= first_e888], default=first_e888 - 1) + 1}

    out, seen_pinned = [], set()
    for key, sig, addr, is_e888, mux in candidates(cfg, m130, e888):
        pin = None if is_e888 else pinned.get(sig.name)
        if pin:
            seen_pinned.add(sig.name)
        if key in have:          # already in the registry (possibly under a pinned id)
            continue
        if pin:
            chid, name, is_active, priority = pin["id"], pin["name"], pin.get("active", False), pin.get("priority", 1)
            if chid in ids:
                raise SystemExit(f"pinned id {chid} ({sig.name}) is already used by another channel")
        else:
            chid, next_id[is_e888] = next_id[is_e888], next_id[is_e888] + 1
            name = cfg["names"].get(sig.name) or auto_name(sig.name, sig.unit, is_e888)
            is_active, priority = sig.name in active, 1
        if name in names:
            raise SystemExit(f"name collision: {name} ({sig.name})")
        names.add(name)
        ids.add(chid)
        out.append(render(chid, name, sig, addr, mux, is_active, priority))
    if (missing := set(pinned) - seen_pinned):
        raise SystemExit(f"pinned signals not found in the M130 DBC: {sorted(missing)}")

    print(f"{len(out)} channels to add")
    if args.dry_run or not out:
        return 0

    text = re.sub(r"^registry_version: \d+", f"registry_version: {reg['registry_version'] + 1}", text,
                  count=1, flags=re.M)
    if "e888_base_id" not in text:
        text = re.sub(r"^(device_id: .*)$",
                      rf"\1\ne888_base_id: 0x{cfg['e888_base_id']:X}   # E888 CAN base id; one of 0xF0/F4/F8/FC, match your unit's configuration",
                      text, count=1, flags=re.M)
    text = text.rstrip("\n") + "\n\n  # --- imported from the DBCs by tools/import_can_csv.py ---\n" + "\n".join(out)
    msgcodec.validate_registry(yaml.safe_load(text), "result")      # never write an invalid registry
    tmp = args.registry.with_suffix(".yaml.tmp")                    # write then rename: no half-written file
    tmp.write_text(text)
    tmp.replace(args.registry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
