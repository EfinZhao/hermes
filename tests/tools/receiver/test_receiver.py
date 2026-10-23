"""receiver.py and decoder.py: checking, seq tracking, versioned decoding, and a real UDP loopback."""
import copy
import json
import socket
import struct
from pathlib import Path

import pytest

import fake_car
import msgcodec as m
from receiver import Receiver, serve, summary
from registry_store import RegistryStore

GOLDEN = Path(__file__).resolve().parents[3] / "messaging" / "golden"
T0 = 1_800_000_000.0


@pytest.fixture
def store(registry):
    s = RegistryStore()
    s.add(registry)
    return s


class Collect:
    """A receiver plus everything it handed out."""

    def __init__(self, store, **kw):
        self.values, self.raw, self.events = [], [], []
        self.rx = Receiver(store, on_value=self.values.append, on_raw_can=self.raw.append,
                           on_event=lambda *e: self.events.append(e), clock=lambda: T0, **kw)

    def feed(self, packets, arrival=None):
        return [self.rx.feed(p, arrival) for p in packets]

    def kinds(self):
        return [e[0] for e in self.events]


def pkt(reg, seq, base_ms=None, flags=0, version=None, device=None, values=None, msg_type=m.DATA):
    """A DATA packet with channel 1 (test.counter) = seq, unless `values` are given."""
    records = [m.SAMPLE.pack(ch, dt, v, 0) for ch, dt, v in (values or [(1, 0, float(seq))])]
    return m.encode_packet(msg_type, records, device_id=reg["device_id"] if device is None else device,
                           registry_version=reg["registry_version"] if version is None else version,
                           seq=seq, base_ms=10_000 + 100 * seq if base_ms is None else base_ms, flags=flags)


def counters(c):
    return [int(v.value) for v in c.values if v.channel_id == 1]


def fake_packets(reg, duration=5.0, **kw):
    return [p for _, ps in fake_car.FakeCar(reg, **kw).batches(duration) for p in ps]


# ---------------------------------------------------------------- done when: named, timestamped values
def test_fake_car_packets_decode_to_named_timestamped_values(registry, store):
    packets = fake_packets(registry)
    c = Collect(store)
    assert all(c.feed(packets))
    sent = [(r[0], m.abs_time_ms(p, r[1]), r[2], r[3]) for p in map(m.decode_packet, packets) for r in p.records]
    got = [(v.channel_id, v.device_ms, v.value, v.quality) for v in c.values]
    assert got == sent
    names = {ch["id"]: (ch["name"], ch["unit"]) for ch in registry["channels"]}
    assert all((v.name, v.unit) == names[v.channel_id] for v in c.values)
    assert {v.registry_version for v in c.values} == {registry["registry_version"]}
    assert c.rx.stats["lost"] == 0 and c.rx.stats["delivered"] == len(packets) and not c.events


def test_wall_times_follow_arrival_times(registry, store):
    """Each packet arrives 5-40 ms after its newest sample; times come out within that delay."""
    c = Collect(store)
    for i, p in enumerate(fake_packets(registry, duration=10.0)):
        pk = m.decode_packet(p)
        newest = pk.base_ms + max(r[1] for r in pk.records)
        c.rx.feed(p, arrival=T0 + newest / 1000 + (5 + i * 13 % 36) / 1000)
    for v in c.values:
        true_wall = T0 + v.device_ms / 1000
        assert 0 <= v.wall_time - true_wall <= 0.040
    assert c.values[-1].wall_time - (T0 + c.values[-1].device_ms / 1000) <= 0.010   # near the 5 ms minimum


# ---------------------------------------------------------------- done when: bad CRC dropped and counted
def test_a_bad_crc_packet_is_dropped_and_counted(registry, store):
    c = Collect(store)
    bad = bytearray(pkt(registry, 1))
    bad[25] ^= 0xFF                                    # flip a payload byte
    assert c.feed([pkt(registry, 0), bytes(bad), pkt(registry, 2)]) == [True, False, True]
    assert c.rx.corrupt == {"crc_mismatch": 1}
    assert counters(c) == [0, 2]
    assert c.rx.stats["lost"] == 1                     # the corrupt packet's seq is a gap
    assert ("corrupt", None, "crc_mismatch") in c.events


