"""Check registry.yaml against its past versions, then write the firmware header and a snapshot.

Usage: uv run python tools/gen_registry.py [--check] [--bump] [--registry P] [--archive D] [--header P]

  1. Checks the append-only rules against every snapshot in messaging/registry_archive/:
     once a channel has been active it may not be deleted and its id may not change meaning
     (unit, source, src); a deprecated channel stays deprecated; params may not be deleted; and
     registry_version must go up by exactly 1 when anything changed (and stay the same when
     nothing did). Planned channels are free to change until they are first active.
  2. Writes messaging/registry_archive/registry_vN.json, a snapshot of this version. The archive
     is local (git-ignored): each team keeps its own, and the rules only compare against the
     snapshots found there. With no snapshots the rules have nothing to check.
  3. Writes firmware/main/registry_gen.h with the `active` channels, params and CAN signals.

  --bump   raise registry_version in registry.yaml by 1 if the registry changed
  --check  change nothing; fail if a rule is broken or the header is missing/out of date (for CI)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MSG = ROOT / "messaging"
sys.path.insert(0, str(MSG))
import msgcodec  # noqa: E402

DEFAULT_REGISTRY = MSG / "registry.yaml"
DEFAULT_ARCHIVE = MSG / "registry_archive"
DEFAULT_HEADER = ROOT / "firmware" / "main" / "registry_gen.h"

# What a channel/param IS. Changing one of these under an existing id breaks old logs, so it
# needs a new id. Everything else (name, rate, priority, live, log, min, max, ...) may change.
CHANNEL_MEANING = ("unit", "source", "src")
PARAM_MEANING = ("unit",)


# ---------------------------------------------------------------- archive
def snapshot_text(reg: dict) -> str:
    return json.dumps(reg, indent=2, sort_keys=True) + "\n"


def load_snapshots(archive: Path) -> dict[int, dict]:
    snaps = {}
    for f in sorted(archive.glob("registry_v*.json")) if archive.exists() else []:
        m = re.fullmatch(r"registry_v(\d+)\.json", f.name)
        if m:
            snaps[int(m.group(1))] = json.loads(f.read_text())
    return snaps


def same_content(a: dict, b: dict) -> bool:
    """True if two registries are the same apart from their version number."""
    def plain(reg):
        return {k: v for k, v in json.loads(json.dumps(reg)).items() if k != "registry_version"}
    return plain(a) == plain(b)


# ---------------------------------------------------------------- rules
def check_rules(reg: dict, snaps: dict[int, dict]) -> list[str]:
    errors, seen = [], set()

    def err(key, msg):
        if key not in seen:
            seen.add(key)
            errors.append(msg)

    channels = {c["id"]: c for c in reg["channels"]}
    params = {p["id"]: p for p in reg["params"]}
    for v in sorted(snaps, reverse=True):                # newest first: report against the latest version
        old = snaps[v]
        for oc in old["channels"]:
            cid, cur = oc["id"], channels.get(oc["id"])
            if oc["status"] == "planned":        # never shipped: free to change or delete until first active
                continue
            if cur is None:
                err(("del", cid), f"channel {cid} ({oc['name']}) was deleted; set `status: deprecated` instead (v{v} has it)")
                continue
            for field in CHANNEL_MEANING:
                if oc.get(field) != cur.get(field):
                    err((cid, field), f"channel {cid} ({cur['name']}): `{field}` changed from {oc.get(field)!r} to "
                        f"{cur.get(field)!r} (v{v}); give the new meaning a NEW id and deprecate this one")
            if oc["status"] == "deprecated" and cur["status"] != "deprecated":
                err(("dep", cid), f"channel {cid} ({cur['name']}) is deprecated in v{v} and cannot be re-enabled; use a new id")
        for op in old["params"]:
            pid, cur = op["id"], params.get(op["id"])
            if cur is None:
                err(("pdel", pid), f"param {pid} ({op['name']}) was deleted (v{v} has it)")
                continue
            for field in PARAM_MEANING:
                if op.get(field) != cur.get(field):
                    err((("p", pid), field), f"param {pid} ({cur['name']}): `{field}` changed from "
                        f"{op.get(field)!r} to {cur.get(field)!r} (v{v}); use a NEW param id")

    version = reg["registry_version"]
    if snaps:
        latest = max(snaps)
        changed = not same_content(reg, snaps[latest])
        if version < latest:
            errors.append(f"registry_version {version} is older than archived v{latest}")
        elif version == latest and changed:
            errors.append(f"registry changed but registry_version is still {version}: set it to {version + 1} "
                          f"(or run with --bump)")
        elif version > latest and version != latest + 1:
            errors.append(f"registry_version jumps from {latest} to {version}; it must go up by exactly 1")
        elif version > latest and not changed:
            errors.append(f"registry_version was raised to {version} but nothing changed since v{latest}")
    return errors


# ---------------------------------------------------------------- C++ header
# Each struct: (one-line description, [(C++ type, field name), ...]). The field order is the column
# order of every table row, and the description line printed above the struct shows it.
STRUCTS = {
    "Param": ("a setting the receiver can change at runtime with a CONFIG message.",
              [("uint16_t", "id"), ("float", "default_value"), ("float", "min"), ("float", "max")]),
    "Channel": ("how one measurement is sent. priority: 0 critical, 1 normal, 2 low (only 1 of every live_decimate "
                "samples goes live).",
                [("uint16_t", "id"), ("Source", "source"), ("uint8_t", "priority"), ("bool", "live"), ("bool", "log"),
                 ("uint16_t", "live_decimate"), ("float", "rate_hz"), ("float", "min"), ("float", "max")]),
    "CanSignal": ("where a CAN channel sits in a frame: value = raw * scale + offset (double, as in the Python codec).",
                  [("uint16_t", "channel_id"), ("uint32_t", "frame_id"), ("bool", "extended"), ("uint8_t", "bus"),
                   ("uint8_t", "start_bit"), ("uint8_t", "length"), ("bool", "big_endian"), ("bool", "is_signed"),
                   ("double", "scale"), ("double", "offset"), ("bool", "has_mux"), ("uint8_t", "mux_start_bit"),
                   ("uint8_t", "mux_length"), ("uint8_t", "mux_value")]),
}


def struct_lines(name: str) -> list[str]:
    desc, fields = STRUCTS[name]
    row = ", ".join(f for _, f in fields)
    return [f"// {name}: {desc} Row: {{{row}}}", f"struct {name} {{"] + [f"  {t} {f};" for t, f in fields] + ["};", ""]


def cpp_float(v: float) -> str:          # float32 literal
    return repr(float(v)) + "f"


def cpp_double(v: float) -> str:
    return repr(float(v))


def cpp_bool(v: bool) -> str:
    return "true" if v else "false"


def ident(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", name)


def render_header(reg: dict) -> str:
    active = sorted((c for c in reg["channels"] if c["status"] == "active"), key=lambda c: c["id"])
    can = sorted((c for c in active if c["source"] == "can"),
                 key=lambda c: (msgcodec.can_frame_id(c, reg), c["id"]))
    out = [
        "// AUTO-GENERATED by tools/gen_registry.py from messaging/registry.yaml. Do not edit by hand.",
        "// Only channels with `status: active` are included.",
        "#pragma once",
        "",
        "#include <cstddef>",
        "#include <cstdint>",
        "",
        "namespace registry {",
        "",
        f"constexpr uint8_t kDeviceId = {reg['device_id']};",
        f"constexpr uint16_t kRegistryVersion = {reg['registry_version']};",
        "",
    ]
    for name, base in sorted((reg.get("can_bases") or {}).items()):
        out.append(f"constexpr uint32_t kCanBase_{ident(name)} = 0x{base:X};")
    out += [
        "",
        "enum class Source : uint8_t { kCan, kAdc, kI2c, kSynthetic };",
        "",
        *struct_lines("Param"),
        *struct_lines("Channel"),
        *struct_lines("CanSignal"),
        f"constexpr size_t kNumParams = {len(reg['params'])};",
        f"constexpr size_t kNumChannels = {len(active)};",
        f"constexpr size_t kNumCanSignals = {len(can)};",
        "",
        f"inline constexpr Param kParams[{max(len(reg['params']), 1)}] = {{",
    ]
    for p in sorted(reg["params"], key=lambda p: p["id"]):
        out.append(f"    {{{p['id']}, {cpp_float(p['default'])}, {cpp_float(p['min'])}, {cpp_float(p['max'])}}},  // {p['name']}")
    out += ["};", "", f"inline constexpr Channel kChannels[{max(len(active), 1)}] = {{"]
    for c in active:
        src = {"can": "kCan", "adc": "kAdc", "i2c": "kI2c", "synthetic": "kSynthetic"}[c["source"]]
        out.append(f"    {{{c['id']}, Source::{src}, {c['priority']}, {cpp_bool(c['live'])}, {cpp_bool(c['log'])}, "
                   f"{c.get('live_decimate', 1)}, {cpp_float(c['rate_hz'])}, {cpp_float(c['min'])}, {cpp_float(c['max'])}}},"
                   f"  // {c['name']}")
    out += ["};", "", f"inline constexpr CanSignal kCanSignals[{max(len(can), 1)}] = {{"]
    for c in can:
        s, fid = c["src"], msgcodec.can_frame_id(c, reg)
        mux = s.get("mux")
        mux_fields = (f"true, {mux['start_bit']}, {mux['length']}, {mux['value']}" if mux else "false, 0, 0, 0")
        out.append(f"    {{{c['id']}, 0x{fid:X}, {cpp_bool(s.get('extended', False))}, {s['bus']}, {s['start_bit']}, {s['length']}, "
                   f"{cpp_bool(s['byte_order'] == 'big_endian')}, {cpp_bool(s['signed'])}, "
                   f"{cpp_double(s['scale'])}, {cpp_double(s['offset'])}, {mux_fields}}},  // {c['name']}")
    out += ["};", "", "}  // namespace registry", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- main
def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--bump", action="store_true")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    ap.add_argument("--header", type=Path, default=DEFAULT_HEADER)
    args = ap.parse_args(argv)
    if args.check and args.bump:
        ap.error("--check changes nothing, so it cannot be combined with --bump")

    text = args.registry.read_text()
    reg = msgcodec.validate_registry(yaml.safe_load(text), str(args.registry))
    snaps = load_snapshots(args.archive)
    if not snaps:
        print(f"note: no snapshots in {args.archive}; the append-only rules had nothing to compare against")

    if args.bump and snaps:
        latest = max(snaps)
        if reg["registry_version"] == latest and not same_content(reg, snaps[latest]):
            text = re.sub(r"^registry_version: \d+", f"registry_version: {latest + 1}", text, count=1, flags=re.M)
            write_atomic(args.registry, text)
            reg = msgcodec.validate_registry(yaml.safe_load(text), str(args.registry))
            print(f"registry_version raised to {latest + 1}")

    errors = check_rules(reg, snaps)
    snap_path = args.archive / f"registry_v{reg['registry_version']}.json"
    want_header = render_header(reg)
    if args.check and not errors:
        if not args.header.exists() or args.header.read_text() != want_header:
            errors.append(f"{args.header} is missing or out of date; run tools/gen_registry.py")
    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        return 1
    if args.check:
        print("registry OK")
        return 0

    if not snap_path.exists():
        write_atomic(snap_path, snapshot_text(reg))
        print(f"wrote {snap_path}")
    write_atomic(args.header, want_header)
    print(f"wrote {args.header}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
