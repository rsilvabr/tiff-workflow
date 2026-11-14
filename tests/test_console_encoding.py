"""The backends' console output reaches the wizard as UTF-8 (2026-10-17).

A program whose output is redirected falls back to a legacy code page (the PowerShell
backends wrote in the console's OEM one, Python uses the ANSI one), so every Japanese folder
name the wizard streamed on screen came out as '?????' -- the log file, written as UTF-8,
was right. Each backend switches its output to UTF-8 when it is redirected
(tiff_workflow.common.setup_console). Real run, both lanes, a hostile path (Japanese +
brackets + space), per the AGENTS.md rule.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from test_presets import _compress_entry, _write_preset_config
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
    r = subprocess.run(backend_cmd(lane, REPO / "tiff_workflow" / "compress_tiff_zip.py", ["--mode", "8", "--input-dir",
                        str(tmp_path), "--staging-dir", str(stage), "--workers", "1"]),
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
    r = subprocess.run(backend_cmd(lane, REPO / "tiff_workflow" / "compress_tiff_zip.py", ["--mode", "1", "--input-dir",
                        str(cwd), "--workers", "1", "--dry-run"]),
                       cwd=str(cwd), capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
    lines = r.stdout.decode("utf-8", "replace").splitlines()
    first_log = next((ln for ln in lines if "| Log:" in ln), "")
    assert "谷津バラ園" in first_log, f"Log line mangled: {first_log!r}"


def test_the_wizard_writes_utf8_to_a_pipe(tmp_path):
    """Audit round 31: the wizard's own output went to a pipe in the ANSI code page, every
    Japanese name as '?' -- a `--run-preset ... > log.txt` had it so in the wizard's own
    lines (a preset's label, the Diagnose report's paths) and in the backend lines it
    streams, which reach it as UTF-8. On screen it was right. It now switches a redirected
    output to UTF-8 as the backends do (tw.setup_console). The backend runs in the wizard's
    folder, so its "Log:" line, streamed through the wizard, carries that folder's name."""
    in_dir = tmp_path / NAME / "in"
    in_dir.mkdir(parents=True)
    make_tiff(tmp_path / "a.tif")
    (tmp_path / "a.tif").rename(in_dir / "a.tif")
    cwd = tmp_path / "作業 [cwd] 50%"
    cwd.mkdir()
    _write_preset_config(tmp_path, {"nightly": _compress_entry(in_dir)})
    env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))

    def wizard(*args):
        r = subprocess.run([sys.executable, str(REPO / "convert_tiff.py"), *args], cwd=str(cwd),
                           env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        assert r.returncode == 0, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
        # strict: the pipe carries UTF-8. Rich breaks long lines at 80 columns.
        return r.stdout.decode("utf-8").replace("\r", "").replace("\n", "")

    listed = wizard("--list-presets")
    assert "谷津バラ園" in listed, f"the preset's label was mangled: {listed[-600:]}"
    ran = wizard("--run-preset", "nightly", "--dry-run")
    assert "谷津バラ園" in ran, f"the wizard's own lines were mangled: {ran[:800]}"
    assert "作業 [cwd]" in ran, f"the backend lines it streamed were mangled: {ran[-1500:]}"

