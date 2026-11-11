"""A real Ctrl+C, in the middle of the work.

The backends promise that an interrupted run leaves no photo out of its place and none of
its own staged or temp files behind; the wizard promises that Ctrl+C stops the backend it
started and everything that backend started. Neither path had a test: nothing in the suite
interrupted a run.

How a Ctrl+C is delivered without touching the console the suite runs in: the program under
test is started with a console of its own (hidden); a helper process attaches to THAT
console and raises CTRL_C_EVENT there -- what the key does, for every process on it.

To have the interrupt land mid-conversion, a `magick` in front of PATH does the real
conversion and then does not return (the "slow" file), so the backend is waiting on it with
a finished staged file on disk.

Windows only (console control events). The paths have Japanese characters, [ ] and a
space, but no '%': the stand-in magick is a .cmd, and cmd.exe expands '%' in its arguments.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tiff_workflow_common as tw  # noqa: E402
from test_regression_classes import make_tiff, read_compression  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HOSTILE = "190815_Film_伊豆沼 [FINAL] x"
TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None

pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="console control events are Windows'"),
]
needs_tools = pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required")

# Started with its own console; Ctrl+C is switched back ON for it first (a parent that was
# itself started in a new process group passes "ignore Ctrl+C" down to its children).
_RUN_SCRIPT = (
    "import ctypes, runpy, sys\n"
    "ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)

_SEND_CTRL_C = (
    "import ctypes, sys, time\n"
    "k = ctypes.windll.kernel32\n"
    "k.FreeConsole()\n"
    "if not k.AttachConsole(int(sys.argv[1])):\n"
    "    sys.exit(3)\n"
    "k.SetConsoleCtrlHandler(None, True)\n"       # not for this helper
    "k.GenerateConsoleCtrlEvent(0, 0)\n"          # CTRL_C_EVENT, every process on that console
    "time.sleep(0.3)\n"
)

_SLOW_MAGICK = r'''import os, subprocess, sys, time
args = sys.argv[1:]
r = subprocess.run([os.environ["TW_REAL_MAGICK"]] + args)
if "-compress" in args and any("slow" in a for a in args):
    # the conversion is done and its output is on disk: now never come back
    open(os.environ["TW_SLOW_MARK"], "w").write("in flight")
    time.sleep(600)
sys.exit(r.returncode)
'''


def send_ctrl_c(pid):
    r = subprocess.run([sys.executable, "-c", _SEND_CTRL_C, str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert r.returncode == 0, f"could not attach to the console of {pid}: rc={r.returncode}"


def alive(pid):
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x100000, False, int(pid))        # SYNCHRONIZE
    if not h:
        return False
    try:
        return k.WaitForSingleObject(h, 0) == 258       # WAIT_TIMEOUT: still running
    finally:
        k.CloseHandle(h)


def wait_for(condition, seconds, what):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {seconds}s waiting for {what}")


def kill_tree(pid):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


@pytest.fixture
def slow_magick(tmp_path):
    """Environment whose `magick` converts for real and, for a file with "slow" in its
    name, then hangs; `mark` appears when that has happened."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "slow_magick.py").write_text(_SLOW_MAGICK, encoding="utf-8")
    (shim / "magick.cmd").write_text(
        '@echo off\r\n"' + sys.executable + '" "%~dp0slow_magick.py" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="ascii", newline="")
    mark = tmp_path / "in_flight.txt"
    env = dict(os.environ, TIFFWORKFLOW_SETTINGS="", TW_REAL_MAGICK=shutil.which("magick"),
               TW_SLOW_MARK=str(mark), PYTHONIOENCODING="utf-8")
    env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    return env, mark


def interrupted_run(script, args, cwd, env, mark, tmp_path):
    """Start a backend with its own console, wait until the slow conversion is in flight,
    press Ctrl+C. Returns (exit code, everything it printed)."""
    out_file = tmp_path / "run_output.txt"
    with open(out_file, "wb") as out:
        p = subprocess.Popen(
            [sys.executable, "-c", _RUN_SCRIPT, str(REPO / script)] + [str(a) for a in args],
            cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            wait_for(lambda: mark.exists() or p.poll() is not None, 120, "the slow conversion")
            assert p.poll() is None, \
                f"the run ended before the interrupt:\n{out_file.read_text(encoding='utf-8', errors='replace')}"
            time.sleep(0.5)
            send_ctrl_c(p.pid)
            rc = p.wait(timeout=120)
        finally:
            if p.poll() is None:
                kill_tree(p.pid)
    return rc, out_file.read_text(encoding="utf-8", errors="replace")


def leftovers(folder):
    """This toolkit's staged / temp files anywhere under `folder`."""
    return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*")
                  if p.is_file() and (tw.LEFTOVER_NAME_RE.search(p.name) or p.suffix == ".mie"))


def photos(tmp_path):
    """Built under a plain name and renamed: the fixture's own exiftool call reads its
    command line in the ANSI code page."""
    plain = tmp_path / "plain"
    plain.mkdir()
    make_tiff(plain / "a fast.tif")
    make_tiff(plain / "b slow.tif")
    work = tmp_path / HOSTILE
    plain.rename(work)
    return work, work / "a fast.tif", work / "b slow.tif"


@needs_tools
@pytest.mark.parametrize("staging", ["staging", "no-staging"])
def test_compress_mode9_interrupted_leaves_every_original_and_nothing_else(tmp_path, slow_magick, staging):
    env, mark = slow_magick
    work, fast, slow = photos(tmp_path)
    before = {p.name: p.read_bytes() for p in (fast, slow)}
    stg = tmp_path / "stg [x] 一時"
    args = ["-Mode", "9", "-InputDir", work, "-ForceSequential"]
    if staging == "staging":
        args += ["-StagingDir", stg]

    rc, out = interrupted_run("compress_tiff_zip.py", args, tmp_path, env, mark, tmp_path)

    assert rc == 130, out
    assert "Interrupted! Cleaning up staging files..." in out and "FATAL" not in out, out
    assert slow.read_bytes() == before[slow.name], "the photo in flight was touched"
    # the first photo was finished or was not: either way it is whole and in its place
    backup = work / "OLD_TIFFs" / fast.name
    if backup.exists():
        assert backup.read_bytes() == before[fast.name] and read_compression(fast).lower() == "zip"
    else:
        assert fast.read_bytes() == before[fast.name]
    assert leftovers(work) == [], out
    assert not stg.exists() or os.listdir(stg) == [], f"staged files stayed: {os.listdir(stg)}"
    assert sorted(p.name for p in work.iterdir() if p.is_file()) == [fast.name, slow.name]

    # and the next run, with the real magick, finds a folder it can work on
    again = subprocess.run([sys.executable, str(REPO / "compress_tiff_zip.py"), "-Mode", "9",
                            "-InputDir", str(work)], cwd=str(tmp_path), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, TIFFWORKFLOW_SETTINGS=""), timeout=300)
    assert again.returncode == 0, again.stdout
    assert "STRANDED" not in again.stdout, again.stdout
    assert read_compression(fast).lower() == "zip" and read_compression(slow).lower() == "zip"
    assert (work / "OLD_TIFFs" / slow.name).read_bytes() == before[slow.name]


