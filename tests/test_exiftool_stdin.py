"""Audit round 27: exiftool never gets an argfile PATH on its command line.

exiftool reads its command line in the ANSI code page. Every exiftool call of this repo used
to pass `-@ <argfile>` with the argfile in %TEMP%; a %TEMP% with characters the code page
lacks (a Windows user name in Japanese, Greek, Polish...) gave
`Error opening arg file C:\\...\\?? O? ae [x] 100%\\probe.args` on EVERY call. The argfile's
CONTENT now goes on stdin (`exiftool -@ -`). Same bug as jxl-photo #553.

Helper rule (AGENTS.md): a test that reads tags must not put a Japanese path on exiftool's
command line either -- `exif_stdin` below sends the path on stdin.
"""

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
import tifffile

REPO = Path(__file__).resolve().parent.parent
COMPRESS = REPO / "compress_tiff_zip.ps1"
COPY_EXIF = {"powershell": REPO / "copy_exif_to_TIFF_ps5.ps1",
             "pwsh": REPO / "copy_exif_to_TIFF_ps7.ps1"}
SHELLS = [s for s in ("powershell", "pwsh") if shutil.which(s)]
TOOLS = (shutil.which("magick") is not None and shutil.which("exiftool") is not None)

# No single Windows code page holds all of these.
_NON_ANSI = "一時 Ωμ ąę [x] 100%"
HOSTILE = "写真 [FINAL] x"


def _acp_is_utf8():
    if sys.platform != "win32":
        return True
    return ctypes.windll.kernel32.GetACP() == 65001


needs_ansi_cp = pytest.mark.skipif(
    sys.platform != "win32" or _acp_is_utf8(),
    reason="needs a non-UTF-8 ANSI code page (Windows): otherwise the bug cannot appear")
needs_shell = pytest.mark.skipif(not SHELLS, reason="PowerShell required")
needs_tools = pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required")


def exif_stdin(path, *args) -> str:
    """exiftool on `path` with ALL arguments on stdin (`-@ -`, UTF-8): a Japanese path on the
    command line would arrive mangled in the ANSI code page -- the very bug under test."""
    data = ("\n".join(["-charset", "filename=utf8", *[str(a) for a in args], str(path)])
            + "\n").encode("utf-8")
    r = subprocess.run(["exiftool", "-@", "-"], input=data, capture_output=True, timeout=120)
    return r.stdout.decode("utf-8", "replace").strip()


def compression(path) -> str:
    return exif_stdin(path, "-s3", "-IFD0:Compression")


def tiff(path: Path, seed=1, h=48, w=64) -> np.ndarray:
    path.parent.mkdir(parents=True, exist_ok=True)
    a = np.random.default_rng(seed).integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    tifffile.imwrite(str(path), a, photometric="rgb")
    return a


def _hostile_env(tmp_path: Path):
    temp = tmp_path / _NON_ANSI / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp))
    env.pop("TIFFWORKFLOW_SETTINGS", None)
    return env


def run_ps(shell, script, args, cwd, env):
    cmd = [shell, "-NoProfile", "-File", str(script), *[str(a) for a in args]]
    r = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, timeout=900)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, out


