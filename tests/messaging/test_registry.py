"""Registry loading/validation and CAN signal extraction against the real DBCs."""
import random

import pytest
import yaml

import msgcodec as m
from conftest import MESSAGING


def test_registry_loads(registry):
    assert registry["registry_version"] >= 1
    assert {c["id"] for c in registry["channels"]} >= {1, 2, 100, 101, 102}


def test_ids_and_names_unique(registry):
    ids = [c["id"] for c in registry["channels"]]
    names = [c["name"] for c in registry["channels"]]
    assert len(ids) == len(set(ids)) and len(names) == len(set(names))


def _can_channels(registry):
    return [c for c in registry["channels"] if c["source"] == "can"]


def _bad_src_cases(good):
    return {
        "missing_key": {k: v for k, v in good["src"].items() if k != "scale"},
        "both_id_forms": {**good["src"], "can_offset": 1},
        "offset_without_base": {k: v for k, v in good["src"].items() if k != "can_id"} | {"can_offset": 1},
        "unknown_base": {k: v for k, v in good["src"].items() if k != "can_id"} | {"can_base": "nope", "can_offset": 1},
        "bad_byte_order": {**good["src"], "byte_order": "middle_endian"},
        "mux_value_too_big": {**good["src"], "mux": {"start_bit": 7, "length": 2, "value": 4}},
    }


@pytest.mark.parametrize("case", ["missing_key", "both_id_forms", "offset_without_base", "unknown_base", "bad_byte_order", "mux_value_too_big"])
def test_registry_rejects_bad_can_src(registry, tmp_path, case):
    good = _can_channels(registry)[0]
    broken = {**registry, "channels": [{**good, "src": _bad_src_cases(good)[case]}]}
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(broken))
    with pytest.raises(ValueError):
        m.load_registry(str(path))


def test_empty_registry_gives_clear_error(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    with pytest.raises(ValueError, match="not a registry"):
        m.load_registry(str(path))


def test_mux_wrong_selector_returns_none():
    ch = {"src": {"start_bit": 15, "length": 8, "byte_order": "big_endian", "signed": False,
                  "scale": 1.0, "offset": 0.0, "mux": {"start_bit": 7, "length": 2, "value": 1}}}
    assert m.extract_channel(bytes([0b01000000, 42, 0, 0, 0, 0, 0, 0]), ch) == 42.0
    assert m.extract_channel(bytes([0b10000000, 42, 0, 0, 0, 0, 0, 0]), ch) is None


def _set_selector(data: bytearray, mux: dict) -> None:
    """Write the (big-endian, within one byte) multiplexer selector into a frame."""
    for i in range(mux["length"]):
        p = mux["start_bit"] - i
        bit = (mux["value"] >> (mux["length"] - 1 - i)) & 1
        data[p // 8] = (data[p // 8] & ~(1 << (p % 8))) | (bit << (p % 8))


def _channel_ids(registry):
    return [pytest.param(c, id=c["name"]) for c in _can_channels(registry)]


def pytest_generate_tests(metafunc):
    if "can_channel" in metafunc.fixturenames:
        reg = m.load_registry(str(MESSAGING / "registry.yaml"))
        metafunc.parametrize("can_channel", _channel_ids(reg))


def test_can_channel_matches_dbc(can_channel, registry, m130_db, e888_db):
    """Every CAN channel (active and planned, M130 and multiplexed E888) matches its DBC
    definition and extracts the same values as cantools."""
    ch, s = can_channel, can_channel["src"]
    db = e888_db if "can_base" in s else m130_db     # this repo has one offset-addressed device, the E888
    msg = db.get_message_by_frame_id(m.can_frame_id(ch, registry))
    mux = s.get("mux")
    sigs = [x for x in msg.signals if x.start == s["start_bit"] and x.length == s["length"]
            and (mux is None or x.multiplexer_ids == [mux["value"]])]
    assert len(sigs) == 1, [x.name for x in sigs]
    sig = sigs[0]
    assert sig.byte_order == s["byte_order"] and sig.is_signed == s["signed"]
    assert abs(sig.scale - s["scale"]) < 1e-9 and abs(sig.offset - s["offset"]) < 1e-9
    rng = random.Random(ch["id"])
    for _ in range(20):
        data = bytearray(rng.randrange(256) for _ in range(msg.length))
        if mux is not None:
            selector = next(x for x in msg.signals if x.is_multiplexer)
            assert (selector.start, selector.length) == (mux["start_bit"], mux["length"])
            _set_selector(data, mux)
        want = msg.decode(bytes(data), decode_choices=False, scaling=True)[sig.name]
        got = m.extract_channel(bytes(data), ch)
        assert got is not None and abs(want - got) < 1e-6, (bytes(data).hex(), want, got)


def test_extractor_matches_cantools_for_every_m130_signal(m130_db):
    """Our bit extractor vs cantools for every non-multiplexed big-endian M130 signal
    (covers signed and sub-byte fields)."""
    rng = random.Random(2)
    n = 0
    for msg in m130_db.messages:
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
