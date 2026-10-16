"""Shared fixtures. `messaging/` is on sys.path via pyproject.toml, so tests `import msgcodec`."""
from pathlib import Path

import cantools
import pytest

import msgcodec

ROOT = Path(__file__).resolve().parent.parent
MESSAGING = ROOT / "messaging"
M130_DBC = MESSAGING / "dbc" / "M1_General_0x640_0x650_0x670_Ver5.dbc"
E888_DBC = MESSAGING / "dbc" / "E8xx.dbc"


@pytest.fixture(scope="session")
def registry():
    return msgcodec.load_registry(str(MESSAGING / "registry.yaml"))


@pytest.fixture(scope="session")
def m130_db():
    return cantools.database.load_file(str(M130_DBC))


@pytest.fixture(scope="session")
def e888_db():
    return cantools.database.load_file(str(E888_DBC))


def crc32_reference(data: bytes) -> int:
    """Bit-by-bit CRC-32 (IEEE), independent of zlib, as the firmware will have to write it."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF
