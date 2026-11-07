import shutil
import subprocess
from pathlib import Path

import pytest


def test_table_logic_with_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run([node, "--test", str(root / "tests/js/table-logic.test.mjs")],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
