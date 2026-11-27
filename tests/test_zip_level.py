"""v3.3: --zip-level (compress, copy-exif; the settings file's `zip_level`; the wizard's Advanced
screen) and PADDED_ZIP_LEVEL (the wizard's padded conversion). ImageMagick takes the Deflate
level of a ZIP TIFF as -quality level x 10; the default, 7, is passed too, and is byte for byte
what magick writes when it is not told -- the owner's files do not change until he sets a level.
Hostile folders and names; the real runs in both lanes."""
import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import tifffile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiff_workflow import common as tw  # noqa: E402
from tiff_workflow import compress_tiff_zip as cz  # noqa: E402
from tiff_workflow import copy_exif_to_tiff as cx  # noqa: E402
from tiff_workflow import wizard as ct  # noqa: E402
from backends import LANES, backend_cmd  # noqa: E402
from test_backend_invariants import (FakeTools, compress_run, exif_run, job, pair,  # noqa: E402
                                     tiff_writes)

TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
needs_tools = pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required")
HOSTILE = "伊豆沼 [FINAL] 50% x"


@pytest.fixture
def tools(monkeypatch):
    return FakeTools(monkeypatch)


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / HOSTILE / "photo 50%off.tif"
    p.parent.mkdir()
    p.write_bytes(b"ORIGINAL")
    return p


