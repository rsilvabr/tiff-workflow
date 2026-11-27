"""Audit round 34 (v3.3.0): the pending fixes of the planner's list (AI_tools, 27/11/2026).

1. mode 8, a file another program holds open: one coherent result -- no `[SOURCE DELETED]`
   promise before anything happened, a file that did not land is not ALSO an OK, and its
   staged file does not stay in the staging folder for ever.
2. a timestamped backup goes with its base (Delete OLD_TIFFs, tools/move_old_backups.py).
3. what a killed run leaves behind is removed by the next run over the same files.
4. associated, non-opaque alpha is a SKIP, not an ERROR on every run.
7. a thumbnail size below 32 in the settings file is refused where the file is read.
8. a '%' in the temp folder no longer refuses a file with '%' in its name.

The real runs use hostile folders and names (Japanese, [ ], '%', spaces), built under a plain
name and renamed; both lanes.
"""

import hashlib
import os
import re
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
from backends import LANES, backend_cmd  # noqa: E402

TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
needs_tools = pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required")
windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="a file held open blocks a rename only on Windows")

HOSTILE = "伊豆沼 [FINAL] 50% x"
COMPRESS = ROOT / "tiff_workflow" / "compress_tiff_zip.py"
COPY_EXIF = ROOT / "tiff_workflow" / "copy_exif_to_tiff.py"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def env_off(**extra):
    e = dict(os.environ, TIFFWORKFLOW_SETTINGS="", TIFFWORKFLOW_NO_GAIN="",
             TIFFWORKFLOW_NO_UPDATE_CHECK="1")
    e.update(extra)
    return e


def run(lane, script, args, cwd, env=None, timeout=900):
    cmd = backend_cmd(lane, script, [str(a) for a in args])
    r = subprocess.run(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       env=env or env_off())
    return r.returncode, r.stdout + r.stderr


def make_tiff(folder: Path, name: str, seed=1, bits=8) -> Path:
    """An uncompressed RGB TIFF named `name` (it may hold '%'): written under a plain name
    and renamed."""
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"_fixture_{seed}.tif"
    dt = np.uint16 if bits == 16 else np.uint8
    a = np.random.default_rng(seed).integers(0, np.iinfo(dt).max, size=(40, 56, 3), dtype=dt)
    tifffile.imwrite(str(tmp), a, photometric="rgb")
    final = folder / name
    tmp.rename(final)
    return final


def exif(path, *args) -> str:
    """exiftool with every argument on stdin (a Japanese path on the command line is mangled)."""
    data = ("\n".join(["-charset", "filename=utf8", *[str(a) for a in args], str(path)]) + "\n")
    r = subprocess.run(["exiftool", "-@", "-"], input=data.encode("utf-8"),
                       capture_output=True, timeout=120)
    return r.stdout.decode("utf-8", "replace").strip()


def compression(path) -> str:
    return exif(path, "-s", "-s", "-s", "-IFD0:Compression")


def done_line(out):
    lines = [ln for ln in out.splitlines() if "Done:" in ln]
    assert lines, out[-3000:]
    return lines[-1]


# ---------------------------------------------------------------------------------------------
# Item 1 -- mode 8, a file held open
# ---------------------------------------------------------------------------------------------

@needs_tools
@windows_only
@pytest.mark.parametrize("lane", LANES)
def test_mode8_a_file_held_open_is_one_error_and_leaves_nothing_staged(tmp_path, lane):
    """The probe of the plan, as a test: two files held open by another handle, mode 8
    --delete-source with a staging folder. v3.2.1 printed `[SOURCE DELETED]` for all three
    (in place nothing is deleted), counted the held .tif OK and error at once, and left its
    staged file in the staging folder for ever."""
    folder = tmp_path / HOSTILE
    held = make_tiff(folder, "0014_14 (2) 50%.tif", seed=1)
    held_tiff = make_tiff(folder, "b 50%.tiff", seed=2)
    make_tiff(folder, "free.tif", seed=3)
    stg = tmp_path / "stg 一時 [x]"
    stg.mkdir()
    before = sha(held)
    args = ["--mode", 8, "--delete-source", "--input-dir", folder, "--staging-dir", stg]
    h1, h2 = open(held, "rb"), open(held_tiff, "rb")
    try:
        rc, out = run(lane, COMPRESS, args, tmp_path)
    finally:
        h1.close()
        h2.close()
    assert rc == 1, out[-3000:]
    assert "SOURCE DELETED" not in out, out[-3000:]
    done = done_line(out)
    assert done.startswith("Done: 2 OK") or " Done: 2 OK" in done, done
    assert "| 2 errors |" in done, done
    assert sha(held) == before, "the held file was changed"
    assert os.listdir(stg) == [], f"staged files stayed: {os.listdir(stg)}"
    assert f"not landed: {held.name} is as it was before the run" in out, out[-3000:]
    assert (f"| {held_tiff.name} | its compressed copy b 50%.tif is in place, both files are "
            f"there") in out, out[-3000:]
    assert held_tiff.exists() and compression(folder / "b 50%.tif") == "Adobe Deflate"

    # the handles are closed: the next run compresses the held file
    rc, out = run(lane, COMPRESS, args, tmp_path)
    assert compression(held) == "Adobe Deflate", out[-3000:]
    assert "SOURCE DELETED" not in out
    assert os.listdir(stg) == []
    # b 50%.tiff is still there and its .tif already exists: reported, never written over
    assert f"SKIP (exists) | {held_tiff.name}" in out, out[-3000:]
    assert rc == 0, out[-3000:]


