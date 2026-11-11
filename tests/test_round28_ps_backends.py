"""Audit round 28 (AI_tools/261111_Fable_review_bug-sweep.md): the PowerShell backends.

Real runs under both shells in a hostile folder (Japanese, [ ], a space, and a '%' where it
matters), plus static checks for the paths a test cannot reach without sabotaging a tool:
  #1/#2  magick wrote a neutral RGB file as 1-channel gray (RGB+IR as 2 samples) -> -type
         from the source layout, and a layout gate after every write
  #3     IPTC text outside Latin came back as '?' through the MIE container
  #6/#7  a failed metadata restore (or an exception in a PS7 runspace) let the file without
         EXIF replace the original
  #8     with -StagingDir the in-place gate was skipped
  #9     an overlay folder name made a copy mode overwrite its source
  #13    the PowerShell overlay readers accepted files the wizard refuses
  #16    a backup named <stem>_yyyyMMdd_HHmmss was restored under another name
  #17    Move-Item -Force deletes the destination before it renames
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
COMPRESS = REPO / "compress_tiff_zip.ps1"
THUMBS = REPO / "generate_thumbnails.ps1"
SHELLS = [s for s in ("powershell", "pwsh") if shutil.which(s)]
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
need_tools = [
    pytest.mark.skipif(not SHELLS, reason="PowerShell required"),
    pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required"),
]
HOSTILE = "190815_Film_伊豆沼_夏 [FINAL]"
FIXTURES = Path(os.environ.get("TW_FIXTURES", r"E:\TESTE")) / "TIFF_scans_varied"
PS_SCRIPTS = ["compress_tiff_zip.ps1", "copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"]


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


def run(shell, script, args, cwd, env=None, timeout=900):
    cmd = [shell, "-NoProfile", "-File", str(script), *[str(a) for a in args]]
    if shell == "powershell" and any(str(a).endswith(":$false") for a in args):
        import convert_tiff   # PS5 cannot bind -Switch:$false through -File (AGENTS.md)
        cmd = convert_tiff._wrap_ps5_command(cmd)
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

MODE_ARGS = {
    "legacy": [],
    "mode1": ["-Mode", 1],
    "mode8": ["-Mode", 8, "-DeleteSource"],
    "mode9": ["-Mode", 9],
}


def _output(root: Path, cam: Path, name: str, key: str) -> Path:
    return cam / "ZIP" / name if key == "mode1" else cam / name


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("key", list(MODE_ARGS))
class TestNeutralRgbStaysRgb:
    pytestmark = need_tools

    def test_neutral_rgb16_keeps_three_samples(self, tmp_path, shell, key):
        root = tmp_path / "root"
        cam = root / HOSTILE / "NORITSU"
        src = neutral_rgb(cam / "0014_14 (2).tif")
        ref = tifffile.imread(str(src))
        args = MODE_ARGS[key]
        if key == "legacy":
            r, out = run(shell, COMPRESS, [], cam)
        else:
            r, out = run(shell, COMPRESS, ["-InputDir", cam, "-Workers", 2, *args], tmp_path)
        assert r.returncode == 0, out[-3000:]
        dst = _output(root, cam, "0014_14 (2).tif", key)
        assert layout(dst) == [("3", "2", "")], (layout(dst), out[-2000:])
        assert np.array_equal(tifffile.imread(str(dst)), ref)


@pytest.mark.parametrize("shell", SHELLS)
class TestRealNeutralScans:
    """The sweep's two real files: a neutral RGB Photoshop scan and a VueScan RGB+IR scan."""
    pytestmark = need_tools + [pytest.mark.skipif(not FIXTURES.is_dir(), reason="real fixtures")]

    @pytest.mark.parametrize("fixture", ["rgb16_photoshop_noicc__000015990010.tif",
                                         "rgbi16_vuescan_srgb__0019.tif"])
    def test_mode9_keeps_every_sample(self, tmp_path, shell, fixture):
        cam = tmp_path / "root" / HOSTILE / "SCAN"
        cam.mkdir(parents=True)
        src = cam / "0014_14 (2).tif"
        shutil.copy(FIXTURES / fixture, src)
        ref = tifffile.imread(str(src))
        before = layout(src)
        r, out = run(shell, COMPRESS, ["-InputDir", tmp_path / "root", "-Mode", 9, "-Workers", 2],
                     tmp_path)
        assert r.returncode == 0, out[-3000:]
        got = tifffile.imread(str(src))
        assert got.shape == ref.shape and np.array_equal(got, ref), out[-2000:]
        assert [x[:2] for x in layout(src)] == [x[:2] for x in before]


