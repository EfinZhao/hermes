import random
import re
import subprocess
import sys

import pytest

import import_can_csv
import msgcodec
import yaml
from conftest import MESSAGING, ROOT

SCRIPT = str(ROOT / "tools" / "import_can_csv.py")


def run_importer(*args):
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout      # show the importer's own message on failure
    return r.stdout


def channels_by_id(path):
    return {c["id"]: c for c in msgcodec.load_registry(str(path))["channels"]}


def defining(path):
    """What the importer decides, as opposed to what a team tunes by hand afterwards (rate, priority, live,
    log, min, max, status, device_id, param defaults, registry_version). Channels are compared by the
    fields that define them, in the same sense as gen_registry.CHANNEL_MEANING plus the id and name."""
    reg = msgcodec.load_registry(str(path))
    return {"channels": {c["id"]: {k: c[k] for k in ("name", "unit", "source", "src")} for c in reg["channels"]},
            "params": {p["id"]: (p["name"], p["unit"]) for p in reg["params"]},
            "can_base_names": sorted(reg["can_bases"])}


def test_importer_is_idempotent():
    """Re-running the importer on the committed registry must add nothing."""
    assert run_importer("--dry-run").strip() == "0 channels to add"


@pytest.mark.parametrize("initial", [None, ""], ids=["missing_file", "empty_file"])
def test_bootstrap_reproduces_committed_registry(tmp_path, initial):
    """From a missing or empty registry.yaml the importer must rebuild the committed registry
    (same ids, names and sources), including the friendly names from the config."""
    target = tmp_path / "registry.yaml"
    if initial is not None:
        target.write_text(initial)
    run_importer("--registry", str(target))

    assert defining(target) == defining(MESSAGING / "registry.yaml")
    # a second run on the rebuilt file adds nothing
    assert run_importer("--registry", str(target), "--dry-run").strip().endswith("0 channels to add")


def test_pinned_channels_get_friendly_names_from_config(tmp_path):
    target = tmp_path / "registry.yaml"
    run_importer("--registry", str(target))
    ch = channels_by_id(target)
    assert (ch[100]["name"], ch[101]["name"], ch[102]["name"]) == ("engine.speed_rpm", "coolant.temp_c", "ecu.battery_v")


def test_two_units_of_the_same_device(tmp_path):
    """Another team may have two E888s: two devices sharing one DBC, each with its own base,
    id block and name prefix."""
    import yaml
    cfg = yaml.safe_load((ROOT / "tools" / "import_can_config.yaml").read_text())
    e888 = next(d for d in cfg["devices"] if d["name"] == "e888")
    rear = {**e888, "name": "e888_rear", "base_id": 0xF4, "first_id": 1100, "prefix": "rear", "names": {}}
    e888["prefix"] = "front"
    cfg["devices"].append(rear)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))

    target = tmp_path / "registry.yaml"
    run_importer("--registry", str(target), "--config", str(cfg_path))
    reg = msgcodec.load_registry(str(target))
    assert reg["can_bases"] == {"e888": 0xF0, "e888_rear": 0xF4}
    by_base = {}
    for ch in reg["channels"]:
        s = ch["src"]
        if s.get("can_base"):
            by_base.setdefault(s["can_base"], []).append(ch)
    assert len(by_base["e888"]) == len(by_base["e888_rear"]) == 44
    front, back = by_base["e888"][0], by_base["e888_rear"][0]
    assert front["name"].startswith("front.") and back["name"].startswith("rear.")
    assert msgcodec.can_frame_id(back, reg) - msgcodec.can_frame_id(front, reg) == 4
    assert all(1000 <= c["id"] < 1100 for c in by_base["e888"])
    assert all(c["id"] >= 1100 for c in by_base["e888_rear"])
    assert run_importer("--registry", str(target), "--config", str(cfg_path), "--dry-run").strip() == "0 channels to add"


