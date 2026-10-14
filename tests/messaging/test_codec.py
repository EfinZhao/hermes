"""Packet codec: encode/decode, packetizer, ring buffer, sequence gaps."""
import pytest

import msgcodec as m


def test_sizes():
    assert m.HEADER.size == 20 and m.FOOTER.size == 4
    assert (m.SAMPLE.size, m.RAWCAN.size, m.CFGREC.size, m.ACKREC.size) == (9, 16, 6, 5)


def test_roundtrip_data():
    pk = m.Packetizer(device_id=20, registry_version=1)
    items = [(1000 + i * 10, 100, 8000.0 + i, 0) for i in range(5)] + [(1050, 101, 91.5, m.Q_CLAMPED)]
    (buf,) = pk.build(m.DATA, items)
    p = m.decode_packet(buf)
    assert (p.msg_type, p.device_id, p.registry_version, p.seq, p.base_ms) == (m.DATA, 20, 1, 0, 1000)
    assert len(p.records) == 6
    chan, dt, val, q = p.records[0]
    assert (chan, dt, q) == (100, 0, 0) and val == 8000.0
    assert any(r[0] == 101 and r[2] == 91.5 and r[3] == m.Q_CLAMPED for r in p.records)
    assert len(buf) == m.OVERHEAD + 6 * m.SAMPLE.size