@needs_tools
def test_compress_mode8_interrupted_deletes_nothing(tmp_path, slow_magick):
    """Mode 8 replaces each original: an interrupt between "staged" and "landed" must leave
    the original, and the run's own staging folder must go."""
    env, mark = slow_magick
    work, fast, slow = photos(tmp_path)
    before = slow.read_bytes()

    rc, out = interrupted_run("compress_tiff_zip.py",
                              ["-Mode", "8", "-InputDir", work, "-DeleteSource", "-ForceSequential"],
                              tmp_path, env, mark, tmp_path)

    assert rc == 130, out
    assert "Interrupted! Cleaning up staging files..." in out, out
    assert slow.read_bytes() == before, "the photo in flight was replaced or deleted"
    assert fast.exists() and "SOURCE DELETED" not in out.split("b slow.tif")[-1]
    assert leftovers(work) == [], out
    assert sorted(p.name for p in work.iterdir()) == [fast.name, slow.name], \
        f"the run's staging folder stayed: {sorted(p.name for p in work.iterdir())}"


def _process_script(tmp_path):
    """A process that ignores Ctrl+C, writes its pid, starts a child that does the same,
    and sleeps: only being KILLED ends it."""
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import ctypes, os, subprocess, sys, time\n"
        "ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "if len(sys.argv) > 2:\n"
        "    subprocess.Popen([sys.executable, __file__, sys.argv[2]])\n"
        "print('child ready', flush=True)\n"
        "time.sleep(600)\n", encoding="utf-8")
    return script


@pytest.mark.parametrize("screen", ["plain", "rich"])
def test_wizard_ctrl_c_kills_the_backend_and_what_it_started(tmp_path, screen):
    """run_subprocess: Ctrl+C at the wizard returns -1, says so, and the process it started
    is gone -- with ITS children (a backend's magick and exiftool), also when they would
    not have died of the Ctrl+C themselves."""
    stubborn = _process_script(tmp_path)
    child_pid, grandchild_pid = tmp_path / "child.pid", tmp_path / "grandchild.pid"
    driver = (
        "import ctypes, sys\n"
        "ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import convert_tiff as ct\n"
        + ("ct.RICH_AVAILABLE = False\nct.console = None\n" if screen == "plain" else "")
        + f"rc = ct.run_subprocess([sys.executable, {str(stubborn)!r}, {str(child_pid)!r}, {str(grandchild_pid)!r}])\n"
        "print('RC=%d' % rc, flush=True)\n"
    )
    out_file = tmp_path / "wizard_output.txt"
    pids = []
    with open(out_file, "wb") as out:
        p = subprocess.Popen([sys.executable, "-c", driver], cwd=str(tmp_path),
                             env=dict(os.environ, TIFFWORKFLOW_SETTINGS="", PYTHONIOENCODING="utf-8",
                                      TIFFWORKFLOW_NO_UPDATE_CHECK="1"),
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            wait_for(lambda: child_pid.exists() and grandchild_pid.exists() and child_pid.read_text()
                     and grandchild_pid.read_text(), 60, "the backend stand-in and its child")
            pids = [int(child_pid.read_text()), int(grandchild_pid.read_text())]
            assert all(alive(x) for x in pids)
            time.sleep(0.5)
            send_ctrl_c(p.pid)
            rc = p.wait(timeout=60)
            text = out_file.read_text(encoding="utf-8", errors="replace")
            assert rc == 0, text
            assert "RC=-1" in text and "Interrupted by user." in text, text
            wait_for(lambda: not any(alive(x) for x in pids), 20,
                     f"the started processes to be gone (alive: {[x for x in pids if alive(x)]})")
        finally:
            if p.poll() is None:
                kill_tree(p.pid)
            for x in pids:
                if alive(x):
                    kill_tree(x)