def _smooth16(path: Path, seed=5):
    """A 16-bit RGB image that compresses like a photo (a gradient with a little noise): the
    levels give different sizes on it."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:240, 0:320]
    base = (x * 150 + y * 90).astype(np.int64)
    img = np.stack([base, base // 2 + 3000, 60000 - base // 3], -1)
    img = np.clip(img + rng.integers(-40, 40, img.shape), 0, 65535).astype(np.uint16)
    tmp = path.parent / "_fixture16.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(str(tmp), img, photometric="rgb")
    tmp.rename(path)
    return path


# --- the tables --------------------------------------------------------------------------

def test_the_level_is_one_setting_with_its_range_in_both_backends():
    for module in (cz, cx):
        p = next(p for p in module.PARAMS if p.name == "zip_level")
        assert (p.kind, p.default, p.lo, p.hi) == ("int", 7, 1, 9)
        assert module.OVERLAY_SPECS["zip_level"] == "int:1:9"
    assert ct._OVERLAY_SETTINGS["PADDED_ZIP_LEVEL"] == "int:1:9"
    assert ct._convert_tiff_script_values()["PADDED_ZIP_LEVEL"] == 7
    assert tw.zip_level_args(7) == ["-quality", "70"] and tw.zip_level_args(1) == ["-quality", "10"]
    with pytest.raises(tw.UsageError, match="must be from 1 to 9, got 0"):
        tw.parse_args(["--mode", "1", "--zip-level", "0"], cz.PARAMS)


def test_the_level_is_read_never_written_as_a_literal():
    """Every ZIP write of the backends passes tw.zip_level_args(<the option>): a literal level
    would pass every test at 7 and drop the owner's setting (AGENTS: settings are never
    hardcoded)."""
    for f in ("compress_tiff_zip.py", "copy_exif_to_tiff.py", "wizard.py"):
        text = (ROOT / "tiff_workflow" / f).read_text(encoding="utf-8")
        tree = ast.parse(text)
        writes = [n for n in ast.walk(tree) if isinstance(n, ast.List)
                  and any(isinstance(e, ast.Constant) and e.value == "zip" for e in n.elts)]
        assert writes, f
        # one per ZIP write (compress has one more: the predictor sample, written at the level)
        writes_count = text.count('"-compress", "zip"') + text.count("tw.predictor_probe(")
        assert writes_count == text.count("tw.zip_level_args("), f
        assert '"-quality", "70"' not in text and "zip_level_args(7)" not in text, f


# --- what the workers send (FakeTools) -----------------------------------------------------

@pytest.mark.parametrize("extra", [(), ("--generate-thumbnail",)], ids=["plain", "thumbnail"])
@pytest.mark.parametrize("level", [7, 3])
def test_compress_writes_at_the_level_asked(tools, photo, extra, level):
    argv = ("--mode", "2") + extra + (() if level == 7 else ("--zip-level", str(level)))
    r = job(compress_run(*argv), photo)
    assert r.result.startswith("OK"), r.result
    writes = tiff_writes(tools)
    assert writes
    for w in writes:
        assert w[w.index("-quality") + 1] == str(level * 10), w


def test_the_predictor_sample_is_written_at_the_level_of_the_write(tools, photo):
    tools.probe = (1000, 500)
    job(compress_run("--mode", "2", "--zip-predictor", "auto", "--zip-level", "2"), photo)
    assert tools.probe_write_args[:2] == ["-quality", "20"]


@pytest.mark.parametrize("level", [7, 4])
def test_copy_exif_writes_at_the_level_asked(tools, photo, level):
    jpg = photo.with_suffix(".jpg")
    argv = ["--compress-zip"] + ([] if level == 7 else ["--zip-level", str(level)])
    r = cx.process_pair(exif_run(*argv), pair(photo, jpg), str(photo.parent), str(photo.parent), 1.0)
    assert r.line.startswith("OK+ZIP"), r.line
    (w,) = [c for c in tools.magick_calls if "-compress" in c]
    assert w[w.index("-quality") + 1] == str(level * 10)


def test_the_padded_conversion_writes_at_its_level(tmp_path, monkeypatch):
    seen = []

    def run(cmd, timeout=None, **kw):
        cmd = [str(c) for c in cmd]
        if "-depth" in cmd:
            seen.append(cmd)
        raise FileNotFoundError("stop after the write's command line")
    monkeypatch.setattr(ct, "_run_captured", run)
    monkeypatch.setattr(ct, "_tiff_extra_pages_are_thumbnails", lambda p: True)
    monkeypatch.setattr(ct, "_tiff_sample_layout", lambda p: [{"spp": 3}])
    monkeypatch.setattr(ct, "_magick_keep_type_args", lambda layout: [])
    monkeypatch.setattr(ct, "PADDED_ZIP_LEVEL", 5)
    src = tmp_path / HOSTILE / "a.tif"
    src.parent.mkdir()
    src.write_bytes(b"x")
    ct._process_single_padded(src, tmp_path)
    (cmd,) = seen
    assert cmd[cmd.index("-quality") + 1] == "50"


# --- the wizard: passed only when edited (the Advanced rule) --------------------------------

def test_the_wizard_passes_the_level_only_when_edited():
    wf = {"origin": "free_compress", "mode": 1}
    assert "--zip-level" not in ct.build_compress_command(wf, [Path(HOSTILE)])
    wf["advanced"] = {"compress": {"zip_level": 7}}
    argv = ct.build_compress_command(wf, [Path(HOSTILE)])
    assert argv[argv.index("--zip-level") + 1] == "7", "an edited value equal to the default is passed"
    wf["advanced"] = {"compress": {"zip_level": 12}}       # a hand-edited preset
    assert "--zip-level" not in ct.build_compress_command(wf, [Path(HOSTILE)])
    assert ct._advanced_summary_rows(wf) == [["Advanced:", "[compress] --zip-level = 12 (ignored: not valid)"]]


# --- real files ----------------------------------------------------------------------------

def _run(lane, args, cwd, env=None):
    cmd = backend_cmd(lane, ROOT / "tiff_workflow" / "compress_tiff_zip.py", [str(a) for a in args])
    r = subprocess.run(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600,
                       env=env or dict(os.environ, TIFFWORKFLOW_SETTINGS=""))
    return r.returncode, r.stdout + r.stderr


@needs_tools
@pytest.mark.parametrize("lane", LANES)
def test_lower_levels_are_larger_and_the_pixels_are_the_same(tmp_path, lane):
    sizes = {}
    for level in (1, 3, 7):
        folder = tmp_path / f"L{level}" / HOSTILE
        src = _smooth16(folder / "0014_14 (2) 50%.tif")
        ref = tifffile.imread(str(src))
        rc, out = _run(lane, ["--mode", 1, "--input-dir", folder, "--zip-level", level], tmp_path)
        assert rc == 0, out[-2000:]
        dst = folder / "ZIP" / src.name
        assert np.array_equal(tifffile.imread(str(dst)), ref), f"level {level}: pixels changed"
        sizes[level] = dst.stat().st_size
    assert sizes[1] > sizes[3] > sizes[7], sizes


@needs_tools
def test_the_settings_file_sets_the_level(tmp_path):
    folder = tmp_path / HOSTILE
    src = _smooth16(folder / "a.tif")
    settings = tmp_path / "s [x].json"
    settings.write_text(json.dumps({"compress_tiff_zip": {"zip_level": 1}}), encoding="utf-8")
    rc, out = _run("sequential", ["--mode", 1, "--input-dir", folder], tmp_path,
                   env=dict(os.environ, TIFFWORKFLOW_SETTINGS=str(settings)))
    assert rc == 0 and "1 value(s): zip_level" in out, out[-2000:]
    one = (folder / "ZIP" / src.name).stat().st_size
    shutil.rmtree(folder / "ZIP")
    rc, out = _run("sequential", ["--mode", 1, "--input-dir", folder], tmp_path)
    assert rc == 0
    assert one > (folder / "ZIP" / src.name).stat().st_size


@needs_tools
@pytest.mark.parametrize("bits", [8, 16])
def test_level_7_is_what_magick_writes_untold(tmp_path, bits):
    """The default is passed explicitly and changes no byte (measured on the owner's scans too:
    the A/B of v3.2.1 and v3.3 is in the coder's report)."""
    tmp_path.mkdir(exist_ok=True)
    src = tmp_path / "src.tif"
    rng = np.random.default_rng(1)
    dt = np.uint16 if bits == 16 else np.uint8
    tifffile.imwrite(str(src), (rng.integers(0, 50, (200, 260, 3)) * (np.iinfo(dt).max // 60)).astype(dt),
                     photometric="rgb")
    untold, told = tmp_path / "untold.tif", tmp_path / "told.tif"
    subprocess.run(["magick", str(src), "-compress", "zip", str(untold)], check=True, timeout=120)
    subprocess.run(["magick", str(src), "-compress", "zip"] + tw.zip_level_args(7) + [str(told)],
                   check=True, timeout=120)
    assert untold.read_bytes() == told.read_bytes()