def crc32_reference(data: bytes) -> int:
    """Bit-by-bit CRC-32 (IEEE), independent of zlib, as the firmware will have to write it."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def test_reference_crc_matches_the_standard_vector():
    assert crc32_reference(b"123456789") == 0xCBF43926


def test_packet_footer_is_the_crc_of_header_and_records():
    (buf,) = m.Packetizer(20, 1).build(m.DATA, [(0, 1, 1.0, 0), (5, 2, 2.0, 0)])
    assert int.from_bytes(buf[-4:], "little") == crc32_reference(buf[:-4])


def test_spec_example_packet():
    """The 33-byte example from the spec: device 20, channel 100 = 8000.0 at 1000 ms."""
    header = "48 4d 01 00 14 00 01 00 00 00 00 00 e8 03 00 00 01 00 09 00"
    record = "64 00 00 00 00 00 fa 45 00"
    crc = "f6 19 66 76"
    want = bytes.fromhex(f"{header} {record} {crc}")
    (buf,) = m.Packetizer(device_id=20, registry_version=1).build(m.DATA, [(1000, 100, 8000.0, 0)])
    assert buf == want


def test_crc_detects_every_single_bit_flip():
    (buf,) = m.Packetizer(20, 1).build(m.DATA, [(0, 1, 1.0, 0)])
    for i in range(len(buf)):
        bad = bytearray(buf)
        bad[i] ^= 0x01
        with pytest.raises(m.PacketError):
            m.decode_packet(bytes(bad))


def test_splitting_and_sequence():
    pk = m.Packetizer(20, 1, max_packet_bytes=1200)
    bufs = pk.build(m.DATA, [(5000 + i, 1, float(i), 0) for i in range(300)])
    assert (1200 - m.OVERHEAD) // m.SAMPLE.size == 130
    assert [len(b) for b in bufs] == [1194, 1194, 384]          # 24 + 9 * (130, 130, 40)
    pkts = [m.decode_packet(b) for b in bufs]
    assert [len(p.records) for p in pkts] == [130, 130, 40]
    assert [p.seq for p in pkts] == [0, 1, 2]
    t = [m.abs_time_ms(p, r[1]) for p in pkts for r in p.records]
    assert t == sorted(t) and t[0] == 5000 and t[-1] == 5299


def test_raw_can_roundtrip():
    (buf,) = m.Packetizer(20, 1).build(m.RAW_CAN, [(10, 0x640, 0, 8, bytes(range(8))),
                                                   (12, 0x1FFFFFFF | m.CAN_EXTENDED, 1, 3, b"\x01\x02\x03")])
    p = m.decode_packet(buf)
    assert p.records[0] == (0, 0x640, 0, 8, bytes(range(8)))
    assert p.records[1][1] == 0x1FFFFFFF | m.CAN_EXTENDED and p.records[1][3] == 3


def test_config_and_ack():
    buf = m.encode_packet(m.CONFIG, [m.CFGREC.pack(1, 50.0)], device_id=20, registry_version=1, seq=7, base_ms=0)
    assert m.decode_packet(buf).records == [(1, 50.0)]
    ack = m.encode_packet(m.ACK, [m.ACKREC.pack(7, m.ACK_APPLIED)], device_id=20,
                          registry_version=1, seq=8, base_ms=0)
    assert m.decode_packet(ack).records == [(7, m.ACK_APPLIED)]


def test_ring_drops_oldest():
    r = m.PacketRing(3)
    for i in range(5):
        r.push(bytes([i]))
    assert r.dropped == 2 and r.pop_oldest() == bytes([2]) and len(r) == 2


@pytest.mark.parametrize("last, new, missing", [
    (10, 11, 0), (10, 14, 3), (0xFFFFFFFF, 0, 0), (0xFFFFFFFE, 1, 2),
    (10, 10 + (1 << 31) - 1, (1 << 31) - 2),               # furthest ahead that still counts as newer
])
def test_missing_between_counts_skipped_packets(last, new, missing):
    assert m.missing_between(last, new) == missing


@pytest.mark.parametrize("last, new", [
    (10, 10),                       # duplicate
    (10, 9), (10, 0),               # older / reordered
    (0, 0xFFFFFFFF),                # one behind across the wrap
    (10, 10 + (1 << 31)),           # exactly half the range away counts as old
])
def test_missing_between_is_none_for_duplicates_and_older_packets(last, new):
    assert m.missing_between(last, new) is None


def test_command_with_payload_rejected():
    buf = m.encode_packet(m.COMMAND, [b"\x01"], device_id=20, registry_version=1, seq=0, base_ms=0)
    with pytest.raises(m.PacketError):
        m.decode_packet(buf)


def with_valid_crc(body: bytes) -> bytes:
    return body + m.FOOTER.pack(crc32_reference(body))


def test_bad_magic_rejected():
    (buf,) = m.Packetizer(20, 1).build(m.DATA, [(0, 1, 1.0, 0)])
    with pytest.raises(m.PacketError, match="bad magic"):
        m.decode_packet(with_valid_crc(b"\x00\x00" + buf[2:-4]))


def test_unsupported_format_version_rejected():
    (buf,) = m.Packetizer(20, 1).build(m.DATA, [(0, 1, 1.0, 0)])
    with pytest.raises(m.PacketError, match="unsupported format version 9"):
        m.decode_packet(with_valid_crc(buf[:2] + b"\x09" + buf[3:-4]))


# ---------------------------------------------------------------- packetizer edge cases
def decode_all(bufs):
    return [m.decode_packet(b) for b in bufs]


def test_items_out_of_order_are_sorted_by_time():
    items = [(1030, 1, 3.0, 0), (1000, 1, 1.0, 0), (1020, 1, 2.0, 0)]
    (pkt,) = decode_all(m.Packetizer(20, 1).build(m.DATA, items))
    assert pkt.base_ms == 1000
    assert [r[2] for r in pkt.records] == [1.0, 2.0, 3.0] and [r[1] for r in pkt.records] == [0, 20, 30]


def test_equal_timestamps_keep_input_order():
    items = [(5, 1, 1.0, 0), (5, 2, 2.0, 0), (5, 3, 3.0, 0)]
    (pkt,) = decode_all(m.Packetizer(20, 1).build(m.DATA, items))
    assert [r[0] for r in pkt.records] == [1, 2, 3]


def test_backfill_flag_is_set_on_every_packet():
    bufs = m.Packetizer(20, 1).build(m.DATA, [(i, 1, 0.0, 0) for i in range(200)], flags=m.FLAG_BACKFILL)
    assert all(p.flags == m.FLAG_BACKFILL for p in decode_all(bufs))
    (plain,) = decode_all(m.Packetizer(20, 1).build(m.DATA, [(0, 1, 0.0, 0)]))
    assert plain.flags == 0


def test_sequence_number_wraps_at_32_bits():
    pk = m.Packetizer(20, 1)
    pk.seq = 0xFFFFFFFF
    bufs = pk.build(m.DATA, [(i, 1, 0.0, 0) for i in range(300)])
    assert [p.seq for p in decode_all(bufs)] == [0xFFFFFFFF, 0, 1]
    assert pk.seq == 2


def test_empty_input_gives_no_packets_and_keeps_seq():
    pk = m.Packetizer(20, 1)
    assert pk.build(m.DATA, []) == [] and pk.seq == 0


def test_records_too_far_apart_in_time_are_rejected():
    pk = m.Packetizer(20, 1)
    with pytest.raises(ValueError, match="65.5 s"):
        pk.build(m.DATA, [(0, 1, 0.0, 0), (0x10000, 1, 0.0, 0)])
    pk.build(m.DATA, [(0, 1, 0.0, 0), (0xFFFF, 1, 0.0, 0)])          # exactly 65535 ms is fine


def test_max_packet_bytes_too_small_for_one_record_is_rejected():
    with pytest.raises(ValueError, match="too small"):
        m.Packetizer(20, 1, max_packet_bytes=30).build(m.DATA, [(0, 1, 0.0, 0)])      # 24 + 9 > 30
    with pytest.raises(ValueError, match="too small"):
        m.Packetizer(20, 1, max_packet_bytes=39).build(m.RAW_CAN, [(0, 0x640, 0, 8, bytes(8))])  # 24 + 16 > 39


def test_raw_can_short_data_is_padded_with_zeros():
    (pkt,) = decode_all(m.Packetizer(20, 1).build(m.RAW_CAN, [(0, 0x640, 0, 3, b"\x01\x02\x03")]))
    dt, can_id, bus, dlc, data = pkt.records[0]
    assert dlc == 3 and data == b"\x01\x02\x03\x00\x00\x00\x00\x00"


def test_raw_can_data_longer_than_8_bytes_is_rejected_not_cut():
    with pytest.raises(ValueError, match="at most 8"):
        m.Packetizer(20, 1).build(m.RAW_CAN, [(0, 0x640, 0, 8, bytes(9))])
