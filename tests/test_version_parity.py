"""The release number lives in every script, in each backend's log, and behind `--version`.

Before this only convert_tiff.py knew it (TOOLKIT_VERSION, pinned to the newest
docs/CHANGELOG.md section by tests/test_diagnose_preset.py), so a backend log did not say
which release wrote it. The release bumps all five places at once; these tests fail when
one is forgotten.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest
import tifffile

import convert_tiff

REPO = Path(__file__).resolve().parent.parent
PS1 = ["compress_tiff_zip.ps1", "copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1",
       "generate_thumbnails.ps1"]
SHELLS = [s for s in ("powershell", "pwsh") if shutil.which(s)]
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
HOSTILE = "写真 [FINAL] x"


@pytest.mark.parametrize("name", PS1)
def test_every_script_carries_the_release_of_convert_tiff(name):
    text = (REPO / name).read_text(encoding="ascii")
    found = re.findall(r"^\$script:ToolkitVersion = '([^']+)'$", text, re.M)
    assert found == [convert_tiff.TOOLKIT_VERSION], (
        f"{name}: $script:ToolkitVersion {found} != convert_tiff.TOOLKIT_VERSION "
        f"{convert_tiff.TOOLKIT_VERSION!r}")
    assert re.search(r"^\$script:ToolkitUrl = '" + re.escape(convert_tiff.TOOLKIT_URL) + "'$",
                     text, re.M), f"{name}: $script:ToolkitUrl differs from TOOLKIT_URL"


def test_version_flag_prints_the_release_and_exits_0():
    r = subprocess.run(["python", str(REPO / "convert_tiff.py"), "--version"],
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


def _script(shell, name):
    if name == "copy_exif":
        return "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
    return name


@pytest.mark.skipif(not SHELLS, reason="PowerShell required")
@pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required")
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("backend,extra", [
    ("compress_tiff_zip.ps1", ["-Mode", "1", "-DryRun"]),
    ("copy_exif", ["-DryRun"]),
    ("generate_thumbnails.ps1", ["-DryRun"]),
])
def test_every_backend_logs_the_release(tmp_path, shell, backend, extra):
    src = _fixture(tmp_path)
    script = _script(shell, backend)
    r = subprocess.run([shell, "-NoProfile", "-File", str(REPO / script), "-InputDir", str(src),
                        *extra], cwd=str(tmp_path), capture_output=True, timeout=600)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, out[-3000:]
    expected = f"tiff-workflow {convert_tiff.TOOLKIT_VERSION} | {script} | PowerShell "
    assert expected in out, f"no '{expected}' line:\n{out[:1500]}"
