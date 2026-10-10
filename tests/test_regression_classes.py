"""
Regression tests for the five recurring bug classes.

Rounds 1-6 of the audit kept re-finding the *same* five failure classes in modes and files the
previous round had not visited, because every fix was pointwise and nothing pinned it. Each
class below gets tests that fail if the fix is reverted anywhere it applies:

    1. exit code      - `return` at script scope exits 0 and discards `$errTotal -gt 0 -> exit 1`
    2. data loss      - in-place compression must not strip EXIF; gates that authorise
                        destroying data must fail CLOSED
    3. PS5/PS7        - the sequential and -Parallel paths must behave identically
    4. path/staging   - a run must never touch another run's in-flight files
    5. dest collision - a failed move must never leave both source and destination gone

Later audit rounds pinned their own fixes the same way; the round-specific classes
(TestRound15Reporting, the TestAuditRound16* group, ...) sit below the five classes.

The PowerShell parts run end to end through the real shells (skipped when a shell or
ImageMagick/exiftool is missing) rather than as Pester tests, because the repo's Pester suites
need Pester 5 and CI/dev boxes here still ship 3.4.0. Static AST invariants live in
tests/helpers/Find-PsInvariantViolations.ps1.
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

import convert_tiff
from convert_tiff import (
    ToolConfig,
    _compare_tiff_metadata,
    _compress_padded_files,
    _process_single_padded,
    _safe_move,
)

REPO = Path(__file__).resolve().parent.parent
HELPER = Path(__file__).resolve().parent / "helpers" / "Find-PsInvariantViolations.ps1"

PS_SCRIPTS = [
    "compress_tiff_zip.ps1",
    "copy_exif_to_TIFF_ps5.ps1",
    "copy_exif_to_TIFF_ps7.ps1",
    "generate_thumbnails.ps1",
]

SHELLS = [s for s in ("powershell", "pwsh") if shutil.which(s)]
IMAGE_TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None

requires_shell = pytest.mark.skipif(not SHELLS, reason="no powershell/pwsh on PATH")
requires_tools = pytest.mark.skipif(
    not IMAGE_TOOLS, reason="ImageMagick (magick) and exiftool required"
)

ARTIST = "RegressionTester"


# -- helpers --------------------------------------------------------

def run_ps(shell, script, args, cwd, timeout=300):
    """Invoke a backend script and return the CompletedProcess (exit code included).

    Decoded as UTF-8 with replacement: a non-ASCII path in the output is raw UTF-8
    from one child and '?' from another (the console code page flips to UTF-8 as
    soon as a pwsh child has run), and the locale code page (cp1252) raised
    UnicodeDecodeError on the Japanese fixture folder (round 20)."""
    cmd = [shell, "-NoProfile", "-File", str(REPO / script)] + [str(a) for a in args]
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout
    )


def run_ps_switched(shell, script, args, cwd, timeout=300):
    """Like run_ps, but routes Windows PowerShell 5.1 through the same -Command wrapper the
    wizard uses (_wrap_ps5_command). PS5 cannot bind `-Switch:$false` when arguments arrive
    via -File -- there they are plain strings -- which is exactly why that wrapper exists.
    Tests that pass -SafeMode:$false must use this, or PS5 silently runs with SafeMode ON
    and the assertion passes for the wrong reason."""
    cmd = [shell, "-NoProfile", "-File", str(REPO / script)] + [str(a) for a in args]
    if shell == "powershell":
        cmd = convert_tiff._wrap_ps5_command(cmd)
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def read_ifd0_compression(path):
    """IFD0 compression. `magick identify -format %[compression]` prints one value per
    page, so on a multi-page file it returns "zipzip" and a naive == "zip" fails."""
    result = subprocess.run(
        ["exiftool", "-s", "-s", "-s", "-IFD0:Compression", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip()


def run_invariant_check(script):
    """Run the AST checker; returns (violations, parse_error_lines)."""
    shell = SHELLS[-1]
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(HELPER), "-Path", str(REPO / script)],
        capture_output=True, text=True, timeout=120,
    )
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    return lines, result.returncode


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

@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "script,extra",
    [("compress_tiff_zip.ps1", ["-Mode", "0"]), ("generate_thumbnails.ps1", [])],
)
class TestExitCodeContract:
    """
    `return` at script scope always exits 0. convert_tiff.py gates step 2 on this exit code,
    so an early exit that discarded $errTotal made a fully failed step 1 read as success.
    """

    def test_missing_dir_in_semicolon_list_exits_1(self, tmp_path, shell, script, extra):
        good = tmp_path / "good"
        good.mkdir()
        missing = tmp_path / "nope"

        result = run_ps(shell, script, extra + ["-InputDir", f"{good};{missing}"], tmp_path)
        assert result.returncode == 1, (
            f"bad path in a ';' list must exit 1\n{result.stdout}\n{result.stderr}"
        )

    def test_valid_empty_dir_exits_0(self, tmp_path, shell, script, extra):
        good = tmp_path / "good"
        good.mkdir()

        result = run_ps(shell, script, extra + ["-InputDir", str(good)], tmp_path)
        assert result.returncode == 0, (
            f"a valid folder with no TIFFs is not an error\n{result.stdout}\n{result.stderr}"
        )


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
def test_all_files_failing_precheck_exits_1(tmp_path, shell):
    """
    Mode 0 with every file failing the pre-check empties $tasks and lands on the second early
    exit with N errors already logged -- the site that used to `return` (exit 0).
    """
    work = tmp_path / "work"
    work.mkdir()
    (work / "broken.tif").write_text("not a tiff at all")

    result = run_ps(shell, "compress_tiff_zip.ps1", ["-Mode", "0", "-InputDir", str(work)], tmp_path)
    assert "No tasks to process" in result.stdout, result.stdout  # the early-exit site
    assert result.returncode == 1, f"errors were logged, exit must be 1\n{result.stdout}"


# copy_exif ships one script per host; running the PS7 one under powershell is not a
# supported combination, so each is pinned to its own shell.
COPY_EXIF_TARGETS = [
    (script, shell)
    for script, shell in (("copy_exif_to_TIFF_ps5.ps1", "powershell"),
                          ("copy_exif_to_TIFF_ps7.ps1", "pwsh"))
    if shell in SHELLS
]


@requires_shell
@pytest.mark.parametrize(
    "script,shell", COPY_EXIF_TARGETS, ids=[f"{s}/{sh}" for s, sh in COPY_EXIF_TARGETS]
)
class TestCopyExifExitCodeContract:
    """
    The round 6 exit-code fix landed in compress_tiff_zip.ps1 and generate_thumbnails.ps1 and was
    never generalised here: a wrong path fell through to "No TIFFs found" and exited 0, so
    convert_tiff.py let Step 2 of workflow 4 run after a Copy EXIF that touched nothing.
    """

    def test_missing_dir_exits_1(self, tmp_path, script, shell):
        result = run_ps(shell, script, ["-InputDir", str(tmp_path / "nope")], tmp_path)
        assert result.returncode == 1, (
            f"a path that does not exist is an error, not an empty folder\n{result.stdout}"
        )
        assert "input directory not found" in result.stdout, result.stdout

    def test_missing_dir_in_semicolon_list_exits_1(self, tmp_path, script, shell):
        good = tmp_path / "good"
        good.mkdir()
        result = run_ps(
            shell, script, ["-InputDir", f"{good};{tmp_path / 'nope'}"], tmp_path
        )
        assert result.returncode == 1, result.stdout

    def test_valid_empty_dir_exits_0(self, tmp_path, script, shell):
        good = tmp_path / "good"
        good.mkdir()
        result = run_ps(shell, script, ["-InputDir", str(good)], tmp_path)
        assert result.returncode == 0, (
            f"a valid folder with no TIFFs is not an error\n{result.stdout}"
        )


@requires_shell
@pytest.mark.parametrize("script", PS_SCRIPTS)
def test_no_return_at_script_scope(script):
    """Static guard so the class cannot come back in a mode this suite does not run."""
    violations, rc = run_invariant_check(script)
    assert rc == 0, f"{script} failed to parse: {violations}"
    offenders = [v for v in violations if v.startswith("SCRIPT_SCOPE_RETURN")]
    assert not offenders, (
        f"{script}: `return` at script scope exits 0 and discards $errTotal:\n"
        + "\n".join(offenders)
    )


# -- CLASS 2: data loss (in-place EXIF, fail-closed gates) ----------

@requires_shell
@requires_tools
class TestInPlaceExifPreservation:
    """
    With no -StagingDir and no output folder the write target IS the source, so magick
    overwrote the original before `-tagsfromfile <source>` ran and every tag was lost while
    the run reported OK. Backup-TiffMetadata parks the metadata first; each of the three
    workers must use it.
    """

    # (label, shell-independent args) -- covers Process-TiffJob, the legacy -Parallel worker
    # and the mode -Parallel worker.
    WORKERS = [
        ("legacy-sequential", ["-ForceSequential"]),
        ("legacy-parallel", ["-ForceParallel"]),
        ("mode2-sequential", ["-Mode", "2", "-InputDir", "work", "-ForceSequential"]),
        ("mode2-parallel", ["-Mode", "2", "-InputDir", "work", "-ForceParallel"]),
    ]

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("label,args", WORKERS, ids=[w[0] for w in WORKERS])
    def test_exif_survives_in_place_compression(self, tmp_path, shell, label, args):
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "photo.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1", args, tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_compression(src).lower() == "zip", "file was not actually compressed"
        assert read_tag(src) == ARTIST, (
            f"[{label}/{shell}] in-place compression stripped EXIF\n{result.stdout}"
        )

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("parallel", ["-ForceSequential", "-ForceParallel"])
    def test_no_thumb_fallback_keeps_exif(self, tmp_path, shell, parallel):
        """
        CLASS 3 too: a failed thumbnail made the PS7 -Parallel block return early, skipping the
        EXIF restore the sequential path reached via $noThumbNote. Same input, same file, two
        different outcomes. An unencodable -ThumbFormat forces the fallback.
        """
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "photo.tif")

        result = run_ps(
            shell, "compress_tiff_zip.ps1",
            [parallel, "-GenerateThumbnail", "-ThumbFormat", "notaformat"], tmp_path,
        )

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "[no thumb]" in result.stdout, (
            f"fallback not exercised -- test is not proving anything\n{result.stdout}"
        )
        assert read_tag(src) == ARTIST, (
            f"[{parallel}/{shell}] the [no thumb] fallback skipped the EXIF restore"
        )


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
def test_legacy_mode_leaves_old_tiffs_untouched(tmp_path, shell):
    """
    OLD_TIFFs holds the pristine originals modes 0/9 preserved on purpose. Every mode >= 0
    excludes it; legacy scanned $PWD with no exclusion at all and recompressed the backups.
    """
    work = tmp_path / "work"
    old = work / "OLD_TIFFs"
    old.mkdir(parents=True)
    make_tiff(work / "photo.tif")
    backup = make_tiff(old / "pristine.tif")

    result = run_ps(shell, "compress_tiff_zip.ps1", ["-ForceSequential"], tmp_path)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert read_compression(backup).lower() != "zip", (
        "the OLD_TIFFs backup was recompressed in place"
    )
    assert read_compression(work / "photo.tif").lower() == "zip", (
        "the real source was not compressed -- the exclusion is too broad"
    )


def make_multipage_tiff(path, subfiletypes, sizes=None):
    """Multi-page TIFF (main + reduced preview + optional MASK page) with the
    given NewSubfileType markers written per IFD, e.g. [None, 1, 4] for a
    scanner RGB + preview + IR file. `sizes` overrides the default geometry
    (main large, REDUCEDIMAGE small, others large)."""
    pages = []
    for i, st in enumerate(subfiletypes):
        p = path.parent / f"_pg{i}_{path.name}"
        if sizes is not None:
            size = sizes[i]
        else:
            size = "64x48" if i == 0 else ("16x12" if st == 1 else "64x48")
        subprocess.run(
            ["magick", "-size", size, "plasma:fractal", "-depth", "16", str(p)],
            capture_output=True, check=True, timeout=120,
        )
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


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestMultiPageCompressE2E:
    """The CRITICAL round 5 class (multi-page TIFF stranded in OLD_TIFFs) and the
    subfiletype-preservation fix, exercised end to end with real files."""

    def test_mask_page_skipped_original_untouched(self, tmp_path, shell):
        """Scanner RGB+IR: the MASK page is not in the compress whitelist, so
        SafeMode must skip the file WITHOUT moving it to OLD_TIFFs."""
        scan = make_multipage_tiff(tmp_path / "scan_ir.tif", [None, 1, 4])

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "0", "-InputDir", str(tmp_path)], tmp_path)

        assert "MULTI" in result.stdout, result.stdout
        assert scan.exists(), "skipped multi-page TIFF vanished from its folder"
        assert not (tmp_path / "OLD_TIFFs").exists(), \
            "a skipped file must never be moved to OLD_TIFFs"

    def test_reducedimage_markers_survive_compression(self, tmp_path, shell):
        """Capture One layout (main + REDUCEDIMAGE thumbnail): ImageMagick drops
        NewSubfileType on rewrite, so the backend must restore it -- otherwise the
        next run sees a 'genuine' multi-page file and thumbnail detection breaks."""
        make_multipage_tiff(tmp_path / "c1_style.tif", [None, 1])

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "0", "-InputDir", str(tmp_path)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK" in result.stdout
        sts = read_subfiletypes(tmp_path / "c1_style.tif")
        assert len(sts) == 2, f"page count changed: {sts}"
        assert sts[0] == "", f"IFD0 must stay untagged, got {sts[0]!r}"
        assert sts[1] in ("REDUCEDIMAGE", "REDUCED"), f"thumbnail marker lost: {sts[1]!r}"
        assert rmse_pages(tmp_path / "OLD_TIFFs" / "c1_style.tif",
                          tmp_path / "c1_style.tif") == "0 (0)"

    def test_page_tagged_thumbnail_compresses_anyway(self, tmp_path, shell):
        """A thumbnail is a thumbnail even when the tag lies: ImageMagick stamps
        PAGE on every page it rewrites, so a previously recompressed Capture One
        file (small thumbnail marked PAGE) was skipped as genuinely multi-page.
        Size, not just the tag, classifies the page."""
        make_multipage_tiff(tmp_path / "rewritten_c1.tif", [2, 2], sizes=["64x48", "16x12"])

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "0", "-InputDir", str(tmp_path)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK" in result.stdout, result.stdout
        assert "MULTI" not in result.stdout, result.stdout
        assert (tmp_path / "OLD_TIFFs" / "rewritten_c1.tif").exists()

    def test_full_size_second_page_still_skips(self, tmp_path, shell):
        """The dimension rule must not open the gate for real pages: a full-size
        untagged extra page (second photo, Photoshop layer) is still MULTI."""
        make_multipage_tiff(tmp_path / "two_photos.tif", [None, None], sizes=["64x48", "64x48"])

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "0", "-InputDir", str(tmp_path)], tmp_path)

        assert "MULTI" in result.stdout, result.stdout
        assert not (tmp_path / "OLD_TIFFs").exists(), \
            "a skipped file must never be moved to OLD_TIFFs"


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestMode8DeleteE2E:
    def test_mode8_deletes_tiff_source_output_pixel_identical(self, tmp_path, shell):
        """The only end-to-end run of the delete path: .tiff source must be gone,
        .tif output must exist, and pixels must match the original exactly."""
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "photo.tiff")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-StagingDir", str(tmp_path / "staging")], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert not src.exists(), ".tiff source was not deleted"
        out = work / "photo.tif"
        assert out.exists(), "compressed .tif output missing"
        assert read_compression(out).lower() in ("zip", "adobe deflate")
        assert rmse_pages(reference, out) == "0 (0)", "pixels differ from the deleted source"


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
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

    def test_export_tree_never_touched(self, tmp_path, shell):
        photoset = self._tree(tmp_path)

        result = run_ps(shell, "compress_tiff_zip.ps1",
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

    def test_exclusion_is_segment_match_not_substring(self, tmp_path, shell):
        photoset = tmp_path / "PhotoSet"
        lookalike = photoset / "My_EXPORT_photos"
        lookalike.mkdir(parents=True)
        make_tiff(lookalike / "keep.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "9", "-InputDir", str(photoset),
                         "-ExcludeFolders", "_EXPORT"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_compression(lookalike / "keep.tif").lower() in ("zip", "adobe deflate"), (
            "'My_EXPORT_photos' was excluded by a substring match -- only exact segments may match"
        )

    def test_path_entry_rejected_with_exit_1(self, tmp_path, shell):
        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "9", "-InputDir", str(tmp_path),
                         "-ExcludeFolders", r"foo\bar"], tmp_path)
        assert result.returncode == 1
        assert "not paths" in result.stdout


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestCopyExifE2E:
    def test_exif_copied_from_jpeg_to_tiff(self, tmp_path, shell):
        """First behavioral test of the actual copy: Make/Model from the JPEG
        must land on the TIFF, and the exit code must be 0."""
        script = "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
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

        result = run_ps(shell, script, ["-InputDir", str(work)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_tag(work / "photo.tif", "EXIF:Make") == "FUJIFILM"
        assert read_tag(work / "photo.tif", "EXIF:Model") == "FinePix S5Pro"

    def test_miss_exits_0_but_failonwarn_exits_1(self, tmp_path, shell):
        """A TIFF without a JPEG pair is a MISS (warning class): default contract
        keeps exit 0, -FailOnWarn turns it into a gateable failure."""
        script = "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
        work = tmp_path / "S5pro"
        work.mkdir()
        make_tiff(work / "lonely.tif")

        ok = run_ps(shell, script, ["-InputDir", str(work)], tmp_path)
        assert ok.returncode == 0, f"{ok.stdout}\n{ok.stderr}"

        strict = run_ps(shell, script, ["-InputDir", str(work), "-FailOnWarn"], tmp_path)
        assert strict.returncode == 1, f"{strict.stdout}\n{strict.stderr}"


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
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

    def test_variant_suffix_pairs_as_plain_ok(self, tmp_path, shell):
        script = "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
        work = tmp_path / "S5pro"
        work.mkdir()
        make_tiff(work / "foto_1.tif")
        self._make_fuji_jpeg(work / "foto.jpg")

        result = run_ps(shell, script, ["-InputDir", str(work)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK | foto_1.tif <= foto.jpg" in result.stdout, result.stdout
        assert "heuristic" not in result.stdout, result.stdout

    def test_three_digit_heuristic_still_warns(self, tmp_path, shell):
        script = "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
        work = tmp_path / "S5pro"
        work.mkdir()
        make_tiff(work / "foto_2024.tif")
        self._make_fuji_jpeg(work / "foto.jpg")

        result = run_ps(shell, script, ["-InputDir", str(work)], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "WARN (heuristic JPEG match)" in result.stdout, result.stdout


@requires_shell
@requires_tools
@pytest.mark.skipif("pwsh" not in SHELLS, reason="thumbnail backend is exercised under pwsh")
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

        gen = run_ps("pwsh", "generate_thumbnails.ps1", args, tmp_path)
        assert gen.returncode == 0, gen.stdout
        created = sorted(root.rglob("*_thumb*"))
        assert len(created) == 2, f"setup failed: {created}"

        rem = run_ps("pwsh", "generate_thumbnails.ps1", args + ["-Remove"], tmp_path)
        assert rem.returncode == 0, rem.stdout
        assert not list(root.rglob("*_thumb*")), (
            "-Remove with the flags that created them found nothing:\n" + rem.stdout
        )

    def test_renamed_thumbnail_is_not_rescanned_as_a_source(self, tmp_path):
        root = self._two_colliding_tiffs(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        args = ["-InputDir", str(root), "-OutputDir", str(out), "-Recursive", "-Format", "tif"]

        gen = run_ps("pwsh", "generate_thumbnails.ps1", args, tmp_path)
        assert gen.returncode == 0, gen.stdout
        assert (out / "photo_thumb_v2.tif").exists(), f"no collision happened:\n{gen.stdout}"

        # Second pass over the output folder: both thumbnails must be recognised as thumbnails
        again = run_ps(
            "pwsh", "generate_thumbnails.ps1",
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
            "pwsh", "generate_thumbnails.ps1",
            ["-InputDir", str(root), "-OutputDir", str(out), "-Recursive", "-Format", "tif"],
            tmp_path,
        )
        assert (out / "photo_thumb_v2.tif").exists()

        rem = run_ps(
            "pwsh", "generate_thumbnails.ps1",
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
                     pages_new=PAGES_OK, pages_orig=PAGES_OK):
        """Fakes a successful 16 -> 8-bit conversion with configurable verification reads."""
        def fake_run(cmd, **kwargs):
            cmd = [str(c) for c in cmd]
            if cmd[0] == "exiftool":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
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
        ],
        ids=["new-dims-unreadable", "orig-dims-unreadable", "dims-garbage",
             "dims-differ", "new-pages-unreadable", "orig-pages-garbage", "pages-differ"],
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


# -- CLASS 3: PS5 / PS7 parity --------------------------------------

@requires_shell
class TestPs5Ps7Parity:
    """
    Functions defined in the parent script are invisible inside ForEach-Object -Parallel, so a
    helper added to the sequential path silently no-ops in the runspace unless it is re-injected
    via ${function:Name} = $using:NameFnDef. Divergence between the two paths is the class.
    """

    @pytest.mark.parametrize("script", PS_SCRIPTS)
    def test_parallel_blocks_reinject_every_helper_they_call(self, script):
        violations, rc = run_invariant_check(script)
        assert rc == 0, f"{script} failed to parse: {violations}"
        missing = [v for v in violations if v.startswith("MISSING_INJECTION")]
        assert not missing, (
            f"{script}: helper(s) called in a -Parallel block without ${{function:...}} "
            f"re-injection -- they are invisible in the runspace:\n" + "\n".join(missing)
        )

    def test_all_three_workers_back_up_metadata_in_place(self):
        """
        compress_tiff_zip.ps1 carries near-duplicate workers (Process-TiffJob, legacy -Parallel,
        mode -Parallel); a fix applied to one has repeatedly been left out of the others.
        """
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        assert source.count("$metaBackupInfo = Backup-TiffMetadata") == 3, (
            "every worker that can write in place must park the metadata first"
        )
        # ...fail-closed when the backup was needed but failed (in-place write refused)...
        assert source.count("$metaBackupInfo.Needed -and -not $metaBackupInfo.Path") == 3
        # ...and every one of them must restore FROM the backup, not from the overwritten source
        assert source.count("$tagSource = if ($metaBackup) { $metaBackup } else {") == 3

    def test_mode8_pixel_gate_and_final_recheck_in_both_paths(self):
        """The mode 8 delete gate must pixel-compare staged vs source (decode-check alone
        cannot see a truncated page), and the file that authorises the delete must be
        re-verified where it actually sits -- the staging move is not atomic cross-volume.

        Pinned per call site rather than as a bare total: the in-place gate now uses
        Test-PixelIdentical too, so a plain count no longer distinguishes the two gates
        (and would pass if a mode 8 gate were deleted and an in-place one duplicated).
        """
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        mode8_gates = re.findall(
            r"Test-PixelIdentical -SrcPath \$srcPath -DstPath \$verify\w+", source
        )
        assert len(mode8_gates) == 2, (
            "sequential and -Parallel workers must both pixel-verify before delete, "
            f"found {len(mode8_gates)}"
        )
        # Round 15: -GenerateThumbnail writes a different page layout ON PURPOSE, so
        # whole-file identity refused every file and mode 8 produced nothing at all. Both
        # workers now branch to the page-0 gate for that case -- which must still be a
        # gate, not a skip.
        thumb_gates = re.findall(
            r"Test-ThumbOutputMatchesSource -SrcPath \$srcPath -DstPath \$verify\w+", source
        )
        assert len(thumb_gates) == 2, (
            "both mode 8 workers must verify a thumbnail output against source page 0, "
            f"found {len(thumb_gates)}"
        )
        assert source.count("final ZIP failed integrity - source preserved") == 2, (
            "both delete loops must re-verify the final destination before Remove-Item"
        )

    def test_legacy_in_place_writes_go_through_verified_temp_sibling(self):
        """An in-place write goes over the only copy; a magick crash destroys the image.
        Every worker must write a temp sibling and replace atomically.

        Round 15 raised the count from 2 to 3: the guard used to be gated on `$mode -lt 0`,
        as if only legacy could write onto its own source. Mode 2 with -OutputDir on the
        input root, and mode 5 for a file already inside a ZIP/ folder, both resolve
        finalDst == srcPath -- and the mode >= 0 -Parallel worker had no guard at all.
        """
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        assert source.count("$inPlaceFinalDst = $writeDst") == 3, (
            "Process-TiffJob and both -Parallel workers must redirect in-place writes"
        )
        assert "$mode -lt 0 -and $writeDst -eq $srcPath" not in source, (
            "the in-place guard must not be limited to legacy mode again"
        )
        # 3, not 2, for the same reason the count above is 3: the mode >= 0 -Parallel worker
        # is an in-place worker too now. These two assertions were left behind when the first
        # one was raised.
        assert source.count("Move-Item -LiteralPath $writeDst -Destination $inPlaceFinalDst") == 3
        # Decode alone is not enough before replacing the ONLY copy: a page-short file still
        # decodes cleanly (that is how -GenerateThumbnail silently dropped a scanner IR page
        # in place). Every in-place worker must pixel-compare too, like mode 8 does.
        assert source.count("$inPlaceOk = Test-PixelIdentical") == 3, (
            "every in-place worker must pixel-verify, not just decode-check, before replacing"
        )

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_both_copy_exif_scripts_scope_staging_cleanup(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        assert "$script:runStagingId" in source, (
            f"{script}: staging cleanup must be scoped to this run, not to every GUID-named file"
        )

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_staging_move_has_no_unprefixed_fallback(self, script):
        """
        Falling back to <writeDir>\\<tif.Name> would pick up a file this run never staged --
        with a shared -StagingDir that is somebody else's in-flight output, moved on top of
        the user's TIFF. Every name this run stages carries the run-scoped prefix.
        """
        source = (REPO / script).read_text(encoding="ascii")
        assert "Join-Path $writeDir $tif.Name" not in source, (
            f"{script}: unprefixed staging fallback is back"
        )

    def test_every_identify_goes_through_the_timeout_wrapper(self):
        """
        A bare `magick identify` has no timeout, so a corrupted file hangs the whole run.
        The conversion calls are deliberately unwrapped; the probes are not.
        """
        code = [
            ln for ln in (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii").splitlines()
            if not ln.lstrip().startswith("#")
        ]
        offenders = [ln.strip() for ln in code if "magick identify" in ln]
        assert not offenders, (
            "use Invoke-MagickWithTimeout for identify probes:\n" + "\n".join(offenders)
        )

    def test_compression_probe_reads_page_zero_only(self):
        """
        exiftool prints one -Compression line per IFD, and `-match` on an array is truthy when
        ANY element matches: an uncompressed main image with a compressed thumbnail page was
        skipped as "already compressed". Each of the three workers plus the modes 0/9
        pre-check must collapse the array to IFD0 first.
        """
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        assert source.count('"$(@($comp)[0])".Trim()') == 4, (
            "every compression probe must decide on the main image, not on any page"
        )
        assert 'Comp        = "$comp"' not in source, (
            "tasks must carry the collapsed value, not the joined array"
        )


@pytest.mark.parametrize("script", PS_SCRIPTS)
def test_ps_sources_are_pure_ascii_without_bom(script):
    """
    PowerShell 5.1 decodes BOM-less files as cp1252, where a stray em-dash becomes `”` -- a
    string delimiter. One non-ASCII character stopped a whole script from parsing (round 5).
    """
    raw = (REPO / script).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{script} has a UTF-8 BOM"
    offenders = [
        (i, b) for i, b in enumerate(raw) if b > 0x7F
    ]
    assert not offenders, (
        f"{script}: non-ASCII byte(s) at offset(s) "
        f"{[o[0] for o in offenders[:5]]} -- use '--', '->' and '-' instead"
    )


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
        old = self._prepare(tmp_path, monkeypatch, ["y", "y", "n"])  # overwrite, move, keep dirs

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
        it = iter(["n", "y", "n"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))

        result = convert_tiff.run_undo_old_tiffs(SimpleNamespace(config=ToolConfig()))

        assert result is True
        assert (tmp_path / "photo.tif").read_bytes() == b"data"


class TestSafeMoveNeverLosesBoth:
    """`dst.unlink()` before `shutil.move` meant a move that then failed left both files gone."""

    def test_failed_move_restores_destination(self, tmp_path, monkeypatch):
        src = tmp_path / "src.tif"
        dst = tmp_path / "dst.tif"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")

        def boom(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(shutil, "move", boom)
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
    Legacy mode writes IN PLACE, so there the loss was irreversible."""

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_refuses_scanner_ir_source_in_every_mode(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "scan.tif"
        # main RGB + reduced preview + full-size IR MASK page, like raw_scan/raw_scan_2
        make_multipage_tiff(src, [None, 1, 4])
        before = src.read_bytes()

        result = run_ps_switched(shell, "compress_tiff_zip.ps1",
                                 ["-Mode", "3", "-InputDir", str(work), "-SafeMode:$false",
                                  "-GenerateThumbnail", "-Workers", "1"], work)

        assert "-GenerateThumbnail refused" in result.stdout, result.stdout
        assert result.returncode == 1, "a refusal must be an error, not a silent success"
        assert not (work / "ZIP" / "scan.tif").exists(), "a page-short output was written"
        assert src.read_bytes() == before, "source must be untouched"

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_legacy_in_place_keeps_every_page(self, tmp_path, shell):
        """The irreversible case: legacy mode (no -Mode) overwrites the only copy."""
        work = tmp_path / "work"
        work.mkdir()
        src = work / "scan.tif"
        make_multipage_tiff(src, [None, 1, 4])
        pages_before = len(read_subfiletypes(src))
        assert pages_before == 3

        run_ps_switched(shell, "compress_tiff_zip.ps1",
                        ["-SafeMode:$false", "-GenerateThumbnail"], work)

        assert len(read_subfiletypes(src)) == 3, (
            "legacy in-place -GenerateThumbnail destroyed a page of the only copy"
        )

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_thumbnail_only_source_is_still_processed(self, tmp_path, shell):
        """A Capture One main+thumbnail pair must keep working -- the guard must not turn
        into a blanket refusal of every multi-page file."""
        work = tmp_path / "work"
        work.mkdir()
        src = work / "photo.tif"
        make_multipage_tiff(src, [None, 1])

        result = run_ps(shell, "compress_tiff_zip.ps1",
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
    MASK and PAGE -- so a scanner RGB+IR file that compress_tiff_zip.ps1 skips was accepted,
    rewritten by magick and moved over the original."""

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("script,shell", [
        ("copy_exif_to_TIFF_ps5.ps1", "powershell"),
        ("copy_exif_to_TIFF_ps7.ps1", "pwsh"),
    ])
    def test_scanner_ir_is_skipped_as_multipage(self, tmp_path, script, shell):
        if shell not in SHELLS:
            pytest.skip(f"{shell} not on PATH")
        work = tmp_path / "S5pro"
        work.mkdir()
        src = work / "scan.tif"
        make_multipage_tiff(src, [None, 1, 4])
        subprocess.run(["magick", "-size", "64x48", "gradient:", str(work / "scan.jpg")],
                       capture_output=True, check=True, timeout=120)
        before = src.read_bytes()

        result = run_ps(shell, script, ["-InputDir", str(work), "-CompressZip",
                                        "-Workers", "1"], tmp_path)

        assert "MULTI" in result.stdout, result.stdout
        assert src.read_bytes() == before, "scanner IR file was rewritten anyway"

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_call_site_pins_the_restricted_list(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        assert 'Test-TiffHasOnlySubfilePages -Path $p.Tiff -PageCount $pc.PageCount ' \
               '-AllowedSubfileTypes @("REDUCEDIMAGE", "REDUCED")' in source, (
            f"{script}: SafeMode must pass the restricted list explicitly, like the four "
            f"call sites in compress_tiff_zip.ps1 -- the default also allows MASK/PAGE"
        )


class TestCopyExifZipIsPixelVerified:
    """The staged ZIP is moved on top of the TIFF it came from and there is no OLD_TIFFs
    backup on this path, so a zero exit from magick is not enough to authorise it."""

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_gate_present_before_metadata_copy(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        assert "Test-PixelIdentical -SrcPath $tiffTarget -DstPath $writeDst" in source, (
            f"{script}: -CompressZip must pixel-verify before the staged file replaces the source"
        )
        assert "ZIP pixel verification failed - original untouched" in source

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("script,shell", [
        ("copy_exif_to_TIFF_ps5.ps1", "powershell"),
        ("copy_exif_to_TIFF_ps7.ps1", "pwsh"),
    ])
    def test_zip_round_trip_is_pixel_identical(self, tmp_path, script, shell):
        if shell not in SHELLS:
            pytest.skip(f"{shell} not on PATH")
        work = tmp_path / "S5pro"
        work.mkdir()
        src = work / "photo.tif"
        make_multipage_tiff(src, [None, 1])
        pristine = tmp_path / "pristine.tif"
        shutil.copy(src, pristine)
        subprocess.run(["magick", "-size", "64x48", "gradient:", str(work / "photo.jpg")],
                       capture_output=True, check=True, timeout=120)

        run_ps(shell, script, ["-InputDir", str(work), "-CompressZip", "-Workers", "1"], tmp_path)

        assert read_ifd0_compression(src).lower().startswith(("zip", "deflate", "adobe"))
        for page in range(2):
            assert rmse_pages(pristine, src, page).startswith("0 "), (
                f"page {page} changed during the -CompressZip round trip"
            )


class TestMode5PathSeparator:
    """-InputDir with forward slashes (legal on Windows) made the mode 5 prefix compare
    fail for every nested file, so the "never climb above the input root" fallback fired
    and the whole tree collapsed into <root>\\ZIP with spurious _v2 renames."""

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_same_tree_for_both_path_forms(self, tmp_path, shell):
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
            run_ps(shell, "compress_tiff_zip.ps1",
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

    @requires_shell
    @requires_tools
    @pytest.mark.skipif("pwsh" not in SHELLS, reason="thumbnail backend is exercised under pwsh")
    def test_embedded_profile_changes_the_thumbnail(self, tmp_path):
        plain, tagged = self._wide_gamut_pair(tmp_path)
        if plain is None:
            pytest.skip("no wide-gamut ICC profile available on this machine")
        work = tmp_path / "work"
        work.mkdir()
        shutil.copy(plain, work / "plain.tif")
        shutil.copy(tagged, work / "tagged.tif")

        run_ps("pwsh", "generate_thumbnails.ps1",
               ["-InputDir", str(work), "-Size", "64"], tmp_path)

        a = (work / "plain_thumb.jpg").read_bytes()
        b = (work / "tagged_thumb.jpg").read_bytes()
        assert a and b
        assert a != b, (
            "tagged and untagged sources produced identical thumbnails -- the embedded "
            "ICC profile is being ignored (-colorspace instead of -profile)"
        )

    @pytest.mark.parametrize("script", ["generate_thumbnails.ps1", "compress_tiff_zip.ps1"])
    def test_profile_is_applied_before_strip(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        assert "Resolve-SrgbProfile" in source, f"{script}: no sRGB profile resolution"
        assert '"-profile", $' in source or '"-profile", $script:SrgbProfilePath' in source, (
            f"{script}: thumbnails must convert through an ICC profile, not just -colorspace"
        )


class TestSubfileTypeZeroIsPreserved:
    """`%[tiff:subfiletype]` prints an EMPTY string both when the tag is absent and when it
    is 0 ("full-resolution image"), so the restore DELETED an explicit 0 instead of keeping
    it. Every scanner TIFF carries an explicit 0 on IFD0."""

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_explicit_zero_survives_compression(self, tmp_path, shell):
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

        run_ps_switched(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "3", "-InputDir", str(work), "-SafeMode:$false", "-Workers", "1"], work)

        after = numeric_markers(work / "ZIP" / "scan.tif")
        assert after == before, f"markers changed: {before} -> {after}"


class TestRunSummaryAlwaysPrinted:
    """When the mode 0/9 pre-check filtered every file the run exited before the summary,
    so a folder where everything was correctly skipped ended on a bare WARN with no
    `Done:` line -- and any log parser keyed on it saw nothing."""

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_done_line_present_when_all_files_skipped(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "photo.tif"
        make_tiff(src)
        # pre-compress so the mode 9 pre-check skips it as already-Deflate
        subprocess.run(["magick", str(src), "-compress", "zip", str(src)],
                       capture_output=True, check=True, timeout=120)

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "9", "-InputDir", str(work), "-Workers", "1"], work)

        assert "Done:" in result.stdout, result.stdout
        assert "1 skipped" in result.stdout
        assert "1/1 processed" in result.stdout, "the skipped file must close the total"
        assert result.returncode == 0
        assert not (work / "OLD_TIFFs").exists(), "a skipped file must not be moved"

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_prefilter_results_carry_the_counter_prefix(self, tmp_path, shell):
        """MULTI/SKIP emitted by the modes 0/9 pre-check bypassed Process-Results, so they
        printed without the [n/total] prefix every other result line carries."""
        work = tmp_path / "work"
        work.mkdir()
        make_multipage_tiff(work / "scan.tif", [None, 1, 4])
        make_tiff(work / "plain.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1",
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
        make_multipage_tiff(src, [None, 1])
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
            folders=[Path("C:/x")], ps_name="pwsh")
        assert "-ExcludeFolders" in cmd
        assert cmd[cmd.index("-ExcludeFolders") + 1] == "_EXPORT;temp"

    def test_cap_workers_not_asked_where_unsupported(self):
        assert convert_tiff._supports_cap_workers({"origin": "free_compress"})
        assert convert_tiff._supports_cap_workers({"origin": "both"})
        assert not convert_tiff._supports_cap_workers({"origin": "copy_exif"})

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("script,shell", [
        ("copy_exif_to_TIFF_ps5.ps1", "powershell"),
        ("copy_exif_to_TIFF_ps7.ps1", "pwsh"),
    ])
    def test_backend_honours_the_flag(self, tmp_path, script, shell):
        """Round 14 changed the semantics this pins. Matching is relative to the scan root
        (jxl-photo's `_is_tool_output_path`), so a folder passed AS a root is not excluded by
        its own name -- only folders nested BELOW a root are. Pointing the tool directly at
        `_EXPORT` to process it therefore still works, which is the point.

        The invariant that matters -- the flag reaches this backend and drops a nested
        `_EXPORT` tree -- is pinned below with a nested layout and -Recurse forced by giving
        the parent as the root."""
        if shell not in SHELLS:
            pytest.skip(f"{shell} not on PATH")
        keep = tmp_path / "S5pro" / "keep"
        drop = tmp_path / "S5pro" / "_EXPORT"
        keep.mkdir(parents=True)
        drop.mkdir(parents=True)
        for d, stem in ((keep, "a"), (drop, "b")):
            make_tiff(d / f"{stem}.tif")
            subprocess.run(["magick", "-size", "32x32", "gradient:", str(d / f"{stem}.jpg")],
                           capture_output=True, check=True, timeout=120)

        result = run_ps(shell, script,
                        ["-InputDir", f"{keep};{drop}", "-DryRun",
                         "-ExcludeFolders", "_EXPORT"], tmp_path)

        # Both roots were named explicitly, so both are processed: neither has an excluded
        # folder BELOW it. The old absolute match dropped b.tif here.
        assert "a.tif" in result.stdout, result.stdout
        assert "b.tif" in result.stdout, (
            "a folder given AS a root must not be excluded by its own name"
        )
        assert "Excluded" not in result.stdout, result.stdout

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("script,shell", [
        ("copy_exif_to_TIFF_ps5.ps1", "powershell"),
        ("copy_exif_to_TIFF_ps7.ps1", "pwsh"),
    ])
    def test_ancestor_name_does_not_wipe_the_run(self, tmp_path, script, shell):
        """The bug this replaced: every ancestor of the file was matched, so a name that
        happened to appear above the root excluded everything and logged it as normal."""
        if shell not in SHELLS:
            pytest.skip(f"{shell} not on PATH")
        root = tmp_path / "S5pro"
        root.mkdir()
        make_tiff(root / "a.tif")
        subprocess.run(["magick", "-size", "32x32", "gradient:", str(root / "a.jpg")],
                       capture_output=True, check=True, timeout=120)

        for name in ("S5pro", tmp_path.name):
            result = run_ps(shell, script,
                            ["-InputDir", str(root), "-DryRun",
                             "-ExcludeFolders", name], tmp_path)
            assert "a.tif" in result.stdout, (
                f"-ExcludeFolders {name!r} excluded the whole run:\n{result.stdout}"
            )

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_segment_regex_covers_both_separators(self, script):
        """A '[\\/]' class (one backslash) matches only '/', so a Windows path would slip
        through the "names, not paths" guard and the segment split.

        Round 14 moved the segment split into Test-PathExcluded, which normalises '/' to
        '\\' and then splits, rather than matching a separator class -- so the half of this
        invariant that moved is pinned on that normalisation instead of on the class."""
        source = (REPO / script).read_text(encoding="ascii")
        assert r"-match '[\\/]'" in source, f"{script}: separator class lost a backslash"
        assert r".Replace('/', '\')" in source, (
            f"{script}: the exclusion matcher no longer normalises forward slashes"
        )


class TestPartialCopyIsCleanedUp:
    """PS7 set $copiedTiffPath only after Copy-Item returned, so a copy that threw mid-write
    left a truncated file in -OutputDir; a later run without -Overwrite then skipped it as
    "exists in OutputDir" and the truncated TIFF persisted. PS5 already guarded this."""

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_destination_is_tracked_before_the_copy(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        idx_copy = source.index("Copy-Item -LiteralPath $p.Tiff -Destination $destTiff")
        window = source[max(0, idx_copy - 400):idx_copy]
        assert "$copiedTiffPath = $destTiff" in window or \
               "Remove-Item -LiteralPath $destTiff" in source[idx_copy:idx_copy + 900], (
            f"{script}: a Copy-Item that throws must still leave the partial file removable"
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

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_explicit_zero_on_the_main_page_survives(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "scan.tif"
        make_multipage_tiff(src, [0, 1])
        assert "[IFD0] SubfileType: 0" in numeric_subfiletypes(src)

        run_ps_switched(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                         "-SafeMode:$false", "-GenerateThumbnail", "-ThumbSize", "16"], work)

        after = numeric_subfiletypes(work / "ZIP" / "scan.tif")
        assert "[IFD0] SubfileType: 0" in after, f"main page lost its marker: {after}"
        assert "[IFD1] SubfileType: 1" in after, f"thumbnail not marked: {after}"
        assert "[IFD0] SubfileType: 2" not in after, "magick's PAGE stamp was left on page 0"

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_source_without_a_marker_gets_none_back(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")

        run_ps(shell, "compress_tiff_zip.ps1",
               ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                "-GenerateThumbnail", "-ThumbSize", "16"], work)

        after = numeric_subfiletypes(work / "ZIP" / "photo.tif")
        assert not any(ln.startswith("[IFD0]") for ln in after), (
            f"a source with no marker must not gain one: {after}"
        )
        assert "[IFD1] SubfileType: 1" in after

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_generated_file_is_not_multi_page_on_a_later_run(self, tmp_path, shell):
        """The round trip that matters: whatever -GenerateThumbnail writes must still be
        touchable by SafeMode afterwards, or the toolkit refuses its own output forever."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")
        run_ps(shell, "compress_tiff_zip.ps1",
               ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                "-GenerateThumbnail", "-ThumbSize", "16"], work)

        second = work / "second"
        second.mkdir()
        shutil.copy2(work / "ZIP" / "photo.tif", second / "photo.tif")
        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "1", "-InputDir", str(second), "-Workers", "1",
                         "-SkipCompressedWithThumb"], second)

        assert "MULTI" not in result.stdout, (
            "SafeMode classified our own thumbnail output as a real multi-page file:\n"
            + result.stdout
        )

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_thumb_page_zero_is_refused(self, tmp_path, shell):
        """A thumbnail-first file has a full-size page 1, which every classifier here reads
        as a real extra page -- the file came back MULTI on every later run."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                         "-GenerateThumbnail", "-ThumbPage", "0"], work)

        assert result.returncode == 1, result.stdout
        assert "-ThumbPage 0 is not supported" in result.stdout
        assert not (work / "ZIP").exists(), "a refused run must not write anything"

    def test_all_three_workers_restore_both_markers(self):
        raw = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        # Both comment forms mention the injection line verbatim (that is how the runspace
        # convention is documented in this repo), so count code only.
        code = re.sub(r"<#.*?#>", "", raw, flags=re.S)
        code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
        assert code.count("Set-TiffThumbMarkers -SrcPath") == 3, (
            "every worker must restore the main page's marker, not just stamp the thumbnail"
        )
        assert "-${thumbIfd}:SubfileType#=1" not in raw, (
            "the thumbnail-only marker write is back"
        )
        assert code.count("${function:Set-TiffThumbMarkers} ") == 2, (
            "the helper must be injected into BOTH parallel runspaces"
        )
        assert "$script:ThumbMarkerFnDef   = ${function:Set-TiffThumbMarkers}.ToString()" in code


class TestSequentialWorkerReportsLikeTheParallelOnes:
    """Process-TiffJob logged a bare WARN for a failed subfiletype/thumbnail-marker write
    while the two -Parallel workers folded it into the result string. The same event was a
    counted warning on PS7 and a clean OK on PS5, and the PS5 line printed without the
    [n/total] prefix every other result line carries."""

    def test_every_worker_folds_the_failure_into_its_result(self):
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        assert source.count("$subfileRestoreFailed = $true") == 3
        assert source.count("$thumbMarkerFailed = $true") == 3
        assert source.count("$exifWarn -or $thumbMarkerFailed -or $subfileRestoreFailed") == 3
        assert 'Write-Log "WARN (subfiletype restore failed)' not in source, (
            "the sequential worker is bypassing Process-Results again"
        )
        assert 'Write-Log "WARN (thumbnail SubfileType marker failed)' not in source


class TestLongPathsAreRefusedLoudly:
    """Staged names used to prepend two full GUIDs (66 characters) and mode 8's default
    staging folder another 55, inside the user's own tree. On a normally nested archive the
    result blew past Windows' 260-character limit and EVERY file failed with magick's bare
    "No such file or directory" -- naming neither the cause nor the limit."""

    def test_staging_names_are_short(self):
        for script in PS_SCRIPTS:
            source = (REPO / script).read_text(encoding="ascii")
            for m in re.finditer(r"\[guid\]::NewGuid\(\)\.ToString\('N'\)(?!\.Substring)", source):
                line = source[:m.start()].count("\n") + 1
                pytest.fail(f"{script}:{line} uses a full 32-char GUID in a path")

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_over_long_output_is_reported_with_the_number(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")
        # Long enough that <out>\photo.tif passes 259 characters without creating it first
        long_out = str(tmp_path / ("d" * 200))

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "2", "-InputDir", str(work), "-Workers", "1",
                         "-OutputDir", long_out], work)

        assert "path too long" in result.stdout, result.stdout
        assert result.returncode == 1
        assert not Path(long_out).exists(), (
            "a refused file must not have its output directory created"
        )


class TestRepeatLastRebuildsForTheCurrentShell:
    """run_repeat_last swapped argv[0] and ran the journalled command unchanged. Replayed
    under powershell.exe, `-File ... -SafeMode:$false` binds the STRING '$false' (truthy),
    so the repeat silently ran with SafeMode ON -- the trap _wrap_ps5_command exists to
    avoid -- and a Copy EXIF command kept pointing at the other shell's backend."""

    def _compress_cmd(self, ps_name):
        return convert_tiff.build_compress_command(
            {"mode": 9, "workers": 8, "safe_mode": False},
            folders=[Path(r"C:\Fotos\x")], ps_name=ps_name)

    @pytest.mark.parametrize("origin", ["pwsh", "powershell"])
    @pytest.mark.parametrize("target", ["pwsh", "powershell"])
    def test_journal_round_trip_is_shell_neutral(self, origin, target):
        parts = convert_tiff._journal_parts(self._compress_cmd(origin))
        assert parts and parts[0].endswith("compress_tiff_zip.ps1")
        rebuilt = convert_tiff._rebuild_journaled_command(parts, target)
        assert convert_tiff._journal_parts(rebuilt) == parts

    def test_ps5_replay_goes_through_the_command_wrapper(self):
        parts = convert_tiff._journal_parts(self._compress_cmd("pwsh"))
        rebuilt = convert_tiff._rebuild_journaled_command(parts, "powershell")
        assert "-Command" in rebuilt and "-File" not in rebuilt, (
            "a PS5 replay must not pass -SafeMode:$false through -File"
        )
        assert "-SafeMode:$false" in rebuilt[-1]

    def test_copy_exif_backend_follows_the_shell(self):
        cmd = convert_tiff.build_copy_exif_command(
            {"workers": 4}, folders=[Path(r"C:\a")], ps_name="powershell")
        parts = convert_tiff._journal_parts(cmd)
        assert parts[0].endswith("copy_exif_to_TIFF_ps5.ps1")
        rebuilt = convert_tiff._rebuild_journaled_command(parts, "pwsh")
        assert any(a.endswith("copy_exif_to_TIFF_ps7.ps1") for a in rebuilt), rebuilt

    def test_quotes_in_a_path_survive_the_round_trip(self):
        cmd = convert_tiff.build_compress_command(
            {"mode": 0}, folders=[Path("C:\\it's here\\x")], ps_name="powershell")
        parts = convert_tiff._journal_parts(cmd)
        assert "C:\\it's here\\x" in parts

    def test_unrecognised_journal_is_refused(self, monkeypatch, capsys):
        cfg = SimpleNamespace(config=ToolConfig())
        cfg.config.last_run_kind = "compress"
        cfg.config.last_run_label = "something"
        cfg.config.last_run_commands = [["pwsh", "-NoProfile", "-EncodedCommand", "AAA="]]
        cfg.config.ps_name = "pwsh"
        assert convert_tiff.run_repeat_last(cfg) is False


