"""Append CAN channels from the M130 / E888 DBCs to messaging/registry.yaml.

Usage:  uv run python tools/import_can_csv.py [--dry-run]

The CSVs in messaging/data/ hold the same signals as the DBCs but drop the multiplexer
selector values E888 needs, so the DBCs are the import source.

Rules
  * Only numeric measurements are imported (signals with a unit, plus a few named
    exceptions). Switches, state enums, version bytes and PDM mirror messages are skipped;
    they can be appended later under new ids.
  * Idempotent: a signal already in the registry (same CAN address) is skipped, existing
    channels are never touched, ids are never reassigned.
  * New channels are `planned` unless listed in ACTIVE_CHANNELS. M130 ids start at 300, E888 at 1000.
  * rate_hz / priority / live are placeholders: the DBC has no cycle times.
  * The text is appended so the comments in registry.yaml survive.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import cantools

ROOT = Path(__file__).resolve().parent.parent
MSG = ROOT / "messaging"
REGISTRY = MSG / "registry.yaml"
M130_DBC = MSG / "dbc" / "M1_General_0x640_0x650_0x670_Ver5.dbc"
E888_DBC = MSG / "dbc" / "E8xx.dbc"

E888_BASE_ID = 0xF0
E888_OFFSETS = (0, 1, 3)              # inputs, status, PWM outputs
M130_FIRST_ID, E888_FIRST_ID = 300, 1000
PDM_MIRRORS = {0x118, 0x119, 0x11A}   # lower-resolution copies of M130 data
KEEP_UNITLESS = {"Lap_Number"}

# signals imported as `active` (enabled) rather than `planned`, with their hand-picked names.
# Edit this set for your own setup.
ACTIVE_CHANNELS = {
    "Throttle_Position": "throttle.position_pct",
    "Inlet_Manifold_Pressure": "intake.manifold_kpa",
    "Wheel_Speed_Front_Left": "wheel.speed_fl_kmh",
}
# hand-picked names where the generated one is awkward or inconsistent
NAME_OVERRIDES = {
    "Wheel_Speed_Front_Right": "wheel.speed_fr_kmh",
    "Wheel_Speed_Rear_Left": "wheel.speed_rl_kmh",
    "Wheel_Speed_Rear_Right": "wheel.speed_rr_kmh",
    "minus_5V": "e888.supply_minus_5v_v",
    "_4_5v": "e888.supply_4v5_v",
    "_8vAux": "e888.aux_8v_v",
    "_5vAux": "e888.aux_5v_v",
}
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


def candidates(m130, e888):
    """Yield (key, name, sig, can_src_fields, is_e888) in a stable order."""
    for msg in sorted(m130.messages, key=lambda m: m.frame_id):
        if msg.frame_id in PDM_MIRRORS:
            continue
        for sig in msg.signals:
            if sig.unit or sig.name in KEEP_UNITLESS:
                key = ("id", msg.frame_id, sig.start, sig.length, None)
                src = {"can_id": f"0x{msg.frame_id:X}"}
                yield key, sig, src, False
    for off in E888_OFFSETS:
        msg = e888.get_message_by_frame_id(E888_BASE_ID + off)
        sel = next((s for s in msg.signals if s.is_multiplexer), None)
        for sig in msg.signals:
            if sig.is_multiplexer or not sig.unit:
                continue
            mux = None
            if sig.multiplexer_ids:
                assert sel is not None and sel.byte_order == "big_endian" and not sel.is_signed
                mux = (sel.start, sel.length, sig.multiplexer_ids[0])
            key = ("off", off, sig.start, sig.length, mux)
            yield key, sig, {"can_offset": off}, True


def existing_keys(reg: dict) -> set:
    keys = set()
    for ch in reg["channels"]:
        s = ch["src"] if ch["source"] == "can" else None
        if not s:
            continue
        mux = (s["mux"]["start_bit"], s["mux"]["length"], s["mux"]["value"]) if "mux" in s else None
        if "can_id" in s:
            keys.add(("id", s["can_id"], s["start_bit"], s["length"], mux))
        else:
            keys.add(("off", s["can_offset"], s["start_bit"], s["length"], mux))
    return keys


def fmt(v: float) -> str:
    return repr(float(v))


def render(chid, name, sig, addr, e888, active) -> str:
    lo, hi = value_range(sig)
    unit = UNIT_OUT.get(sig.unit, sig.unit) if sig.unit else "count"
    addr_s = ", ".join(f"{k}: {v}" for k, v in addr.items())
    mux_s = ""
    if sig.multiplexer_ids:
        sel_start, sel_len = MUX_SELECTORS[(addr["can_offset"])]
        mux_s = f", mux: {{ start_bit: {sel_start}, length: {sel_len}, value: {sig.multiplexer_ids[0]} }}"
    order = sig.byte_order
    src = (f"{{ {addr_s}, bus: 0, start_bit: {sig.start}, length: {sig.length}, "
           f"byte_order: {order}, signed: {str(sig.is_signed).lower()}, "
           f"scale: {fmt(sig.scale)}, offset: {fmt(sig.offset)}{mux_s} }}")
    return (f"  - id: {chid}\n    name: {name}\n    unit: \"{unit}\"\n    source: can\n"
            f"    rate_hz: 10              # placeholder: DBC has no cycle time\n"
            f"    priority: 1\n    live: {str(active).lower()}\n    log: true\n"
            f"    min: {fmt(lo)}\n    max: {fmt(hi)}\n"
            f"    status: {'active' if active else 'planned'}\n    src: {src}\n")


MUX_SELECTORS: dict[int, tuple[int, int]] = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(MSG))
    import msgcodec
    reg = msgcodec.load_registry(str(REGISTRY))
    m130 = cantools.database.load_file(str(M130_DBC))
    e888 = cantools.database.load_file(str(E888_DBC))
    for off in E888_OFFSETS:
        sel = next(s for s in e888.get_message_by_frame_id(E888_BASE_ID + off).signals if s.is_multiplexer)
        MUX_SELECTORS[off] = (sel.start, sel.length)

    have = existing_keys(reg)
    names = {c["name"] for c in reg["channels"]}
    ids = {c["id"] for c in reg["channels"]}
    next_id = {False: max([i for i in ids if M130_FIRST_ID <= i < E888_FIRST_ID], default=M130_FIRST_ID - 1) + 1,
               True: max([i for i in ids if i >= E888_FIRST_ID], default=E888_FIRST_ID - 1) + 1}

    out, added = [], 0
    for key, sig, addr, e888_flag in candidates(m130, e888):
        if key in have:          # e.g. the hand-written channels 100-102
            continue
        active = sig.name in ACTIVE_CHANNELS
        name = ACTIVE_CHANNELS.get(sig.name) or NAME_OVERRIDES.get(sig.name) or auto_name(sig.name, sig.unit, e888_flag)
        if name in names:
            raise SystemExit(f"name collision: {name} ({sig.name})")
        chid = next_id[e888_flag]
        next_id[e888_flag] += 1
        names.add(name)
        out.append(render(chid, name, sig, addr, e888_flag, active))
        added += 1
    print(f"{added} channels to add")
    if args.dry_run or not added:
        return 0

    text = REGISTRY.read_text()
    text = re.sub(r"^registry_version: \d+", f"registry_version: {reg['registry_version'] + 1}", text, count=1, flags=re.M)
    if "e888_base_id" not in text:
        text = re.sub(r"^(device_id: .*)$", r"\1\ne888_base_id: 0xF0   # E888 CAN base id; one of 0xF0/F4/F8/FC, match your unit's configuration",
                      text, count=1, flags=re.M)
    text = text.rstrip("\n") + "\n\n  # --- imported from the M130 / E888 DBCs by tools/import_can_csv.py ---\n" + "\n".join(out)
    tmp = REGISTRY.with_suffix(".yaml.tmp")      # write then rename: a crash can't leave a half-written registry
    tmp.write_text(text)
    tmp.replace(REGISTRY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
