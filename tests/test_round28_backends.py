"""Audit round 28 (AI_tools/261111_Fable_review_bug-sweep.md): the backends.

Found and fixed in the PowerShell backends of v2.9.1; the same runs now drive the Python
backends, in both lanes, in a hostile folder (Japanese, [ ], a space, and a '%' where it
matters). The paths a real run cannot reach without sabotaging a tool are in
tests/test_backend_invariants.py.
  #1/#2  magick wrote a neutral RGB file as 1-channel gray (RGB+IR as 2 samples) -> -type
         from the source layout, and a layout gate after every write
  #3     IPTC text outside Latin came back as '?' through the MIE container
  #6/#7  a failed metadata restore (or an exception in a PS7 runspace) let the file without
         EXIF replace the original
  #8     with -StagingDir the in-place gate was skipped
  #9     an overlay folder name made a copy mode overwrite its source
  #13    the backends' overlay reader accepted files the wizard refuses
  #16    a backup named <stem>_yyyyMMdd_HHmmss was restored under another name
  #17    a swap that deletes the destination before it renames
  #18    mode 8 without -DeleteSource made photo_v2/_v3/... on every run
  #20    generate_thumbnails -Remove deleted any TIFF named *_thumb
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
import tifffile

REPO = Path(__file__).resolve().parent.parent
COMPRESS = REPO / "compress_tiff_zip.py"
THUMBS = REPO / "generate_thumbnails.py"
sys.path.insert(0, str(REPO))
import tiff_workflow_common as tw  # noqa: E402
from backends import LANES, backend_cmd  # noqa: E402
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
need_tools = [
    pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required"),
]
HOSTILE = "190815_Film_伊豆沼_夏 [FINAL]"
FIXTURES = Path(os.environ.get("TW_FIXTURES", r"E:\TESTE")) / "TIFF_scans_varied"
PS_SCRIPTS = ["compress_tiff_zip.py", "copy_exif_to_tiff.py", "copy_exif_to_tiff.py"]


def exif(path, *args) -> str:
    """exiftool with every argument on stdin (a Japanese path on the command line is mangled)."""
    data = ("\n".join(["-charset", "filename=utf8", *[str(a) for a in args], str(path)]) + "\n")
    r = subprocess.run(["exiftool", "-@", "-"], input=data.encode("utf-8"),
                       capture_output=True, timeout=120)
    return r.stdout.decode("utf-8", "replace").strip()


def layout(path):
    """[(spp, photometric, extrasamples)] per IFD."""
    out = exif(path, "-a", "-G1", "-s", "-s", "-n", "-SamplesPerPixel",
               "-PhotometricInterpretation", "-ExtraSamples")
    ifds = {}
    for line in out.splitlines():
        m = re.match(r"^\[IFD(\d+)\]\s+(\w+):\s*(.*)$", line)
        if m:
            ifds.setdefault(int(m.group(1)), {})[m.group(2)] = m.group(3).strip()
    return [(d.get("SamplesPerPixel", "1"), d.get("PhotometricInterpretation"),
             d.get("ExtraSamples", "")) for _, d in sorted(ifds.items())]


def env_off(**extra):
    e = dict(os.environ, TIFFWORKFLOW_SETTINGS="", TIFFWORKFLOW_NO_UPDATE_CHECK="1")
    e.update(extra)
    return e


def run(lane, script, args, cwd, env=None, timeout=900):
    cmd = backend_cmd(lane, script, [*[str(a) for a in args]])
    r = subprocess.run(cmd,
                       cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env or env_off())
    return r, r.stdout + r.stderr


def neutral_rgb(path: Path, h=40, w=56, bits=16):
    path.parent.mkdir(parents=True, exist_ok=True)
    dt = np.uint16 if bits == 16 else np.uint8
    g = np.random.default_rng(7).integers(0, np.iinfo(dt).max, size=(h, w), dtype=dt)
    tifffile.imwrite(str(path), np.stack([g, g, g], -1), photometric="rgb")
    return path


def color_rgb(path: Path, h=40, w=56):
    path.parent.mkdir(parents=True, exist_ok=True)
    a = np.random.default_rng(3).integers(0, 65535, size=(h, w, 3), dtype=np.uint16)
    tifffile.imwrite(str(path), a, photometric="rgb")
    return path


# ---------------------------------------------------------------------------------------------
# #1 / #2: the sample layout survives every mode
# ---------------------------------------------------------------------------------------------

# mode 2 without -OutputDir: the input root is the flat output, so each file directly in it
# is written over (the in-place replace). The fixtures here have no subfolders.
MODE_ARGS = {
    "mode2": ["--mode", 2],
    "mode1": ["--mode", 1],
    "mode8": ["--mode", 8, "--delete-source"],
    "mode9": ["--mode", 9],
}


def _output(root: Path, cam: Path, name: str, key: str) -> Path:
    return cam / "ZIP" / name if key == "mode1" else cam / name


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("key", list(MODE_ARGS))
class TestNeutralRgbStaysRgb:
    pytestmark = need_tools

    def test_neutral_rgb16_keeps_three_samples(self, tmp_path, lane, key):
        root = tmp_path / "root"
        cam = root / HOSTILE / "NORITSU"
        src = neutral_rgb(cam / "0014_14 (2).tif")
        ref = tifffile.imread(str(src))
        args = MODE_ARGS[key]
        r, out = run(lane, COMPRESS, ["--input-dir", cam, "--workers", 2, *args], tmp_path)
        assert r.returncode == 0, out[-3000:]
        dst = _output(root, cam, "0014_14 (2).tif", key)
        assert layout(dst) == [("3", "2", "")], (layout(dst), out[-2000:])
        assert np.array_equal(tifffile.imread(str(dst)), ref)


@pytest.mark.parametrize("lane", LANES)
class TestRealNeutralScans:
    """The sweep's two real files: a neutral RGB Photoshop scan and a VueScan RGB+IR scan."""
    pytestmark = need_tools + [pytest.mark.skipif(not FIXTURES.is_dir(), reason="real fixtures")]

    @pytest.mark.parametrize("fixture", ["rgb16_photoshop_noicc__000015990010.tif",
                                         "rgbi16_vuescan_srgb__0019.tif"])
    def test_mode9_keeps_every_sample(self, tmp_path, lane, fixture):
        cam = tmp_path / "root" / HOSTILE / "SCAN"
        cam.mkdir(parents=True)
        src = cam / "0014_14 (2).tif"
        shutil.copy(FIXTURES / fixture, src)
        ref = tifffile.imread(str(src))
        before = layout(src)
        r, out = run(lane, COMPRESS, ["--input-dir", tmp_path / "root", "--mode", 9, "--workers", 2],
                     tmp_path)
        assert r.returncode == 0, out[-3000:]
        got = tifffile.imread(str(src))
        assert got.shape == ref.shape and np.array_equal(got, ref), out[-2000:]
        assert [x[:2] for x in layout(src)] == [x[:2] for x in before]


