"""Reference implementation of the packet spec: packet codec, packetizer, ring buffer,
CAN signal extraction and registry loading.

This is the *specification in executable form*. Any firmware or receiver must produce and
accept exactly the bytes this module does. All multi-byte fields are little-endian.
"""
from __future__ import annotations

import struct
import zlib
from collections import deque
from dataclasses import dataclass, field

import yaml

# ---------------------------------------------------------------- constants
MAGIC = 0x4D48         # little-endian on the wire: bytes 48 4D, ASCII "HM"
FORMAT_VERSION = 1

# message types
DATA, CONFIG, COMMAND, ACK, HEARTBEAT, RAW_CAN = 0, 1, 2, 3, 4, 5

# header flags
FLAG_BACKFILL = 0x01   # packet was buffered (ring buffer / SD) and is being re-sent late

# sample quality bits (0 = perfectly fine)
Q_INVALID = 0x01       # value must not be used (cereal's `valid=false`)
Q_STALE = 0x02         # source stopped updating; value is the last known one
Q_CLAMPED = 0x04       # value was outside the registry min/max
Q_FAULT = 0x08         # sensor/diagnostic fault reported by the source

# ACK status codes (for CONFIG)
ACK_APPLIED, ACK_OUT_OF_RANGE, ACK_UNKNOWN_PARAM, ACK_BAD_REGISTRY = 0, 1, 2, 3

# layouts
HEADER = struct.Struct("<HBBBBHIIHH")      # 20 bytes
FOOTER = struct.Struct("<I")               # CRC-32 of header + payload, 4 bytes
SAMPLE = struct.Struct("<HHfB")            # channel_id, dt_ms, value, quality  = 9 bytes
RAWCAN = struct.Struct("<HIBB8s")          # dt_ms, can_id, bus, dlc, data     = 16 bytes
CFGREC = struct.Struct("<Hf")              # param_id, value                    = 6 bytes
ACKREC = struct.Struct("<IB")              # acked_seq, status                  = 5 bytes

RECORD = {DATA: SAMPLE, RAW_CAN: RAWCAN, CONFIG: CFGREC, ACK: ACKREC}
OVERHEAD = HEADER.size + FOOTER.size       # 24 bytes
CAN_EXTENDED = 0x80000000                  # bit 31 of can_id: 29-bit identifier


class PacketError(ValueError):
    pass


# ---------------------------------------------------------------- encode / decode
def encode_packet(msg_type: int, records: list[bytes], *, device_id: int, registry_version: int,
                  seq: int, base_ms: int, flags: int = 0) -> bytes:
    payload = b"".join(records)
    header = HEADER.pack(MAGIC, FORMAT_VERSION, msg_type, device_id, flags,
                         registry_version, seq & 0xFFFFFFFF, base_ms & 0xFFFFFFFF,
                         len(records), len(payload))
    body = header + payload
    return body + FOOTER.pack(zlib.crc32(body) & 0xFFFFFFFF)


@dataclass
class Packet:
    msg_type: int
    device_id: int
    flags: int
    registry_version: int
    seq: int
    base_ms: int
    records: list = field(default_factory=list)


def decode_packet(buf: bytes) -> Packet:
    if len(buf) < OVERHEAD:
        raise PacketError("too short")
    (magic, fmt, msg_type, device_id, flags, reg_ver, seq, base_ms,
     count, payload_len) = HEADER.unpack_from(buf, 0)
    if magic != MAGIC:
        raise PacketError("bad magic")
    if fmt != FORMAT_VERSION:
        raise PacketError(f"unsupported format version {fmt}")
    if len(buf) != OVERHEAD + payload_len:
        raise PacketError("length mismatch")
    (crc,) = FOOTER.unpack_from(buf, HEADER.size + payload_len)
    if crc != zlib.crc32(buf[:HEADER.size + payload_len]) & 0xFFFFFFFF:
        raise PacketError("crc mismatch")
    layout = RECORD.get(msg_type)
    records = []
    if layout is not None:
        if count * layout.size != payload_len:
            raise PacketError("record count does not match payload length")
        for i in range(count):
            records.append(layout.unpack_from(buf, HEADER.size + i * layout.size))
    elif payload_len:
        raise PacketError(f"msg_type {msg_type} has no defined record layout in v1")
    return Packet(msg_type, device_id, flags, reg_ver, seq, base_ms, records)


