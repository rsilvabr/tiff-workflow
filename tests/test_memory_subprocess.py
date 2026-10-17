"""audit round 18: memory caps, thread-free subprocess capture, load-scaled timeouts.

The wizard used to run up to 64 ImageMagick workers with nothing but the user's
judgement between them and the RAM: a real `magick compare` peaked at 5.35 GiB
(68 B/px) on an 84 MP scan, and `capture_output=True` starts two reader threads
per subprocess call -- under memory exhaustion a thread's bootstrap can fail with
MemoryError before it signals "started", and `Thread.start()` then waits forever,
before the timeout is armed. These tests pin:

  T1  every subprocess call in convert_tiff.py goes through `_run_captured`,
      except the two version probes and the streaming `run_subprocess`;
  T2  a real run starts no reader thread;
  T3  `_run_captured` decodes UTF-8/newlines and forwards the timeout;
  T4  the worker cap arithmetic and its "cannot read memory" fail-open;
  T5  the pixel estimate comes from the real files (unknown size = larger);
  T6  `_work_timeout` scales with the number of workers sharing the disk;
  T7  run_diagnose_tiffs calls the cap and runs the pool with the result;
  T8-T10 the backends cap by memory and accept the parameter on PS5;
  the parity module (test_ps_helper_parity.py) pins Test-PixelIdentical across
  its three copies and the -LoadFactor call sites.
"""

import ast
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import convert_tiff as ct
from test_regression_classes import IMAGE_TOOLS, SHELLS, make_tiff, read_tag, run_ps, run_ps_switched

REPO = Path(__file__).resolve().parent.parent

requires_tools = pytest.mark.skipif(
    not IMAGE_TOOLS, reason="ImageMagick (magick) and exiftool required"
)
requires_pwsh = pytest.mark.skipif("pwsh" not in SHELLS, reason="needs pwsh")
requires_ps5 = pytest.mark.skipif("powershell" not in SHELLS, reason="needs powershell")


# ---------------------------------------------------------------------------
# T1 - no subprocess call outside the capture helper
# ---------------------------------------------------------------------------

_SUBPROCESS_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}
# The only functions allowed to reach subprocess directly: the capture helper
# itself, the two version probes (run once per executable, documented exception)
# and the streaming Popen of run_subprocess (reader thread created once at
# process start, no memory pressure -- documented exception).
_ALLOWED_OWNERS = {"_run_captured", "detect_powershell_version", "_probe_version",
                   "run_subprocess"}


