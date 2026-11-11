"""A -DryRun creates nothing: no staging/output folder, no ZIP/, no file changes.

Checked while planning round 27 (jxl-photo #614: a dry run used the temp folder before it
existed). There is no TEMP_DIR setting in this toolkit, so that bug does not exist here;
this pins it. The copy_exif row with -OutputDir is the one that did fail: the PowerShell
scripts of v2.9.1 created that folder in a dry run (tests/test_v3_fixes.py).
"""

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest
import tifffile

REPO = Path(__file__).resolve().parent.parent
from backends import LANES, backend_cmd, run_backend  # noqa: E402
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
pytestmark = [
    pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required"),
]
HOSTILE = "写真 [FINAL] x"


def _copy_exif(lane):
    return "copy_exif_to_tiff.py"


# (id, script for a lane, extra args as a function of the missing folder)
ROWS = [
    ("compress-mode1-missing-staging", lambda sh: "compress_tiff_zip.py",
     lambda miss: ["-Mode", "1", "-DryRun", "-StagingDir", miss]),
    ("compress-mode2-missing-output", lambda sh: "compress_tiff_zip.py",
     lambda miss: ["-Mode", "2", "-DryRun", "-OutputDir", miss]),
    ("compress-mode8-no-staging", lambda sh: "compress_tiff_zip.py",
     lambda miss: ["-Mode", "8", "-DryRun"]),
    ("copy-exif-compresszip-missing-staging", _copy_exif,
     lambda miss: ["-DryRun", "-CompressZip", "-StagingDir", miss]),
    ("copy-exif-compresszip-missing-output", _copy_exif,
     lambda miss: ["-DryRun", "-CompressZip", "-OutputDir", miss]),
    ("thumbnails-missing-output", lambda sh: "generate_thumbnails.py",
     lambda miss: ["-DryRun", "-OutputDir", miss]),
]


def _build_fixture(src: Path):
    src.mkdir(parents=True)
    a = np.random.default_rng(1).integers(0, 255, size=(48, 64, 3), dtype=np.uint8)
    tifffile.imwrite(str(src / "DSCF0001 (2).tif"), a, photometric="rgb")
    safe = Path(tempfile.mkdtemp(prefix="tw_dry_")) / "safe.jpg"
    try:
        subprocess.run(["magick", "-size", "64x48", "gradient:", str(safe)],
                       check=True, capture_output=True, timeout=120)
        shutil.copy(safe, src / "DSCF0001 (2).jpg")
    finally:
        shutil.rmtree(safe.parent, ignore_errors=True)


def _snapshot(root: Path):
    return sorted((str(p.relative_to(root)), hashlib.md5(p.read_bytes()).hexdigest())
                  for p in root.rglob("*") if p.is_file()) + \
           sorted((str(p.relative_to(root)), "dir") for p in root.rglob("*") if p.is_dir())


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_dry_run_creates_nothing(tmp_path, lane, row):
    _, script_for, args_for = row
    root = tmp_path / HOSTILE
    src = root / "S5pro 01"
    _build_fixture(src)
    missing = tmp_path / "does not exist [yet]"
    before = _snapshot(root)
    cmd = backend_cmd(lane, REPO / script_for(lane), ["-InputDir", str(src), *[str(a) for a in args_for(str(missing))]])
    r = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, timeout=600)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, out[-3000:]
    assert not missing.exists(), f"a dry run created {missing}"
    assert _snapshot(root) == before, "a dry run changed the fixture folder"