def golden(name):
    return json.loads((GOLDEN / f"{name}.json").read_text())["cases"]


@pytest.mark.parametrize("case", golden("invalid"), ids=lambda c: c["name"])
def test_golden_invalid_packets_are_counted_by_reason(store, case):
    c = Collect(store)
    assert c.feed([bytes.fromhex(case["hex"])]) == [False]
    assert c.rx.corrupt == {case["error"]: 1}
    assert not c.values


# ---------------------------------------------------------------- done when: gaps and loss, including u32 wrap
def test_skipped_seqs_are_counted_as_lost(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s) for s in (0, 1, 4, 5, 9)])
    assert c.rx.stats["lost"] == 5
    assert [e for e in c.events if e[0] == "gap"] == [("gap", 20, 2), ("gap", 20, 3)]


def test_seq_wrapping_to_zero_is_not_a_gap(registry, store):
    c = Collect(store)
    c.feed(fake_packets(registry, duration=2.0, start_seq=0xFFFFFFFA))
    assert c.rx.stats["lost"] == 0 and c.rx.stats["delivered"] == 20 and not c.events


def test_a_gap_across_the_wrap_is_counted(registry, store):
    c = Collect(store)
    seqs = [0xFFFFFFFD, 0xFFFFFFFE, 1, 2]              # 0xFFFFFFFF and 0 are missing
    c.feed([pkt(registry, s, base_ms=10_000 + 100 * i) for i, s in enumerate(seqs)])
    assert c.rx.stats["lost"] == 2


def test_fake_car_loss_matches_the_receivers_count(registry, store):
    import random
    packets = fake_packets(registry, duration=30.0)
    sent = fake_car.impair(packets, random.Random(5), loss=0.2)
    c = Collect(store)
    c.feed(sent)
    seqs = sorted(m.decode_packet(p).seq for p in sent)
    assert c.rx.stats["lost"] == (seqs[-1] - seqs[0] + 1) - len(seqs) > 0


def test_a_repeat_is_dropped_not_delivered_twice(registry, store):
    c = Collect(store)
    p1 = pkt(registry, 1)
    assert c.feed([pkt(registry, 0), p1, p1, pkt(registry, 2), p1]) == [True, True, False, True, False]
    assert counters(c) == [0, 1, 2]
    assert c.rx.stats["duplicates"] == 2 and c.rx.stats["lost"] == 0


def test_a_reordered_packet_fills_its_gap(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s) for s in (0, 1, 3, 2, 4)])
    assert counters(c) == [0, 1, 3, 2, 4]
    assert c.rx.stats["lost"] == 0 and c.rx.stats["late"] == 1
    assert c.kinds() == ["gap", "late"]


def test_fake_car_reordering_and_duplicates_lose_nothing(registry, store):
    import random
    packets = fake_packets(registry, duration=30.0)
    c = Collect(store)
    c.feed(fake_car.impair(packets, random.Random(2), reorder=0.2, duplicate=0.2))
    assert c.rx.stats["lost"] == 0 and c.rx.stats["delivered"] == len(packets)
    assert c.rx.stats["late"] > 0 and c.rx.stats["duplicates"] > 0
    assert sorted(counters(c)) == list(range(len(packets)))


def test_backfilled_packets_fill_the_gap_and_are_marked(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s) for s in range(0, 10)])
    c.feed([pkt(registry, s) for s in range(20, 30)])            # 10-19 were buffered on the device
    assert c.rx.stats["lost"] == 10
    c.feed([pkt(registry, s, flags=m.FLAG_BACKFILL) for s in range(10, 20)])
    assert c.rx.stats["lost"] == 0 and c.rx.stats["late"] == 10 and c.rx.stats["reboots"] == 0
    assert [v.backfill for v in c.values] == [False] * 20 + [True] * 10
    assert sorted(counters(c)) == list(range(30))


