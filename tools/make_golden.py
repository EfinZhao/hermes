"""Write the golden packet files in messaging/golden/ (the answer key for every implementation).

Usage: uv run python tools/make_golden.py [--check] [--out DIR]

Each file holds cases: the exact bytes of packets (as hex) and the values they must decode to.
Firmware and receivers must produce and read these bytes exactly; see messaging/golden/README.md.
Everything is built with msgcodec.py, so the Python reference and the answer key cannot disagree.
Running this again gives identical files; --check fails if the committed files are out of date.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "messaging"))
import msgcodec as m  # noqa: E402

DEFAULT_OUT = ROOT / "messaging" / "golden"
DEVICE_ID, REGISTRY_VERSION = 20, 1
TYPE_NAMES = {m.DATA: "DATA", m.CONFIG: "CONFIG", m.ACK: "ACK", m.RAW_CAN: "RAW_CAN"}


# ---------------------------------------------------------------- number formats
def f32_bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def bits_f32(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def hex32(v: int) -> str:
    return f"0x{v:08x}"


def float_fields(x: float) -> dict:
    """The float32 as exact bits, plus a readable value (null for NaN and infinity, which JSON lacks)."""
    f = struct.unpack("<f", struct.pack("<f", x))[0]
    return {"value": f if math.isfinite(f) else None, "value_bits": hex32(f32_bits(f))}


# ---------------------------------------------------------------- records <-> JSON
def record_json(msg_type: int, rec: tuple) -> dict:
    """A decoded record (as returned by msgcodec.decode_packet) as a JSON object."""
    if msg_type == m.DATA:
        channel_id, dt_ms, value, quality = rec
        return {"channel_id": channel_id, "dt_ms": dt_ms, **float_fields(value), "quality": quality}
    if msg_type == m.RAW_CAN:
        dt_ms, can_id, bus, dlc, data = rec
        return {"dt_ms": dt_ms, "can_id": f"0x{can_id:08x}", "extended": bool(can_id & m.CAN_EXTENDED),
                "bus": bus, "dlc": dlc, "data": data.hex()}
    if msg_type == m.CONFIG:
        param_id, value = rec
        return {"param_id": param_id, **float_fields(value)}
    acked_seq, status = rec
    return {"acked_seq": acked_seq, "status": status}


def record_from_json(msg_type: int, j: dict) -> tuple:
    """The inverse: a JSON record as the tuple msgcodec packs (used to build a packet from a case)."""
    if msg_type == m.DATA:
        return j["channel_id"], j["dt_ms"], bits_f32(int(j["value_bits"], 16)), j["quality"]
    if msg_type == m.RAW_CAN:
        return j["dt_ms"], int(j["can_id"], 16), j["bus"], j["dlc"], bytes.fromhex(j["data"])
    if msg_type == m.CONFIG:
        return j["param_id"], bits_f32(int(j["value_bits"], 16))
    return j["acked_seq"], j["status"]


def packet_json(buf: bytes) -> dict:
    p = m.decode_packet(buf)                          # also proves the bytes decode
    return {
        "hex": buf.hex(), "size": len(buf),
        "header": {"msg_type": p.msg_type, "device_id": p.device_id, "flags": p.flags,
                   "registry_version": p.registry_version, "seq": p.seq, "base_ms": p.base_ms,
                   "record_count": len(p.records), "payload_len": len(buf) - m.OVERHEAD},
        "crc32": hex32(int.from_bytes(buf[-4:], "little")),
        "records": [record_json(p.msg_type, r) for r in p.records],
    }


# ---------------------------------------------------------------- case builders
def encode_case(name, description, msg_type, *, seq, base_ms, records, flags=0):
    """One packet built directly from a header and a list of JSON records."""
    packed = [m.RECORD[msg_type].pack(*record_from_json(msg_type, r)) for r in records]
    buf = m.encode_packet(msg_type, packed, device_id=DEVICE_ID, registry_version=REGISTRY_VERSION,
                          seq=seq, base_ms=base_ms, flags=flags)
    case = {"name": name, "description": description, "kind": "encode", "msg_type": TYPE_NAMES[msg_type],
            "params": {"device_id": DEVICE_ID, "registry_version": REGISTRY_VERSION, "seq": seq,
                       "base_ms": base_ms, "flags": flags},
            "records": records, "packets": [packet_json(buf)]}
    assert case["packets"][0]["records"] == records, name           # decoding gives back what went in
    return case


def packetize_case(name, description, msg_type, items, *, start_seq=0, max_packet_bytes=1200, flags=0):
    """Samples fed to the Packetizer (sorted, batched and split into packets)."""
    pk = m.Packetizer(DEVICE_ID, REGISTRY_VERSION, max_packet_bytes)
    pk.seq = start_seq
    if msg_type == m.DATA:
        raw = [(t, ch, bits_f32(int(vb, 16)), q) for t, ch, vb, q in items]
        items_json = [{"t_ms": t, "channel_id": ch, **float_fields(bits_f32(int(vb, 16))), "quality": q}
                      for t, ch, vb, q in items]
    else:
        raw = [(t, int(cid, 16), bus, dlc, bytes.fromhex(data)) for t, cid, bus, dlc, data in items]
        items_json = [{"t_ms": t, "can_id": cid, "bus": bus, "dlc": dlc, "data": data}
                      for t, cid, bus, dlc, data in items]
    bufs = pk.build(msg_type, raw, flags=flags)
    return {"name": name, "description": description, "kind": "packetize", "msg_type": TYPE_NAMES[msg_type],
            "params": {"device_id": DEVICE_ID, "registry_version": REGISTRY_VERSION,
                       "max_packet_bytes": max_packet_bytes, "start_seq": start_seq, "flags": flags},
            "items": items_json, "packets": [packet_json(b) for b in bufs]}


def sample(value: float) -> str:
    return hex32(f32_bits(value))


def data_rec(channel_id, dt_ms, value, quality=0):
    return {"channel_id": channel_id, "dt_ms": dt_ms, **float_fields(value), "quality": quality}


def config_rec(param_id, value):
    return {"param_id": param_id, **float_fields(value)}


# ---------------------------------------------------------------- the files
def data_file():
    float_values = [0.0, -0.0, 1.5, -273.15, 3.4028234663852886e38, 1.1754943508222875e-38,
                    1.401298464324817e-45, math.inf, -math.inf, bits_f32(0x7FC00000)]
    ramp = [(2000 + 7 * i, 1 + i % 5, sample(i * 0.5), 0) for i in range(130)]
    many = [(5000 + i, 1 + i % 3, sample(float(i)), 0) for i in range(300)]
    small = [(100 + i, 1, sample(float(i)), 0) for i in range(10)]
    cases = [
        encode_case("single_record", "The spec example: channel 100 = 8000.0 at 1000 ms.", m.DATA,
                    seq=0, base_ms=1000, records=[data_rec(100, 0, 8000.0)]),
        encode_case("quality_flags", "Every quality bit alone, then all together.", m.DATA, seq=0, base_ms=5000,
                    records=[data_rec(7, dt, 1.0, q) for dt, q in
                             [(0, 0), (1, m.Q_INVALID), (2, m.Q_STALE), (3, m.Q_CLAMPED), (4, m.Q_FAULT), (100, 0x0F)]]),
        encode_case("float_values", "Zero, negative zero, extremes, denormal, infinities and NaN (compare the bits).",
                    m.DATA, seq=3, base_ms=100,
                    records=[data_rec(10 + i, i, v) for i, v in enumerate(float_values)]),
        encode_case("largest_header_values", "Biggest numbers in every field.", m.DATA, seq=0xFFFFFFFF,
                    base_ms=0xFFFFFFFF, flags=m.FLAG_BACKFILL,
                    records=[data_rec(32767, 0xFFFF, -1.0, 0x0F)]),
        encode_case("backfill_flag", "A buffered packet sent late (flag bit 0).", m.DATA, seq=41, base_ms=60000,
                    flags=m.FLAG_BACKFILL, records=[data_rec(1, 0, 1.0), data_rec(1, 100, 2.0)]),
        packetize_case("full_packet", "130 samples fill a 1200-byte packet exactly (1194 bytes).", m.DATA, ramp),
        packetize_case("split_into_three_packets", "300 samples split into 130 + 130 + 40, seq 0, 1, 2.", m.DATA, many),
        packetize_case("seq_wraps_at_32_bits", "Three packets starting at seq 0xFFFFFFFE: 0xFFFFFFFE, 0xFFFFFFFF, 0.",
                       m.DATA, small, start_seq=0xFFFFFFFE, max_packet_bytes=60),
        packetize_case("unsorted_input", "Samples arrive out of order and with equal times; the packet is sorted by "
                       "time and equal times keep their input order.", m.DATA,
                       [(1030, 3, sample(3.0), 0), (1000, 1, sample(1.0), 0), (1020, 2, sample(2.0), 0),
                        (1000, 9, sample(9.0), 0)]),
        packetize_case("small_max_packet_bytes", "max_packet_bytes 60 allows 4 samples per packet: 4 + 4 + 2.",
                       m.DATA, small, max_packet_bytes=60),
        packetize_case("backfill_split", "The backfill flag is set on every packet of a split batch.", m.DATA,
                       small, flags=m.FLAG_BACKFILL, start_seq=500, max_packet_bytes=60),
    ]
    return "data", "DATA packets: measurements from the registry's channels.", cases


def raw_can_file():
    def rec(dt, can_id, bus, dlc, data):
        return {"dt_ms": dt, "can_id": f"0x{can_id:08x}", "extended": bool(can_id & m.CAN_EXTENDED), "bus": bus,
                "dlc": dlc, "data": data.hex().ljust(16, "0")}
    frames = [(1000 + 3 * i, f"0x{0x640 + i % 4:08x}", i % 2, 8, bytes((i + k) & 0xFF for k in range(8)).hex())
              for i in range(100)]
    cases = [
        encode_case("standard_frame", "An 11-bit id with 8 data bytes.", m.RAW_CAN, seq=0, base_ms=1000,
                    records=[rec(0, 0x640, 0, 8, bytes(range(8)))]),
        encode_case("extended_frame_short_data", "A 29-bit id (bit 31 set) with 3 data bytes, zero padded.",
                    m.RAW_CAN, seq=1, base_ms=2000,
                    records=[rec(0, 0x1FFFFFFF | m.CAN_EXTENDED, 1, 3, b"\x01\x02\x03")]),
        encode_case("empty_data", "A frame with no data bytes.", m.RAW_CAN, seq=2, base_ms=3000,
                    records=[rec(0, 0x7FF, 0, 0, b"")]),
        packetize_case("split_into_two_packets", "100 frames split into 73 + 27 at 1200 bytes.", m.RAW_CAN, frames),
    ]
    return "raw_can", "RAW_CAN packets: unprocessed CAN frames.", cases


def config_file():
    cases = [
        encode_case("single_param", "Set param 1 to 50.0.", m.CONFIG, seq=7, base_ms=0,
                    records=[config_rec(1, 50.0)]),
        encode_case("several_params", "Three params in one packet.", m.CONFIG, seq=8, base_ms=0,
                    records=[config_rec(1, 100.0), config_rec(2, 1200.0), config_rec(3, 5.0)]),
        encode_case("extreme_values", "Largest param id and a negative value.", m.CONFIG, seq=0xFFFFFFFF, base_ms=0,
                    records=[config_rec(0xFFFF, -0.5)]),
    ]
    return "config", "CONFIG packets: change a setting on the device.", cases


def ack_file():
    names = {m.ACK_APPLIED: "applied", m.ACK_OUT_OF_RANGE: "out_of_range",
             m.ACK_UNKNOWN_PARAM: "unknown_param", m.ACK_BAD_REGISTRY: "bad_registry"}
    cases = [encode_case(f"status_{name}", f"Acknowledges seq 7 with status {code} ({name}).", m.ACK,
                         seq=100 + code, base_ms=0, records=[{"acked_seq": 7, "status": code}])
             for code, name in names.items()]
    cases.append(encode_case("largest_acked_seq", "Acknowledges the largest sequence number.", m.ACK, seq=0,
                             base_ms=0, records=[{"acked_seq": 0xFFFFFFFF, "status": 0}]))
    return "ack", "ACK packets: the reply to a CONFIG.", cases


def crc_file():
    vectors = [(b"", "empty"), (b"123456789", "the standard CRC-32 check string"), (b"a", "one byte"),
               (b"The quick brown fox jumps over the lazy dog", "a sentence"), (bytes(32), "32 zero bytes"),
               (bytes(range(256)), "every byte value")]
    cases = [{"name": name, "description": f"CRC-32 (IEEE) of {name}.", "kind": "crc32", "input_hex": data.hex(),
              "crc32": hex32(zlib.crc32(data))} for data, name in vectors]
    check = next(c for c in cases if c["input_hex"] == b"123456789".hex())
    assert check["crc32"] == "0xcbf43926"                      # the value the spec requires
    return "crc32", "CRC-32 (IEEE, as zlib) test vectors. A packet's last 4 bytes are this over the rest.", cases


def invalid_file():
    (good,) = m.Packetizer(DEVICE_ID, REGISTRY_VERSION).build(m.DATA, [(1000, 100, 8000.0, 0)])
    body = good[:-4]

    def with_crc(b: bytes) -> bytes:
        return b + m.FOOTER.pack(zlib.crc32(b) & 0xFFFFFFFF)

    def flip(b: bytes, i: int) -> bytes:
        return b[:i] + bytes([b[i] ^ 1]) + b[i + 1:]

    count_2 = bytearray(body)
    count_2[16:18] = (2).to_bytes(2, "little")                  # record_count says 2, payload holds 1
    bad = [
        ("too_short", "Fewer bytes than a header plus a CRC.", good[:23], "too_short", "too short"),
        ("bad_magic", "Wrong magic number (CRC is valid).", with_crc(b"\x00\x00" + body[2:]), "bad_magic", "bad magic"),
        ("unsupported_format_version", "Format version 9 (CRC is valid).", with_crc(body[:2] + b"\x09" + body[3:]),
         "unsupported_format_version", "unsupported format version 9"),
        ("trailing_byte", "One extra byte after the CRC.", good + b"\x00", "length_mismatch", "length mismatch"),
        ("truncated", "The last record byte and CRC are missing.", good[:-5], "length_mismatch", "length mismatch"),
        ("flipped_payload_bit", "One bit changed in a record.", flip(good, 20), "crc_mismatch", "crc mismatch"),
        ("flipped_crc_bit", "One bit changed in the CRC.", flip(good, len(good) - 1), "crc_mismatch", "crc mismatch"),
        ("record_count_mismatch", "record_count is 2 but the payload holds one record (CRC is valid).",
         with_crc(bytes(count_2)), "record_count_mismatch", "record count does not match payload length"),
    ]
    cases = []
    for name, description, data, code, message in bad:
        try:
            m.decode_packet(data)
        except m.PacketError as e:
            assert str(e) == message, (name, str(e))
        else:
            raise AssertionError(f"{name} was accepted")
        cases.append({"name": name, "description": description, "kind": "invalid", "hex": data.hex(),
                      "error": code, "python_message": message})
    return "invalid", "Packets every decoder must reject. `error` is the reason, the same for all implementations.", cases


# ---------------------------------------------------------------- output
def dump(obj, level: int = 0) -> str:
    """JSON with 2-space indentation, except that objects/lists holding only plain values (one record,
    one header) stay on a single line, which keeps files with hundreds of records short."""
    flat = isinstance(obj, (dict, list)) and not any(isinstance(v, (dict, list)) for v in (
        obj.values() if isinstance(obj, dict) else obj))
    if not isinstance(obj, (dict, list)) or flat or not obj:
        return json.dumps(obj)
    pad, inner = "  " * level, "  " * (level + 1)
    if isinstance(obj, dict):
        body = ",\n".join(f"{inner}{json.dumps(k)}: {dump(v, level + 1)}" for k, v in obj.items())
        return "{\n" + body + "\n" + pad + "}"
    return "[\n" + ",\n".join(inner + dump(v, level + 1) for v in obj) + "\n" + pad + "]"


def render() -> dict[str, str]:
    files = {}
    for builder in (data_file, raw_can_file, config_file, ack_file, crc_file, invalid_file):
        name, description, cases = builder()
        doc = {"description": description, "format_version": m.FORMAT_VERSION, "magic": f"0x{m.MAGIC:04x}",
               "header_bytes": m.HEADER.size, "footer_bytes": m.FOOTER.size, "cases": cases}
        files[f"{name}.json"] = dump(doc) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="change nothing; fail if the files are out of date")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    files = render()
    stale = [n for n, text in files.items() if not (args.out / n).exists() or (args.out / n).read_text() != text]
    if args.check:
        for n in stale:
            print(f"ERROR: {args.out / n} is missing or out of date; run tools/make_golden.py", file=sys.stderr)
        print("golden files OK" if not stale else "", end="\n" if not stale else "")
        return 1 if stale else 0
    args.out.mkdir(parents=True, exist_ok=True)
    for n in stale:
        tmp = (args.out / n).with_name(n + ".tmp")
        tmp.write_text(files[n])
        tmp.replace(args.out / n)
    print(f"wrote {len(stale)} file(s) to {args.out}" if stale else "golden files already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
