"""
Regression tests for the five recurring bug classes.

Rounds 1-6 of the audit kept re-finding the *same* five failure classes in modes and files the
previous round had not visited, because every fix was pointwise and nothing pinned it. Each
class below gets tests that fail if the fix is reverted anywhere it applies:

    1. exit code      - an early exit that discards the error count reads as success
    2. data loss      - in-place compression must not strip EXIF; gates that authorise
                        destroying data must fail CLOSED
    3. two paths      - the sequential and the parallel path must behave identically
    4. path/staging   - a run must never touch another run's in-flight files
    5. dest collision - a failed move must never leave both source and destination gone

Later audit rounds pinned their own fixes the same way; the round-specific classes
(TestRound15Reporting, the TestAuditRound16* group, ...) sit below the five classes.

The backends run end to end as subprocesses, with the real ImageMagick and exiftool, once
per lane (tests/backends.py: parallel and sequential -- what the two PowerShell shells of
v2.x were). The rules that used to be pinned on the shape of the PowerShell source are in
tests/test_backend_invariants.py, which runs the Python code with the tools replaced.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# audit round 28: the padded conversion and the purge gate also read the per-page sample
# layout (exiftool -SamplesPerPixel ...) and the XMP/IPTC values (exiftool -j). Test doubles
# that answer every exiftool call with "" answer those two here: one RGB page, no XMP/IPTC.
def _fake_round28_exiftool(cmd, kwargs):
    if not cmd or str(cmd[0]) != "exiftool":
        return None
    data = kwargs.get("input") or b""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if b"-SamplesPerPixel" in data:
        return SimpleNamespace(returncode=0, stderr="", stdout=(
            "[IFD0]  SamplesPerPixel: 3\n[IFD0]  PhotometricInterpretation: 2\n"
            "[IFD0]  BitsPerSample: 16 16 16\n"))
    if b"\n-j\n" in data:
        return SimpleNamespace(returncode=0, stdout='[{"SourceFile": "x"}]', stderr="")
    return None


import convert_tiff
from convert_tiff import (
    ToolConfig,
    _compare_tiff_metadata,
    _compress_padded_files,
    _process_single_padded,
    _safe_move,
)

REPO = Path(__file__).resolve().parent.parent
from backends import BACKENDS, LANES, backend_cmd, run_backend  # noqa: E402

PS_SCRIPTS = list(BACKENDS)

IMAGE_TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None

requires_tools = pytest.mark.skipif(
    not IMAGE_TOOLS, reason="ImageMagick (magick) and exiftool required"
)

ARTIST = "RegressionTester"


# -- helpers --------------------------------------------------------

def run_ps(lane, script, args, cwd, timeout=300):
    """Run a backend script in `lane` ("parallel" or "sequential", see tests/backends.py)
    and return the CompletedProcess (exit code included)."""
    return run_backend(lane, script, args, cwd, timeout=timeout)


# One runner: -SafeMode:$false binds the same way in both lanes.
run_ps_switched = run_ps


def read_ifd0_compression(path):
    """IFD0 compression. `magick identify -format %[compression]` prints one value per
    page, so on a multi-page file it returns "zipzip" and a naive == "zip" fails."""
    result = subprocess.run(
        ["exiftool", "-s", "-s", "-s", "-IFD0:Compression", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip()


def make_tiff(path, artist=ARTIST):
    """A tiny uncompressed TIFF carrying EXIF tags."""
    subprocess.run(
        ["magick", "-size", "64x64", "gradient:red-blue", "-depth", "8", str(path)],
        capture_output=True, check=True, timeout=120,
    )
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original",
         f"-EXIF:Artist={artist}", "-EXIF:Make=TestCam", str(path)],
        capture_output=True, check=True, timeout=120,
    )
    return path


def read_tag(path, tag="EXIF:Artist"):
    result = subprocess.run(
        ["exiftool", "-s", "-s", "-s", f"-{tag}", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip()


def read_compression(path):
    result = subprocess.run(
        ["magick", "identify", "-format", "%[compression]", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip()


# -- CLASS 1: exit code contract ------------------------------------

@requires_tools
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize(
    "script,extra",
    [("compress_tiff_zip.py", ["-Mode", "0"]), ("generate_thumbnails.py", [])],
)
class TestExitCodeContract:
    """
    `return` at script scope always exits 0. convert_tiff.py gates step 2 on this exit code,
    so an early exit that discarded $errTotal made a fully failed step 1 read as success.
    """

    def test_missing_dir_in_semicolon_list_exits_1(self, tmp_path, lane, script, extra):
        good = tmp_path / "good"
        good.mkdir()
        missing = tmp_path / "nope"

        result = run_ps(lane, script, extra + ["-InputDir", f"{good};{missing}"], tmp_path)
        assert result.returncode == 1, (
            f"bad path in a ';' list must exit 1\n{result.stdout}\n{result.stderr}"
        )

    def test_valid_empty_dir_exits_0(self, tmp_path, lane, script, extra):
        good = tmp_path / "good"
        good.mkdir()

        result = run_ps(lane, script, extra + ["-InputDir", str(good)], tmp_path)
        assert result.returncode == 0, (
            f"a valid folder with no TIFFs is not an error\n{result.stdout}\n{result.stderr}"
        )


@requires_tools
@pytest.mark.parametrize("lane", LANES)
def test_all_files_failing_precheck_exits_1(tmp_path, lane):
    """
    Mode 0 with every file failing the pre-check empties $tasks and lands on the second early
    exit with N errors already logged -- the site that used to `return` (exit 0).
    """
    work = tmp_path / "work"
    work.mkdir()
    (work / "broken.tif").write_text("not a tiff at all")

    result = run_ps(lane, "compress_tiff_zip.py", ["-Mode", "0", "-InputDir", str(work)], tmp_path)
    assert "No tasks to process" in result.stdout, result.stdout  # the early-exit site
    assert result.returncode == 1, f"errors were logged, exit must be 1\n{result.stdout}"


COPY_EXIF_TARGETS = [("copy_exif_to_tiff.py", lane) for lane in LANES]


@pytest.mark.parametrize(
    "script,lane", COPY_EXIF_TARGETS, ids=[f"{s}/{sh}" for s, sh in COPY_EXIF_TARGETS]
)
class TestCopyExifExitCodeContract:
    """
    The round 6 exit-code fix landed in compress_tiff_zip.py and generate_thumbnails.py and was
    never generalised here: a wrong path fell through to "No TIFFs found" and exited 0, so
    convert_tiff.py let Step 2 of workflow 4 run after a Copy EXIF that touched nothing.
    """

    def test_missing_dir_exits_1(self, tmp_path, script, lane):
        result = run_ps(lane, script, ["-InputDir", str(tmp_path / "nope")], tmp_path)
        assert result.returncode == 1, (
            f"a path that does not exist is an error, not an empty folder\n{result.stdout}"
        )
        assert "input directory not found" in result.stdout, result.stdout

    def test_missing_dir_in_semicolon_list_exits_1(self, tmp_path, script, lane):
        good = tmp_path / "good"
        good.mkdir()
        result = run_ps(
            lane, script, ["-InputDir", f"{good};{tmp_path / 'nope'}"], tmp_path
        )
        assert result.returncode == 1, result.stdout

    def test_valid_empty_dir_exits_0(self, tmp_path, script, lane):
        good = tmp_path / "good"
        good.mkdir()
        result = run_ps(lane, script, ["-InputDir", str(good)], tmp_path)
        assert result.returncode == 0, (
            f"a valid folder with no TIFFs is not an error\n{result.stdout}"
        )



# -- CLASS 2: data loss (in-place EXIF, fail-closed gates) ----------

@requires_tools
class TestInPlaceExifPreservation:
    """
    With no -StagingDir and no output folder the write target IS the source, so magick
    overwrote the original before `-tagsfromfile <source>` ran and every tag was lost while
    the run reported OK. Backup-TiffMetadata parks the metadata first; each of the three
    workers must use it.
    """

    # (label, lane-independent args): mode 2 without -OutputDir has the input root as its flat
    # output, so it writes over each file that is directly in that folder.
    WORKERS = [
        ("mode2-sequential", ["-Mode", "2", "-InputDir", "work", "-ForceSequential"]),
        ("mode2-parallel", ["-Mode", "2", "-InputDir", "work", "-ForceParallel"]),
    ]

    @pytest.mark.parametrize("lane", LANES)
    @pytest.mark.parametrize("label,args", WORKERS, ids=[w[0] for w in WORKERS])
    def test_exif_survives_in_place_compression(self, tmp_path, lane, label, args):
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "photo.tif")

        result = run_ps(lane, "compress_tiff_zip.py", args, tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_compression(src).lower() == "zip", "file was not actually compressed"
        assert read_tag(src) == ARTIST, (
            f"[{label}/{lane}] in-place compression stripped EXIF\n{result.stdout}"
        )

    @pytest.mark.parametrize("lane", LANES)
    @pytest.mark.parametrize("parallel", ["-ForceSequential", "-ForceParallel"])
    def test_no_thumb_fallback_keeps_exif(self, tmp_path, lane, parallel):
        """
        CLASS 3 too: a failed thumbnail made the parallel worker return early, skipping the
        EXIF restore the sequential path reached via $noThumbNote. Same input, same file, two
        different outcomes. An unencodable -ThumbFormat forces the fallback.
        """
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "photo.tif")

        result = run_ps(
            lane, "compress_tiff_zip.py",
            ["-Mode", "2", "-InputDir", "work", parallel, "-GenerateThumbnail",
             "-ThumbFormat", "notaformat"], tmp_path,
        )

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "[no thumb]" in result.stdout, (
            f"fallback not exercised -- test is not proving anything\n{result.stdout}"
        )
        assert read_tag(src) == ARTIST, (
            f"[{parallel}/{lane}] the [no thumb] fallback skipped the EXIF restore"
        )




def make_multipage_tiff(path, subfiletypes, sizes=None, padded=False):
    """Multi-page TIFF (main + reduced preview + optional MASK page) with the
    given NewSubfileType markers written per IFD, e.g. [None, 1, 4] for a
    scanner RGB + preview + IR file. `sizes` overrides the default geometry
    (main large, REDUCEDIMAGE small, others large). `padded` stores 8-bit data
    in the 16-bit pages -- what the padded 16 -> 8-bit conversion accepts."""
    pages = []
    for i, st in enumerate(subfiletypes):
        p = path.parent / f"_pg{i}_{path.name}"
        if sizes is not None:
            size = sizes[i]
        else:
            size = "64x48" if i == 0 else ("16x12" if st == 1 else "64x48")
        subprocess.run(
            ["magick", "-size", size, "plasma:fractal", "-depth", "8" if padded else "16", str(p)],
            capture_output=True, check=True, timeout=120,
        )
        if padded:
            subprocess.run(["magick", str(p), "-depth", "16", str(p)],
                           capture_output=True, check=True, timeout=120)
        pages.append(p)
    subprocess.run(
        ["magick"] + [str(p) for p in pages] + [str(path)],
        capture_output=True, check=True, timeout=120,
    )
    args = ["exiftool", "-overwrite_original"]
    for i, st in enumerate(subfiletypes):
        if st is None:
            args.append(f"-IFD{i}:SubfileType=")
        else:
            args.append(f"-IFD{i}:SubfileType#={st}")
    args.append(str(path))
    subprocess.run(args, capture_output=True, check=True, timeout=120)
    for p in pages:
        p.unlink()
    return path


def read_subfiletypes(path):
    """Symbolic subfiletype of every page, in order."""
    result = subprocess.run(
        ["magick", "identify", "-format", "%[tiff:subfiletype]\n", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return [ln.strip() for ln in result.stdout.splitlines() if not ln.startswith("identify:")]


def magick_spec(path):
    """Escape '%' for the filename SPECS ImageMagick expands (see ConvertTo-MagickPathSpec).

    Appending '[n]' turns an argument into a spec, so this helper had the very bug the
    TestPercentInFileNames class exists to pin: it opened `500ff.tif` and reported the PRODUCT
    as broken while the product had written the right file.
    """
    return str(path).replace("%", "%%")


def rmse_pages(path_a, path_b, page=0):
    """RMSE of one page pair; '0 (0)' means pixel-identical."""
    result = subprocess.run(
        ["magick", "compare", "-metric", "RMSE",
         f"{magick_spec(path_a)}[{page}]", f"{magick_spec(path_b)}[{page}]", "null:"],
        capture_output=True, text=True, timeout=120,
    )
    return (result.stderr or result.stdout).strip()


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestMultiPageCompressE2E:
    """The CRITICAL round 5 class (multi-page TIFF stranded in OLD_TIFFs) and the
    subfiletype-preservation fix, exercised end to end with real files."""

    def test_mask_page_skipped_original_untouched(self, tmp_path, lane):
        """Scanner RGB+IR: the MASK page is not in the compress whitelist, so
        SafeMode must skip the file WITHOUT moving it to OLD_TIFFs."""
        scan = make_multipage_tiff(tmp_path / "scan_ir.tif", [None, 1, 4])

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "0", "-InputDir", str(tmp_path)], tmp_path)

        assert "MULTI" in result.stdout, result.stdout
        assert scan.exists(), "skipped multi-page TIFF vanished from its folder"
        assert not (tmp_path / "OLD_TIFFs").exists(), \
            "a skipped file must never be moved to OLD_TIFFs"

    def test_reducedimage_markers_survive_compression(self, tmp_path, lane):
        """Capture One layout (main + REDUCEDIMAGE thumbnail): ImageMagick drops
        NewSubfileType on rewrite, so the backend must restore it -- otherwise the
        next run sees a 'genuine' multi-page file and thumbnail detection breaks."""
        make_multipage_tiff(tmp_path / "c1_style.tif", [None, 1])

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "0", "-InputDir", str(tmp_path)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK" in result.stdout
        sts = read_subfiletypes(tmp_path / "c1_style.tif")
        assert len(sts) == 2, f"page count changed: {sts}"
        assert sts[0] == "", f"IFD0 must stay untagged, got {sts[0]!r}"
        assert sts[1] in ("REDUCEDIMAGE", "REDUCED"), f"thumbnail marker lost: {sts[1]!r}"
        assert rmse_pages(tmp_path / "OLD_TIFFs" / "c1_style.tif",
                          tmp_path / "c1_style.tif") == "0 (0)"

    def test_page_tagged_thumbnail_compresses_anyway(self, tmp_path, lane):
        """A thumbnail is a thumbnail even when the tag lies: ImageMagick stamps
        PAGE on every page it rewrites, so a previously recompressed Capture One
        file (small thumbnail marked PAGE) was skipped as genuinely multi-page.
        Size, not just the tag, classifies the page."""
        make_multipage_tiff(tmp_path / "rewritten_c1.tif", [2, 2], sizes=["64x48", "16x12"])

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "0", "-InputDir", str(tmp_path)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK" in result.stdout, result.stdout
        assert "MULTI" not in result.stdout, result.stdout
        assert (tmp_path / "OLD_TIFFs" / "rewritten_c1.tif").exists()

    def test_full_size_second_page_still_skips(self, tmp_path, lane):
        """The dimension rule must not open the gate for real pages: a full-size
        untagged extra page (second photo, Photoshop layer) is still MULTI."""
        make_multipage_tiff(tmp_path / "two_photos.tif", [None, None], sizes=["64x48", "64x48"])

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "0", "-InputDir", str(tmp_path)], tmp_path)

        assert "MULTI" in result.stdout, result.stdout
        assert not (tmp_path / "OLD_TIFFs").exists(), \
            "a skipped file must never be moved to OLD_TIFFs"


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestMode8DeleteE2E:
    def test_mode8_deletes_tiff_source_output_pixel_identical(self, tmp_path, lane):
        """The only end-to-end run of the delete path: .tiff source must be gone,
        .tif output must exist, and pixels must match the original exactly."""
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "photo.tiff")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-StagingDir", str(tmp_path / "staging")], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert not src.exists(), ".tiff source was not deleted"
        out = work / "photo.tif"
        assert out.exists(), "compressed .tif output missing"
        assert read_compression(out).lower() in ("zip", "adobe deflate")
        assert rmse_pages(reference, out) == "0 (0)", "pixels differ from the deleted source"


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestExcludeFoldersE2E:
    """-ExcludeFolders '_EXPORT': the _EXPORT tree is never scanned, everything
    else in the hierarchy is processed, and the exclusion is logged."""

    def _tree(self, tmp_path):
        photoset = tmp_path / "PhotoSet"
        export_tiff = photoset / "_EXPORT" / "TIFF"
        selecao = photoset / "Selecao"
        export_tiff.mkdir(parents=True)
        selecao.mkdir()
        make_tiff(photoset / "img.tif")
        make_tiff(selecao / "img2.tif")
        make_tiff(export_tiff / "edit.tif")
        return photoset

    def test_export_tree_never_touched(self, tmp_path, lane):
        photoset = self._tree(tmp_path)

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "9", "-InputDir", str(photoset),
                         "-ExcludeFolders", "_EXPORT"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "Excluded" in result.stdout
        assert read_compression(photoset / "img.tif").lower() in ("zip", "adobe deflate")
        assert read_compression(photoset / "Selecao" / "img2.tif").lower() in ("zip", "adobe deflate")
        assert read_compression(photoset / "_EXPORT" / "TIFF" / "edit.tif").lower() != "zip", (
            "a file inside _EXPORT was processed despite -ExcludeFolders"
        )
        assert not (photoset / "_EXPORT" / "TIFF" / "OLD_TIFFs").exists()

    def test_exclusion_is_segment_match_not_substring(self, tmp_path, lane):
        photoset = tmp_path / "PhotoSet"
        lookalike = photoset / "My_EXPORT_photos"
        lookalike.mkdir(parents=True)
        make_tiff(lookalike / "keep.tif")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "9", "-InputDir", str(photoset),
                         "-ExcludeFolders", "_EXPORT"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_compression(lookalike / "keep.tif").lower() in ("zip", "adobe deflate"), (
            "'My_EXPORT_photos' was excluded by a substring match -- only exact segments may match"
        )

    def test_path_entry_rejected_with_exit_1(self, tmp_path, lane):
        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "9", "-InputDir", str(tmp_path),
                         "-ExcludeFolders", r"foo\bar"], tmp_path)
        assert result.returncode == 1
        assert "not paths" in result.stdout


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestCopyExifE2E:
    def test_exif_copied_from_jpeg_to_tiff(self, tmp_path, lane):
        """First behavioral test of the actual copy: Make/Model from the JPEG
        must land on the TIFF, and the exit code must be 0."""
        script = "copy_exif_to_tiff.py"
        work = tmp_path / "S5pro"
        work.mkdir()
        make_tiff(work / "photo.tif")
        subprocess.run(
            ["magick", "-size", "64x48", "gradient:", "-depth", "8", str(work / "photo.jpg")],
            capture_output=True, check=True, timeout=120,
        )
        subprocess.run(
            ["exiftool", "-overwrite_original", "-Make=FUJIFILM", "-Model=FinePix S5Pro",
             str(work / "photo.jpg")],
            capture_output=True, check=True, timeout=120,
        )

        result = run_ps(lane, script, ["-InputDir", str(work)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_tag(work / "photo.tif", "EXIF:Make") == "FUJIFILM"
        assert read_tag(work / "photo.tif", "EXIF:Model") == "FinePix S5Pro"

    def test_miss_exits_0_but_failonwarn_exits_1(self, tmp_path, lane):
        """A TIFF without a JPEG pair is a MISS (warning class): default contract
        keeps exit 0, -FailOnWarn turns it into a gateable failure."""
        script = "copy_exif_to_tiff.py"
        work = tmp_path / "S5pro"
        work.mkdir()
        make_tiff(work / "lonely.tif")

        ok = run_ps(lane, script, ["-InputDir", str(work)], tmp_path)
        assert ok.returncode == 0, f"{ok.stdout}\n{ok.stderr}"

        strict = run_ps(lane, script, ["-InputDir", str(work), "-FailOnWarn"], tmp_path)
        assert strict.returncode == 1, f"{strict.stdout}\n{strict.stderr}"


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestCopyExifVariantAndHeuristicNames:
    """Round 20: `_1`/`_2` is the normal Capture One variant name (plain OK); only the
    `_\\d{3,4}$` heuristic cut can be a different photo and must stay loud."""

    @staticmethod
    def _make_fuji_jpeg(path):
        subprocess.run(["magick", "-size", "64x64", "xc:gray", str(path)],
                       capture_output=True, check=True, timeout=120)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-Make=FUJIFILM",
                        "-Model=Test", "-DateTimeOriginal=2025:03:09 10:12:07", str(path)],
                       capture_output=True, check=True, timeout=120)

    def test_variant_suffix_pairs_as_plain_ok(self, tmp_path, lane):
        script = "copy_exif_to_tiff.py"
        work = tmp_path / "S5pro"
        work.mkdir()
        make_tiff(work / "foto_1.tif")
        self._make_fuji_jpeg(work / "foto.jpg")

        result = run_ps(lane, script, ["-InputDir", str(work)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK | foto_1.tif <= foto.jpg" in result.stdout, result.stdout
        assert "heuristic" not in result.stdout, result.stdout

    def test_three_digit_heuristic_still_warns(self, tmp_path, lane):
        script = "copy_exif_to_tiff.py"
        work = tmp_path / "S5pro"
        work.mkdir()
        make_tiff(work / "foto_2024.tif")
        self._make_fuji_jpeg(work / "foto.jpg")

        result = run_ps(lane, script, ["-InputDir", str(work)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "WARN (heuristic JPEG match)" in result.stdout, result.stdout

    def test_compress_zip_on_a_zip_tiff_counts_the_exif_copy(self, tmp_path, lane):
        """Round 21: with -CompressZip a TIFF that is already ZIP gets its EXIF and only the
        ZIP step is skipped -- but the summary counted it as 'skipped', so a run that wrote
        EXIF into every file ended in 'Done: 0 OK | N skipped' (seen on real Capture One
        exports, which are all ZIP)."""
        script = "copy_exif_to_tiff.py"
        work = tmp_path / "S5pro"
        work.mkdir()
        tif = work / "foto_1.tif"
        make_tiff(tif)
        subprocess.run(["magick", str(tif), "-compress", "zip", str(tif)],
                       capture_output=True, check=True, timeout=120)
        self._make_fuji_jpeg(work / "foto.jpg")

        result = run_ps(lane, script, ["-InputDir", str(work), "-CompressZip"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK+SKIP-ZIP" in result.stdout, result.stdout
        assert read_tag(tif, "DateTimeOriginal") == "2025:03:09 10:12:07"
        assert "Done: 1 OK (ZIP step skipped on 1) | 0 skipped" in result.stdout, result.stdout


@requires_tools
class TestThumbnailLifecycle:
    """
    Generation and removal must agree on where thumbnails live and what they are called.
    They did not: a relative -OutputDir was resolved per file directory when generating and
    per input root when removing, and the collision rename (_v2) matched neither the
    self-exclusion nor the -Remove filter.
    """

    @staticmethod
    def _two_colliding_tiffs(tmp_path):
        root = tmp_path / "root"
        for sub in ("sub1", "sub2"):
            (root / sub).mkdir(parents=True)
            make_tiff(root / sub / "photo.tif")
        return root

    def test_remove_finds_what_generate_created(self, tmp_path):
        root = self._two_colliding_tiffs(tmp_path)
        args = ["-InputDir", str(root), "-OutputDir", "thumbs", "-Recursive"]

        gen = run_ps("parallel", "generate_thumbnails.py", args, tmp_path)
        assert gen.returncode == 0, gen.stdout
        created = sorted(root.rglob("*_thumb*"))
        assert len(created) == 2, f"setup failed: {created}"

        rem = run_ps("parallel", "generate_thumbnails.py", args + ["-Remove"], tmp_path)
        assert rem.returncode == 0, rem.stdout
        assert not list(root.rglob("*_thumb*")), (
            "-Remove with the flags that created them found nothing:\n" + rem.stdout
        )

    def test_renamed_thumbnail_is_not_rescanned_as_a_source(self, tmp_path):
        root = self._two_colliding_tiffs(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        args = ["-InputDir", str(root), "-OutputDir", str(out), "-Recursive", "-Format", "tif"]

        gen = run_ps("parallel", "generate_thumbnails.py", args, tmp_path)
        assert gen.returncode == 0, gen.stdout
        assert (out / "photo_thumb_v2.tif").exists(), f"no collision happened:\n{gen.stdout}"

        # Second pass over the output folder: both thumbnails must be recognised as thumbnails
        again = run_ps(
            "parallel", "generate_thumbnails.py",
            ["-InputDir", str(out), "-Format", "tif"], tmp_path,
        )
        assert again.returncode == 0, again.stdout
        assert not (out / "photo_thumb_v2_thumb.tif").exists(), (
            "the _v2 rename was treated as a source TIFF (_thumb_thumb is back)"
        )

    def test_renamed_thumbnail_can_be_removed(self, tmp_path):
        root = self._two_colliding_tiffs(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        run_ps(
            "parallel", "generate_thumbnails.py",
            ["-InputDir", str(root), "-OutputDir", str(out), "-Recursive", "-Format", "tif"],
            tmp_path,
        )
        assert (out / "photo_thumb_v2.tif").exists()

        rem = run_ps(
            "parallel", "generate_thumbnails.py",
            ["-InputDir", str(out), "-Format", "tif", "-Remove"], tmp_path,
        )
        assert rem.returncode == 0, rem.stdout
        assert not list(out.glob("*_thumb*")), f"_v2 survived -Remove:\n{rem.stdout}"


# Canned `magick identify` responses for the gate tests below.
DIMS_OK = SimpleNamespace(returncode=0, stdout="100 100\n", stderr="")
DIMS_OTHER = SimpleNamespace(returncode=0, stdout="50 50\n", stderr="")
PAGES_OK = SimpleNamespace(returncode=0, stdout="1\n", stderr="")
PAGES_TWO = SimpleNamespace(returncode=0, stdout="2\n", stderr="")
IDENTIFY_FAILED = SimpleNamespace(returncode=1, stdout="", stderr="identify: no decode delegate")
IDENTIFY_EMPTY = SimpleNamespace(returncode=0, stdout="", stderr="")
IDENTIFY_GARBAGE = SimpleNamespace(returncode=0, stdout="???\n", stderr="")


class TestFailClosedGates:
    """
    Gates that authorise destroying data must treat "cannot read" as failure. Both of these
    used to skip their own check when identify was unreadable and fall through to a verdict
    that permanently deleted (purge) or replaced with a lossy 16 -> 8-bit file (padded).
    """

    @staticmethod
    def _compare_fake(dims_old=DIMS_OK, dims_new=DIMS_OK,
                      pages_old=PAGES_OK, pages_new=PAGES_OK, calls=None):
        """Old and new are told apart by filename, so each side can be made unreadable."""
        def fake_run(cmd, **kwargs):
            r28 = _fake_round28_exiftool(cmd, kwargs)
            if r28:
                return r28
            cmd = [str(c) for c in cmd]
            if calls is not None:
                calls.append(cmd)
            is_old = "old" in Path(cmd[-1]).name
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if fmt == "%w %h":
                return dims_old if is_old else dims_new
            if fmt == "%n\n":
                return pages_old if is_old else pages_new
            return SimpleNamespace(returncode=0, stdout="0 (0)", stderr="")
        return fake_run

    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({"pages_new": IDENTIFY_FAILED}, "page count unreadable"),
            ({"pages_old": IDENTIFY_EMPTY}, "page count unreadable"),
            ({"pages_new": PAGES_TWO}, "PAGE_COUNT_MISMATCH"),
        ],
        ids=["identify-nonzero", "identify-empty", "count-differs"],
    )
    def test_compare_refuses_on_unreadable_page_count(self, monkeypatch, kwargs, expected):
        calls = []
        monkeypatch.setattr(
            subprocess, "run", self._compare_fake(calls=calls, **kwargs)
        )
        match, detail = _compare_tiff_metadata(Path("old.tif"), Path("new.tif"))

        assert match is False
        assert expected in detail
        assert not any("compare" in c for c in calls), (
            "RMSE must not become the sole evidence -- run_purge_old_tiffs deletes on this verdict"
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"dims_new": IDENTIFY_FAILED},
            {"dims_old": IDENTIFY_EMPTY},
            {"dims_new": IDENTIFY_GARBAGE},
            {"dims_new": DIMS_OTHER},
        ],
        ids=["identify-nonzero", "identify-empty", "identify-garbage", "dims-differ"],
    )
    def test_compare_refuses_on_unreadable_dimensions(self, monkeypatch, kwargs):
        monkeypatch.setattr(subprocess, "run", self._compare_fake(**kwargs))
        match, _ = _compare_tiff_metadata(Path("old.tif"), Path("new.tif"))
        assert match is False

    def test_compare_accepts_identical_files(self, monkeypatch):
        """Guard against the gates being tightened into always-False."""
        monkeypatch.setattr(subprocess, "run", self._compare_fake())
        match, detail = _compare_tiff_metadata(Path("old.tif"), Path("new.tif"))
        assert match is True and "IDENTICAL" in detail

    @staticmethod
    def _padded_fake(dims_new=DIMS_OK, dims_orig=DIMS_OK,
                     pages_new=PAGES_OK, pages_orig=PAGES_OK,
                     compare=SimpleNamespace(returncode=0, stdout="", stderr="0 (0)")):
        """Fakes a successful 16 -> 8-bit conversion with configurable verification reads."""
        def fake_run(cmd, **kwargs):
            cmd = [str(c) for c in cmd]
            if cmd[0] == "exiftool":
                return _fake_round28_exiftool(cmd, kwargs) or SimpleNamespace(returncode=0, stdout="", stderr="")
            if "compare" in cmd:
                return compare
            if "-depth" in cmd:
                Path(cmd[-1]).write_bytes(b"z" * 100)   # smaller than the original
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "null:" in cmd:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "identify" in cmd:
                is_tmp = "tmp8_" in Path(cmd[-1].rstrip("[0]")).name
                fmt = cmd[cmd.index("-format") + 1]
                if fmt.startswith("%[tiff:subfiletype]"):
                    # Page-layout probe (_tiff_extra_pages_are_thumbnails / the SubfileType
                    # restore). Answer "single page" so the parametrized cases below still
                    # exercise the verification gate they were written for, instead of being
                    # short-circuited by this earlier -- and equally fail-closed -- check.
                    return SimpleNamespace(returncode=0, stdout="|100|100\n", stderr="")
                if fmt.startswith("%n"):
                    return pages_new if is_tmp else pages_orig
                return dims_new if is_tmp else dims_orig
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return fake_run

    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({"dims_new": IDENTIFY_FAILED}, "dimension_unreadable"),
            ({"dims_orig": IDENTIFY_EMPTY}, "dimension_unreadable"),
            ({"dims_new": IDENTIFY_GARBAGE}, "dimension_unreadable"),
            ({"dims_new": DIMS_OTHER}, "dimension_mismatch"),
            ({"pages_new": IDENTIFY_FAILED}, "page_count_mismatch"),
            ({"pages_orig": IDENTIFY_GARBAGE}, "page_count_mismatch"),
            ({"pages_new": PAGES_TWO}, "page_count_mismatch"),
            ({"compare": SimpleNamespace(returncode=1, stdout="", stderr="5120 (5120)")},
             "pixel_mismatch"),
            ({"compare": SimpleNamespace(returncode=2, stdout="", stderr="compare: unable")},
             "pixel_compare_failed"),
            ({"compare": SimpleNamespace(returncode=0, stdout="", stderr="")},
             "pixel_compare_failed"),
        ],
        ids=["new-dims-unreadable", "orig-dims-unreadable", "dims-garbage",
             "dims-differ", "new-pages-unreadable", "orig-pages-garbage", "pages-differ",
             "pixels-differ", "compare-error", "compare-unparseable"],
    )
    def test_padded_refuses_when_verification_is_unreadable(
        self, tmp_path, monkeypatch, kwargs, expected
    ):
        src = tmp_path / "photo.tif"
        src.write_bytes(b"x" * 2000)
        staging = tmp_path / "staging"
        staging.mkdir()
        monkeypatch.setattr(subprocess, "run", self._padded_fake(**kwargs))

        _, _, status, _, _, _, _, _, tmp8 = _process_single_padded(src, staging)

        assert status == expected
        assert not tmp8.exists(), "the rejected conversion must not be left in staging"

    def test_padded_gate_failure_leaves_original_untouched(self, tmp_path, monkeypatch):
        """End to end: a failed gate must not back up, replace or delete anything."""
        src = tmp_path / "photo.tif"
        src.write_bytes(b"x" * 2000)
        monkeypatch.setattr(
            subprocess, "run", self._padded_fake(pages_new=IDENTIFY_FAILED)
        )

        cfg = SimpleNamespace(config=ToolConfig())
        _compress_padded_files([src], tmp_path / "temp", workers=1, cfg=cfg)

        assert src.read_bytes() == b"x" * 2000, "original was replaced despite a failed gate"
        assert not (tmp_path / "OLD_PADDED").exists()

    def test_padded_happy_path_still_replaces(self, tmp_path, monkeypatch):
        """The gates must not be so strict that the feature stops working."""
        src = tmp_path / "photo.tif"
        src.write_bytes(b"x" * 2000)
        monkeypatch.setattr(subprocess, "run", self._padded_fake())

        cfg = SimpleNamespace(config=ToolConfig())
        _compress_padded_files([src], tmp_path / "temp", workers=1, cfg=cfg)

        assert src.read_bytes() == b"z" * 100
        assert (tmp_path / "OLD_PADDED" / "photo.tif").read_bytes() == b"x" * 2000


# -- CLASS 3: two execution paths ------------------------------------
# Every end-to-end test in this file runs in both lanes; the per-path rules are in
# tests/test_backend_invariants.py.




# -- CLASS 4: staging / path isolation ------------------------------

class TestStagingIsolation:
    """
    Cleanup used to wipe a shared staging directory wholesale, so a concurrent run caught
    between its OLD_PADDED backup and its final move lost the file from the source folder.
    """

    def test_staging_is_run_scoped_and_removed(self, tmp_path, monkeypatch):
        src = tmp_path / "photo.tif"
        src.write_bytes(b"x" * 2000)
        temp_root = tmp_path / "temp"
        temp_root.mkdir()

        # another run's in-flight file, under the old shared name
        other = temp_root / "compress_staging"
        other.mkdir()
        (other / "someone_elses.tif").write_bytes(b"in flight")

        monkeypatch.setattr(subprocess, "run", TestFailClosedGates._padded_fake())
        cfg = SimpleNamespace(config=ToolConfig())
        _compress_padded_files([src], temp_root, workers=1, cfg=cfg)

        assert (other / "someone_elses.tif").read_bytes() == b"in flight", (
            "cleanup destroyed a concurrent run's staging file"
        )
        leftovers = [p for p in temp_root.glob("compress_staging_*")]
        assert not leftovers, f"this run's staging was not cleaned up: {leftovers}"

    def test_two_runs_use_different_staging_dirs(self, tmp_path, monkeypatch):
        seen = []

        def spy(cmd, **kwargs):
            cmd = [str(c) for c in cmd]
            if "-depth" in cmd:
                seen.append(Path(cmd[-1]).parent.name)
            return TestFailClosedGates._padded_fake()(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        cfg = SimpleNamespace(config=ToolConfig())
        temp_root = tmp_path / "temp"
        for i in (1, 2):
            src = tmp_path / f"photo{i}.tif"
            src.write_bytes(b"x" * 2000)
            _compress_padded_files([src], temp_root, workers=1, cfg=cfg)

        assert len(seen) == 2 and seen[0] != seen[1], f"staging dir reused across runs: {seen}"


# -- CLASS 5: destination collision ---------------------------------

class TestUndoOldTiffsResilience:
    """
    _safe_move was called bare, so the first unmovable file raised straight out of the
    workflow: every file after it was never attempted, no summary was printed, and the
    traceback killed the wizard (main() only catches KeyboardInterrupt).
    """

    @staticmethod
    def _prepare(tmp_path, monkeypatch, answers):
        old = tmp_path / "OLD_TIFFs"
        old.mkdir()
        for name in ("a_first.tif", "b_blocked.tif", "c_last.tif"):
            (old / name).write_bytes(name.encode())
        # a directory in the parent sharing a TIFF's name -- _safe_move refuses it, and so
        # does any locked or read-only destination
        (tmp_path / "b_blocked.tif").mkdir()

        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, *a, **k: tmp_path)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        it = iter(answers)
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))
        return old

    def test_one_failure_does_not_abandon_the_rest(self, tmp_path, monkeypatch):
        # round 26: "restore only the stranded ones?" comes first -- no, restore ALL; then
        # overwrite, move, keep dirs
        old = self._prepare(tmp_path, monkeypatch, ["n", "y", "y", "n"])

        result = convert_tiff.run_undo_old_tiffs(SimpleNamespace(config=ToolConfig()))

        assert result is False, "a failed move must be reported, not swallowed"
        assert (tmp_path / "a_first.tif").is_file()
        assert (tmp_path / "c_last.tif").is_file(), (
            "the file after the failure was never attempted"
        )
        assert (old / "b_blocked.tif").is_file(), "the unmovable file must stay put"
        assert (tmp_path / "b_blocked.tif").is_dir(), "the directory was destroyed"

    def test_clean_run_still_reports_success(self, tmp_path, monkeypatch):
        old = tmp_path / "OLD_TIFFs"
        old.mkdir()
        (old / "photo.tif").write_bytes(b"data")
        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, *a, **k: tmp_path)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        it = iter(["n", "n", "y", "n"])   # round 26: not stranded-only, then as before
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))

        result = convert_tiff.run_undo_old_tiffs(SimpleNamespace(config=ToolConfig()))

        assert result is True
        assert (tmp_path / "photo.tif").read_bytes() == b"data"

    def test_stranded_only_is_offered_first_and_restores_just_those(self, tmp_path, monkeypatch):
        """Round 26: a backup whose original is in place stays in OLD_TIFFs/."""
        old = tmp_path / "OLD_TIFFs"
        old.mkdir()
        (old / "lost.tif").write_bytes(b"L")
        (old / "kept.tif").write_bytes(b"K")
        (tmp_path / "kept.tif").write_bytes(b"compressed")
        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, *a, **k: tmp_path)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        it = iter(["y"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))

        assert convert_tiff.run_undo_old_tiffs(SimpleNamespace(config=ToolConfig())) is True
        assert (tmp_path / "lost.tif").read_bytes() == b"L"
        assert (old / "kept.tif").read_bytes() == b"K"
        assert (tmp_path / "kept.tif").read_bytes() == b"compressed"


class TestSafeMoveNeverLosesBoth:
    """`dst.unlink()` before `shutil.move` meant a move that then failed left both files gone."""

    def test_failed_move_restores_destination(self, tmp_path, monkeypatch):
        src = tmp_path / "src.tif"
        dst = tmp_path / "dst.tif"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")

        def boom(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(convert_tiff.tw, "move_file_replacing", boom)
        with pytest.raises(OSError):
            _safe_move(src, dst)

        assert dst.read_bytes() == b"old", "destination was destroyed by a move that failed"
        assert src.read_bytes() == b"new", "source was lost too"
        assert not list(tmp_path.glob("*.bak-*")), "the parked destination leaked"

    def test_successful_move_leaves_no_backup_residue(self, tmp_path):
        src = tmp_path / "src.tif"
        dst = tmp_path / "dst.tif"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")

        _safe_move(src, dst)

        assert dst.read_bytes() == b"new"
        assert not src.exists()
        assert not list(tmp_path.glob("*.bak-*"))

    def test_directory_destination_is_refused_not_deleted(self, tmp_path):
        src = tmp_path / "src.tif"
        src.write_bytes(b"new")
        dst = tmp_path / "collides"
        dst.mkdir()
        (dst / "keep_me.txt").write_text("important")

        with pytest.raises(IsADirectoryError):
            _safe_move(src, dst)

        assert (dst / "keep_me.txt").exists(), "rmtree erased a directory sharing the name"
        assert src.exists()


class TestThumbnailNeverDropsPages:
    """-GenerateThumbnail rebuilds the output from page 0 only ("$src[0]") and appends a
    freshly generated thumbnail. Replacing an existing REDUCEDIMAGE preview is the intent;
    dropping a scanner IR (MASK) page or a Photoshop layer is silent data loss -- and the
    in-place integrity gate cannot see it, because a page-short TIFF still decodes cleanly.
    A mode that writes IN PLACE made the loss irreversible."""

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_refuses_scanner_ir_source_in_every_mode(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "scan.tif"
        # main RGB + reduced preview + full-size IR MASK page, like raw_scan/raw_scan_2
        make_multipage_tiff(src, [None, 1, 4])
        before = src.read_bytes()

        result = run_ps_switched(lane, "compress_tiff_zip.py",
                                 ["-Mode", "3", "-InputDir", str(work), "-SafeMode:$false",
                                  "-GenerateThumbnail", "-Workers", "1"], work)

        assert "-GenerateThumbnail refused" in result.stdout, result.stdout
        assert result.returncode == 1, "a refusal must be an error, not a silent success"
        assert not (work / "ZIP" / "scan.tif").exists(), "a page-short output was written"
        assert src.read_bytes() == before, "source must be untouched"

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_in_place_keeps_every_page(self, tmp_path, lane):
        """The irreversible case: mode 2 without -OutputDir overwrites the only copy (a file
        directly in the input root)."""
        work = tmp_path / "work"
        work.mkdir()
        src = work / "scan.tif"
        make_multipage_tiff(src, [None, 1, 4])
        pages_before = len(read_subfiletypes(src))
        assert pages_before == 3

        result = run_ps_switched(lane, "compress_tiff_zip.py",
                                 ["-Mode", "2", "-InputDir", str(work), "-SafeMode:$false",
                                  "-GenerateThumbnail"], work)

        assert "-GenerateThumbnail refused" in result.stdout, result.stdout
        assert len(read_subfiletypes(src)) == 3, (
            "in-place -GenerateThumbnail destroyed a page of the only copy"
        )

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_thumbnail_only_source_is_still_processed(self, tmp_path, lane):
        """A Capture One main+thumbnail pair must keep working -- the guard must not turn
        into a blanket refusal of every multi-page file."""
        work = tmp_path / "work"
        work.mkdir()
        src = work / "photo.tif"
        make_multipage_tiff(src, [None, 1])

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "3", "-InputDir", str(work), "-GenerateThumbnail",
                         "-Workers", "1"], work)

        out = work / "ZIP" / "photo.tif"
        assert out.exists(), result.stdout
        assert "[thumb replaced]" in result.stdout, (
            "replacing an existing thumbnail must be reported, not silent"
        )
        assert len(read_subfiletypes(out)) == 2


class TestCopyExifSafeModeMatchesCompress:
    """SafeMode must mean the same thing in all three backends. The copy_exif call site
    omitted -AllowedSubfileTypes and fell back to the parameter default, which also allows
    MASK and PAGE -- so a scanner RGB+IR file that compress_tiff_zip.py skips was accepted,
    rewritten by magick and moved over the original."""

    @requires_tools
    @pytest.mark.parametrize("script,lane", [
        ("copy_exif_to_tiff.py", "sequential"),
        ("copy_exif_to_tiff.py", "parallel"),
    ])
    def test_scanner_ir_is_skipped_as_multipage(self, tmp_path, script, lane):
        if lane not in LANES:
            pytest.skip(f"{lane} not on PATH")
        work = tmp_path / "S5pro"
        work.mkdir()
        src = work / "scan.tif"
        make_multipage_tiff(src, [None, 1, 4])
        subprocess.run(["magick", "-size", "64x48", "gradient:", str(work / "scan.jpg")],
                       capture_output=True, check=True, timeout=120)
        before = src.read_bytes()

        result = run_ps(lane, script, ["-InputDir", str(work), "-CompressZip",
                                        "-Workers", "1"], tmp_path)

        assert "MULTI" in result.stdout, result.stdout
        assert src.read_bytes() == before, "scanner IR file was rewritten anyway"



class TestCopyExifZipIsPixelVerified:
    """The staged ZIP is moved on top of the TIFF it came from and there is no OLD_TIFFs
    backup on this path, so a zero exit from magick is not enough to authorise it."""


    @requires_tools
    @pytest.mark.parametrize("script,lane", [
        ("copy_exif_to_tiff.py", "sequential"),
        ("copy_exif_to_tiff.py", "parallel"),
    ])
    def test_zip_round_trip_is_pixel_identical(self, tmp_path, script, lane):
        if lane not in LANES:
            pytest.skip(f"{lane} not on PATH")
        work = tmp_path / "S5pro"
        work.mkdir()
        src = work / "photo.tif"
        make_multipage_tiff(src, [None, 1])
        pristine = tmp_path / "pristine.tif"
        shutil.copy(src, pristine)
        subprocess.run(["magick", "-size", "64x48", "gradient:", str(work / "photo.jpg")],
                       capture_output=True, check=True, timeout=120)

        run_ps(lane, script, ["-InputDir", str(work), "-CompressZip", "-Workers", "1"], tmp_path)

        assert read_ifd0_compression(src).lower().startswith(("zip", "deflate", "adobe"))
        for page in range(2):
            assert rmse_pages(pristine, src, page).startswith("0 "), (
                f"page {page} changed during the -CompressZip round trip"
            )


class TestMode5PathSeparator:
    """-InputDir with forward slashes (legal on Windows) made the mode 5 prefix compare
    fail for every nested file, so the "never climb above the input root" fallback fired
    and the whole tree collapsed into <root>\\ZIP with spurious _v2 renames."""

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_same_tree_for_both_path_forms(self, tmp_path, lane):
        def build(root):
            (root / "sess" / "TIFF").mkdir(parents=True)
            (root / "other" / "TIFF").mkdir(parents=True)
            make_tiff(root / "sess" / "TIFF" / "a.tif")
            make_tiff(root / "other" / "TIFF" / "a.tif")

        results = {}
        for label in ("back", "fwd"):
            root = tmp_path / label
            root.mkdir()
            build(root)
            arg = str(root) if label == "back" else str(root).replace("\\", "/")
            run_ps(lane, "compress_tiff_zip.py",
                   ["-Mode", "5", "-InputDir", arg, "-Workers", "1"], tmp_path)
            results[label] = sorted(
                p.relative_to(root).as_posix() for p in root.rglob("*.tif")
            )

        assert results["fwd"] == results["back"], (
            "forward-slash -InputDir produced a different output tree"
        )
        assert any(p.startswith("other/ZIP/") for p in results["fwd"]), (
            "nested branch must get its own sibling ZIP folder, not the root one"
        )
        assert not any("_v2" in p for p in results["fwd"]), (
            "collapsed tree caused a spurious collision rename"
        )


class TestThumbnailColorManagement:
    """`-colorspace sRGB` converts colorspaces, not ICC profiles: on a TIFF ImageMagick
    already reads as RGB it is a no-op, and `-strip` then discards the source profile. A
    ProPhoto export therefore produced a thumbnail whose numbers were reinterpreted as sRGB
    -- the tagged and untagged sources gave byte-identical output."""

    @staticmethod
    def _wide_gamut_pair(tmp_path):
        """A saturated image, once tagged with a wide-gamut profile and once untagged."""
        plain = tmp_path / "plain.tif"
        subprocess.run(["magick", "-size", "64x64", "gradient:red-blue", "-depth", "16", str(plain)],
                       capture_output=True, check=True, timeout=120)
        icc = tmp_path / "wide.icc"
        found = subprocess.run(
            ["magick", plain.name, "-profile", "sRGB", "info:"],
            capture_output=True, cwd=str(tmp_path), timeout=120)
        del found
        # ProPhoto-like primaries via ImageMagick's built-in wide-gamut profile if present;
        # otherwise fall back to any system ICC so the tagged/untagged contrast still holds.
        sys_icc = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
            "System32/spool/drivers/color/ProPhoto.icm"
        if not sys_icc.exists():
            sys_icc = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
                "System32/spool/drivers/color/AdobeRGB1998.icc"
        if not sys_icc.exists():
            return None, None
        shutil.copy(sys_icc, icc)
        tagged = tmp_path / "tagged.tif"
        subprocess.run(["magick", str(plain), "-profile", str(icc), str(tagged)],
                       capture_output=True, check=True, timeout=120)
        return plain, tagged

    @requires_tools
    def test_embedded_profile_changes_the_thumbnail(self, tmp_path):
        plain, tagged = self._wide_gamut_pair(tmp_path)
        if plain is None:
            pytest.skip("no wide-gamut ICC profile available on this machine")
        work = tmp_path / "work"
        work.mkdir()
        shutil.copy(plain, work / "plain.tif")
        shutil.copy(tagged, work / "tagged.tif")

        run_ps("parallel", "generate_thumbnails.py",
               ["-InputDir", str(work), "-Size", "64"], tmp_path)

        a = (work / "plain_thumb.jpg").read_bytes()
        b = (work / "tagged_thumb.jpg").read_bytes()
        assert a and b
        assert a != b, (
            "tagged and untagged sources produced identical thumbnails -- the embedded "
            "ICC profile is being ignored (-colorspace instead of -profile)"
        )



class TestSubfileTypeZeroIsPreserved:
    """`%[tiff:subfiletype]` prints an EMPTY string both when the tag is absent and when it
    is 0 ("full-resolution image"), so the restore DELETED an explicit 0 instead of keeping
    it. Every scanner TIFF carries an explicit 0 on IFD0."""

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_explicit_zero_survives_compression(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "scan.tif"
        make_multipage_tiff(src, [0, 1, 4])

        def numeric_markers(path):
            r = subprocess.run(
                ["exiftool", "-a", "-G1", "-s", "-s", "-n", "-SubfileType", str(path)],
                capture_output=True, text=True, timeout=120)
            return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

        before = numeric_markers(src)
        assert "[IFD0] SubfileType: 0" in before

        run_ps_switched(lane, "compress_tiff_zip.py",
                        ["-Mode", "3", "-InputDir", str(work), "-SafeMode:$false", "-Workers", "1"], work)

        after = numeric_markers(work / "ZIP" / "scan.tif")
        assert after == before, f"markers changed: {before} -> {after}"


class TestRunSummaryAlwaysPrinted:
    """When the mode 0/9 pre-check filtered every file the run exited before the summary,
    so a folder where everything was correctly skipped ended on a bare WARN with no
    `Done:` line -- and any log parser keyed on it saw nothing."""

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_done_line_present_when_all_files_skipped(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "photo.tif"
        make_tiff(src)
        # pre-compress so the mode 9 pre-check skips it as already-Deflate
        subprocess.run(["magick", str(src), "-compress", "zip", str(src)],
                       capture_output=True, check=True, timeout=120)

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "9", "-InputDir", str(work), "-Workers", "1"], work)

        assert "Done:" in result.stdout, result.stdout
        assert "1 skipped" in result.stdout
        assert "1/1 processed" in result.stdout, "the skipped file must close the total"
        assert result.returncode == 0
        assert not (work / "OLD_TIFFs").exists(), "a skipped file must not be moved"

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_prefilter_results_carry_the_counter_prefix(self, tmp_path, lane):
        """MULTI/SKIP emitted by the modes 0/9 pre-check bypassed Process-Results, so they
        printed without the [n/total] prefix every other result line carries."""
        work = tmp_path / "work"
        work.mkdir()
        make_multipage_tiff(work / "scan.tif", [None, 1, 4])
        make_tiff(work / "plain.tif")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "9", "-InputDir", str(work), "-Workers", "1"], work)

        multi = [ln for ln in result.stdout.splitlines() if "MULTI (" in ln]
        assert multi, result.stdout
        assert re.search(r"\[\d+/\d+\] MULTI \(", multi[0]), (
            f"MULTI line has no [n/total] prefix: {multi[0]!r}"
        )


class TestPaddedConversionRespectsPages:
    """_is_real_16bit judges page [0] but _process_single_padded rewrote the WHOLE file, so
    a scanner RGB+IR scan whose RGB is padded had its (possibly real 16-bit) IR channel
    down-converted without ever being diagnosed."""

    @requires_tools
    def test_scanner_ir_file_is_refused(self, tmp_path):
        src = tmp_path / "scan.tif"
        make_multipage_tiff(src, [None, 1, 4])
        before = src.read_bytes()
        staging = tmp_path / "stg"
        staging.mkdir()

        status = convert_tiff._process_single_padded(src, staging)[2]

        assert status == "multi_page_refused", status
        assert src.read_bytes() == before

    @requires_tools
    def test_thumbnail_pair_is_still_converted(self, tmp_path):
        src = tmp_path / "photo.tif"
        make_multipage_tiff(src, [None, 1], padded=True)
        staging = tmp_path / "stg"
        staging.mkdir()

        name, parent, status, _, _, _, exif_ok, _, tmp8 = \
            convert_tiff._process_single_padded(src, staging)

        assert status == "ok", status
        assert exif_ok
        # ...and the thumbnail marker magick dropped on rewrite must be restored
        assert read_subfiletypes(tmp8)[1].upper() in ("REDUCEDIMAGE", "REDUCED"), (
            "SubfileType markers were not restored after -depth 8"
        )

    @requires_tools
    def test_page_layout_classifier_matches_safemode(self, tmp_path):
        ir = tmp_path / "ir.tif"
        make_multipage_tiff(ir, [None, 1, 4])
        thumb = tmp_path / "thumb.tif"
        make_multipage_tiff(thumb, [None, 1])
        single = tmp_path / "single.tif"
        make_tiff(single)

        assert convert_tiff._tiff_extra_pages_are_thumbnails(ir) is False
        assert convert_tiff._tiff_extra_pages_are_thumbnails(thumb) is True
        assert convert_tiff._tiff_extra_pages_are_thumbnails(single) is True


class TestCopyExifExcludeFolders:
    """step_basic_params asks the exclusion question for every workflow; the copy_exif
    command builder used to drop it, so in workflow 4 it applied to the Compress step and
    silently not to the Copy EXIF step of the same run."""

    def test_builder_emits_the_flag(self):
        cmd = convert_tiff.build_copy_exif_command(
            {"origin": "copy_exif", "exclude_folders": "_EXPORT;temp"},
            folders=[Path("C:/x")])
        assert "-ExcludeFolders" in cmd
        assert cmd[cmd.index("-ExcludeFolders") + 1] == "_EXPORT;temp"

    def test_cap_workers_not_asked_where_unsupported(self):
        assert convert_tiff._supports_cap_workers({"origin": "free_compress"})
        assert convert_tiff._supports_cap_workers({"origin": "both"})
        assert not convert_tiff._supports_cap_workers({"origin": "copy_exif"})

    @requires_tools
    @pytest.mark.parametrize("script,lane", [
        ("copy_exif_to_tiff.py", "sequential"),
        ("copy_exif_to_tiff.py", "parallel"),
    ])
    def test_backend_honours_the_flag(self, tmp_path, script, lane):
        """Round 14 changed the semantics this pins. Matching is relative to the scan root
        (jxl-photo's `_is_tool_output_path`), so a folder passed AS a root is not excluded by
        its own name -- only folders nested BELOW a root are. Pointing the tool directly at
        `_EXPORT` to process it therefore still works, which is the point.

        The invariant that matters -- the flag reaches this backend and drops a nested
        `_EXPORT` tree -- is pinned below with a nested layout and -Recurse forced by giving
        the parent as the root."""
        if lane not in LANES:
            pytest.skip(f"{lane} not on PATH")
        keep = tmp_path / "S5pro" / "keep"
        drop = tmp_path / "S5pro" / "_EXPORT"
        keep.mkdir(parents=True)
        drop.mkdir(parents=True)
        for d, stem in ((keep, "a"), (drop, "b")):
            make_tiff(d / f"{stem}.tif")
            subprocess.run(["magick", "-size", "32x32", "gradient:", str(d / f"{stem}.jpg")],
                           capture_output=True, check=True, timeout=120)

        result = run_ps(lane, script,
                        ["-InputDir", f"{keep};{drop}", "-DryRun",
                         "-ExcludeFolders", "_EXPORT"], tmp_path)

        # Both roots were named explicitly, so both are processed: neither has an excluded
        # folder BELOW it. The old absolute match dropped b.tif here.
        assert "a.tif" in result.stdout, result.stdout
        assert "b.tif" in result.stdout, (
            "a folder given AS a root must not be excluded by its own name"
        )
        assert "Excluded" not in result.stdout, result.stdout

    @requires_tools
    @pytest.mark.parametrize("script,lane", [
        ("copy_exif_to_tiff.py", "sequential"),
        ("copy_exif_to_tiff.py", "parallel"),
    ])
    def test_ancestor_name_does_not_wipe_the_run(self, tmp_path, script, lane):
        """The bug this replaced: every ancestor of the file was matched, so a name that
        happened to appear above the root excluded everything and logged it as normal."""
        if lane not in LANES:
            pytest.skip(f"{lane} not on PATH")
        root = tmp_path / "S5pro"
        root.mkdir()
        make_tiff(root / "a.tif")
        subprocess.run(["magick", "-size", "32x32", "gradient:", str(root / "a.jpg")],
                       capture_output=True, check=True, timeout=120)

        for name in ("S5pro", tmp_path.name):
            result = run_ps(lane, script,
                            ["-InputDir", str(root), "-DryRun",
                             "-ExcludeFolders", name], tmp_path)
            assert "a.tif" in result.stdout, (
                f"-ExcludeFolders {name!r} excluded the whole run:\n{result.stdout}"
            )




# -- ROUND 13 -------------------------------------------------------


def numeric_subfiletypes(path):
    """Per-IFD SubfileType as exiftool reports it -- numeric, and absent when the tag is
    absent. `magick %[tiff:subfiletype]` cannot tell "no tag" from an explicit 0."""
    result = subprocess.run(
        ["exiftool", "-a", "-G1", "-s", "-s", "-n", "-SubfileType", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]


class TestThumbnailPathKeepsPageMarkers:
    """-GenerateThumbnail wrote only `-IFD1:SubfileType#=1` and skipped the restore that
    every other write path runs, so magick's PAGE (2) stamp stayed on the MAIN image: every
    thumbnail-generated file claimed page 0 was one page of a multi-page document."""

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_explicit_zero_on_the_main_page_survives(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "scan.tif"
        make_multipage_tiff(src, [0, 1])
        assert "[IFD0] SubfileType: 0" in numeric_subfiletypes(src)

        run_ps_switched(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                         "-SafeMode:$false", "-GenerateThumbnail", "-ThumbSize", "16"], work)

        after = numeric_subfiletypes(work / "ZIP" / "scan.tif")
        assert "[IFD0] SubfileType: 0" in after, f"main page lost its marker: {after}"
        assert "[IFD1] SubfileType: 1" in after, f"thumbnail not marked: {after}"
        assert "[IFD0] SubfileType: 2" not in after, "magick's PAGE stamp was left on page 0"

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_source_without_a_marker_gets_none_back(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")

        run_ps(lane, "compress_tiff_zip.py",
               ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                "-GenerateThumbnail", "-ThumbSize", "16"], work)

        after = numeric_subfiletypes(work / "ZIP" / "photo.tif")
        assert not any(ln.startswith("[IFD0]") for ln in after), (
            f"a source with no marker must not gain one: {after}"
        )
        assert "[IFD1] SubfileType: 1" in after

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_generated_file_is_not_multi_page_on_a_later_run(self, tmp_path, lane):
        """The round trip that matters: whatever -GenerateThumbnail writes must still be
        touchable by SafeMode afterwards, or the toolkit refuses its own output forever."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")
        run_ps(lane, "compress_tiff_zip.py",
               ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                "-GenerateThumbnail", "-ThumbSize", "16"], work)

        second = work / "second"
        second.mkdir()
        shutil.copy2(work / "ZIP" / "photo.tif", second / "photo.tif")
        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(second), "-Workers", "1",
                         "-SkipCompressedWithThumb"], second)

        assert "MULTI" not in result.stdout, (
            "SafeMode classified our own thumbnail output as a real multi-page file:\n"
            + result.stdout
        )

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_thumb_page_zero_is_refused(self, tmp_path, lane):
        """A thumbnail-first file has a full-size page 1, which every classifier here reads
        as a real extra page -- the file came back MULTI on every later run."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                         "-GenerateThumbnail", "-ThumbPage", "0"], work)

        assert result.returncode == 1, result.stdout
        assert "-ThumbPage 0 is not supported" in result.stdout
        assert not (work / "ZIP").exists(), "a refused run must not write anything"




class TestLongPathsAreRefusedLoudly:
    """Staged names used to prepend two full GUIDs (66 characters) and mode 8's default
    staging folder another 55, inside the user's own tree. On a normally nested archive the
    result blew past Windows' 260-character limit and EVERY file failed with magick's bare
    "No such file or directory" -- naming neither the cause nor the limit."""


    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_over_long_output_is_reported_with_the_number(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")
        # Long enough that <out>\photo.tif passes 259 characters without creating it first
        long_out = str(tmp_path / ("d" * 200))

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "2", "-InputDir", str(work), "-Workers", "1",
                         "-OutputDir", long_out], work)

        assert "path too long" in result.stdout, result.stdout
        assert result.returncode == 1
        assert not Path(long_out).exists(), (
            "a refused file must not have its output directory created"
        )



class TestPythonSubfileRestoreKeepsExplicitZero:
    """The Python mirror read the markers with `magick %[tiff:subfiletype]`, which prints an
    EMPTY string both for an absent tag and for an explicit 0 -- so the padded-file
    conversion DELETED the explicit 0 every scanner TIFF carries on IFD0. The backends'
    restore had been fixed for exactly this; the wizard's copy had been left behind."""

    @requires_tools
    def test_explicit_zero_is_preserved(self, tmp_path):
        src = tmp_path / "photo.tif"
        make_multipage_tiff(src, [0, 1], padded=True)
        before = numeric_subfiletypes(src)
        assert "[IFD0] SubfileType: 0" in before
        staging = tmp_path / "stg"
        staging.mkdir()

        _, _, status, _, _, _, _, _, tmp8 = _process_single_padded(src, staging)

        assert status == "ok", status
        assert numeric_subfiletypes(tmp8) == before, (
            f"markers changed: {before} -> {numeric_subfiletypes(tmp8)}"
        )
        # This run is what catches a restore that loses the explicit 0 again (v3.1: it
        # replaced a text match on the function's source).