# ---------------------------------------------------------------------------------------------
# Item 2 -- a timestamped backup goes with its base
# ---------------------------------------------------------------------------------------------

def _purge(monkeypatch, folder, before_delete=None):
    """The wizard's Delete OLD_TIFFs on `folder`, answered yes; `before_delete` runs where the
    questions are (after the check, before the deletion)."""
    from types import SimpleNamespace

    from tiff_workflow import wizard as ct

    def confirm():
        if before_delete:
            before_delete()
        return True
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    monkeypatch.setattr(ct, "step_folder", lambda cfg, prompt: folder)
    monkeypatch.setattr(ct, "_purge_confirm", confirm)
    cfg = SimpleNamespace(config=ct.ToolConfig())
    cfg.config.last_workers = 2
    ct.run_purge_old_tiffs(cfg)


@pytest.fixture
def library(tmp_path):
    """A folder with the photo in place (`p 50%.tif`) and its OLD_TIFFs."""
    folder = tmp_path / HOSTILE
    place = make_tiff(folder, "p 50%.tif", seed=11)
    old = folder / "OLD_TIFFs"
    old.mkdir()
    return folder, place, old


@needs_tools
def test_purge_deletes_the_stamped_backup_and_keeps_an_older_base(library, monkeypatch, capsys):
    """The stamped backup is the original of the file in place; the base an OLDER version of the
    photo. v3.2.1 paired each backup with its own name: the stamped one was an orphan (kept for
    ever, then STRANDED on every run once the base was gone)."""
    folder, place, old = library
    stamped = old / "p 50%_20260805_101500.tif"
    shutil.copy2(place, stamped)
    base = make_tiff(old, "p 50%.tif", seed=12)          # other pixels
    _purge(monkeypatch, folder)
    out = capsys.readouterr().out
    assert not stamped.exists(), out
    assert base.exists(), "the older version is the only copy of it"
    assert "MISMATCH" in out, out
    assert tw.find_stranded_originals(str(folder), True, ("OLD_TIFFs",), []) == []


@needs_tools
def test_purge_deletes_both_when_both_match_and_removes_the_folder(library, monkeypatch, capsys):
    folder, place, old = library
    shutil.copy2(place, old / "p 50%_20260805_101500.tif")
    shutil.copy2(place, old / "p 50%.tif")
    _purge(monkeypatch, folder)
    out = capsys.readouterr().out
    assert not old.exists(), out
    assert place.exists()