class TestPythonSubfileRestoreMatchesPowerShell:
    """The Python mirror read the markers with `magick %[tiff:subfiletype]`, which prints an
    EMPTY string both for an absent tag and for an explicit 0 -- so the padded-file
    conversion DELETED the explicit 0 every scanner TIFF carries on IFD0. The PowerShell
    twin was fixed for exactly this; the mirror had been left behind."""

    @requires_tools
    def test_explicit_zero_is_preserved(self, tmp_path):
        src = tmp_path / "photo.tif"
        make_multipage_tiff(src, [0, 1])
        before = numeric_subfiletypes(src)
        assert "[IFD0] SubfileType: 0" in before
        staging = tmp_path / "stg"
        staging.mkdir()

        _, _, status, _, _, _, _, _, tmp8 = _process_single_padded(src, staging)

        assert status == "ok", status
        assert numeric_subfiletypes(tmp8) == before, (
            f"markers changed: {before} -> {numeric_subfiletypes(tmp8)}"
        )

    def test_restore_does_not_use_the_ambiguous_magick_property(self):
        source = (REPO / "convert_tiff.py").read_text(encoding="utf-8")
        body = source[source.index("def _restore_subfile_types"):]
        body = body[:body.index("\ndef ", 1)]
        assert '"%[tiff:subfiletype]' not in body, (
            "the restore is reading the property that cannot tell 'absent' from 0 again"
        )
        assert '"exiftool", "-a", "-G1", "-s", "-s", "-n", "-SubfileType"' in body