# ---------------------------------------------------------------- packetizer (device side)
class Packetizer:
    """Collects samples for one batch window, then emits one or more DATA packets."""

    def __init__(self, device_id: int, registry_version: int, max_packet_bytes: int = 1200):
        self.device_id = device_id
        self.registry_version = registry_version
        self.max_packet_bytes = max_packet_bytes
        self.seq = 0

    def build(self, msg_type: int, items: list[tuple], flags: int = 0) -> list[bytes]:
        """items: (t_ms, *fields) tuples. DATA: (t_ms, channel_id, value, quality)
        RAW_CAN: (t_ms, can_id, bus, dlc, data)."""
        if not items:
            return []
        layout = RECORD[msg_type]
        per_packet = (self.max_packet_bytes - OVERHEAD) // layout.size
        if per_packet < 1:
            raise ValueError("max_packet_bytes too small for one record")
        items = sorted(items, key=lambda it: it[0])
        packets = []
        for i in range(0, len(items), per_packet):
            chunk = items[i:i + per_packet]
            base = chunk[0][0]
            records = []
            for t_ms, *rest in chunk:
                dt = t_ms - base
                if not 0 <= dt <= 0xFFFF:
                    raise ValueError("record is more than 65.5 s from packet base time")
                records.append(layout.pack(*self._fields(msg_type, dt, rest)))
            packets.append(encode_packet(msg_type, records, device_id=self.device_id,
                                         registry_version=self.registry_version,
                                         seq=self.seq, base_ms=base, flags=flags))
            self.seq = (self.seq + 1) & 0xFFFFFFFF
        return packets

    @staticmethod
    def _fields(msg_type, dt, rest):
        if msg_type == DATA:
            channel_id, value, quality = rest
            return channel_id, dt, float(value), quality
        if msg_type == RAW_CAN:
            can_id, bus, dlc, data = rest
            if len(data) > 8:
                raise ValueError(f"RAW_CAN data is {len(data)} bytes; the record holds at most 8")
            return dt, can_id, bus, dlc, bytes(data).ljust(8, b"\0")
        raise ValueError("Packetizer.build supports DATA and RAW_CAN")


# ---------------------------------------------------------------- ring buffer (device side)
class PacketRing:
    """FIFO of packets. When full, the OLDEST packet is dropped to make room.
    Dropped packets show up as a sequence gap at the server and can be recovered from SD."""

    def __init__(self, capacity: int):
        self.q = deque(maxlen=capacity)
        self.dropped = 0

    def push(self, pkt: bytes) -> None:
        if len(self.q) == self.q.maxlen:
            self.dropped += 1
        self.q.append(pkt)

    def pop_oldest(self) -> bytes | None:
        return self.q.popleft() if self.q else None

    def __len__(self):
        return len(self.q)


# ---------------------------------------------------------------- server helpers
def missing_between(last_seq: int, new_seq: int) -> int | None:
    """How many packets were skipped between the last packet received and a new one (handles u32 wrap).

    Returns None when `new_seq` is a duplicate or an older packet (it is within half the 32-bit
    range behind `last_seq`); the caller should drop it or treat it as a late arrival.
    A device reboot (seq restarts at 0 and base_ms goes backwards) looks like an old packet here;
    spotting it needs base_ms, so that is the receiver's job."""
    ahead = (new_seq - last_seq) & 0xFFFFFFFF
    if ahead == 0 or ahead >= 1 << 31:
        return None
    return ahead - 1


def abs_time_ms(pkt: Packet, dt_ms: int) -> int:
    """Device-clock time (ms since boot) of a record. Server maps this to wall-clock time."""
    return pkt.base_ms + dt_ms


# ---------------------------------------------------------------- CAN signal extraction
class FrameTooShort(ValueError):
    """The CAN frame has fewer bytes than the signal needs."""