@needs_tools
def test_purge_a_stamped_backup_changed_after_the_check_keeps_its_base(library, monkeypatch,
                                                                       capsys):
    """W-O16: the stamped one changed while the questions were open -> kept; its base then has a
    stamped sibling next to it and stays too."""
    folder, place, old = library
    stamped = old / "p 50%_20260805_101500.tif"
    shutil.copy2(place, stamped)
    base = old / "p 50%.tif"
    shutil.copy2(place, base)

    def touch():
        st = stamped.stat()
        os.utime(stamped, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    _purge(monkeypatch, folder, touch)
    out = capsys.readouterr().out
    assert stamped.exists() and base.exists(), out
    assert "KEPT (its timestamped sibling p 50%_20260805_101500.tif is still here)" in out, out
    assert tw.find_stranded_originals(str(folder), True, ("OLD_TIFFs",), []) == []


@needs_tools
def test_purge_a_tiff_backup_of_a_tif_output_is_verified(library, monkeypatch, capsys):
    """A .tiff source is written as .tif: its backup was an orphan, kept for ever."""
    folder, place, old = library
    shutil.copy2(place, old / "p 50%.tiff")
    _purge(monkeypatch, folder)
    out = capsys.readouterr().out
    assert not (old / "p 50%.tiff").exists(), out


@needs_tools
def test_purge_a_lone_stamped_backup_is_checked_against_its_base_name(library, monkeypatch,
                                                                      capsys):
    """The base backup is gone (an earlier version deleted it): the stamped one is compared with
    <base>.tif in place, and the content decides -- an own-name photo with a stamp-shaped name
    and an unrelated photo in place is a MISMATCH, kept."""
    folder, place, old = library
    lone = old / "p 50%_20260805_101500.tif"
    shutil.copy2(place, lone)
    own = make_tiff(old, "IMG_20190815_143000.tif", seed=21)
    make_tiff(folder, "IMG.tif", seed=22)
    _purge(monkeypatch, folder)
    out = capsys.readouterr().out
    assert not lone.exists(), out
    assert own.exists(), out
    assert "MISMATCH" in out


def _load_mob():
    if str(ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(ROOT / "tools"))
    import move_old_backups as mob
    return mob


def _backup_library(tmp_path):
    src = tmp_path / "G"
    cam = src / "2025" / HOSTILE
    old = cam / "OLD_TIFFs"
    old.mkdir(parents=True)
    (old / "d 50%.tif").write_bytes(b"older d")
    (old / "d 50%_20260803_081300.tif").write_bytes(b"orig d")
    (cam / "d 50%.tif").write_bytes(b"zip d")
    return src, cam, old


def test_move_old_backups_keeps_a_base_whose_renamed_sibling_was_skipped(tmp_path, capsys):
    """A renamed sibling that is skipped (a DIFFERENT file of its name already on the backup)
    used to let its base go: the stamped one was then alone, read as a photo of its own name,
    STRANDED on every compress run. Now the base stays until the sibling has moved."""
    mob = _load_mob()
    src, cam, old = _backup_library(tmp_path)
    dest = tmp_path / "H"
    taken = dest / "2025" / HOSTILE / "OLD_TIFFs" / "d 50%_20260803_081300.tif"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"SOMETHING ELSE")
    rc = mob.main(["--source", str(src), "--roots", "2025", "--folder", "OLD_TIFFs",
                   "--dest", str(dest), "--go"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert (old / "d 50%.tif").exists() and (old / "d 50%_20260803_081300.tif").exists(), out
    assert "KEPT (its timestamped sibling d 50%_20260803_081300.tif is still here)" in out, out
    assert tw.find_stranded_originals(str(cam), True, ("OLD_TIFFs",), []) == []
    # fixed by hand: the next run moves both
    taken.unlink()
    rc = mob.main(["--source", str(src), "--roots", "2025", "--folder", "OLD_TIFFs",
                   "--dest", str(dest), "--go"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert not old.exists()
    assert taken.read_bytes() == b"orig d"


def test_move_old_backups_runs_from_a_copy_laid_out_like_local(tmp_path):
    """The owner's _LOCAL keeps the script NEXT to tiff_workflow/ (the repo: in tools/)."""
    local = tmp_path / "LOCAL 一時 [x]"
    local.mkdir()
    shutil.copy2(ROOT / "tools" / "move_old_backups.py", local / "move_old_backups.py")
    shutil.copytree(ROOT / "tiff_workflow", local / "tiff_workflow",
                    ignore=shutil.ignore_patterns("__pycache__"))
    src, cam, old = _backup_library(tmp_path)
    r = subprocess.run([sys.executable, "-B", str(local / "move_old_backups.py"), "--source",
                        str(src), "--roots", "2025", "--folder", "OLD_TIFFs", "--dest",
                        str(tmp_path / "H")], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Dry run: nothing moved" in r.stdout
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy2(ROOT / "tools" / "move_old_backups.py", alone / "move_old_backups.py")
    r = subprocess.run([sys.executable, "-B", str(alone / "move_old_backups.py"), "--folder",
                        "OLD_TIFFs", "--source", str(src), "--roots", "2025"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120)
    assert r.returncode == 2 and "tiff_workflow/common.py not found" in r.stdout, r.stdout


# ---------------------------------------------------------------------------------------------
# Item 3 -- what a killed run leaves behind
# ---------------------------------------------------------------------------------------------

RUN_ID = "aaaa1111"
OTHER_ID = "0123abcd"


def _photo(folder: Path, name="x 50% [1].tif", data=b"PHOTO"):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(data)
    return p


def _shapes(photo: Path, run_id=OTHER_ID):
    """The three leftover shapes of `photo` a killed run leaves next to it."""
    d = photo.parent
    made = [d / f"{photo.name}.{run_id}_0123456789ab.tif",
            d / f"{run_id}_0123456789ab_{photo.name}",
            d / f"{photo.name}_exiftool_tmp"]
    for m in made:
        m.write_bytes(b"LEFTOVER")
    return made


class TestSweepLeftovers:
    """tw.sweep_leftovers removes a name only when all six conditions hold; each failing
    alone keeps it."""

    def test_all_five_hold_and_the_three_shapes_go(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
        photo = _photo(tmp_path / HOSTILE)
        made = _shapes(photo)
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, False)
        assert sorted(swept.removed) == sorted(str(m) for m in made)
        assert not any(m.exists() for m in made) and photo.read_bytes() == b"PHOTO"

    def test_too_recent_is_kept(self, tmp_path):
        photo = _photo(tmp_path / HOSTILE)
        made = _shapes(photo)
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, False)
        assert swept.removed == [] and all(m.exists() for m in made)
        assert {why for _p, why in swept.kept} == {"too recent"}

    def test_this_runs_own_files_are_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
        photo = _photo(tmp_path / HOSTILE)
        made = _shapes(photo, run_id=RUN_ID)
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, False)
        assert [str(m) for m in made[:2]] == sorted(p for p, why in swept.kept
                                                    if why == "this run's own")[::-1] or \
            {p for p, why in swept.kept if why == "this run's own"} == {str(m) for m in made[:2]}
        assert made[0].exists() and made[1].exists()

    def test_a_missing_source_keeps_them(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
        folder = tmp_path / HOSTILE
        photo = _photo(folder)
        made = _shapes(photo)
        target = folder / "x 50% [1].tif"
        swept = tw.sweep_leftovers([(str(target), str(folder / "gone.tiff"))], RUN_ID, False)
        assert swept.removed == [] and all(m.exists() for m in made)
        assert {why for _p, why in swept.kept} == {"its original is missing"}

    @pytest.mark.skipif(sys.platform != "win32", reason="a junction is a Windows link")
    def test_a_link_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
        photo = _photo(tmp_path / HOSTILE)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = photo.parent / f"{OTHER_ID}_0123456789ab_{photo.name}"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)],
                       capture_output=True, check=True)
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, False)
        assert swept.removed == [] and link.exists()
        assert [why for _p, why in swept.kept] == ["not a regular file"]

    def test_a_name_that_only_looks_like_a_leftover_is_never_touched(self, tmp_path, monkeypatch):
        """Shaped like a staged file, but of a file this run does not write; and a photo alone
        named like a staged file."""
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
        folder = tmp_path / HOSTILE
        photo = _photo(folder)
        other = _photo(folder, f"{OTHER_ID}_0123456789ab_other.tif", b"OTHER")
        scan = _photo(folder, "0123abcd_0123456789ab_scan.tif", b"A PHOTO")
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, False)
        assert swept == tw.Swept([], [])
        assert other.read_bytes() == b"OTHER" and scan.read_bytes() == b"A PHOTO"

    def test_a_photo_named_with_digits_next_to_its_target_is_never_touched(self, tmp_path,
                                                                          monkeypatch):
        """Review of v3.3.0 (M1): a date and a time put in front of a name by a renaming tool
        give the staged shape (digits are hex), and the photo it names is right there. A name
        of digits only is no leftover: neither removed nor reported. A real leftover next to
        it still goes."""
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age
        folder = tmp_path / HOSTILE
        photo = _photo(folder, "scan 50% [1].tif")
        lookalikes = [_photo(folder, f"20190815_201908151430_{photo.name}", b"PHOTO 2"),
                      _photo(folder, f"{photo.name}.20190815_201908151430.tif", b"PHOTO 3"),
                      _photo(folder, f"20190815_201908151430_{photo.name}_exiftool_tmp", b"P4")]
        real = _shapes(photo)
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, False)
        assert sorted(swept.removed) == sorted(str(m) for m in real)
        assert swept.kept == []
        assert [p.read_bytes() for p in lookalikes] == [b"PHOTO 2", b"PHOTO 3", b"P4"]
        assert photo.read_bytes() == b"PHOTO"

    def test_exiftools_temp_of_a_staged_file_or_a_temp_sibling(self, tmp_path, monkeypatch):
        """Review of v3.3.0 (N2): exiftool killed while writing a staged file or a temp sibling
        leaves `<that name>_exiftool_tmp`: it goes with that name's run id -- kept when it is
        this run's."""
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age
        photo = _photo(tmp_path / HOSTILE)
        d = photo.parent
        theirs = [d / f"{OTHER_ID}_0123456789ab_{photo.name}_exiftool_tmp",
                  d / f"{photo.name}.{OTHER_ID}_0123456789ab.tif_exiftool_tmp"]
        own = d / f"{RUN_ID}_0123456789ab_{photo.name}_exiftool_tmp"
        for p in theirs + [own]:
            p.write_bytes(b"LEFTOVER")
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, False)
        assert sorted(swept.removed) == sorted(str(p) for p in theirs)
        assert swept.kept == [(str(own), "this run's own")] and own.exists()

    def test_the_toolkit_never_writes_a_name_of_digits_only(self, monkeypatch):
        """Condition 6 must never keep a leftover of this toolkit: its names always carry a
        hex letter (uuid4 gives digits only once in ~12 000 names)."""
        import uuid as _uuid
        hexes = iter(["0" * 32, "1" * 32, "0123456789ab" + "0" * 20])
        monkeypatch.setattr(tw.uuid, "uuid4", lambda: _uuid.UUID(next(hexes)))
        assert tw.staged_name("12345678", "x.tif") == "12345678_0123456789ab_x.tif"
        hexes = iter(["0" * 32, "abc" + "0" * 29])
        assert tw.new_temp_sibling_path("x.tif", "12345678") == "x.tif.12345678_abc000000000.tif"
        hexes = iter(["0" * 32])
        assert tw.staged_name("1234567a", "x.tif") == "1234567a_000000000000_x.tif"

    def test_the_staging_report_skips_a_name_of_digits_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age
        stg = tmp_path / HOSTILE
        _photo(stg, "20190815_201908151430_scan.tif", b"12345")
        _photo(stg, f"{OTHER_ID}_0123456789ab_scan.tif", b"123")
        assert tw.staged_leftovers(str(stg), RUN_ID) == (1, 3)

    def test_a_tiff_source_with_a_tif_target(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
        folder = tmp_path / HOSTILE
        src = _photo(folder, "y 50%.tiff")
        target = folder / "y 50%.tif"
        sib = folder / f"y 50%.tif.{OTHER_ID}_0123456789ab.tif"
        sib.write_bytes(b"LEFTOVER")
        not_ours = folder / f"y 50%.tiff.{OTHER_ID}_0123456789ab.tif"
        not_ours.write_bytes(b"LEFTOVER OF ANOTHER TARGET")
        swept = tw.sweep_leftovers([(str(target), str(src))], RUN_ID, False)
        assert swept.removed == [str(sib)] and not_ours.exists()

    def test_a_dry_run_removes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
        photo = _photo(tmp_path / HOSTILE)
        made = _shapes(photo)
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, True)
        assert len(swept.removed) == 3 and all(m.exists() for m in made)

    def test_the_age_counts_from_the_creation_too(self, tmp_path, monkeypatch):
        """A staged file keeps its original's old write time (_keep_source_times): the
        creation time keeps it young."""
        photo = _photo(tmp_path / HOSTILE)
        made = _shapes(photo)
        old = 1_200_000_000
        for m in made:
            os.utime(m, (old, old))
        swept = tw.sweep_leftovers([(str(photo), str(photo))], RUN_ID, False)
        assert swept.removed == [] and all(m.exists() for m in made)

    def test_the_limit_is_a_safety_constant_not_a_setting(self):
        from tiff_workflow import compress_tiff_zip as cz
        from tiff_workflow import copy_exif_to_tiff as cx
        for specs in (cz.OVERLAY_SPECS, cx.OVERLAY_SPECS):
            assert not any("leftover" in k for k in specs)
        assert tw.LEFTOVER_MIN_AGE_S == 3600


