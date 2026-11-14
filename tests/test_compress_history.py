"""compress_tiff_zip.py appends an xmpMM:History entry to every TIFF it rewrites.

Action=converted, When, SoftwareAgent (tool, version, link), Parameters -- the format of
convert_tiff.py's padded conversion and of jxl-photo. Since v3.1.1 it is written in the SAME
exiftool command as the tag copy, with `=` after it (`+=` there doubles every copied entry,
jxl-photo #531): one rewrite of the file, BEFORE the page-count / integrity / pixel gates. Real
runs in both lanes (sequential and parallel), a copy mode and in place, in a hostile folder; one
file name has a '%'.
"""

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import tifffile

from tiff_workflow import wizard as convert_tiff
from test_exiftool_stdin import exif_stdin

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tiff_workflow" / "compress_tiff_zip.py"
from backends import LANES, backend_cmd, run_backend  # noqa: E402
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
pytestmark = [
    pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required"),
]
HOSTILE = "写真 [FINAL] x"
AGENT = f"{convert_tiff.TOOLKIT_NAME} {convert_tiff.TOOLKIT_VERSION} ({convert_tiff.TOOLKIT_URL})"

# The ways the per-file work can run: (id, lane, extra args, in place?)
PATHS = [
    ("mode-sequential", "sequential", [], False),
    ("inplace-sequential", "sequential", [], True),
    ("mode-parallel", "parallel", ["--workers", "3"], False),
    ("inplace-parallel", "parallel", ["--workers", "3"], True),
]
PATH_IDS = [p[0] for p in PATHS]


def make_tiff_named(directory: Path, name: str, seed=1, suffix=".tif") -> Path:
    """A TIFF whose FINAL name may contain '%': built under a safe name, then renamed
    (magick would write `500ff.tif` for `50%off.tif`)."""
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"_fixture_{seed}{suffix}"
    a = np.random.default_rng(seed).integers(0, 255, size=(48, 64, 3), dtype=np.uint8)
    tifffile.imwrite(str(tmp), a, photometric="rgb")
    final = directory / f"{name}{suffix}"
    tmp.rename(final)
    return final


def history(path):
    """The xmpMM:History entries of `path`, raw values (-n keeps When ISO-formatted)."""
    out = exif_stdin(path, "-j", "-struct", "-n", "-XMP-xmpMM:History")
    data = json.loads(out)[0] if out else {}
    h = data.get("History", [])
    return [h] if isinstance(h, dict) else list(h)


def iso_when(text):
    """exiftool prints an XMP date as `2026:11:10 22:04:16+09:00` even with -n; the file
    holds the ISO form. Turn the date part back into ISO and let fromisoformat judge."""
    m = re.fullmatch(r"(\d{4}):(\d\d):(\d\d)[ T](.+)", text)
    return datetime.fromisoformat(f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}" if m else text)


def md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def run(lane, args, cwd, *more):
    cmd = backend_cmd(lane, SCRIPT, [*[str(a) for a in args], *more])
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=900)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def convert(lane, extra, in_place, src):
    """Run one worker path: mode 1 writes ZIP/<name>; mode 2 without -OutputDir has the
    input root as its flat output, so it writes over each file that is directly in that
    folder (a temp sibling, the gates, then the rename). `src` has no subfolders."""
    return run(lane, ["--mode", 2 if in_place else 1, "--input-dir", src, *extra], src.parent)


def output_of(src, name, in_place):
    return (src / f"{name}.tif") if in_place else (src / "ZIP" / f"{name}.tif")


@pytest.mark.parametrize("pid,lane,extra,in_place", PATHS, ids=PATH_IDS)
def test_h1_every_worker_path_appends_one_entry(tmp_path, pid, lane, extra, in_place):
    """H1/H6: sources have no entry -> exactly one, in the format of the padded conversion."""
    src = tmp_path / HOSTILE / "S5pro 01"
    names = ["DSCF 50%off (2)", "DSCF0002 (3)", "DSCF0003 (4)"]
    files = [make_tiff_named(src, n, seed=i + 1) for i, n in enumerate(names)]
    before = {f.name: md5(f) for f in files}
    rc, out = convert(lane, extra, in_place, src)
    assert rc == 0, out[-3000:]
    assert "history not written" not in out, out[-2000:]
    for n in names:
        dst = output_of(src, n, in_place)
        entries = history(dst)
        assert len(entries) == 1, (n, entries, out[-1500:])
        e = entries[0]
        assert e["Action"] == "converted"
        assert e["SoftwareAgent"] == AGENT
        assert e["Parameters"] == f"compress_tiff_zip mode {2 if in_place else 1}: ZIP (Deflate)"
        when = iso_when(e["When"])
        assert when.utcoffset() is not None, e["When"]
    if in_place:
        assert all(md5(f) != before[f.name] for f in files), "the files were not replaced in place"
        assert sorted(p.name for p in src.iterdir()) == sorted(f.name for f in files)
    else:
        # mode 1 leaves the source alone
        assert {f.name: md5(f) for f in files} == before


