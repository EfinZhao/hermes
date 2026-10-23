"""Receive what a device sends: check packets, track seq gaps, decode values, report problems.

Usage: uv run python tools/receiver/receiver.py [--host H] [--port P] [--registry PATH] [--archive DIR]
                                                [--quiet] [--idle-exit S]

`Receiver.feed(bytes)` is the front door for any transport; the command line listens on UDP and
prints each value. Per packet:
  1. Corrupt packets (too short, bad magic, format version, length or CRC) are dropped and
     counted by reason, never partly decoded.
  2. Only DATA and RAW_CAN are decoded. COMMAND, HEARTBEAT and unknown types are rejected;
     CONFIG and ACK are counted as ignored (this receiver sends no CONFIG yet).
  3. seq is tracked per device: a jump ahead counts the skipped packets as lost; a late packet
     that fills a gap (reordered, or BACKFILL) is delivered and taken off the lost count; a
     repeat (same seq and base_ms) is dropped. Wrapping from 4294967295 to 0 is normal. If device
     time goes back by more than 2 s without the BACKFILL flag, the device rebooted: its seq
     tracking and timebase start again.
  4. Values are decoded with the registry version the packet names and passed to `on_value`.
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from collections import Counter, OrderedDict, deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "messaging"))
import msgcodec  # noqa: E402
from decoder import Decoder, RawFrame, Value  # noqa: E402
from registry_store import DEFAULT_ARCHIVE, DEFAULT_REGISTRY, RegistryStore  # noqa: E402
from timebase import Timebase  # noqa: E402

# PacketError message (from msgcodec.decode_packet) -> counter name
CORRUPT_REASONS = [("too short", "too_short"), ("bad magic", "bad_magic"),
                   ("unsupported format version", "unsupported_format_version"),
                   ("length mismatch", "length_mismatch"), ("crc mismatch", "crc_mismatch"),
                   ("record count", "record_count_mismatch")]
REJECTED_TYPES = "rejected_type"   # COMMAND, HEARTBEAT, unknown
DECODED_TYPES = {msgcodec.DATA, msgcodec.RAW_CAN}
IGNORED_TYPES = {msgcodec.CONFIG, msgcodec.ACK}
QUALITY_NAMES = [(msgcodec.Q_INVALID, "INVALID"), (msgcodec.Q_STALE, "STALE"),
                 (msgcodec.Q_CLAMPED, "CLAMPED"), (msgcodec.Q_FAULT, "FAULT")]


def corrupt_reason(err: msgcodec.PacketError) -> str:
    text = str(err)
    return next((key for prefix, key in CORRUPT_REASONS if text.startswith(prefix)), "other")


def newest_dt(pkt: msgcodec.Packet) -> int:
    """dt_ms of the packet's newest record (records are sorted, but don't rely on it)."""
    dt = 1 if pkt.msg_type == msgcodec.DATA else 0        # position of dt_ms in the record
    return max((r[dt] for r in pkt.records), default=0)


class Stream:
    """seq and clock state for one device."""

    def __init__(self, track: int):
        self.track = track
        self.reset()

    def reset(self) -> None:
        self.last_seq: int | None = None             # seq and unwrapped base_ms of the newest packet
        self.last_ms: int | None = None
        self.missing: OrderedDict[int, tuple[int, int]] = OrderedDict()  # skipped seq -> base_ms range
        self.seen: deque[int] = deque()              # recent seqs, oldest first
        self.seen_ms: dict[int, int] = {}            # recent seq -> its raw base_ms, to spot repeats
        self.timebase = Timebase()

    def remember(self, seq: int, raw_ms: int) -> None:
        if len(self.seen) == self.track:
            self.seen_ms.pop(self.seen.popleft(), None)
        self.seen.append(seq)
        self.seen_ms[seq] = raw_ms

    def mark_missing(self, count: int, lo_ms: int, hi_ms: int) -> None:
        """`count` seqs after last_seq were skipped; their base_ms must lie in [lo_ms, hi_ms]."""
        for i in range(max(1, count - self.track + 1), count + 1):   # only the newest `track` are kept
            self.missing[(self.last_seq + i) & 0xFFFFFFFF] = (lo_ms, hi_ms)
        while len(self.missing) > self.track:
            self.missing.popitem(last=False)

    def start(self, seq: int, base_ms: int) -> None:
        self.last_seq, self.last_ms = seq, base_ms


class Receiver:
    def __init__(self, store: RegistryStore, on_value=None, on_raw_can=None, on_event=None,
                 clock=time.time, track: int = 4096, reboot_ms: int = 2000):
        """on_value(Value), on_raw_can(RawFrame), on_event(kind, device_id, detail): all optional.
        Events: "gap" (detail = packets missing), "late", "duplicate", "reboot" (detail = seq),
        "corrupt" (detail = reason), "rejected" (detail = msg_type), "unknown_version" (detail = version).
        `track`: how many recent seqs per device are remembered, for gap filling and repeats.
        `reboot_ms`: device time going back by more than this (without BACKFILL) means a reboot."""
        self.decoder = Decoder(store)
        self.on_value, self.on_raw_can, self.on_event = on_value, on_raw_can, on_event
        self.clock, self.track, self.reboot_ms = clock, track, reboot_ms
        self.streams: dict[int, Stream] = {}
        self.stats: Counter[str] = Counter()     # packets, delivered, lost, late, duplicates, ...
        self.corrupt: Counter[str] = Counter()   # by reason

    def _event(self, kind: str, device_id: int | None, detail=None) -> None:
        if self.on_event:
            self.on_event(kind, device_id, detail)

    def feed(self, data: bytes, arrival: float | None = None) -> bool:
        """Handle one packet. Returns True if its contents were delivered."""
        arrival = self.clock() if arrival is None else arrival
        self.stats["packets"] += 1
        try:
            pkt = msgcodec.decode_packet(data)
        except msgcodec.PacketError as err:
            if str(err).startswith("msg_type"):              # a reserved type that carries a payload
                self.stats[REJECTED_TYPES] += 1
                self._event("rejected", data[4], data[3])
                return False
            reason = corrupt_reason(err)
            self.corrupt[reason] += 1
            self._event("corrupt", None, reason)
            return False
        if pkt.msg_type in IGNORED_TYPES:
            self.stats["ignored"] += 1
            return False
        if pkt.msg_type not in DECODED_TYPES:
            self.stats[REJECTED_TYPES] += 1
            self._event("rejected", pkt.device_id, pkt.msg_type)
            return False

        st = self.streams.setdefault(pkt.device_id, Stream(self.track))
        base_ms = self._track_seq(st, pkt)
        if base_ms is None:
            return False
        if st.last_seq == pkt.seq and not pkt.flags & msgcodec.FLAG_BACKFILL:   # the newest packet
            st.timebase.observe(base_ms + newest_dt(pkt), arrival)

        items = self.decoder.decode(pkt, base_ms, st.timebase)
        if items is None:
            self.stats["unknown_version"] += 1
            self._event("unknown_version", pkt.device_id, pkt.registry_version)
            return False
        self.stats["delivered"] += 1
        for item in items:
            if isinstance(item, Value):
                self.stats["values"] += 1
                if self.on_value:
                    self.on_value(item)
            else:
                self.stats["raw_frames"] += 1
                if self.on_raw_can:
                    self.on_raw_can(item)
        return True

    def _track_seq(self, st: Stream, pkt: msgcodec.Packet) -> int | None:
        """Update seq state. Returns the packet's unwrapped base_ms, or None to drop it (a repeat)."""
        seq, raw, backfill = pkt.seq, pkt.base_ms, bool(pkt.flags & msgcodec.FLAG_BACKFILL)
        base = st.timebase.unwrap(raw)
        if st.last_seq is None:                                     # first packet from this device
            st.start(seq, base)
        elif st.seen_ms.get(seq) == raw:                            # same seq, same time: a repeat
            self.stats["duplicates"] += 1
            self._event("duplicate", pkt.device_id, seq)
            return None
        elif seq in st.missing and st.missing[seq][0] <= base <= st.missing[seq][1]:
            del st.missing[seq]                                     # late, fills a gap
            self.stats["lost"] -= 1
            self.stats["late"] += 1
            self._event("late", pkt.device_id, seq)
        elif not backfill and base < st.last_ms - self.reboot_ms:   # device time went back: reboot
            self.stats["reboots"] += 1
            self._event("reboot", pkt.device_id, seq)
            st.reset()
            base = st.timebase.unwrap(raw)
            st.start(seq, base)
        elif (gap := msgcodec.missing_between(st.last_seq, seq)) is not None and base >= st.last_ms:
            if gap:                                                 # newer: the normal case
                st.mark_missing(gap, st.last_ms, base)
                self.stats["lost"] += gap
                self._event("gap", pkt.device_id, gap)
            st.start(seq, base)
        else:                                                       # older than anything tracked
            self.stats["late"] += 1
            self._event("late", pkt.device_id, seq)
        st.remember(seq, raw)
        return base


