"""Run:  python3 test_afr_msg.py [path/to/M1_General.dbc]"""
import os
import sys
import random

import afr_msg as m

HERE = os.path.dirname(os.path.abspath(__file__))
DBC = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("M1_DBC", "")


def test_registry_loads():
    reg = m.load_registry(os.path.join(HERE, "registry.yaml"))
    assert reg["registry_version"] >= 1
    assert {c["id"] for c in reg["channels"]} >= {1, 2, 100, 101, 102}


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


def test_sizes():
    assert m.HEADER.size == 20 and m.FOOTER.size == 4
    assert (m.SAMPLE.size, m.RAWCAN.size, m.CFGREC.size, m.ACKREC.size) == (9, 16, 6, 5)


def test_crc_detects_corruption():
    pk = m.Packetizer(20, 1)
    (buf,) = pk.build(m.DATA, [(0, 1, 1.0, 0)])
    for i in range(len(buf)):
        bad = bytearray(buf)
        bad[i] ^= 0x01
        try:
            m.decode_packet(bytes(bad))
        except m.PacketError:
            continue
        raise AssertionError(f"corruption at byte {i} not detected")


def test_splitting_and_sequence():
    pk = m.Packetizer(20, 1, max_packet_bytes=1200)
    items = [(5000 + i, 1, float(i), 0) for i in range(300)]
    bufs = pk.build(m.DATA, items)
    per = (1200 - m.OVERHEAD) // m.SAMPLE.size
    assert per == 130 and len(bufs) == 3 and all(len(b) <= 1200 for b in bufs)
    pkts = [m.decode_packet(b) for b in bufs]
    assert [p.seq for p in pkts] == [0, 1, 2]
    assert sum(len(p.records) for p in pkts) == 300
    # times reconstruct correctly
    t = [m.abs_time_ms(p, r[1]) for p in pkts for r in p.records]
    assert t == sorted(t) and t[0] == 5000 and t[-1] == 5299


def test_raw_can_roundtrip():
    pk = m.Packetizer(20, 1)
    (buf,) = pk.build(m.RAW_CAN, [(10, 0x640, 0, 8, bytes(range(8))),
                                   (12, 0x1FFFFFFF | m.CAN_EXTENDED, 1, 3, b"\x01\x02\x03")])
    p = m.decode_packet(buf)
    assert p.records[0] == (0, 0x640, 0, 8, bytes(range(8)))
    assert p.records[1][1] == 0x1FFFFFFF | m.CAN_EXTENDED and p.records[1][3] == 3


def test_config_and_ack():
    rec = [m.CFGREC.pack(1, 50.0)]
    buf = m.encode_packet(m.CONFIG, rec, device_id=20, registry_version=1, seq=7, base_ms=0)
    p = m.decode_packet(buf)
    assert p.records == [(1, 50.0)]
    ack = m.encode_packet(m.ACK, [m.ACKREC.pack(7, m.ACK_APPLIED)], device_id=20,
                          registry_version=1, seq=8, base_ms=0)
    assert m.decode_packet(ack).records == [(7, m.ACK_APPLIED)]


def test_ring_drops_oldest():
    r = m.PacketRing(3)
    for i in range(5):
        r.push(bytes([i]))
    assert r.dropped == 2 and r.pop_oldest() == bytes([2]) and len(r) == 2


def test_gap_detection_and_wrap():
    assert m.missing_between(10, 11) == 0
    assert m.missing_between(10, 14) == 3
    assert m.missing_between(0xFFFFFFFF, 0) == 0          # wraps cleanly
    assert m.missing_between(0xFFFFFFFE, 1) == 2


def test_unknown_msg_type_with_payload_rejected():
    buf = m.encode_packet(m.COMMAND, [b"\x01"], device_id=20, registry_version=1, seq=0, base_ms=0)
    try:
        m.decode_packet(buf)
    except m.PacketError:
        return
    raise AssertionError("COMMAND must be rejected in v1")


def test_bad_magic_and_version():
    pk = m.Packetizer(20, 1)
    (buf,) = pk.build(m.DATA, [(0, 1, 1.0, 0)])
    for bad in (b"\x00\x00" + buf[2:], buf[:2] + b"\x09" + buf[3:]):
        try:
            m.decode_packet(bad)
        except m.PacketError:
            continue
        raise AssertionError("bad header accepted")


def test_can_extraction_against_cantools():
    """Registry CAN channels must agree with the real M130 DBC, and our bit extraction must
    agree with cantools."""
    import cantools
    db = cantools.database.load_file(DBC)
    reg = m.load_registry(os.path.join(HERE, "registry.yaml"))
    rng = random.Random(1)
    checked = 0
    for ch in reg["channels"]:
        if ch["source"] != "can" or "can_id" not in ch["src"] or "mux" in ch["src"]:
            continue
        s = ch["src"]
        msg = db.get_message_by_frame_id(s["can_id"])
        sig = next(x for x in msg.signals if x.start == s["start_bit"] and x.length == s["length"])
        # registry matches DBC definition
        assert sig.byte_order == s["byte_order"] and sig.is_signed == s["signed"]
        assert abs(sig.scale - s["scale"]) < 1e-9 and abs(sig.offset - s["offset"]) < 1e-9, ch["name"]
        # extraction matches cantools on random frames
        for _ in range(200):
            data = bytes(rng.randrange(256) for _ in range(8))
            want = msg.decode(data, decode_choices=False, scaling=True)[sig.name]
            got = m.extract_signal(data, s["start_bit"], s["length"], s["byte_order"],
                                   s["signed"], s["scale"], s["offset"])
            assert abs(want - got) < 1e-6, (ch["name"], data.hex(), want, got)
        checked += 1
    assert checked >= 3, checked


