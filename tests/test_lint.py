"""v3.1 (T11): ruff and mypy find nothing in convert_tiff.py, the package and tools/, with the
settings in pyproject.toml (ruff: pyflakes, pycodestyle's errors, bugbear; mypy: Python 3.9,
the annotated functions).

They are development tools, not something the toolkit needs at run time: the test is skipped
in a Python that does not have them (`python -m pip install ruff "mypy<2"`; mypy 2 no longer
checks against Python 3.9)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("tool, args", [("ruff", ["check", "--no-cache"]), ("mypy", [])])
def test_the_code_is_clean(tool, args):
    if importlib.util.find_spec(tool) is None:
        pytest.skip(f"{tool} is not installed in this Python")
    r = subprocess.run([sys.executable, "-m", tool, *args], cwd=str(ROOT),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=900)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, out[-6000:]
