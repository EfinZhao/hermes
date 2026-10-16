"""fake_car.py: the simulated device (packets), the network impairments, and a real UDP loopback."""
import random
import socket
from collections import Counter

import pytest

import fake_car
import msgcodec as m


@pytest.fixture(scope="module")
def reg():
    return m.load_registry(str(fake_car.DEFAULT_REGISTRY))


def make_packets(reg, duration=10.0, **kw):
    car = fake_car.FakeCar(reg, **kw)
    return [p for _, packets in car.batches(duration) for p in packets], car


def decode_all(packets):
    return [m.decode_packet(p) for p in packets]


def samples(packets):
    """(channel_id, absolute time ms, value, quality) of every record."""
    return [(r[0], m.abs_time_ms(pkt, r[1]), r[2], r[3]) for pkt in decode_all(packets) for r in pkt.records]


# ---------------------------------------------------------------- the device
def test_packets_decode_and_carry_the_registry_identity(reg):
    packets, _ = make_packets(reg)
    assert packets
    for pkt in decode_all(packets):
        assert pkt.msg_type == m.DATA and pkt.flags == 0
        assert (pkt.device_id, pkt.registry_version) == (reg["device_id"], reg["registry_version"])


def test_seq_counts_up_by_one_from_the_start_value(reg):
    packets, _ = make_packets(reg, start_seq=7)
    assert [p.seq for p in decode_all(packets)] == list(range(7, 7 + len(packets)))


def test_seq_wraps_at_32_bits(reg):
    packets, _ = make_packets(reg, start_seq=0xFFFFFFFE)
    seqs = [p.seq for p in decode_all(packets)]
    assert seqs[:3] == [0xFFFFFFFE, 0xFFFFFFFF, 0]
    assert all(m.missing_between(a, b) == 0 for a, b in zip(seqs, seqs[1:]))


def test_base_ms_is_device_time_since_boot_and_increases(reg):
    packets, car = make_packets(reg, boot_ms=12_345)
    pkts = decode_all(packets)
    assert pkts[0].base_ms == 12_345
    assert [p.base_ms for p in pkts] == sorted(p.base_ms for p in pkts)
    for pkt in pkts:                                    # records sit inside the batch window that made them
        window_start = car.boot_ms + (pkt.base_ms - car.boot_ms) // car.batch_ms * car.batch_ms
        assert all(window_start <= m.abs_time_ms(pkt, r[1]) < window_start + car.batch_ms for r in pkt.records)


def test_only_active_live_channels_are_sent(reg):
    packets, car = make_packets(reg, duration=2)
    expected = {c["id"] for c in reg["channels"] if c["status"] == "active" and c["live"]}
    assert {s[0] for s in samples(packets)} == expected == {c["id"] for c in car.channels}


def test_active_channels_that_are_not_live_and_planned_live_channels_are_not_sent(reg):
    import copy
    custom = copy.deepcopy(reg)
    by_id = {c["id"]: c for c in custom["channels"]}
    by_id[100]["live"] = False                           # active, log only (SD card)
    by_id[303]["live"] = True                            # planned: not compiled into the firmware
    packets, _ = make_packets(custom, duration=2)
    sent = {s[0] for s in samples(packets)}
    assert 100 not in sent and 303 not in sent and 101 in sent


def test_sample_counts_follow_the_registry_rate_and_decimation(reg):
    packets, _ = make_packets(reg, duration=10)
    counts = Counter(s[0] for s in samples(packets))
    by_id = {c["id"]: c for c in reg["channels"]}
    for cid, n in counts.items():
        ch = by_id[cid]
        sent_hz = ch["rate_hz"] / (ch.get("live_decimate", 1) if ch["priority"] == 2 else 1)
        assert n == round(10 * sent_hz), (ch["name"], n)
    assert counts[2] == 100                              # test.sine: 100 Hz, 1 of every 10 sent


def test_counter_channel_counts_up_by_one(reg):
    packets, _ = make_packets(reg, duration=3)
    values = [s[2] for s in sorted(samples(packets), key=lambda s: s[1]) if s[0] == 1]
    assert values == [float(i) for i in range(len(values))] and len(values) == 30


