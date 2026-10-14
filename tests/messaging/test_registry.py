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
    """name -> (broken src, text the error must contain)"""
    no_id = {k: v for k, v in good["src"].items() if k != "can_id"}
    return {
        "missing_key": ({k: v for k, v in good["src"].items() if k != "scale"}, "src missing ['scale']"),
        "both_id_forms": ({**good["src"], "can_offset": 1}, "exactly one of can_id / can_offset"),
        "offset_without_base": (no_id | {"can_offset": 1}, "can_base and can_offset go together"),
        "unknown_base": (no_id | {"can_base": "nope", "can_offset": 1}, "'nope' is not in the registry's can_bases"),
        "bad_byte_order": ({**good["src"], "byte_order": "middle_endian"}, "bad byte_order"),
        "mux_value_too_big": ({**good["src"], "mux": {"start_bit": 7, "length": 2, "value": 4}}, "bad mux block"),
    }


@pytest.mark.parametrize("case", ["missing_key", "both_id_forms", "offset_without_base", "unknown_base",
                                  "bad_byte_order", "mux_value_too_big"])
def test_registry_rejects_bad_can_src(registry, tmp_path, case):
    good = _can_channels(registry)[0]
    src, message = _bad_src_cases(good)[case]
    broken = {**registry, "channels": [{**good, "src": src}]}
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(broken))
    with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("]", r"\]")):
        m.load_registry(str(path))


# ---------------------------------------------------------------- validate_registry, one rule at a time
def small_registry():
    """A minimal valid registry: one param, one synthetic and one CAN channel."""
    channel = {"id": 1, "name": "test.counter", "unit": "count", "source": "synthetic", "rate_hz": 10,
               "priority": 1, "live": True, "log": True, "min": 0, "max": 100, "status": "active",
               "src": {"kind": "counter"}}
    can = {**channel, "id": 2, "name": "engine.speed_rpm", "source": "can",
           "src": {"can_id": 0x640, "bus": 0, "start_bit": 7, "length": 16, "byte_order": "big_endian",
                   "signed": False, "scale": 1.0, "offset": 0.0}}
    return {"registry_version": 1, "device_id": 1,
            "params": [{"id": 1, "name": "batch_window_ms", "default": 100, "min": 20, "max": 1000, "unit": "ms"}],
            "channels": [channel, can]}


def test_small_registry_is_valid():
    m.validate_registry(small_registry())


def edit_channel(**changes):
    def apply(reg):
        reg["channels"][0].update(changes)
    return apply


def edit_can_src(**changes):
    def apply(reg):
        reg["channels"][1]["src"].update(changes)
    return apply


def drop_channel_key(key):
    def apply(reg):
        del reg["channels"][0][key]
    return apply


def duplicate_param(reg):
    reg["params"].append({**reg["params"][0], "name": "other"})


def edit_param(**changes):
    def apply(reg):
        reg["params"][0].update(changes)
    return apply


def drop_param_key(key):
    def apply(reg):
        del reg["params"][0][key]
    return apply


