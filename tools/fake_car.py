"""Pretend to be the device: send realistic DATA packets for the registry's channels over UDP.

Usage: uv run python tools/fake_car.py [--host H] [--port P] [--duration S] [--loss F] [--reorder F]
                                       [--duplicate F] [--speed X] [--seed N] [--start-seq N] [--boot-ms N]

It behaves like the real device: every active, live channel is sampled at its registry rate
(priority 2 channels send 1 of every `live_decimate` samples), samples are collected for one batch
window (the `batch_window_ms` param), and each window is turned into packets with the reference
Packetizer, so `seq` counts up and `base_ms` is the time of the window's first sample, in ms since "boot".
Values are plausible waveforms, not real car data.

  --loss       chance (0 to 1) that a packet is dropped, which leaves a gap in `seq`
  --reorder    chance that a packet is held back and arrives 1 to 3 packets late
  --duplicate  chance that a packet is sent twice
  --speed      1 = real time, 2 = twice as fast, 0 = as fast as possible
  --start-seq  first sequence number (try 4294967290 to watch the counter wrap)
  --boot-ms    device clock at the first sample
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "messaging"))
import msgcodec  # noqa: E402

DEFAULT_REGISTRY = ROOT / "messaging" / "registry.yaml"

# (low, high, period in seconds) of the waveform used for a channel with this unit
UNIT_WAVES = {"rpm": (1200, 8500, 9.0), "C": (60, 95, 90.0), "V": (12.4, 14.2, 20.0), "%": (0, 100, 6.0),
              "kPa": (40, 160, 7.0), "km/h": (0, 90, 12.0)}


def param_default(reg: dict, name: str) -> float:
    return next(p["default"] for p in reg["params"] if p["name"] == name)


class FakeCar:
    """The device side: turns time into packets. No sockets, no sleeping."""

    def __init__(self, reg: dict, *, seed: int = 0, boot_ms: int = 10_000, batch_ms: int | None = None,
                 start_seq: int = 0):
        self.reg, self.boot_ms = reg, boot_ms
        self.batch_ms = int(batch_ms or param_default(reg, "batch_window_ms"))
        self.channels = [c for c in reg["channels"] if c["status"] == "active" and c["live"]]
        self.next_index = {c["id"]: 0 for c in self.channels}              # next sample number per channel
        self.rngs = {c["id"]: random.Random(seed * 100003 + c["id"]) for c in self.channels}
        self.packetizer = msgcodec.Packetizer(reg["device_id"], reg["registry_version"],
                                              int(param_default(reg, "max_packet_bytes")))
        self.packetizer.seq = start_seq & 0xFFFFFFFF

    def value(self, ch: dict, index: int, t_s: float) -> float:
        """A plausible reading for this channel at time t_s seconds."""
        src, lo, hi = ch["src"], ch["min"], ch["max"]
        if ch["source"] == "synthetic" and src.get("kind") == "counter":
            return float(index)
        if ch["source"] == "synthetic" and src.get("kind") == "sine":
            return (lo + hi) / 2 + (hi - lo) / 2 * math.sin(2 * math.pi * src["freq_hz"] * t_s)
        wave_lo, wave_hi, period = UNIT_WAVES.get(ch["unit"], (lo + 0.2 * (hi - lo), lo + 0.8 * (hi - lo), 10.0))
        margin = 0.03 * (hi - lo)                                          # stay clear of the registry limits
        wave_lo, wave_hi = max(wave_lo, lo + margin), min(wave_hi, hi - margin)
        phase = ch["id"] * 0.7
        mid, amp = (wave_lo + wave_hi) / 2, (wave_hi - wave_lo) / 2
        noise = self.rngs[ch["id"]].gauss(0, 0.0025 * (wave_hi - wave_lo))
        return min(max(mid + amp * math.sin(2 * math.pi * t_s / period + phase) + noise, lo), hi)

    def batches(self, duration_s: float):
        """Yield (window_end_ms, packets) for each batch window over the given duration."""
        for w in range(math.ceil(duration_s * 1000 / self.batch_ms)):
            window_end = self.boot_ms + (w + 1) * self.batch_ms
            items = []
            for ch in self.channels:
                every = ch.get("live_decimate", 1) if ch["priority"] == 2 else 1
                period_ms = 1000 / ch["rate_hz"]
                while True:
                    k = self.next_index[ch["id"]]
                    t = self.boot_ms + round(k * period_ms)
                    if t >= window_end:
                        break
                    self.next_index[ch["id"]] = k + 1
                    if k % every == 0:
                        v, quality = msgcodec.apply_quality(self.value(ch, k, (t - self.boot_ms) / 1000), ch)
                        items.append((t, ch["id"], v, quality))
            yield window_end, self.packetizer.build(msgcodec.DATA, items)


def impair(packets: list[bytes], rng: random.Random, *, loss: float = 0.0, reorder: float = 0.0,
           duplicate: float = 0.0) -> list[bytes]:
    """What the network does: drop some packets, delay some by 1-3 places, send some twice."""
    out, held = [], []                                   # held: [places left to wait, packet]
    for pkt in packets:
        arrived = [h for h in held if h[0] <= 1]
        for h in held:
            h[0] -= 1
        held = [h for h in held if h[0] > 0]
        if rng.random() >= loss:
            if rng.random() < reorder:
                held.append([rng.randint(1, 3), pkt])
            else:
                out.append(pkt)
                if rng.random() < duplicate:
                    out.append(pkt)
        out.extend(h[1] for h in arrived)
    out.extend(h[1] for h in held)
    return out


def run(reg: dict, send, *, duration: float = 10.0, loss: float = 0.0, reorder: float = 0.0,
        duplicate: float = 0.0, speed: float = 1.0, seed: int = 0, start_seq: int = 0,
        boot_ms: int = 10_000, batch_ms: int | None = None) -> dict:
    """Generate and 'send' (call `send(bytes)`) packets in real time or faster. Returns counts."""
    car = FakeCar(reg, seed=seed, boot_ms=boot_ms, batch_ms=batch_ms, start_seq=start_seq)
    rng = random.Random(seed)
    stats = {"batches": 0, "built": 0, "sent": 0, "samples": 0, "dropped": 0, "duplicated": 0}
    started = time.monotonic()
    for window_end, packets in car.batches(duration):
        stats["batches"] += 1
        stats["built"] += len(packets)
        stats["samples"] += sum(len(msgcodec.decode_packet(p).records) for p in packets)
        sent = impair(packets, rng, loss=loss, reorder=reorder, duplicate=duplicate)
        stats["sent"] += len(sent)
        stats["dropped"] += len(packets) - len(set(sent))
        stats["duplicated"] += len(sent) - len(set(sent))
        for pkt in sent:
            send(pkt)
        if speed > 0:
            delay = started + (window_end - car.boot_ms) / 1000 / speed - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--duration", type=float, default=10.0, help="seconds of device time")
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--reorder", type=float, default=0.0)
    ap.add_argument("--duplicate", type=float, default=0.0)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--start-seq", type=int, default=0)
    ap.add_argument("--boot-ms", type=int, default=10_000)
    ap.add_argument("--batch-ms", type=int, default=None, help="batch window; default is the registry param")
    args = ap.parse_args(argv)
    for name in ("loss", "reorder", "duplicate"):
        if not 0 <= getattr(args, name) <= 1:
            ap.error(f"--{name} must be between 0 and 1")

    reg = msgcodec.load_registry(str(args.registry))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"sending to {args.host}:{args.port} for {args.duration:g} s of device time (Ctrl-C to stop)")
    try:
        stats = run(reg, lambda pkt: sock.sendto(pkt, (args.host, args.port)), duration=args.duration,
                    loss=args.loss, reorder=args.reorder, duplicate=args.duplicate, speed=args.speed,
                    seed=args.seed, start_seq=args.start_seq, boot_ms=args.boot_ms, batch_ms=args.batch_ms)
    except KeyboardInterrupt:
        print("stopped")
        return 0
    finally:
        sock.close()
    print(f"built {stats['built']} packets holding {stats['samples']} samples; sent {stats['sent']} "
          f"({stats['dropped']} dropped, {stats['duplicated']} duplicated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
