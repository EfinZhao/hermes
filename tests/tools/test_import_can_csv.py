import subprocess
import sys

from conftest import ROOT


def test_importer_is_idempotent():
    """Re-running the importer on the committed registry must add nothing."""
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "import_can_csv.py"), "--dry-run"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0 channels to add", out.stdout