def bit_positions(start_bit: int, length: int, byte_order: str) -> list[int]:
    """Frame bit numbers (byte * 8 + bit, as in a DBC) of a signal, least significant bit first
    for little-endian (Intel) and most significant bit first for big-endian (Motorola)."""
    if byte_order == "little_endian":            # Intel: start bit is the LSB
        return [start_bit + i for i in range(length)]
    if byte_order == "big_endian":               # Motorola: start bit is the MSB
        out, p = [], start_bit
        for _ in range(length):
            out.append(p)
            p = (p // 8 + 1) * 8 + 7 if p % 8 == 0 else p - 1
        return out
    raise ValueError(byte_order)


def extract_signal(data: bytes, start_bit: int, length: int, byte_order: str,
                   signed: bool, scale: float, offset: float) -> float:
    """DBC-style extraction. `start_bit` is numbered exactly as in a DBC file.
    Raises FrameTooShort if `data` is shorter than the signal needs."""
    bits = bit_positions(start_bit, length, byte_order)
    if max(p // 8 for p in bits) >= len(data):
        raise FrameTooShort(f"frame has {len(data)} bytes, signal needs {max(p // 8 for p in bits) + 1}")
    raw = 0
    if byte_order == "little_endian":
        for i, p in enumerate(bits):
            raw |= ((data[p // 8] >> (p % 8)) & 1) << i
    else:
        for p in bits:
            raw = (raw << 1) | ((data[p // 8] >> (p % 8)) & 1)
    if signed and raw >= 1 << (length - 1):
        raw -= 1 << length
    return raw * scale + offset


# ---------------------------------------------------------------- registry
STATUSES = {"active", "planned", "deprecated"}
SOURCES = {"can", "adc", "i2c", "synthetic"}
BYTE_ORDERS = {"big_endian", "little_endian"}
CAN_SIGNAL_KEYS = {"bus", "start_bit", "length", "byte_order", "signed", "scale", "offset"}
CHANNEL_KEYS = {"id", "name", "unit", "source", "rate_hz", "priority", "live", "log", "min", "max", "status", "src"}
PARAM_KEYS = {"id", "name", "default", "min", "max", "unit"}


def _check_can_src(ch: dict, reg: dict) -> None:
    """A CAN channel is addressed either by an absolute `can_id`, or by `can_base` + `can_offset`
    where `can_base` names an entry of the registry's `can_bases` table (devices whose CAN ids are
    set by a configurable base, like an expansion unit). `extended: true` marks 29-bit ids.
    Multiplexed signals add a `mux` block."""
    s = ch["src"]
    cid = ch["id"]
    missing = CAN_SIGNAL_KEYS - s.keys()
    if missing:
        raise ValueError(f"channel {cid}: src missing {sorted(missing)}")
    if ("can_id" in s) == ("can_offset" in s):
        raise ValueError(f"channel {cid}: src needs exactly one of can_id / can_offset")
    if ("can_base" in s) != ("can_offset" in s):
        raise ValueError(f"channel {cid}: can_base and can_offset go together")
    if "can_base" in s and s["can_base"] not in (reg.get("can_bases") or {}):
        raise ValueError(f"channel {cid}: can_base {s['can_base']!r} is not in the registry's can_bases")
    if not isinstance(s.get("extended", False), bool):
        raise ValueError(f"channel {cid}: extended must be true or false")
    frame_id = s["can_id"] if "can_id" in s else reg["can_bases"][s["can_base"]] + s["can_offset"]
    if frame_id > (0x1FFFFFFF if s.get("extended") else 0x7FF):
        raise ValueError(f"channel {cid}: CAN id 0x{frame_id:X} is too large"
                         + ("" if s.get("extended") else " for a standard frame; set extended: true for 29-bit ids"))
    if s["byte_order"] not in BYTE_ORDERS:
        raise ValueError(f"channel {cid}: bad byte_order {s['byte_order']}")
    if not 1 <= s["length"] <= 32 or not 0 <= s["start_bit"] <= 63:
        raise ValueError(f"channel {cid}: bad start_bit/length")
    if max(p // 8 for p in bit_positions(s["start_bit"], s["length"], s["byte_order"])) >= 8:
        raise ValueError(f"channel {cid}: signal does not fit in an 8-byte CAN frame")
    mux = s.get("mux")
    if mux is not None:
        if mux.keys() != {"start_bit", "length", "value"} or mux["value"] >= 1 << mux["length"]:
            raise ValueError(f"channel {cid}: bad mux block")
        if max(p // 8 for p in bit_positions(mux["start_bit"], mux["length"], s["byte_order"])) >= 8:
            raise ValueError(f"channel {cid}: mux selector does not fit in an 8-byte CAN frame")


def validate_registry(reg: object, source: str = "registry") -> dict:
    """Check an already-parsed registry; return it, or raise ValueError."""
    if not isinstance(reg, dict) or "channels" not in reg or "params" not in reg:
        raise ValueError(f"{source}: not a registry (file empty, or missing params/channels)")
    seen_ids, seen_names = set(), set()
    for ch in reg["channels"]:
        if missing := CHANNEL_KEYS - ch.keys():
            raise ValueError(f"channel {ch.get('id', '?')}: missing {sorted(missing)}")
        if ch["min"] > ch["max"]:
            raise ValueError(f"channel {ch['id']}: min is greater than max")
        if not 1 <= ch["id"] <= 32767:
            raise ValueError(f"channel id out of range: {ch['id']}")
        if ch["id"] in seen_ids or ch["name"] in seen_names:
            raise ValueError(f"duplicate channel id/name: {ch['id']} {ch['name']}")
        if ch["status"] not in STATUSES or ch["source"] not in SOURCES:
            raise ValueError(f"bad status/source on channel {ch['id']}")
        if ch["priority"] == 2 and "live_decimate" not in ch:
            raise ValueError(f"priority 2 channel {ch['id']} needs live_decimate")
        if ch["source"] == "can":
            _check_can_src(ch, reg)
        seen_ids.add(ch["id"])
        seen_names.add(ch["name"])
    for p in reg["params"]:
        if missing := PARAM_KEYS - p.keys():
            raise ValueError(f"param {p.get('id', '?')}: missing {sorted(missing)}")
        if not p["min"] <= p["default"] <= p["max"]:
            raise ValueError(f"param {p['id']} ({p['name']}): default must be between min and max")
    pids = [p["id"] for p in reg["params"]]
    if len(pids) != len(set(pids)):
        raise ValueError("duplicate param id")
    return reg


def load_registry(path: str) -> dict:
    with open(path) as f:
        return validate_registry(yaml.safe_load(f), path)


def can_frame_id(ch: dict, reg: dict) -> int:
    """Absolute CAN id a channel is read from."""
    s = ch["src"]
    return s["can_id"] if "can_id" in s else reg["can_bases"][s["can_base"]] + s["can_offset"]


def extract_channel(data: bytes, ch: dict) -> float | None:
    """Decode one CAN channel from a frame. Returns None when the channel is multiplexed and this
    frame carries a different multiplexer value, or when the frame is too short for the signal.
    The multiplexer selector has the same byte order as the signal."""
    s = ch["src"]
    mux = s.get("mux")
    try:
        if mux is not None:
            sel = extract_signal(data, mux["start_bit"], mux["length"], s["byte_order"], False, 1, 0)
            if sel != mux["value"]:
                return None
        return extract_signal(data, s["start_bit"], s["length"], s["byte_order"],
                              s["signed"], s["scale"], s["offset"])
    except FrameTooShort:
        return None


def apply_quality(value: float, ch: dict) -> tuple[float, int]:
    """Clamp to the registry range and flag it. NaN is kept but flagged INVALID."""
    if value != value:                                  # NaN
        return value, Q_INVALID
    if value < ch["min"]:
        return float(ch["min"]), Q_CLAMPED
    if value > ch["max"]:
        return float(ch["max"]), Q_CLAMPED
    return value, 0
