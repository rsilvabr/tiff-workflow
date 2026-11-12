"""Audit round 26: modes 0/9 archive the original in OLD_TIFFs/ only AFTER its compressed
copy has landed -- the irreversible step comes last (jxl-photo's rule).

Until round 25 the planning loop moved EVERY original of the run to OLD_TIFFs/ before a
single file was compressed, so for the whole run the original places were empty, and a run
killed by anything but Ctrl+C stranded them all (365 on 2026-08-03). These tests drive the
real backend in both lanes (parallel and sequential: tests/backends.py) in a hostile folder
(Japanese, [ ], a space, %).
"""

import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import tifffile

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "compress_tiff_zip.py"
from backends import LANES, backend_cmd, run_backend  # noqa: E402
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
pytestmark = [
    pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required"),
]
HOSTILE = "伊豆沼_夏 [FINAL] 5% off"


def tiff(path: Path, h=48, w=64, seed=1, ext_bits=8):
    path.parent.mkdir(parents=True, exist_ok=True)
    dt = np.uint16 if ext_bits == 16 else np.uint8
    a = np.random.default_rng(seed).integers(0, np.iinfo(dt).max, size=(h, w, 3), dtype=dt)
    tifffile.imwrite(str(path), a, photometric="rgb")
    return path


def compression(path: Path) -> int:
    with tifffile.TiffFile(str(path)) as t:
        return int(t.pages[0].compression)


def pixels(path: Path):
    return tifffile.imread(str(path))


def cmd(lane, root, *args):
    return backend_cmd(lane, SCRIPT, ["--input-dir", str(root),
            *[str(a) for a in args]])


def run(lane, root, *args, cwd):
    r = subprocess.run(cmd(lane, root, *args), cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=900)
    return r, r.stdout + r.stderr


def leftovers(folder: Path):
    import re
    pat = re.compile(r"\.[0-9a-f]{8}_[0-9a-f]{12}\.tiff?$", re.I)
    return [p for p in folder.rglob("*") if pat.search(p.name)]


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("staging", [False, True], ids=["in-place", "staging"])
def test_mode9_lands_copies_and_keeps_originals(tmp_path, lane, staging):
    cam = tmp_path / "root" / HOSTILE / "01_D810"
    srcs = {n: tiff(cam / n, seed=i) for i, n in enumerate(["a.tif", "b (2).tif"])}
    srcs["c.tiff"] = tiff(cam / "c.tiff", seed=9)          # a .tiff source -> c.tif
    orig = {n: p.read_bytes() for n, p in srcs.items()}
    args = ["--mode", 9]
    if staging:
        args += ["--staging-dir", tmp_path / "staging"]
    r, out = run(lane, tmp_path / "root", *args, cwd=tmp_path)
    assert r.returncode == 0, out[-3000:]
    old = cam / "OLD_TIFFs"
    for n in ("a.tif", "b (2).tif"):
        assert compression(cam / n) == 8, out[-2000:]
        assert (old / n).read_bytes() == orig[n]              # the original, untouched
        assert np.array_equal(pixels(cam / n), pixels(old / n))
    assert compression(cam / "c.tif") == 8
    assert (old / "c.tiff").read_bytes() == orig["c.tiff"]
    assert not (cam / "c.tiff").exists()
    assert "archived in OLD_TIFFs after their compressed copy landed" in out
    assert leftovers(tmp_path) == [], leftovers(tmp_path)
    # a second run over its own output: nothing to do, no new backup
    r, out = run(lane, tmp_path / "root", *args, cwd=tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert sorted(p.name for p in old.iterdir()) == ["a.tif", "b (2).tif", "c.tiff"]
    assert "STRANDED" not in out


@pytest.mark.parametrize("lane", LANES)
def test_a_killed_mode9_run_never_leaves_a_place_empty(tmp_path, lane):
    """Kill the backend (taskkill /T /F, like closing the window) after the first file is
    done. At no moment of the run, and not after the kill, may an original's place be
    empty. Against the round 25 file every place empties in the first second."""
    root = tmp_path / "root"
    cam = root / HOSTILE / "01_D810"
    names = [f"DSC_{i:04d} ({i}).tif" for i in range(6)]
    for i, n in enumerate(names):
        tiff(cam / n, h=1500, w=2000, seed=i, ext_bits=16)     # ~18 MB of noise each
    orig = {n: (cam / n).read_bytes() for n in names}
    args = ["--mode", 9, "--workers", 2]      # two at once in the parallel lane, one in the other
    p = subprocess.Popen(cmd(lane, root, *args), cwd=str(tmp_path),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    empty_streak = {n: 0 for n in names}
    violations = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            for n in names:
                if (cam / n).exists():
                    empty_streak[n] = 0
                else:
                    empty_streak[n] += 1
                    if empty_streak[n] >= 3:           # >= ~150 ms: not the rename instant
                        violations.append(n)
            time.sleep(0.05)

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    seen_ok = False
    lines = []
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace")
        lines.append(line)
        if "OK (" in line or "archived in OLD_TIFFs" in line:
            seen_ok = True
            break
        if violations:
            break
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    p.wait(timeout=60)
    time.sleep(0.3)
    stop.set()
    w.join(timeout=5)
    assert not violations, (
        f"original places were empty during the run: {sorted(set(violations))}\n"
        + "".join(lines[-20:]))
    assert seen_ok, "".join(lines[-40:])
    old = cam / "OLD_TIFFs"
    for n in names:
        assert (cam / n).exists(), f"{n}: nothing in its place after the kill"
        if (old / n).exists():
            assert (old / n).read_bytes() == orig[n]
            assert compression(cam / n) == 8
        else:
            assert (cam / n).read_bytes() == orig[n]
    # the next run finishes the job and finds nothing stranded
    r, out = run(lane, root, "--mode", 9, cwd=tmp_path)
    assert r.returncode == 0, out[-3000:]
    assert "STRANDED" not in out
    for n in names:
        assert compression(cam / n) == 8
        assert (old / n).read_bytes() == orig[n]


@pytest.mark.parametrize("lane", LANES)
def test_an_original_edited_during_the_run_is_never_archived(tmp_path, lane):
    """The landing re-checks the original (size + date) right before archiving it: an
    edit saved while the group was compressing must win -- the copy is dropped, the edited
    file stays in place, nothing goes to OLD_TIFFs/."""
    root = tmp_path / "root"
    cam = root / HOSTILE / "01_D810"
    a = tiff(cam / "a.tif", h=200, w=200, seed=1)
    tiff(cam / "b.tif", h=2500, w=3000, seed=2, ext_bits=16)  # keeps the group busy
    p = subprocess.Popen(cmd(lane, root, "--mode", 9), cwd=str(tmp_path),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines = []
    edited = False
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace")
        lines.append(line)
        if not edited and "OK (" in line and "a.tif" in line:
            with open(a, "ab") as f:                      # the owner saves an edit
                f.write(b"\0" * 16)
            edited = True
    p.wait(timeout=600)
    out = "".join(lines)
    assert edited, out[-3000:]
    assert "ERROR (keep original) | a.tif | the original changed" in out, out[-3000:]
    assert p.returncode == 1
    assert compression(a) == 1 and a.read_bytes().endswith(b"\0" * 16)
    assert not (cam / "OLD_TIFFs" / "a.tif").exists()
    assert compression(cam / "b.tif") == 8                    # the other file landed
    assert (cam / "OLD_TIFFs" / "b.tif").exists()
    assert leftovers(tmp_path) == []