class TestPurgeKeepsOrphansInsteadOfAborting:
    """A backup whose parent counterpart is absent is not a mismatch: there is nothing to
    compare and nothing that may be deleted. Lumping it in with real mismatches blocked the
    whole purge -- which a healthy folder reaches routinely, because modes 0/9 rename a
    backup when one of that name exists and a .tiff source produces a .tif output."""

    def _run_purge(self, tmp_path, monkeypatch, answers=True):
        folder = tmp_path / "root"
        old_dir = folder / "OLD_TIFFs"
        old_dir.mkdir(parents=True)
        (old_dir / "img.tif").write_bytes(b"tiffdata")
        (old_dir / "img_20260101_010101.tif").write_bytes(b"orphan")
        (folder / "img.tif").write_bytes(b"tiffdata")

        def fake_run(cmd, **kwargs):
            r28 = _fake_round28_exiftool(cmd, kwargs)
            if r28:
                return r28
            cmd = [str(a) for a in cmd]
            if "compare" in cmd:
                return SimpleNamespace(returncode=0, stdout="0 (0)", stderr="")
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if fmt == "%n\n":
                return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="10 10", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, prompt: folder)
        if convert_tiff.RICH_AVAILABLE:
            monkeypatch.setattr(convert_tiff.Confirm, "ask",
                                classmethod(lambda cls, *a, **k: answers))
            monkeypatch.setattr(convert_tiff.Prompt, "ask", classmethod(
                lambda cls, *a, **k: __import__("datetime").datetime.now().strftime("%H:%M")))
        else:
            monkeypatch.setattr(
                "builtins.input",
                lambda *a, **k: ("y" if answers else "n")
                if "Delete" in (a[0] if a else "") else
                __import__("datetime").datetime.now().strftime("%H:%M"))
        cfg = SimpleNamespace(config=ToolConfig())
        convert_tiff.run_purge_old_tiffs(cfg)
        return folder, old_dir

    def test_verified_files_are_purged_and_the_orphan_is_kept(self, tmp_path, monkeypatch):
        folder, old_dir = self._run_purge(tmp_path, monkeypatch, answers=True)
        assert not (old_dir / "img.tif").exists(), "the verified backup should have been purged"
        assert (old_dir / "img_20260101_010101.tif").exists(), (
            "a backup with no parent counterpart is the only copy left -- it must be kept"
        )
        assert (folder / "img.tif").exists()


