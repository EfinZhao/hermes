"""Every registry version this receiver knows, so a packet is decoded with the version it names.

Versions come from the snapshots in messaging/registry_archive/ (registry_vN.json, written by
tools/gen_registry.py) plus the current messaging/registry.yaml, which wins if both have the same
version. To decode packets from an old version, keep that version's snapshot in the archive.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "messaging"))
import msgcodec  # noqa: E402

DEFAULT_REGISTRY = ROOT / "messaging" / "registry.yaml"
DEFAULT_ARCHIVE = ROOT / "messaging" / "registry_archive"


class RegistryStore:
    def __init__(self):
        self._regs: dict[int, dict] = {}
        self._channels: dict[int, dict[int, dict]] = {}   # version -> channel id -> channel

    @classmethod
    def load(cls, registry: Path | None = DEFAULT_REGISTRY, archive: Path | None = DEFAULT_ARCHIVE):
        """Read every snapshot in `archive`, then `registry`. Either may be None or missing."""
        store = cls()
        for f in sorted(archive.glob("registry_v*.json")) if archive and archive.is_dir() else []:
            m = re.fullmatch(r"registry_v(\d+)\.json", f.name)
            if not m:
                continue
            reg = msgcodec.validate_registry(json.loads(f.read_text()), str(f))
            if reg.get("registry_version") != int(m.group(1)):
                raise ValueError(f"{f}: holds registry_version {reg.get('registry_version')}")
            store.add(reg)
        if registry and Path(registry).exists():
            store.add(msgcodec.load_registry(str(registry)))
        return store

    def add(self, reg: dict) -> None:
        version = reg["registry_version"]
        self._regs[version] = reg
        self._channels[version] = {ch["id"]: ch for ch in reg["channels"]}

    def versions(self) -> list[int]:
        return sorted(self._regs)

    def get(self, version: int) -> dict | None:
        return self._regs.get(version)

    def channel(self, version: int, channel_id: int) -> dict | None:
        """What `channel_id` meant in `version`, or None if that version or channel is unknown."""
        return self._channels.get(version, {}).get(channel_id)