@pytest.mark.parametrize("lane", LANES)
class TestLayoutGate:
    pytestmark = need_tools

    def test_neutral_rgb_page_next_to_a_gray_page_is_refused_not_collapsed(self, tmp_path, lane):
        """-type applies to every page of a write, so a neutral RGB page beside a gray IR page
        cannot be kept both ways: the gate must refuse it, original untouched."""
        root = tmp_path / "root"
        cam = root / HOSTILE / "SCAN"
        cam.mkdir(parents=True)
        src = cam / "scan (1).tif"
        g = np.random.default_rng(5).integers(0, 65535, size=(40, 56), dtype=np.uint16)
        with tifffile.TiffWriter(str(src)) as w:
            w.write(np.stack([g, g, g], -1), photometric="rgb")
            w.write(g, photometric="minisblack")
        orig = src.read_bytes()
        r, out = run(lane, COMPRESS, ["--input-dir", root, "--mode", 9, "--no-safe-mode",
                                       "--workers", 2], tmp_path)
        assert r.returncode == 1, out[-3000:]
        assert "sample layout changed" in out, out[-3000:]
        assert src.read_bytes() == orig
        assert not (cam / "OLD_TIFFs").exists() or not any((cam / "OLD_TIFFs").iterdir())

    def test_opaque_associated_alpha_stays_associated(self, tmp_path, lane):
        root = tmp_path / "root"
        cam = root / HOSTILE / "A"
        cam.mkdir(parents=True)
        src = cam / "alpha.tif"
        c = np.random.default_rng(2).integers(0, 65535, size=(40, 56, 3), dtype=np.uint16)
        a = np.full((40, 56), 65535, dtype=np.uint16)
        tifffile.imwrite(str(src), np.dstack([c, a]), photometric="rgb", extrasamples=[1])
        ref = tifffile.imread(str(src))
        r, out = run(lane, COMPRESS, ["--input-dir", root, "--mode", 9, "--workers", 2], tmp_path)
        assert r.returncode == 0, out[-3000:]
        assert layout(src) == [("4", "2", "1")], layout(src)
        assert np.array_equal(tifffile.imread(str(src)), ref)

    def test_translucent_associated_alpha_is_refused(self, tmp_path, lane):
        """magick un-premultiplies and re-premultiplies: the alpha samples come back changed
        (1791 of a real scan's) and no gate sees it. Refused, original untouched."""
        root = tmp_path / "root"
        cam = root / HOSTILE / "A"
        cam.mkdir(parents=True)
        src = cam / "alpha (2).tif"
        rng = np.random.default_rng(2)
        a = rng.integers(1, 65535, size=(40, 56), dtype=np.uint16)
        c = (rng.integers(0, 65535, size=(40, 56, 3)) * (a[..., None] / 65535)).astype(np.uint16)
        tifffile.imwrite(str(src), np.dstack([c, a]), photometric="rgb", extrasamples=[1])
        orig = src.read_bytes()
        r, out = run(lane, COMPRESS, ["--input-dir", root, "--mode", 9, "--workers", 2], tmp_path)
        assert r.returncode == 1, out[-3000:]
        assert "associated alpha that is not opaque" in out
        assert src.read_bytes() == orig