def make_jpeg(path: Path, plain: Path):
    """A JPEG with camera EXIF, written in the plain folder `plain` (exiftool reads its command
    line in the ANSI code page) and moved to `path`."""
    plain.mkdir(parents=True, exist_ok=True)
    tmp = plain / "_fixture.jpg"
    subprocess.run(["magick", "-size", "16x16", "xc:gray", str(tmp)], check=True,
                   capture_output=True, timeout=120)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-EXIF:Make=FUJIFILM",
                    "-EXIF:Model=FinePixS5Pro", str(tmp)], check=True, capture_output=True,
                   timeout=120)
    shutil.copy2(tmp, path)
    tmp.unlink()
    return path


def _killed_copy_exif_folder(tmp_path):
    """A copy-exif session a killed --compress-zip run left: a staged file and a temp sibling
    of one photo, exiftool's temp file next to the other."""
    folder = tmp_path / HOSTILE
    a = make_tiff(folder, "x 50%.tif", seed=31)
    b = make_tiff(folder, "0014_14 (2).tif", seed=32)
    make_jpeg(folder / "x 50%.jpg", tmp_path / "plain")
    make_jpeg(folder / "0014_14 (2).jpg", tmp_path / "plain")
    left = [folder / f"{OTHER_ID}_0123456789ab_{b.name}",
            folder / f"{b.name}.{OTHER_ID}_0123456789ab.tif",
            folder / f"{a.name}_exiftool_tmp"]
    for p in left:
        p.write_bytes(b"LEFTOVER OF A KILLED RUN")
    return folder, a, b, left