def test_old_backfill_from_before_the_receiver_started_is_not_a_reboot(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s) for s in range(50, 55)])
    c.feed([pkt(registry, s, flags=m.FLAG_BACKFILL) for s in range(40, 45)])
    assert c.rx.stats["reboots"] == 0 and c.rx.stats["late"] == 5 and len(c.values) == 10


def test_a_device_reboot_restarts_seq_tracking_and_the_timebase(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s, base_ms=600_000 + 100 * s) for s in range(0, 50)], arrival=T0)
    c.feed([pkt(registry, s, base_ms=2_000 + 100 * s) for s in range(0, 5)], arrival=T0 + 100)
    assert c.rx.stats["reboots"] == 1 and c.rx.stats["lost"] == 0 and c.rx.stats["duplicates"] == 0
    assert len(c.values) == 55
    after = c.values[50]
    assert after.wall_time == pytest.approx(T0 + 100)            # re-anchored to the new arrival
    c.feed([pkt(registry, 6, base_ms=2_600)], arrival=T0 + 100.6)
    assert c.rx.stats["lost"] == 1                                # seq 5 missing after the reboot


def test_a_reboot_onto_a_seq_that_was_missing_is_still_a_reboot(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s, base_ms=600_000 + 100 * s) for s in (0, 1, 2, 5)])   # 3 and 4 missing
    c.feed([pkt(registry, s, base_ms=1_500 + 100 * s) for s in (3, 4, 5)])        # new boot, seq 3
    assert c.rx.stats["reboots"] == 1 and c.rx.stats["late"] == 0 and c.rx.stats["duplicates"] == 0


def test_reordering_right_after_start_is_not_a_reboot(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s) for s in (5, 4, 6)])
    assert c.rx.stats["reboots"] == 0 and c.rx.stats["late"] == 1 and counters(c) == [5, 4, 6]


def test_devices_are_tracked_separately(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s, device=d) for s in range(5) for d in (20, 21)])
    assert c.rx.stats["lost"] == 0 and c.rx.stats["duplicates"] == 0
    assert {v.device_id for v in c.values} == {20, 21}


# ---------------------------------------------------------------- done when: the packet's registry version
def test_values_decode_with_the_registry_version_in_the_packet(registry):
    old = copy.deepcopy(registry)
    old["registry_version"] = registry["registry_version"] - 1
    ch100 = next(ch for ch in old["channels"] if ch["id"] == 100)
    ch100.update(name="engine.rpm_v1", unit="krpm")
    s = RegistryStore()
    s.add(old)
    s.add(registry)
    c = Collect(s)
    c.feed([pkt(registry, 0, version=old["registry_version"], values=[(100, 0, 8.0)]),
            pkt(registry, 1, values=[(100, 0, 8000.0)])])
    assert [(v.registry_version, v.name, v.unit) for v in c.values] == [
        (old["registry_version"], "engine.rpm_v1", "krpm"),
        (registry["registry_version"], "engine.speed_rpm", "rpm")]


def test_an_unknown_registry_version_is_dropped_and_counted(registry, store):
    c = Collect(store)
    assert c.feed([pkt(registry, 0, version=77)]) == [False]
    assert c.rx.stats["unknown_version"] == 1 and not c.values
    assert ("unknown_version", 20, 77) in c.events


def test_a_channel_missing_from_its_version_is_skipped(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, 0, values=[(1, 0, 5.0), (9999, 0, 1.0), (100, 0, 8000.0)])])
    assert [v.channel_id for v in c.values] == [1, 100]
    assert c.rx.decoder.unknown_channel == 1


def golden_store(cases):
    """A registry holding every channel id the golden DATA packets use."""
    ids = {r["channel_id"] for case in cases for p in case["packets"] for r in p["records"]}
    reg = {"registry_version": 1, "params": [], "channels": [
        {"id": i, "name": f"ch{i}", "unit": "u"} for i in sorted(ids)]}
    s = RegistryStore()
    s.add(reg)
    return s


def f32_bits(x):
    return f"0x{struct.unpack('<I', struct.pack('<f', x))[0]:08x}"