# ---------------------------------------------------------------------------------------------
# #3: IPTC in Japanese
# ---------------------------------------------------------------------------------------------

def _with_iptc(path: Path):
    color_rgb(path)
    data = "\n".join(["-charset", "filename=utf8", "-q", "-overwrite_original",
                      "-IPTC:CodedCharacterSet=UTF8", "-IPTC:Keywords=伊豆沼",
                      "-IPTC:Caption-Abstract=伊豆沼の夏", "-EXIF:Make=FUJIFILM", str(path)]) + "\n"
    subprocess.run(["exiftool", "-@", "-"], input=data.encode("utf-8"), check=True,
                   capture_output=True)
    return path


@pytest.mark.parametrize("lane", LANES)
class TestIptcSurvivesTheMieContainer:
    pytestmark = need_tools

    def test_percent_in_the_path_mode9(self, tmp_path, lane):
        root = tmp_path / "root"
        cam = root / f"{HOSTILE} 50%" / "NORITSU"
        src = _with_iptc(cam / "0014_14 (2).tif")
        r, out = run(lane, COMPRESS, ["--input-dir", root, "--mode", 9, "--workers", 2], tmp_path)
        assert r.returncode == 0, out[-3000:]
        assert exif(src, "-s3", "-IPTC:Keywords") == "伊豆沼", out[-2000:]
        assert exif(src, "-s3", "-IPTC:Caption-Abstract") == "伊豆沼の夏"

    def test_mode2_in_place_without_percent(self, tmp_path, lane):
        cam = tmp_path / HOSTILE / "NORITSU"
        src = _with_iptc(cam / "0014_14 (2).tif")
        r, out = run(lane, COMPRESS, ["--mode", 2, "--input-dir", cam], tmp_path)
        assert r.returncode == 0, out[-3000:]
        assert exif(src, "-s3", "-IPTC:Keywords") == "伊豆沼", out[-2000:]
        assert exif(src, "-s3", "-EXIF:Make") == "FUJIFILM"


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("folder", [HOSTILE, f"{HOSTILE} 50%"], ids=["plain", "percent"])
@need_tools[0]
def test_copy_exif_keeps_utf8_iptc_of_the_jpeg(tmp_path, lane, folder):
    """Review of round 28: the JPEG -> TIFF copy is '-IPTC:All' too, so the JPEG's UTF-8 IPTC
    reached the TIFF as '???' (with or without the MIE container), the run saying OK. The
    TIFF's own Latin IPTC must survive the change of charset as well."""
    script = "copy_exif_to_tiff.py"
    cam = tmp_path / folder / "S5pro"
    tif = color_rgb(cam / "_DSF4796 (2).tif")
    exif(tif, "-q", "-overwrite_original", "-IPTC:Credit=São Paulo")
    assert exif(tif, "-s3", "-IPTC:CodedCharacterSet") == ""      # Latin, as old software wrote it
    jpg = cam / "_DSF4796 (2).jpg"
    subprocess.run(["magick", "-size", "64x48", "gradient:", "-depth", "8", str(tmp_path / "j.jpg")],
                   check=True, capture_output=True)
    shutil.move(str(tmp_path / "j.jpg"), str(jpg))
    exif(jpg, "-q", "-overwrite_original", "-Make=FUJIFILM", "-Model=FinePix S5Pro",
         "-IPTC:CodedCharacterSet=UTF8", "-IPTC:Keywords=伊豆沼", "-IPTC:Caption-Abstract=伊豆沼の夏")
    assert exif(jpg, "-s3", "-IPTC:Keywords") == "伊豆沼"
    r, out = run(lane, REPO / script, ["--input-dir", cam, "--workers", 2], tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert exif(tif, "-s3", "-EXIF:Model") == "FinePix S5Pro", out[-2000:]
    assert exif(tif, "-s3", "-IPTC:Keywords") == "伊豆沼", out[-2000:]
    assert exif(tif, "-s3", "-IPTC:Caption-Abstract") == "伊豆沼の夏"
    assert exif(tif, "-s3", "-IPTC:Credit") == "São Paulo"


@pytest.mark.parametrize("lane", LANES)
@need_tools[0]
def test_copy_exif_creates_no_iptc_when_neither_file_has_one(tmp_path, lane):
    script = "copy_exif_to_tiff.py"
    cam = tmp_path / HOSTILE / "S5pro"
    tif = color_rgb(cam / "_DSF4796 (2).tif")
    jpg = cam / "_DSF4796 (2).jpg"
    subprocess.run(["magick", "-size", "64x48", "gradient:", "-depth", "8", str(tmp_path / "j.jpg")],
                   check=True, capture_output=True)
    shutil.move(str(tmp_path / "j.jpg"), str(jpg))
    exif(jpg, "-q", "-overwrite_original", "-Make=FUJIFILM", "-Model=FinePix S5Pro")
    r, out = run(lane, REPO / script, ["--input-dir", cam, "--workers", 2], tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert "] OK |" in out and "WARN" not in out, out[-3000:]
    assert exif(tif, "-s3", "-EXIF:Model") == "FinePix S5Pro"
    assert exif(tif, "-a", "-G1", "-s", "-IPTC:all") == ""


# ---------------------------------------------------------------------------------------------
# #8: an in-place write with -StagingDir is still verified (and still works)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
@need_tools[0]
def test_mode2_in_place_with_staging_replaces_through_the_gate(tmp_path, lane):
    cam = tmp_path / HOSTILE / "NORITSU"
    src = color_rgb(cam / "0014_14 (2).tif")
    ref = tifffile.imread(str(src))
    stg = tmp_path / "stg [x]"
    r, out = run(lane, COMPRESS, ["--mode", 2, "--input-dir", cam, "--staging-dir", stg], tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert "Moved 1 file(s)" in out and os.listdir(stg) == [], out[-2000:]
    assert exif(src, "-s3", "-IFD0:Compression") in ("Adobe Deflate", "Deflate")
    assert np.array_equal(tifffile.imread(str(src)), ref)


# ---------------------------------------------------------------------------------------------
# #7: a failed metadata restore blocks the replace -- a real run with the failure injected
# (review of round 28: the fix shipped with static checks only)
# ---------------------------------------------------------------------------------------------

_EXIFTOOL_SHIM = r'''import os, subprocess, sys
data = sys.stdin.buffer.read() if "-" in sys.argv[1:] else None
if data and b"-tagsfromfile" in data and b"-all:all" in data and b"-overwrite_original" in data:
    sys.stderr.write("Error: injected failure\n")      # what a full staging disk does
    sys.exit(1)
r = subprocess.run([os.environ["TW_REAL_EXIFTOOL"]] + sys.argv[1:], input=data, capture_output=True)
sys.stdout.buffer.write(r.stdout)
sys.stderr.buffer.write(r.stderr)
sys.exit(r.returncode)
'''


def _failing_restore_env(tmp_path):
    """PATH with an `exiftool` in front that fails the metadata restore (-tagsfromfile ...
    -all:all) and passes every other call to the real one."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "shim_exiftool.py").write_text(_EXIFTOOL_SHIM, encoding="utf-8")
    (shim / "exiftool.cmd").write_text(
        '@echo off\r\n"' + sys.executable + '" "%~dp0shim_exiftool.py" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="ascii", newline="")
    env = env_off(TW_REAL_EXIFTOOL=shutil.which("exiftool"))
    env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    return env


@pytest.mark.skipif(os.name != "nt", reason="the injected exiftool is a .cmd")
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("key", ["mode2", "mode8", "mode9"])
@need_tools[0]
def test_a_failed_metadata_restore_never_replaces_the_original(tmp_path, lane, key):
    cam = tmp_path / HOSTILE / "NORITSU"
    src = color_rgb(cam / "0014_14 (2).tif")
    exif(src, "-q", "-overwrite_original", "-EXIF:Make=FUJIFILM")
    orig = src.read_bytes()
    env = _failing_restore_env(tmp_path)
    r, out = run(lane, COMPRESS, ["--input-dir", cam, "--workers", 2, *MODE_ARGS[key]],
                 tmp_path, env=env)
    if key == "mode9":
        # the original is kept in OLD_TIFFs, so this stays a warning (and exit 0)
        assert r.returncode == 0 and "WARN (exiftool failed, ZIP ok)" in out, out[-3000:]
        assert (cam / "OLD_TIFFs" / src.name).read_bytes() == orig
        return
    assert r.returncode == 1, out[-3000:]
    assert "metadata restore failed" in out, out[-3000:]
    assert src.read_bytes() == orig, "a file without its metadata replaced the original"
    assert exif(src, "-s3", "-EXIF:Make") == "FUJIFILM"


# ---------------------------------------------------------------------------------------------
# #17: move_file_replacing never leaves the destination missing
# ---------------------------------------------------------------------------------------------

def test_move_file_replacing_is_one_rename(tmp_path):
    """A swap that deletes the destination first and renames after left the place of a photo
    empty when the run was killed in between: 4792 looks out of 60764 saw it missing. One
    rename (MoveFileEx REPLACE_EXISTING, os.replace) never does."""
    d = tmp_path / HOSTILE
    d.mkdir()
    target = d / "0014_14 (2).tif"
    target.write_bytes(bytes(4096))
    stop = threading.Event()
    seen = {"n": 0, "missing": 0}

    def watch():
        while not stop.is_set():
            if os.path.exists(target):
                seen["n"] += 1
            else:
                seen["missing"] += 1

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    try:
        for i in range(400):
            src = d / f"0014_14 (2).tif.tmp_{i}.tif"
            src.write_bytes(bytes(4096))
            for _attempt in range(20):
                try:
                    tw.move_file_replacing(str(src), str(target))
                    break
                except PermissionError:      # the watcher had it open for its look
                    import time
                    time.sleep(0.005)
            else:
                pytest.fail(f"swap {i} never went through")
    finally:
        stop.set()
        w.join(5)
    assert seen["n"] > 100
    assert seen["missing"] == 0, seen
    assert sorted(x.name for x in d.iterdir()) == ["0014_14 (2).tif"]


# ---------------------------------------------------------------------------------------------
# Review of round 28: the rename refuses a READ-ONLY destination, which the old swap
# replaced -- mode 8 and every in-place swap failed on a scan that kept the attribute
# ---------------------------------------------------------------------------------------------

def _writable_again(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="an open file blocks a rename on Windows only")
def test_move_file_replacing_replaces_a_read_only_destination(tmp_path):
    """...and a swap that still fails (the destination is held open) leaves the original
    read-only, exactly as it was."""
    d = tmp_path / f"{HOSTILE} 50%"
    d.mkdir()
    to = d / "0014_14 (2).tif"
    src = d / "0014_14 (2).tif.0a1b2c3d_0123456789ab.tif"
    to.write_bytes(b"\x01\x01\x01")
    src.write_bytes(b"\x02\x02\x02\x02")
    os.chmod(to, stat.S_IREAD)
    held = d / "held.tif"
    src2 = d / "held.tif.0a1b2c3d_0123456789ab.tif"
    held.write_bytes(b"\x03\x03\x03")
    src2.write_bytes(b"\x04\x04\x04\x04")
    os.chmod(held, stat.S_IREAD)
    try:
        tw.move_file_replacing(str(src), str(to))
        assert to.read_bytes() == b"\x02\x02\x02\x02" and not src.exists()
        assert os.stat(to).st_mode & stat.S_IWRITE, "the new file is not read-only"

        with open(held, "rb"):
            with pytest.raises(OSError):
                tw.move_file_replacing(str(src2), str(held))
        assert held.read_bytes() == b"\x03\x03\x03" and src2.exists()
        assert not os.stat(held).st_mode & stat.S_IWRITE, "the attribute was not put back"
    finally:
        _writable_again(tmp_path)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("key", ["mode2", "mode8"])
@need_tools[0]
def test_a_read_only_original_is_still_replaced(tmp_path, lane, key):
    """v2.9.0 compressed a read-only TIFF in place and in mode 8; with the first
    Move-FileReplacing both said "move failed" / "in-place replace failed"."""
    cam = tmp_path / HOSTILE / "NORITSU"
    src = color_rgb(cam / "0014_14 (2).tif")
    ref = tifffile.imread(str(src))
    os.chmod(src, stat.S_IREAD)
    try:
        r, out = run(lane, COMPRESS, ["--input-dir", cam, "--workers", 2, *MODE_ARGS[key]],
                     tmp_path)
        assert r.returncode == 0, out[-3000:]
        assert exif(src, "-s3", "-IFD0:Compression") in ("Adobe Deflate", "Deflate"), out[-2000:]
        assert np.array_equal(tifffile.imread(str(src)), ref)
        # no staged copy or temp sibling left behind
        tiffs = [p.name for p in cam.rglob("*") if p.suffix.lower() in (".tif", ".tiff")]
        assert tiffs == ["0014_14 (2).tif"], out[-2000:]
    finally:
        _writable_again(tmp_path)


# ---------------------------------------------------------------------------------------------
# #9: an overlay folder name never turns a copy mode into an in-place replace
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
@need_tools[0]
def test_overlay_export_name_cannot_make_mode7_overwrite(tmp_path, lane):
    root = tmp_path / "root"
    src = color_rgb(root / HOSTILE / "_EXPORT" / "TIFF" / "0014_14 (2).tif")
    orig = src.read_bytes()
    ov = tmp_path / "ov [x].json"
    ov.write_text(json.dumps({"compress_tiff_zip": {"ExportZipSubfolder": "TIFF"}}), encoding="utf-8")
    r, out = run(lane, COMPRESS, ["--input-dir", root, "--mode", 7, "--workers", 1], tmp_path,
                 env=env_off(TIFFWORKFLOW_SETTINGS=str(ov)))
    assert "ExportZipSubfolder" in out
    assert r.returncode == 1, out[-3000:]
    assert "would write over its own source" in out, out[-3000:]
    assert src.read_bytes() == orig


# ---------------------------------------------------------------------------------------------
# #13: the backends' overlay reader refuses what the wizard refuses
# ---------------------------------------------------------------------------------------------

BAD_FILES = {
    "line comment": b'{"s": {"X": 5} // c\n}',
    "block comment": b'{"s": /* c */ {"X": 5}}',
    "trailing comma": b'{"s": {"X": 5,}}',
    "single quotes": b"{'s': {'X': 5}}",
    "bare key": b'{"s": {X: 5}}',
    "hex": b'{"s": {"X": 0x10}}',
    "leading zero": b'{"s": {"X": 010}}',
    "utf-16": '{"s": {"X": 5}}'.encode("utf-16"),
    "cp932 bytes": '{"s": {"X": 5, "N": "伊豆沼"}}'.encode("cp932"),
    "text after the end": b'{"s": {"X": 5}} x',
    "empty key": b'{"s": {"X": 5, "": 1}}',
    "keys that differ in case": b'{"s": {"X": 5, "x": 6}}',
    "root array": b'[{"s": {"X": 5}}]',
    "section is not an object": b'{"s": [1, 2]}',
    "empty file": b'',
}
GOOD_FILES = {
    "plain": b'{"s": {"X": 5}}',
    "utf-8 bom": b'\xef\xbb\xbf{"s": {"X": 5}}',
    "unicode name": '{"s": {"X": 5, "N": "伊豆沼 [x]"}}'.encode("utf-8"),
    "escapes": b'{"s": {"X": 5, "N": "a\\u00e9\\n\\"b"}, "t": [1, -2.5e3, true, null]}',
}


@pytest.mark.parametrize("label", list(BAD_FILES) + list(GOOD_FILES))
def test_overlay_reader_is_as_strict_as_the_wizard(tmp_path, monkeypatch, label):
    """The wizard plans with the settings file and the backend runs with it: a file one of
    them reads and the other refuses means a run with another value than the one shown."""
    import convert_tiff
    good = label in GOOD_FILES
    f = tmp_path / "写真 [x]" / "ov.json"
    f.parent.mkdir()
    f.write_bytes(GOOD_FILES[label] if good else BAD_FILES[label])
    try:
        sec = convert_tiff._settings_overlay_section(f, "s")
        wizard = "read" if sec and "X" in sec else "refused"
    except (ValueError, OSError):
        wizard = "refused"
    monkeypatch.setenv("TIFFWORKFLOW_SETTINGS", str(f))
    got = tw.read_settings_overlay("s", {"X": "int:1:"}, "")
    backend = "read" if got["values"].get("X") == 5 else "refused"
    assert wizard == backend == ("read" if good else "refused"), (label, wizard, backend, got)
    if not good:
        assert got["values"] == {}
        assert got["warnings"], "a refused file says so"


# ---------------------------------------------------------------------------------------------
# #16: a stamped name that is the photo's own name is restored under that name
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
@need_tools[0]
def test_stranded_backup_with_a_date_stamped_name_keeps_its_name(tmp_path, lane):
    root = tmp_path / "root"
    old = root / HOSTILE / "OLD_TIFFs"
    b = color_rgb(old / "Scan_20190815_143000.tif")
    orig = b.read_bytes()
    ref = tifffile.imread(str(b))
    r, out = run(lane, COMPRESS, ["--input-dir", root, "--mode", 9, "--restore-stranded"], tmp_path)
    restored = [ln for ln in out.splitlines() if "RESTORED (stranded original)" in ln]
    assert restored and restored[0].endswith("Scan_20190815_143000.tif"), out[-3000:]
    # restored under its own name -- and then compressed by the same mode 9 run, the
    # original archived again
    assert np.array_equal(tifffile.imread(str(root / HOSTILE / "Scan_20190815_143000.tif")), ref)
    assert (old / "Scan_20190815_143000.tif").read_bytes() == orig
    assert not (root / HOSTILE / "Scan.tif").exists()


@pytest.mark.parametrize("lane", LANES)
@need_tools[0]
def test_a_real_collision_copy_still_restores_to_the_base_name(tmp_path, lane):
    """OLD_TIFFs holds photo.tif AND photo_20190815_143000.tif: the stamp is the collision
    rename, so with nothing in the parent folder the base name is the stranded one."""
    root = tmp_path / "root"
    old = root / HOSTILE / "OLD_TIFFs"
    color_rgb(old / "photo.tif")
    color_rgb(old / "photo_20190815_143000.tif")
    r, out = run(lane, COMPRESS, ["--input-dir", root, "--mode", 9], tmp_path)
    assert "STRANDED" in out
    assert "photo.tif" in out


# ---------------------------------------------------------------------------------------------
# #18: mode 8 without -DeleteSource does not multiply copies
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
@need_tools[0]
def test_mode8_tiff_source_second_run_skips(tmp_path, lane):
    root = tmp_path / "root"
    cam = root / HOSTILE
    color_rgb(cam / "0014_14 (2).tiff")
    for _ in range(3):
        r, out = run(lane, COMPRESS, ["--input-dir", root, "--mode", 8, "--workers", 1], tmp_path)
        assert r.returncode == 0, out[-3000:]
    names = sorted(p.name for p in cam.iterdir() if p.is_file())
    assert names == ["0014_14 (2).tif", "0014_14 (2).tiff"], names


# ---------------------------------------------------------------------------------------------
# #20: -Remove keeps a TIFF too large to be a thumbnail this script wrote
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
@need_tools[0]
def test_remove_keeps_a_photo_named_like_a_thumbnail(tmp_path, lane):
    d = tmp_path / HOSTILE
    big = color_rgb(d / "hitchhiker_thumb.tif", h=800, w=1200)
    small = color_rgb(d / "photo_thumb.tif", h=100, w=150)
    orig = big.read_bytes()
    r, out = run(lane, THUMBS, ["--input-dir", d, "--remove"], tmp_path)
    assert big.read_bytes() == orig, out[-2000:]
    assert not small.exists(), out[-2000:]
    assert "not a thumbnail this script wrote" in out
