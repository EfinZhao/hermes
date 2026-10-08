"""Packet codec: encode/decode, packetizer, ring buffer, sequence gaps."""
import pytest

import afr_msg as m


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


def test_crc_vector():
    import zlib
    assert zlib.crc32(b"123456789") == 0xCBF43926


def test_spec_example_packet():
    """The 33-byte example from the spec: channel 100 = 8000.0 at 1000 ms."""
    header = "20 af 01 00 14 00 01 00 00 00 00 00 e8 03 00 00 01 00 09 00"
    record = "64 00 00 00 00 00 fa 45 00"
    crc = "92 6e be fa"
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
    assert len(bufs) == 3 and all(len(b) <= 1200 for b in bufs)
    pkts = [m.decode_packet(b) for b in bufs]
    assert [p.seq for p in pkts] == [0, 1, 2]
    assert sum(len(p.records) for p in pkts) == 300
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
])
def test_gap_detection_and_wrap(last, new, missing):
    assert m.missing_between(last, new) == missing


def test_command_with_payload_rejected():
    buf = m.encode_packet(m.COMMAND, [b"\x01"], device_id=20, registry_version=1, seq=0, base_ms=0)
    with pytest.raises(m.PacketError):
        m.decode_packet(buf)


def test_bad_magic_and_version_rejected():
    (buf,) = m.Packetizer(20, 1).build(m.DATA, [(0, 1, 1.0, 0)])
    for bad in (b"\x00\x00" + buf[2:], buf[:2] + b"\x09" + buf[3:]):
        with pytest.raises(m.PacketError):
            m.decode_packet(bad)