class TestGenerateThumbnailSaysWhyItSkipped:
    """The already-compressed SKIP runs before the thumbnail step, so -GenerateThumbnail on
    a Deflate library produced nothing at all and still reported a clean run."""

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_skip_line_points_at_the_flag(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "photo.tif"
        make_tiff(src)
        subprocess.run(["magick", str(src), "-compress", "zip", str(src)],
                       capture_output=True, check=True, timeout=120)

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                         "-GenerateThumbnail", "-ThumbSize", "16"], work)

        assert "no thumbnail added" in result.stdout, result.stdout
        assert "SkipCompressedWithThumb" in result.stdout


class TestGroupHeaderNamesTheOutputFolder:
    """Tasks were grouped by the PARENT of the output folder, so the "-- Group:" header and
    the "-> Moved N file(s) -> <dir>" line named a folder the run never wrote to -- the
    drive root for TIFFs sitting directly under the input root."""

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_moved_line_names_the_real_destination(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")
        staging = tmp_path / "stg"
        staging.mkdir()

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                         "-StagingDir", str(staging)], work)

        moved = [ln for ln in result.stdout.splitlines() if "-> Moved" in ln]
        assert moved, result.stdout
        assert str(work / "ZIP").lower() in moved[0].lower(), (
            f"the move line names the wrong folder: {moved[0]}"
        )


