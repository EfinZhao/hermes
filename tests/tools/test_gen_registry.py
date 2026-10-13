"""gen_registry.py: append-only rules, versioning, snapshot and C++ header output."""
import copy
import json
import shutil
import subprocess

import pytest
import yaml

import gen_registry as gen
from conftest import ROOT


def channel(cid, name, **kw):
    return {"id": cid, "name": name, "unit": "rpm", "source": "synthetic", "rate_hz": 10, "priority": 1,
            "live": True, "log": True, "min": 0, "max": 100, "status": "active", "src": {"kind": "counter"}, **kw}


def can_channel(cid, name, **kw):
    src = {"can_id": 0x640, "bus": 0, "start_bit": 7, "length": 16, "byte_order": "big_endian",
           "signed": False, "scale": 1.0, "offset": 0.0}
    return channel(cid, name, source="can", src=src, **kw)


def base_registry():
    return {"registry_version": 1, "device_id": 7, "can_bases": {"unit": 0x10},
            "params": [{"id": 1, "name": "batch_window_ms", "default": 100, "min": 20, "max": 1000, "unit": "ms",
                        "desc": "x"}],
            "channels": [channel(1, "test.counter"), can_channel(100, "engine.speed_rpm")]}


class Env:
    """A scratch registry + archive + header, and a way to run the generator on them."""

    def __init__(self, tmp_path, capsys):
        self.dir, self.capsys = tmp_path, capsys
        self.registry, self.archive = tmp_path / "registry.yaml", tmp_path / "archive"
        self.header = tmp_path / "out" / "registry_gen.h"

    def run(self, reg, *flags):
        self.registry.write_text(yaml.safe_dump(reg))
        code = gen.main(["--registry", str(self.registry), "--archive", str(self.archive),
                         "--header", str(self.header), *flags])
        return code, self.capsys.readouterr().err

    def release(self, reg):
        """Generate and archive `reg` as a released version."""
        code, err = self.run(reg)
        assert code == 0, err


@pytest.fixture
def env(tmp_path, capsys):
    return Env(tmp_path, capsys)


@pytest.fixture
def released(env):
    """An environment where base_registry() (version 1) has been generated and archived."""
    reg = base_registry()
    env.release(reg)
    return env, reg


def next_version(reg, **changes):
    new = copy.deepcopy(reg)
    new["registry_version"] += 1
    new.update(changes)
    return new


# ---------------------------------------------------------------- output
def test_first_run_writes_snapshot_and_header(env):
    code, err = env.run(base_registry())
    assert code == 0, err
    assert json.loads((env.archive / "registry_v1.json").read_text())["registry_version"] == 1
    header = env.header.read_text()
    assert "kRegistryVersion = 1" in header and "kDeviceId = 7" in header
    assert "kCanBase_unit = 0x10" in header and "// engine.speed_rpm" in header


def test_header_has_only_active_channels(env):
    reg = base_registry()
    reg["channels"].append(channel(50, "later.sensor", status="planned"))
    reg["channels"].append(channel(51, "old.sensor", status="deprecated"))
    env.release(reg)
    header = env.header.read_text()
    assert "kNumChannels = 2" in header
    assert "later.sensor" not in header and "old.sensor" not in header


def test_header_resolves_base_plus_offset_and_mux(env):
    reg = base_registry()
    src = {"can_base": "unit", "can_offset": 3, "bus": 0, "start_bit": 4, "length": 13, "byte_order": "big_endian",
           "signed": True, "scale": 0.25, "offset": -1.5, "mux": {"start_bit": 7, "length": 3, "value": 2}}
    reg["channels"].append(channel(300, "unit.input", source="can", src=src))
    env.release(reg)
    assert "{300, 0x13, false, 0, 4, 13, true, true, 0.25, -1.5, true, 7, 3, 2},  // unit.input" in env.header.read_text()


def test_generation_is_repeatable(released):
    env, reg = released
    first = env.header.read_text()
    env.run(reg)
    assert env.header.read_text() == first


@pytest.mark.skipif(shutil.which("c++") is None, reason="no C++ compiler")
def test_generated_header_compiles(released, tmp_path):
    env, _ = released
    src = tmp_path / "t.cpp"
    src.write_text('#include "registry_gen.h"\n'
                   'static_assert(registry::kNumChannels == 2, "channels");\n'
                   'static_assert(registry::kCanSignals[0].frame_id == 0x640, "frame id");\n'
                   'int main() { return 0; }\n')
    cmd = ["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(env.header.parent), str(src),
           "-o", str(tmp_path / "t")]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_committed_generated_files_are_up_to_date():
    """Fails if registry.yaml was edited without re-running tools/gen_registry.py."""
    assert gen.main(["--check"]) == 0


