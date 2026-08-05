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
    """Invoke a backend script and return the CompletedProcess (exit code included)."""
    cmd = [shell, "-NoProfile", "-File", str(REPO / script)] + [str(a) for a in args]
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
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
    The v2.5 exit-code fix landed in compress_tiff_zip.ps1 and generate_thumbnails.ps1 and was
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


def rmse_pages(path_a, path_b, page=0):
    """RMSE of one page pair; '0 (0)' means pixel-identical."""
    result = subprocess.run(
        ["magick", "compare", "-metric", "RMSE", f"{path_a}[{page}]", f"{path_b}[{page}]", "null:"],
        capture_output=True, text=True, timeout=120,
    )
    return (result.stderr or result.stdout).strip()


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
class TestMultiPageCompressE2E:
    """The CRITICAL v2.4 class (multi-page TIFF stranded in OLD_TIFFs) and the
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
            r"\(Test-PixelIdentical -SrcPath \$srcPath -DstPath \$verify\w+", source
        )
        assert len(mode8_gates) == 2, (
            "sequential and -Parallel workers must both pixel-verify before delete, "
            f"found {len(mode8_gates)}"
        )
        assert source.count("final ZIP failed integrity - source preserved") == 2, (
            "both delete loops must re-verify the final destination before Remove-Item"
        )

    def test_legacy_in_place_writes_go_through_verified_temp_sibling(self):
        """Legacy mode (-1) wrote ZIP directly over the only copy; a magick crash destroyed
        the image. Both legacy workers must write a temp sibling and replace atomically."""
        source = (REPO / "compress_tiff_zip.ps1").read_text(encoding="ascii")
        assert source.count("$inPlaceFinalDst = $writeDst") == 2, (
            "Process-TiffJob and the legacy -Parallel worker must both redirect in-place writes"
        )
        assert source.count("Move-Item -LiteralPath $writeDst -Destination $inPlaceFinalDst") == 2
        # Decode alone is not enough before replacing the ONLY copy: a page-short file still
        # decodes cleanly (that is how -GenerateThumbnail silently dropped a scanner IR page
        # in place). Both in-place workers must pixel-compare too, like mode 8 does.
        assert source.count("$inPlaceOk = Test-PixelIdentical") == 2, (
            "both in-place workers must pixel-verify, not just decode-check, before replacing"
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
    string delimiter. One non-ASCII character stopped a whole script from parsing (v2.4).
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

        assert "Excluded 1 file(s)" in result.stdout, result.stdout
        assert "a.tif" in result.stdout
        assert "b.tif" not in result.stdout, "_EXPORT tree was not excluded"

    @pytest.mark.parametrize("script", ["copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"])
    def test_segment_regex_covers_both_separators(self, script):
        """A '[\\/]' class (one backslash) matches only '/', so a Windows path would slip
        through the "names, not paths" guard and the segment split."""
        source = (REPO / script).read_text(encoding="ascii")
        assert r"-match '[\\/]'" in source, f"{script}: separator class lost a backslash"
        assert r"-split '[\\/]'" in source, f"{script}: separator class lost a backslash"


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