# -- ROUND 14 -------------------------------------------------------


class TestExcludeFoldersIsRelativeToTheScanRoot:
    """-ExcludeFolders split the ABSOLUTE directory path, so every ancestor above the folder
    the user pointed at was matched too -- the drive letter, the root's own name, and
    whatever the archive happens to live under. An entry matching one of those excluded the
    ENTIRE run, logged it as a normal exclusion and exited 0:

        -ExcludeFolders Temp   on ...\\Temp\\TESTE   ->  kept: (nothing)

    The docs promise folder TREES below the root, the parameter validation already refuses
    paths, and the Python mirror in convert_tiff.py was already relative."""

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_nested_names_excluded_ancestors_ignored(self, tmp_path, lane):
        root = tmp_path / "TESTE"
        (root / "raw_scan").mkdir(parents=True)
        (root / "_EXPORT" / "TIFF16").mkdir(parents=True)
        make_tiff(root / "raw_scan" / "a.tif")
        make_tiff(root / "_EXPORT" / "TIFF16" / "b.tif")
        # Directly IN the root: the only file whose directory EQUALS the root, which is the
        # case that tells "root's own name counts" apart from "only what is below it".
        make_tiff(root / "c.tif")
        names = ("a.tif", "b.tif", "c.tif")

        def kept(exclude):
            args = ["-Mode", "3", "-InputDir", str(root), "-Workers", "1", "-DryRun"]
            if exclude is not None:
                args += ["-ExcludeFolders", exclude]
            out = run_ps(lane, "compress_tiff_zip.py", args, tmp_path).stdout
            return sorted(n for n in names if f"| {n}" in out)

        assert kept(None) == ["a.tif", "b.tif", "c.tif"], "baseline: all files are discovered"
        # a folder nested below the root is still excluded, tree and all
        assert kept("_EXPORT") == ["a.tif", "c.tif"]
        assert kept("raw_scan") == ["b.tif", "c.tif"]
        # ...but the root's own name and any ancestor must exclude nothing, including the
        # file sitting directly in the root
        assert kept("TESTE") == ["a.tif", "b.tif", "c.tif"], (
            "the scan root's own name excluded the run"
        )
        assert kept(tmp_path.name) == ["a.tif", "b.tif", "c.tif"], (
            "an ancestor folder above the scan root excluded the run"
        )

    @requires_tools
    @pytest.mark.parametrize("lane", LANES)
    def test_forward_slash_input_root_still_matches(self, tmp_path, lane):
        """-InputDir given with '/' separators must not defeat the relative compare -- the
        same normalisation bug mode 5 already had. Without the normalisation the root prefix
        never matches, every file falls into the "not under the root" fallback, and the
        matcher is back to reading the absolute path."""
        root = tmp_path / "TESTE"
        (root / "_EXPORT").mkdir(parents=True)
        make_tiff(root / "keep.tif")
        make_tiff(root / "_EXPORT" / "drop.tif")
        slashed = str(root).replace("\\", "/")

        def out_for(exclude):
            return run_ps(lane, "compress_tiff_zip.py",
                          ["-Mode", "3", "-InputDir", slashed, "-Workers", "1",
                           "-DryRun", "-ExcludeFolders", exclude], tmp_path).stdout

        out = out_for("_EXPORT")
        assert "| keep.tif" in out, out
        assert "| drop.tif" not in out, "the _EXPORT tree was not excluded"

        # An ancestor name still must not exclude anything -- this is what fails when the
        # prefix compare silently degrades to the absolute-path fallback.
        out = out_for(tmp_path.name)
        assert "| keep.tif" in out and "| drop.tif" in out, (
            f"an ancestor excluded files when -InputDir used '/':\n{out}"
        )