def test_extended_frames_flow_from_dbc_to_registry_to_header(tmp_path):
    """A 29-bit frame whose id is below 0x7FF must stay extended all the way to the C++ header."""
    import cantools
    import yaml
    import gen_registry
    from cantools.database.can import Message, Signal
    from cantools.database.conversion import BaseConversion

    def sig(name):
        return Signal(name, start=7, length=16, byte_order="big_endian", unit="km/h",
                      conversion=BaseConversion.factory(scale=0.1, offset=0))

    db = cantools.database.Database(messages=[
        Message(0x100, "Extended", 8, [sig("Speed")], is_extended_frame=True),
        Message(0x200, "Standard", 8, [sig("Other")]),
    ])
    dbc = tmp_path / "ext.dbc"
    cantools.database.dump_file(db, str(dbc))
    cfg = {"devices": [{"name": "ext", "dbc": str(dbc), "first_id": 300, "active": ["Speed", "Other"]}]}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    target = tmp_path / "registry.yaml"
    run_importer("--registry", str(target), "--config", str(cfg_path))

    by_name = {c["name"]: c for c in msgcodec.load_registry(str(target))["channels"] if c["source"] == "can"}
    assert by_name["speed.kmh"]["src"]["extended"] is True and by_name["speed.kmh"]["src"]["can_id"] == 0x100
    assert "extended" not in by_name["other.kmh"]["src"]
    header = tmp_path / "registry_gen.h"
    assert gen_registry.main(["--registry", str(target), "--archive", str(tmp_path / "a"), "--header", str(header)]) == 0
    text = header.read_text()
    assert "0x100, true," in text and "0x200, false," in text


# ---------------------------------------------------------------- existing channels are never changed
def drop_blocks(text, ids):
    for cid in ids:
        text, n = re.subn(rf"  - id: {cid}\n(?:    .*\n)+\n?", "", text)
        assert n == 1, cid
    return text


def test_importer_only_adds_and_never_touches_existing_channels(tmp_path):
    target = tmp_path / "registry.yaml"
    run_importer("--registry", str(target))
    before = channels_by_id(target)
    removed = [310, 330]
    removed_addresses = [before[i]["src"] for i in removed]

    text = drop_blocks(target.read_text(), removed)
    text = "# my own notes\n" + text
    text = re.sub(r"(  - id: 305\n(?:    .*\n)*?    rate_hz: )10", r"\g<1>99", text)      # hand-tuned
    target.write_text(text)

    run_importer("--registry", str(target))
    after = channels_by_id(target)

    assert len(after) == len(before)
    new_ids = sorted(set(after) - set(before))
    assert new_ids == [max(before) + 1, max(before) + 2] or len(new_ids) == 2
    assert sorted(after[i]["src"]["start_bit"] for i in new_ids) == sorted(a["start_bit"] for a in removed_addresses)
    assert [after[i]["src"] for i in new_ids] == removed_addresses        # the same signals came back
    untouched = [i for i in before if i not in removed]
    assert all(after[i] == before[i] or i == 305 for i in untouched)
    assert after[305]["rate_hz"] == 99 and {**after[305], "rate_hz": 10} == before[305]
    out = target.read_text()
    assert out.startswith("# my own notes\n")
    assert yaml.safe_load(out)["registry_version"] == yaml.safe_load(text)["registry_version"] + 1


# ---------------------------------------------------------------- configuration errors
def config_with(tmp_path, change):
    cfg = yaml.safe_load((ROOT / "tools" / "import_can_config.yaml").read_text())
    change(cfg)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def device(cfg, name):
    return next(d for d in cfg["devices"] if d["name"] == name)