def _subprocess_call_owners(node, current):
    """Yield (innermost function name, lineno) for every `subprocess.<x>()`."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _subprocess_call_owners(child, child.name)
            continue
        if isinstance(child, ast.Call):
            f = child.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "subprocess"
                    and f.attr in _SUBPROCESS_ATTRS):
                yield current or "<module>", child.lineno
        yield from _subprocess_call_owners(child, current)


def test_no_subprocess_call_outside_the_capture_helper():
    """T1: every subprocess.run/Popen/call/check_output/check_call in
    convert_tiff.py lives inside _run_captured, the two version probes or
    run_subprocess. The nested _kill_tree is NOT an exception: it goes through
    _run_captured too."""
    tree = ast.parse((REPO / "convert_tiff.py").read_text(encoding="utf-8"))
    offenders = [(owner, lineno)
                 for owner, lineno in _subprocess_call_owners(tree, None)
                 if owner not in _ALLOWED_OWNERS]
    assert not offenders, (
        "convert_tiff.py: subprocess call(s) outside _run_captured "
        "(and the documented probes):\n"
        + "\n".join(f"  line {lineno}: {owner}()" for owner, lineno in offenders))


# ---------------------------------------------------------------------------
# T2 - a real run starts no reader thread
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows reader threads")
def test_run_captured_starts_no_thread(monkeypatch):
    """T2: capture_output=True starts a `_readerthread` per stream on Windows;
    `_run_captured` writes to temp files instead, so starting ANY thread fails."""
    if shutil.which("magick") is None:
        pytest.skip("needs magick")

    def guard_start(self, *args, **kwargs):
        raise AssertionError("a reader thread was started")

    monkeypatch.setattr(threading.Thread, "start", guard_start)
    r = ct._run_captured(["magick", "-version"], 30, text=True)
    assert r.returncode == 0
    assert "ImageMagick" in r.stdout


# ---------------------------------------------------------------------------
# T3 - text decoding and timeout propagation
# ---------------------------------------------------------------------------

def test_run_captured_text_and_timeout():
    """T3: text=True decodes (UTF-8, CRLF normalised); a timeout still raises
    subprocess.TimeoutExpired out of the helper's internal subprocess.run.

    The child disables its own newline translation (reconfigure(newline='')):
    without it, Windows converts the '\\n' of 'a\\r\\nb' to '\\r\\n' and the
    child emits 'a\\r\\r\\nb' -- the helper cannot be expected to guess that."""
    code = ("import sys; sys.stdout.reconfigure(newline=''); "
            "sys.stdout.write('a\\r\\nb'); sys.stderr.write('e')")
    r = ct._run_captured([sys.executable, "-c", code], 30, text=True)
    assert r.returncode == 0
    assert r.stdout == "a\nb"
    assert r.stderr == "e"

    with pytest.raises(subprocess.TimeoutExpired):
        ct._run_captured([sys.executable, "-c", "import time; time.sleep(30)"], 1)


# ---------------------------------------------------------------------------
# T4 - cap arithmetic
# ---------------------------------------------------------------------------

def test_pool_cap_arithmetic(monkeypatch):
    """T4: the cap is fraction * available // (pixels * bpp); a disabled cap
    never even reads the memory, and unknown memory does not cap."""
    gib = 2 ** 30
    monkeypatch.setattr(ct, "_available_memory_bytes", lambda: (40 * gib, "physical"))
    monkeypatch.setattr(ct, "_max_image_pixels", lambda paths: 84_200_000)
    monkeypatch.setattr(ct, "WORKER_MEMORY_FRACTION", 0.8)
    assert ct._memory_capped_pool_workers(16, ["x"], 75, "t") == 5

    # fraction 0 disables the cap -- and must not read the memory at all
    monkeypatch.setattr(ct, "WORKER_MEMORY_FRACTION", 0)

    def boom():
        raise AssertionError("available memory was read although the cap is disabled")

    monkeypatch.setattr(ct, "_available_memory_bytes", boom)
    assert ct._memory_capped_pool_workers(16, ["x"], 75, "t") == 16

    # unreadable memory leaves the request alone
    monkeypatch.setattr(ct, "WORKER_MEMORY_FRACTION", 0.8)
    monkeypatch.setattr(ct, "_available_memory_bytes", lambda: (None, None))
    assert ct._memory_capped_pool_workers(16, ["x"], 75, "t") == 16

    # 1 worker is already the floor
    monkeypatch.setattr(ct, "_available_memory_bytes", lambda: (40 * gib, "physical"))
    assert ct._memory_capped_pool_workers(1, ["x"], 75, "t") == 1


# ---------------------------------------------------------------------------
# T5 - the pixel estimate from real files
# ---------------------------------------------------------------------------

@requires_tools
def test_max_image_pixels_real(tmp_path):
    """T5: the largest IFD0 area wins (a non-ASCII name included), and a file
    exiftool does not report counts as _UNKNOWN_IMAGE_PIXELS -- never smaller."""
    a = tmp_path / "a.tif"
    b = tmp_path / "foto \u00e7.tif"
    subprocess.run(["magick", "-size", "400x300", "xc:gray", "-depth", "16", str(a)],
                   capture_output=True, check=True, timeout=120)
    subprocess.run(["magick", "-size", "200x100", "xc:gray", "-depth", "16", str(b)],
                   capture_output=True, check=True, timeout=120)

    assert ct._max_image_pixels([a, b]) == 120000
    assert ct._max_image_pixels([a, b, tmp_path / "missing.tif"]) == ct._UNKNOWN_IMAGE_PIXELS


# ---------------------------------------------------------------------------
# T6 - timeouts scale with the workers sharing the disk
# ---------------------------------------------------------------------------

def test_work_timeout_scales_with_load(tmp_path, monkeypatch):
    """T6: base + size_mb * TIMEOUT_S_PER_MB, multiplied by the pool's worker
    count (_TIMEOUT_LOAD); a single worker keeps the round-16 formula."""
    f = tmp_path / "big.tif"
    with open(f, "wb") as fh:
        fh.truncate(4 * 1024 * 1024)

    monkeypatch.setattr(ct, "_TIMEOUT_LOAD", 1)
    assert ct._work_timeout(60, f) == pytest.approx(60 + 4 * ct.TIMEOUT_S_PER_MB)
    monkeypatch.setattr(ct, "_TIMEOUT_LOAD", 8)
    assert ct._work_timeout(60, f) == pytest.approx(60 + 4 * ct.TIMEOUT_S_PER_MB * 8)


# ---------------------------------------------------------------------------
# T7 - the diagnose pool budgets memory per file instead of lowering the workers
# ---------------------------------------------------------------------------

@requires_tools
def test_diagnose_uses_a_memory_budget(tmp_path, monkeypatch):
    """T7 (v2.7.1): run_diagnose_tiffs asks the memory budget with the compare
    bytes-per-pixel and runs the pool with the REQUESTED workers. Before, one 220 MP
    scan in a batch of 32 MP photos sized every worker for 220 MP (4 -> 3 workers on
    a machine with plenty of free RAM); each check now reserves its own estimate."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_tiff(in_dir / "a.tif")
    make_tiff(in_dir / "b.tif")

    calls = []
    real_budget = ct._memory_budget

    def spy_budget(paths, bytes_per_pixel, what):
        calls.append(([str(p) for p in paths], bytes_per_pixel, what))
        return real_budget(paths, bytes_per_pixel, what)

    monkeypatch.setattr(ct, "_memory_budget", spy_budget)
    monkeypatch.setattr(ct, "_memory_capped_pool_workers",
                        lambda *a, **k: pytest.fail("Diagnose lowered the worker count again"))
    monkeypatch.setattr(ct, "_is_real_16bit", lambda *a, **k: (True, 0.5, "real"))

    seen_workers = []
    real_pool = ct.concurrent.futures.ThreadPoolExecutor

    class SpyPool(real_pool):
        def __init__(self, *args, **kwargs):
            seen_workers.append(kwargs.get("max_workers", args[0] if args else None))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ct.concurrent.futures, "ThreadPoolExecutor", SpyPool)

    # folder, scope ('-' = everywhere), skip names ('-' = none), workers, temp dir,
    # compress, preset converts?
    answers = iter([str(in_dir), "-", "-", "4", "", "1", "n"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    cfg = SimpleNamespace(config=ct.ToolConfig(), save_config=lambda: None)
    assert ct.run_diagnose_tiffs(cfg) is True

    assert calls, "the memory budget was never consulted"
    paths, bpp, what = calls[0]
    assert bpp == ct._COMPARE_BYTES_PER_PIXEL
    assert sorted(Path(p).name for p in paths) == ["a.tif", "b.tif"]
    assert seen_workers == [4], "the pool must keep the requested workers"


# ---------------------------------------------------------------------------
# T8-T10 - the backends
# ---------------------------------------------------------------------------

def _logs(cwd, script_name, result):
    """stdout plus every log file the run wrote under cwd/Logs/<script_name>."""
    text = result.stdout or ""
    for log in Path(cwd, "Logs", script_name).glob("*.log"):
        text += log.read_text(encoding="utf-8", errors="replace")
    return text


@requires_pwsh
@requires_tools
def test_compress_caps_workers_by_memory(tmp_path):
    """T8: a tiny -WorkerMemoryFraction forces the cap; the run logs the
    reduction, exits 0 and still writes both outputs."""
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_tiff(in_dir / "a.tif")
    make_tiff(in_dir / "b.tif")

    result = run_ps("pwsh", "compress_tiff_zip.ps1",
                    ["-Mode", "3", "-InputDir", str(in_dir), "-Workers", "8",
                     "-WorkerMemoryFraction", "0.000001"], tmp_path)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    log = _logs(tmp_path, "compress_tiff_zip", result)
    assert "Workers reduced from 8 to 1" in log, log
    outputs = sorted((in_dir / "ZIP").glob("*.tif"))
    assert [p.name for p in outputs] == ["a.tif", "b.tif"], log


@requires_pwsh
@requires_tools
def test_copy_exif_ps7_caps_workers_by_memory(tmp_path):
    """T9: the ps7 backend caps its -Parallel pool the same way, and the EXIF
    copy still lands on the TIFF."""
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

    result = run_ps("pwsh", "copy_exif_to_TIFF_ps7.ps1",
                    ["-InputDir", str(work), "-Workers", "8",
                     "-WorkerMemoryFraction", "0.000001"], tmp_path)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    log = _logs(tmp_path, "Copy-S5Pro-Exif", result)
    assert "Workers reduced from 8 to 1" in log, log
    assert read_tag(work / "photo.tif", "EXIF:Make") == "FUJIFILM"
    assert read_tag(work / "photo.tif", "EXIF:Model") == "FinePix S5Pro"


@requires_ps5
@requires_tools
def test_ps5_accepts_the_memory_fraction(tmp_path):
    """T10: the PS5 script accepts -WorkerMemoryFraction for interface parity
    (it is sequential and ignores the value); the exit code must not change."""
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

    base = ["-InputDir", str(work)]
    without = run_ps_switched("powershell", "copy_exif_to_TIFF_ps5.ps1", base, tmp_path)
    with_param = run_ps_switched("powershell", "copy_exif_to_TIFF_ps5.ps1",
                                 base + ["-WorkerMemoryFraction", "0.5"], tmp_path)

    assert with_param.returncode == without.returncode, (
        f"without: {without.returncode}\n{without.stdout}\n{without.stderr}\n"
        f"with: {with_param.returncode}\n{with_param.stdout}\n{with_param.stderr}")
    # The exit-code half cannot fail pre-fix: PowerShell routes an unknown named
    # parameter into $args for a non-advanced script, so the old copy "accepted"
    # it silently. The declaration in the param() block is what the interface
    # parity is about -- without this, the test proves nothing about the fix.
    assert "WorkerMemoryFraction" in ct._ps_param_defaults("copy_exif_to_TIFF_ps5.ps1")


# ---------------------------------------------------------------------------
# R18-1 (review): the estimate must read non-ASCII paths
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe and its OEM code page")
@requires_pwsh
@requires_tools
def test_compress_memory_estimate_reads_non_ascii_paths(tmp_path):
    """R18-1: exiftool prints UTF-8 (-charset filename=utf8), but PowerShell decodes
    a native command's output with [Console]::OutputEncoding -- the OEM code page
    (850) in a scheduled `cmd /k`. A folder name with an accent then never matched
    and every file counted as UnknownImagePixels (150 MP), dwarfing the real cost.

    Driven through a cp850 .cmd so the console code page is 850 even though pytest's
    own console may differ. 400x300 = 0.12 MP must format as "(0 MP)". """
    in_dir = tmp_path / "Sess\u00e3o"
    in_dir.mkdir()
    tiff = in_dir / "foto.tif"
    subprocess.run(["magick", "-size", "400x300", "xc:gray", "-depth", "16", str(tiff)],
                   capture_output=True, check=True, timeout=120)

    cmd_file = tmp_path / "run_compress.cmd"
    script = REPO / "compress_tiff_zip.ps1"
    cmd_file.write_bytes(("\r\n".join([
        "@echo off",
        "chcp 850 >nul",
        f'cd /d "{tmp_path}"',
        f'pwsh -NoProfile -NonInteractive -File "{script}" -Mode 3 '
        f'-InputDir "{in_dir}" -Workers 4 -WorkerMemoryFraction 0.8',
        "exit /b %ERRORLEVEL%",
    ]) + "\r\n").encode("cp850"))

    result = subprocess.run(["cmd", "/c", str(cmd_file)], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=300)
    text = (result.stdout.decode("utf-8", "replace")
            + result.stderr.decode("utf-8", "replace"))
    for log in (tmp_path / "Logs" / "compress_tiff_zip").glob("*.log"):
        text += log.read_text(encoding="utf-8", errors="replace")

    mem_lines = [ln.strip() for ln in text.splitlines() if "Memory:" in ln]
    assert mem_lines, f"the memory estimate was never logged:\n{text}"
    assert "150 MP" not in mem_lines[0], (
        "the accented path did not match exiftool's output: every file was counted "
        f"as {ct._UNKNOWN_IMAGE_PIXELS} px:\n{mem_lines[0]}")
    assert "(0 MP)" in mem_lines[0], (
        f"400x300 = 0.12 MP must format as (0 MP):\n{mem_lines[0]}")
