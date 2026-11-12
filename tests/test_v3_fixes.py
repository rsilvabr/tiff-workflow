"""v3.0.0: the problems of the PowerShell edition found while porting it to Python.

Each of these was confirmed on the v2.9.1 .ps1 scripts (both shells) on 2026-11-11 and is
listed in docs/powershell_edition.md. None of them damaged a file; three made a run that did
nothing (or did not do what was asked) end with exit code 0.

    1. `-InputDir .` kept its trailing `\\.`, so the folder exclusion fell back to the whole
       path and an -ExcludeFolders entry equal to the folder's OWN name excluded the whole
       run (generate_thumbnails and copy_exif; compress had been fixed in round 16).
    2. copy_exif `-DryRun -CompressZip -OutputDir X` created the folder X.
    3. Legacy mode with -StagingDir (or the output path) naming an existing FILE logged an
       ERROR for every folder and exited 0.
    4. `-Mode 19` (a mode that does not exist) found no files and exited 0.

Also here: the two round-16 rules about how compress resolves -InputDir and -OutputDir,
end to end (they were pinned on the PowerShell source before).
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backends import LANES  # noqa: E402
from test_regression_classes import make_tiff, requires_tools, run_ps  # noqa: E402

HOSTILE = "写真 [x] 5%"


def hostile_folder(tmp_path, *names):
    """A folder with a hostile name holding `names`, built under a plain name and renamed
    (the fixtures' own exiftool call reads its command line in the ANSI code page)."""
    plain = tmp_path / "plain"
    plain.mkdir()
    for n in names:
        if n.lower().endswith((".jpg", ".jpeg")):
            subprocess.run(["magick", "-size", "64x48", "gradient:", str(plain / n)],
                           check=True, capture_output=True, timeout=120)
        else:
            make_tiff(plain / n)
    work = tmp_path / HOSTILE
    plain.rename(work)
    return work


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestRootsAndOutputDirAreResolvedOnce:
    @pytest.mark.parametrize("script,extra", [
        ("compress_tiff_zip.py", ["--mode", "9", "--dry-run"]),        # round 16
        ("generate_thumbnails.py", ["--recursive", "--dry-run"]),      # 1
        ("copy_exif_to_tiff.py", ["--dry-run"]),                      # 1
    ])
    def test_dot_root_is_normalised_before_the_exclusion(self, tmp_path, lane, script, extra):
        work = hostile_folder(tmp_path, "a.tif", "a.jpg")
        result = run_ps(lane, script, extra + ["--input-dir", ".", "--exclude-folders", work.name], work)
        assert result.returncode == 0, result.stdout
        assert "| a.tif" in result.stdout and "DRY" in result.stdout, result.stdout

    def test_relative_output_dir_is_one_folder_for_every_root(self, tmp_path, lane):
        """Round 16: '-InputDir A;B -OutputDir out' scattered one flat output across A\\out
        and B\\out."""
        for sub in ("A", "B"):
            (tmp_path / sub).mkdir()
            make_tiff(tmp_path / sub / "x.tif")
        result = run_ps(lane, "compress_tiff_zip.py",
                        ["--mode", "2", "--input-dir", f"{tmp_path / 'A'};{tmp_path / 'B'}",
                         "--output-dir", "out"], tmp_path)
        assert result.returncode == 0, result.stdout
        assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["x.tif", "x_v2.tif"]
        assert not (tmp_path / "A" / "out").exists() and not (tmp_path / "B" / "out").exists()


@requires_tools
@pytest.mark.parametrize("lane", LANES)
def test_copy_exif_dry_run_creates_no_output_folder(tmp_path, lane):
    """2. A dry run plans and reports; it creates nothing."""
    work = hostile_folder(tmp_path, "a.tif", "a.jpg")
    out_dir = tmp_path / "out [not yet]"
    stage = tmp_path / "stage [not yet]"
    result = run_ps(lane, "copy_exif_to_tiff.py",
                    ["--input-dir", work, "--dry-run", "--compress-zip", "--output-dir", out_dir,
                     "--staging-dir", stage], tmp_path)
    assert result.returncode == 0, result.stdout
    assert "DRY (EXIF + ZIP) | a.tif <= a.jpg" in result.stdout, result.stdout
    assert not out_dir.exists() and not stage.exists()
    assert sorted(p.name for p in work.iterdir()) == ["a.jpg", "a.tif"]


@requires_tools
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("flag", ["--staging-dir", "--output-dir"])
def test_a_file_where_a_folder_is_needed_exits_1(tmp_path, lane, flag):
    """3. Nothing was compressed: that is a failed run, not a clean one. (Found in the
    legacy mode, which left with v3.1; the same question for mode 2, in place.)"""
    work = hostile_folder(tmp_path, "a.tif")
    before = (work / "a.tif").read_bytes()
    not_a_folder = tmp_path / "a file.txt"
    not_a_folder.write_text("x")
    result = run_ps(lane, "compress_tiff_zip.py",
                    ["--mode", "2", "--input-dir", work, flag, not_a_folder], tmp_path)
    assert result.returncode == 1, result.stdout
    if flag == "--staging-dir":
        assert "ERROR (StagingDir exists as a file) | a.tif" in result.stdout, result.stdout
    else:
        # v3.1: said before anything starts. Until v3.0.0 os.makedirs raised in the middle of
        # the run and it ended in `FATAL (unhandled error) | FileExistsError`.
        assert f"ERROR: -OutputDir is a file, not a folder: {not_a_folder}" in result.stdout, result.stdout
        assert "FATAL" not in result.stdout and not (tmp_path / "Logs").exists(), result.stdout
    assert (work / "a.tif").read_bytes() == before
    assert not_a_folder.read_text() == "x"
    assert sorted(p.name for p in work.iterdir()) == ["a.tif"], "something was left next to it"


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("mode", ["10", "19", "99", "-1", "-5"])
def test_a_mode_that_does_not_exist_is_refused(tmp_path, lane, mode):
    """4. A typo in -Mode must not read as "nothing to do"."""
    if shutil.which("magick") is None or shutil.which("exiftool") is None:
        pytest.skip("ImageMagick and exiftool required")
    result = run_ps(lane, "compress_tiff_zip.py", ["--mode", mode, "--input-dir", tmp_path], tmp_path)
    assert result.returncode == 1, result.stdout
    assert f"-Mode {mode} does not exist" in result.stdout
    assert not (tmp_path / "Logs").exists(), "refused before anything is created"


@requires_tools
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("args", [[], ["--input-dir", "."], ["--restore-stranded"],
                                  ["--staging-dir", "stg", "--generate-thumbnail"]],
                         ids=["nothing", "inputdir", "restore-stranded", "other-options"])
def test_compress_without_a_mode_is_refused(tmp_path, lane, args):
    """v3.1: until v3.0.0 a run without -Mode was the "legacy" mode -- it compressed every
    TIFF under the CURRENT folder in place, with no OLD_TIFFs backup. There is no default
    mode now: a forgotten -Mode touches nothing."""
    work = hostile_folder(tmp_path, "a.tif")
    before = (work / "a.tif").read_bytes()
    result = run_ps(lane, "compress_tiff_zip.py", args, work)
    assert result.returncode == 1, result.stdout
    assert "-Mode is required" in result.stdout, result.stdout
    assert (work / "a.tif").read_bytes() == before, "a run without -Mode rewrote the file"
    assert sorted(p.name for p in work.iterdir()) == ["a.tif"], "refused before anything is created"
