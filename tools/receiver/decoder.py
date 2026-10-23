"""Turn a checked packet's records into named, timestamped values.

DATA records are looked up in the registry version the packet names, so a channel id always
means what it meant when the packet was built. RAW_CAN frames need no registry.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "messaging"))
import msgcodec  # noqa: E402
from registry_store import RegistryStore  # noqa: E402
from timebase import Timebase  # noqa: E402


@dataclass(frozen=True)
class Value:
    device_id: int
    registry_version: int
    channel_id: int
    name: str
    unit: str
    device_ms: int            # ms since the device booted (unwrapped)
    wall_time: float | None   # Unix seconds, estimated by the timebase
    value: float
    quality: int              # msgcodec.Q_* bits, 0 = good
    backfill: bool            # the packet was buffered on the device and sent late


@dataclass(frozen=True)
class RawFrame:
    device_id: int
    device_ms: int
    wall_time: float | None
    can_id: int               # without the extended-id bit
    extended: bool            # 29-bit identifier
    bus: int
    data: bytes               # dlc bytes
    backfill: bool


class Decoder:
    def __init__(self, store: RegistryStore):
        self.store = store
        self.unknown_channel = 0   # DATA records skipped: id not in their registry version

    def decode(self, pkt: msgcodec.Packet, base_ms: int, timebase: Timebase) -> list[Value | RawFrame] | None:
        """`base_ms` is the packet's base time already unwrapped by `timebase`.
        Returns None for a DATA packet whose registry version isn't in the store."""
        backfill = bool(pkt.flags & msgcodec.FLAG_BACKFILL)
        out: list[Value | RawFrame] = []
        if pkt.msg_type == msgcodec.DATA:
            if self.store.get(pkt.registry_version) is None:
                return None
            for channel_id, dt_ms, value, quality in pkt.records:
                ch = self.store.channel(pkt.registry_version, channel_id)
                if ch is None:
                    self.unknown_channel += 1
                    continue
                t = base_ms + dt_ms
                out.append(Value(pkt.device_id, pkt.registry_version, channel_id, ch["name"], ch["unit"],
                                 t, timebase.to_wall(t), value, quality, backfill))
        elif pkt.msg_type == msgcodec.RAW_CAN:
            for dt_ms, can_id, bus, dlc, data in pkt.records:
                t = base_ms + dt_ms
                out.append(RawFrame(pkt.device_id, t, timebase.to_wall(t), can_id & ~msgcodec.CAN_EXTENDED,
                                    bool(can_id & msgcodec.CAN_EXTENDED), bus, data[:min(dlc, 8)], backfill))
        return out