@needs_tools
@pytest.mark.parametrize("lane", LANES)
def test_copy_exif_cleans_what_a_killed_run_left_and_then_works(tmp_path, monkeypatch, capsys,
                                                                lane):
    """v3.2.1: with `x 50%.tif_exiftool_tmp` there, every exiftool write of that photo failed
    ("Temporary file already exists") -- ERROR (exiftool EXIF) on every run until someone
    deleted it by hand."""
    from tiff_workflow import copy_exif_to_tiff as cx
    folder, a, b, left = _killed_copy_exif_folder(tmp_path)
    monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
    # a temp folder of its own: with the age limit at 0 the run sweeps every tw_* file of the
    # temp folder, also the ones other tests running at the same time are using
    private = tmp_path / "temp"
    private.mkdir()
    monkeypatch.setattr(tw.tempfile, "tempdir", str(private))
    monkeypatch.chdir(tmp_path)
    workers = ["--workers", "1"] if lane == "sequential" else []
    rc = cx.main(["--input-dir", str(folder), "--compress-zip", *workers])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert not any(p.exists() for p in left), out
    assert "Removed 3 leftover file(s) of an interrupted run" in out, out
    assert out.count("] OK+ZIP |") == 2, out
    assert compression(a) == "Adobe Deflate" and compression(b) == "Adobe Deflate"