# ---------------------------------------------------------------- command line
def quality_text(q: int) -> str:
    return ",".join(name for bit, name in QUALITY_NAMES if q & bit)


def print_value(v: Value) -> None:
    when = datetime.fromtimestamp(v.wall_time).strftime("%H:%M:%S.%f")[:-3] if v.wall_time else "--:--:--"
    flags = " ".join(f for f in (quality_text(v.quality), "BACKFILL" if v.backfill else "") if f)
    print(f"{when}  dev{v.device_id}  {v.name:<28} {v.value:12.3f} {v.unit:<6} {flags}".rstrip())


def print_raw(f: RawFrame) -> None:
    print(f"raw  dev{f.device_id}  bus{f.bus}  0x{f.can_id:0{8 if f.extended else 3}X}  {f.data.hex(' ')}")


def print_event(kind: str, device_id: int | None, detail) -> None:
    text = {"gap": f"{detail} packet(s) missing", "late": f"late packet seq {detail}",
            "duplicate": f"repeat of seq {detail} dropped", "reboot": "device rebooted",
            "corrupt": f"corrupt packet dropped ({detail})", "rejected": f"msg_type {detail} rejected",
            "unknown_version": f"registry_version {detail} unknown; add its snapshot to the archive"}[kind]
    print(f"! {'dev' + str(device_id) + ': ' if device_id is not None else ''}{text}", file=sys.stderr)


