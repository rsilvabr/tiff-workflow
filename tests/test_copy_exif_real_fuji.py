"""audit round 20: Copy EXIF finds Capture One variant TIFFs and falls back to the RAF.

Real photos only. The fixtures live outside the repo and are pointed at by
`TW_FUJI_FIXTURES` (rewritten by the owner on 2026-10-10 from a read-only archive; no
default in the code, the folder is private and never committed). Nothing here ever
writes into the fixture folder: each test copies the subfolder it needs to tmp_path
and runs the backend there.

Fixture layout -- the owner's real Capture One exports, a `<stem>_1.TIF` /
`<stem>_2.TIF` variant next to `<stem>.JPG` and `<stem>.RAF`:

    250309_東京駅_S5Pro     _DSF4796.TIF + .JPG + .RAF, _DSF4817_1/_2.TIF + _DSF4817.JPG,
                          _DSF4828_1.TIF + _DSF4828.JPG, _DSF4853_1.TIF + _DSF4853.JPG
    250309_S5Pro_rafonly  _DSF4796.TIF + _DSF4796.RAF (S5 Pro RAW-only session, no JPEG)
    250428_S3Pro          DSCF0092_1.TIF + DSCF0092.RAF (S3 Pro, no JPEG)

The module skips itself when the env var is unset, the folder is missing, or any of
pwsh / powershell / magick / exiftool is missing. The backends and the real files are
driven through the runners of tests/test_regression_classes.py, not copies of them.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from test_regression_classes import magick_spec, page_count, read_ifd0_compression, run_ps

FIXTURES = os.environ.get("TW_FUJI_FIXTURES")

if not FIXTURES:
    pytest.skip("TW_FUJI_FIXTURES is not set", allow_module_level=True)

FIXTURE_ROOT = Path(FIXTURES)
if not FIXTURE_ROOT.is_dir():
    pytest.skip(f"TW_FUJI_FIXTURES is not a folder: {FIXTURES}", allow_module_level=True)

_MISSING = [tool for tool in ("pwsh", "powershell", "magick", "exiftool")
            if shutil.which(tool) is None]
if _MISSING:
    pytest.skip("missing tools: " + ", ".join(_MISSING), allow_module_level=True)

S5_VARIANTS = "250309_東京駅_S5Pro"
S5_RAFONLY = "250309_S5Pro_rafonly"
S3_RAFONLY = "250428_S3Pro"

COPY_EXIF_TARGETS = [
    ("pwsh", "copy_exif_to_TIFF_ps7.ps1"),
    ("powershell", "copy_exif_to_TIFF_ps5.ps1"),
]


def copy_fixture(tmp_path, name):
    dst = tmp_path / name
    shutil.copytree(FIXTURE_ROOT / name, dst)
    return dst


def read_tag(path, tag):
    """One tag, raw, via a UTF-8 `-@` argfile so non-ASCII paths stay intact.

    One call per tag on purpose: `-s -s -s` prints the VALUE only (no tag names),
    which is exactly how the backends read tags, so several tags in one call
    could not be mapped back to their names."""
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(["-charset", "filename=utf8", "-s", "-s", "-s",
                            f"-{tag}", str(path)]) + "\n")
        argfile = fh.name
    try:
        result = subprocess.run(["exiftool", "-@", argfile],
                                capture_output=True, timeout=120)
    finally:
        os.unlink(argfile)
    return result.stdout.decode("utf-8", errors="replace").strip()


def ae_pages(path_a, path_b, page):
    """Absolute pixel-error count of one page pair; '0' means pixel-identical."""
    result = subprocess.run(
        ["magick", "compare", "-metric", "AE",
         f"{magick_spec(path_a)}[{page}]", f"{magick_spec(path_b)}[{page}]", "null:"],
        capture_output=True, text=True, timeout=600,
    )
    return (result.stderr or result.stdout).strip()


def backend_args(shell, folder):
    args = ["-InputDir", str(folder)]
    if shell == "pwsh":
        args += ["-Workers", "4"]
    return args


@pytest.mark.parametrize("shell,script", COPY_EXIF_TARGETS)
class TestCaptureOneVariants:
    def test_t1_capture_one_variants_pair_with_the_jpeg(self, tmp_path, shell, script):
        work = copy_fixture(tmp_path, S5_VARIANTS)

        result = run_ps(shell, script, backend_args(shell, work), tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        for line in (
            "OK | _DSF4817_1.TIF <= _DSF4817.JPG",
            "OK | _DSF4817_2.TIF <= _DSF4817.JPG",
            "OK | _DSF4828_1.TIF <= _DSF4828.JPG",
            "OK | _DSF4853_1.TIF <= _DSF4853.JPG",
            "OK | _DSF4796.TIF <= _DSF4796.JPG",
        ):
            assert line in result.stdout, f"missing result line: {line}\n{result.stdout}"
        assert "MISS" not in result.stdout, result.stdout
        assert "heuristic" not in result.stdout, result.stdout

        for tif_name, jpg_name in (
            ("_DSF4817_1.TIF", "_DSF4817.JPG"),
            ("_DSF4817_2.TIF", "_DSF4817.JPG"),
            ("_DSF4828_1.TIF", "_DSF4828.JPG"),
            ("_DSF4853_1.TIF", "_DSF4853.JPG"),
            ("_DSF4796.TIF", "_DSF4796.JPG"),
        ):
            out = work / tif_name
            jpg = FIXTURE_ROOT / S5_VARIANTS / jpg_name
            for tag in ("DateTimeOriginal", "ExposureTime", "FNumber", "ISO"):
                assert read_tag(out, tag) == read_tag(jpg, tag), (
                    f"{tif_name}: {tag} does not match the JPEG it was paired with"
                )
            assert page_count(out) == 2, f"{tif_name}: page count changed"
            assert read_ifd0_compression(out) == read_ifd0_compression(
                FIXTURE_ROOT / S5_VARIANTS / tif_name
            ), f"{tif_name}: compression changed"
            for page in (0, 1):
                assert ae_pages(out, FIXTURE_ROOT / S5_VARIANTS / tif_name,
                                page).split()[0] == "0", (
                    f"{tif_name}: page {page} pixels changed"
                )

    def test_t2_raf_is_the_source_when_there_is_no_jpeg(self, tmp_path, shell, script):
        work = copy_fixture(tmp_path, S5_RAFONLY)

        result = run_ps(shell, script, backend_args(shell, work), tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert ("OK | _DSF4796.TIF <= _DSF4796.RAF [EXIF from RAW]"
                in result.stdout), result.stdout

        out = work / "_DSF4796.TIF"
        jpg = FIXTURE_ROOT / S5_VARIANTS / "_DSF4796.JPG"
        for tag in ("DateTimeOriginal", "ExposureTime", "FNumber", "ISO"):
            assert read_tag(out, tag) == read_tag(jpg, tag), (
                f"{tag}: the RAF source did not reproduce the JPEG value"
            )
        assert read_tag(out, "FocalPlaneXResolution") == "", (
            "the RAF preview's focal-plane resolution (630) leaked onto the TIFF"
        )
        assert read_tag(out, "ExifImageWidth") != "1440", (
            "the RAF preview's ExifImageWidth (1440) leaked onto the TIFF"
        )
        for page in (0, 1):
            assert ae_pages(out, FIXTURE_ROOT / S5_RAFONLY / "_DSF4796.TIF",
                            page).split()[0] == "0", f"page {page} pixels changed"

    def test_t3_s3pro_raw_only_session(self, tmp_path, shell, script):
        work = copy_fixture(tmp_path, S3_RAFONLY)

        result = run_ps(shell, script, backend_args(shell, work), tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        for line in (
            "OK | DSCF0092_1.TIF <= DSCF0092.RAF [EXIF from RAW]",
            "OK | DSCF0095_1.TIF <= DSCF0095.RAF [EXIF from RAW]",
        ):
            assert line in result.stdout, f"missing result line: {line}\n{result.stdout}"

        for tif_name, raf_name in (("DSCF0092_1.TIF", "DSCF0092.RAF"),
                                   ("DSCF0095_1.TIF", "DSCF0095.RAF")):
            out = work / tif_name
            raf = FIXTURE_ROOT / S3_RAFONLY / raf_name
            assert read_tag(out, "Model") == "FinePix S3Pro"
            assert read_tag(out, "DateTimeOriginal") == read_tag(raf, "DateTimeOriginal"), (
                f"{tif_name}: DateTimeOriginal does not match its RAF"
            )

    def test_t4_second_run_over_own_output(self, tmp_path, shell, script):
        work = copy_fixture(tmp_path, S5_VARIANTS)
        first = run_ps(shell, script, backend_args(shell, work), tmp_path)
        assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"

        again = run_ps(shell, script, backend_args(shell, work), tmp_path)

        assert again.returncode == 0, f"{again.stdout}\n{again.stderr}"
        assert "MISS" not in again.stdout, again.stdout
        assert "ERROR" not in again.stdout, again.stdout
        for tif_name, jpg_name in (("_DSF4817_1.TIF", "_DSF4817.JPG"),
                                   ("_DSF4817_2.TIF", "_DSF4817.JPG"),
                                   ("_DSF4828_1.TIF", "_DSF4828.JPG"),
                                   ("_DSF4853_1.TIF", "_DSF4853.JPG"),
                                   ("_DSF4796.TIF", "_DSF4796.JPG")):
            out = work / tif_name
            jpg = FIXTURE_ROOT / S5_VARIANTS / jpg_name
            for tag in ("DateTimeOriginal", "ExposureTime", "FNumber", "ISO"):
                assert read_tag(out, tag) == read_tag(jpg, tag), (
                    f"{tif_name}: {tag} changed on the second run"
                )
            for page in (0, 1):
                assert ae_pages(out, FIXTURE_ROOT / S5_VARIANTS / tif_name,
                                page).split()[0] == "0", (
                    f"{tif_name}: page {page} pixels changed on the second run"
                )

    def test_t5_percent_name_with_raf(self, tmp_path, shell, script):
        work = copy_fixture(tmp_path, S5_RAFONLY)
        (work / "_DSF4796.TIF").rename(work / "50%off_1.TIF")
        (work / "_DSF4796.RAF").rename(work / "50%off.RAF")

        result = run_ps(shell, script, backend_args(shell, work), tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert ("OK | 50%off_1.TIF <= 50%off.RAF [EXIF from RAW]"
                in result.stdout), result.stdout
        assert read_tag(work / "50%off_1.TIF", "DateTimeOriginal") == read_tag(
            work / "50%off.RAF", "DateTimeOriginal"
        ), "the '%' RAF name lost its EXIF on the way to the TIFF"