CONFIG_ERRORS = {
    "duplicate_device_names": (lambda c: device(c, "e888").update(name="ecu"), "device names must be unique"),
    "same_first_id": (lambda c: device(c, "e888").update(first_id=300), "different first_id"),
    "base_id_without_offsets": (lambda c: device(c, "e888").pop("offsets"), "has base_id but no offsets"),
    "pinned_id_not_below_first_id": (
        lambda c: device(c, "ecu")["pinned"]["Engine_Speed"].update(id=300),
        "pinned id 300 .* must be below the lowest first_id 300"),
    "id_blocks_overlap": (lambda c: device(c, "e888").update(first_id=305), r"id block \[300, 305\) is full"),
    "name_collision": (lambda c: device(c, "ecu")["names"].update(Wheel_Speed_Front_Right="wheel.speed_fl_kmh"),
                       "name collision: wheel.speed_fl_kmh"),
    "pinned_signal_missing_from_dbc": (
        lambda c: device(c, "ecu")["pinned"].update(No_Such_Signal={"id": 150, "name": "x.y"}),
        r"pinned signals not found in .*: \['No_Such_Signal'\]"),
    "pinned_id_already_used_by_another_channel": (
        lambda c: device(c, "ecu")["pinned"].update(Coolant_Temperature={"id": 100, "name": "coolant.temp_c"}),
        "pinned id 100 .* is already used"),
}


@pytest.mark.parametrize("case", CONFIG_ERRORS)
def test_importer_rejects_bad_configuration(tmp_path, case):
    change, message = CONFIG_ERRORS[case]
    cfg = config_with(tmp_path, change)
    with pytest.raises(SystemExit, match=message):
        import_can_csv.main(["--registry", str(tmp_path / "registry.yaml"), "--config", cfg])
    assert not (tmp_path / "registry.yaml").exists()          # nothing half-written


# ---------------------------------------------------------------- Intel (little-endian) multiplexed DBC
def intel_mux_db(selector_order="little_endian"):
    import cantools
    from cantools.database.can import Message, Signal
    from cantools.database.conversion import BaseConversion

    def sig(name, start, length, order="little_endian", **kw):
        return Signal(name, start=start, length=length, byte_order=order, unit="V",
                      conversion=BaseConversion.factory(scale=0.5, offset=1), **kw)

    start = 0 if selector_order == "little_endian" else 7        # keep the selector clear of the data bytes
    signals = [Signal("sel", start=start, length=4, byte_order=selector_order, is_multiplexer=True)]
    signals += [sig(f"x{k}", 8, 16, multiplexer_ids=[k], multiplexer_signal="sel") for k in range(2)]
    return cantools.database.Database(messages=[Message(0x40, "Muxed", 8, signals, strict=False)])


def intel_config(tmp_path, db):
    import cantools
    dbc = tmp_path / "intel.dbc"
    cantools.database.dump_file(db, str(dbc))
    cfg = {"devices": [{"name": "unit", "dbc": str(dbc), "first_id": 300, "base_id": 0x40, "offsets": [0],
                        "prefix": "unit"}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path), dbc


def test_intel_multiplexed_dbc_imports_and_decodes_like_cantools(tmp_path):
    import cantools
    cfg, dbc = intel_config(tmp_path, intel_mux_db())
    target = tmp_path / "registry.yaml"
    run_importer("--registry", str(target), "--config", cfg)
    reg = msgcodec.load_registry(str(target))
    channels = [c for c in reg["channels"] if c["source"] == "can"]
    assert len(channels) == 2 and all(c["src"]["byte_order"] == "little_endian" for c in channels)

    db = cantools.database.load_file(str(dbc))
    msg = db.messages[0]
    rng = random.Random(5)
    for _ in range(200):
        data = bytes([(rng.randrange(256) & 0xF0) | rng.randrange(2)]) + bytes(rng.randrange(256) for _ in range(7))
        decoded = msg.decode(data, decode_choices=False, scaling=True)
        for ch in channels:
            name = f"x{ch['src']['mux']['value']}"
            got = msgcodec.extract_channel(data, ch)
            assert (got is None) == (name not in decoded)
            if got is not None:
                assert abs(got - decoded[name]) < 1e-9


def test_importer_rejects_a_selector_with_a_different_byte_order(tmp_path):
    cfg, _ = intel_config(tmp_path, intel_mux_db(selector_order="big_endian"))
    with pytest.raises(SystemExit, match="same byte order"):
        import_can_csv.main(["--registry", str(tmp_path / "registry.yaml"), "--config", cfg])