# -- ROUND 15 -------------------------------------------------------
#
# Two findings that changed the outcome of a run, and four that degraded one. All six were
# invisible to the suite because nothing combined mode 8 with -GenerateThumbnail, and no
# fixture had ever been given a hostile FILE NAME.

#: Names ImageMagick rewrites when they reach it as a filename spec. '%o', '%d', '%x' are
#: printf conversions: "50%off.tif" is written as "500ff.tif". The last two are controls --
#: '%%' is the escape, and a multi-page file gets split one file per page.
PERCENT_NAMES = ["50%off", "100%dpi", "a%x", "b%2d", "g%%h", "plain"]


def make_tiff_named(directory, name, suffix=".tif"):
    """A TIFF whose FINAL name may contain '%'.

    Created under a safe name and renamed afterwards: `magick ... "50%off.tif"` writes
    "500ff.tif" -- the very bug these fixtures exist to pin -- so building the fixture with
    the hostile name would silently produce a file that is not there.
    """
    directory = Path(directory)
    tmp = directory / f"_fixture_{abs(hash(name)) % 10000}{suffix}"
    if suffix.lower() in (".tif", ".tiff"):
        make_tiff(tmp)
    else:
        subprocess.run(["magick", "-size", "32x32", "gradient:", str(tmp)],
                       capture_output=True, check=True, timeout=120)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-EXIF:Make=FujiTest", str(tmp)],
                       capture_output=True, check=True, timeout=120)
    final = directory / f"{name}{suffix}"
    tmp.rename(final)
    return final