def test_values_stay_inside_the_registry_range_and_are_not_flagged(reg):
    packets, _ = make_packets(reg, duration=30)
    by_id = {c["id"]: c for c in reg["channels"]}
    for cid, _, value, quality in samples(packets):
        assert by_id[cid]["min"] <= value <= by_id[cid]["max"] and quality == 0


def test_values_actually_move(reg):
    packets, _ = make_packets(reg, duration=20)
    by_channel = {}
    for cid, _, value, _ in samples(packets):
        by_channel.setdefault(cid, set()).add(round(value, 3))
    assert all(len(v) > 10 for v in by_channel.values())


def test_packets_respect_max_packet_bytes(reg):
    packets, _ = make_packets(reg, batch_ms=60_000)     # one huge window forces splitting
    assert len(packets) > 1 and all(len(p) <= 1200 for p in packets)


def test_same_seed_gives_the_same_bytes_and_a_different_seed_does_not(reg):
    a, _ = make_packets(reg, duration=3, seed=4)
    b, _ = make_packets(reg, duration=3, seed=4)
    c, _ = make_packets(reg, duration=3, seed=5)
    assert a == b and a != c


# ---------------------------------------------------------------- the network
PACKETS = [i.to_bytes(2, "big") for i in range(1000)]       # sortable stand-ins for packets


def test_impair_with_everything_off_changes_nothing():
    assert fake_car.impair(PACKETS, random.Random(1)) == PACKETS


def test_loss_drops_roughly_that_fraction():
    out = fake_car.impair(PACKETS, random.Random(1), loss=0.3)
    assert 640 <= len(out) <= 760 and len(set(out)) == len(out)
    assert fake_car.impair(PACKETS, random.Random(1), loss=1.0) == []


def test_reorder_delays_packets_but_loses_none():
    out = fake_car.impair(PACKETS, random.Random(1), reorder=0.3)
    assert sorted(out) == sorted(PACKETS) and out != PACKETS
    late = sum(1 for a, b in zip(out, out[1:]) if a > b)
    assert late > 50
    assert max(out.index(p) - PACKETS.index(p) for p in PACKETS) <= 4        # at most about 3 places late


def test_duplicate_sends_some_twice():
    out = fake_car.impair(PACKETS, random.Random(1), duplicate=0.2)
    counts = Counter(out)
    assert set(counts) == set(PACKETS) and set(counts.values()) == {1, 2}
    assert 150 <= len(out) - len(PACKETS) <= 250


def test_impair_is_repeatable_for_a_seed():
    kw = dict(loss=0.1, reorder=0.1, duplicate=0.1)
    assert fake_car.impair(PACKETS, random.Random(9), **kw) == fake_car.impair(PACKETS, random.Random(9), **kw)


def test_loss_leaves_gaps_the_receiver_can_count(reg):
    packets, _ = make_packets(reg, duration=20)
    out = fake_car.impair(packets, random.Random(2), loss=0.25)
    seqs = [p.seq for p in decode_all(out)]
    missing = sum(m.missing_between(a, b) for a, b in zip(seqs, seqs[1:]))
    assert missing + len(seqs) <= len(packets) and missing > 0


# ---------------------------------------------------------------- whole program over UDP
def test_run_sends_real_udp_datagrams_that_decode(reg):
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        stats = fake_car.run(reg, lambda pkt: client.sendto(pkt, server.getsockname()),
                             duration=2, speed=0, loss=0.2, duplicate=0.1, seed=3)
        received = []
        while len(received) < stats["sent"]:
            received.append(server.recv(2048))
    finally:
        client.close()
        server.close()
    assert len(received) == stats["sent"]
    assert all(m.decode_packet(d).msg_type == m.DATA for d in received)
    assert stats["built"] - stats["dropped"] == len(set(received))


def test_run_paces_itself_in_real_time_unless_told_not_to(reg):
    import time
    t0 = time.monotonic()
    fake_car.run(reg, lambda pkt: None, duration=0.5, speed=1)
    assert 0.4 <= time.monotonic() - t0 <= 1.5
    t0 = time.monotonic()
    fake_car.run(reg, lambda pkt: None, duration=0.5, speed=0)
    assert time.monotonic() - t0 < 0.3


@pytest.mark.parametrize("flag", ["--loss", "--reorder", "--duplicate"])
def test_probabilities_outside_0_to_1_are_rejected(flag):
    with pytest.raises(SystemExit):
        fake_car.main([flag, "1.5"])
