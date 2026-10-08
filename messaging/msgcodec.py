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
def missing_between(last_seq: int, new_seq: int) -> int:
    """How many packets were skipped between two consecutively received sequence numbers
    (handles u32 wrap)."""
    return (new_seq - last_seq - 1) & 0xFFFFFFFF


def abs_time_ms(pkt: Packet, dt_ms: int) -> int:
    """Device-clock time (ms since boot) of a record. Server maps this to wall-clock time."""
    return pkt.base_ms + dt_ms


# ---------------------------------------------------------------- CAN signal extraction
def extract_signal(data: bytes, start_bit: int, length: int, byte_order: str,
                   signed: bool, scale: float, offset: float) -> float:
    """DBC-style extraction. `start_bit` is numbered exactly as in a DBC file."""
    raw = 0
    if byte_order == "little_endian":            # Intel: start bit is the LSB
        for i in range(length):
            p = start_bit + i
            raw |= ((data[p // 8] >> (p % 8)) & 1) << i
    elif byte_order == "big_endian":             # Motorola: start bit is the MSB
        p = start_bit
        for _ in range(length):
            raw = (raw << 1) | ((data[p // 8] >> (p % 8)) & 1)
            p = (p // 8 + 1) * 8 + 7 if p % 8 == 0 else p - 1
    else:
        raise ValueError(byte_order)
    if signed and raw >= 1 << (length - 1):
        raw -= 1 << length
    return raw * scale + offset


# ---------------------------------------------------------------- registry
STATUSES = {"active", "planned", "deprecated"}
SOURCES = {"can", "adc", "i2c", "synthetic"}
BYTE_ORDERS = {"big_endian", "little_endian"}
CAN_SIGNAL_KEYS = {"bus", "start_bit", "length", "byte_order", "signed", "scale", "offset"}


def _check_can_src(ch: dict, reg: dict) -> None:
    """A CAN channel is addressed by an absolute `can_id`, or by `can_offset` from the
    E888 base id (`e888_base_id` in the registry). Multiplexed signals add a `mux` block."""
    s = ch["src"]
    cid = ch["id"]
    missing = CAN_SIGNAL_KEYS - s.keys()
    if missing:
        raise ValueError(f"channel {cid}: src missing {sorted(missing)}")
    if ("can_id" in s) == ("can_offset" in s):
        raise ValueError(f"channel {cid}: src needs exactly one of can_id / can_offset")
    if "can_offset" in s and "e888_base_id" not in reg:
        raise ValueError(f"channel {cid}: can_offset needs e888_base_id in the registry")
    if s["byte_order"] not in BYTE_ORDERS:
        raise ValueError(f"channel {cid}: bad byte_order {s['byte_order']}")
    if not 1 <= s["length"] <= 32 or not 0 <= s["start_bit"] <= 63:
        raise ValueError(f"channel {cid}: bad start_bit/length")
    mux = s.get("mux")
    if mux is not None and (mux.keys() != {"start_bit", "length", "value"}
                            or mux["value"] >= 1 << mux["length"]):
        raise ValueError(f"channel {cid}: bad mux block")


def load_registry(path: str) -> dict:
    with open(path) as f:
        reg = yaml.safe_load(f)
    seen_ids, seen_names = set(), set()
    for ch in reg["channels"]:
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
    pids = [p["id"] for p in reg["params"]]
    if len(pids) != len(set(pids)):
        raise ValueError("duplicate param id")
    return reg


def can_frame_id(ch: dict, reg: dict) -> int:
    """Absolute CAN id a channel is read from."""
    s = ch["src"]
    return s["can_id"] if "can_id" in s else reg["e888_base_id"] + s["can_offset"]


def extract_channel(data: bytes, ch: dict) -> float | None:
    """Decode one CAN channel from a frame. Returns None when the channel is multiplexed
    and this frame carries a different multiplexer value."""
    s = ch["src"]
    mux = s.get("mux")
    if mux is not None:
        sel = extract_signal(data, mux["start_bit"], mux["length"], "big_endian", False, 1, 0)
        if sel != mux["value"]:
            return None
    return extract_signal(data, s["start_bit"], s["length"], s["byte_order"],
                          s["signed"], s["scale"], s["offset"])


def apply_quality(value: float, ch: dict) -> tuple[float, int]:
    """Clamp to registry range; flag it."""
    if value < ch["min"]:
        return float(ch["min"]), Q_CLAMPED
    if value > ch["max"]:
        return float(ch["max"]), Q_CLAMPED
    return value, 0