def test_extraction_every_signal_in_dbc():
    """Stronger check: our extractor vs cantools for EVERY non-multiplexed signal in the DBC
    (covers signed and sub-byte big-endian fields)."""
    import cantools
    db = cantools.database.load_file(DBC)
    rng = random.Random(2)
    n = 0
    for msg in db.messages:
        if msg.is_multiplexed() or msg.length < 8 or msg.frame_id >= 0x80000000:
            continue
        for sig in msg.signals:
            if sig.byte_order != "big_endian":
                continue
            for _ in range(50):
                data = bytes(rng.randrange(256) for _ in range(msg.length))
                want = msg.decode(data, decode_choices=False, scaling=True)[sig.name]
                got = m.extract_signal(data, sig.start, sig.length, sig.byte_order,
                                       sig.is_signed, sig.scale, sig.offset)
                assert abs(want - got) < 1e-6, (msg.name, sig.name, data.hex(), want, got)
            n += 1
    assert n > 100, n
    print(f"  extractor agrees with cantools on {n} DBC signals")


def _load_dbcs():
    import cantools
    m130 = cantools.database.load_file(DBC)
    e888 = cantools.database.load_file(os.path.join(os.path.dirname(DBC), "E8xx.dbc"))
    return m130, e888


def test_registry_can_channels_match_dbcs():
    """EVERY CAN channel in the registry (active and planned, M130 and E888 incl. multiplexed)
    must match its DBC definition and extract the same values as cantools."""
    m130, e888 = _load_dbcs()
    reg = m.load_registry(os.path.join(HERE, "registry.yaml"))
    rng = random.Random(3)
    n = 0
    for ch in reg["channels"]:
        if ch["source"] != "can":
            continue
        s = ch["src"]
        db = e888 if "can_offset" in s else m130
        msg = db.get_message_by_frame_id(m.can_frame_id(ch, reg))
        mux = s.get("mux")
        sigs = [x for x in msg.signals if x.start == s["start_bit"] and x.length == s["length"]
                and (mux is None or x.multiplexer_ids == [mux["value"]])]
        assert len(sigs) == 1, (ch["name"], [x.name for x in sigs])
        sig = sigs[0]
        assert sig.byte_order == s["byte_order"] and sig.is_signed == s["signed"], ch["name"]
        assert abs(sig.scale - s["scale"]) < 1e-9 and abs(sig.offset - s["offset"]) < 1e-9, ch["name"]
        for _ in range(20):
            data = bytearray(rng.randrange(256) for _ in range(msg.length))
            if mux is not None:
                sel = next(x for x in msg.signals if x.is_multiplexer)
                assert (sel.start, sel.length) == (mux["start_bit"], mux["length"]), ch["name"]
                # force the selector to the channel's mux value (selectors are big-endian)
                for i in range(mux["length"]):
                    p = mux["start_bit"] - i if i <= mux["start_bit"] % 8 else None
                    assert p is not None
                    bit = (mux["value"] >> (mux["length"] - 1 - i)) & 1
                    data[p // 8] = (data[p // 8] & ~(1 << (p % 8))) | (bit << (p % 8))
            want = msg.decode(bytes(data), decode_choices=False, scaling=True)[sig.name]
            got = m.extract_channel(bytes(data), ch)
            assert got is not None and abs(want - got) < 1e-6, (ch["name"], bytes(data).hex(), want, got)
        n += 1
    assert n >= 3, n
    print(f"  {n} registry CAN channels agree with the DBCs")


def test_importer_is_idempotent():
    """Re-running tools/import_can_csv.py on the committed registry must add nothing."""
    import subprocess
    script = os.path.join(HERE, "..", "tools", "import_can_csv.py")
    out = subprocess.run([sys.executable, script, "--dry-run"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0 channels to add", out.stdout


def test_mux_wrong_selector_returns_none():
    ch = {"src": {"start_bit": 15, "length": 8, "byte_order": "big_endian", "signed": False,
                  "scale": 1.0, "offset": 0.0, "mux": {"start_bit": 7, "length": 2, "value": 1}}}
    assert m.extract_channel(bytes([0b01000000, 42, 0, 0, 0, 0, 0, 0]), ch) == 42.0
    assert m.extract_channel(bytes([0b10000000, 42, 0, 0, 0, 0, 0, 0]), ch) is None


def test_registry_rejects_bad_can_src():
    import tempfile
    import yaml
    reg = m.load_registry(os.path.join(HERE, "registry.yaml"))
    good = next(c for c in reg["channels"] if c["source"] == "can")
    bad_srcs = [
        {k: v for k, v in good["src"].items() if k != "scale"},                    # missing key
        {**good["src"], "can_offset": 1},                                           # both id forms
        {**good["src"], "byte_order": "middle_endian"},                             # bad order
        {**good["src"], "mux": {"start_bit": 7, "length": 2, "value": 4}},          # value too big
    ]
    for src in bad_srcs:
        broken = {**reg, "channels": [{**good, "src": src}]}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(broken, f)
        try:
            m.load_registry(f.name)
        except ValueError:
            continue
        finally:
            os.unlink(f.name)
        raise AssertionError(f"bad src accepted: {src}")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        if "cantools" in name or "every_signal" in name or "match_dbcs" in name or "importer" in name:
            if not DBC:
                print(f"SKIP {name} (no DBC path given)")
                continue
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
