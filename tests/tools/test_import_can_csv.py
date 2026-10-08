import subprocess
import sys

import pytest

import msgcodec
from conftest import MESSAGING, ROOT

SCRIPT = str(ROOT / "tools" / "import_can_csv.py")


def run_importer(*args):
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout      # show the importer's own message on failure
    return r.stdout


def channels_by_id(path):
    return {c["id"]: c for c in msgcodec.load_registry(str(path))["channels"]}


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

    committed = msgcodec.load_registry(str(MESSAGING / "registry.yaml"))
    rebuilt = msgcodec.load_registry(str(target))
    for key in ("registry_version", "device_id", "can_bases", "params"):
        assert rebuilt[key] == committed[key], key
    assert channels_by_id(target) == channels_by_id(MESSAGING / "registry.yaml")
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
