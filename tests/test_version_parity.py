"""The release number lives in two places, in each backend's log, and behind `--version`.

convert_tiff.py has TOOLKIT_VERSION (pinned to the newest docs/CHANGELOG.md section by
tests/test_diagnose_preset.py; the Windows program's build reads it there), and the three
backends take theirs from tiff_workflow_common.py. The release bumps both at once; these
tests fail when one is forgotten.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
import tifffile

import convert_tiff

REPO = Path(__file__).resolve().parent.parent
import tiff_workflow_common  # noqa: E402
from backends import BACKENDS, LANES, backend_cmd  # noqa: E402
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
HOSTILE = "写真 [FINAL] x"


def test_the_backends_carry_the_release_of_convert_tiff():
    assert tiff_workflow_common.TOOLKIT_VERSION == convert_tiff.TOOLKIT_VERSION
    assert tiff_workflow_common.TOOLKIT_URL == convert_tiff.TOOLKIT_URL


@pytest.mark.parametrize("name", BACKENDS)
def test_no_backend_has_a_version_of_its_own(name):
    """One number for the three of them: a literal in a backend would be a third place."""
    text = (REPO / name).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(TOOLKIT_VERSION|__version__)\s*=", text, re.M), name
    assert "tw.TOOLKIT_VERSION" in text


def test_version_flag_prints_the_release_and_exits_0():
    r = subprocess.run([sys.executable, str(REPO / "convert_tiff.py"), "--version"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"tiff-workflow {convert_tiff.TOOLKIT_VERSION}"


def _fixture(tmp_path):
    src = tmp_path / HOSTILE / "S5pro 01"
    src.mkdir(parents=True)
    a = np.random.default_rng(1).integers(0, 255, size=(48, 64, 3), dtype=np.uint8)
    tifffile.imwrite(str(src / "DSCF0001 (2).tif"), a, photometric="rgb")
    safe = Path(tempfile.mkdtemp(prefix="tw_ver_")) / "safe.jpg"
    try:
        subprocess.run(["magick", "-size", "64x48", "gradient:", str(safe)],
                       check=True, capture_output=True, timeout=120)
        shutil.copy(safe, src / "DSCF0001 (2).jpg")
    finally:
        shutil.rmtree(safe.parent, ignore_errors=True)
    return src


@pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required")
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("backend,extra", [
    ("compress_tiff_zip.py", ["-Mode", "1", "-DryRun"]),
    ("copy_exif_to_tiff.py", ["-DryRun"]),
    ("generate_thumbnails.py", ["-DryRun"]),
])
def test_every_backend_logs_the_release(tmp_path, lane, backend, extra):
    src = _fixture(tmp_path)
    script = backend
    r = subprocess.run(backend_cmd(lane, script, ["-InputDir", str(src), *extra]),
                       cwd=str(tmp_path), capture_output=True, timeout=600)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, out[-3000:]
    expected = f"tiff-workflow {convert_tiff.TOOLKIT_VERSION} | {script} | Python "
    assert expected in out, f"no '{expected}' line:\n{out[:1500]}"