REJECTIONS = {
    "id_zero": (edit_channel(id=0), "channel id out of range: 0"),
    "id_too_big": (edit_channel(id=32768), "channel id out of range: 32768"),
    "duplicate_id": (edit_channel(id=2), "duplicate channel id/name: 2"),
    "duplicate_name": (edit_channel(name="engine.speed_rpm"), "duplicate channel id/name: 2 engine.speed_rpm"),
    "bad_status": (edit_channel(status="retired"), "bad status/source on channel 1"),
    "bad_source": (edit_channel(source="radio"), "bad status/source on channel 1"),
    "priority_2_without_decimate": (edit_channel(priority=2), "priority 2 channel 1 needs live_decimate"),
    "missing_unit": (drop_channel_key("unit"), r"channel 1: missing \['unit'\]"),
    "missing_min": (drop_channel_key("min"), r"channel 1: missing \['min'\]"),
    "missing_rate": (drop_channel_key("rate_hz"), r"channel 1: missing \['rate_hz'\]"),
    "channel_min_above_max": (edit_channel(min=10, max=5), "channel 1: min is greater than max"),
    "duplicate_param_id": (duplicate_param, "duplicate param id"),
    "param_missing_default": (drop_param_key("default"), r"param 1: missing \['default'\]"),
    "param_min_above_max": (edit_param(min=500, max=100, default=200), "param 1 .*default must be between min and max"),
    "param_default_below_min": (edit_param(default=10), "default must be between min and max"),
    "param_default_above_max": (edit_param(default=2000), "default must be between min and max"),
    "can_start_bit_too_big": (edit_can_src(start_bit=64), "bad start_bit/length"),
    "can_length_zero": (edit_can_src(length=0), "bad start_bit/length"),
    "can_length_too_long": (edit_can_src(length=33), "bad start_bit/length"),
    "can_signal_runs_off_the_frame": (edit_can_src(start_bit=63, length=16), "does not fit in an 8-byte CAN frame"),
    "can_little_endian_runs_off_the_frame": (edit_can_src(byte_order="little_endian", start_bit=56, length=16),
                                             "does not fit in an 8-byte CAN frame"),
    "mux_missing_key": (edit_can_src(mux={"start_bit": 0, "length": 2}), "bad mux block"),
    "mux_extra_key": (edit_can_src(mux={"start_bit": 0, "length": 2, "value": 1, "bits": 3}), "bad mux block"),
    "mux_selector_off_the_frame": (edit_can_src(mux={"start_bit": 60, "length": 8, "value": 1}),
                                   "mux selector does not fit"),
}


@pytest.mark.parametrize("case", REJECTIONS)
def test_validate_registry_rejects(case):
    edit, message = REJECTIONS[case]
    reg = small_registry()
    edit(reg)
    with pytest.raises(ValueError, match=message):
        m.validate_registry(reg)


def test_registry_without_params_or_channels_is_rejected():
    for reg in ({}, {"params": []}, {"channels": []}, None, [1, 2]):
        with pytest.raises(ValueError, match="not a registry"):
            m.validate_registry(reg)


def test_min_equal_to_max_and_default_on_a_limit_are_accepted():
    reg = small_registry()
    reg["channels"][0].update(min=5, max=5)
    reg["params"][0].update(default=20)                  # equals min
    m.validate_registry(reg)


# ---------------------------------------------------------------- demo channels map to the intended signals
DEMO_SIGNALS = {
    100: "Engine_Speed", 101: "Coolant_Temperature", 102: "ECU_Battery_Voltage",
    300: "Inlet_Manifold_Pressure", 302: "Throttle_Position", 338: "Wheel_Speed_Front_Left",
}


@pytest.mark.parametrize("channel_id, signal_name", DEMO_SIGNALS.items())
def test_demo_channels_map_to_the_intended_dbc_signal(registry, m130_db, channel_id, signal_name):
    ch = next(c for c in registry["channels"] if c["id"] == channel_id)
    assert ch["status"] == "active"
    s = ch["src"]
    msg = m130_db.get_message_by_frame_id(m.can_frame_id(ch, registry))
    sig = next(x for x in msg.signals if x.start == s["start_bit"] and x.length == s["length"])
    assert sig.name == signal_name


def test_empty_registry_gives_clear_error(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    with pytest.raises(ValueError, match="not a registry"):
        m.load_registry(str(path))


def _registry_with(can_src_changes, registry):
    good = next(c for c in registry["channels"] if c["source"] == "can")
    return {**registry, "channels": [{**good, "src": {**good["src"], **can_src_changes}}]}


def _load(reg, tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump(reg))
    return m.load_registry(str(path))


def test_extended_can_ids(registry, tmp_path):
    ok = _load(_registry_with({"can_id": 0x18FEF100, "extended": True}, registry), tmp_path)
    assert ok["channels"][0]["src"]["extended"] is True
    _load(_registry_with({"can_id": 0x100, "extended": True}, registry), tmp_path)      # low id, still 29-bit


@pytest.mark.parametrize("changes, message", [
    ({"can_id": 0x800}, "too large for a standard frame; set extended: true"),
    ({"can_id": 0x20000000, "extended": True}, "is too large"),
    ({"extended": "yes"}, "extended must be true or false"),
])
def test_rejects_bad_extended_or_oversized_can_ids(registry, tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        _load(_registry_with(changes, registry), tmp_path)


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
