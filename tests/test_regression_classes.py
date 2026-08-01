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
        assert source.count("$metaBackup = Backup-TiffMetadata") == 3, (
            "every worker that can write in place must park the metadata first"
        )
        # ...and every one of them must restore FROM the backup, not from the overwritten source
        assert source.count("$tagSource = if ($metaBackup) { $metaBackup } else {") == 3

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