@pytest.mark.parametrize("case", golden("data"), ids=lambda c: c["name"])
def test_golden_data_packets_decode_exactly(case):
    c = Collect(golden_store(golden("data")))
    for p in case["packets"]:
        before = len(c.values)
        assert c.feed([bytes.fromhex(p["hex"])]) == [True]
        got = c.values[before:]
        assert [(v.channel_id, v.device_ms - p["header"]["base_ms"], f32_bits(v.value), v.quality) for v in got] == [
            (r["channel_id"], r["dt_ms"], r["value_bits"], r["quality"]) for r in p["records"]]
        assert all(v.backfill == bool(p["header"]["flags"] & m.FLAG_BACKFILL) for v in got)


@pytest.mark.parametrize("case", golden("raw_can"), ids=lambda c: c["name"])
def test_golden_raw_can_packets_decode_exactly(store, case):
    c = Collect(store)
    for p in case["packets"]:
        before = len(c.raw)
        assert c.feed([bytes.fromhex(p["hex"])]) == [True]
        want = [(int(r["can_id"], 16) & 0x7FFFFFFF, bool(int(r["can_id"], 16) >> 31), r["bus"],
                 bytes.fromhex(r["data"])[:r["dlc"]]) for r in p["records"]]
        assert [(f.can_id, f.extended, f.bus, f.data) for f in c.raw[before:]] == want


# ---------------------------------------------------------------- message types
def test_command_heartbeat_and_unknown_types_are_rejected(registry, store):
    c = Collect(store)
    empty = [m.encode_packet(t, [], device_id=20, registry_version=2, seq=0, base_ms=0)
             for t in (m.COMMAND, m.HEARTBEAT, 99)]
    with_payload = m.encode_packet(m.COMMAND, [b"\x01\x02\x03"], device_id=20, registry_version=2, seq=0, base_ms=0)
    assert c.feed(empty + [with_payload]) == [False] * 4
    assert c.rx.stats["rejected_type"] == 4 and not c.rx.corrupt and not c.rx.streams


@pytest.mark.parametrize("name", ["config", "ack"])
def test_config_and_ack_packets_are_ignored(store, name):
    c = Collect(store)
    packets = [bytes.fromhex(p["hex"]) for case in golden(name) for p in case["packets"]]
    assert not any(c.feed(packets))
    assert c.rx.stats["ignored"] == len(packets) and not c.rx.streams


# ---------------------------------------------------------------- UDP and the summary
def test_udp_loopback_with_fake_car(registry, store):
    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.bind(("127.0.0.1", 0))
    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        stats = fake_car.run(registry, lambda p: tx_sock.sendto(p, rx_sock.getsockname()), duration=3.0, speed=0)
        values = []
        rx = Receiver(store, on_value=values.append)
        serve(rx_sock, rx, idle_exit=0.3)
    finally:
        rx_sock.close()
        tx_sock.close()
    assert rx.stats["delivered"] == stats["sent"] and len(values) == stats["samples"]
    assert all(v.wall_time is not None for v in values)


def test_command_line_receives_fake_car_packets(registry, capsys):
    import threading
    import receiver
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    t = threading.Thread(target=receiver.main, args=(["--host", "127.0.0.1", "--port", str(port),
                                                      "--idle-exit", "0.5"],))
    t.start()
    try:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(50):                              # wait until it is listening
            if "listening" in capsys.readouterr().out:
                break
            threading.Event().wait(0.02)
        fake_car.run(registry, lambda p: tx.sendto(p, ("127.0.0.1", port)), duration=1.0, speed=0)
        tx.close()
    finally:
        t.join(5)
    out = capsys.readouterr().out
    assert "engine.speed_rpm" in out and "10 packets: 10 delivered (80 values" in out


def test_command_line_needs_a_registry(tmp_path):
    import receiver
    with pytest.raises(SystemExit):
        receiver.main(["--registry", str(tmp_path / "none.yaml"), "--archive", str(tmp_path)])


def test_summary_reports_the_counts(registry, store):
    c = Collect(store)
    c.feed([pkt(registry, s) for s in (0, 1, 3)] + [b"junk"])
    text = summary(c.rx)
    assert "4 packets: 3 delivered" in text and "1 lost (25.0%)" in text and "too_short" in text