def make_jpeg(path: Path):
    """A JPEG that carries Make/Model of the Fuji S5 Pro, built under a safe name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = Path(tempfile.mkdtemp(prefix="tw_jpg_")) / "safe.jpg"
    try:
        subprocess.run(["magick", "-size", "64x48", "gradient:", str(safe)],
                       check=True, capture_output=True, timeout=120)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-Make=FUJIFILM",
                        "-Model=FinePixS5Pro", str(safe)],
                       check=True, capture_output=True, timeout=120)
        shutil.copy(safe, path)
    finally:
        shutil.rmtree(safe.parent, ignore_errors=True)


# --- T1-1 static ------------------------------------------------------------------------

_ENC = r"\$OutputEncoding = \[System\.Text\.UTF8Encoding\]::new\(\$false\)"
_SCRIPTS_WITH_EXIFTOOL = ["compress_tiff_zip.ps1", "copy_exif_to_TIFF_ps5.ps1",
                          "copy_exif_to_TIFF_ps7.ps1"]


def test_no_ps1_passes_an_argfile_path_to_exiftool():
    bad = []
    for ps1 in sorted(REPO.glob("*.ps1")):
        for n, line in enumerate(ps1.read_text(encoding="ascii").splitlines(), 1):
            if re.search(r"exiftool -@ (?!-)", line):
                bad.append(f"{ps1.name}:{n}: {line.strip()}")
    assert not bad, "exiftool -@ <path> left (use `ReadAllText($f) | exiftool -@ -`):\n" + "\n".join(bad)


def test_generate_thumbnails_never_calls_exiftool():
    """It needs no $OutputEncoding line: pinned so that a future exiftool call there is noticed."""
    src = (REPO / "generate_thumbnails.ps1").read_text(encoding="ascii")
    code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code if re.search(r"\bexiftool\b", l)]


@pytest.mark.parametrize("name", _SCRIPTS_WITH_EXIFTOOL)
def test_output_encoding_is_set_at_script_scope_and_in_every_parallel_block(name):
    src = (REPO / name).read_text(encoding="ascii")
    assert re.search(r"^" + _ENC + r"$", src, re.M), f"{name}: no script-scope $OutputEncoding"
    lines = src.splitlines()
    missing = []
    for i, line in enumerate(lines):
        if re.search(r"ForEach-Object\b.*-Parallel \{\s*$", line):
            # first non-blank statement of the block
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if not re.fullmatch(r"\s*" + _ENC, lines[j]):
                missing.append(f"{name}:{i + 1}")
    assert not missing, ("-Parallel block without $OutputEncoding as its first statement: "
                         + ", ".join(missing))


@pytest.mark.parametrize("rel", ["convert_tiff.py", "tools/real_photo_battery.py"])
def test_python_never_passes_an_argfile_path(rel):
    src = (REPO / rel).read_text(encoding="utf-8")
    args = re.findall(r'"-@"\s*,\s*([^\],)]+)', src)
    assert args, f"{rel}: expected at least one exiftool -@ call"
    assert all(a.strip() == '"-"' for a in args), (rel, args)


# --- T1-2 real: %TEMP% outside the ANSI code page ------------------------------------------

def _fixture(tmp_path, names=("DSCF0001 (2)",)):
    src = tmp_path / HOSTILE / "S5pro 01"
    arrays = {}
    for i, n in enumerate(names):
        arrays[n] = tiff(src / f"{n}.tif", seed=i + 1)
    return src, arrays


@needs_ansi_cp
@needs_shell
@needs_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestCompressUnderNonAnsiTemp:
    def test_mode1_writes_a_deflate_copy(self, tmp_path, shell):
        src, _ = _fixture(tmp_path)
        rc, out = run_ps(shell, COMPRESS, ["-Mode", 1, "-InputDir", src], tmp_path,
                         _hostile_env(tmp_path))
        assert rc == 0, out[-3000:]
        assert "exiftool check" not in out, out[-2000:]
        assert compression(src / "ZIP" / "DSCF0001 (2).tif") == "Adobe Deflate"

    def test_mode8_delete_source_lands_in_place(self, tmp_path, shell):
        src, arrays = _fixture(tmp_path)
        rc, out = run_ps(shell, COMPRESS, ["-Mode", 8, "-InputDir", src, "-DeleteSource"],
                         tmp_path, _hostile_env(tmp_path))
        assert rc == 0, out[-3000:]
        f = src / "DSCF0001 (2).tif"
        assert compression(f) == "Adobe Deflate"
        assert np.array_equal(tifffile.imread(str(f)), arrays["DSCF0001 (2)"])

    def test_mode0_keeps_the_original_in_old_tiffs(self, tmp_path, shell):
        import hashlib
        src, _ = _fixture(tmp_path)
        before = hashlib.md5((src / "DSCF0001 (2).tif").read_bytes()).hexdigest()
        rc, out = run_ps(shell, COMPRESS, ["-Mode", 0, "-InputDir", src], tmp_path,
                         _hostile_env(tmp_path))
        assert rc == 0, out[-3000:]
        kept = src / "OLD_TIFFs" / "DSCF0001 (2).tif"
        assert hashlib.md5(kept.read_bytes()).hexdigest() == before
        assert compression(src / "DSCF0001 (2).tif") == "Adobe Deflate"

    @pytest.mark.skipif("pwsh" not in SHELLS, reason="pwsh required")
    def test_mode1_parallel_block(self, tmp_path, shell):
        if shell != "pwsh":
            pytest.skip("-ForceParallel needs PowerShell 7")
        names = ("DSCF0001 (2)", "DSCF0002 (3)", "DSCF0003 (4)")
        src, _ = _fixture(tmp_path, names)
        rc, out = run_ps(shell, COMPRESS, ["-Mode", 1, "-InputDir", src, "-ForceParallel",
                                           "-Workers", 3], tmp_path, _hostile_env(tmp_path))
        assert rc == 0, out[-3000:]
        for n in names:
            assert compression(src / "ZIP" / f"{n}.tif") == "Adobe Deflate", out[-2000:]


@needs_ansi_cp
@needs_shell
@needs_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestCopyExifUnderNonAnsiTemp:
    def _pair(self, src, name, seed):
        tiff(src / f"{name}.tif", seed=seed)
        make_jpeg(src / f"{name}.jpg")

    def test_compress_zip_copies_exif_and_compresses(self, tmp_path, shell):
        src = tmp_path / HOSTILE / "S5pro 01"
        self._pair(src, "DSCF0001 (2)", 1)
        rc, out = run_ps(shell, COPY_EXIF[shell], ["-InputDir", src, "-CompressZip"],
                         tmp_path, _hostile_env(tmp_path))
        assert rc == 0, out[-3000:]
        f = src / "DSCF0001 (2).tif"
        assert exif_stdin(f, "-s3", "-Make") == "FUJIFILM", out[-2000:]
        assert compression(f) == "Adobe Deflate"

    def test_compress_zip_parallel_block(self, tmp_path, shell):
        if shell != "pwsh":
            pytest.skip("the parallel block exists only in the PowerShell 7 copy")
        names = ("DSCF0001 (2)", "DSCF0002 (3)", "DSCF0003 (4)")
        src = tmp_path / HOSTILE / "S5pro 01"
        for i, n in enumerate(names):
            self._pair(src, n, i + 1)
        rc, out = run_ps(shell, COPY_EXIF[shell],
                         ["-InputDir", src, "-CompressZip", "-Workers", 3],
                         tmp_path, _hostile_env(tmp_path))
        assert rc == 0, out[-3000:]
        for n in names:
            f = src / f"{n}.tif"
            assert exif_stdin(f, "-s3", "-Make") == "FUJIFILM", out[-2000:]
            assert compression(f) == "Adobe Deflate"


# --- T1-3 Python: tempfile.tempdir outside the ANSI code page -------------------------------

@needs_ansi_cp
@needs_tools
def test_python_exiftool_calls_survive_a_non_ansi_tempdir(tmp_path, monkeypatch):
    import convert_tiff
    src = tmp_path / HOSTILE / "a.tif"
    tiff(src, h=48, w=64)
    reference = convert_tiff._tiff_metadata_signature(src)
    assert reference is not None
    non_ansi = tmp_path / _NON_ANSI
    non_ansi.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(non_ansi))
    sig = convert_tiff._tiff_metadata_signature(src)
    assert sig is not None, "the purge gate could not read the tags under a non-ANSI TEMP"
    assert sig["icc"] == reference["icc"]
    px = convert_tiff._image_pixels([src])
    assert list(px.values()) == [64 * 48], px