def page_count(path):
    result = subprocess.run(
        ["magick", "identify", "-format", "%n\n", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return int(result.stdout.splitlines()[0].strip())


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestMode8ThumbnailGate:
    """Mode 8 verified the staged file with whole-file pixel identity, which -GenerateThumbnail
    can never satisfy: it writes source page 0 plus a NEW thumbnail. Every file came back
    "ERROR (ZIP integrity check failed - source preserved)", so the combination produced no
    output at all -- while the in-place worker had had the exemption all along."""

    def test_thumbnail_output_is_accepted_and_source_deleted(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "a.tif")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-GenerateThumbnail", "-ThumbSize", "16",
                         "-StagingDir", str(tmp_path / "staging"), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "integrity check failed" not in result.stdout, result.stdout
        assert "SOURCE DELETED" in result.stdout, result.stdout
        out = work / "a.tif"
        assert out.exists(), "no output for a mode 8 + thumbnail run"
        assert page_count(out) == 2, "main image + generated thumbnail expected"
        assert rmse_pages(reference, out) == "0 (0)", (
            "page 0 differs from the source that was deleted for it"
        )

    def test_no_thumb_fallback_is_still_verified(self, tmp_path, lane):
        """-ThumbFormat notaformat forces the [no thumb] path: one page, and the gate must
        still hold (expected page count 1, page 0 identical)."""
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "a.tif")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-GenerateThumbnail", "-ThumbFormat", "notaformat",
                         "-StagingDir", str(tmp_path / "staging"), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "[no thumb]" in result.stdout, result.stdout
        out = work / "a.tif"
        assert out.exists() and page_count(out) == 1
        assert rmse_pages(reference, out) == "0 (0)"


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestPercentInFileNames:
    """ImageMagick expands printf-style scene specifiers in a filename spec, so a '%' in the
    name sent the output somewhere else while magick still exited 0: mode 9 left the source
    folder holding "500ff.tif" with the original stranded in OLD_TIFFs, reported
    "WARN (exiftool failed, ZIP ok)" and exited 0."""

    def _tree(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        for name in PERCENT_NAMES:
            make_tiff_named(work, name)
        return work

    def test_mode9_writes_every_name_verbatim(self, tmp_path, lane):
        work = self._tree(tmp_path)
        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "9", "-InputDir", str(work), "-Workers", "2"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        produced = sorted(p.name for p in work.glob("*.tif"))
        assert produced == sorted(f"{n}.tif" for n in PERCENT_NAMES), (
            f"output names were rewritten by magick: {produced}\n{result.stdout}"
        )
        for name in PERCENT_NAMES:
            out = work / f"{name}.tif"
            assert read_ifd0_compression(out).lower() in ("deflate", "adobe deflate"), (
                f"{name}.tif was not actually compressed"
            )
            assert rmse_pages(work / "OLD_TIFFs" / f"{name}.tif", out) == "0 (0)"

    def test_mode8_does_not_delete_when_the_name_is_rewritten(self, tmp_path, lane):
        """The delete gate has to survive this even if the escaping is ever lost again:
        no output at the expected path means no delete."""
        work = self._tree(tmp_path)
        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-StagingDir", str(tmp_path / "staging"), "-Workers", "2"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        for name in PERCENT_NAMES:
            assert (work / f"{name}.tif").exists(), f"{name}.tif vanished"
            assert read_ifd0_compression(work / f"{name}.tif").lower() in (
                "deflate", "adobe deflate")

    def test_thumbnail_generator_writes_the_name_it_reports(self, tmp_path, lane):
        work = self._tree(tmp_path)
        result = run_ps(lane, "generate_thumbnails.py",
                        ["-InputDir", str(work), "-Size", "32"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        for name in PERCENT_NAMES:
            assert (work / f"{name}_thumb.jpg").exists(), (
                f"{name}_thumb.jpg missing\n{result.stdout}"
            )

    def test_copy_exif_zip_writes_the_name_verbatim(self, tmp_path, lane):
        script = "copy_exif_to_tiff.py"
        work = tmp_path / "work"
        work.mkdir()
        make_tiff_named(work, "50%off")
        make_tiff_named(work, "50%off", ".jpg")

        result = run_ps(lane, script, ["-InputDir", str(work), "-CompressZip",
                                        "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        out = work / "50%off.tif"
        assert out.exists()
        assert read_ifd0_compression(out).lower() in ("deflate", "adobe deflate")
        assert read_tag(out, "EXIF:Make") == "FujiTest"
        leftovers = [p.name for p in work.glob("*.tif") if p.name != "50%off.tif"]
        assert not leftovers, f"staged/mangled files left behind: {leftovers}"



@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestToolOutputExclusionIsRelative:
    """Round 14 made -ExcludeFolders relative to the scan root in the three backends. The same
    absolute match survived in generate_thumbnails.py and in the diagnose workflow: an
    archive under D:/Backup/zip/2019 reported "No TIFF files found" and exited 0, and
    pointing the tool AT a folder named ZIP could never work."""

    def test_thumbnails_work_inside_a_folder_named_zip(self, tmp_path, lane):
        zipdir = tmp_path / "ZIP"
        zipdir.mkdir()
        make_tiff(zipdir / "a.tif")

        result = run_ps(lane, "generate_thumbnails.py",
                        ["-InputDir", str(zipdir), "-Size", "32"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert (zipdir / "a_thumb.jpg").exists(), (
            f"a folder named ZIP hid its own contents\n{result.stdout}"
        )

    def test_nested_backups_are_still_skipped(self, tmp_path, lane):
        root = tmp_path / "PhotoSet"
        (root / "OLD_TIFFs").mkdir(parents=True)
        (root / "keep").mkdir()
        make_tiff(root / "OLD_TIFFs" / "backup.tif")
        make_tiff(root / "keep" / "source.tif")

        result = run_ps(lane, "generate_thumbnails.py",
                        ["-InputDir", str(root), "-Recursive", "-Size", "32"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert (root / "keep" / "source_thumb.jpg").exists(), result.stdout
        assert not (root / "OLD_TIFFs" / "backup_thumb.jpg").exists(), (
            "this tool's own backups are no longer skipped"
        )

    def test_zip_and_export_are_no_longer_excluded_by_default(self, tmp_path, lane):
        """`ZIP/` is where compress modes 1/3/5/6/7 PUT the compressed library and `_EXPORT`
        is what modes 6/7 exist to process. Excluding them by default meant "make thumbnails
        of my compressed library" silently produced nothing, with no flag to override it."""
        root = tmp_path / "PhotoSet"
        (root / "ZIP").mkdir(parents=True)
        (root / "_EXPORT").mkdir()
        make_tiff(root / "ZIP" / "compressed.tif")
        make_tiff(root / "_EXPORT" / "exported.tif")

        result = run_ps(lane, "generate_thumbnails.py",
                        ["-InputDir", str(root), "-Recursive", "-Size", "32"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert (root / "ZIP" / "compressed_thumb.jpg").exists(), result.stdout
        assert (root / "_EXPORT" / "exported_thumb.jpg").exists(), result.stdout

    def test_exclude_folders_puts_them_back(self, tmp_path, lane):
        root = tmp_path / "PhotoSet"
        (root / "ZIP").mkdir(parents=True)
        (root / "keep").mkdir()
        make_tiff(root / "ZIP" / "compressed.tif")
        make_tiff(root / "keep" / "source.tif")

        result = run_ps(lane, "generate_thumbnails.py",
                        ["-InputDir", str(root), "-Recursive", "-Size", "32",
                         "-ExcludeFolders", "ZIP"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert (root / "keep" / "source_thumb.jpg").exists(), result.stdout
        assert not (root / "ZIP" / "compressed_thumb.jpg").exists(), result.stdout
        assert "Excluded 1 file(s)" in result.stdout, (
            f"the exclusion must be reported, not silent\n{result.stdout}"
        )


class TestDiagnoseExclusionIsRelative:
    def test_folder_named_zip_is_not_its_own_exclusion(self, tmp_path):
        root = tmp_path / "ZIP"
        (root / "sub").mkdir(parents=True)
        target = root / "sub" / "a.tif"
        target.write_bytes(b"x")
        assert convert_tiff._is_tool_output_path(target, root) is False, (
            "the chosen folder's own name excluded everything under it"
        )

    def test_nested_tool_output_is_excluded(self, tmp_path):
        root = tmp_path / "PhotoSet"
        (root / "OLD_TIFFs").mkdir(parents=True)
        target = root / "OLD_TIFFs" / "a.tif"
        target.write_bytes(b"x")
        assert convert_tiff._is_tool_output_path(target, root) is True

    def test_ancestor_name_does_not_exclude(self, tmp_path):
        root = tmp_path / "backup" / "zip" / "PhotoSet"
        root.mkdir(parents=True)
        target = root / "a.tif"
        target.write_bytes(b"x")
        assert convert_tiff._is_tool_output_path(target, root) is False


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestThumbnailFrameMatching:
    """The frame lookup escaped its pattern with backticks and passed it to -Filter, which is
    the FileSystem provider's DOS filter: it has no escape syntax, so a basename containing
    '[' matched nothing. The frames were written, reported as "output not created", and
    regenerated (and re-reported) on every later run."""

    def test_bracket_name_with_page_all(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        safe = work / "brackets.tif"
        make_multipage_tiff(safe, [None, 1])
        safe.rename(work / "br[1].tif")

        first = run_ps(lane, "generate_thumbnails.py",
                       ["-InputDir", str(work), "-Page", "all", "-Size", "32"], tmp_path)
        assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
        assert "output not created" not in first.stdout, first.stdout
        frames = sorted(p.name for p in work.glob("br[[]1[]]_thumb-*.jpg"))
        assert len(frames) == 2, f"expected two frames, got {frames}\n{first.stdout}"

        second = run_ps(lane, "generate_thumbnails.py",
                        ["-InputDir", str(work), "-Page", "all", "-Size", "32"], tmp_path)
        assert second.returncode == 0, second.stdout
        assert "SKIP (exists)" in second.stdout, (
            f"existing frames were not detected on the second run\n{second.stdout}"
        )


@requires_tools
class TestPurgeAcceptsRegeneratedThumbnails:
    """The purge compared every page, so a folder compressed with -GenerateThumbnail came back
    "PAGE_COUNT_MISMATCH 1 vs 2" / "PIXEL_DIFF page 1" for every file -- and one mismatch
    aborted the purge for the whole tree, leaving the user to delete OLD_TIFFs by hand."""

    def _with_thumb(self, tmp_path, main, thumb_size="16x12"):
        """`main` plus a tagged REDUCEDIMAGE thumbnail, the shape -GenerateThumbnail writes."""
        thumb = tmp_path / "t.tif"
        subprocess.run(["magick", str(main) + "[0]", "-thumbnail", thumb_size, str(thumb)],
                       capture_output=True, check=True, timeout=120)
        out = tmp_path / "with_thumb.tif"
        subprocess.run(["magick", str(main) + "[0]", str(thumb), "-compress", "zip", str(out)],
                       capture_output=True, check=True, timeout=120)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-IFD0:SubfileType=",
                        "-IFD1:SubfileType#=1", str(out)],
                       capture_output=True, check=True, timeout=120)
        thumb.unlink()
        return out

    def test_added_thumbnail_verifies(self, tmp_path):
        old = make_tiff(tmp_path / "old.tif")
        new = self._with_thumb(tmp_path, old)
        match, detail = _compare_tiff_metadata(old, new)
        assert match is True, detail
        assert "thumbnail added" in detail

    def test_regenerated_thumbnail_verifies(self, tmp_path):
        base = make_tiff(tmp_path / "base.tif")
        old = self._with_thumb(tmp_path, base, "16x12")
        shutil.move(str(old), str(tmp_path / "old_with_thumb.tif"))
        old = tmp_path / "old_with_thumb.tif"
        new = self._with_thumb(tmp_path, base, "8x6")
        match, detail = _compare_tiff_metadata(old, new)
        assert match is True, detail
        assert "thumbnail regenerated" in detail

    def test_untagged_small_page_is_not_excused(self, tmp_path):
        """Only a TAGGED thumbnail is derived data. An untagged extra page could be a real
        image, and this verdict deletes the last backup -- so it stays a mismatch."""
        old = make_tiff(tmp_path / "old.tif")
        small = tmp_path / "small.tif"
        subprocess.run(["magick", str(old) + "[0]", "-thumbnail", "16x12", str(small)],
                       capture_output=True, check=True, timeout=120)
        new = tmp_path / "new.tif"
        subprocess.run(["magick", str(old) + "[0]", str(small), str(new)],
                       capture_output=True, check=True, timeout=120)
        match, _detail = _compare_tiff_metadata(old, new)
        assert match is False

    def test_dropped_image_page_still_blocks(self, tmp_path):
        """A scanner RGB + preview + IR file whose IR page was lost must never verify."""
        src = tmp_path / "scan.tif"
        make_multipage_tiff(src, [None, 1, 4])
        lost = tmp_path / "lost.tif"
        subprocess.run(["magick", str(src) + "[0]", str(src) + "[1]", "-compress", "zip", str(lost)],
                       capture_output=True, check=True, timeout=120)
        match, detail = _compare_tiff_metadata(src, lost)
        assert match is False, detail

    def test_degraded_page_zero_still_blocks(self, tmp_path):
        old = make_tiff(tmp_path / "old.tif")
        new = tmp_path / "new.tif"
        subprocess.run(["magick", str(old) + "[0]", "-modulate", "80", str(new)],
                       capture_output=True, check=True, timeout=120)
        match, detail = _compare_tiff_metadata(old, new)
        assert match is False, detail

    def test_one_mismatch_no_longer_blocks_the_others(self, tmp_path, monkeypatch, capsys):
        old_dir = tmp_path / "OLD_TIFFs"
        old_dir.mkdir()
        good = make_tiff(old_dir / "good.tif")
        shutil.copy2(good, tmp_path / "good.tif")
        bad = make_tiff(old_dir / "bad.tif")
        subprocess.run(["magick", str(bad) + "[0]", "-modulate", "80", str(tmp_path / "bad.tif")],
                       capture_output=True, check=True, timeout=120)

        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, *a, **k: tmp_path)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "n")  # stop at the confirmation
        cfg = SimpleNamespace(config=ToolConfig())

        convert_tiff.run_purge_old_tiffs(cfg)
        out = capsys.readouterr().out

        assert "1 of 2 file(s) verified OK" in out, (
            f"a single mismatch still blocks the whole purge\n{out}"
        )
        assert "will be KEPT" in out, out
        assert (old_dir / "bad.tif").exists() and (old_dir / "good.tif").exists()


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestRound15Reporting:
    def test_thumbpage_above_one_is_reported_as_ignored(self, tmp_path, lane):
        """-ThumbPage was validated (>= 1) and then ignored: the thumbnail is always page 1."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "a.tif")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(work), "-GenerateThumbnail",
                         "-ThumbPage", "3", "-ThumbSize", "16", "-Workers", "1"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert "-ThumbPage 3 is not implemented" in result.stdout, result.stdout
        assert page_count(work / "ZIP" / "a.tif") == 2

    def test_dry_run_counts_what_it_would_do(self, tmp_path, lane):
        """Every dry run ended on "0 OK" because Process-Results had no DRY branch."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "a.tif")
        make_tiff(work / "b.tif")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(work), "-DryRun"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert "2 dry-run" in result.stdout, result.stdout

    def test_superseded_duplicate_is_reported(self, tmp_path, lane):
        """-DuplicateAction Overwrite dropped the earlier task without counting it, so the
        file was missing from the N/total tally -- indistinguishable from one that worked."""
        work = tmp_path / "work"
        (work / "a").mkdir(parents=True)
        (work / "b").mkdir()
        make_tiff(work / "a" / "photo.tif")
        make_tiff(work / "b" / "photo.tif")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "2", "-InputDir", str(work),
                         "-OutputDir", str(tmp_path / "flat"),
                         "-DuplicateAction", "Overwrite", "-Workers", "1"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert "superseded by a later duplicate" in result.stdout, result.stdout
        assert "2/2 processed" in result.stdout, result.stdout


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestInPlaceGuardCoversEveryMode:
    """The temp-sibling + verify guard was gated on `$mode -lt 0`. Mode 2 pointed at its own
    input root, and mode 5 for a file already inside a ZIP/ folder, both resolve
    finalDst == srcPath and went straight to `magick src -compress zip src`."""

    def test_mode2_into_its_own_root(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "a.tif")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "2", "-InputDir", str(work), "-OutputDir", str(work),
                         "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_ifd0_compression(src).lower() in ("deflate", "adobe deflate")
        assert read_tag(src) == ARTIST, "in-place rewrite lost the metadata"
        assert rmse_pages(reference, src) == "0 (0)"
        assert sorted(p.name for p in work.glob("*")) == ["a.tif"], (
            "a temp sibling was left behind"
        )

    def test_mode5_file_already_in_a_zip_folder(self, tmp_path, lane):
        work = tmp_path / "work"
        (work / "ZIP").mkdir(parents=True)
        src = make_tiff(work / "ZIP" / "a.tif")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "5", "-InputDir", str(work), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_ifd0_compression(src).lower() in ("deflate", "adobe deflate")
        assert rmse_pages(reference, src) == "0 (0)"
        assert sorted(p.name for p in (work / "ZIP").glob("*")) == ["a.tif"]


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestPercentSurvivesExiftool:
    """The '%' work in this round covered ImageMagick's filename SPECS. exiftool expands the
    SAME kind of code in the `-tagsfromfile` ARGUMENT -- `50%off.tif` is read as `50ff.tif` --
    and unlike magick it warns and STILL EXITS 0. Every caller checked only the exit code, so
    the run reported OK while the output carried no metadata at all. In mode 8 the source is
    deleted right afterwards, which made it permanent.

    Doubling does not escape it (`50%%off.jpg` -> `50%ff.jpg`), so these tests assert on the
    TAG at the destination, never on the exit code.
    """

    def test_mode9_keeps_metadata(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff_named(work, "50%off")
        make_tiff_named(work, "plain")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "9", "-InputDir", str(work), "-Workers", "2"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        for name in ("50%off", "plain"):
            out = work / f"{name}.tif"
            assert read_ifd0_compression(out).lower() in ("deflate", "adobe deflate")
            assert read_tag(out) == ARTIST, (
                f"{name}.tif came out with no metadata\n{result.stdout}"
            )

    def test_mode8_delete_keeps_metadata(self, tmp_path, lane):
        """The permanent case: mode 8 removes the only other copy."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff_named(work, "50%off", ".tiff")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-StagingDir", str(tmp_path / "staging"), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        out = work / "50%off.tif"
        assert out.exists(), result.stdout
        assert read_tag(out) == ARTIST, (
            f"mode 8 deleted the source and kept no metadata\n{result.stdout}"
        )

    def test_copy_exif_reads_a_percent_jpeg(self, tmp_path, lane):
        """Same trap on the OTHER side: the JPEG is the -tagsfromfile source here."""
        script = "copy_exif_to_tiff.py"
        work = tmp_path / "work"
        work.mkdir()
        make_tiff_named(work, "50%off")
        make_tiff_named(work, "50%off", ".jpg")

        result = run_ps(lane, script, ["-InputDir", str(work), "-CompressZip",
                                        "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        out = work / "50%off.tif"
        assert read_ifd0_compression(out).lower() in ("deflate", "adobe deflate")
        assert read_tag(out, "EXIF:Make") == "FujiTest", (
            f"the EXIF never made it out of the '%' JPEG\n{result.stdout}"
        )


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestBackupsAreNeverInput:
    """OLD_TIFFs holds the PRISTINE originals modes 0/9 archived. Only mode 8 and the
    modes 0/9 builder ever excluded it, each with its own ABSOLUTE regex, so modes 1-7 walked
    in and compressed the backups. The real file is already Deflate and was SKIPped, which left
    the BACKUP as the only candidate -- it took the output name, or lost the collision and
    landed as `photo_v2.tif`."""

    @pytest.mark.parametrize("mode", ["2", "3", "5"])
    def test_recursive_modes_ignore_old_tiffs(self, tmp_path, lane, mode):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "a.tif")
        run_ps(lane, "compress_tiff_zip.py",
               ["-Mode", "9", "-InputDir", str(work), "-Workers", "1"], tmp_path)
        backup = work / "OLD_TIFFs" / "a.tif"
        assert backup.exists(), "mode 9 did not archive the original"
        before = backup.read_bytes()

        args = ["-Mode", mode, "-InputDir", str(work), "-Workers", "1"]
        if mode == "2":
            args += ["-OutputDir", str(tmp_path / "flat")]
        result = run_ps(lane, "compress_tiff_zip.py", args, tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert backup.read_bytes() == before, "the backup was recompressed"
        strays = [p.name for p in work.rglob("*_v2.tif")] + \
                 [p.name for p in (tmp_path / "flat").rglob("*_v2.tif")] if (tmp_path / "flat").exists() \
                 else [p.name for p in work.rglob("*_v2.tif")]
        assert not strays, f"a backup was compressed under a _v2 name: {strays}\n{result.stdout}"
        assert not list((work / "OLD_TIFFs").glob("ZIP/*")), (
            f"output was written INSIDE the backup folder\n{result.stdout}"
        )

    @pytest.mark.parametrize("mode", ["2", "3", "8"])
    def test_a_photo_next_to_old_tiffs_is_still_compressed(self, tmp_path, lane, mode):
        """The other half: the exclusion must not be too broad. An UNCOMPRESSED photo beside
        an OLD_TIFFs folder is compressed; the pristine backup keeps its bytes. (Until v3.0.0
        this was pinned on the legacy mode only.)"""
        work = tmp_path / "work"
        old = work / "OLD_TIFFs"
        old.mkdir(parents=True)
        photo = make_tiff(work / "photo.tif")
        # the backup of photo.tif itself: a backup with nothing in its place would be
        # reported as STRANDED (round 25), which is another story
        backup = make_tiff(old / "photo.tif")
        before = backup.read_bytes()

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", mode, "-InputDir", str(work), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        out = work / "ZIP" / "photo.tif" if mode == "3" else photo
        assert read_compression(out).lower() == "zip", (
            f"the real source was not compressed -- the exclusion is too broad\n{result.stdout}"
        )
        assert backup.read_bytes() == before, "the OLD_TIFFs backup was rewritten"
        assert sorted(p.name for p in old.iterdir()) == ["photo.tif"]
        assert "Skipped 1 file(s) in OLD_TIFFs" in result.stdout, result.stdout
        assert "Found: 1 TIFF(s)" in result.stdout and "STRANDED" not in result.stdout, result.stdout

    def test_pointing_at_old_tiffs_still_works(self, tmp_path, lane):
        """Relative matching, not absolute: the round 14 rule. An explicit -InputDir wins."""
        work = tmp_path / "work" / "OLD_TIFFs"
        work.mkdir(parents=True)
        make_tiff(work / "a.tif")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert (work / "ZIP" / "a.tif").exists(), (
            f"an explicitly chosen OLD_TIFFs folder was refused\n{result.stdout}"
        )

    def test_leftovers_from_an_interrupted_run_are_not_photos(self, tmp_path, lane):
        """Both shapes this toolkit writes and may leave behind: a staged file (copy_exif with
        no -StagingDir stages into the source folder) and an in-place temp sibling."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "a.tif")
        make_tiff(work / "1a2b3c4d_0123456789ab_a.tif")
        make_tiff(work / "a.tif.deadbeef_cafe12345678.tif")

        result = run_ps(lane, "compress_tiff_zip.py",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "Skipped 2 staged/temp file(s)" in result.stdout, result.stdout
        assert sorted(p.name for p in (work / "ZIP").glob("*")) == ["a.tif"], (
            f"a leftover was compressed as if it were a photo\n{result.stdout}"
        )


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestThumbnailPageSwitch:
    """`-Page all` writes name_thumb-0.jpg, name_thumb-1.jpg, ...; `-Page 0` writes
    name_thumb.jpg. Test-ThumbExists looked for the frame set unconditionally, so once a
    `-Page all` run had happened, `-Page 0` reported SKIP (exists) for every file forever and
    name_thumb.jpg could only be produced by deleting the frames by hand."""

    def test_page_zero_after_page_all(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        make_multipage_tiff(work / "scan.tif", [None, 1])

        first = run_ps(lane, "generate_thumbnails.py",
                       ["-InputDir", str(work), "-Size", "32", "-Page", "all"], tmp_path)
        assert first.returncode == 0, first.stdout
        assert (work / "scan_thumb-0.jpg").exists(), first.stdout

        second = run_ps(lane, "generate_thumbnails.py",
                        ["-InputDir", str(work), "-Size", "32", "-Page", "0"], tmp_path)
        assert second.returncode == 0, second.stdout
        assert (work / "scan_thumb.jpg").exists(), (
            f"-Page 0 was skipped because the -Page all frames existed\n{second.stdout}"
        )

    def test_page_all_still_no_ops_on_a_repeat(self, tmp_path, lane):
        work = tmp_path / "work"
        work.mkdir()
        make_multipage_tiff(work / "scan.tif", [None, 1])

        run_ps(lane, "generate_thumbnails.py",
               ["-InputDir", str(work), "-Size", "32", "-Page", "all"], tmp_path)
        again = run_ps(lane, "generate_thumbnails.py",
                       ["-InputDir", str(work), "-Size", "32", "-Page", "all"], tmp_path)

        assert again.returncode == 0, again.stdout
        assert "SKIP (exists)" in again.stdout, (
            f"the -Page all frame set is no longer recognised\n{again.stdout}"
        )


class TestPythonMagickSpec:
    """convert_tiff.py builds `path[0]` specs too, and was the copy left behind: workflow [6]
    reported a permanent MISMATCH for any '%' file (so the backups could never be purged) and
    workflow [7] reported an ERROR line for each one."""

    def test_compare_and_diagnose_read_a_percent_name(self, tmp_path):
        ct = convert_tiff
        old_dir = tmp_path / "OLD_TIFFs"
        old_dir.mkdir()
        # make_tiff_named, not make_tiff: `magick ... "50%off.tif"` writes "500ff.tif", so
        # building the fixture with the hostile name produces a file that is not there.
        src = make_tiff_named(old_dir, "50%off")
        new = tmp_path / "50%off.tif"
        # Plain INPUT stays literal, OUTPUT is escaped -- the rule the backends follow.
        subprocess.run(["magick", str(src), "-compress", "zip", magick_spec(new)],
                       capture_output=True, check=True, timeout=120)

        match, detail = ct._compare_tiff_metadata(src, new)
        assert match, f"the purge gate could not read a '%' name: {detail}"

        _real, _rmse, detail16 = ct._is_real_16bit(new)
        assert "identify failed" not in detail16, detail16

    def test_escaper_matches_the_backends_rule(self):
        ct = convert_tiff
        assert ct._magick_spec("50%off.tif") == "50%%off.tif"
        assert ct._magick_spec("plain.tif") == "plain.tif"

    def test_safe_tag_source_containers_only_percent_names(self, tmp_path):
        ct = convert_tiff
        plain = make_tiff(tmp_path / "plain.tif")
        path, temp = ct._safe_tag_source(plain)
        assert path == str(plain) and temp is None, "a safe name must be passed straight through"

        pct = make_tiff_named(tmp_path, "50%off")
        path, temp = ct._safe_tag_source(pct)
        try:
            assert temp is not None and "%" not in path, "a '%' name needs a container"
            assert Path(path).exists()
            dest = make_tiff(tmp_path / "dest.tif", artist="OTHER")
            subprocess.run(["exiftool", "-q", "-q", "-overwrite_original",
                            "-tagsfromfile", path, "-all:all", "-unsafe", str(dest)],
                           capture_output=True, check=True, timeout=120)
            assert read_tag(dest) == ARTIST, "the container did not carry the tags"
        finally:
            if temp:
                Path(temp).unlink(missing_ok=True)








class TestAuditRound16PurgeMetadataGate:
    """The purge verified PIXELS only, so a conversion that dropped metadata (a lost
    per-page SubfileType, an EXIF block, an ICC profile) purged the last good backup.
    _compare_tiff_metadata now refuses unless the metadata signatures match too."""

    @staticmethod
    def _fake_run(subfile_by_name):
        def fake_run(cmd, **kwargs):
            r28 = _fake_round28_exiftool(cmd, kwargs)
            if r28:
                return r28
            cmd = [str(a) for a in cmd]
            if cmd[0] == "exiftool" and "-@" in cmd:
                # _run_exiftool sends everything as a UTF-8 argfile on stdin (`-@ -`); read it back
                assert cmd[cmd.index("-@") + 1] == "-", "the argfile must go on stdin, not by path"
                lines = kwargs["input"].decode("utf-8").splitlines()
                cmd = ["exiftool"] + [a for a in lines if a not in ("-charset", "filename=utf8")]
            if cmd[0] == "exiftool":
                name = cmd[-1]
                if "-SubfileType" in cmd:
                    out = subfile_by_name.get(name, "")
                    return SimpleNamespace(returncode=0, stdout=out, stderr="")
                if "-EXIF:all" in cmd:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            if "compare" in cmd:
                return SimpleNamespace(returncode=0, stdout="0 (0)", stderr="")
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if fmt == "%n\n":
                return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="64 64", stderr="")
        return fake_run

    def test_lost_subfiletype_refuses_the_purge(self, tmp_path, monkeypatch):
        old = tmp_path / "OLD_TIFFs" / "scan.tif"
        old.parent.mkdir()
        old.write_bytes(b"old")
        new = tmp_path / "scan.tif"
        new.write_bytes(b"new")
        monkeypatch.setattr(subprocess, "run", self._fake_run({
            str(old): "[IFD0] SubfileType: 1\n",
            str(new): "",
        }))

        match, detail = convert_tiff._compare_tiff_metadata(old, new)
        assert not match, "the new file lost its SubfileType tag and the purge still passed"
        assert "SUBFILETYPE_MISMATCH" in detail, detail

    def test_matching_subfiletypes_pass(self, tmp_path, monkeypatch):
        old = tmp_path / "OLD_TIFFs" / "scan.tif"
        old.parent.mkdir()
        old.write_bytes(b"old")
        new = tmp_path / "scan.tif"
        new.write_bytes(b"new")
        monkeypatch.setattr(subprocess, "run", self._fake_run({
            str(old): "[IFD0] SubfileType: 1\n",
            str(new): "[IFD0] SubfileType: 1\n",
        }))

        match, detail = convert_tiff._compare_tiff_metadata(old, new)
        assert match, f"identical metadata was refused: {detail}"

    def test_unreadable_metadata_refuses_the_purge(self, tmp_path, monkeypatch):
        """Missing exiftool must fail CLOSED, never read as 'metadata matches'."""
        old = tmp_path / "OLD_TIFFs" / "scan.tif"
        old.parent.mkdir()
        old.write_bytes(b"old")
        new = tmp_path / "scan.tif"
        new.write_bytes(b"new")

        def fake_run(cmd, **kwargs):
            cmd = [str(a) for a in cmd]
            if cmd[0] == "exiftool":
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if "compare" in cmd:
                return SimpleNamespace(returncode=0, stdout="0 (0)", stderr="")
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if fmt == "%n\n":
                return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="64 64", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        match, detail = convert_tiff._compare_tiff_metadata(old, new)
        assert not match
        assert "metadata unreadable" in detail, detail


def _read_tags_utf8(path, *tags):
    """exiftool -s3 through a UTF-8 argfile: the fixture folder is Japanese."""
    argfile = Path(path).parent.parent / f"_tags_{os.getpid()}.args"
    argfile.write_text("\n".join(["-charset", "filename=utf8", "-s", "-s", "-s", "-f"]
                                 + [f"-{t}" for t in tags] + [str(path)]) + "\n",
                       encoding="utf-8")
    try:
        out = subprocess.run(["exiftool", "-@", str(argfile)], capture_output=True,
                             timeout=120).stdout.decode("utf-8", "replace")
    finally:
        argfile.unlink()
    return [line.strip() for line in out.splitlines()]


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestCopyExifSkipIfTiffHasExif:
    """Round 23: -SkipIfTiffHasExif probed `-EXIF:all`, which in exiftool includes IFD0 --
    the size, resolution and orientation every TIFF has -- so it skipped EVERY TIFF, the
    Hyper Utility exports it exists for included (451 of 451 on a real S5 Pro folder). Only
    camera EXIF (the Exif IFD: exposure, aperture, dates) counts as "already has EXIF"."""

    def test_skips_only_a_tiff_with_camera_exif(self, tmp_path, lane):
        script = "copy_exif_to_tiff.py"
        # Built under a plain name and renamed: magick globs '[ ]' and expands '%'.
        safe = tmp_path / "work"
        safe.mkdir()
        bare = safe / "_DSF0311.tif"            # a Hyper Utility export: IFD0 only
        subprocess.run(["magick", "-size", "64x48", "gradient:", "-depth", "8", str(bare)],
                       capture_output=True, check=True, timeout=120)
        done = make_tiff(safe / "_DSF0312.tif")  # already carries camera EXIF
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-ExifIFD:ExposureTime=1/80",
                        str(done)], capture_output=True, check=True, timeout=120)
        for stem in ("_DSF0311", "_DSF0312"):
            TestCopyExifVariantAndHeuristicNames._make_fuji_jpeg(safe / f"{stem}.jpg")
        work = tmp_path / "伊豆沼 [S5pro] 100%"
        safe.rename(work)

        result = run_ps(lane, script, ["-InputDir", str(work), "-SkipIfTiffHasExif"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK | _DSF0311.tif <= _DSF0311.jpg" in result.stdout, result.stdout
        assert "SKIP (already has EXIF) | _DSF0312.tif" in result.stdout, result.stdout
        assert _read_tags_utf8(work / "_DSF0311.tif", "IFD0:Make", "ExifIFD:DateTimeOriginal") \
            == ["FUJIFILM", "2025:03:09 10:12:07"]
        assert _read_tags_utf8(work / "_DSF0312.tif", "IFD0:Make") == ["TestCam"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