class TestCopyExifProbesTheMainImage:
    """compress_tiff_zip.ps1 collapses the -Compression probe to IFD0 in four places; the
    two Copy EXIF backends were the copies left behind, so an uncompressed main image whose
    thumbnail page happened to be compressed would be reported "already ZIP"."""

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_probe_reads_page_zero_only(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        assert "-IFD0:Compression" in source, f"{script}: probe still reads every IFD"
        # Audit round 16 routed the probe through Invoke-ExifTool; the collapse to the
        # first output line now happens on the helper's .Output array.
        assert '$comp = "$(@($compRes.Output)[0])".Trim()' in source, (
            f"{script}: -match on the raw array is truthy for ANY page"
        )

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_exif_gate_does_not_read_a_stale_exit_code(self, script):
        """`native | Select-Object -First 1` stops the upstream pipeline, so $LASTEXITCODE
        may still hold an earlier command's value -- and that gate decides whether the TIFF
        may be overwritten."""
        source = (REPO / script).read_text(encoding="ascii")
        assert "-EXIF:all $p.Tiff 2>$null | Select-Object -First 1" not in source, (
            f"{script}: the fail-closed EXIF check can read a stale exit code again"
        )
        # Audit round 16: the probe runs through Invoke-ExifTool, which returns the real
        # exiftool exit code on .ExitCode; the gate must read that, not $LASTEXITCODE.
        assert "$exifProbeExit = $exifProbeRes.ExitCode" in source


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

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_skip_line_points_at_the_flag(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        src = work / "photo.tif"
        make_tiff(src)
        subprocess.run(["magick", str(src), "-compress", "zip", str(src)],
                       capture_output=True, check=True, timeout=120)

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1",
                         "-GenerateThumbnail", "-ThumbSize", "16"], work)

        assert "no thumbnail added" in result.stdout, result.stdout
        assert "SkipCompressedWithThumb" in result.stdout


