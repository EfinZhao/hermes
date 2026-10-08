"""Shared fixtures. `messaging/` is on sys.path via pyproject.toml, so tests `import afr_msg`."""
from pathlib import Path

import cantools
import pytest

import afr_msg

ROOT = Path(__file__).resolve().parent.parent
MESSAGING = ROOT / "messaging"
M130_DBC = MESSAGING / "dbc" / "M1_General_0x640_0x650_0x670_Ver5.dbc"
E888_DBC = MESSAGING / "dbc" / "E8xx.dbc"


@pytest.fixture(scope="session")
def registry():
    return afr_msg.load_registry(str(MESSAGING / "registry.yaml"))


@pytest.fixture(scope="session")
def m130_db():
    return cantools.database.load_file(str(M130_DBC))


@pytest.fixture(scope="session")
def e888_db():
    return cantools.database.load_file(str(E888_DBC))