@needs_tools
def test_copy_exif_keeps_recent_leftovers_and_a_dry_run_removes_nothing(tmp_path, monkeypatch,
                                                                        capsys):
    from tiff_workflow import copy_exif_to_tiff as cx
    folder, a, b, left = _killed_copy_exif_folder(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = cx.main(["--input-dir", str(folder), "--compress-zip", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and all(p.exists() for p in left), out
    assert "Kept 3 file(s) shaped like the leftovers" in out, out
    monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
    rc = cx.main(["--input-dir", str(folder), "--compress-zip", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and all(p.exists() for p in left), out
    assert "Would remove 3 leftover file(s)" in out, out


def test_sweep_temp_files_takes_only_its_own_old_names(tmp_path, monkeypatch):
    private = tmp_path / "temp 一時"
    private.mkdir()
    monkeypatch.setattr(tw.tempfile, "tempdir", str(private))
    mine = [private / "tw_0123456789abcdef.mie", private / "tw_0123456789abcdef.png"]
    others = [private / "tw_0123.mie", private / "other.mie", private / "tw_0123456789abcdef.txt"]
    for p in mine + others:
        p.write_bytes(b"x")
    assert tw.sweep_temp_files() == 0, "too recent"
    monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
    assert tw.sweep_temp_files(dry_run=True) == 2 and all(p.exists() for p in mine)
    assert tw.sweep_temp_files() == 2
    assert not any(p.exists() for p in mine) and all(p.exists() for p in others)


def test_compress_reports_what_an_earlier_run_left_in_the_staging_folder(tmp_path, monkeypatch):
    """A shared --staging-dir: reported, never removed (age alone proves nothing there)."""
    from tiff_workflow import compress_tiff_zip as cz
    monkeypatch.setattr(tw, "LEFTOVER_MIN_AGE_S", -60)  # any age (a file time can be a hair ahead of time.time())
    stg = tmp_path / "stg 一時 [x]"
    stg.mkdir()
    staged = stg / f"{OTHER_ID}_0123456789ab_p 50%.tif"
    staged.write_bytes(b"X" * 2_000_000)
    opt, _bound = tw.parse_args(["--mode", "1", "--staging-dir", str(stg), "--dry-run"], cz.PARAMS)
    lines = []

    def log(msg="", level="INFO"):
        lines.append((level, msg))
    log.path = ""
    run = cz.Run(opt, log, [])
    cz._sweep_leftovers(run, [])
    assert ("WARN", f"NOTE: 1 file(s) (2.0 MB) left in the staging folder by an earlier run: "
                    f"{stg}") in lines
    assert staged.exists()


# ---------------------------------------------------------------------------------------------
# Item 4 -- associated, non-opaque alpha: a SKIP
# ---------------------------------------------------------------------------------------------

REAL_ASSOC = (Path(os.environ.get("TW_FIXTURES", r"E:\TESTE")) / "TIFF_scans_varied"
              / "rgba16_assoc_deflate__kodakgold2000001.tif")


def _synthetic_assoc(path: Path):
    rng = np.random.default_rng(2)
    a = rng.integers(1, 65535, size=(40, 56), dtype=np.uint16)
    c = (rng.integers(0, 65535, size=(40, 56, 3)) * (a[..., None] / 65535)).astype(np.uint16)
    tifffile.imwrite(str(path), np.dstack([c, a]), photometric="rgb", extrasamples=[1])


def _real_assoc(path: Path):
    """A crop of the owner's real associated-RGBA scan, written uncompressed."""
    with tifffile.TiffFile(str(REAL_ASSOC)) as t:
        # the right edge, where the alpha is not opaque
        data = t.pages[0].asarray()[3500:3611, 4529:4679]
    tifffile.imwrite(str(path), data, photometric="rgb", extrasamples=[1])


def _assoc_fixture(folder: Path, kind: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "_fixture_alpha.tif"
    if kind == "real":
        if not REAL_ASSOC.is_file():
            pytest.skip(f"the owner's fixtures are not here: {REAL_ASSOC}")
        _real_assoc(tmp)
    else:
        _synthetic_assoc(tmp)
    final = folder / "kodak assoc (2) 50%.tif"
    tmp.rename(final)
    pages = tw.tiff_facts(str(final)).pages
    assert tw.associated_alpha_rewritable(str(final), pages) is False, \
        "the fixture must be one ImageMagick would change"
    return final


@needs_tools
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("kind", ["synthetic", "real"])
@pytest.mark.parametrize("mode", [8, 9])
def test_compress_skips_associated_alpha_in_every_mode(tmp_path, lane, kind, mode):
    """v3.2.1: ERROR (associated alpha that is not opaque ... refused) and exit 1 on every run."""
    folder = tmp_path / HOSTILE
    src = _assoc_fixture(folder, kind)
    before = sha(src)
    for _ in range(2):
        rc, out = run(lane, COMPRESS, ["--mode", mode, "--input-dir", folder], tmp_path)
        assert rc == 0, out[-3000:]
        assert (f"SKIP (not rewritten - associated alpha that is not opaque: ImageMagick would "
                f"change it) | {src.name}") in out, out[-3000:]
        assert sha(src) == before
        assert not (folder / "OLD_TIFFs").exists()


@needs_tools
@pytest.mark.parametrize("lane", LANES)
def test_copy_exif_copies_the_exif_and_skips_the_zip_of_associated_alpha(tmp_path, lane):
    folder = tmp_path / HOSTILE
    src = _assoc_fixture(folder, "synthetic")
    make_jpeg(folder / "kodak assoc (2) 50%.jpg", tmp_path / "plain")
    for _ in range(2):
        rc, out = run(lane, COPY_EXIF, ["--input-dir", folder, "--compress-zip"], tmp_path)
        assert rc == 0, out[-3000:]
        assert f"OK+SKIP-ZIP (associated alpha that is not opaque) | {src.name}" in out, out[-3000:]
        assert "FUJIFILM" in exif(src, "-s", "-s", "-s", "-EXIF:Make")
        assert compression(src) == "Uncompressed"


# ---------------------------------------------------------------------------------------------
# Item 6 -- the Summary lists the Advanced options
# ---------------------------------------------------------------------------------------------

def _summary(monkeypatch, capsys, workflow, screen):
    """What the screen before a run shows for `workflow`, on `screen`: step_confirm's Summary
    until v3.3, the review screen since (it lists the edited Advanced options the same way,
    _advanced_summary_rows); left with `q`."""
    import io
    from types import SimpleNamespace

    from tiff_workflow import wizard as ct
    out = io.StringIO()
    if screen == "rich":
        pytest.importorskip("rich")
        from rich.console import Console
        monkeypatch.setattr(ct, "RICH_AVAILABLE", True)
        monkeypatch.setattr(ct, "console", Console(file=out, force_terminal=False,
                                                   color_system=None, width=300))
        monkeypatch.setattr(ct.Prompt, "ask", staticmethod(lambda *a, **k: "q"))
    else:
        monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
        monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    cfg = SimpleNamespace(config=ct.ToolConfig(), save_config=lambda: None)
    assert ct._review_screen(cfg, workflow, Path("a"), [["Task:", "x"]], lambda dry: [],
                             lambda: None, lambda: None, set()) == "q"
    return out.getvalue() + capsys.readouterr().out


@pytest.mark.parametrize("screen", ["plain", "rich"])
def test_the_summary_lists_each_edited_advanced_option(monkeypatch, capsys, screen):
    wf = {"origin": "both", "mode": 9, "folders": [Path("a"), Path("b")], "pattern": "S5pro",
          "advanced": {"compress": {"magick_timeout": 180, "srgb_profile": "",
                                    "worker_memory_fraction": "lots"},
                       "copy_exif": {"fail_on_warn": True}}}
    text = _summary(monkeypatch, capsys, wf, screen)
    assert "Advanced:" in text, text
    assert "[compress] --magick-timeout = 180" in text, text
    assert "[compress] --srgb-profile = (auto)" in text, text
    assert "[compress] --worker-memory-fraction = 'lots' (ignored: not valid)" in text, text
    assert "[copy_exif] --fail-on-warn = on" in text, text
    # REGISTRY order: magick_timeout, srgb_profile, worker_memory_fraction
    assert text.index("--magick-timeout") < text.index("--srgb-profile") < \
        text.index("--worker-memory-fraction")


@pytest.mark.parametrize("screen", ["plain", "rich"])
def test_no_edited_option_no_row(monkeypatch, capsys, screen):
    wf = {"origin": "free_compress", "mode": 9, "input_dir": "x"}
    assert "Advanced:" not in _summary(monkeypatch, capsys, wf, screen)


# ---------------------------------------------------------------------------------------------
# Item 7 -- a thumbnail size below 32 in the settings file
# ---------------------------------------------------------------------------------------------

THUMBNAILS = ROOT / "tiff_workflow" / "generate_thumbnails.py"


def _settings(tmp_path, size):
    import json
    path = tmp_path / "settings 一時 [x].json"
    path.write_text(json.dumps({"generate_thumbnails": {"size": size}}), encoding="utf-8")
    return path


@needs_tools
@pytest.mark.parametrize("size,taken", [(16, False), (32, True), (4096, True), (4097, False)])
def test_the_settings_file_refuses_a_size_out_of_range_where_it_is_read(tmp_path, size, taken):
    """v3.2.1: `"size": 16` passed the file's "int:1:" and every run stopped with exit 2."""
    folder = tmp_path / HOSTILE
    make_tiff(folder, "a 50%.tif", seed=41)
    env = env_off(TIFFWORKFLOW_SETTINGS=str(_settings(tmp_path, size)))
    rc, out = run("sequential", THUMBNAILS, ["--input-dir", folder, "--dry-run"], tmp_path, env=env)
    assert rc == 0, out[-3000:]
    if taken:
        assert "1 value(s): size" in out, out[-3000:]
    else:
        assert f"size = {size} is not valid (a whole number from 32 to 4096)" in out, out[-3000:]


def test_the_wizard_plans_with_the_value_the_backend_runs_with(tmp_path, monkeypatch):
    from tiff_workflow import wizard as ct
    monkeypatch.setenv("TIFFWORKFLOW_SETTINGS", str(_settings(tmp_path, 16)))
    assert ct._backend_default("generate_thumbnails.py", "size") == 256
    _p, values, warnings = ct._overlay_scan("generate_thumbnails.py")
    assert "size" not in values and any("from 32 to 4096" in w for w in warnings), warnings
    monkeypatch.setenv("TIFFWORKFLOW_SETTINGS", str(_settings(tmp_path, 64)))
    assert ct._backend_default("generate_thumbnails.py", "size") == 64


def test_the_command_line_refuses_it_with_the_common_sentence():
    from tiff_workflow import generate_thumbnails as gt
    with pytest.raises(tw.UsageError) as e:
        tw.parse_args(["--size", "16"], gt.PARAMS)
    assert str(e.value) == "argument --size: must be from 32 to 4096, got 16"


def test_the_limit_has_one_copy():
    """32..4096 lives in the P of `size`; no other copy in the backend or the wizard."""
    for f in ("generate_thumbnails.py", "wizard.py"):
        text = (ROOT / "tiff_workflow" / f).read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if "4096" in ln]
        if f == "generate_thumbnails.py":
            assert lines == ['    P("size", "int", 256, lo=32, hi=4096, help="Thumbnail size in pixels"),'], lines
        else:
            assert lines == [], lines


# ---------------------------------------------------------------------------------------------
# Item 8 -- '%' in the temp folder and '%' in the file name
# ---------------------------------------------------------------------------------------------

def _tagged_tiff(folder: Path, name: str, plain: Path) -> Path:
    """An uncompressed TIFF with EXIF:Artist, made under a plain name in a plain folder and
    moved to `folder / name`."""
    plain.mkdir(parents=True, exist_ok=True)
    tmp = make_tiff(plain, "_tagged.tif", seed=51)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-EXIF:Artist=Fuji Tester",
                    str(tmp)], check=True, capture_output=True, timeout=120)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / name
    shutil.copy2(tmp, final)
    tmp.unlink()
    return final


def _temp_env(tmp_path, percent=True):
    temp = tmp_path / ("tmp 50% 伊豆沼 [x]" if percent else "tmp plain")
    temp.mkdir(exist_ok=True)
    return env_off(TEMP=str(temp), TMP=str(temp)), temp


def _mie_left(*folders):
    return [n for f in folders if f.exists() for n in os.listdir(f) if n.lower().endswith(".mie")]


@needs_tools
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("mode", [8, 9])
def test_a_percent_in_the_temp_folder_no_longer_refuses_a_percent_name(tmp_path, lane, mode):
    """v3.2.1: ERROR (metadata container failed - write refused) for every '%' file on a
    machine whose temp folder has a '%' in its path."""
    folder = tmp_path / "伊豆沼 [FINAL] x"                 # no '%' in the folder
    src = _tagged_tiff(folder, "0014_14 (2) 50%.tif", tmp_path / "plain")
    env, temp = _temp_env(tmp_path)
    rc, out = run(lane, COMPRESS, ["--mode", mode, "--input-dir", folder], tmp_path, env=env)
    assert rc == 0, out[-3000:]
    assert f"OK (Uncompressed -> ZIP) | {src.name}" in out, out[-3000:]
    assert compression(src) == "Adobe Deflate"
    assert exif(src, "-s", "-s", "-s", "-EXIF:Artist") == "Fuji Tester"
    assert _mie_left(folder, temp) == []


@needs_tools
@pytest.mark.parametrize("lane", LANES)
def test_the_container_goes_to_the_staging_folder_when_the_others_have_a_percent(tmp_path, lane):
    folder = tmp_path / HOSTILE                               # '%' in the folder too
    src = _tagged_tiff(folder, "0014_14 (2) 50%.tif", tmp_path / "plain")
    stg = tmp_path / "stg 一時 [x]"
    env, temp = _temp_env(tmp_path)
    rc, out = run(lane, COMPRESS, ["--mode", 8, "--input-dir", folder, "--staging-dir", stg],
                  tmp_path, env=env)
    assert rc == 0, out[-3000:]
    assert compression(src) == "Adobe Deflate"
    assert exif(src, "-s", "-s", "-s", "-EXIF:Artist") == "Fuji Tester"
    assert _mie_left(folder, temp, stg) == []


@needs_tools
@pytest.mark.parametrize("lane", LANES)
def test_every_folder_with_a_percent_is_still_refused(tmp_path, lane):
    folder = tmp_path / HOSTILE
    src = _tagged_tiff(folder, "0014_14 (2) 50%.tif", tmp_path / "plain")
    before = sha(src)
    stg = tmp_path / "stg 50% [x]"
    env, temp = _temp_env(tmp_path)
    rc, out = run(lane, COMPRESS, ["--mode", 8, "--input-dir", folder, "--staging-dir", stg],
                  tmp_path, env=env)
    assert rc == 1, out[-3000:]
    assert f"ERROR (metadata container failed - write refused) | {src.name}" in out, out[-3000:]
    assert sha(src) == before


def test_metadata_container_path_order(tmp_path, monkeypatch):
    temp_pct = tmp_path / "t 50%"
    temp_pct.mkdir()
    stg = tmp_path / "stg [x]"
    photo_dir = tmp_path / "伊豆沼 50% x"
    monkeypatch.setattr(tw.tempfile, "gettempdir", lambda: str(temp_pct))
    p = tw.metadata_container_path(stg / "a.tif", photo_dir / "a 50%.tif")
    assert os.path.dirname(p) == str(stg) and re.fullmatch(r"tw_[0-9a-f]{16}\.mie", os.path.basename(p))
    assert tw.TEMP_FILE_RE.match(os.path.basename(p))
    with pytest.raises(tw.ToolError):
        tw.metadata_container_path(photo_dir / "a 50%.tif")
    plain = tmp_path / "plain"
    monkeypatch.setattr(tw.tempfile, "gettempdir", lambda: str(plain))
    assert os.path.dirname(tw.metadata_container_path(photo_dir / "a 50%.tif")) == str(plain)


@needs_tools
@pytest.mark.parametrize("case", ["temp", "photo-folder", "refused"])
def test_the_wizards_tag_source_follows_the_same_rule(tmp_path, monkeypatch, case):
    from tiff_workflow import wizard as ct
    temp_pct = tmp_path / "t 50% 伊豆沼"
    temp_pct.mkdir()
    temp_plain = tmp_path / "t plain"
    temp_plain.mkdir()
    folder = tmp_path / ("伊豆沼 50% x" if case == "refused" else "伊豆沼 [x]")
    src = _tagged_tiff(folder, "a 50%.tif", tmp_path / "plain")
    monkeypatch.setattr(tw.tempfile, "gettempdir",
                        lambda: str(temp_plain if case == "temp" else temp_pct))
    source, temp = ct._safe_tag_source(src)
    if case == "refused":
        assert (source, temp) == (None, None)
        return
    try:
        expected = temp_plain if case == "temp" else folder
        assert source == temp and os.path.dirname(temp) == str(expected)
        assert "%" not in temp and Path(temp).is_file()
    finally:
        if temp:
            Path(temp).unlink(missing_ok=True)
