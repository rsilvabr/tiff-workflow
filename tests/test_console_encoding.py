"""The backends' console output reaches the wizard as UTF-8 (2026-10-17).

A program whose output is redirected falls back to a legacy code page (the PowerShell
backends wrote in the console's OEM one, Python uses the ANSI one), so every Japanese folder
name the wizard streamed on screen came out as '?????' -- the log file, written as UTF-8,
was right. Each backend switches its output to UTF-8 when it is redirected
(tiff_workflow_common.setup_console). Real run, both lanes, a hostile path (Japanese +
brackets + space), per the AGENTS.md rule.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from test_regression_classes import make_tiff

REPO = Path(__file__).resolve().parent.parent
from backends import LANES, backend_cmd, run_backend  # noqa: E402
NAME = "260517_谷津バラ園_お台場 [FINAL]"

pytestmark = pytest.mark.skipif(
    shutil.which("magick") is None or shutil.which("exiftool") is None,
    reason="ImageMagick + exiftool required")


@pytest.mark.parametrize("lane", LANES)
def test_compress_streams_japanese_folder_names(tmp_path, lane):
    # Built under an ASCII name, then renamed: the fixture's own exiftool call would hit
    # the ANSI command-line trap this repo already works around in convert_tiff.py.
    build = tmp_path / "build" / "1_Z7"
    build.mkdir(parents=True)
    make_tiff(build / "_a.tif")
    (build / "_a.tif").rename(build / "DSC_0014 (2).tif")
    (tmp_path / "build").rename(tmp_path / NAME)
    stage = tmp_path.parent / (tmp_path.name + "_stage")  # outside the scanned tree
    stage.mkdir()
    # the owner's preset: mode 8 with staging, which prints a "-- Group: <folder>" line
    r = subprocess.run(backend_cmd(lane, REPO / "compress_tiff_zip.py", ["-Mode", "8", "-InputDir",
                        str(tmp_path), "-StagingDir", str(stage), "-Workers", "1"]),
                       cwd=str(tmp_path), capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    assert "谷津バラ園" in out, f"the folder name was mangled on screen:\n{out[-1500:]}"
    assert "?????" not in out


@pytest.mark.parametrize("lane", LANES)
def test_the_first_line_already_carries_utf8(tmp_path, lane):
    """The "Log: <cwd>\\Logs\\..." line is printed before any exiftool probe (which
    switched the encoding around its own call and put it back), so it shows whether the
    backend's plain output is UTF-8 -- run it from a Japanese folder."""
    cwd = tmp_path / NAME
    cwd.mkdir()
    r = subprocess.run(backend_cmd(lane, REPO / "compress_tiff_zip.py", ["-Mode", "1", "-InputDir",
                        str(cwd), "-Workers", "1", "-DryRun"]),
                       cwd=str(cwd), capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
    lines = r.stdout.decode("utf-8", "replace").splitlines()
    first_log = next((ln for ln in lines if "| Log:" in ln), "")
    assert "谷津バラ園" in first_log, f"Log line mangled: {first_log!r}"