def summary(rx: Receiver) -> str:
    s = rx.stats
    expected = s["delivered"] + s["lost"]
    loss = f"{100 * s['lost'] / expected:.1f}%" if expected else "0%"
    corrupt = sum(rx.corrupt.values())
    return (f"{s['packets']} packets: {s['delivered']} delivered ({s['values']} values, {s['raw_frames']} raw frames), "
            f"{s['lost']} lost ({loss}), {s['late']} late, {s['duplicates']} repeats, {corrupt} corrupt"
            f"{' ' + str(dict(rx.corrupt)) if corrupt else ''}, {s[REJECTED_TYPES]} rejected, "
            f"{s['ignored']} ignored, {s['reboots']} reboots, {s['unknown_version']} unknown version, "
            f"{rx.decoder.unknown_channel} unknown channel records")


def serve(sock: socket.socket, rx: Receiver, idle_exit: float = 0.0) -> None:
    """Feed every datagram from `sock` to `rx`. Stops after `idle_exit` s without one (0 = never)."""
    sock.settimeout(idle_exit or None)
    try:
        while True:
            data, _ = sock.recvfrom(65535)
            rx.feed(data)
    except socket.timeout:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (0.0.0.0 = every interface)")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="folder of registry_vN.json snapshots")
    ap.add_argument("--quiet", action="store_true", help="print only events and the summary, not every value")
    ap.add_argument("--idle-exit", type=float, default=0.0, help="stop after this many seconds with no packets")
    args = ap.parse_args(argv)

    store = RegistryStore.load(args.registry, args.archive)
    if not store.versions():
        ap.error(f"no registry found ({args.registry}, {args.archive})")
    rx = Receiver(store, on_value=None if args.quiet else print_value,
                  on_raw_can=None if args.quiet else print_raw, on_event=print_event)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((args.host, args.port))
    except OSError as err:
        sock.close()
        ap.error(f"cannot listen on {args.host}:{args.port}: {err}")
    print(f"listening on {args.host}:{args.port}, registry versions {store.versions()} (Ctrl-C to stop)")
    try:
        serve(sock, rx, args.idle_exit)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print(summary(rx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