# ---------------------------------------------------------------- allowed edits
def test_allowed_edits_with_version_bump(released):
    env, reg = released
    new = next_version(reg)
    new["channels"][0]["name"] = "test.renamed"                 # rename
    new["channels"][1].update(rate_hz=50, priority=0, live=False, min=-5, max=20000)   # tuning
    new["channels"].append(channel(2, "test.new"))              # new channel
    new["channels"].append(channel(60, "future", status="planned"))
    new["params"][0]["default"] = 200                           # param tuning
    code, err = env.run(new)
    assert code == 0, err
    assert (env.archive / "registry_v2.json").exists()


def test_planned_can_become_active_and_active_can_be_deprecated(released):
    env, reg = released
    reg2 = copy.deepcopy(reg)
    reg2["channels"].append(channel(60, "future", status="planned"))
    reg2["registry_version"] = 2
    env.release(reg2)
    reg3 = next_version(reg2)
    reg3["channels"][2]["status"] = "active"
    reg3["channels"][0]["status"] = "deprecated"
    code, err = env.run(reg3)
    assert code == 0, err


# ---------------------------------------------------------------- rejections
def rejected(env, reg, text):
    code, err = env.run(reg)
    assert code == 1 and text in err, err


def test_rejects_deleted_channel(released):
    env, reg = released
    new = next_version(reg)
    del new["channels"][0]
    rejected(env, new, "was deleted")


def test_rejects_deleted_param(released):
    env, reg = released
    new = next_version(reg)
    new["params"] = []
    rejected(env, new, "param 1 (batch_window_ms) was deleted")


@pytest.mark.parametrize("field, value", [("unit", "kPa"), ("source", "adc"), ("src", {"kind": "sine", "freq_hz": 1})])
def test_rejects_changed_meaning_of_a_channel(released, field, value):
    env, reg = released
    new = next_version(reg)
    new["channels"][0][field] = value
    rejected(env, new, f"`{field}` changed")


def test_rejects_changed_can_scale(released):
    env, reg = released
    new = next_version(reg)
    new["channels"][1]["src"]["scale"] = 0.1
    rejected(env, new, "`src` changed")


def test_rejects_reused_id(released):
    """Deleting a channel and giving its id to a different measurement."""
    env, reg = released
    new = next_version(reg)
    new["channels"][0] = channel(1, "totally.different", unit="kPa")
    rejected(env, new, "NEW id")


def test_rejects_changed_param_unit(released):
    env, reg = released
    new = next_version(reg)
    new["params"][0]["unit"] = "s"
    rejected(env, new, "param 1 (batch_window_ms): `unit` changed")


def test_rejects_reviving_a_deprecated_channel(released):
    env, reg = released
    dep = next_version(reg)
    dep["channels"][0]["status"] = "deprecated"
    env.release(dep)
    back = next_version(dep)
    back["channels"][0]["status"] = "active"
    rejected(env, back, "cannot be re-enabled")


def test_rejects_change_without_version_bump(released):
    env, reg = released
    new = copy.deepcopy(reg)
    new["channels"][0]["rate_hz"] = 99
    rejected(env, new, "registry_version is still 1")


def test_rejects_older_version(released):
    env, reg = released
    env.release(next_version(reg, device_id=8))
    old = copy.deepcopy(reg)
    old["device_id"] = 8
    rejected(env, old, "older than archived v2")


def test_rejects_version_jump(released):
    env, reg = released
    new = next_version(reg)
    new["registry_version"] = 3
    new["channels"][0]["rate_hz"] = 99
    rejected(env, new, "must go up by exactly 1")


def test_rejects_version_bump_without_change(released):
    env, reg = released
    rejected(env, next_version(reg), "nothing changed")


def test_unchanged_registry_passes(released):
    env, reg = released
    code, err = env.run(reg)
    assert code == 0, err


# ---------------------------------------------------------------- flags
def test_bump_raises_version_when_changed(released):
    env, reg = released
    new = copy.deepcopy(reg)
    new["channels"][0]["rate_hz"] = 99
    code, err = env.run(new, "--bump")
    assert code == 0, err
    assert yaml.safe_load(env.registry.read_text())["registry_version"] == 2
    assert (env.archive / "registry_v2.json").exists()


def test_bump_does_nothing_when_unchanged(released):
    env, reg = released
    code, err = env.run(reg, "--bump")
    assert code == 0 and yaml.safe_load(env.registry.read_text())["registry_version"] == 1


def test_check_mode_writes_nothing_and_flags_stale_files(released):
    env, reg = released
    code, _ = env.run(reg, "--check")
    assert code == 0
    env.header.write_text("// stale\n")
    code, err = env.run(reg, "--check")
    assert code == 1 and "out of date" in err
    assert env.header.read_text() == "// stale\n"          # --check never writes


def test_check_mode_flags_missing_snapshot(env):
    code, err = env.run(base_registry(), "--check")
    assert code == 1 and "registry_v1.json is missing" in err
    assert not env.archive.exists()