@pytest.mark.parametrize("pid,lane,extra,in_place", PATHS, ids=PATH_IDS)
def test_h2_earlier_entries_are_kept_not_doubled(tmp_path, pid, lane, extra, in_place):
    """H2 (jxl-photo #531): a source with one entry ends with exactly two."""
    src = tmp_path / HOSTILE / "S5pro 01"
    f = make_tiff_named(src, "DSCF 50%off (2)")
    exif_stdin(f, "-q", "-overwrite_original",
               "-XMP-xmpMM:History+={Action=saved,SoftwareAgent=Capture One 16}")
    assert len(history(f)) == 1
    rc, out = convert(lane, extra, in_place, src)
    assert rc == 0, out[-3000:]
    entries = history(output_of(src, "DSCF 50%off (2)", in_place))
    assert len(entries) == 2, entries
    assert entries[0]["Action"] == "saved" and entries[0]["SoftwareAgent"] == "Capture One 16"
    assert entries[1]["Action"] == "converted" and entries[1]["SoftwareAgent"] == AGENT


@pytest.mark.parametrize("lane", LANES)
def test_h3_mode0_second_run_adds_nothing(tmp_path, lane):
    """H3: mode 0 compresses in place and keeps the original in OLD_TIFFs/. Run 2 skips the
    ZIP file: no new entry, same bytes."""
    src = tmp_path / HOSTILE / "S5pro 01"
    f = make_tiff_named(src, "DSCF 50%off (2)")
    rc, out = run(lane, ["--mode", 0, "--input-dir", src], tmp_path)
    assert rc == 0, out[-3000:]
    assert len(history(f)) == 1
    h1 = md5(f)
    rc, out = run(lane, ["--mode", 0, "--input-dir", src], tmp_path)
    assert rc == 0, out[-3000:]
    assert len(history(f)) == 1, "run 2 appended another entry"
    assert md5(f) == h1, "run 2 rewrote the file"


@pytest.mark.parametrize("lane", LANES)
def test_h4_mode8_delete_source_and_thumbnail(tmp_path, lane):
    """H4: mode 8 -DeleteSource leaves the entry on the file in place; mode 1
    -GenerateThumbnail says so in Parameters and keeps the page markers."""
    src = tmp_path / HOSTILE / "mode8"
    f = make_tiff_named(src, "DSCF 50%off (2)")
    rc, out = run(lane, ["--mode", 8, "--input-dir", src, "--delete-source"], tmp_path)
    assert rc == 0, out[-3000:]
    entries = history(f)
    assert len(entries) == 1 and entries[0]["Parameters"] == "compress_tiff_zip mode 8: ZIP (Deflate)"

    src = tmp_path / HOSTILE / "thumb"
    make_tiff_named(src, "DSCF 50%off (2)")
    rc, out = run(lane, ["--mode", 1, "--input-dir", src, "--generate-thumbnail"], tmp_path)
    assert rc == 0, out[-3000:]
    dst = src / "ZIP" / "DSCF 50%off (2).tif"
    entries = history(dst)
    assert len(entries) == 1, entries
    assert entries[0]["Parameters"].endswith(" + thumbnail page"), entries[0]
    with tifffile.TiffFile(str(dst)) as t:
        assert len(t.pages) == 2, "History must not cost the thumbnail page"
    assert exif_stdin(dst, "-a", "-G1", "-s", "-s", "-n", "-SubfileType").count("SubfileType: 1") >= 1


@pytest.mark.parametrize("lane", LANES)
def test_h5_the_purge_gate_still_accepts_the_pair(tmp_path, lane):
    """H5: Delete OLD_TIFFs compares the backup with the new file; the History entry (XMP,
    not pixels, not SubfileType/EXIF presence) must not make the gate refuse."""
    src = tmp_path / HOSTILE / "S5pro 01"
    f = make_tiff_named(src, "DSCF 50%off (2)")
    rc, out = run(lane, ["--mode", 0, "--input-dir", src], tmp_path)
    assert rc == 0, out[-3000:]
    old = src / "OLD_TIFFs" / f.name
    assert old.exists()
    match, detail = convert_tiff._compare_tiff_metadata(old, f)
    assert match is True, detail


# --- H6 static -----------------------------------------------------------------------------

