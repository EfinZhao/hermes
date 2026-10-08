import subprocess
import sys

import pytest

import msgcodec
from conftest import MESSAGING, ROOT

SCRIPT = str(ROOT / "tools" / "import_can_csv.py")


def run_importer(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, check=True).stdout


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
    for key in ("registry_version", "device_id", "e888_base_id", "params"):
        assert rebuilt[key] == committed[key], key
    assert channels_by_id(target) == channels_by_id(MESSAGING / "registry.yaml")
    # a second run on the rebuilt file adds nothing
    assert run_importer("--registry", str(target), "--dry-run").strip().endswith("0 channels to add")


def test_pinned_channels_get_friendly_names_from_config(tmp_path):
    target = tmp_path / "registry.yaml"
    run_importer("--registry", str(target))
    ch = channels_by_id(target)
    assert (ch[100]["name"], ch[101]["name"], ch[102]["name"]) == ("engine.speed_rpm", "coolant.temp_c", "ecu.battery_v")