@pytest.mark.parametrize("shell", SHELLS)
class TestLayoutGate:
    pytestmark = need_tools

    def test_neutral_rgb_page_next_to_a_gray_page_is_refused_not_collapsed(self, tmp_path, shell):
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
        r, out = run(shell, COMPRESS, ["-InputDir", root, "-Mode", 9, "-SafeMode:$false",
                                       "-Workers", 2], tmp_path)
        assert r.returncode == 1, out[-3000:]
        assert "sample layout changed" in out, out[-3000:]
        assert src.read_bytes() == orig
        assert not (cam / "OLD_TIFFs").exists() or not any((cam / "OLD_TIFFs").iterdir())

    def test_opaque_associated_alpha_stays_associated(self, tmp_path, shell):
        root = tmp_path / "root"
        cam = root / HOSTILE / "A"
        cam.mkdir(parents=True)
        src = cam / "alpha.tif"
        c = np.random.default_rng(2).integers(0, 65535, size=(40, 56, 3), dtype=np.uint16)
        a = np.full((40, 56), 65535, dtype=np.uint16)
        tifffile.imwrite(str(src), np.dstack([c, a]), photometric="rgb", extrasamples=[1])
        ref = tifffile.imread(str(src))
        r, out = run(shell, COMPRESS, ["-InputDir", root, "-Mode", 9, "-Workers", 2], tmp_path)
        assert r.returncode == 0, out[-3000:]
        assert layout(src) == [("4", "2", "1")], layout(src)
        assert np.array_equal(tifffile.imread(str(src)), ref)

    def test_translucent_associated_alpha_is_refused(self, tmp_path, shell):
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
        r, out = run(shell, COMPRESS, ["-InputDir", root, "-Mode", 9, "-Workers", 2], tmp_path)
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


@pytest.mark.parametrize("shell", SHELLS)
class TestIptcSurvivesTheMieContainer:
    pytestmark = need_tools

    def test_percent_in_the_path_mode9(self, tmp_path, shell):
        root = tmp_path / "root"
        cam = root / f"{HOSTILE} 50%" / "NORITSU"
        src = _with_iptc(cam / "0014_14 (2).tif")
        r, out = run(shell, COMPRESS, ["-InputDir", root, "-Mode", 9, "-Workers", 2], tmp_path)
        assert r.returncode == 0, out[-3000:]
        assert exif(src, "-s3", "-IPTC:Keywords") == "伊豆沼", out[-2000:]
        assert exif(src, "-s3", "-IPTC:Caption-Abstract") == "伊豆沼の夏"

    def test_legacy_in_place_without_percent(self, tmp_path, shell):
        cam = tmp_path / HOSTILE / "NORITSU"
        src = _with_iptc(cam / "0014_14 (2).tif")
        r, out = run(shell, COMPRESS, [], cam)
        assert r.returncode == 0, out[-3000:]
        assert exif(src, "-s3", "-IPTC:Keywords") == "伊豆沼", out[-2000:]
        assert exif(src, "-s3", "-EXIF:Make") == "FUJIFILM"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("folder", [HOSTILE, f"{HOSTILE} 50%"], ids=["plain", "percent"])
