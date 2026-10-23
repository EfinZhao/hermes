"""registry_store.py: every known registry version, looked up by the version a packet names."""
import copy
import json

import pytest

import registry_store
from registry_store import RegistryStore


@pytest.fixture
def v2(registry):
    return copy.deepcopy(registry)


def snapshot(tmp_path, reg, name=None):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / (name or f"registry_v{reg['registry_version']}.json")).write_text(json.dumps(reg))


def old_version(reg, version, **changes):
    """A copy of `reg` as version `version`, with channel 100's fields changed."""
    old = copy.deepcopy(reg)
    old["registry_version"] = version
    next(c for c in old["channels"] if c["id"] == 100).update(changes)
    return old


def test_loads_the_current_registry(registry, tmp_path):
    store = RegistryStore.load(registry_store.DEFAULT_REGISTRY, tmp_path / "no_archive")
    assert store.versions() == [registry["registry_version"]]
    assert store.channel(registry["registry_version"], 100)["name"] == "engine.speed_rpm"


def test_each_version_keeps_its_own_meaning(v2, tmp_path):
    archive = tmp_path / "archive"
    snapshot(archive, old_version(v2, 1, name="engine.rpm_old"))
    store = RegistryStore.load(registry_store.DEFAULT_REGISTRY, archive)
    assert store.versions() == [1, v2["registry_version"]]
    assert store.channel(1, 100)["name"] == "engine.rpm_old"
    assert store.channel(v2["registry_version"], 100)["name"] == "engine.speed_rpm"


def test_the_yaml_wins_over_a_snapshot_of_the_same_version(v2, tmp_path):
    archive = tmp_path / "archive"
    snapshot(archive, old_version(v2, v2["registry_version"], name="engine.stale_snapshot"))
    store = RegistryStore.load(registry_store.DEFAULT_REGISTRY, archive)
    assert store.channel(v2["registry_version"], 100)["name"] == "engine.speed_rpm"


def test_a_snapshot_whose_name_and_version_disagree_is_an_error(v2, tmp_path):
    archive = tmp_path / "archive"
    snapshot(archive, old_version(v2, 1), name="registry_v5.json")
    with pytest.raises(ValueError, match="registry_v5.json"):
        RegistryStore.load(None, archive)


def test_an_invalid_snapshot_is_an_error(v2, tmp_path):
    archive = tmp_path / "archive"
    bad = old_version(v2, 1)
    bad["channels"].append(copy.deepcopy(bad["channels"][0]))     # duplicate id
    snapshot(archive, bad)
    with pytest.raises(ValueError, match="duplicate"):
        RegistryStore.load(None, archive)


def test_other_files_in_the_archive_are_ignored(v2, tmp_path):
    archive = tmp_path / "archive"
    snapshot(archive, old_version(v2, 1))
    (archive / "README.md").write_text("notes")
    (archive / "registry_vX.json").write_text("{}")
    assert RegistryStore.load(None, archive).versions() == [1]


def test_nothing_to_load_gives_an_empty_store(tmp_path):
    store = RegistryStore.load(None, None)
    assert store.versions() == []
    assert RegistryStore.load(tmp_path / "missing.yaml", tmp_path / "missing").versions() == []


def test_unknown_versions_and_channels_give_none(registry):
    store = RegistryStore()
    store.add(registry)
    assert store.get(99) is None
    assert store.channel(99, 100) is None
    assert store.channel(registry["registry_version"], 9999) is None
