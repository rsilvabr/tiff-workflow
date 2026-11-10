"""Audit round 25: originals stranded in OLD_TIFFs/.

Modes 0/9 move every original of a folder to OLD_TIFFs/ before writing its compressed
copy. On 2026-08-03 a mode 9 run died mid-group without running the round 16 trap (window
closed / process killed), leaving 365 originals in OLD_TIFFs/ with NOTHING in their place;
the next run walked past them (discovery excludes OLD_TIFFs/), and on 2026-10-17 a backup
script moved them off the disk. These tests drive the real backend under both shells in a
hostile folder (Japanese, [ ], a space, %).
"""

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

REPO = Path(__file__).resolve().parent.parent
SHELLS = [s for s in ("powershell", "pwsh") if shutil.which(s)]
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
pytestmark = [
    pytest.mark.skipif(not SHELLS, reason="PowerShell required"),
    pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required"),
]
HOSTILE = "伊豆沼_夏 [FINAL] 5% off"


def tiff(path: Path, compressed=False, seed=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    a = np.random.default_rng(seed).integers(0, 255, size=(32, 48, 3), dtype=np.uint8)
    tifffile.imwrite(str(path), a, photometric="rgb",
                     compression="zlib" if compressed else None)
    return path


def compression(path: Path) -> int:
    with tifffile.TiffFile(str(path)) as t:
        return int(t.pages[0].compression)


def run(shell, root: Path, *args, cwd: Path):
    cmd = [shell, "-NoProfile", "-File", str(REPO / "compress_tiff_zip.ps1"),
           "-InputDir", str(root), "-ForceSequential", *[str(a) for a in args]]
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    return r, r.stdout + r.stderr


def stranded_lines(out: str):
    return [ln for ln in out.splitlines() if "STRANDED (original in OLD_TIFFs" in ln]


@pytest.fixture
def tree(tmp_path):
    """root/<hostile>/01_D810 with:
    a.tif  -- STRANDED: only OLD_TIFFs/a.tif exists
    b.tif  -- healthy: compressed b.tif in place + its backup
    c      -- two backups claim c.tif (c.tif + a timestamped namesake), none in place
    d      -- timestamped backup whose base d.tif is in place: healthy (round 28: the stamp is
              a collision rename only next to OLD_TIFFs/d.tif, the backup it collided with)
    e      -- .tiff backup whose output was written as e.tif: healthy
    OLD_TIFFs/OLD_TIFFs/z.tif -- a backup of a backup: never reported
    """
    root = tmp_path / "root"
    cam = root / HOSTILE / "01_D810"
    old = cam / "OLD_TIFFs"
    tiff(old / "a.tif", seed=1)
    tiff(cam / "b.tif", compressed=True, seed=2)
    tiff(old / "b.tif", seed=2)
    tiff(old / "c.tif", seed=3)
    tiff(old / "c_20260803_081300.tif", seed=4)
    tiff(cam / "d.tif", compressed=True, seed=5)
    tiff(old / "d.tif", seed=5)
    tiff(old / "d_20260803_081300.tif", seed=5)
    tiff(cam / "e.tif", compressed=True, seed=6)
    tiff(old / "e.tiff", seed=6)
    tiff(old / "OLD_TIFFs" / "z.tif", seed=7)
    return root, cam, old


@pytest.mark.parametrize("shell", SHELLS)
def test_stranded_originals_are_reported_in_every_mode(tree, tmp_path, shell):
    root, cam, old = tree
    before = {p: p.read_bytes() for p in old.rglob("*.tif*")}
    for mode in (8, 9):
        r, out = run(shell, root, "-Mode", mode, "-DryRun", cwd=tmp_path)
        assert r.returncode == 0, out[-3000:]
        names = sorted(Path(ln.split("| ", 1)[1].strip()).name for ln in stranded_lines(out))
        assert names == ["a.tif", "c.tif", "c_20260803_081300.tif"], out[-3000:]
        assert "Run again with -RestoreStranded" in out
        assert "still in OLD_TIFFs with nothing in their place" in out   # the end summary
    assert {p: p.read_bytes() for p in old.rglob("*.tif*")} == before
    assert not (cam / "a.tif").exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_restore_puts_them_back_and_this_run_compresses_them(tree, tmp_path, shell):
    root, cam, old = tree
    original_a = (old / "a.tif").read_bytes()
    r, out = run(shell, root, "-Mode", 8, "-RestoreStranded",
                 "-StagingDir", tmp_path / "staging", cwd=tmp_path)
    assert r.returncode == 0, out[-3000:]
    # a.tif: back in place, compressed by this very run (mode 8: verified, in place)
    assert (cam / "a.tif").exists() and not (old / "a.tif").exists()
    assert compression(cam / "a.tif") == 8, out[-3000:]
    a_pixels = tifffile.imread(str(cam / "a.tif"))
    with tifffile.TiffFile(__import__("io").BytesIO(original_a)) as t:
        assert np.array_equal(a_pixels, t.asarray())
    # c: two backups claim c.tif -- neither may be moved
    assert (old / "c.tif").exists() and (old / "c_20260803_081300.tif").exists()
    assert not (cam / "c.tif").exists()
    assert "more than one backup claims" in out
    # the healthy backups and the nested one are untouched
    for n in ("b.tif", "d.tif", "d_20260803_081300.tif", "e.tiff"):
        assert (old / n).exists()
    assert (old / "OLD_TIFFs" / "z.tif").exists()
    assert "1 restored" in out


@pytest.mark.parametrize("shell", SHELLS)
def test_restore_in_a_dry_run_moves_nothing(tree, tmp_path, shell):
    root, cam, old = tree
    r, out = run(shell, root, "-Mode", 9, "-RestoreStranded", "-DryRun", cwd=tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert "DRY: would restore to" in out
    assert (old / "a.tif").exists() and not (cam / "a.tif").exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_long_paths_are_seen_and_restored(tmp_path, shell):
    """Round 26: past 260 characters Windows PowerShell 5.1's Get-ChildItem/Test-Path lost
    the trail -- a backup whose original WAS in place could read as stranded, and a real
    one could be missed. The detection now goes through \\\\?\\ in both shells."""
    root = tmp_path / "root"
    deep = root / HOSTILE
    while len(str(deep)) < 250:
        deep = deep / "長いフォルダ名 [x] 50%"
    import os
    def lp(p):
        return "\\\\?\\" + str(p)
    os.makedirs(lp(deep / "OLD_TIFFs"), exist_ok=True)
    def put(p, seed):
        a = np.random.default_rng(seed).integers(0, 255, size=(16, 16, 3), dtype=np.uint8)
        buf = __import__("io").BytesIO()
        tifffile.imwrite(buf, a, photometric="rgb", compression="zlib")
        with open(lp(p), "wb") as f:
            f.write(buf.getvalue())
    put(deep / "OLD_TIFFs" / "healthy.tif", 1)
    put(deep / "healthy.tif", 1)                       # its original place is NOT empty
    put(deep / "OLD_TIFFs" / "lost.tif", 2)            # nothing in its place
    assert len(str(deep / "OLD_TIFFs" / "healthy.tif")) > 260
    r, out = run(shell, root, "-Mode", 8, "-DryRun", cwd=tmp_path)
    names = sorted(Path(ln.split("| ", 1)[1].strip()).name for ln in stranded_lines(out))
    assert names == ["lost.tif"], out[-3000:]
    r, out = run(shell, root, "-Mode", 8, "-DryRun", "-RestoreStranded", cwd=tmp_path)
    assert "DRY: would restore" in out
    r, out = run(shell, root, "-Mode", 9, "-RestoreStranded", cwd=tmp_path)
    assert os.path.exists(lp(deep / "lost.tif")), out[-3000:]
    assert not os.path.exists(lp(deep / "OLD_TIFFs" / "lost.tif"))
    assert "1 restored" in out


@pytest.mark.parametrize("shell", SHELLS)
def test_excluded_folders_and_mode0_depth(tree, tmp_path, shell):
    root, cam, old = tree
    # an excluded folder's backups are not looked at
    r, out = run(shell, root, "-Mode", 9, "-DryRun", "-ExcludeFolders", "01_D810",
                 cwd=tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert stranded_lines(out) == []
    # mode 0 is not recursive: only <root>\OLD_TIFFs is its business
    r, out = run(shell, root, "-Mode", 0, "-DryRun", cwd=tmp_path)
    assert stranded_lines(out) == []
    r, out = run(shell, cam, "-Mode", 0, "-DryRun", cwd=tmp_path)
    assert len(stranded_lines(out)) == 3, out[-3000:]