@need_tools[0]
@need_tools[1]
def test_copy_exif_keeps_utf8_iptc_of_the_jpeg(tmp_path, shell, folder):
    """Review of round 28: the JPEG -> TIFF copy is '-IPTC:All' too, so the JPEG's UTF-8 IPTC
    reached the TIFF as '???' (with or without the MIE container), the run saying OK. The
    TIFF's own Latin IPTC must survive the change of charset as well."""
    script = "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
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
    r, out = run(shell, REPO / script, ["-InputDir", cam, "-Workers", 2], tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert exif(tif, "-s3", "-EXIF:Model") == "FinePix S5Pro", out[-2000:]
    assert exif(tif, "-s3", "-IPTC:Keywords") == "伊豆沼", out[-2000:]
    assert exif(tif, "-s3", "-IPTC:Caption-Abstract") == "伊豆沼の夏"
    assert exif(tif, "-s3", "-IPTC:Credit") == "São Paulo"


@pytest.mark.parametrize("shell", SHELLS)
@need_tools[0]
@need_tools[1]
def test_copy_exif_creates_no_iptc_when_neither_file_has_one(tmp_path, shell):
    script = "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
    cam = tmp_path / HOSTILE / "S5pro"
    tif = color_rgb(cam / "_DSF4796 (2).tif")
    jpg = cam / "_DSF4796 (2).jpg"
    subprocess.run(["magick", "-size", "64x48", "gradient:", "-depth", "8", str(tmp_path / "j.jpg")],
                   check=True, capture_output=True)
    shutil.move(str(tmp_path / "j.jpg"), str(jpg))
    exif(jpg, "-q", "-overwrite_original", "-Make=FUJIFILM", "-Model=FinePix S5Pro")
    r, out = run(shell, REPO / script, ["-InputDir", cam, "-Workers", 2], tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert "] OK |" in out and "WARN" not in out, out[-3000:]
    assert exif(tif, "-s3", "-EXIF:Model") == "FinePix S5Pro"
    assert exif(tif, "-a", "-G1", "-s", "-IPTC:all") == ""


# ---------------------------------------------------------------------------------------------
# #8: legacy with -StagingDir is still verified (and still works)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shell", SHELLS)
@need_tools[0]
@need_tools[1]
def test_legacy_with_staging_replaces_through_the_gate(tmp_path, shell):
    cam = tmp_path / HOSTILE / "NORITSU"
    src = color_rgb(cam / "0014_14 (2).tif")
    ref = tifffile.imread(str(src))
    r, out = run(shell, COMPRESS, ["-StagingDir", tmp_path / "stg [x]"], cam)
    assert r.returncode == 0, out[-3000:]
    assert exif(src, "-s3", "-IFD0:Compression") in ("Adobe Deflate", "Deflate")
    assert np.array_equal(tifffile.imread(str(src)), ref)


def test_the_replace_gate_keys_on_the_destination_not_on_the_temp_sibling():
    s = COMPRESS.read_text(encoding="ascii")
    # Process-TiffJob and the mode -Parallel worker
    assert s.count("$replacesSource = [bool]$inPlaceFinalDst -or") == 2
    # the legacy -Parallel worker
    assert "if ($inPlaceFinalDst -or ($finalDst -eq $src)) {" in s
    # a failed metadata restore blocks the replace in all three, and mode 8 in both
    assert s.count("$inPlaceOk = -not $exifWarn") == 3
    assert s.count('if ($exifWarn) { $integrityReason = "metadata restore failed" }') == 2


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
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("key", ["legacy", "mode8", "mode9"])
@need_tools[0]
@need_tools[1]
def test_a_failed_metadata_restore_never_replaces_the_original(tmp_path, shell, key):
    cam = tmp_path / HOSTILE / "NORITSU"
    src = color_rgb(cam / "0014_14 (2).tif")
    exif(src, "-q", "-overwrite_original", "-EXIF:Make=FUJIFILM")
    orig = src.read_bytes()
    env = _failing_restore_env(tmp_path)
    if key == "legacy":
        r, out = run(shell, COMPRESS, ["-Workers", 2], cam, env=env)
    else:
        r, out = run(shell, COMPRESS, ["-InputDir", cam, "-Workers", 2, *MODE_ARGS[key]],
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
# #6: the restore result starts as a failure in every worker
# ---------------------------------------------------------------------------------------------

def test_copy_exit_starts_failed_and_the_catch_keeps_it():
    s = COMPRESS.read_text(encoding="ascii")
    assert "$copyExit = 0" not in s
    assert s.count("$copyExit = 1\n") + s.count("$copyExit = 1\r\n") == 6   # init + catch, x3


def test_every_magick_write_gets_the_type_args_and_every_worker_gates_the_layout():
    s = COMPRESS.read_text(encoding="ascii")
    writes = re.findall(r"magick -quiet [^\n]*-compress zip[^\n]*", s)
    assert len(writes) == 9, writes
    assert all("@typeArgs" in w for w in writes), writes
    assert s.count("Test-TiffSampleLayoutKept -SrcPages $layoutPages") == 3
    for script in PS_SCRIPTS[1:]:
        t = (REPO / script).read_text(encoding="ascii")
        assert "-compress zip @typeArgs" in t
        assert "Test-TiffSampleLayoutKept -SrcPages $srcLayout.Pages" in t


def test_no_move_item_force_onto_a_photo():
    for script in PS_SCRIPTS:
        s = (REPO / script).read_text(encoding="ascii")
        code = "\n".join(ln for ln in s.splitlines() if not ln.lstrip().startswith("#"))
        code = re.sub(r"<#.*?#>", "", code, flags=re.S)
        assert "Move-Item -Force -LiteralPath $stagePath" not in code, script
        assert "-Destination $inPlaceFinalDst -Force" not in code, script
        assert "-Destination $To -Force" not in code, script
        assert "-Destination $FinalDst -Force" not in code, script


# ---------------------------------------------------------------------------------------------
# #17: Move-FileReplacing never leaves the destination missing
# ---------------------------------------------------------------------------------------------

def _function(text, name):
    m = re.search(r"^function\s+" + re.escape(name) + r"\s*\{", text, re.M)
    i, depth = m.end() - 1, 0
    while i < len(text):
        depth += (text[i] == "{") - (text[i] == "}")
        if depth == 0:
            break
        i += 1
    return text[m.start():i + 1]


@pytest.mark.parametrize("shell", SHELLS)
def test_move_file_replacing_is_one_rename(tmp_path, shell):
    d = tmp_path / HOSTILE
    d.mkdir()
    fn = _function(COMPRESS.read_text(encoding="ascii"), "Move-FileReplacing")
    harness = tmp_path / "h.ps1"
    harness.write_text("param([string]$Dir)\n" + fn + r"""
$to = Join-Path $Dir "0014_14 (2).tif"
[System.IO.File]::WriteAllBytes($to, (New-Object byte[] 4096))
for ($i = 0; $i -lt 400; $i++) {
    $from = "$to.tmp_$i.tif"
    [System.IO.File]::WriteAllBytes($from, (New-Object byte[] 4096))
    for ($k = 0; $k -lt 20; $k++) { try { Move-FileReplacing -From $from -To $to; break } catch { Start-Sleep -Milliseconds 5 } }
}
[System.IO.File]::WriteAllText((Join-Path $Dir "done"), "1")
""", encoding="utf-8-sig")
    target = d / "0014_14 (2).tif"
    stop = threading.Event()
    seen = {"n": 0, "missing": 0}

    def watch():
        while not stop.is_set():
            if (d / "done").exists():
                return
            if target.exists() or (d / "done").exists():
                seen["n"] += 1
            elif seen["n"]:
                seen["missing"] += 1

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    r = subprocess.run([shell, "-NoProfile", "-File", str(harness), "-Dir", str(d)],
                       capture_output=True, text=True, timeout=300)
    stop.set()
    w.join(5)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (d / "done").exists(), r.stdout + r.stderr
    assert seen["n"] > 100
    assert seen["missing"] == 0, seen
    assert sorted(p.name for p in d.iterdir()) == ["0014_14 (2).tif", "done"]


# ---------------------------------------------------------------------------------------------
# Review of round 28: MoveFileEx refuses a READ-ONLY destination, which Move-Item -Force
# replaced -- mode 8 and every in-place swap failed on a scan that kept the attribute
# ---------------------------------------------------------------------------------------------

def _writable_again(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IWRITE)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("script", PS_SCRIPTS)
def test_move_file_replacing_replaces_a_read_only_destination(tmp_path, shell, script):
    """...and a swap that still fails (the destination is held open) leaves the original
    read-only, exactly as it was."""
    d = tmp_path / f"{HOSTILE} 50%"
    d.mkdir()
    fn = _function((REPO / script).read_text(encoding="ascii"), "Move-FileReplacing")
    harness = tmp_path / "h.ps1"
    harness.write_text("param([string]$Dir)\n" + fn + r"""
$to = Join-Path $Dir "0014_14 (2).tif"
$from = "$to.0a1b2c3d_0123456789ab.tif"
[System.IO.File]::WriteAllBytes($to, [byte[]](1, 1, 1))
[System.IO.File]::WriteAllBytes($from, [byte[]](2, 2, 2, 2))
(Get-Item -LiteralPath $to).IsReadOnly = $true
$r = @{}
try { Move-FileReplacing -From $from -To $to; $r.swap = "ok" } catch { $r.swap = "threw: $($_.Exception.Message)" }
$r.len = (Get-Item -LiteralPath $to).Length
$r.readonly = (Get-Item -LiteralPath $to).IsReadOnly
$r.fromLeft = [System.IO.File]::Exists($from)

$held = Join-Path $Dir "held.tif"
$from2 = "$held.0a1b2c3d_0123456789ab.tif"
[System.IO.File]::WriteAllBytes($held, [byte[]](3, 3, 3))
[System.IO.File]::WriteAllBytes($from2, [byte[]](4, 4, 4, 4))
(Get-Item -LiteralPath $held).IsReadOnly = $true
$lock = [System.IO.File]::Open($held, 'Open', 'Read', 'Read')
try { Move-FileReplacing -From $from2 -To $held; $r.heldSwap = "ok" } catch { $r.heldSwap = "threw" }
$lock.Dispose()
$r.heldLen = (Get-Item -LiteralPath $held).Length
$r.heldReadonly = (Get-Item -LiteralPath $held).IsReadOnly
$r.from2Left = [System.IO.File]::Exists($from2)
[System.IO.File]::WriteAllText((Join-Path $Dir "result.json"), ($r | ConvertTo-Json))
""", encoding="utf-8-sig")
    r = subprocess.run([shell, "-NoProfile", "-File", str(harness), "-Dir", str(d)],
                       capture_output=True, text=True, timeout=300)
    try:
        assert r.returncode == 0 and (d / "result.json").exists(), r.stdout + r.stderr
        got = json.loads((d / "result.json").read_text(encoding="utf-8-sig"))
        assert got["swap"] == "ok", got
        assert (got["len"], got["readonly"], got["fromLeft"]) == (4, False, False), got
        assert got["heldSwap"] == "threw", got
        assert (got["heldLen"], got["heldReadonly"], got["from2Left"]) == (3, True, True), got
    finally:
        _writable_again(tmp_path)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("key", ["legacy", "mode8"])
@need_tools[0]
@need_tools[1]
def test_a_read_only_original_is_still_replaced(tmp_path, shell, key):
    """v2.9.0 compressed a read-only TIFF in place and in mode 8; with the first
    Move-FileReplacing both said "move failed" / "in-place replace failed"."""
    cam = tmp_path / HOSTILE / "NORITSU"
    src = color_rgb(cam / "0014_14 (2).tif")
    ref = tifffile.imread(str(src))
    os.chmod(src, stat.S_IREAD)
    try:
        if key == "legacy":
            r, out = run(shell, COMPRESS, [], cam)
        else:
            r, out = run(shell, COMPRESS, ["-InputDir", cam, "-Workers", 2, *MODE_ARGS[key]],
                         tmp_path)
        assert r.returncode == 0, out[-3000:]
        assert exif(src, "-s3", "-IFD0:Compression") in ("Adobe Deflate", "Deflate"), out[-2000:]
        assert np.array_equal(tifffile.imread(str(src)), ref)
        # no staged copy or temp sibling left behind (legacy keeps its log under Logs\)
        tiffs = [p.name for p in cam.rglob("*") if p.suffix.lower() in (".tif", ".tiff")]
        assert tiffs == ["0014_14 (2).tif"], out[-2000:]
    finally:
        _writable_again(tmp_path)


# ---------------------------------------------------------------------------------------------
# #9: an overlay folder name never turns a copy mode into an in-place replace
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shell", SHELLS)
@need_tools[0]
@need_tools[1]
def test_overlay_export_name_cannot_make_mode7_overwrite(tmp_path, shell):
    root = tmp_path / "root"
    src = color_rgb(root / HOSTILE / "_EXPORT" / "TIFF" / "0014_14 (2).tif")
    orig = src.read_bytes()
    ov = tmp_path / "ov [x].json"
    ov.write_text(json.dumps({"compress_tiff_zip": {"ExportZipSubfolder": "TIFF"}}), encoding="utf-8")
    r, out = run(shell, COMPRESS, ["-InputDir", root, "-Mode", 7, "-Workers", 1], tmp_path,
                 env=env_off(TIFFWORKFLOW_SETTINGS=str(ov)))
    assert "ExportZipSubfolder" in out
    assert r.returncode == 1, out[-3000:]
    assert "would write over its own source" in out, out[-3000:]
    assert src.read_bytes() == orig


# ---------------------------------------------------------------------------------------------
# #13: the PowerShell overlay readers refuse what the wizard refuses
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
}
GOOD_FILES = {
    "plain": b'{"s": {"X": 5}}',
    "utf-8 bom": b'\xef\xbb\xbf{"s": {"X": 5}}',
    "unicode name": '{"s": {"X": 5, "N": "伊豆沼 [x]"}}'.encode("utf-8"),
    "escapes": b'{"s": {"X": 5, "N": "a\\u00e9\\n\\"b"}, "t": [1, -2.5e3, true, null]}',
}

_HARNESS = r"""
param([string]$FunctionFile, [string]$Dir, [string]$OutFile)
. $FunctionFile
$out = New-Object System.Collections.Generic.List[string]
foreach ($f in (Get-ChildItem -LiteralPath $Dir -Filter '*.json' | Sort-Object Name)) {
    $env:TIFFWORKFLOW_SETTINGS = $f.FullName
    $r = Get-SettingsOverlay -Section 's' -Specs @{ 'X' = 'int:1:' } -ScriptRoot ''
    $v = if ($r.Values.ContainsKey('X')) { 'read' } else { 'refused' }
    $out.Add($f.BaseName + '=' + $v)
}
[System.IO.File]::WriteAllText($OutFile, ($out -join "`n"), [System.Text.UTF8Encoding]::new($false))
"""


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("script", ["compress_tiff_zip.ps1", "copy_exif_to_TIFF_ps5.ps1",
                                    "copy_exif_to_TIFF_ps7.ps1", "generate_thumbnails.ps1"])
def test_overlay_reader_is_as_strict_as_the_wizard(tmp_path, shell, script):
    import convert_tiff
    work = tmp_path / "ov"
    work.mkdir()
    cases = {**{f"bad_{i:02d}": b for i, b in enumerate(BAD_FILES.values())},
             **{f"good_{i:02d}": b for i, b in enumerate(GOOD_FILES.values())}}
    for name, raw in cases.items():
        (work / f"{name}.json").write_bytes(raw)
        # the wizard's verdict is the reference
        try:
            sec = convert_tiff._settings_overlay_section(work / f"{name}.json", "s")
            py = "read" if sec and "X" in sec else "refused"
        except (ValueError, OSError):
            py = "refused"
        assert py == ("refused" if name.startswith("bad") else "read"), (name, py)
    fn = _function((REPO / script).read_text(encoding="ascii"), "Get-SettingsOverlay")
    (tmp_path / "f.ps1").write_text(fn + "\n", encoding="utf-8")
    (tmp_path / "h.ps1").write_text(_HARNESS, encoding="utf-8")
    r = subprocess.run([shell, "-NoProfile", "-File", str(tmp_path / "h.ps1"), str(tmp_path / "f.ps1"),
                        str(work), str(tmp_path / "out.txt")], capture_output=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")
    got = dict(line.split("=") for line in (tmp_path / "out.txt").read_text(encoding="utf-8").split("\n"))
    labels = {**{f"bad_{i:02d}": k for i, k in enumerate(BAD_FILES)},
              **{f"good_{i:02d}": k for i, k in enumerate(GOOD_FILES)}}
    wrong = [f"{labels[n]}: {v}" for n, v in got.items()
             if v != ("refused" if n.startswith("bad") else "read")]
    assert not wrong, wrong


# ---------------------------------------------------------------------------------------------
# #16: a stamped name that is the photo's own name is restored under that name
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shell", SHELLS)
@need_tools[0]
@need_tools[1]
def test_stranded_backup_with_a_date_stamped_name_keeps_its_name(tmp_path, shell):
    root = tmp_path / "root"
    old = root / HOSTILE / "OLD_TIFFs"
    b = color_rgb(old / "Scan_20190815_143000.tif")
    orig = b.read_bytes()
    ref = tifffile.imread(str(b))
    r, out = run(shell, COMPRESS, ["-InputDir", root, "-Mode", 9, "-RestoreStranded"], tmp_path)
    restored = [ln for ln in out.splitlines() if "RESTORED (stranded original)" in ln]
    assert restored and restored[0].endswith("Scan_20190815_143000.tif"), out[-3000:]
    # restored under its own name -- and then compressed by the same mode 9 run, the
    # original archived again
    assert np.array_equal(tifffile.imread(str(root / HOSTILE / "Scan_20190815_143000.tif")), ref)
    assert (old / "Scan_20190815_143000.tif").read_bytes() == orig
    assert not (root / HOSTILE / "Scan.tif").exists()


@pytest.mark.parametrize("shell", SHELLS)
@need_tools[0]
@need_tools[1]
def test_a_real_collision_copy_still_restores_to_the_base_name(tmp_path, shell):
    """OLD_TIFFs holds photo.tif AND photo_20190815_143000.tif: the stamp is the collision
    rename, so with nothing in the parent folder the base name is the stranded one."""
    root = tmp_path / "root"
    old = root / HOSTILE / "OLD_TIFFs"
    color_rgb(old / "photo.tif")
    color_rgb(old / "photo_20190815_143000.tif")
    r, out = run(shell, COMPRESS, ["-InputDir", root, "-Mode", 9], tmp_path)
    assert "STRANDED" in out
    assert "photo.tif" in out


# ---------------------------------------------------------------------------------------------
# #18: mode 8 without -DeleteSource does not multiply copies
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shell", SHELLS)
@need_tools[0]
@need_tools[1]
def test_mode8_tiff_source_second_run_skips(tmp_path, shell):
    root = tmp_path / "root"
    cam = root / HOSTILE
    color_rgb(cam / "0014_14 (2).tiff")
    for _ in range(3):
        r, out = run(shell, COMPRESS, ["-InputDir", root, "-Mode", 8, "-Workers", 1], tmp_path)
        assert r.returncode == 0, out[-3000:]
    names = sorted(p.name for p in cam.iterdir() if p.is_file())
    assert names == ["0014_14 (2).tif", "0014_14 (2).tiff"], names


# ---------------------------------------------------------------------------------------------
# #20: -Remove keeps a TIFF too large to be a thumbnail this script wrote
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shell", SHELLS)
@need_tools[0]
@need_tools[1]
def test_remove_keeps_a_photo_named_like_a_thumbnail(tmp_path, shell):
    d = tmp_path / HOSTILE
    big = color_rgb(d / "hitchhiker_thumb.tif", h=800, w=1200)
    small = color_rgb(d / "photo_thumb.tif", h=100, w=150)
    orig = big.read_bytes()
    r, out = run(shell, THUMBS, ["-InputDir", d, "-Remove"], tmp_path)
    assert big.read_bytes() == orig, out[-2000:]
    assert not small.exists(), out[-2000:]
    assert "not a thumbnail this script wrote" in out