class TestGroupHeaderNamesTheOutputFolder:
    """Tasks were grouped by the PARENT of the output folder, so the "-- Group:" header and
    the "-> Moved N file(s) -> <dir>" line named a folder the run never wrote to -- the
    drive root for TIFFs sitting directly under the input root."""

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_moved_line_names_the_real_destination(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "photo.tif")
        staging = tmp_path / "stg"
        staging.mkdir()

        result = run_ps(shell, "compress_tiff_zip.ps1",
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

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_nested_names_excluded_ancestors_ignored(self, tmp_path, shell):
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
            out = run_ps(shell, "compress_tiff_zip.ps1", args, tmp_path).stdout
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

    @requires_shell
    @requires_tools
    @pytest.mark.parametrize("shell", SHELLS)
    def test_forward_slash_input_root_still_matches(self, tmp_path, shell):
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
            return run_ps(shell, "compress_tiff_zip.ps1",
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

    @pytest.mark.parametrize("script", ["compress_tiff_zip.ps1",
                                        "copy_exif_to_TIFF_ps5.ps1",
                                        "copy_exif_to_TIFF_ps7.ps1"])
    def test_no_backend_matches_the_absolute_path(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        for offender in (r"$_.DirectoryName -split '[\\/]'",
                         r"$df.DirectoryName -split '[\\/]'"):
            assert offender not in source, (
                f"{script}: exclusion is matching the absolute path again"
            )
        assert "Test-PathExcluded -Directory" in source, (
            f"{script}: exclusion no longer goes through the relative matcher"
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


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestMode8ThumbnailGate:
    """Mode 8 verified the staged file with whole-file pixel identity, which -GenerateThumbnail
    can never satisfy: it writes source page 0 plus a NEW thumbnail. Every file came back
    "ERROR (ZIP integrity check failed - source preserved)", so the combination produced no
    output at all -- while the in-place worker had had the exemption all along."""

    def test_thumbnail_output_is_accepted_and_source_deleted(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "a.tif")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(shell, "compress_tiff_zip.ps1",
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

    def test_no_thumb_fallback_is_still_verified(self, tmp_path, shell):
        """-ThumbFormat notaformat forces the [no thumb] path: one page, and the gate must
        still hold (expected page count 1, page 0 identical)."""
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "a.tif")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-GenerateThumbnail", "-ThumbFormat", "notaformat",
                         "-StagingDir", str(tmp_path / "staging"), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "[no thumb]" in result.stdout, result.stdout
        out = work / "a.tif"
        assert out.exists() and page_count(out) == 1
        assert rmse_pages(reference, out) == "0 (0)"


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
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

    def test_mode9_writes_every_name_verbatim(self, tmp_path, shell):
        work = self._tree(tmp_path)
        result = run_ps(shell, "compress_tiff_zip.ps1",
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

    def test_mode8_does_not_delete_when_the_name_is_rewritten(self, tmp_path, shell):
        """The delete gate has to survive this even if the escaping is ever lost again:
        no output at the expected path means no delete."""
        work = self._tree(tmp_path)
        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-StagingDir", str(tmp_path / "staging"), "-Workers", "2"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        for name in PERCENT_NAMES:
            assert (work / f"{name}.tif").exists(), f"{name}.tif vanished"
            assert read_ifd0_compression(work / f"{name}.tif").lower() in (
                "deflate", "adobe deflate")

    def test_thumbnail_generator_writes_the_name_it_reports(self, tmp_path, shell):
        work = self._tree(tmp_path)
        result = run_ps(shell, "generate_thumbnails.ps1",
                        ["-InputDir", str(work), "-Size", "32"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        for name in PERCENT_NAMES:
            assert (work / f"{name}_thumb.jpg").exists(), (
                f"{name}_thumb.jpg missing\n{result.stdout}"
            )

    def test_copy_exif_zip_writes_the_name_verbatim(self, tmp_path, shell):
        script = "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
        work = tmp_path / "work"
        work.mkdir()
        make_tiff_named(work, "50%off")
        make_tiff_named(work, "50%off", ".jpg")

        result = run_ps(shell, script, ["-InputDir", str(work), "-CompressZip",
                                        "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        out = work / "50%off.tif"
        assert out.exists()
        assert read_ifd0_compression(out).lower() in ("deflate", "adobe deflate")
        assert read_tag(out, "EXIF:Make") == "FujiTest"
        leftovers = [p.name for p in work.glob("*.tif") if p.name != "50%off.tif"]
        assert not leftovers, f"staged/mangled files left behind: {leftovers}"


class TestWriteLandedCheck:
    """magick exits 0 whether or not the file it was asked to write exists. Every worker has
    to confirm the write landed, or the next step (marker write, EXIF restore, staging move)
    operates on a file that is not there and the run reports WARN instead of failing."""

    @pytest.mark.parametrize("script,expected", [
        ("compress_tiff_zip.ps1", 6),          # 3 workers x (thumbnail branch + normal branch)
        ("copy_exif_to_TIFF_ps5.ps1", 1),
        ("copy_exif_to_TIFF_ps7.ps1", 1),
    ])
    def test_every_worker_checks_the_output_exists(self, script, expected):
        source = (REPO / script).read_text(encoding="ascii")
        assert source.count("wrote no file") == expected, (
            f"{script}: a magick write is not checked for having produced a file"
        )

    @pytest.mark.parametrize("script", PS_SCRIPTS)
    def test_output_paths_go_through_the_escaper(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        assert "function ConvertTo-MagickPathSpec" in source, (
            f"{script}: no '%' escaping for magick filename specs"
        )
        assert "-compress zip $writeDst 2>&1" not in source, (
            f"{script}: raw output path handed to magick again (must be escaped first)"
        )


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestToolOutputExclusionIsRelative:
    """Round 14 made -ExcludeFolders relative to the scan root in the three backends. The same
    absolute match survived in generate_thumbnails.ps1 and in the diagnose workflow: an
    archive under D:/Backup/zip/2019 reported "No TIFF files found" and exited 0, and
    pointing the tool AT a folder named ZIP could never work."""

    def test_thumbnails_work_inside_a_folder_named_zip(self, tmp_path, shell):
        zipdir = tmp_path / "ZIP"
        zipdir.mkdir()
        make_tiff(zipdir / "a.tif")

        result = run_ps(shell, "generate_thumbnails.ps1",
                        ["-InputDir", str(zipdir), "-Size", "32"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert (zipdir / "a_thumb.jpg").exists(), (
            f"a folder named ZIP hid its own contents\n{result.stdout}"
        )

    def test_nested_backups_are_still_skipped(self, tmp_path, shell):
        root = tmp_path / "PhotoSet"
        (root / "OLD_TIFFs").mkdir(parents=True)
        (root / "keep").mkdir()
        make_tiff(root / "OLD_TIFFs" / "backup.tif")
        make_tiff(root / "keep" / "source.tif")

        result = run_ps(shell, "generate_thumbnails.ps1",
                        ["-InputDir", str(root), "-Recursive", "-Size", "32"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert (root / "keep" / "source_thumb.jpg").exists(), result.stdout
        assert not (root / "OLD_TIFFs" / "backup_thumb.jpg").exists(), (
            "this tool's own backups are no longer skipped"
        )

    def test_zip_and_export_are_no_longer_excluded_by_default(self, tmp_path, shell):
        """`ZIP/` is where compress modes 1/3/5/6/7 PUT the compressed library and `_EXPORT`
        is what modes 6/7 exist to process. Excluding them by default meant "make thumbnails
        of my compressed library" silently produced nothing, with no flag to override it."""
        root = tmp_path / "PhotoSet"
        (root / "ZIP").mkdir(parents=True)
        (root / "_EXPORT").mkdir()
        make_tiff(root / "ZIP" / "compressed.tif")
        make_tiff(root / "_EXPORT" / "exported.tif")

        result = run_ps(shell, "generate_thumbnails.ps1",
                        ["-InputDir", str(root), "-Recursive", "-Size", "32"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert (root / "ZIP" / "compressed_thumb.jpg").exists(), result.stdout
        assert (root / "_EXPORT" / "exported_thumb.jpg").exists(), result.stdout

    def test_exclude_folders_puts_them_back(self, tmp_path, shell):
        root = tmp_path / "PhotoSet"
        (root / "ZIP").mkdir(parents=True)
        (root / "keep").mkdir()
        make_tiff(root / "ZIP" / "compressed.tif")
        make_tiff(root / "keep" / "source.tif")

        result = run_ps(shell, "generate_thumbnails.ps1",
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


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestThumbnailFrameMatching:
    """The frame lookup escaped its pattern with backticks and passed it to -Filter, which is
    the FileSystem provider's DOS filter: it has no escape syntax, so a basename containing
    '[' matched nothing. The frames were written, reported as "output not created", and
    regenerated (and re-reported) on every later run."""

    def test_bracket_name_with_page_all(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        safe = work / "brackets.tif"
        make_multipage_tiff(safe, [None, 1])
        safe.rename(work / "br[1].tif")

        first = run_ps(shell, "generate_thumbnails.ps1",
                       ["-InputDir", str(work), "-Page", "all", "-Size", "32"], tmp_path)
        assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
        assert "output not created" not in first.stdout, first.stdout
        frames = sorted(p.name for p in work.glob("br[[]1[]]_thumb-*.jpg"))
        assert len(frames) == 2, f"expected two frames, got {frames}\n{first.stdout}"

        second = run_ps(shell, "generate_thumbnails.ps1",
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


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestRound15Reporting:
    def test_thumbpage_above_one_is_reported_as_ignored(self, tmp_path, shell):
        """-ThumbPage was validated (>= 1) and then ignored: the thumbnail is always page 1."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "a.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "1", "-InputDir", str(work), "-GenerateThumbnail",
                         "-ThumbPage", "3", "-ThumbSize", "16", "-Workers", "1"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert "-ThumbPage 3 is not implemented" in result.stdout, result.stdout
        assert page_count(work / "ZIP" / "a.tif") == 2

    def test_dry_run_counts_what_it_would_do(self, tmp_path, shell):
        """Every dry run ended on "0 OK" because Process-Results had no DRY branch."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "a.tif")
        make_tiff(work / "b.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "1", "-InputDir", str(work), "-DryRun"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert "2 dry-run" in result.stdout, result.stdout

    def test_superseded_duplicate_is_reported(self, tmp_path, shell):
        """-DuplicateAction Overwrite dropped the earlier task without counting it, so the
        file was missing from the N/total tally -- indistinguishable from one that worked."""
        work = tmp_path / "work"
        (work / "a").mkdir(parents=True)
        (work / "b").mkdir()
        make_tiff(work / "a" / "photo.tif")
        make_tiff(work / "b" / "photo.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "2", "-InputDir", str(work),
                         "-OutputDir", str(tmp_path / "flat"),
                         "-DuplicateAction", "Overwrite", "-Workers", "1"], tmp_path)

        assert result.returncode == 0, result.stdout
        assert "superseded by a later duplicate" in result.stdout, result.stdout
        assert "2/2 processed" in result.stdout, result.stdout


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestInPlaceGuardCoversEveryMode:
    """The temp-sibling + verify guard was gated on `$mode -lt 0`. Mode 2 pointed at its own
    input root, and mode 5 for a file already inside a ZIP/ folder, both resolve
    finalDst == srcPath and went straight to `magick src -compress zip src`."""

    def test_mode2_into_its_own_root(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        src = make_tiff(work / "a.tif")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "2", "-InputDir", str(work), "-OutputDir", str(work),
                         "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_ifd0_compression(src).lower() in ("deflate", "adobe deflate")
        assert read_tag(src) == ARTIST, "in-place rewrite lost the metadata"
        assert rmse_pages(reference, src) == "0 (0)"
        assert sorted(p.name for p in work.glob("*")) == ["a.tif"], (
            "a temp sibling was left behind"
        )

    def test_mode5_file_already_in_a_zip_folder(self, tmp_path, shell):
        work = tmp_path / "work"
        (work / "ZIP").mkdir(parents=True)
        src = make_tiff(work / "ZIP" / "a.tif")
        reference = tmp_path / "reference.tif"
        shutil.copy2(src, reference)

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "5", "-InputDir", str(work), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert read_ifd0_compression(src).lower() in ("deflate", "adobe deflate")
        assert rmse_pages(reference, src) == "0 (0)"
        assert sorted(p.name for p in (work / "ZIP").glob("*")) == ["a.tif"]


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestPercentSurvivesExiftool:
    """The '%' work in this round covered ImageMagick's filename SPECS. exiftool expands the
    SAME kind of code in the `-tagsfromfile` ARGUMENT -- `50%off.tif` is read as `50ff.tif` --
    and unlike magick it warns and STILL EXITS 0. Every caller checked only the exit code, so
    the run reported OK while the output carried no metadata at all. In mode 8 the source is
    deleted right afterwards, which made it permanent.

    Doubling does not escape it (`50%%off.jpg` -> `50%ff.jpg`), so these tests assert on the
    TAG at the destination, never on the exit code.
    """

    def test_mode9_keeps_metadata(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff_named(work, "50%off")
        make_tiff_named(work, "plain")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "9", "-InputDir", str(work), "-Workers", "2"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        for name in ("50%off", "plain"):
            out = work / f"{name}.tif"
            assert read_ifd0_compression(out).lower() in ("deflate", "adobe deflate")
            assert read_tag(out) == ARTIST, (
                f"{name}.tif came out with no metadata\n{result.stdout}"
            )

    def test_mode8_delete_keeps_metadata(self, tmp_path, shell):
        """The permanent case: mode 8 removes the only other copy."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff_named(work, "50%off", ".tiff")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "8", "-InputDir", str(work), "-DeleteSource",
                         "-StagingDir", str(tmp_path / "staging"), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        out = work / "50%off.tif"
        assert out.exists(), result.stdout
        assert read_tag(out) == ARTIST, (
            f"mode 8 deleted the source and kept no metadata\n{result.stdout}"
        )

    def test_copy_exif_reads_a_percent_jpeg(self, tmp_path, shell):
        """Same trap on the OTHER side: the JPEG is the -tagsfromfile source here."""
        script = "copy_exif_to_TIFF_ps5.ps1" if shell == "powershell" else "copy_exif_to_TIFF_ps7.ps1"
        work = tmp_path / "work"
        work.mkdir()
        make_tiff_named(work, "50%off")
        make_tiff_named(work, "50%off", ".jpg")

        result = run_ps(shell, script, ["-InputDir", str(work), "-CompressZip",
                                        "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        out = work / "50%off.tif"
        assert read_ifd0_compression(out).lower() in ("deflate", "adobe deflate")
        assert read_tag(out, "EXIF:Make") == "FujiTest", (
            f"the EXIF never made it out of the '%' JPEG\n{result.stdout}"
        )


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestBackupsAreNeverInput:
    """OLD_TIFFs holds the PRISTINE originals modes 0/9 archived. Only legacy, mode 8 and the
    modes 0/9 builder ever excluded it, each with its own ABSOLUTE regex, so modes 1-7 walked
    in and compressed the backups. The real file is already Deflate and was SKIPped, which left
    the BACKUP as the only candidate -- it took the output name, or lost the collision and
    landed as `photo_v2.tif`."""

    @pytest.mark.parametrize("mode", ["2", "3", "5"])
    def test_recursive_modes_ignore_old_tiffs(self, tmp_path, shell, mode):
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "a.tif")
        run_ps(shell, "compress_tiff_zip.ps1",
               ["-Mode", "9", "-InputDir", str(work), "-Workers", "1"], tmp_path)
        backup = work / "OLD_TIFFs" / "a.tif"
        assert backup.exists(), "mode 9 did not archive the original"
        before = backup.read_bytes()

        args = ["-Mode", mode, "-InputDir", str(work), "-Workers", "1"]
        if mode == "2":
            args += ["-OutputDir", str(tmp_path / "flat")]
        result = run_ps(shell, "compress_tiff_zip.ps1", args, tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert backup.read_bytes() == before, "the backup was recompressed"
        strays = [p.name for p in work.rglob("*_v2.tif")] + \
                 [p.name for p in (tmp_path / "flat").rglob("*_v2.tif")] if (tmp_path / "flat").exists() \
                 else [p.name for p in work.rglob("*_v2.tif")]
        assert not strays, f"a backup was compressed under a _v2 name: {strays}\n{result.stdout}"
        assert not list((work / "OLD_TIFFs").glob("ZIP/*")), (
            f"output was written INSIDE the backup folder\n{result.stdout}"
        )

    def test_pointing_at_old_tiffs_still_works(self, tmp_path, shell):
        """Relative matching, not absolute: the round 14 rule. An explicit -InputDir wins."""
        work = tmp_path / "work" / "OLD_TIFFs"
        work.mkdir(parents=True)
        make_tiff(work / "a.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert (work / "ZIP" / "a.tif").exists(), (
            f"an explicitly chosen OLD_TIFFs folder was refused\n{result.stdout}"
        )

    def test_leftovers_from_an_interrupted_run_are_not_photos(self, tmp_path, shell):
        """Both shapes this toolkit writes and may leave behind: a staged file (copy_exif with
        no -StagingDir stages into the source folder) and an in-place temp sibling."""
        work = tmp_path / "work"
        work.mkdir()
        make_tiff(work / "a.tif")
        make_tiff(work / "1a2b3c4d_0123456789ab_a.tif")
        make_tiff(work / "a.tif.deadbeef_cafe12345678.tif")

        result = run_ps(shell, "compress_tiff_zip.ps1",
                        ["-Mode", "1", "-InputDir", str(work), "-Workers", "1"], tmp_path)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "Skipped 2 staged/temp file(s)" in result.stdout, result.stdout
        assert sorted(p.name for p in (work / "ZIP").glob("*")) == ["a.tif"], (
            f"a leftover was compressed as if it were a photo\n{result.stdout}"
        )


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestThumbnailPageSwitch:
    """`-Page all` writes name_thumb-0.jpg, name_thumb-1.jpg, ...; `-Page 0` writes
    name_thumb.jpg. Test-ThumbExists looked for the frame set unconditionally, so once a
    `-Page all` run had happened, `-Page 0` reported SKIP (exists) for every file forever and
    name_thumb.jpg could only be produced by deleting the frames by hand."""

    def test_page_zero_after_page_all(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        make_multipage_tiff(work / "scan.tif", [None, 1])

        first = run_ps(shell, "generate_thumbnails.ps1",
                       ["-InputDir", str(work), "-Size", "32", "-Page", "all"], tmp_path)
        assert first.returncode == 0, first.stdout
        assert (work / "scan_thumb-0.jpg").exists(), first.stdout

        second = run_ps(shell, "generate_thumbnails.ps1",
                        ["-InputDir", str(work), "-Size", "32", "-Page", "0"], tmp_path)
        assert second.returncode == 0, second.stdout
        assert (work / "scan_thumb.jpg").exists(), (
            f"-Page 0 was skipped because the -Page all frames existed\n{second.stdout}"
        )

    def test_page_all_still_no_ops_on_a_repeat(self, tmp_path, shell):
        work = tmp_path / "work"
        work.mkdir()
        make_multipage_tiff(work / "scan.tif", [None, 1])

        run_ps(shell, "generate_thumbnails.ps1",
               ["-InputDir", str(work), "-Size", "32", "-Page", "all"], tmp_path)
        again = run_ps(shell, "generate_thumbnails.ps1",
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

    def test_escaper_matches_the_powershell_rule(self):
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


class TestAuditRound16InputRootsAreNormalised:
    """`Join-Path $PWD.Path "."` leaves a trailing `\\.` (and `a\\..\\b` keeps its `..`),
    which broke Test-PathExcluded's root compare: files directly under the root fell
    into its absolute-path fallback, so an -ExcludeFolders entry matching the root's
    OWN name excluded the whole run."""

    def test_roots_are_normalised_before_they_reach_the_exclusion_check(self):
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        normalise = "try { $dir = [System.IO.Path]::GetFullPath($dir) }"
        assert normalise in source, "input roots are no longer normalised"
        excluded = "Test-PathExcluded -Directory $df.DirectoryName -Root $inputRoot"
        assert excluded in source
        assert source.index(normalise) < source.index(excluded), (
            "GetFullPath must run in the -InputDir root loop, before Test-PathExcluded "
            "ever sees the root"
        )


class TestAuditRound16InterruptRollback:
    """Modes 0/9 move originals into OLD_TIFFs/ before the verified ZIP lands; the
    per-group rollback only runs on the normal exit path, so Ctrl+C used to strand
    them there. The trap now walks $script:movedOriginals and puts back any original
    whose output never landed (or does not decode)."""

    def test_moved_originals_are_tracked(self):
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        assert "$script:movedOriginals = [System.Collections.ArrayList]::new()" in source
        assert "[void]$script:movedOriginals.Add(" in source, (
            "mode 0/9 moves no longer register the original for the interrupt trap"
        )

    def test_trap_restores_stranded_originals(self):
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        trap = source[source.index("trap {"):source.index("# Detect PowerShell version")]
        assert "foreach ($mv in @($script:movedOriginals.ToArray()))" in trap, (
            "the interrupt trap does not walk the moved-originals list"
        )
        assert "Move-Item -LiteralPath $mv.Backup -Destination $mv.Original" in trap
        assert "RESTORED (interrupt cleanup)" in trap

    def test_trap_keeps_an_original_only_when_its_output_decodes(self):
        """A partial/corrupt FinalDst must not veto the restore."""
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        trap = source[source.index("trap {"):source.index("# Detect PowerShell version")]
        assert "Test-ZipIntegrity -Path $mv.FinalDst" in trap, (
            "the trap trusts the mere EXISTENCE of the output again"
        )


class TestAuditRound16SkipRollbackIsVerified:
    """"SKIP rollback (ZIP exists)" used to fire on the mere existence of FinalDst, so a
    partial/corrupt write vetoed the rollback and stranded the original in OLD_TIFFs/.
    Both rollback copies (PS7 parallel and PS5 sequential) now decode the output first."""

    def test_skip_rollback_is_gated_on_zip_integrity(self):
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        gated = re.findall(
            r"Test-ZipIntegrity -Path \$t\.FinalDst[^}]*?"
            r'Write-Log "SKIP rollback \(ZIP exists\)',
            source, re.S,
        )
        assert len(gated) == 2, (
            "expected the integrity gate ahead of BOTH 'SKIP rollback (ZIP exists)' "
            f"branches, found {len(gated)}"
        )
        corrupt = re.findall(
            r'SKIP rollback \(ZIP exists\).{0,400}?'
            r'Remove-Item -LiteralPath \$t\.FinalDst',
            source, re.S,
        )
        assert len(corrupt) == 2, (
            "an output that fails the integrity check must be deleted before the "
            "original is restored"
        )


class TestAuditRound16OutputDirAndPageCount:
    """A RELATIVE -OutputDir was re-based on EACH input root by Resolve-Output, so
    '-InputDir A;B -OutputDir out' scattered one flat output across A\\out and B\\out.
    It is resolved ONCE against $PWD before the task loop. Separately: modes 1-7 ship
    without a pixel gate, so a rewrite that silently dropped pages reported OK -- a
    page-count shrink is now an ERROR."""

    def test_relative_output_dir_is_resolved_once_against_pwd(self):
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        resolved = "$OutputDir = [System.IO.Path]::GetFullPath((Join-Path $PWD.Path $OutputDir))"
        assert resolved in source, "a relative -OutputDir is re-based per input root again"
        assert source.count(resolved) == 1
        # The resolution must sit between the root loop and the task loop.
        assert source.index("$inputRoots += $dir") < source.index(resolved), (
            "-OutputDir must be resolved AFTER the roots are known..."
        )
        assert source.index(resolved) < source.index("$script:total = 0"), (
            "...and BEFORE the task loop runs"
        )

    def test_page_count_shrink_is_an_error_for_modes_1_to_7(self):
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        gate = "if (-not $generateThumb -and $mode -ge 1 -and $mode -le 7) {"
        assert gate in source, "the page-count shrink gate lost its mode range"
        assert 'ERROR (page count shrink)' in source, (
            "a silent page drop is no longer reported as an ERROR"
        )
        assert source.index(gate) < source.index('ERROR (page count shrink)'), (
            "the ERROR must be emitted from inside the modes 1-7 gate"
        )


class TestAuditRound16CopyExifRestoreGates:
    """The staging mapping was registered BEFORE the metadata/subfiletype restore gates,
    so a staged ZIP whose restore failed was moved ON TOP of the EXIF-bearing original
    and reported as a mere WARN (exit 0). A failed restore is now an ERROR that leaves
    the original untouched, and the JPEG->TIFF copy no longer passes -unsafe (it stamped
    the JPEG's geometry onto a TIFF whose dimensions legitimately differ)."""

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_failed_restore_is_an_error_and_leaves_the_original_untouched(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        assert "ERROR (exiftool metadata restore failed - original untouched)" in source
        assert "ERROR (subfiletype restore failed - original untouched)" in source

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_staging_mapping_is_registered_after_the_restore_gates(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        meta_err = source.index("ERROR (exiftool metadata restore failed - original untouched)")
        sub_err = source.index("ERROR (subfiletype restore failed - original untouched)")
        assert meta_err < sub_err, "the subfiletype gate must run after the metadata gate"
        if script.endswith("ps5.ps1"):
            registered = source.index("$script:stagingMap[$p.Tiff] = $stagingName")
            assert registered > sub_err, (
                "the staging mapping is registered before the restore gates again"
            )
        else:
            # PS7 hands the staging name back on the result object; the error results
            # must carry StagingName = $null so the move loop never sees the file.
            for line in source.splitlines():
                if "restore failed - original untouched" in line:
                    assert "StagingName = $null" in line, line

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_jpeg_copy_block_has_no_bare_unsafe(self, script):
        source = (REPO / script).read_text(encoding="ascii")
        start = source.index("$tagsArgs = @('-tagsfromfile'")
        end = source.index(")", start)
        block = source[start:end]
        assert "'-unsafe'" not in block, (
            f"{script}: -unsafe crept back into the JPEG->TIFF tagsfromfile copy"
        )
        assert "--ImageWidth" in block and "--Orientation" in block, (
            f"{script}: the geometry tags must be excluded explicitly instead"
        )


class TestAuditRound16ThumbnailFrameDigits:
    """Test-ThumbExists matched `name_thumb-*.jpg`, so `name_thumb-copy.jpg` (or any
    non-frame sibling) counted as the frame set and -Page all reported SKIP (exists)
    forever. The middle of a frame name must be all digits; the check exists in three
    places (the shared helper and both shell paths)."""

    def test_frame_existence_check_requires_digits_three_times(self):
        source = (REPO / "generate_thumbnails.ps1").read_text(encoding="ascii")
        count = source.count("-match '^\\d+$'")
        assert count == 3, f"expected the digit check in 3 places, found {count}"


class TestAuditRound16PurgeMetadataGate:
    """The purge verified PIXELS only, so a conversion that dropped metadata (a lost
    per-page SubfileType, an EXIF block, an ICC profile) purged the last good backup.
    _compare_tiff_metadata now refuses unless the metadata signatures match too."""

    @staticmethod
    def _fake_run(subfile_by_name):
        def fake_run(cmd, **kwargs):
            cmd = [str(a) for a in cmd]
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
