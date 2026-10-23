#!/usr/bin/env python3
"""
convert_tiff.py -- TIFF Workflow Manager

Unified wizard for TIFF processing workflows:
  [1] Compress TIFFs  → Zip/Deflate, modes 0-9 (any folder)
  [2] Fuji: Copy EXIF from JPEG to TIFF (AutoFind, S3/S5 Pro)
  [3] Fuji: Compress → Zip/Deflate (AutoFind, S3/S5 Pro)
  [4] Fuji: Copy+Compress combined in one pass (AutoFind, S3/S5 Pro)
  [5] Restore OLD_TIFFs move TIFFs back to parent folder
  [6] Delete OLD_TIFFs verify copy, then purge
  [7] Diagnose TIFFs check if 16-bit is real or padded
  [8] Generate Thumbnails create sRGB thumbnails from TIFFs

Supports AutoFind for S3/S5 Pro folders, persistent config,
and streaming output from PowerShell backends.
"""

# Written into xmpMM:History by the padded 16 -> 8-bit conversion. Not a setting: it is
# the release this file belongs to (tests/test_diagnose_preset.py pins it to the newest
# docs/CHANGELOG.md section).
TOOLKIT_NAME = "tiff-workflow"
TOOLKIT_VERSION = "2.7.1"
TOOLKIT_URL = "https://github.com/rsilvabr/tiff-workflow"

DEBUG_TIMING = False  # Set True to benchmark compression modes

WORKER_MEMORY_FRACTION = 0.8
# Caps the parallel workers of the Python workflows (diagnose, padded compress,
# Delete OLD_TIFFs) so their ImageMagick processes fit in memory: the peak of one
# worker is estimated from the largest image of the batch (a `magick compare`
# holds ~68 bytes per pixel -- 5.4 GB for an 84 MP scan). The run uses at most
# this fraction of the memory WORKER_MEMORY_LIMIT picks. 0 disables the cap.

WORKER_MEMORY_LIMIT = "both"
# "both" -> the smaller of what the system can still commit (RAM + pagefile)
#           and the physical RAM still available (default);
# "commit" or "physical" -> only that reading.

DIAGNOSE_CACHE = True
# Diagnose (workflow 7, its Repeat and its presets) remembers each TIFF's verdict --
# real 16-bit, padded, not 16-bit -- with its size and modification time in
# .convert_tiff_diagnose_cache.json next to the config, written every few files while
# it runs; the next run checks only the files that are new or changed. Errors are
# never remembered. The padded -> 8-bit conversion still compares every file's pixels
# before replacing it. False = check everything on every run.

# Subprocess timeouts (seconds)
CONVERT_TIMEOUT_S = 60   # magick depth/format conversions
COMPARE_TIMEOUT_S = 120  # magick compare (RMSE)

# audit round 16: fixed timeouts false-failed multi-GB film scans on slow disks.
# Every magick/exiftool call that processes a WHOLE file (depth convert, RMSE
# compare, full integrity decode, full metadata dump) now scales its timeout with
# input size:
#
#     timeout_s = base + size_mb * TIMEOUT_S_PER_MB      (floor: base)
#
# 0.25 s/MB tolerates a combined read+decode+write throughput of ~4 MB/s --
# slower than a USB-2 hard disk. Multiplied by the number of workers running at
# once (_TIMEOUT_LOAD): they share the disk. Header-only probes (identify,
# single-tag exiftool reads) keep their short fixed timeouts.
TIMEOUT_S_PER_MB = 0.25

import argparse
import concurrent.futures
import csv
import functools
import hashlib
import io
import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.box import SIMPLE as BOX_SIMPLE
    from rich.prompt import Prompt, IntPrompt, Confirm
    from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn
    RICH_AVAILABLE = True
    console = Console(force_terminal=True)
except ImportError:
    RICH_AVAILABLE = False
    console = None


# --- Paths ---------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()


# --- Config -------------------------------------------------------

def _config_base() -> Path:
    """Folder of the config file (and of the Diagnose cache next to it)."""
    if platform.system() == "Windows":
        return Path(os.environ.get("USERPROFILE", Path.home()))
    return Path.home()


@dataclass
class ToolConfig:
    """Configuration for convert_tiff tools."""
    staging_dir: Optional[str] = None
    default_workers: int = 8

    last_input_dir: Optional[str] = None
    last_workers: Optional[int] = None
    last_staging: Optional[str] = None
    last_pattern: Optional[str] = None
    last_mode: Optional[int] = None
    last_origin: Optional[str] = None
    # Per-machine default for -ExcludeFolders (';'-separated folder NAMES).
    # Lives in the local config, never hardcoded: a fresh install defaults to
    # empty so nobody silently skips folders they did not choose to skip.
    last_exclude_folders: Optional[str] = None
    # Journal of the last executed workflow (for "Repeat last workflow").
    # Stores the fully built command lines -- repeating re-runs them verbatim
    # (with the PowerShell executable swapped for the currently detected one),
    # so no wizard state has to be reconstructed. Dry-run is never stored.
    last_run_kind: Optional[str] = None
    last_run_label: Optional[str] = None
    last_run_commands: Optional[List[List[str]]] = None
    last_run_time: Optional[str] = None
    # Path of the last manifest CSV run (menu option 3). Only the path is
    # kept -- a repeat re-reads the CSV through the same loader guards.
    last_manifest_path: Optional[str] = None
    # Settings of the last manifest run (menu 3), so a repeat or a preset runs it with
    # the SAME answers: the step_basic_params dict and the mode for rows without one.
    last_manifest_workflow: Optional[Dict] = None
    last_manifest_default_mode: Optional[int] = None
    # AutoFind of the last Fuji workflow (2/3/4): {"root": str, "patterns": [str]}.
    # A repeat or preset searches again, so sessions added since are included.
    last_autofind: Optional[Dict] = None
    # Settings of the last Diagnose run (workflow 7): {"roots": [str], "scope": str,
    # "skip_names": str, "workers": int, "temp_dir": str|None, "compress_tmp": str}.
    # A repeat or preset re-scans the roots and only REPORTS (never converts).
    last_diagnose: Optional[Dict] = None
    # Named snapshots of the journal above: name -> {kind, label, commands, time,
    # manifest_path, manifest_workflow, manifest_default_mode, autofind, diagnose,
    # saved_at}.
    presets: Optional[Dict[str, Dict]] = None
    ps_major: int = 0  # detected at startup, not persisted
    ps_name: str = "powershell"  # "pwsh" or "powershell", not persisted


class ConfigManager:
    """Persistent JSON config for convert_tiff."""

    def __init__(self):
        self.config_path = self._get_config_path()
        self.config = ToolConfig()
        self._load_config()

    def _get_config_path(self) -> Path:
        return _config_base() / ".convert_tiff_config.json"

    def _load_config(self) -> None:
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if hasattr(self.config, k):
                            setattr(self.config, k, v)
                    self._sanitise_presets()
            except Exception as e:
                # audit round 16: a corrupt config was swallowed silently and the
                # wizard ran on defaults with no hint why the saved state vanished.
                print(f"WARNING: ignoring unreadable config file {self.config_path}: {e}",
                      file=sys.stderr)

    def _sanitise_presets(self) -> None:
        """Drop presets that are not a name -> settings mapping.

        The config file is a plain JSON the user can (and does) hand-edit, and
        every consumer of `presets` assumed both levels were dicts: a list, or a
        single preset saved as a string, took down the preset menu, --list-presets
        and the main menu's counter with a traceback before anything could be
        run. Discarding the malformed entries with a warning keeps the good ones
        usable -- and the file is only rewritten when the user saves, so nothing
        is lost behind their back. An entry whose journal could never run (no
        label, or no commands and not a manifest) is discarded here too: it would
        only refuse at run time.
        """
        raw = self.config.presets
        if raw is None:
            return
        if not isinstance(raw, dict):
            print(f"Warning: 'presets' in the config is a {type(raw).__name__}, not a "
                  f"mapping of name -> settings. Ignoring it.", file=sys.stderr)
            self.config.presets = {}
            return
        clean, bad = {}, []
        for name, body in raw.items():
            if isinstance(name, str) and isinstance(body, dict) and _journal_runnable(body):
                clean[name] = body
            else:
                bad.append(str(name))
        if bad:
            print(f"Warning: ignoring {len(bad)} malformed preset(s) in the config: "
                  f"{', '.join(sorted(bad)[:5])}"
                  + (" ..." if len(bad) > 5 else ""), file=sys.stderr)
        self.config.presets = clean

    def save_config(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(self.config)
            # Runtime-detected fields, not persisted
            data.pop("ps_major", None)
            data.pop("ps_name", None)
            with open(self.config_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            # audit round 16: same story on save -- a config that silently never
            # persisted looked exactly like one that did.
            print(f"WARNING: could not save config file {self.config_path}: {e}",
                  file=sys.stderr)


# --- PowerShell Version Detection -----------------------------------

def detect_powershell_version():
    """Detect PowerShell version. Returns (major, name, version), e.g. (7, "pwsh", "7.4.1")."""
    import re
    for ps_name in ["pwsh", "powershell"]:
        try:
            result = subprocess.run(
                [ps_name, "-NoProfile", "-Command",
                 "try { $PSVersionTable.PSVersion.Major } catch { 0 }; "
                 "try { $PSVersionTable.PSVersion.ToString() } catch { 'unknown' }"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                # Use regex to extract first number (version major)
                match = re.search(r'(\d+)', result.stdout.strip())
                major = int(match.group(1)) if match else 0
                # Extract version string (e.g., "7.4.1")
                version_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', result.stdout.strip())
                version = version_match.group(1) if version_match else "unknown"
                if major > 0:
                    return major, ps_name, version
        except Exception:
            pass
    return 0, "powershell", "unknown"


# --- Dependency check ----------------------------------------------

BACKEND_SCRIPTS = [
    "compress_tiff_zip.ps1",
    "generate_thumbnails.ps1",
    "copy_exif_to_TIFF_ps7.ps1",
    "copy_exif_to_TIFF_ps5.ps1",
]


def _probe_version(cmd: List[str], timeout: int = 10) -> Optional[str]:
    """Run a version probe, return the first non-empty output line or None."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            if line.strip():
                return line.strip()
    except Exception:
        pass
    return None


def check_dependencies(cfg) -> List[Tuple[str, bool, str]]:
    """Probe external tools. Returns [(name, ok, detail), ...]. Read-only."""
    results = []
    magick_ver = _probe_version(["magick", "-version"]) if shutil.which("magick") else None
    results.append(("magick", magick_ver is not None, magick_ver or "not found on PATH"))
    exif_ver = _probe_version(["exiftool", "-ver"]) if shutil.which("exiftool") else None
    results.append(("exiftool", exif_ver is not None, exif_ver or "not found on PATH"))
    ps_major = cfg.config.ps_major
    if ps_major > 0:
        ps_detail = f"PS{ps_major} ({cfg.config.ps_name})"
        if ps_major < 7:
            ps_detail += " -- sequential only, parallelism DISABLED"
        results.append(("PowerShell", True, ps_detail))
    else:
        results.append(("PowerShell", False, "not found on PATH"))
    missing = [s for s in BACKEND_SCRIPTS if not (SCRIPT_DIR / s).exists()]
    results.append(("backend scripts", not missing,
                    "all present" if not missing else "missing: " + ", ".join(missing)))
    results.append(("rich", RICH_AVAILABLE,
                    "installed" if RICH_AVAILABLE else "plain-text UI (pip install rich)"))
    return results


def show_environment_panel(cfg) -> None:
    """Print the dependency status panel shown at startup (jxl_photo style)."""
    checks = check_dependencies(cfg)
    if RICH_AVAILABLE and console:
        parts = []
        for name, ok, _ in checks:
            mark = "[green][[OK]][/green]" if ok else "[red][[!!]][/red]"
            parts.append(f"{mark} {name}")
        console.print(Panel(" | ".join(parts), title="TIFF Workflow Environment", border_style="cyan"))
        for name, ok, detail in checks:
            if not ok:
                console.print(f"[yellow]  {name}: {escape(detail)}[/yellow]")
    else:
        print("--- Environment ---")
        for name, ok, detail in checks:
            mark = "[OK]" if ok else "[!!]"
            print(f"  {mark} {name}: {detail}")


# --- Helpers ------------------------------------------------------

def find_folders_by_pattern(root: Path, patterns: List[str]) -> Dict[Path, int]:
    """
    Recursively find folders matching any of the patterns.
    Returns dict of {folder_path: tiff_count}
    """
    results = {}
    # audit round 16: OLD_PADDED (the padded-compress backup folder) was missing here --
    # AutoFind walked into it and reprocessed the backups. _TOOL_OUTPUT_PARTS had it.
    # audit round 17: one list only -- the ZIP/_EXPORT names are read from the backend's
    # own settings (a literal copy here let an edited -ZipSubfolderName be walked into).
    exclude_names = _TOOL_OUTPUT_PARTS
    for path in root.rglob("*/"):
        if not path.is_dir():
            continue  # Python < 3.11: rglob("*/") also yields files
        if path.name.startswith("."):
            continue
        if path.name.lower() in exclude_names:
            continue
        for pat in patterns:
            if pat.lower() in path.name.lower():
                tiffs = [f for f in path.glob("*.tif") if f.stat().st_size > 0] + \
                        [f for f in path.glob("*.tiff") if f.stat().st_size > 0]
                if tiffs:
                    results[path] = len(tiffs)
                break
    return dict(sorted(results.items(), key=lambda x: x[0].name))


def truncate_path(p: Path, max_len: int = 50) -> str:
    """Truncate long paths for display. Result is always <= max_len."""
    s = str(p)
    if len(s) <= max_len:
        return s
    parts = s.split(os.sep)
    ellipsis = "..."
    if len(parts) > 3:
        candidate = f"{parts[0]}{os.sep}{ellipsis}{os.sep}{parts[-2]}{os.sep}{parts[-1]}"
        if len(candidate) <= max_len:
            return candidate
    # Fallback: keep the tail of the path within max_len
    keep = max_len - len(ellipsis)
    if keep <= 0:
        return ellipsis[:max_len]
    return ellipsis + s[-keep:]


#: The PowerShell backends declare [ValidateRange(1, 64)] on -Workers; anything outside
#: that range fails at parameter binding and the backend never starts.
MAX_WORKERS = 64


def clamp_workers(value: int, default: int = 8) -> int:
    """Keep -Workers inside the range the PowerShell backends accept."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return 1
    if n > MAX_WORKERS:
        return MAX_WORKERS
    return n


#: Shipped defaults of the backend parameters the wizard offers or passes. Used ONLY
#: when the .ps1 cannot be read or the parameter is missing -- the source of truth is
#: the param() block of the script itself (tests pin these to it).
_BACKEND_PARAM_FALLBACKS = {
    ("compress_tiff_zip.ps1", "SafeMode"): True,
    ("compress_tiff_zip.ps1", "SkipLzwAsCompressed"): False,
    ("compress_tiff_zip.ps1", "CapWorkers"): 0,
    ("compress_tiff_zip.ps1", "DuplicateAction"): "Numbered",
    ("compress_tiff_zip.ps1", "ThumbSize"): 256,
    ("compress_tiff_zip.ps1", "ThumbQuality"): "85",
    ("compress_tiff_zip.ps1", "ThumbFormat"): "jpg",
    ("compress_tiff_zip.ps1", "ZipSubfolderName"): "ZIP",
    ("compress_tiff_zip.ps1", "ExportZipSubfolder"): "ZIP",
    ("compress_tiff_zip.ps1", "ExportMarker"): "_EXPORT",
    ("copy_exif_to_TIFF_ps7.ps1", "SafeMode"): True,
    ("copy_exif_to_TIFF_ps5.ps1", "SafeMode"): True,
    ("generate_thumbnails.ps1", "Size"): 256,
    ("generate_thumbnails.ps1", "Quality"): "85",
    ("generate_thumbnails.ps1", "Format"): "jpg",
    ("generate_thumbnails.ps1", "Page"): "0",
}

_PS_PARAM_LINE = re.compile(
    r"""^\s*(?:\[[^\]]*\]\s*)*\$(\w+)\s*=\s*("(?:[^"]*)"|'(?:[^']*)'|\$true|\$false|-?\d+(?:\.\d+)?)""",
    re.IGNORECASE)


@functools.lru_cache(maxsize=None)
def _ps_param_defaults(script_name: str) -> Dict[str, object]:
    """name -> default of every parameter in the script's top param() block.

    Only the block from the first line that starts with `param(` to the first line
    that is just `)`; values: $true/$false -> bool, "..."/'...' -> str, numbers ->
    int/float. {} when the file cannot be read."""
    try:
        text = (SCRIPT_DIR / script_name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: Dict[str, object] = {}
    inside = False
    for line in text.splitlines():
        if not inside:
            inside = line.lstrip().lower().startswith("param(")
            continue
        if line.strip() == ")":
            break
        m = _PS_PARAM_LINE.match(line)
        if not m:
            continue
        raw = m.group(2)
        low = raw.lower()
        if low in ("$true", "$false"):
            val: object = (low == "$true")
        elif raw[0] in "\"'":
            val = raw[1:-1]
        elif "." in raw:
            val = float(raw)
        else:
            val = int(raw)
        out[m.group(1)] = val
    return out


def _backend_default(script_name: str, param: str):
    """The default of `param` as written in `script_name`'s param() block; the
    shipped value from _BACKEND_PARAM_FALLBACKS only when it cannot be read or has
    another type than the fallback (a typo must not reach a command line)."""
    fallback = _BACKEND_PARAM_FALLBACKS[(script_name, param)]
    val = _ps_param_defaults(script_name).get(param)
    if val is None or type(val) is not type(fallback):
        return fallback
    return val


def _tool_output_names() -> set:
    """Lower-cased folder names that hold this toolkit's own output (discovery skips
    them below the scan root). The ZIP/_EXPORT names come from compress_tiff_zip.ps1's
    own settings: a copy here let an edited -ZipSubfolderName be walked into."""
    names = {"old_tiffs", "old_padded", "logs", "converted_zip"}
    for p in ("ZipSubfolderName", "ExportZipSubfolder", "ExportMarker"):
        names.add(str(_backend_default("compress_tiff_zip.ps1", p)).lower())
    return names


#: Folder names that hold this toolkit's own output. Discovery skips them, but only BELOW
#: the folder the user pointed at -- see _is_tool_output_path.
_TOOL_OUTPUT_PARTS = _tool_output_names()


def _is_tool_output_path(path: Path, root: Path) -> bool:
    """True when `path` sits inside one of this toolkit's own output folders BELOW `root`.

    Evaluated RELATIVE to the scan root, never against the absolute path. Matching the
    absolute path also matched every ancestor: a library under `D:\\Backup\\zip\\2019\\`
    reported "No TIFF files found" and exited 0, and pointing a workflow straight AT a
    folder named `ZIP` or `OLD_TIFFs` could never work. That is the same bug the three
    PowerShell backends fixed in audit round 14 (Test-PathExcluded); the diagnose workflow
    kept the absolute form, while the manifest generator already did it relative.

    A path that is not under the root at all falls back to the whole path -- in doubt, skip.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return any(part.lower() in _TOOL_OUTPUT_PARTS for part in rel.parts)


# Captured at import: some tests replace this module's `subprocess` with a stub.
_DEVNULL = subprocess.DEVNULL
_CompletedProcess = subprocess.CompletedProcess


def _run_captured(cmd, timeout, text=False, input=None):
    """subprocess.run with stdout/stderr captured through temp FILES.

    capture_output=True makes communicate() start two reader threads per call on
    Windows. Under memory exhaustion a thread can fail in its bootstrap
    (MemoryError) before it signals "started", and Thread.start() then waits
    forever -- before the timeout is even armed (jxl-photo, 2026-10-04: one
    worker hung 60 min). With files there is no thread: a failed spawn raises
    OSError, which the callers already handle. Copied from jxl-photo; keep in
    step. text=True decodes as UTF-8 (what magick and exiftool print), where
    subprocess's text mode used the console code page."""
    stdin_kw = {"stdin": _DEVNULL} if input is None else {"input": input}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        r = subprocess.run(cmd, stdout=out, stderr=err, timeout=timeout, **stdin_kw)
        out.seek(0)
        err.seek(0)
        o = r.stdout if r.stdout is not None else out.read()
        e = r.stderr if r.stderr is not None else err.read()
    if text:
        if isinstance(o, bytes):
            o = o.decode("utf-8", "replace").replace("\r\n", "\n")
        if isinstance(e, bytes):
            e = e.decode("utf-8", "replace").replace("\r\n", "\n")
    return _CompletedProcess(cmd, r.returncode, o, e)


def _run_exiftool(args, timeout, text=False):
    """exiftool with its arguments in a UTF-8 argfile (`-charset filename=utf8`).

    On Windows exiftool reads its command line in the ANSI code page, so a path with
    characters outside it -- `G:\\2019\\190815_Film_伊豆沼_夏\\...` -- arrived mangled
    and could not be opened. Every write gate then failed closed: the padded conversion
    refused every file under a Japanese folder name (`subfile_restore_failed`). An
    argfile is read as UTF-8 and passes any name through; it is how _image_pixels has
    always called exiftool."""
    fd, argfile = tempfile.mkstemp(suffix=".args")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(["-charset", "filename=utf8"] + [str(a) for a in args]) + "\n")
        return _run_captured(["exiftool", "-@", argfile], timeout, text=text)
    finally:
        try:
            os.unlink(argfile)
        except OSError:
            pass


# Memory helpers copied from jxl-photo's jxl_recompressor.py (v2.9.0); keep in step.

def _windows_memory_status():
    """GlobalMemoryStatusEx as (ullAvailPhys, ullAvailPageFile), or None."""
    import ctypes

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = _MemoryStatusEx()
    st.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return int(st.ullAvailPhys), int(st.ullAvailPageFile)


def _linux_mem_available():
    """/proc/meminfo MemAvailable in bytes, or None."""
    with open("/proc/meminfo", encoding="ascii") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return None


def _available_commit_bytes():
    """Memory the system can still commit, in bytes, or None if unknown.
    Windows: ullAvailPageFile (RAM + pagefile not yet committed -- the limit
    WinError 1455 reports). Linux: MemAvailable."""
    try:
        if sys.platform == "win32":
            st = _windows_memory_status()
            return st[1] if st else None
        return _linux_mem_available()
    except Exception:
        return None


def _available_physical_bytes():
    """Physical RAM still available (free + standby cache), in bytes, or None
    if unknown. Windows: ullAvailPhys. Linux: MemAvailable (no commit limit by
    default there, so both readings are the same)."""
    try:
        if sys.platform == "win32":
            st = _windows_memory_status()
            return st[0] if st else None
        return _linux_mem_available()
    except Exception:
        return None


_MEMORY_LIMITS = ("both", "commit", "physical")
# How the cap's log and warning name the budget it used.
_MEMORY_BUDGET_TEXT = {"commit": "the system can still commit (RAM + pagefile)",
                       "physical": "of physical RAM still available"}


def _available_memory_bytes():
    """(bytes, "commit"|"physical") the worker cap budgets from, per
    WORKER_MEMORY_LIMIT: the smaller of the two ("both"), or just one. A reading
    that fails is left out; (None, None) when nothing could be read. An unknown
    setting counts as "both", the stricter budget."""
    limit = WORKER_MEMORY_LIMIT if WORKER_MEMORY_LIMIT in _MEMORY_LIMITS else "both"
    readings = []
    if limit in ("both", "commit"):
        readings.append((_available_commit_bytes(), "commit"))
    if limit in ("both", "physical"):
        readings.append((_available_physical_bytes(), "physical"))
    readings = [r for r in readings if r[0] is not None]
    if not readings:
        return None, None
    return min(readings, key=lambda r: r[0])


# Peak private memory of one ImageMagick process, in bytes per pixel of the image's
# main page, measured on an 84 MP 16-bit scan (ImageMagick 7.1.2 Q16-HDRI; plan
# AI_tools/261010_Claude_plan_memory-subprocess.md), +~10% margin.
_COMPARE_BYTES_PER_PIXEL = 75     # magick compare (68 measured)
_CONVERT_BYTES_PER_PIXEL = 15     # depth/format conversion, decode (12.7 measured)
_UNKNOWN_IMAGE_PIXELS = 150_000_000


def _say(msg: str, style: str = "") -> None:
    """One line on the console, Rich-styled when available."""
    if RICH_AVAILABLE and console:
        console.print(f"[{style}]{escape(msg)}[/{style}]" if style else escape(msg))
    else:
        print(msg)


def _max_image_pixels(paths) -> int:
    """Largest IFD0 width*height among `paths` (see _image_pixels)."""
    return max(_image_pixels(paths).values(), default=0)


def _image_pixels(paths) -> Dict[str, int]:
    """{normcased absolute path: IFD0 width*height} for `paths`, read in batches of 400
    by exiftool (-j). A file exiftool does not report, or reports without a size,
    counts as _UNKNOWN_IMAGE_PIXELS -- an unknown size must never shrink the estimate."""
    paths = [str(p) for p in paths]
    sizes: Dict[str, int] = {}
    # On a hard disk each batch of 400 can take a minute: say what is happening and how
    # far it got, instead of a silent screen before the first result line.
    many = len(paths) > 400
    t0 = time.time()
    if many:
        _say(f"Reading the image size of {len(paths)} file(s) for the memory estimate...", "dim")
    for off in range(0, len(paths), 400):
        if many and off:
            _say(f"  sizes read: {off}/{len(paths)} ({time.time() - t0:.0f}s)", "dim")
        chunk = paths[off:off + 400]
        seen = {}
        argfile = None
        try:
            fd, argfile = tempfile.mkstemp(suffix=".args")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(["-charset", "filename=utf8", "-j", "-n",
                                   "-IFD0:ImageWidth", "-IFD0:ImageHeight"] + chunk) + "\n")
            r = _run_captured(["exiftool", "-@", argfile], _work_timeout(60), text=True)
            for entry in json.loads(r.stdout or "[]"):
                try:
                    px = int(entry.get("ImageWidth") or 0) * int(entry.get("ImageHeight") or 0)
                except (TypeError, ValueError):
                    px = 0
                seen[os.path.normcase(os.path.abspath(str(entry.get("SourceFile", ""))))] = px
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        finally:
            if argfile:
                try:
                    os.unlink(argfile)
                except OSError:
                    pass
        for p in chunk:
            key = os.path.normcase(os.path.abspath(p))
            px = seen.get(key, 0)
            sizes[key] = px if px > 0 else _UNKNOWN_IMAGE_PIXELS
    return sizes


class _MemoryBudget:
    """Bytes of memory the running ImageMagick jobs may hold together. A job reserves
    its OWN estimate before it starts and waits while that would not fit, so small
    files run on every worker and only a huge one waits for room -- instead of
    sizing every worker for the largest file of the batch. A job larger than the
    whole budget runs alone (it waits for the others to finish, never forever)."""

    def __init__(self, total: int):
        self.total = max(1, int(total))
        self.used = 0
        self._cv = threading.Condition()

    def acquire(self, need: int) -> int:
        need = min(max(0, int(need)), self.total)
        with self._cv:
            while self.used and self.used + need > self.total:
                self._cv.wait()
            self.used += need
        return need

    def release(self, held: int) -> None:
        with self._cv:
            self.used -= held
            self._cv.notify_all()


def _memory_budget(paths, bytes_per_pixel: int, what: str):
    """(_MemoryBudget, {path: pixels}) for a pool over `paths`, or (None, {}) when the
    cap is off or the available memory cannot be read. Prints the budget and what the
    largest and the typical file need."""
    if WORKER_MEMORY_FRACTION <= 0 or not paths:
        return None, {}
    avail, budget = _available_memory_bytes()
    if avail is None:
        _say("Memory: could not read the available memory -- workers not capped", "dim")
        return None, {}
    sizes = _image_pixels(paths)
    total = avail * WORKER_MEMORY_FRACTION
    ordered = sorted(sizes.values())
    largest, typical = ordered[-1], ordered[len(ordered) // 2]
    _say(f"Memory: {total / 2**30:.1f} GB for {what} jobs ({avail / 2**30:.1f} GB "
         f"{_MEMORY_BUDGET_TEXT[budget]} x WORKER_MEMORY_FRACTION={WORKER_MEMORY_FRACTION}) "
         f"| typical file {typical / 1e6:.0f} MP ~{typical * bytes_per_pixel / 2**30:.1f} GB, "
         f"largest {largest / 1e6:.0f} MP ~{largest * bytes_per_pixel / 2**30:.1f} GB "
         f"-- a large file waits for room, the workers are not reduced", "dim")
    return _MemoryBudget(total), sizes


def _memory_capped_pool_workers(requested: int, paths, bytes_per_pixel: int,
                                what: str) -> int:
    """`requested` lowered so that many ImageMagick workers fit in memory; prints
    the estimate, and a warning when it lowered the count."""
    if WORKER_MEMORY_FRACTION <= 0 or requested <= 1 or not paths:
        return requested
    avail, budget = _available_memory_bytes()
    if avail is None:
        _say("Memory: could not read the available memory -- workers not capped", "dim")
        return requested
    pixels = _max_image_pixels(paths)
    per_job = pixels * bytes_per_pixel
    cap = max(1, int(avail * WORKER_MEMORY_FRACTION // per_job))
    workers = min(requested, cap)
    _say(f"Memory: ~{per_job / 2**30:.1f} GB per worker ({what}, "
         f"{pixels / 1e6:.0f} MP) | {avail / 2**30:.1f} GB available ({budget}) "
         f"| workers {workers}", "dim")
    if cap < requested:
        _say(f"Workers reduced from {requested} to {workers}: {requested} {what} "
             f"processes at ~{per_job / 2**30:.1f} GB each would not fit in the "
             f"{avail / 2**30:.1f} GB {_MEMORY_BUDGET_TEXT[budget]} "
             f"(WORKER_MEMORY_FRACTION={WORKER_MEMORY_FRACTION}).", "yellow")
    return workers


# Workers running at once in the current pool (set around each ThreadPoolExecutor):
# they share the disk, so each gets ~1/N of the throughput TIMEOUT_S_PER_MB assumes.
_TIMEOUT_LOAD = 1


def _work_timeout(base_s: float, *paths) -> float:
    """Size-scaled subprocess timeout (audit round 16; see TIMEOUT_S_PER_MB for the
    formula). Missing/unreadable paths contribute 0, so the floor is always `base_s`.
    """
    total = 0
    for p in paths:
        try:
            total += Path(p).stat().st_size
        except (OSError, TypeError, ValueError):
            continue
    return base_s + (total / (1024 * 1024)) * TIMEOUT_S_PER_MB * max(1, _TIMEOUT_LOAD)


_magick_alias_dir: Optional[Path] = None
# ONE lock for the one-time alias-dir creation. `with threading.Lock():` built a new lock
# per call, so the worker pools raced it and made several alias dirs.
_magick_alias_lock = threading.Lock()
# Per-thread {source: alias} of the file being worked on (see _scoped_magick_aliases).
_magick_alias_scope = threading.local()


def _bracket_free_alias(path: Path) -> Optional[Path]:
    """Hard link (copy fallback) of `path` under a bracket-free name in %TEMP%.

    Exists because no '[' escape works on Windows ImageMagick (see _magick_spec).
    Returns None on any failure -- callers fail closed. Inside a _scoped_magick_aliases
    function one alias per source is made and deleted when it returns; outside one,
    aliases are wiped at exit.

    The hard link fails across volumes (a photo library on G:, %TEMP% on C:), and the
    fallback is a FULL COPY of the TIFF: before the scope, a Diagnose over 5000 files
    left ~3 copies of each in %TEMP% until the run ended -- 434 GB on 2026-10-17.
    """
    global _magick_alias_dir
    try:
        if not path.is_file():
            return None
        scope = getattr(_magick_alias_scope, "items", None)
        key = os.path.normcase(os.path.abspath(path))
        if scope is not None and key in scope and scope[key].exists():
            return scope[key]
        with _magick_alias_lock:
            if _magick_alias_dir is None:
                _magick_alias_dir = Path(tempfile.mkdtemp(prefix="magick_alias_"))
                import atexit
                atexit.register(shutil.rmtree, str(_magick_alias_dir), True)
        alias = _magick_alias_dir / (
            f"{uuid.uuid4().hex}_{path.name.replace('[', '(').replace(']', ')')}")
        try:
            os.link(path, alias)
        except OSError:
            shutil.copy2(path, alias)
        if scope is not None:
            scope[key] = alias
        return alias
    except Exception:
        return None


def _scoped_magick_aliases(fn):
    """Decorator for the per-file workers (diagnose check, padded conversion, OLD_TIFFs
    compare): the bracket-free aliases made while `fn` runs are shared by its magick
    calls and deleted when it returns, so a run holds at most one alias per worker.
    A nested call leaves the cleanup to the outermost one."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if getattr(_magick_alias_scope, "items", None) is not None:
            return fn(*args, **kwargs)
        _magick_alias_scope.items = {}
        try:
            return fn(*args, **kwargs)
        finally:
            items, _magick_alias_scope.items = _magick_alias_scope.items, None
            for alias in items.values():
                try:
                    alias.unlink()
                except OSError:
                    pass
    return wrapper


def _magick_spec(path) -> str:
    """Escape '%' for the filename SPECS ImageMagick expands.

    Python mirror of ConvertTo-MagickPathSpec in the PowerShell backends. ImageMagick reads
    printf-style scene specifiers in a filename spec: `50%off.tif` addresses `500ff.tif`
    ('%o' consumed), `100%dpi.tif` addresses `1000pi.tif`. '%%' is the escape.

    WHERE THIS APPLIES, exactly:
      - EVERY output path.
      - An INPUT that carries a '[n]' page suffix -- appending the suffix turns the argument
        into a filename spec.
    And where it must NOT be used:
      - A PLAIN input path. There ImageMagick opens the name literally, so an escaped name is
        simply not found.

    The backends were fixed for this; the Python side was the copy left behind, so workflow
    [6] Delete OLD_TIFFs reported a permanent MISMATCH for any '%' file (it could never be
    purged) and workflow [7] Diagnose reported an ERROR line for each one.

    audit round 16 -- '[' and ']': appending a '[n]' selector ALSO exposes the path to
    glob parsing, and a glob match WINS over the literal file: with `foo[1].tif` and
    `foo1.tif` side by side, `identify "foo[1].tif[0]"` reads foo1.tif. No escape exists
    on Windows ImageMagick (verified live on 7.1.2-27: `\\[` and `[[]` are treated
    literally and simply fail to open, and a `tiff:` prefix still globs), so a path
    containing brackets is routed through a bracket-free hard-link alias -- the only form
    in which the literal file wins. If the alias cannot be made, a backslash-escaped name
    is returned instead: magick cannot open it, so the caller's gate fails CLOSED rather
    than comparing the wrong file. The '[n]' selectors are appended by CALLERS after this
    returns and are never touched. OUTPUT specs are never aliased (magick would write the
    alias, not the real name): outputs use generated bracket-free names instead (see
    _process_single_padded).
    """
    s = str(path)
    if "[" in s or "]" in s:
        alias = _bracket_free_alias(Path(path))
        if alias is not None:
            s = str(alias)
        else:
            s = s.replace("[", "\\[").replace("]", "\\]")
    return s.replace("%", "%%")


def _safe_tag_source(path: Path):
    """Return (source_for_tagsfromfile, temp_to_delete_or_None) for `path`.

    exiftool expands printf-style filename format codes in the `-tagsfromfile` ARGUMENT, so
    `50%off.tif` is read as `50ff.tif` -- and exiftool STILL EXITS 0, so a returncode check
    passes while nothing was copied. Doubling the '%' does not escape it. The fix is to hand
    exiftool a name without one: park the tags in a temp MIE container first, exactly like
    Backup-TiffMetadata does in the PowerShell backends. The source FILE OPERAND is not
    format-expanded, only the -tagsfromfile argument is, so dumping FROM the '%' name works.

    Returns (None, None) when the container was needed but could not be written -- callers
    must treat that as a failure, never as "copy from the original name".
    """
    if "%" not in str(path):
        return str(path), None
    fd, tmp = tempfile.mkstemp(suffix=".mie")
    os.close(fd)
    os.unlink(tmp)
    if "%" in tmp:
        # `-o` is format-expanded too, so a '%' anywhere in the temp dir breaks the container.
        return None, None
    try:
        r = _run_exiftool(["-q", "-q", "-all:all", "-o", tmp, str(path)],
                          _work_timeout(60, path))
        if r.returncode != 0 or not Path(tmp).exists():
            Path(tmp).unlink(missing_ok=True)
            return None, None
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        return None, None
    return tmp, tmp


def _file_digest(path: Path) -> str:
    """SHA-256 of a file, streamed so large TIFF sidecars do not land in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_move(src: Path, dst: Path) -> None:
    """
    Move file, overwriting destination if it exists (Windows-safe).

    The destination is only destroyed once the replacement is in place: the old code
    unlinked dst first, so a move that failed afterwards (disk full, permission, cross-device)
    left BOTH files gone. An existing dst is parked under a sibling .bak-<uuid> name, and only
    unlinked after the move succeeds; on failure it is put back.

    A destination that is a directory is an error, not something to delete -- the old
    shutil.rmtree(dst) would recursively erase a whole folder that merely shared the filename.
    """
    if dst.exists() and dst.is_dir():
        raise IsADirectoryError(f"refusing to replace directory with file: {dst}")

    backup = None
    if dst.exists():
        backup = dst.with_name(f"{dst.name}.bak-{uuid.uuid4().hex[:8]}")
        os.replace(str(dst), str(backup))
    try:
        shutil.move(str(src), str(dst))
    except Exception:
        if backup is not None:
            os.replace(str(backup), str(dst))
        raise
    if backup is not None:
        try:
            backup.unlink()
        except OSError:
            pass


# --- AutoFind: Pattern Selection ----------------------------------

def step_pattern(cfg: ToolConfig) -> Optional[str]:
    """Choose AutoFind pattern (S5, S3, Both, Custom)."""
    if RICH_AVAILABLE and console:
        console.print("\n[bold cyan]Auto-Find Pattern[/bold cyan]")
        console.print("  [1] [bold]S5 Pro[/bold] folders -- matches 'S5pro' in folder name")
        console.print("  [2] [bold]S3 Pro[/bold] folders -- matches 'S3pro' in folder name")
        console.print("  [3] [bold]Both[/bold]          -- matches 'S5pro' and 'S3pro'")
        console.print("  [4] [bold]Custom[/bold]         -- type any pattern")
        choice = Prompt.ask("Choice", choices=["1", "2", "3", "4"], default="1")
    else:
        print("\n--- Auto-Find Pattern ---")
        print("[1] S5 Pro folders -- matches 'S5pro' in folder name")
        print("[2] S3 Pro folders -- matches 'S3pro' in folder name")
        print("[3] Both          -- matches 'S5pro' and 'S3pro'")
        print("[4] Custom        -- type any pattern")
        choice = input("Choice [1]: ").strip() or "1"

    patterns_map = {
        "1": ["S5pro"],
        "2": ["S3pro"],
        "3": ["S5pro", "S3pro"],
    }
    if choice in patterns_map:
        return patterns_map[choice]
    if choice == "4":
        if RICH_AVAILABLE and console:
            pat = Prompt.ask("Custom pattern (case-insensitive, partial match)").strip()
        else:
            pat = input("Custom pattern (case-insensitive, partial match): ").strip()
        if not pat:
            return None
        return [pat]
    return None


# --- AutoFind: Scan and Preview ------------------------------------

def step_autofind(cfg: ToolConfig, patterns: List[str], root: Path) -> Optional[List[Path]]:
    """Scan folders matching pattern, let user confirm."""
    if RICH_AVAILABLE and console:
        console.print(f"\n[cyan]Scanning for folders matching:[/cyan] {', '.join(patterns)}")
    else:
        print(f"\nScanning for folders matching: {', '.join(patterns)}")

    found = find_folders_by_pattern(root, patterns)
    if not found:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No matching folders found.[/yellow]")
        else:
            print("No matching folders found.")
        return None

    if RICH_AVAILABLE and console:
        table = Table(title=f"Found {len(found)} session(s)", header_style="bold cyan")
        table.add_column("#", justify="right", style="dim", width=4)
        table.add_column("Folder", style="green")
        table.add_column("TIFFs", justify="center", style="yellow", width=6)
        table.add_column("Path", style="dim")
        for i, (folder, count) in enumerate(found.items(), 1):
            table.add_row(str(i), folder.name, str(count), truncate_path(folder, 40))
        console.print(Panel(table, border_style="green"))
    else:
        print(f"\nFound {len(found)} session(s):")
        for i, (folder, count) in enumerate(found.items(), 1):
            print(f"  {i}. {folder.name} ({count} TIFFs) -- {folder}")

    total_tiffs = sum(found.values())
    if RICH_AVAILABLE and console:
        msg = f"Proceed with all {len(found)} session(s) ({total_tiffs} TIFFs)?"
        if not Confirm.ask(f"[green]{msg}[/green]", default=True):
            return None
    else:
        msg = f"Proceed with all {len(found)} session(s) ({total_tiffs} TIFFs)? [Y/n]: "
        if input(msg).strip().lower().startswith("n"):
            return None

    # Verify folders still exist before returning
    existing = [p for p in found.keys() if p.exists()]
    if len(existing) != len(found):
        if RICH_AVAILABLE and console:
            console.print(f"[yellow]Note: {len(found) - len(existing)} folder(s) were removed during selection.[/yellow]")
    return existing


# --- Mode Selection (Free Compress 0-8) ---------------------------

MODE_NAMES = {
    0: "In-place (same folder, non-recursive)",
    1: "Subfolder (ZIP/ in each folder)",
    2: "Flat (all to one output folder)",
    3: "Recursive subfolders (each folder gets ZIP subfolder)",
    4: "Folder rename (TIFF -> ZIP in parent name)",
    5: "Sibling folder (ZIP at grandparent level)",
    6: "Export marker full (_EXPORT tree)",
    7: "Export marker subfolder (_EXPORT/TIFF tree)",
    8: "In-place recursive + delete source",
    9: "In-place recursive + OLD_TIFFs",
}

MODE_DESCS = {
    0: "Non-recursive. TIFFs stay in original folders, compressed TIFF next to each file. Originals moved to OLD_TIFFs/.",
    1: "Non-recursive. Each folder gets a 'ZIP' subfolder with compressed files.",
    2: "Recursive. All TIFFs merged into a single output folder.",
    3: "Recursive. Each subfolder gets its own 'ZIP' subfolder.",
    4: "Recursive. Parent folders renamed: foldername_TIFF -> foldername_ZIP.",
    5: "Recursive. ZIP folder created alongside the top-level source folder.",
    6: "Recursive. Only TIFFs inside folders named _EXPORT (Lightroom, Capture One, etc.).",
    7: "Recursive. Only TIFFs inside _EXPORT/TIFF subfolder structure.",
    8: "Recursive. ZIP next to TIFF in same folder. Deletes originals after confirmation.",
    9: "Recursive. Compressed TIFF in place. Originals moved to OLD_TIFFs/ subfolder.",
}


def step_mode(cfg: ToolConfig) -> Optional[int]:
    """Select mode 0-9 for Free Compress."""
    if RICH_AVAILABLE and console:
        console.print("\n[bold cyan]Step 2: Organization Mode (Free Compress)[/bold cyan]")
        for m, name in MODE_NAMES.items():
            style = "red" if m == 8 else "green"
            console.print(f"[{m}] [bold {style}]{name}[/bold {style}]")
            console.print(f"    {MODE_DESCS[m]}\n")
        valid = [str(m) for m in MODE_NAMES]
        choice = Prompt.ask("Select mode", choices=valid, default=str(cfg.config.last_mode or 0))
    else:
        print("\n--- Mode (0-9) ---")
        for m, name in MODE_NAMES.items():
            warning = " [!]️" if m == 8 else ""
            print(f"[{m}] {name}{warning}")
            print(f"    {MODE_DESCS[m]}\n")
        valid = [str(m) for m in MODE_NAMES]
        # Same default as the Rich path, which offers the last mode used
        _default_mode = str(cfg.config.last_mode or 0)
        choice = input(f"Mode [{_default_mode}]: ").strip() or _default_mode

    try:
        mode = int(choice)
        if 0 <= mode <= 9:
            return mode
    except ValueError:
        pass
    return None


# --- Folder Selection ----------------------------------------------

def step_folder(cfg: ToolConfig, prompt_text: str = "Input folder") -> Optional[Path]:
    """Choose input folder."""
    default = cfg.config.last_input_dir or str(Path.cwd())
    if RICH_AVAILABLE and console:
        folder = Prompt.ask(f"[cyan]{prompt_text}[/cyan]", default=default).strip()
    else:
        folder = input(f"{prompt_text} [{default}]: ").strip() or default
    # Strip surrounding quotes from pasted/dragged paths
    if len(folder) >= 2 and folder[0] == folder[-1] and folder[0] in ("'", '"'):
        folder = folder[1:-1]
    p = Path(folder)
    if not p.exists():
        if RICH_AVAILABLE and console:
            console.print(f"[red]Folder not found: {escape(folder)}[/red]")
        else:
            print(f"ERROR: Folder not found: {folder}")
        return None
    if not p.is_dir():
        if RICH_AVAILABLE and console:
            console.print(f"[red]Not a directory: {escape(folder)}[/red]")
        else:
            print(f"ERROR: Not a directory: {folder}")
        return None
    # ';' breaks the multi-folder contract (paths are joined/split on ';')
    if ";" in str(p):
        if RICH_AVAILABLE and console:
            console.print(f"[red]Folder path contains ';' which is not supported: {escape(str(p))}[/red]")
        else:
            print(f"ERROR: Folder path contains ';' which is not supported: {p}")
        return None
    cfg.config.last_input_dir = str(p.resolve())
    return p


# --- Basic Parameters ---------------------------------------------

def _supports_cap_workers(workflow: Dict) -> bool:
    """True only for workflows whose backend is compress_tiff_zip.ps1.

    The Copy EXIF backends have no -CapWorkers parameter (they take -Workers directly),
    so asking the question there produced an answer nothing could act on. -ExcludeFolders
    is the opposite call: it now exists on all three backends and is emitted for all of them.
    """
    return workflow.get("origin") in ("free_compress", "compress", "both")


def step_basic_params(cfg: ToolConfig, workflow: Dict) -> bool:
    """Workers, DryRun, Staging."""
    # The backend this run will actually launch: each one's param() defaults are the
    # source of the prompt defaults below (audit round 17).
    _ref = "compress_tiff_zip.ps1" if _supports_cap_workers(workflow) else ("copy_exif_to_TIFF_ps5.ps1" if cfg.config.ps_name == "powershell" else "copy_exif_to_TIFF_ps7.ps1")
    safe_default = _backend_default(_ref, "SafeMode")
    skip_default = (_backend_default("compress_tiff_zip.ps1", "SkipLzwAsCompressed")
                    if _ref == "compress_tiff_zip.ps1" else False)
    cap_default = _backend_default("compress_tiff_zip.ps1", "CapWorkers")
    if RICH_AVAILABLE and console:
        workers_str = Prompt.ask(
            "[cyan]Workers[/cyan]",
            default=str(cfg.config.last_workers or cfg.config.default_workers)
        ).strip()
        try:
            workers = int(workers_str)
            if workers < 1:
                raise ValueError()
            clamped = clamp_workers(workers, cfg.config.default_workers)
            if clamped != workers:
                console.print(f"[yellow]Workers clamped to {clamped} (backend accepts 1-{MAX_WORKERS}).[/yellow]")
            workflow["workers"] = clamped
        except ValueError:
            console.print("[red]Invalid, using default.[/red]")
            workflow["workers"] = clamp_workers(cfg.config.default_workers)

        staging = Prompt.ask(
            "[cyan]Staging folder (SSD for faster I/O)[/cyan]",
            default=cfg.config.last_staging or cfg.config.staging_dir or ""
        ).strip()
        workflow["staging"] = staging
        cfg.config.last_staging = staging

        workflow["dry_run"] = Confirm.ask("Dry-run mode?", default=False)
        
        # SafeMode and SkipLzw options
        workflow["safe_mode"] = Confirm.ask("[cyan]Safe mode?[/cyan] (skip multi-page TIFFs)", default=safe_default)
        workflow["skip_lzw"] = Confirm.ask("[cyan]Skip LZW as already compressed?[/cyan] (LZW files will be ignored)", default=skip_default)

        # -CapWorkers only exists on compress_tiff_zip.ps1; asking it for a Copy EXIF
        # workflow produced an answer that was silently dropped by the command builder.
        if _supports_cap_workers(workflow):
            cap_str = Prompt.ask("[cyan]Cap workers?[/cyan] (0 = no cap)", default=str(cap_default))
            try:
                workflow["cap_workers"] = max(0, min(int(cap_str), MAX_WORKERS))
            except ValueError:
                console.print(f"[yellow]Invalid cap '{cap_str}', using {cap_default}[/yellow]")
                workflow["cap_workers"] = cap_default

        excl_default = cfg.config.last_exclude_folders or ""
        excl = Prompt.ask(
            "[cyan]Exclude folders?[/cyan] (';'-separated folder names, '-' = none)",
            default=excl_default or "-"
        ).strip()
        if excl in ("-", "", "none"):
            workflow["exclude_folders"] = ""
            cfg.config.last_exclude_folders = None
        else:
            workflow["exclude_folders"] = excl
            cfg.config.last_exclude_folders = excl

        # ForceParallel/ForceSequential: offer to toggle detected behavior
        if cfg.config.ps_major >= 7:
            if Confirm.ask("[yellow]Force sequential? (override parallelism)[/yellow]", default=False):
                workflow["force_sequential"] = True
        else:
            if Confirm.ask("[yellow]Force parallel? (enable parallelism via -ForceParallel)[/yellow]", default=False):
                workflow["force_parallel"] = True
    else:
        workers_str = input(f"Workers [{(cfg.config.last_workers or cfg.config.default_workers)}]: ").strip()
        try:
            raw_workers = int(workers_str) if workers_str else (cfg.config.last_workers or cfg.config.default_workers)
        except ValueError:
            raw_workers = cfg.config.last_workers or cfg.config.default_workers
        workflow["workers"] = clamp_workers(raw_workers, cfg.config.default_workers)
        if workflow["workers"] != raw_workers:
            print(f"Workers clamped to {workflow['workers']} (backend accepts 1-{MAX_WORKERS}).")
        staging = input(f"Staging folder (empty=disabled) []: ").strip()
        workflow["staging"] = staging
        cfg.config.last_staging = staging
        dry = input("Dry-run? [y/N]: ").strip().lower()
        workflow["dry_run"] = (dry == "y")
        safe = input(f"Safe mode? (skip multi-page TIFFs) [{'Y/n' if safe_default else 'y/N'}]: ").strip().lower()
        workflow["safe_mode"] = (False if safe in ("n", "no")
                                 else True if safe in ("y", "yes") else safe_default)
        skip_lzw = input(f"Skip LZW as already compressed? (LZW files will be ignored) [{'Y/n' if skip_default else 'y/N'}]: ").strip().lower()
        workflow["skip_lzw"] = (True if skip_lzw in ("y", "yes")
                                else False if skip_lzw in ("n", "no") else skip_default)
        if _supports_cap_workers(workflow):
            cap_str = input(f"Cap workers? (0 = no cap) [{cap_default}]: ").strip()
            try:
                workflow["cap_workers"] = max(0, min(int(cap_str), MAX_WORKERS)) if cap_str else cap_default
            except ValueError:
                print(f"Invalid cap '{cap_str}', using {cap_default}")
                workflow["cap_workers"] = cap_default
        excl_default = cfg.config.last_exclude_folders or ""
        excl = input(f"Exclude folders? (';'-separated names, '-' = none) [{excl_default or '-'}]: ").strip()
        if excl == "":
            excl = excl_default
        if excl in ("-", "none"):
            workflow["exclude_folders"] = ""
            cfg.config.last_exclude_folders = None
        else:
            workflow["exclude_folders"] = excl
            cfg.config.last_exclude_folders = excl
        if cfg.config.ps_major >= 7:
            fp = input("Force sequential? (y/N): ").strip().lower()
            if fp == "y":
                workflow["force_sequential"] = True
        else:
            fp = input("Force parallel? (y/N): ").strip().lower()
            if fp == "y":
                workflow["force_parallel"] = True

    return True


# --- Summary Panel ------------------------------------------------

def step_confirm(workflow: Dict, cfg: ToolConfig) -> bool:
    """Show summary and confirm."""
    origin = workflow.get("origin", "?")
    dest = workflow.get("dest", "?")
    mode = workflow.get("mode", "?")
    dry = "Yes" if workflow.get("dry_run") else "No"
    folders_count = len(workflow.get("folders", []))

    if RICH_AVAILABLE and console:
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(style="bold cyan")
        table.add_column()
        if origin in ("copy_exif",):
            table.add_row("Workflow:", f"Copy EXIF ({origin})")
        elif origin == "free_compress":
            table.add_row("Workflow:", f"Compress TIFFs")
        elif origin in ("compress", "both"):
            table.add_row("Workflow:", f"Fuji: Compress ({origin})")

        if origin != "free_compress" and folders_count > 1:
            pattern_val = workflow.get("pattern", [])
            pattern_str = ", ".join(pattern_val) if isinstance(pattern_val, list) else str(pattern_val)
            table.add_row("Pattern:", pattern_str or "?")
            table.add_row("Sessions:", f"{folders_count} folder(s)")
        elif origin == "free_compress":
            table.add_row("Mode:", f"{mode} - {MODE_NAMES.get(mode, '?')}")
            table.add_row("Folder:", workflow.get("input_dir", "?")[:60])
        else:
            table.add_row("Folder:", workflow.get("folders", [workflow.get("input_dir")])[0].name if workflow.get("folders") else workflow.get("input_dir", "?"))

        table.add_row("Workers:", str(workflow.get("workers", 8)))
        if workflow.get("cap_workers"):
            table.add_row("Cap workers:", str(workflow["cap_workers"]))
        if workflow.get("exclude_folders"):
            table.add_row("[yellow]Excluding:[/yellow]", workflow["exclude_folders"])
        table.add_row("Staging:", workflow.get("staging") or "disabled")
        table.add_row("Dry-run:", dry)
        if workflow.get("force_parallel"):
            table.add_row("[yellow]Parallelism:[/yellow]", "FORCED ON (-ForceParallel)")
        elif workflow.get("force_sequential"):
            table.add_row("[yellow]Parallelism:[/yellow]", "FORCED OFF (-ForceSequential)")

        if origin == "free_compress" and mode == 8:
            table.add_row("[red]Delete source:[/red]", "ON -- originals will be DELETED")

        console.print(Panel(table, title="[bold]Summary[/bold]", border_style="green"))
        if not Confirm.ask("[yellow]Proceed?[/yellow]", default=True):
            console.print("[dim]Cancelled.[/dim]")
            return False
    else:
        print("\n=== Summary ===")
        print(f"  Workflow: {origin}")
        if origin == "free_compress":
            print(f"  Mode: {mode} - {MODE_NAMES.get(mode, '?')}")
            print(f"  Folder: {workflow.get('input_dir', '?')}")
        else:
            print(f"  Pattern: {workflow.get('pattern', '?')}")
            print(f"  Sessions: {folders_count} folder(s)")
        print(f"  Workers: {workflow.get('workers', 8)}")
        if workflow.get("cap_workers"):
            print(f"  Cap workers: {workflow['cap_workers']}")
        if workflow.get("exclude_folders"):
            print(f"  Excluding: {workflow['exclude_folders']}")
        print(f"  Staging: {workflow.get('staging') or 'disabled'}")
        print(f"  Dry-run: {dry}")
        if workflow.get("force_parallel"):
            print(f"  Parallelism: FORCED ON (-ForceParallel)")
        elif workflow.get("force_sequential"):
            print(f"  Parallelism: FORCED OFF (-ForceSequential)")
        confirm = input("Proceed? [Y/n]: ").strip().lower()
        if confirm == "n":
            print("Cancelled.")
            return False

    return True


# --- Command Builders ----------------------------------------------

def _wrap_ps5_command(cmd: List[str]) -> List[str]:
    """powershell.exe 5.1 cannot bind switch values like -SafeMode:$false passed
    via -File (command-line args arrive as strings). Wrap the invocation in
    -Command so $true/$false literals are evaluated by the PowerShell parser."""
    import re
    exe, rest = cmd[0], cmd[1:]
    try:
        file_idx = rest.index("-File")
        script = rest[file_idx + 1]
        args = rest[file_idx + 2:]
    except (ValueError, IndexError):
        return cmd
    prefix = rest[:file_idx]

    def _quote(a: str) -> str:
        # Parameter names and $bool switch values must stay unquoted to bind correctly
        if re.fullmatch(r"-\w+(:\$(?:true|false))?", a):
            return a
        return "'" + a.replace("'", "''") + "'"

    invocation = "& " + _quote(script) + (" " + " ".join(_quote(a) for a in args) if args else "")
    return [exe] + prefix + ["-Command", invocation]


def _unwrap_ps5_invocation(invocation: str) -> Optional[List[str]]:
    """Exact inverse of the quoting _wrap_ps5_command applies.

    Returns [script, *args], or None when the string does not have the shape that
    function produces (never guess: a mis-parsed argument list drives a converter that
    can delete sources in mode 8)."""
    s = invocation.strip()
    if not s.startswith("& "):
        return None
    s = s[2:]
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == " ":
            i += 1
            continue
        if s[i] == "'":
            i += 1
            buf = []
            closed = False
            while i < n:
                if s[i] == "'":
                    # '' is an escaped single quote inside a single-quoted PowerShell string
                    if i + 1 < n and s[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                buf.append(s[i])
                i += 1
            if not closed:
                return None
            out.append("".join(buf))
        else:
            j = s.find(" ", i)
            if j < 0:
                j = n
            out.append(s[i:j])
            i = j
    return out or None


def _journal_parts(cmd: List[str]) -> Optional[List[str]]:
    """Reduce a built PowerShell command line to its shell-neutral [script, *args].

    A journalled command has to survive the PowerShell available changing between the run
    and the repeat. Replaying a `-File ... -SafeMode:$false` command under powershell.exe
    5.1 silently binds the STRING '$false' -- which is truthy -- so the repeat ran with
    SafeMode ON, the exact trap _wrap_ps5_command exists to avoid; and a Copy EXIF command
    kept pointing at the backend written for the other shell. Stripping the wrapper here
    lets run_repeat_last rebuild the invocation for whatever PowerShell is detected now.

    Returns None when the shape is not recognised, so the caller can refuse instead of
    running something it does not understand."""
    if not cmd:
        return None
    if "-File" in cmd:
        i = cmd.index("-File")
        return cmd[i + 1:] if i + 1 < len(cmd) else None
    if "-Command" in cmd:
        i = cmd.index("-Command")
        return _unwrap_ps5_invocation(cmd[i + 1]) if i + 1 < len(cmd) else None
    return None


def _journaled_script_path(parts: List[str], ps_name: str) -> Path:
    """The backend a journalled command will actually run under `ps_name`.

    The two Copy EXIF backends are not interchangeable -- one is the PS5 sequential
    implementation, the other drives PS7 runspaces -- so the script follows the shell we
    are about to run, not the shell that happened to record the journal."""
    script = Path(parts[0])
    if script.name in ("copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_TIFF_ps7.ps1"):
        return SCRIPT_DIR / ("copy_exif_to_TIFF_ps5.ps1" if ps_name == "powershell"
                             else "copy_exif_to_TIFF_ps7.ps1")
    return script


def _rebuild_journaled_command(parts: List[str], ps_name: str,
                               non_interactive: bool = False) -> List[str]:
    """Turn a shell-neutral [script, *args] back into a command for `ps_name`.

    `non_interactive` (--run-preset) adds -NonInteractive, so a prompt the
    backend might still ask cannot wait forever on a closed stdin; the flag is
    inserted before the PS5 wrap, which preserves the prefix."""
    cmd = [ps_name, "-NoProfile", "-File", str(_journaled_script_path(parts, ps_name))] + list(parts[1:])
    if non_interactive:
        cmd = cmd[:2] + ["-NonInteractive"] + cmd[2:]
    if ps_name == "powershell":
        cmd = _wrap_ps5_command(cmd)
    return cmd


def build_compress_command(workflow: Dict, folders: List[Path] = None, ps_name: str = "pwsh") -> List[str]:
    """Build powershell command for compress_tiff_zip.ps1."""
    script = SCRIPT_DIR / "compress_tiff_zip.ps1"
    cmd = [ps_name, "-NoProfile", "-File", str(script)]

    if workflow.get("mode") is not None:
        cmd += ["-Mode", str(workflow["mode"])]

    if folders and len(folders) == 1:
        cmd += ["-InputDir", str(folders[0])]
    elif folders and len(folders) > 1:
        # Pass as semicolon-separated list, script handles each
        folder_list = ";".join(str(f) for f in folders)
        cmd += ["-InputDir", folder_list]

    # Mode 2 (flat) writes into the input root when -OutputDir is missing, which mixes
    # the flattened copies with the sources and overwrites root-level TIFFs in place.
    if workflow.get("output_dir"):
        cmd += ["-OutputDir", workflow["output_dir"]]
    if workflow.get("duplicate_action"):
        cmd += ["-DuplicateAction", str(workflow["duplicate_action"])]

    if workflow.get("staging"):
        cmd += ["-StagingDir", workflow["staging"]]
    if workflow.get("workers"):
        cmd += ["-Workers", str(clamp_workers(workflow["workers"]))]
    if workflow.get("cap_workers") is not None:
        cmd += ["-CapWorkers", str(int(workflow["cap_workers"]))]
    if workflow.get("exclude_folders"):
        cmd += ["-ExcludeFolders", workflow["exclude_folders"]]
    if workflow.get("dry_run"):
        cmd += ["-DryRun"]
    # Passed explicitly whenever the wizard decided it -- even when it equals the
    # default: omitting it let a backend whose own default was edited run with a
    # different value than the summary showed (audit round 17, the jxl-photo #467 class).
    if isinstance(workflow.get("safe_mode"), bool):
        cmd += ["-SafeMode:$true" if workflow["safe_mode"] else "-SafeMode:$false"]
    if isinstance(workflow.get("skip_lzw"), bool):
        cmd += ["-SkipLzwAsCompressed:$true" if workflow["skip_lzw"] else "-SkipLzwAsCompressed:$false"]
    if workflow.get("overwrite"):
        cmd += ["-Overwrite"]
    if workflow.get("delete_source"):
        cmd += ["-DeleteSource"]
    if workflow.get("force_parallel") == True:
        cmd += ["-ForceParallel"]
    if workflow.get("force_sequential") == True:
        cmd += ["-ForceSequential"]
    
    # Thumbnail generation
    if workflow.get("generate_thumbnail"):
        cmd += ["-GenerateThumbnail"]
        if workflow.get("thumb_size"):
            cmd += ["-ThumbSize", str(workflow["thumb_size"])]
        if workflow.get("thumb_quality"):
            cmd += ["-ThumbQuality", str(workflow["thumb_quality"])]
        if workflow.get("thumb_format"):
            cmd += ["-ThumbFormat", str(workflow["thumb_format"])]
        if workflow.get("thumb_page"):
            cmd += ["-ThumbPage", str(workflow["thumb_page"])]
        if workflow.get("skip_compressed_with_thumb"):
            cmd += ["-SkipCompressedWithThumb"]

    if ps_name == "powershell":
        cmd = _wrap_ps5_command(cmd)

    return cmd


def build_copy_exif_command(workflow: Dict, folders: List[Path] = None, extra_flags: List[str] = None, ps_name: str = "pwsh") -> List[str]:
    """Build powershell command for copy_exif_to_TIFF.ps1."""
    if ps_name == "powershell":
        script = SCRIPT_DIR / "copy_exif_to_TIFF_ps5.ps1"
    else:
        script = SCRIPT_DIR / "copy_exif_to_TIFF_ps7.ps1"
    cmd = [ps_name, "-NoProfile", "-File", str(script)]

    if folders and len(folders) == 1:
        cmd += ["-InputDir", str(folders[0])]
    elif folders and len(folders) > 1:
        folder_list = ";".join(str(f) for f in folders)
        cmd += ["-InputDir", folder_list]

    if workflow.get("workers"):
        cmd += ["-Workers", str(clamp_workers(workflow["workers"]))]
    if workflow.get("staging"):
        cmd += ["-StagingDir", workflow["staging"]]
    if workflow.get("output_dir"):
        cmd += ["-OutputDir", workflow["output_dir"]]
    if workflow.get("dry_run"):
        cmd += ["-DryRun"]
    if workflow.get("skip_exif"):
        cmd += ["-SkipIfTiffHasExif"]
    if workflow.get("compress_zip"):
        cmd += ["-CompressZip"]
    if isinstance(workflow.get("safe_mode"), bool):
        cmd += ["-SafeMode:$true" if workflow["safe_mode"] else "-SafeMode:$false"]
    # -SkipLzwAsCompressed is a [switch] in the Copy EXIF backends (a [bool] in
    # compress_tiff_zip.ps1): its default is OFF by type, so only "yes" is passed.
    if workflow.get("skip_lzw"):
        cmd += ["-SkipLzwAsCompressed:$true"]
    if workflow.get("overwrite"):
        cmd += ["-Overwrite"]
    # step_basic_params asks this for every workflow. It used to be dropped here, so in
    # workflow 4 the exclusion applied to the Compress step and silently not to the
    # Copy EXIF step of the same run.
    if workflow.get("exclude_folders"):
        cmd += ["-ExcludeFolders", workflow["exclude_folders"]]

    if extra_flags:
        cmd += extra_flags

    if ps_name == "powershell":
        cmd = _wrap_ps5_command(cmd)

    return cmd


# --- Subprocess Runner ----------------------------------------------

def _decode_console_line(raw: bytes) -> str:
    """Decode subprocess output: try UTF-8, fall back to the console OEM codepage
    (Windows PowerShell 5 emits OEM, e.g. cp850, not UTF-8)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        enc = locale.getpreferredencoding(False)
        if sys.platform == "win32":
            try:
                import ctypes
                enc = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
            except Exception:
                pass
        return raw.decode(enc, errors="replace")


# CreateProcess fails past 32767 characters. A Fuji preset redoes the AutoFind on
# every run, so its ';'-joined -InputDir grows with every session (round 22).
_CMDLINE_SPILL_CHARS = 24000
_INPUTDIR_FILE_BACKENDS = ("compress_tiff_zip.ps1", "copy_exif_to_TIFF_ps5.ps1",
                           "copy_exif_to_TIFF_ps7.ps1")


def _spill_long_input_dir(cmd: List[str]) -> Tuple[List[str], Optional[str]]:
    """Swap a too-long -InputDir for -InputDirFile <temp file> (one folder per line).

    Returns (command, temp path or None). The caller deletes the temp file once the
    process has ended. Commands that are short enough, or that do not run one of
    _INPUTDIR_FILE_BACKENDS, or that carry no -InputDir, come back unchanged."""
    if len(subprocess.list2cmdline([str(a) for a in cmd])) <= _CMDLINE_SPILL_CHARS:
        return cmd, None
    parts = _journal_parts([str(a) for a in cmd])
    if not parts or Path(parts[0]).name not in _INPUTDIR_FILE_BACKENDS:
        return cmd, None
    if "-InputDir" not in parts:
        return cmd, None
    i = parts.index("-InputDir")
    if i + 1 >= len(parts):
        return cmd, None
    folders = [f for f in parts[i + 1].split(";") if f.strip()]
    fd, list_path = tempfile.mkstemp(prefix="tw_inputdirs_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(folders) + "\n")
    new_parts = parts[:i] + ["-InputDirFile", list_path] + parts[i + 2:]
    cmd = [str(a) for a in cmd]
    if "-File" in cmd:
        j = cmd.index("-File")
        return cmd[:j + 1] + new_parts, list_path
    j = cmd.index("-Command")
    return _wrap_ps5_command(cmd[:j] + ["-File"] + new_parts), list_path


def run_subprocess(cmd: List[str], timeout: Optional[int] = None,
                   stdin=None) -> int:
    """Run command, stream output with Rich coloring. Has configurable timeout.

    `stdin` is passed through to Popen when not None (the unattended preset
    path closes it with DEVNULL, so no prompt can wait on the console)."""
    import time
    import sys
    import threading

    cmd, _spill_path = _spill_long_input_dir(cmd)

    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        # Binary mode: lines are decoded manually in the reader thread
        # (UTF-8 with locale fallback, see _decode_console_line).
    }
    if stdin is not None:
        popen_kwargs["stdin"] = stdin
    if sys.platform == "win32":
        # Keep the child in the same console process group so Ctrl+C propagates.
        popen_kwargs["creationflags"] = 0

    try:
        try:
            process = subprocess.Popen(cmd, **popen_kwargs)
        except (FileNotFoundError, OSError) as e:
            if RICH_AVAILABLE and console:
                console.print(f"[red]ERROR: Failed to start process: {e}[/red]")
            else:
                print(f"ERROR: Failed to start process: {e}")
            return -1
        output_queue = []
        queue_lock = threading.Lock()
        stop_reader = threading.Event()

        def reader():
            try:
                for raw_line in process.stdout:
                    line = _decode_console_line(raw_line)
                    with queue_lock:
                        output_queue.append(line)
            except Exception:
                pass
            finally:
                stop_reader.set()

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        def _kill_tree():
            try:
                if sys.platform == "win32":
                    _run_captured(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        10,
                    )
                else:
                    process.kill()
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        start_time = time.time()

        def _drain_lines():
            with queue_lock:
                pending = output_queue[:]
                output_queue.clear()
            for line in pending:
                line = line.strip()
                if not line:
                    continue
                if RICH_AVAILABLE and console:
                    if " OK " in line or "+ZIP" in line:
                        console.print(f"  [green]{escape(line)}[/green]")
                    elif " | ERROR |" in line or " ERROR " in line:
                        console.print(f"  [red]{escape(line)}[/red]")
                    elif " | WARN |" in line or "WARNING" in line:
                        console.print(f"  [yellow]{escape(line)}[/yellow]")
                    elif "DRY" in line:
                        console.print(f"  [blue]{escape(line)}[/blue]")
                    else:
                        console.print(f"  {escape(line)}")
                else:
                    print(f"  {line}")

        try:
            while True:
                _drain_lines()

                if stop_reader.is_set() and process.poll() is not None:
                    _drain_lines()  # final drain: the reader may queue the last lines after our check
                    break

                if timeout is not None and time.time() - start_time > timeout:
                    _kill_tree()
                    if RICH_AVAILABLE and console:
                        console.print(f"[red]ERROR: Process timed out after {timeout}s[/red]")
                    else:
                        print(f"ERROR: Process timed out after {timeout}s")
                    return -1

                time.sleep(0.05)
        except KeyboardInterrupt:
            _kill_tree()
            if RICH_AVAILABLE and console:
                console.print("[yellow]Interrupted by user.[/yellow]")
            else:
                print("Interrupted by user.")
            return -1
        except Exception as e:
            _kill_tree()
            if RICH_AVAILABLE and console:
                console.print(f"[red]ERROR: {e}[/red]")
            else:
                print(f"ERROR: {e}")
            return -1

        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree()
            return -1

        if process.returncode != 0:
            if RICH_AVAILABLE and console:
                console.print(f"[red]WARNING: Process exited with code {process.returncode}[/red]")
            else:
                print(f"WARNING: Process exited with code {process.returncode}")
        return process.returncode
    finally:
        if _spill_path:
            try:
                os.unlink(_spill_path)
            except OSError:
                pass


# --- Undo OLD_TIFFs ------------------------------------------------

def run_undo_old_tiffs(cfg: ToolConfig) -> bool:
    """Move TIFFs from OLD_TIFFs/ back to parent folder."""
    folder = step_folder(cfg, "Root folder to scan for OLD_TIFFs")
    if folder is None:
        return False

    # Find all OLD_TIFFs folders recursively
    old_dirs = sorted([d for d in folder.rglob("*") if d.is_dir() and d.name.lower() == "old_tiffs"])
    if not old_dirs:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No OLD_TIFFs folders found.[/yellow]")
        else:
            print("No OLD_TIFFs folders found.")
        return True

    # Count only what the move loop below will actually touch: counting every file made the
    # header promise more than the run delivered whenever sidecars sat in OLD_TIFFs/.
    def _restorable(od) -> list:
        return [f for f in Path(od).glob("*")
                if f.is_file() and f.suffix.lower() in (".tif", ".tiff")]

    counts = {od: len(_restorable(od)) for od in old_dirs}
    total_files = sum(counts.values())

    if RICH_AVAILABLE and console:
        console.print(f"\n[cyan]Found {len(old_dirs)} OLD_TIFFs folder(s) with {total_files} TIFF(s)[/cyan]")
        for od in old_dirs:
            console.print(f"  {counts[od]:>4} TIFFs: {escape(str(od))}")
    else:
        print(f"\nFound {len(old_dirs)} OLD_TIFFs folder(s) with {total_files} TIFF(s):")
        for od in old_dirs:
            print(f"  {counts[od]:>4} TIFFs: {od}")

    if RICH_AVAILABLE and console:
        overwrite = Confirm.ask("\n[yellow]Overwrite existing files in parent folder?[/yellow]", default=False)
        if not overwrite:
            console.print("[dim]Will skip files that already exist in parent folder.[/dim]")
    else:
        resp = input("\nOverwrite existing files in parent folder? [y/N]: ").strip().lower()
        overwrite = (resp == "y")
        if not overwrite:
            print("Will skip files that already exist in parent folder.")

    if RICH_AVAILABLE and console:
        if not Confirm.ask("\n[yellow]Move all files back to parent folder?[/yellow]", default=True):
            console.print("[dim]Cancelled.[/dim]")
            return False
    else:
        resp = input("\nMove all files back to parent folder? [Y/n]: ").strip().lower()
        if resp == "n":
            print("Cancelled.")
            return False

    # Move files
    moved = 0
    skipped = 0
    failed = 0

    def _try_move(src: Path, dest: Path, label: str) -> bool:
        """
        One unmovable file must not abandon the rest of the restore. _safe_move was called
        bare, so a locked/read-only destination (or a directory sharing the name) raised
        straight out of the workflow: files after it were never attempted, no summary was
        printed, and the traceback killed the wizard. run_purge_old_tiffs already works
        per-file this way.
        """
        nonlocal moved, failed
        try:
            _safe_move(src, dest)
        except Exception as e:
            failed += 1
            if RICH_AVAILABLE and console:
                console.print(f"  [red]FAILED: {escape(src.name)} -- {escape(str(e))}[/red]")
            else:
                print(f"  FAILED: {src.name} -- {e}")
            return False
        moved += 1
        if RICH_AVAILABLE and console:
            console.print(f"  [green]{label}: {escape(src.name)}[/green]")
        else:
            print(f"  {label}: {src.name}")
        return True

    for od in old_dirs:
        old_path = Path(od)
        parent = old_path.parent
        for f in list(old_path.glob("*")):
            # audit round 16: is_file, not just exists -- a DIRECTORY named "*.tif" made
            # _safe_move raise IsADirectoryError and count as FAILED, and a dangling
            # symlink reported exists()==False yet passed the suffix check.
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".tif", ".tiff"):
                continue
            dest = parent / f.name
            if dest.exists():
                if overwrite:
                    _try_move(f, dest, "OVERWRITE")
                else:
                    skipped += 1
                    if RICH_AVAILABLE and console:
                        console.print(f"  [yellow]SKIP (exists): {escape(f.name)}[/yellow]")
                    else:
                        print(f"  SKIP (exists): {f.name}")
            else:
                _try_move(f, dest, "MOVED")

    # Ask to delete empty OLD_TIFFs folders
    def _remove_empty_dirs():
        for od in old_dirs:
            try:
                if any(Path(od).glob("*")):
                    continue
                Path(od).rmdir()
            except OSError as e:
                if RICH_AVAILABLE and console:
                    console.print(f"  [yellow]Could not remove {escape(str(od))}: {escape(str(e))}[/yellow]")
                else:
                    print(f"  Could not remove {od}: {e}")
                continue
            if RICH_AVAILABLE and console:
                console.print(f"  [green]Removed: {escape(str(od))}[/green]")
            else:
                print(f"  Removed: {od}")

    if RICH_AVAILABLE and console:
        if Confirm.ask("\n[cyan]Delete empty OLD_TIFFs folders?[/cyan]", default=False):
            _remove_empty_dirs()
    else:
        resp = input("\nDelete empty OLD_TIFFs folders? [y/N]: ").strip().lower()
        if resp == "y":
            _remove_empty_dirs()

    parts = [f"Moved {moved} file(s)"]
    if skipped > 0:
        parts.append(f"skipped {skipped}")
    if failed > 0:
        parts.append(f"FAILED {failed}")
    summary = "Done. " + ", ".join(parts) + "."
    if RICH_AVAILABLE and console:
        console.print(f"\n[{'red' if failed else 'green'}]{escape(summary)}[/{'red' if failed else 'green'}]")
    else:
        print(f"\n{summary}")

    return failed == 0


def _strip_magick_noise(text: str) -> str:
    """Remove ImageMagick's own warning lines before a metric is parsed out of the text.

    `magick compare -metric RMSE ... null:` prints the metric on stderr and mixes its TIFF
    warnings into the same stream. On real scanner files that reads:

        0 (0)compare: Wrong data type 3 for "PixelXDimension"; tag ignored. ... @ warning/...

    The metric happens to come first, so the regex below still finds it -- but this text
    feeds the gate that authorises permanently deleting an original, and a warning emitted
    while READING (before the metric) would be parsed as the metric instead. Drop anything
    that is tool chatter and parse only what is left.
    """
    cleaned = re.sub(r"(?:compare|magick|identify|convert)\s*:.*?(?:\n|$)", " ", text or "")
    return re.sub(r"@\s+(?:warning|error)/\S+", " ", cleaned)


def _tiff_page_geometry(path) -> Optional[List[Tuple[str, int, int]]]:
    """(subfiletype, width, height) for every page, or None when it cannot be read.

    One `identify` call, same format string the PowerShell page classifiers use. None means
    "cannot classify" and every caller must treat it as a refusal, never as "no thumbnails".
    """
    try:
        r = _run_captured(
            ["magick", "identify", "-ping", "-format", "%[tiff:subfiletype]|%w|%h\n", str(path)],
            _work_timeout(30, path), text=True,
        )
        if r.returncode != 0 or not r.stdout:
            return None
    except Exception:
        return None

    geo: List[Tuple[str, int, int]] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split("|")
        if len(parts) < 3:
            return None
        try:
            geo.append((parts[0].strip().upper(), int(parts[1]), int(parts[2])))
        except ValueError:
            return None
    return geo or None


def _image_page_indices(geo: List[Tuple[str, int, int]]) -> List[int]:
    """Indices of the pages that carry the actual image, i.e. everything that is not a
    thumbnail. Page 0 is always an image page.

    A later page counts as a thumbnail only when it is BOTH tagged REDUCEDIMAGE/REDUCED
    AND strictly smaller than page 0. That is deliberately stricter than SafeMode's rule
    (which accepts either), because the only caller is the purge gate: being wrong here
    deletes the last backup of a page. Every thumbnail this toolkit writes is tagged
    (Set-TiffThumbMarkers), so the strict rule still covers the case it exists for; an
    untagged small page is simply not excused, and the file is kept.

    A full-size scanner IR (MASK) page is an image page under either rule.
    """
    if not geo:
        return []
    main_w, main_h = geo[0][1], geo[0][2]
    keep = [0]
    for i, (subfile, w, h) in enumerate(geo[1:], start=1):
        is_thumb = (subfile in ("REDUCEDIMAGE", "REDUCED")
                    and main_w > 0 and main_h > 0 and 0 < w < main_w and 0 < h < main_h)
        if not is_thumb:
            keep.append(i)
    return keep


# Structural TIFF tags a lossless recompress legitimately rewrites (new strip layout,
# new compression, magick's own stamps). Excluded from the EXIF-presence check below so
# a healthy uncompressed -> ZIP pair still passes the purge gate.
_METADATA_STRUCTURAL_TAGS = frozenset({
    "SubfileType", "ImageWidth", "ImageHeight", "BitsPerSample", "Compression",
    "PhotometricInterpretation", "FillOrder", "StripOffsets", "RowsPerStrip",
    "StripByteCounts", "PlanarConfiguration", "SamplesPerPixel", "PageNumber",
    "Predictor", "TileWidth", "TileLength", "TileOffsets", "TileByteCounts",
    "ThumbnailOffset", "ThumbnailLength", "JPEGTables",
    "ModifyDate", "DateTime", "Software",
})


def _tiff_metadata_signature(path) -> Optional[Dict]:
    """Key metadata of a TIFF for the purge gate, or None when it cannot be read.

    audit round 16: the purge verified PIXELS only, so a conversion that dropped
    metadata (a lost per-page SubfileType, an EXIF block or ICC profile that never
    landed) purged the last good backup. Captured per file:
      - per-IFD SubfileType (same exiftool command and parse as _restore_subfile_types),
      - EXIF PRESENCE per page (whether a page's IFD carries any non-structural tag),
        plus presence of non-IFD metadata groups file-wide. Identity is impossible
        here: a bare `magick -compress zip` rewrites IFD0 and does not carry user EXIF
        tags across (verified live), so the comparison is presence, not equality of
        values. Per page because an added/regenerated thumbnail page legitimately
        carries tags page 0 never had,
      - the ICC profile, as a SHA-256 of its raw bytes (identity; verified to survive
        a magick ZIP recompress byte-for-byte).
    None means "cannot know" -- missing exiftool, unreadable tags -- and the caller
    must treat it as a refusal, never as "metadata matches". Fail closed.
    """
    try:
        timeout = _work_timeout(30, path)
        r = _run_exiftool(
            ["-a", "-G1", "-s", "-s", "-n", "-SubfileType", str(path)],
            timeout, text=True,
        )
        if r.returncode != 0:
            return None
        subfile = {}
        for line in (r.stdout or "").splitlines():
            m = re.match(r"^\[IFD(\d+)\]\s+SubfileType:\s*(\d+)\s*$", line.strip())
            if m:
                subfile[int(m.group(1))] = int(m.group(2))

        r = _run_exiftool(
            ["-a", "-G1", "-s", "-EXIF:all", str(path)],
            timeout, text=True,
        )
        if r.returncode != 0:
            return None
        exif_pages: Dict[int, bool] = {}
        exif_other = set()
        for line in (r.stdout or "").splitlines():
            m = re.match(r"^\[([^\]]+)\]\s+(\S+)\s*:\s*(.*)$", line.strip())
            if not m or m.group(2) in _METADATA_STRUCTURAL_TAGS:
                continue
            ifd = re.fullmatch(r"IFD(\d+)", m.group(1))
            if ifd:
                exif_pages[int(ifd.group(1))] = True
            else:
                exif_other.add(m.group(1))

        r = _run_exiftool(
            ["-q", "-q", "-b", "-ICC_Profile", str(path)],
            timeout,
        )
        if r.returncode != 0:
            return None
        icc = r.stdout
        if isinstance(icc, str):  # mocked subprocess results are str
            icc = icc.encode()
        icc_hash = hashlib.sha256(icc).hexdigest() if icc else None
    except Exception:
        return None
    return {"subfile": subfile, "exif_pages": exif_pages,
            "exif_other": exif_other, "icc": icc_hash}


@_scoped_magick_aliases
def _compare_tiff_metadata(old_path: Path, new_path: Path) -> tuple[bool, str]:
    """
    Compare two TIFFs pixel-by-pixel using RMSE.
    Returns (match, details).
    - If RMSE = 0 (within tolerance), images are pixel-identical
    - If dimensions differ, returns False immediately
    - If RMSE > 0, images differ
    """
    import subprocess

    def get_dimensions(path):
        try:
            result = _run_captured(
                ["magick", "identify", "-ping", "-format", "%w %h", f"{_magick_spec(path)}[0]"],
                _work_timeout(30, path), text=True
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if len(parts) >= 2:
                    return int(parts[0]), int(parts[1])
            return None, None
        except FileNotFoundError:
            return None, None
        except Exception:
            return None, None

    def get_page_count(path):
        try:
            result = _run_captured(
                ["magick", "identify", "-ping", "-format", "%n\n", str(path)],
                _work_timeout(30, path), text=True
            )
            if result.returncode == 0 and result.stdout:
                return int(result.stdout.splitlines()[0].strip())
            return None
        except Exception:
            return None

    old_w, old_h = get_dimensions(old_path)
    new_w, new_h = get_dimensions(new_path)

    if old_w is None or new_w is None:
        return False, "magick identify failed"

    if old_w != new_w or old_h != new_h:
        return False, f"DIMENSION_MISMATCH {old_w}x{old_h} vs {new_w}x{new_h}"

    # Page-count check: RMSE below only compares page [0], so a lost extra page must block the
    # purge. This gate authorises permanent deletion, so an unreadable count fails CLOSED --
    # skipping the check on None let a page-losing conversion pass on nothing but page-0 RMSE.
    old_pages = get_page_count(old_path)
    new_pages = get_page_count(new_path)
    if old_pages is None or new_pages is None:
        return False, "page count unreadable (cannot verify pages were preserved)"
    # A differing page count is still a refusal by default -- it is decided below, together
    # with the one exception (a thumbnail page added or regenerated), so that the exception
    # cannot be reached without reading the page layout of BOTH files.

    # Every page is compared pairwise. `magick compare A B` on multi-page files pairs
    # pages across the two lists and returns garbage RMSE (~27% for identical data), so
    # a single whole-file compare cannot be trusted; comparing only [0] would miss a
    # corrupted extra page. This gate authorises permanent deletion, so any page that
    # differs (or cannot be compared) blocks the purge.
    def _pages_identical(pairs) -> tuple[bool, str]:
        for old_page, new_page in pairs:
            try:
                result = _run_captured(
                    ["magick", "compare", "-metric", "RMSE",
                     f"{_magick_spec(old_path)}[{old_page}]", f"{_magick_spec(new_path)}[{new_page}]", "null:"],
                    _work_timeout(COMPARE_TIMEOUT_S, old_path, new_path), text=True
                )
                output = result.stdout.strip() if result.stdout else result.stderr.strip() if result.stderr else ""

                if result.returncode not in (0, 1):
                    return False, f"compare failed on page {old_page}: {output}"

                import re
                output = _strip_magick_noise(output).strip()
                # Same pattern as _is_real_16bit: magick prints RMSE in scientific notation for
                # large values ("1.23457e+06 (0.0188)"), which the old pattern mis-parsed --
                # it matched "06" as the RMSE.
                match = re.search(r"([\d.]+(?:[eE][+-]?\d+)?)\s*\(([\d.eE+-]+)\)", output)
                if not match:
                    return False, f"parse failed on page {old_page}: '{output}'"

                rmse = float(match.group(1))
                if rmse != 0.0:
                    return False, f"PIXEL_DIFF page {old_page} RMSE={rmse}"

            except subprocess.TimeoutExpired:
                return False, f"compare timeout on page {old_page}"
            except FileNotFoundError:
                return False, "ImageMagick not found (magick command missing)"
            except Exception as e:
                return False, f"compare error on page {old_page}: {e}"
        return True, ""

    def _metadata_matches(pairs) -> tuple[bool, str]:
        """audit round 16: pixel equality alone does not prove the backup is redundant --
        a conversion can drop metadata (a per-page SubfileType, the EXIF block, the ICC
        profile) while leaving every pixel intact, and this gate authorises permanently
        deleting the original. Compared on the same page pairs that were pixel-compared:
        per-IFD SubfileType (absent == 0 per the TIFF spec), EXIF presence per group,
        and ICC profile identity. Missing exiftool or unreadable tags refuse the purge.
        """
        old_meta = _tiff_metadata_signature(old_path)
        new_meta = _tiff_metadata_signature(new_path)
        if old_meta is None or new_meta is None:
            return False, "metadata unreadable (exiftool) -- refusing purge"
        for old_page, new_page in pairs:
            old_st = old_meta["subfile"].get(old_page, 0)
            new_st = new_meta["subfile"].get(new_page, 0)
            if old_st != new_st:
                return False, f"SUBFILETYPE_MISMATCH page {old_page}: {old_st} vs {new_st}"
            # EXIF presence on the compared page: an added/regenerated thumbnail page
            # legitimately carries tags page 0 never had, so only aligned pages compare.
            if old_meta["exif_pages"].get(old_page, False) != new_meta["exif_pages"].get(new_page, False):
                return False, f"EXIF_MISMATCH page {old_page} (metadata block present on only one side)"
        if old_meta["exif_other"] != new_meta["exif_other"]:
            return False, ("EXIF_MISMATCH (metadata groups differ: "
                           f"{sorted(old_meta['exif_other'])} vs {sorted(new_meta['exif_other'])})")
        if old_meta["icc"] != new_meta["icc"]:
            return False, "ICC_PROFILE_MISMATCH"
        return True, ""

    if old_pages == new_pages:
        strict_ok, strict_detail = _pages_identical([(i, i) for i in range(old_pages)])
        if strict_ok:
            meta_ok, meta_detail = _metadata_matches([(i, i) for i in range(old_pages)])
            if not meta_ok:
                return False, meta_detail
            return True, f"IDENTICAL ({old_w}x{old_h}, {old_pages} page(s))"
    else:
        strict_ok, strict_detail = False, f"PAGE_COUNT_MISMATCH {old_pages} vs {new_pages}"

    # One difference is EXPECTED and safe: -GenerateThumbnail adds a thumbnail page, or
    # replaces the one the source had with a freshly generated one at another size. Judged
    # strictly, every file in a folder compressed with thumbnails came back MISMATCH
    # ("PAGE_COUNT_MISMATCH 1 vs 2", "PIXEL_DIFF page 1"), so the backups could never be
    # purged and the user was told a perfectly good archive did not match.
    #
    # Compare the IMAGE pages instead -- the thumbnail is derived data, and page 0 (the
    # image the backup exists for) still has to be pixel-identical. Anything else keeps the
    # strict verdict, and an unreadable page layout keeps it too: in doubt, do not delete.
    old_geo = _tiff_page_geometry(old_path)
    new_geo = _tiff_page_geometry(new_path)
    if (old_geo is None or new_geo is None
            or len(old_geo) != old_pages or len(new_geo) != new_pages):
        return False, strict_detail

    old_img = _image_page_indices(old_geo)
    new_img = _image_page_indices(new_geo)
    if not old_img or len(old_img) != len(new_img):
        return False, strict_detail
    if len(old_img) == old_pages and len(new_img) == new_pages:
        # No thumbnail on either side -- nothing to excuse, the strict verdict stands.
        return False, strict_detail

    img_ok, _img_detail = _pages_identical(list(zip(old_img, new_img)))
    if not img_ok:
        return False, strict_detail

    meta_ok, meta_detail = _metadata_matches(list(zip(old_img, new_img)))
    if not meta_ok:
        return False, meta_detail

    if new_pages > old_pages:
        note = "thumbnail added"
    elif new_pages < old_pages:
        note = "thumbnail removed"
    else:
        note = "thumbnail regenerated"
    return True, f"IDENTICAL ({old_w}x{old_h}, {len(old_img)} image page(s), {note})"


@_scoped_magick_aliases
def _is_real_16bit(tiff_path: Path, temp_dir: Path = None, compress_tmp: str = "none") -> tuple[bool, float, str]:
    """
    Check if a TIFF is real 16-bit or padded 8-bit (stretched to 16).
    Method: Convert to 8-bit and back to 16-bit. If RMSE=0, it's padded 8-bit.
    Uses temp_dir for temporary files if provided.
    """
    import subprocess

    work_dir = temp_dir if temp_dir else Path(tempfile.gettempdir())

    def get_depth(path):
        try:
            result = _run_captured(
                ["magick", "identify", "-ping", "-format", "%z", f"{_magick_spec(path)}[0]"],
                _work_timeout(30, path), text=True
            )
            if result.returncode == 0:
                first = result.stdout.strip().split()[0]
                return int(first), ""
            err = result.stderr.strip() if result.stderr else ""
            return None, err
        except FileNotFoundError:
            return None, "ImageMagick not found (magick command missing)"
        except Exception as e:
            return None, str(e)

    depth, err_msg = get_depth(tiff_path)
    if depth is None:
        return False, 0.0, f"magick identify failed: {err_msg}"
    if depth != 16:
        return False, 0.0, f"{depth}-bit (not 16-bit)"

    tmp8 = None
    tmp16 = None
    try:
        unique_id = uuid.uuid4().hex[:12]
        tmp8 = work_dir / f"diag_8bit_{unique_id}.tif"
        tmp16 = work_dir / f"diag_16bit_{unique_id}.tif"

        compress_8 = f"-compress {compress_tmp}" if compress_tmp != "none" else ""
        compress_16 = f"-compress {compress_tmp}" if compress_tmp == "zip" else ""

        t0 = time.time() if DEBUG_TIMING else None
        try:
            # [0] on every step: multi-page TIFFs (Capture One main+thumbnail, scanner
            # RGB+IR) made the whole-file compare return garbage RMSE (~27%) even for
            # pixel-identical data, so padded files were misclassified as real 16-bit.
            result = _run_captured(
                ["magick", f"{_magick_spec(tiff_path)}[0]", "-depth", "8"] + (compress_8.split() if compress_8 else []) + [_magick_spec(tmp8)],
                _work_timeout(CONVERT_TIMEOUT_S, tiff_path)
            )
        except FileNotFoundError:
            return False, 0.0, "ERROR: ImageMagick not found"
        t1 = time.time() if DEBUG_TIMING else None
        if result.returncode != 0:
            return False, 0.0, "ERROR: 8-bit conversion failed"

        try:
            result = _run_captured(
                ["magick", f"{_magick_spec(tmp8)}[0]", "-depth", "16"] + (compress_16.split() if compress_16 else []) + [_magick_spec(tmp16)],
                _work_timeout(CONVERT_TIMEOUT_S, tiff_path)
            )
        except FileNotFoundError:
            return False, 0.0, "ERROR: ImageMagick not found"
        t2 = time.time() if DEBUG_TIMING else None
        if result.returncode != 0:
            return False, 0.0, "ERROR: 16-bit back conversion failed"

        if not tmp16.exists():
            return False, 0.0, "ERROR: round-trip file missing"

        try:
            result = _run_captured(
                ["magick", "compare", "-metric", "RMSE", f"{_magick_spec(tiff_path)}[0]", f"{_magick_spec(tmp16)}[0]", "null:"],
                _work_timeout(COMPARE_TIMEOUT_S, tiff_path), text=True
            )
        except FileNotFoundError:
            return False, 0.0, "ERROR: ImageMagick not found (compare failed)"
        t3 = time.time() if DEBUG_TIMING else None

        if DEBUG_TIMING and t3 is not None:
            import os
            sz8 = os.path.getsize(tmp8) if tmp8.exists() else 0
            sz16 = os.path.getsize(tmp16) if tmp16.exists() else 0
            print(f"[DEBUG] {tiff_path.name} | 8bit:{(t1-t0):.2f}s({sz8//1024}KB) 16bit:{(t2-t1):.2f}s({sz16//1024}KB) compare:{(t3-t2):.2f}s")

        output = result.stdout.strip() if result.stdout else result.stderr.strip() if result.stderr else ""

        if result.returncode not in (0, 1):
            return False, 0.0, f"ERROR: compare failed (exit={result.returncode}): {output}"

        import re
        output = _strip_magick_noise(output).strip()
        match = re.search(r"([\d.]+(?:[eE][+-]?\d+)?)\s*\(([\d.eE+-]+)\)", output)
        if not match:
            return False, 0.0, f"ERROR: RMSE parse failed: '{output}'"

        rmse = float(match.group(1))

        if rmse == 0.0:
            return False, 0.0, "padded 8-bit (round-trip RMSE=0)"
        else:
            return True, rmse, f"real 16-bit (RMSE={rmse})"

    except subprocess.TimeoutExpired:
        return False, 0.0, "ERROR: timeout during comparison"
    except Exception as e:
        return False, 0.0, f"ERROR: {e}"
    finally:
        for f in (tmp8, tmp16):
            if f and f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass


def _split_semicolons(value: Optional[str]) -> List[str]:
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def _scope_parts(scope: Optional[str]) -> List[str]:
    """`_EXPORT/TIFF16` (either slash) -> ["_export", "tiff16"]."""
    return [p.lower() for p in re.split(r"[\\/]+", (scope or "").strip()) if p]


def _diagnose_targets(roots, scope: str = "", skip_names: str = "") -> List[Path]:
    """The TIFFs Diagnose checks under `roots`, sorted.

    Folders holding this toolkit's own output are skipped below each root (see
    _is_tool_output_path). `scope` narrows the search to files whose folder path
    contains those folder names one after the other, case-insensitive:
    `_EXPORT/TIFF16` keeps `<root>/2025/Shoot/_EXPORT/TIFF16/a.tif` and everything
    below it -- and everything under a root that already is such a folder. A name in
    the scope is exempt from the tool-output skip: the export marker is one of those
    names, and a scope that names it must be able to enter it.
    `skip_names` is a ';'-separated list of file-name patterns (`*NXSTUDIO*`),
    case-insensitive.
    """
    want = _scope_parts(scope)
    skip_dirs = _TOOL_OUTPUT_PARTS - set(want)
    patterns = [p.lower() for p in _split_semicolons(skip_names)]
    import fnmatch
    found = []
    roots = list(roots)
    # A photo library on a hard disk takes minutes to walk: report every few seconds.
    _say(f"Searching for TIFFs under {'; '.join(str(r) for r in roots)} ...", "dim")
    t0 = last = time.time()
    folders = 0
    for root in roots:
        root = Path(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]
            folders += 1
            now = time.time()
            if now - last >= 5:
                last = now
                _say(f"  {folders} folder(s) scanned, {len(found)} TIFF(s) so far "
                     f"({now - t0:.0f}s) -- {Path(dirpath).name}", "dim")
            parts = [p.lower() for p in Path(dirpath).parts]
            if want and not any(parts[i:i + len(want)] == want
                                for i in range(len(parts) - len(want) + 1)):
                continue
            for name in filenames:
                low = name.lower()
                if not low.endswith((".tif", ".tiff")):
                    continue
                if any(fnmatch.fnmatchcase(low, p) for p in patterns):
                    continue
                found.append(Path(dirpath) / name)
    _say(f"  Found {len(found)} TIFF(s) in {folders} folder(s) ({time.time() - t0:.0f}s)", "dim")
    return sorted(found)


def _diagnose_status(is_real: bool, detail: str) -> str:
    """"padded", "real", "other" (not 16-bit) or "error"."""
    if not is_real and "padded" in detail.lower():
        return "padded"
    if is_real:
        return "real"
    if "not 16-bit" in detail:
        return "other"
    return "error"


def _fmt_duration(seconds: float) -> str:
    """`45s`, `12m`, `2h05m`."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _display_path(path: Path, roots=None) -> str:
    """A file as the progress line shows it: from the scan root's own folder down
    (`2025/Shoot [FINAL]/_EXPORT/TIFF16/a.tif`), so same-named files in different
    sessions can be told apart. A long path keeps its first two and last three parts."""
    for r in roots or ():
        try:
            Path(path).relative_to(r)
            rel = Path(path).relative_to(Path(r).parent)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) > 5 and len(str(rel)) > 110:
            parts = parts[:2] + ("...",) + parts[-3:]
        return os.sep.join(parts)
    return str(path)


def _diagnose_pool(tiff_files: List[Path], workers: int, temp_dir: Optional[Path],
                   compress_tmp: str, on_result=None, roots=None,
                   budget: Optional["_MemoryBudget"] = None,
                   pixels: Optional[Dict[str, int]] = None) -> List[tuple]:
    """Run _is_real_16bit over `tiff_files` with `workers` threads, printing one line
    per file (its path below `roots`, see _display_path). Returns
    [(path, is_real, rmse, detail)] in completion order. `on_result` is called with
    each result as it arrives (the Diagnose cache saves from it). On Ctrl+C the files
    not started yet are cancelled instead of all being run first. With `budget`, each
    check first reserves its own estimate (`pixels` x _COMPARE_BYTES_PER_PIXEL)."""
    global _TIMEOUT_LOAD

    def process_one(tiff_path):
        if budget is None:
            return tiff_path, _is_real_16bit(tiff_path, temp_dir, compress_tmp)
        key = os.path.normcase(os.path.abspath(tiff_path))
        need = (pixels or {}).get(key, _UNKNOWN_IMAGE_PIXELS) * _COMPARE_BYTES_PER_PIXEL
        held = budget.acquire(need)
        try:
            return tiff_path, _is_real_16bit(tiff_path, temp_dir, compress_tmp)
        finally:
            budget.release(held)

    results = []
    total = len(tiff_files)
    t_start = time.time()
    _TIMEOUT_LOAD = workers
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(process_one, f): f for f in tiff_files}
        for future in concurrent.futures.as_completed(futures):
            try:
                tiff_path, (is_real, stddev, detail) = future.result()
            except Exception as e:
                tiff_path = futures[future]
                is_real = False
                stddev = 0.0
                detail = f"ERROR: {e}"
            results.append((tiff_path, is_real, stddev, detail))

            status = _diagnose_status(is_real, detail)
            shown = {"padded": "PADDED", "real": "real 16-bit"}.get(status, detail)
            progress = f"[{len(results)}/{total}]"
            where = _display_path(tiff_path, roots)
            if RICH_AVAILABLE and console:
                color = {"padded": "yellow", "real": "dim"}.get(status, "red")
                console.print(f"  {progress} [{color}]{escape(shown)}[/{color}] {escape(where)}")
            else:
                print(f"  {progress} {shown} - {where}")
            if on_result is not None:
                on_result(results[-1])
            done = len(results)
            if done % 25 == 0 and done < total:
                spent = time.time() - t_start
                _say(f"  {done}/{total} checked in {_fmt_duration(spent)} -- about "
                     f"{_fmt_duration(spent / done * (total - done))} left", "cyan")
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    finally:
        _TIMEOUT_LOAD = 1
    return results


def _ask_line(prompt_text: str, default: str) -> str:
    """One free-text answer; '-' (the shown default when empty) means none."""
    if RICH_AVAILABLE and console:
        value = Prompt.ask(f"[cyan]{prompt_text}[/cyan]", default=default or "-").strip()
    else:
        value = input(f"{prompt_text} [{default or '-'}]: ").strip() or (default or "-")
    return "" if value in ("-", "none") else value


def run_diagnose_tiffs(cfg: ToolConfig) -> bool:
    """
    Workflow 7: Diagnose 16-bit TIFFs.
    Check if TIFFs marked as 16-bit are real 16-bit data or padded 8-bit.
    Uses parallel processing with configurable temp directory.
    """
    folder = step_folder(cfg, "Root folder to scan for TIFFs")
    if folder is None:
        return False

    scope = _ask_line("Only inside this folder path (e.g. _EXPORT/TIFF16, '-' = everywhere)", "")
    skip_names = _ask_line("Skip file names matching (';'-separated, e.g. *NXSTUDIO*, '-' = none)", "")

    # Relative to the folder the user chose (see _is_tool_output_path): testing the absolute
    # path meant an ancestor named zip/logs/_export hid every file under it, and pointing
    # the workflow AT such a folder found nothing at all.
    tiff_files = _diagnose_targets([folder], scope, skip_names)

    if not tiff_files:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No TIFF files found.[/yellow]")
        else:
            print("No TIFF files found.")
        return True

    workers = cfg.config.last_workers or cfg.config.default_workers
    if RICH_AVAILABLE and console:
        workers_input = Prompt.ask(f"\n[cyan]Workers[/cyan]", default=str(workers))
        if workers_input:
            try:
                workers = int(workers_input)
            except ValueError:
                console.print(f"[yellow]Invalid workers value '{workers_input}', using default {workers}[/yellow]")
    else:
        resp = input(f"\nWorkers [{workers}]: ").strip()
        if resp:
            try:
                workers = int(resp)
            except ValueError:
                print(f"Invalid workers value '{resp}', using default {workers}")

    workers = clamp_workers(workers, cfg.config.default_workers)
    # Remember the choice like the other workflows do
    cfg.config.last_workers = workers
    cfg.save_config()

    temp_dir = None
    if RICH_AVAILABLE and console:
        if Confirm.ask("\n[cyan]Use a specific temp directory?[/cyan]", default=False):
            temp_dir_input = input("Temp directory (leave empty for system temp): ").strip()
            if temp_dir_input:
                temp_dir = Path(temp_dir_input)
                # audit round 16: an unwritable/invalid temp dir raised out of the wizard.
                try:
                    temp_dir.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    console.print(f"[red]Cannot use temp dir {escape(str(temp_dir))}: {escape(str(e))} -- using system temp.[/red]")
                    temp_dir = None
            if temp_dir is not None:
                console.print(f"[dim]Using temp dir: {temp_dir}[/dim]")
    else:
        resp = input("\nUse a specific temp directory? (leave empty for system temp): ").strip()
        if resp:
            temp_dir = Path(resp)
            # audit round 16: an unwritable/invalid temp dir raised out of the wizard.
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"ERROR: cannot use temp dir {temp_dir}: {e} -- using system temp.")
                temp_dir = None
        if temp_dir is not None:
            print(f"Using temp dir: {temp_dir}")

    compress_tmp = "none"
    if RICH_AVAILABLE and console:
        console.print("\n[cyan]Compress temp TIFFs during comparison?[/cyan]")
        console.print("  [1] No      (uncompressed, fast)")
        console.print("  [2] LZW     (8-bit LZW, balanced)")
        console.print("  [3] ZIP     (8+16 bit Deflate, slower)")
        compress_choice = Prompt.ask("Choice", choices=["1", "2", "3"], default="1")
        compress_map = {"1": "none", "2": "lzw", "3": "zip"}
        compress_tmp = compress_map.get(compress_choice, "none")
    else:
        print("\nCompress temp TIFFs during comparison?")
        print("  1: No (uncompressed, fast)")
        print("  2: LZW (8-bit LZW, balanced)")
        print("  3: ZIP (8+16 bit Deflate, slower)")
        resp = input("[1]: ").strip() or "1"
        compress_map = {"1": "none", "2": "lzw", "3": "zip"}
        compress_tmp = compress_map.get(resp, "none")

    convert_q = ("Repeat/preset: also convert the padded files it finds? (pixel-verified, "
                 "originals kept in OLD_PADDED)")
    if RICH_AVAILABLE and console:
        convert_padded = Confirm.ask(f"\n[cyan]{escape(convert_q)}[/cyan]", default=False)
    else:
        convert_padded = input(f"\n{convert_q} [y/N]: ").strip().lower() == "y"

    settings = {"roots": [str(folder.resolve())], "scope": scope, "skip_names": skip_names,
                "workers": workers, "temp_dir": str(temp_dir) if temp_dir else None,
                "compress_tmp": compress_tmp, "convert_padded": convert_padded}
    _save_last_run(cfg, "diagnose", _diagnose_label(settings), None, diagnose=settings)

    # Same cache as the Diagnose preset: a file already checked and unchanged since is not
    # checked again, so converting what a preset found does not re-read the whole library.
    # The conversion re-verifies every file's pixels itself (_padded_pixels_status).
    cache = _load_diagnose_cache() if DIAGNOSE_CACHE else {}
    results, todo, keys = _split_cached(tiff_files, cache)
    if results:
        _say(f"{len(results)} TIFF(s) unchanged since they were last checked -- verdict reused", "dim")

    total_start = time.time() if DEBUG_TIMING else None
    if todo:
        budget, pixels = _memory_budget(todo, _COMPARE_BYTES_PER_PIXEL, "16-bit check")
        if RICH_AVAILABLE and console:
            console.print(f"\n[cyan]Diagnosing {len(todo)} TIFF file(s) with {workers} workers...[/cyan]")
        else:
            print(f"\nDiagnosing {len(todo)} TIFF file(s) with {workers} workers...")
        if DIAGNOSE_CACHE:
            results += _check_with_cache(todo, keys, cache, workers, temp_dir, compress_tmp,
                                         roots=[folder], budget=budget, pixels=pixels)
        else:
            results += _diagnose_pool(todo, workers, temp_dir, compress_tmp, roots=[folder],
                                      budget=budget, pixels=pixels)
    statuses = [_diagnose_status(r[1], r[3]) for r in results]
    padded_files = [r[0] for r, s in zip(results, statuses) if s == "padded"]
    padded_count = len(padded_files)
    real_count = statuses.count("real")
    other_count = statuses.count("other")
    error_count = statuses.count("error")

    if RICH_AVAILABLE and console:
        console.print(f"\n[bold]Results:[/bold]")
        summary_table = Table(box=BOX_SIMPLE)
        summary_table.add_column("Type", style="cyan")
        summary_table.add_column("Count", style="green")
        summary_table.add_row("Real 16-bit", str(real_count))
        if padded_count > 0:
            summary_table.add_row("[yellow]Padded 8-bit (stretched)[/yellow]", str(padded_count))
        else:
            summary_table.add_row("Padded 8-bit", "0")
        if other_count > 0:
            summary_table.add_row("[magenta]Other bit depth[/magenta]", str(other_count))
        if error_count > 0:
            summary_table.add_row("[red]Errors/Failed[/red]", str(error_count))
        console.print(summary_table)

        if DEBUG_TIMING and total_start is not None:
            elapsed = time.time() - total_start
            console.print(f"\n[dim][DEBUG] Total time: {elapsed:.2f}s[/dim]")

        if padded_count > 0:
            console.print(f"\n[yellow]Warning: {padded_count} file(s) are padded 8-bit (converted from 8-bit to 16-bit without adding real data).[/yellow]")

            if Confirm.ask("\n[cyan]Compress padded files to 8-bit ZIP?[/cyan]", default=False):
                console.print(f"[cyan]Compressing {len(padded_files)} file(s)...[/cyan]")
                _compress_padded_files(padded_files, temp_dir, workers, cfg)
    else:
        print(f"\nResults:")
        print(f"  Real 16-bit: {real_count}")
        print(f"  Padded 8-bit: {padded_count}")
        if other_count > 0:
            print(f"  Other bit depth: {other_count}")
        if error_count > 0:
            print(f"  Errors/Failed: {error_count}")
        if DEBUG_TIMING and total_start is not None:
            elapsed = time.time() - total_start
            print(f"\n[DEBUG] Total time: {elapsed:.2f}s")
        if padded_count > 0:
            print(f"\nWarning: {padded_count} file(s) are padded 8-bit (converted from 8-bit to 16-bit without adding real data).")
            resp = input("\nCompress padded files to 8-bit ZIP? [y/N]: ").strip().lower()
            if resp == "y":
                print(f"Compressing {len(padded_files)} file(s)...")
                _compress_padded_files(padded_files, temp_dir, workers, cfg)

    return True


def _diagnose_label(settings: Dict) -> str:
    roots = settings.get("roots") or []
    where = truncate_path(Path(roots[0]), 50) if len(roots) == 1 else f"{len(roots)} folders"
    label = f"Diagnose TIFFs -- {settings.get('scope') or 'all folders'}"
    if settings.get("skip_names"):
        label += f", skip {settings['skip_names']}"
    return f"{label} -- {where} " + ("[converts padded]" if settings.get("convert_padded")
                                      else "[report only]")


def _diagnose_cache_path() -> Path:
    return _config_base() / ".convert_tiff_diagnose_cache.json"


def _file_key(path: Path) -> Optional[Tuple[str, int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns


def _load_diagnose_cache() -> Dict:
    try:
        with open(_diagnose_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_diagnose_cache(cache: Dict) -> None:
    path = _diagnose_cache_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError as e:
        _say(f"WARNING: could not save the Diagnose cache {path}: {e}", "yellow")


_CACHE_SAVE_EVERY = 25  # results between two cache writes during a Diagnose run


def _split_cached(files: List[Path], cache: Dict) -> Tuple[List[tuple], List[Path], Dict]:
    """(results already known, files to check, {file: key}). A cached verdict counts only
    while the file keeps the size and modification time it had when checked."""
    cached, todo, keys = [], [], {}
    for f in files:
        key = _file_key(f)
        keys[f] = key
        hit = cache.get(key[0]) if key else None
        if (isinstance(hit, dict) and hit.get("size") == key[1]
                and hit.get("mtime_ns") == key[2]
                and hit.get("status") in ("real", "padded", "other")):
            cached.append((f, hit["status"] == "real", float(hit.get("rmse") or 0.0),
                           str(hit.get("detail") or "")))
        else:
            todo.append(f)
    return cached, todo, keys


def _check_with_cache(todo: List[Path], keys: Dict, cache: Dict, workers: int,
                      temp_dir: Optional[Path], compress_tmp: str, roots=None,
                      budget=None, pixels=None) -> List[tuple]:
    """_diagnose_pool over `todo`, recording each verdict in `cache` and writing the
    cache every _CACHE_SAVE_EVERY results and once more at the end -- also when the
    run is interrupted -- so a stopped run keeps what it already checked."""
    pending = [0]

    def record(result):
        f, is_real, rmse, detail = result
        status = _diagnose_status(is_real, detail)
        key = keys.get(f)
        if key and status != "error":
            cache[key[0]] = {"size": key[1], "mtime_ns": key[2], "status": status,
                             "rmse": rmse, "detail": detail}
            pending[0] += 1
            if pending[0] >= _CACHE_SAVE_EVERY:
                _save_diagnose_cache(cache)
                pending[0] = 0

    try:
        return _diagnose_pool(todo, workers, temp_dir, compress_tmp, on_result=record,
                              roots=roots, budget=budget, pixels=pixels)
    finally:
        if pending[0]:
            _save_diagnose_cache(cache)


def _run_diagnose_journal(cfg, entry: Dict, title: str, unattended: bool = False,
                          dry_run: bool = False) -> int:
    """A Diagnose repeat/preset: re-scan the saved roots and REPORT the padded files.

    Converts them only when the preset was saved with `convert_padded` (asked in
    workflow 7, default no): the same _compress_padded_files as the menu, which
    verifies every file's pixels and keeps the original in OLD_PADDED/. Files whose verdict is cached (DIAGNOSE_CACHE)
    and whose size and modification time did not change are not checked again. The
    report goes to Logs/convert_tiff/diagnose_<timestamp>.log under the current
    folder. 0 = ran, 1 = a root is
    missing, the run was cancelled, or a file could not be checked."""
    d = entry["diagnose"]
    roots = [Path(r) for r in d.get("roots") or []]
    gone = [r for r in roots if not r.is_dir()]
    if gone:
        _say(f"Diagnose root is gone: {'; '.join(str(r) for r in gone)}", "red")
        return 1

    files = _diagnose_targets(roots, d.get("scope") or "", d.get("skip_names") or "")
    cache = _load_diagnose_cache() if DIAGNOSE_CACHE else {}
    cached, todo, keys = _split_cached(files, cache)

    _say(f"\n{title}", "bold cyan")
    _say(f"  {entry.get('label') or ''}")
    _say(f"  saved {entry.get('time')}", "dim")
    _say(f"  {len(files)} TIFF(s): {len(todo)} to check, {len(cached)} unchanged since "
         f"they were last checked")
    convert = bool(d.get("convert_padded"))
    if convert:
        _say("Padded files found are converted to 8-bit ZIP after the check -- each one "
             "pixel-verified, the original kept in OLD_PADDED\\.", "yellow")
    else:
        _say("Report only: padded files are listed, never converted (convert them from "
             "workflow 7 in the menu).", "yellow")
    if dry_run:
        _say("--dry-run: nothing checked.", "yellow")
        return 0
    if not unattended:
        if RICH_AVAILABLE and console:
            go = Confirm.ask("Run it now?", default=False)
        else:
            go = input("Run it now? [y/N]: ").strip().lower() == "y"
        if not go:
            _say("Cancelled.", "dim")
            return 1

    results = list(cached)
    if todo:
        temp_dir = Path(d["temp_dir"]) if d.get("temp_dir") else None
        if temp_dir is not None:
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                _say(f"Cannot use temp dir {temp_dir}: {e} -- using system temp.", "yellow")
                temp_dir = None
        workers = clamp_workers(d.get("workers") or cfg.config.default_workers,
                                cfg.config.default_workers)
        budget, pixels = _memory_budget(todo, _COMPARE_BYTES_PER_PIXEL, "16-bit check")
        _say(f"\nDiagnosing {len(todo)} TIFF file(s) with {workers} workers...", "cyan")
        compress_tmp = d.get("compress_tmp") or "none"
        if DIAGNOSE_CACHE:
            results += _check_with_cache(todo, keys, cache, workers, temp_dir, compress_tmp,
                                         roots=roots, budget=budget, pixels=pixels)
        else:
            results += _diagnose_pool(todo, workers, temp_dir, compress_tmp, roots=roots,
                                      budget=budget, pixels=pixels)

    by_status = {"padded": [], "real": [], "other": [], "error": []}
    for f, is_real, rmse, detail in sorted(results, key=lambda r: str(r[0])):
        by_status[_diagnose_status(is_real, detail)].append((f, detail))

    lines = [f"{title} -- {time.strftime('%Y-%m-%d %H:%M:%S')}",
             entry.get("label") or "",
             f"Roots: {'; '.join(str(r) for r in roots)}",
             f"Scope: {d.get('scope') or '(all folders)'} | Skip names: {d.get('skip_names') or '(none)'}",
             f"Checked now: {len(todo)} | unchanged (cached): {len(cached)}",
             "",
             f"Real 16-bit: {len(by_status['real'])}",
             f"Padded 8-bit: {len(by_status['padded'])}",
             f"Other bit depth: {len(by_status['other'])}",
             f"Errors: {len(by_status['error'])}"]
    if convert and by_status["padded"]:
        padded = [f for f, _ in by_status["padded"]]
        _say(f"\nConverting {len(padded)} padded file(s) to 8-bit ZIP...", "cyan")
        temp_dir = Path(d["temp_dir"]) if d.get("temp_dir") else None
        done = _compress_padded_files(padded, temp_dir, clamp_workers(
            d.get("workers") or cfg.config.default_workers, cfg.config.default_workers), cfg)
        n = (done or {}).get("converted", 0) if isinstance(done, dict) else 0
        lines.insert(lines.index(f"Errors: {len(by_status['error'])}") + 1,
                     f"Converted to 8-bit now: {n} of {len(padded)} (originals in OLD_PADDED\\; "
                     f"the rest were kept -- see the lines above)")
    if by_status["padded"]:
        lines += ["", "Padded 8-bit (16-bit files holding 8-bit data):"]
        lines += [f"  {f}" for f, _ in by_status["padded"]]
    if by_status["error"]:
        lines += ["", "Could not be checked:"]
        lines += [f"  {f} -- {detail}" for f, detail in by_status["error"]]

    print()
    for line in lines:
        print(line)
    try:
        # Logs\ of the current folder, like the backends' own logs.
        log_dir = Path.cwd() / "Logs" / "convert_tiff"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"diagnose_{time.strftime('%Y%m%d_%H%M%S')}.log"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\nReport: {log_path}")
    except OSError as e:
        _say(f"WARNING: could not write the report: {e}", "yellow")
    return 1 if by_status["error"] else 0


def _tiff_extra_pages_are_thumbnails(tiff_path) -> Optional[bool]:
    """Python mirror of Test-TiffHasOnlySubfilePages (compress_tiff_zip.ps1).

    True when every page beyond IFD0 is a thumbnail: tagged REDUCEDIMAGE/REDUCED AND
    strictly smaller than the main image. False when any extra page is a real page
    (scanner IR MASK, Photoshop layer, second photo). None when it cannot be determined.

    audit round 16: an UNTAGGED page that is merely strictly smaller than page 0 used to
    be excused here, and the padded-compress path then lossily down-converted it. The
    purge-path classifier (_image_page_indices) has always required the tag -- being
    wrong there deletes the last backup. The two classifiers now use the same rule, and
    an untagged extra page refuses the lossy path (fail closed). Every thumbnail this
    toolkit writes is tagged (Set-TiffThumbMarkers), so the strict rule still covers the
    case it exists for.

    MASK and PAGE are deliberately NOT accepted, exactly like the four PowerShell call
    sites: a full-size IR channel is a real page, not a preview.
    """
    try:
        r = _run_captured(
            ["magick", "identify", "-ping", "-format", "%[tiff:subfiletype]|%w|%h\n", str(tiff_path)],
            _work_timeout(30, tiff_path), text=True,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        lines = [l for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return None

    if len(lines) <= 1:
        return True

    def _wh(parts):
        try:
            return int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            return 0, 0

    main_w, main_h = _wh(lines[0].strip().split("|"))
    for line in lines[1:]:
        parts = line.strip().split("|")
        w, h = _wh(parts)
        # Same rule as _image_page_indices (audit round 16): tag AND strictly smaller.
        is_thumb = (parts[0].strip().upper() in ("REDUCEDIMAGE", "REDUCED")
                    and main_w > 0 and main_h > 0 and 0 < w < main_w and 0 < h < main_h)
        if not is_thumb:
            return False
    return True


def _tiff_page_count(path) -> Optional[int]:
    """Number of IFDs in a TIFF, or None when it cannot be read (never a silent 0).

    `%n` prints once per frame, so "%n\\n" plus the first line is the only reliable form."""
    try:
        r = _run_captured(
            ["magick", "identify", "-ping", "-format", "%n\n", str(path)],
            _work_timeout(30, path), text=True,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        return int(r.stdout.splitlines()[0].strip())
    except Exception:
        return None


def _restore_subfile_types(src_path, dst_path) -> bool:
    """Re-apply the source per-page SubfileType markers to a magick-written TIFF.

    Python mirror of Restore-TiffSubfileTypes: `-depth 8` rewrites every page and drops the
    tag, so a Capture One thumbnail came back stamped PAGE. This was the only rewrite path
    in the project without the restore.

    Markers are read with exiftool, NOT with `magick %[tiff:subfiletype]`. That property
    prints an EMPTY string both when the tag is ABSENT and when it is an explicit 0
    ("full-resolution image"), so a source carrying an explicit 0 -- every scanner TIFF does
    -- had the tag DELETED from the output instead of preserved; and the symbolic names only
    covered four values, so anything else silently became "no tag". exiftool prints one line
    per IFD that really has the tag, with its numeric value, which tells the cases apart.
    The PowerShell twin was fixed for exactly this; the mirror had been left behind.
    """
    try:
        r = _run_exiftool(
            ["-a", "-G1", "-s", "-s", "-n", "-SubfileType", str(src_path)],
            30, text=True,
        )
        if r.returncode != 0:
            return False
    except Exception:
        return False

    src_types = {}
    for line in (r.stdout or "").splitlines():
        m = re.match(r"^\[IFD(\d+)\]\s+SubfileType:\s*(\d+)\s*$", line.strip())
        if m:
            src_types[int(m.group(1))] = int(m.group(2))

    pages = _tiff_page_count(src_path)
    if pages is None:
        return False
    # Single-page sources need no restore: magick only stamps PAGE when it writes a
    # multi-page file, and an absent NewSubfileType means 0 per the TIFF spec anyway.
    if pages <= 1:
        return True

    tags = []
    for i in range(pages):
        if i in src_types:
            tags.append("-IFD%d:SubfileType#=%d" % (i, src_types[i]))
        else:
            # Source had no marker on this page: clear whatever magick stamped on it.
            tags.append("-IFD%d:SubfileType=" % i)
    if not tags:
        return True
    try:
        r = _run_exiftool(
            ["-q", "-q", "-overwrite_original"] + tags + [str(dst_path)],
            30,
        )
        return r.returncode == 0
    except Exception:
        return False


def _exiftool_copy_failed(stderr: str) -> bool:
    """True when exiftool's stderr says tags were NOT (fully) written.

    audit round 16: the padded-compress gate checked only `returncode == 0` plus
    "Error opening file", but exiftool exits 0 with WARNINGS on a partial copy
    ("Warning: Sorry, ... not written", "Warning: Can't ...") -- and this gate
    authorises replacing the original with a LOSSY 8-bit rewrite. Benign per-tag
    chatter (scanner files emit plenty) still passes: only warnings that say a tag
    could not be written fail the copy.
    """
    for line in (stderr or "").splitlines():
        low = line.strip().lower()
        if "error opening file" in low:
            return True
        if "warning:" in low and any(k in low for k in ("sorry", "not written", "can't")):
            return True
    return False


def _padded_history_arg() -> str:
    """The exiftool assignment that appends this conversion to xmpMM:History, in the
    format jxl-photo writes: Action, When, SoftwareAgent (tool, version, link),
    Parameters. Earlier entries (Capture One, ...) are kept. Values are kept free of
    the struct syntax characters (, { } [ ] |) instead of escaping them."""
    from datetime import datetime
    when = datetime.now().astimezone().isoformat(timespec="seconds")
    agent = f"{TOOLKIT_NAME} {TOOLKIT_VERSION} ({TOOLKIT_URL})"
    params = "convert_tiff: 16-bit padded -> 8-bit ZIP (pixel-identical)"
    return ("-XMP-xmpMM:History+={Action=converted,When=" + when
            + ",SoftwareAgent=" + agent + ",Parameters=" + params + "}")


def _append_padded_history(path: Path) -> bool:
    """Append the conversion's History entry to `path` (the staged 8-bit file). A
    SEPARATE exiftool call on purpose: in the same call as `-tagsfromfile -all:all`, a
    `+=` makes exiftool ADD the copied History to the one magick carried over, doubling
    every earlier entry (jxl-photo #531). False when it could not be written."""
    try:
        r = _run_exiftool(["-q", "-overwrite_original", _padded_history_arg(),
                           str(path)], _work_timeout(30, path), text=True)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and not _exiftool_copy_failed(r.stderr)


@_scoped_magick_aliases
def _padded_pixels_status(orig: Path, new8: Path) -> Optional[str]:
    """None when page [0] of the 8-bit rewrite is pixel-identical to the original's;
    "pixel_mismatch" when it differs (the original is real 16-bit); "pixel_compare_failed"
    when the comparison cannot say. Fail closed: only a parsed 0 with exit 0 passes."""
    try:
        r = _run_captured(
            ["magick", "compare", "-metric", "AE", f"{_magick_spec(orig)}[0]",
             f"{_magick_spec(new8)}[0]", "null:"],
            _work_timeout(COMPARE_TIMEOUT_S, orig), text=True
        )
    except Exception:
        return "pixel_compare_failed"
    out = _strip_magick_noise((r.stderr or "") + (r.stdout or "")).strip()
    m = re.match(r"([\d.]+(?:[eE][+-]?\d+)?)", out)
    if r.returncode == 1 or (m and r.returncode == 0 and float(m.group(1)) != 0.0):
        return "pixel_mismatch"
    if r.returncode != 0 or not m:
        return "pixel_compare_failed"
    return None


@_scoped_magick_aliases
def _process_single_padded(tiff_path, staging):
    """Process a single padded file. Returns (name, parent, status, size_orig, size_zip, ratio, exif_ok, error_msg, tmp8_path)."""
    name = tiff_path.name
    parent = tiff_path.parent
    unique_id = uuid.uuid4().hex[:8]
    # Bracket-free staging name (audit round 16): tmp8 feeds an ImageMagick OUTPUT spec
    # (_magick_spec below), and no '[' escape exists on Windows -- a bracket in `name`
    # would make magick write a different file than the one we verify and move.
    tmp8 = staging / f"tmp8_{unique_id}_{name.replace('[', '(').replace(']', ')')}"
    final_dst = parent / name
    status = None

    # _is_real_16bit judges page [0] only, but the conversion below rewrites the WHOLE file.
    # On a scanner RGB+IR scan whose RGB is padded, that would down-convert an IR channel
    # that may hold real 16-bit data and was never diagnosed. Refuse those files; a Capture
    # One main+thumbnail pair is still converted (the thumbnail is already 8-bit).
    # Fail-closed: an undeterminable page layout is refused too.
    extras_ok = _tiff_extra_pages_are_thumbnails(tiff_path)
    if extras_ok is not True:
        status = "multi_page_refused" if extras_ok is False else "page_layout_unreadable"
        return (name, parent, status, None, None, None, False, None, tmp8)

    try:
        try:
            result = _run_captured(
                ["magick", str(tiff_path), "-depth", "8", "-compress", "zip", _magick_spec(tmp8)],
                _work_timeout(CONVERT_TIMEOUT_S, tiff_path)
            )
        except FileNotFoundError:
            status = "magick_not_found"
            return (name, parent, status, None, None, None, False, None, tmp8)
        except Exception as e:
            status = "error"
            return (name, parent, status, None, None, None, False, str(e), tmp8)

        if result.returncode != 0:
            status = "magick_error"
            return (name, parent, status, None, None, None, False, None, tmp8)

        # A '%' in the name makes exiftool read a DIFFERENT file for -tagsfromfile and still
        # exit 0, so `exif_ok` was true while nothing had been copied -- and this path replaces
        # the original with a LOSSY 8-bit conversion. See _safe_tag_source.
        tag_source, tag_temp = _safe_tag_source(tiff_path)
        try:
            if tag_source is None:
                exif_ok = False
            else:
                exif_result = _run_exiftool(
                    ["-q", "-overwrite_original",
                     "-tagsfromfile", tag_source, "-all:all", "-unsafe",
                     "--Compression", "--BitsPerSample", "--SampleFormat",
                     str(tmp8)],
                    _work_timeout(30, tiff_path), text=True
                )
                # ONE -q above, not two: the second suppresses warnings, and an unreadable
                # tagsfromfile source is only ever reported as a warning (exit stays 0).
                # audit round 16: exit-0 partial copies are caught by _exiftool_copy_failed.
                exif_ok = (exif_result.returncode == 0
                           and not _exiftool_copy_failed(exif_result.stderr))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            exif_ok = False
        finally:
            if tag_temp:
                Path(tag_temp).unlink(missing_ok=True)

        # The only record that this file held 16-bit samples before. Not written means
        # the metadata is not what was planned: the file is treated like a failed EXIF
        # copy (original preserved).
        if exif_ok:
            exif_ok = _append_padded_history(tmp8)

        # magick drops the per-page SubfileType markers on rewrite, so a Capture One
        # thumbnail came back stamped PAGE and stopped being recognised as a thumbnail by
        # SafeMode and -SkipCompressedWithThumb on later runs. The three PowerShell
        # backends all restore them; this path was the only one that did not.
        # The restore runs AFTER the integrity gate below: an unreadable page count must
        # surface as page_count_mismatch from the gate, not as a restore failure.

        if not tmp8.exists():
            status = "missing"
            return (name, parent, status, None, None, None, False, None, tmp8)

        # Integrity check: ensure the compressed file decodes and dimensions match
        try:
            verify_result = _run_captured(
                ["magick", str(tmp8), "null:"],
                _work_timeout(30, tmp8)
            )
            if verify_result.returncode != 0:
                status = "integrity_error"
                return (name, parent, status, None, None, None, False, None, tmp8)

            # This gate authorises replacing the original with a LOSSY 16 -> 8-bit conversion,
            # so every branch fails CLOSED. Previously an unreadable identify (non-zero exit or
            # unparseable output) fell through to status = "ok" and the original was replaced
            # with nothing verified.
            def _read_dims(path) -> Optional[tuple]:
                r = _run_captured(
                    ["magick", "identify", "-ping", "-format", "%w %h\n", f"{_magick_spec(path)}[0]"],
                    _work_timeout(30, path), text=True
                )
                if r.returncode != 0 or not r.stdout:
                    return None
                parts = r.stdout.splitlines()[0].strip().split()
                if len(parts) < 2:
                    return None
                return (parts[0], parts[1])

            new_dims = _read_dims(tmp8)
            orig_dims = _read_dims(tiff_path)
            if new_dims is None or orig_dims is None:
                status = "dimension_unreadable"
                return (name, parent, status, None, None, None, False, None, tmp8)
            if new_dims != orig_dims:
                status = "dimension_mismatch"
                return (name, parent, status, None, None, None, False, None, tmp8)

            # Page-count parity: -depth 8 on a multi-page TIFF can drop pages, and the
            # dimension check above only looks at page [0].
            def _read_pages(path) -> Optional[int]:
                r = _run_captured(
                    ["magick", "identify", "-ping", "-format", "%n\n", str(path)],
                    _work_timeout(30, path), text=True
                )
                if r.returncode != 0 or not r.stdout:
                    return None
                try:
                    return int(r.stdout.splitlines()[0].strip())
                except ValueError:
                    return None

            new_pages = _read_pages(tmp8)
            orig_pages = _read_pages(tiff_path)
            if new_pages is None or orig_pages is None or new_pages != orig_pages:
                status = "page_count_mismatch"
                return (name, parent, status, None, None, None, False, None, tmp8)

            # The 8-bit rewrite must hold exactly the pixels of the original. Nothing above
            # looks at pixels: the verdict that called the file "padded" may be a cached one
            # (DIAGNOSE_CACHE), and a file that is really 16-bit must never be replaced by
            # its 8-bit rounding. -metric AE counts differing pixels; 0 with exit 0 passes,
            # anything else (different, timeout, unparseable) refuses.
            status = _padded_pixels_status(tiff_path, tmp8)
            if status is not None:
                return (name, parent, status, None, None, None, False, None, tmp8)
        except Exception as e:
            status = "integrity_error"
            return (name, parent, status, None, None, None, False, str(e), tmp8)

        # audit round 16: _restore_subfile_types' False return was IGNORED -- the file
        # was replaced as if the markers had been restored. A failed restore means the
        # rewrite's metadata cannot be trusted, so the file is degraded (WARN, original
        # preserved), not processed as OK. Runs after the integrity gate so an unreadable
        # page layout still surfaces as the gate's own status.
        if not _restore_subfile_types(tiff_path, tmp8):
            status = "subfile_restore_failed"
            return (name, parent, status, None, None, None, False, None, tmp8)

        size_orig = tiff_path.stat().st_size
        size_zip = tmp8.stat().st_size
        ratio = (1 - size_zip / size_orig) * 100 if size_orig > 0 else 0
        status = "ok"
        return (name, parent, status, size_orig, size_zip, ratio, exif_ok, None, tmp8)
    finally:
        if status and status != "ok" and tmp8.exists():
            try:
                tmp8.unlink()
            except Exception:
                pass


def _compress_padded_files(padded_files: list, temp_dir: Path, workers: int, cfg: ToolConfig):
    """
    Compress padded 8-bit TIFFs to 8-bit ZIP.
    Converts 16-bit to 8-bit then ZIP compresses. Preserves EXIF via exiftool.
    Originals are backed up to an OLD_PADDED/ subfolder before being replaced.
    Uses parallel processing with ThreadPoolExecutor. Returns {"converted": n} -- the
    files actually replaced (a non-empty dict, so `if _compress_padded_files(...)` holds).
    """
    global _TIMEOUT_LOAD
    converted = 0
    if temp_dir is None:
        temp_dir = Path(tempfile.gettempdir())
    # Run-scoped staging dir. A shared "compress_staging" meant the cleanup below wiped
    # every file in it, including another concurrent run's in-flight output -- if that run
    # was between the OLD_PADDED backup and the final move, the file vanished from the
    # source folder. Same reasoning as $runStagingId in compress_tiff_zip.ps1.
    staging = temp_dir / f"compress_staging_{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=True)

    # Same per-file memory budget as Diagnose: the workers asked for run, and a huge scan
    # waits for room instead of sizing every worker for itself.
    budget, pixels = _memory_budget(padded_files, _COMPARE_BYTES_PER_PIXEL, "8-bit conversion")

    def _process_one(tiff_path):
        if budget is None:
            return _process_single_padded(tiff_path, staging)
        need = pixels.get(os.path.normcase(os.path.abspath(tiff_path)),
                          _UNKNOWN_IMAGE_PIXELS) * _COMPARE_BYTES_PER_PIXEL
        held = budget.acquire(need)
        try:
            return _process_single_padded(tiff_path, staging)
        finally:
            budget.release(held)

    total = len(padded_files)
    t_start = time.time()

    # Each file is finished -- printed, backed up, moved into place -- AS SOON AS its
    # conversion is done. The pool used to convert every file first and only then print
    # and replace them all: an hour of blank screen on a large batch, and every staged
    # file held on the staging disk until the end.
    def _finalize(i, result):
        nonlocal converted
        name, parent, status, size_orig, size_zip, ratio, exif_ok, error_msg, tmp8 = result
        final_dst = parent / name

        where = f"{parent.name}{os.sep}{name}"
        if RICH_AVAILABLE and console:
            console.print(f"  [{i+1}/{len(padded_files)}] {escape(where)}")
        else:
            print(f"  [{i+1}/{len(padded_files)}] {where}")

        if status == "magick_not_found":
            if RICH_AVAILABLE and console:
                console.print(f"    [red]FAILED: magick not found[/red]")
            else:
                print(f"    FAILED: magick not found")
        elif status == "error":
            if RICH_AVAILABLE and console:
                console.print(f"    [red]FAILED: {escape(str(error_msg))}[/red]")
            else:
                print(f"    FAILED: {error_msg}")
        elif status == "magick_error":
            if RICH_AVAILABLE and console:
                console.print(f"    [red]FAILED: magick error[/red]")
            else:
                print(f"    FAILED: magick error")
        elif status == "missing":
            if RICH_AVAILABLE and console:
                console.print(f"    [red]FAILED: output missing[/red]")
            else:
                print(f"    FAILED: output missing")
        elif status in ("multi_page_refused", "page_layout_unreadable"):
            reason = ("multi-page TIFF with non-thumbnail pages (scanner IR / layers): the "
                      "diagnosis only looked at page 0, so converting the whole file could "
                      "down-convert pages that were never checked"
                      if status == "multi_page_refused"
                      else "page layout unreadable")
            if RICH_AVAILABLE and console:
                console.print(f"    [yellow]SKIPPED: {escape(reason)} -- original preserved[/yellow]")
            else:
                print(f"    SKIPPED: {reason} -- original preserved")
        elif status == "subfile_restore_failed":
            # audit round 16: _restore_subfile_types said the markers could not be
            # restored, so the rewrite's metadata is not trustworthy -- degraded, not OK.
            msg = "WARN: could not restore per-page SubfileType markers -- original preserved"
            if RICH_AVAILABLE and console:
                console.print(f"    [yellow]{escape(msg)}[/yellow]")
            else:
                print(f"    {msg}")
        elif status == "pixel_mismatch":
            msg = ("SKIPPED: the 8-bit version is not pixel-identical -- this file holds "
                   "real 16-bit data; original preserved")
            if RICH_AVAILABLE and console:
                console.print(f"    [yellow]{escape(msg)}[/yellow]")
            else:
                print(f"    {msg}")
        elif status in ("integrity_error", "dimension_mismatch",
                        "dimension_unreadable", "page_count_mismatch",
                        "pixel_compare_failed"):
            if RICH_AVAILABLE and console:
                console.print(f"    [red]FAILED: {status}[/red]")
            else:
                print(f"    FAILED: {status}")
        elif status == "ok":
            if not exif_ok:
                # Do NOT overwrite original if EXIF was lost
                if RICH_AVAILABLE and console:
                    console.print(f"    [red]FAILED: EXIF copy or History entry failed — original preserved[/red]")
                else:
                    print(f"    FAILED: EXIF copy or History entry failed — original preserved")
                tmp8.unlink(missing_ok=True)
                return

            if RICH_AVAILABLE and console:
                console.print(f"    [green]OK[/green] {_format_size(size_orig)} -> {_format_size(size_zip)} ({ratio:.1f}% smaller, EXIF preserved)")
            else:
                print(f"    OK {_format_size(size_orig)} -> {_format_size(size_zip)} ({ratio:.1f}% smaller, EXIF preserved)")

            if size_zip < size_orig:
                # Back up the original to OLD_PADDED/ before replacing (lossy 16 -> 8-bit)
                backup_dir = parent / "OLD_PADDED"
                try:
                    backup_dir.mkdir(exist_ok=True)
                    backup_path = backup_dir / name
                    if backup_path.exists():
                        v = 2
                        while (backup_dir / f"{final_dst.stem}_v{v}{final_dst.suffix}").exists():
                            v += 1
                        backup_path = backup_dir / f"{final_dst.stem}_v{v}{final_dst.suffix}"
                    _safe_move(final_dst, backup_path)
                except Exception as e:
                    if RICH_AVAILABLE and console:
                        console.print(f"    [red]FAILED: backup to OLD_PADDED failed ({escape(str(e))}) — original preserved[/red]")
                    else:
                        print(f"    FAILED: backup to OLD_PADDED failed ({e}) — original preserved")
                    tmp8.unlink(missing_ok=True)
                    return
                # Bare, this move took the whole workflow down with a traceback: the files
                # after it were never processed and no summary was printed, while the original
                # sat in OLD_PADDED/ looking like it had been replaced. Put the backup back and
                # carry on with the next file -- run_undo_old_tiffs already works this way.
                try:
                    _safe_move(tmp8, final_dst)
                except Exception as e:
                    try:
                        _safe_move(backup_path, final_dst)
                        restored = " -- original restored"
                    except Exception:
                        restored = f" -- ORIGINAL LEFT IN OLD_PADDED/{backup_path.name}"
                    if RICH_AVAILABLE and console:
                        console.print(f"    [red]FAILED: could not move the converted file into place ({escape(str(e))}){escape(restored)}[/red]")
                    else:
                        print(f"    FAILED: could not move the converted file into place ({e}){restored}")
                    tmp8.unlink(missing_ok=True)
                    return
                converted += 1
                if RICH_AVAILABLE and console:
                    console.print(f"    [dim]Original backed up: OLD_PADDED/{escape(backup_path.name)}[/dim]")
                else:
                    print(f"    Original backed up: OLD_PADDED/{backup_path.name}")
            else:
                if RICH_AVAILABLE and console:
                    console.print(f"    [dim]SKIPPED (ZIP larger than original)[/dim]")
                else:
                    print(f"    SKIPPED (ZIP larger than original)")
                tmp8.unlink(missing_ok=True)


    _TIMEOUT_LOAD = workers
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(_process_one, f): i for i, f in enumerate(padded_files)}
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                result = future.result()
            except Exception as e:
                f = padded_files[futures[future]]
                # audit round 16: the last slot is the staging path the result loop may
                # unlink; putting the SOURCE path there risked deleting the original.
                result = (f.name, f.parent, "error", None, None, None, False, str(e), None)
            _finalize(done - 1, result)
            if done % 25 == 0 and done < total:
                spent = time.time() - t_start
                _say(f"  {done}/{total} done in {_fmt_duration(spent)} -- about "
                     f"{_fmt_duration(spent / done * (total - done))} left", "cyan")
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    finally:
        _TIMEOUT_LOAD = 1

    # Safe to wipe wholesale: the directory belongs to this run only.
    shutil.rmtree(staging, ignore_errors=True)

    return {"converted": converted}


def run_purge_old_tiffs(cfg: ToolConfig) -> bool:
    """
    Verify and delete OLD_TIFFs/ folders.
    Compares each file in OLD_TIFFs with the equivalent in the parent folder
    (same filename), confirming content matches (ignoring compression).
    Shows sizes and asks for time confirmation before deleting.
    """
    global _TIMEOUT_LOAD
    folder = step_folder(cfg, "Root folder to scan for OLD_TIFFs")
    if folder is None:
        return False

    # Find all OLD_TIFFs folders recursively (case-insensitive for Unix)
    old_dirs = sorted([d for d in folder.rglob("*") if d.is_dir() and d.name.lower() == "old_tiffs"])
    if not old_dirs:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No OLD_TIFFs folders found.[/yellow]")
        else:
            print("No OLD_TIFFs folders found.")
        return True

    # Collect all files in OLD_TIFFs with their parent-equivalent paths
    items = []  # list of (old_file Path, new_file Path, old_size)
    mismatches = []

    for od in old_dirs:
        parent = od.parent
        for f in sorted(od.glob("*")):
            if not f.is_file():
                continue
            new_file = parent / f.name
            old_size = f.stat().st_size
            items.append((f, new_file, old_size))

    if not items:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No files found in OLD_TIFFs folders.[/yellow]")
        else:
            print("No files found in OLD_TIFFs folders.")
        return True

    # A backup whose parent counterpart is simply ABSENT is not a mismatch: there is nothing
    # to compare and nothing that may be deleted, so it is kept and reported. It used to be
    # lumped in with real mismatches and blocked the whole purge -- which a healthy folder
    # reaches routinely, because modes 0/9 rename a backup when one of that name already
    # exists (photo_20260805_101500.tif) and a photo.tiff source produces a photo.tif output.
    orphans = []
    MISSING_PARENT = "parent file missing"

    # Verify each image file; non-TIFF sidecars must at least have an identical-size copy in the parent
    def _verify(item):
        old_file, new_file, _ = item
        if old_file.suffix.lower() not in (".tif", ".tiff"):
            if not new_file.exists():
                return (old_file, new_file, MISSING_PARENT)
            # Compare content, not just size: this gate authorises permanent deletion, and a
            # same-size sidecar with different content used to pass.
            try:
                if new_file.stat().st_size != old_file.stat().st_size:
                    return (old_file, new_file, "sidecar differs from parent copy (size)")
                if _file_digest(old_file) != _file_digest(new_file):
                    return (old_file, new_file, "sidecar differs from parent copy (content)")
            except OSError as e:
                return (old_file, new_file, f"read failed: {e}")
            return None
        if not new_file.exists():
            return (old_file, new_file, MISSING_PARENT)
        match, detail = _compare_tiff_metadata(old_file, new_file)
        if not match:
            return (old_file, new_file, detail)
        return None

    workers = clamp_workers(cfg.config.last_workers or cfg.config.default_workers)
    workers = _memory_capped_pool_workers(workers, [it[0] for it in items if Path(it[0]).suffix.lower() in (".tif", ".tiff")], _COMPARE_BYTES_PER_PIXEL, "copy verification")
    _TIMEOUT_LOAD = workers
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for result in executor.map(_verify, items):
                if result:
                    (orphans if result[2] == MISSING_PARENT else mismatches).append(result)
    finally:
        _TIMEOUT_LOAD = 1

    orphan_keys = {(o[0], o[1]) for o in orphans}
    # A file that does not verify is KEPT -- and only that file. One mismatch used to abort
    # the purge for the entire tree, so a single bad conversion (or, before the thumbnail
    # rule above, any file compressed with -GenerateThumbnail) left the user with no way to
    # clean up the backups except deleting them by hand, which is exactly what this gate
    # exists to prevent. Same shape as the orphan handling right above.
    mismatch_keys = {(m[0], m[1]) for m in mismatches}
    deletable = [it for it in items
                 if (it[0], it[1]) not in orphan_keys and (it[0], it[1]) not in mismatch_keys]

    # Display results
    total_old_size = sum(s for _, _, s in deletable)

    if RICH_AVAILABLE and console:
        console.print(f"\n[cyan]OLD_TIFFs review -- {len(items)} file(s) in {len(old_dirs)} folder(s)[/cyan]")
        console.print(f"[dim]Total size in OLD_TIFFs: {_format_size(total_old_size)}[/dim]\n")
        console.print("[bold]File comparison (OLD_TIFFs vs parent):[/bold]")
    else:
        print(f"\nOLD_TIFFs review -- {len(items)} file(s) in {len(old_dirs)} folder(s)")
        print(f"Total size in OLD_TIFFs: {_format_size(total_old_size)}\n")
        print("File comparison (OLD_TIFFs vs parent):")

    mismatch_set = {(m[0], m[1]) for m in mismatches}
    mismatch_detail = {(m[0], m[1]): m[2] for m in mismatches}

    for old_file, new_file, old_size in items:
        is_mismatch = (old_file, new_file) in mismatch_set
        is_orphan = (old_file, new_file) in orphan_keys
        if new_file.exists():
            new_size = new_file.stat().st_size
            size_info = f"{_format_size(old_size)} -> {_format_size(new_size)}"
        else:
            size_info = f"{_format_size(old_size)} (parent missing)"
        detail = ""
        if is_mismatch:
            status = "[red]MISMATCH[/red]"
            detail = "  <- " + mismatch_detail.get((old_file, new_file), "")
        elif is_orphan:
            status = "[yellow]KEPT[/yellow]"
            detail = "  <- no counterpart in the parent folder, nothing to verify against"
        else:
            status = "OK"
        if RICH_AVAILABLE and console:
            console.print(f"  [{status}] {escape(str(old_file.relative_to(folder)))}  {size_info}{escape(detail)}")
        else:
            status_str = (status.replace("[red]", "").replace("[/red]", "")
                                .replace("[yellow]", "").replace("[/yellow]", ""))
            print(f"  [{status_str}] {old_file.relative_to(folder)}  {size_info}{detail}")

    if mismatches:
        head = (f"{len(mismatches)} file(s) did not verify against their parent copy and will "
                f"be KEPT (nothing below is deleted for them):")
        if RICH_AVAILABLE and console:
            console.print(f"\n[red]{escape(head)}[/red]")
            for old_file, new_file, reason in mismatches:
                console.print(f"  [red]! {escape(str(old_file.relative_to(folder)))}: {escape(reason)}[/red]")
        else:
            print(f"\n{head}")
            for old_file, new_file, reason in mismatches:
                print(f"  ! {old_file.relative_to(folder)}: {reason}")

    if orphans:
        note = (f"{len(orphans)} file(s) have no counterpart in the parent folder and will be "
                f"KEPT (a mode 0/9 backup is renamed when one of that name already exists, and "
                f"a .tiff source produces a .tif output, so the names need not line up).")
        if RICH_AVAILABLE and console:
            console.print(f"\n[yellow]{escape(note)}[/yellow]")
            for old_file, _new_file, _reason in orphans:
                console.print(f"  [yellow]= {escape(str(old_file.relative_to(folder)))}[/yellow]")
        else:
            print(f"\n{note}")
            for old_file, _new_file, _reason in orphans:
                print(f"  = {old_file.relative_to(folder)}")

    if not deletable:
        msg = "Nothing can be purged: no OLD_TIFFs file has a verified copy in its parent folder."
        if RICH_AVAILABLE and console:
            console.print(f"\n[yellow]{escape(msg)}[/yellow]")
        else:
            print(f"\n{msg}")
        return not mismatches

    # Summary
    if RICH_AVAILABLE and console:
        console.print(f"\n[green]{len(deletable)} of {len(items)} file(s) verified OK.[/green]")
        console.print(f"[dim]Total to delete: {_format_size(total_old_size)}[/dim]")
    else:
        print(f"\n{len(deletable)} of {len(items)} file(s) verified OK.")
        print(f"Total to delete: {_format_size(total_old_size)}")

    # Confirm
    if RICH_AVAILABLE and console:
        if not Confirm.ask("\n[red]Delete ALL OLD_TIFFs content? THIS CANNOT BE UNDONE.[/red]", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return False
        # Time confirmation (like mode 8)
        time_str = Prompt.ask("[red]Type current time (HH:MM) to confirm deletion[/red]").strip()
    else:
        resp = input("\nDelete ALL OLD_TIFFs content? THIS CANNOT BE UNDONE. [y/N]: ").strip().lower()
        if resp != "y":
            print("Cancelled.")
            return False
        time_str = input("Type current time (HH:MM) to confirm: ").strip()

    # Delete files
    from datetime import datetime
    try:
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        # Allow ±1 minute tolerance for typing delay
        time_parts = time_str.split(':')
        current_parts = current_time.split(':')
        time_min = int(time_parts[0]) * 60 + int(time_parts[1])
        current_min = int(current_parts[0]) * 60 + int(current_parts[1])
        diff = abs(time_min - current_min)
        diff = min(diff, 1440 - diff)  # Handle midnight wraparound
        if diff > 1:
            if RICH_AVAILABLE and console:
                console.print(f"[red]Time mismatch: expected {current_time}, got {time_str}.[/red]")
            else:
                print(f"Time mismatch: expected {current_time}, got {time_str}.")
            if RICH_AVAILABLE and console:
                console.print("[dim]Cancelled.[/dim]")
            else:
                print("Cancelled.")
            return False
    except Exception as e:
        if RICH_AVAILABLE and console:
            console.print(f"[red]Error validating time: {e}[/red]")
        else:
            print(f"Error validating time: {e}")
        return False

    # `deletable`, never `items`: an orphaned backup is the only copy of that image left,
    # so it is kept even when everything else in the folder verified clean.
    deleted = 0
    for old_file, _, _ in deletable:
        if not old_file.exists():
            continue
        try:
            old_file.unlink()
            deleted += 1
            if RICH_AVAILABLE and console:
                console.print(f"  [green]DELETED: {escape(str(old_file.relative_to(folder)))}[/green]")
            else:
                print(f"  DELETED: {old_file.relative_to(folder)}")
        except Exception as e:
            if RICH_AVAILABLE and console:
                console.print(f"  [red]ERROR deleting {escape(old_file.name)}: {escape(str(e))}[/red]")
            else:
                print(f"  ERROR deleting {old_file.name}: {e}")

    # Try to remove empty directories
    for od in sorted(old_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if od.exists() and not any(od.iterdir()):
                od.rmdir()
                if RICH_AVAILABLE and console:
                    console.print(f"  [green]Removed folder: {escape(str(od.relative_to(folder)))}[/green]")
                else:
                    print(f"  Removed folder: {od.relative_to(folder)}")
        except Exception:
            pass

    if RICH_AVAILABLE and console:
        console.print(f"\n[green]Done. Deleted {deleted} file(s).[/green]")
    else:
        print(f"\nDone. Deleted {deleted} file(s).")

    # False when something did not verify: those backups were deliberately kept, and the
    # caller should not read the run as "everything checked out".
    return not mismatches


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    if size_bytes < 0:
        return "0B"
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"

def _save_last_run(cfg, kind: str, label: str, commands: List[List[str]],
                   manifest_workflow: Optional[Dict] = None,
                   manifest_default_mode: Optional[int] = None,
                   autofind: Optional[Dict] = None,
                   diagnose: Optional[Dict] = None) -> None:
    """Record the last executed workflow for the 'Repeat last workflow' menu entry.

    Command-driven workflows (1-4, 8) are journaled with their command lines;
    Diagnose (7) with its settings (`diagnose`, no commands) -- a repeat of it only
    reports. Restore/Delete OLD_TIFFs (5/6) re-ask their own questions and are not
    repeatable.
    Commands are stored with dry-run stripped -- a repeat always runs for real.
    Manifest runs store no commands (None): a repeat re-reads the CSV through
    the loader guards instead of replaying command lines.

    `manifest_workflow`/`manifest_default_mode`/`autofind`/`diagnose` are written ALWAYS
    (None for the kinds that do not use them): keeping an older run's values
    around would let a repeat run with settings the wizard never answered.
    """
    cfg.config.last_run_kind = kind
    cfg.config.last_run_label = label
    cfg.config.last_run_commands = [[str(a) for a in cmd] for cmd in (commands or [])] or None
    cfg.config.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
    if manifest_workflow is not None:
        manifest_workflow = {k: v for k, v in manifest_workflow.items() if k != "dry_run"}
        manifest_workflow["dry_run"] = False
    cfg.config.last_manifest_workflow = manifest_workflow
    cfg.config.last_manifest_default_mode = manifest_default_mode
    cfg.config.last_autofind = autofind
    cfg.config.last_diagnose = diagnose
    cfg.save_config()


_JOURNAL_FIELDS = ("kind", "label", "commands", "time", "manifest_path",
                   "manifest_workflow", "manifest_default_mode", "autofind", "diagnose")


def _journal_snapshot(cfg) -> Dict:
    """The last-run journal as one plain dict -- what a repeat runs and what a preset
    stores. Copies, so a later run cannot change a saved preset."""
    c = cfg.config
    return json.loads(json.dumps({
        "kind": c.last_run_kind, "label": c.last_run_label,
        "commands": c.last_run_commands, "time": c.last_run_time,
        "manifest_path": c.last_manifest_path,
        "manifest_workflow": c.last_manifest_workflow,
        "manifest_default_mode": c.last_manifest_default_mode,
        "autofind": c.last_autofind,
        "diagnose": c.last_diagnose,
    }))


def _journal_runnable(entry: Dict) -> bool:
    if not entry.get("label"):
        return False
    if entry.get("kind") == "diagnose":
        return isinstance(entry.get("diagnose"), dict) and bool(entry["diagnose"].get("roots"))
    return bool(entry.get("commands") or entry.get("kind") == "manifest")


WORKFLOW_OPTIONS = [
    ("1", "Compress TIFFs", "To Zip/Deflate, modes 0-9 (any folder)"),
    ("2", "Fuji: Copy EXIF", "From JPEG to TIFF (AutoFind, S3/S5 Pro)"),
    ("3", "Fuji: Compress", "To Zip/Deflate (AutoFind, S3/S5 Pro)"),
    ("4", "Fuji: Copy + Compress", "Combined in one pass (AutoFind, S3/S5 Pro)"),
    ("5", "Restore OLD_TIFFs", "Move TIFFs from OLD_TIFFs/ to parent folder"),
    ("6", "Delete OLD_TIFFs", "Verify copy matches, then delete"),
    ("7", "Diagnose TIFFs", "Check if 16-bit is real or padded 8-bit"),
    ("8", "Generate Thumbnails", "Create sRGB thumbnails from TIFFs"),
]


def show_menu() -> Optional[str]:
    """Show workflow submenu (option 1 of the main menu), return choice or "0" for back."""
    valid = [k for k, _, _ in WORKFLOW_OPTIONS] + ["0"]
    if RICH_AVAILABLE and console:
        console.print()
        table = Table(title="New Workflow -- select type", box=BOX_SIMPLE, header_style="bold cyan")
        table.add_column("#", justify="center", style="cyan", width=4)
        table.add_column("Workflow", style="green")
        table.add_column("Description", style="dim")
        for key, name, desc in WORKFLOW_OPTIONS:
            table.add_row(key, name, desc)
        table.add_row("0", "Back", "Return to main menu")
        console.print(table)
        choice = Prompt.ask("\n[cyan]Select workflow[/cyan]", choices=valid)
    else:
        print("\n============================================")
        print("  New Workflow -- select type")
        print("============================================")
        for key, name, desc in WORKFLOW_OPTIONS:
            print(f"  [{key}] {name:<16} -- {desc}")
        print("  [0] Back             -- return to main menu")
        print("============================================")
        while True:
            choice = input("Select: ").strip()
            if choice in valid:
                break
            print(f"Invalid choice. Valid options: {', '.join(valid)}")

    return choice


def show_main_menu(cfg) -> str:
    """Display main menu - NO DEFAULT (jxl_photo style)."""
    last_label = cfg.config.last_run_label
    last_time = cfg.config.last_run_time
    has_last_run = _journal_runnable(_journal_snapshot(cfg))
    if has_last_run:
        repeat_text = f"Repeat last workflow ({last_label} -- {last_time})"
        if len(repeat_text) > 100:
            repeat_text = repeat_text[:97] + "..."
    else:
        repeat_text = "Repeat last workflow (none saved)"

    has_manifests = get_latest_manifest() is not None
    presets = cfg.config.presets or {}

    options = [
        ("1", "New workflow", True),
        ("2", repeat_text, has_last_run),
        ("3", "Run from manifest (CSV)", has_manifests),
        ("4", "Check dependencies again", True),
        ("5", f"Presets ({len(presets)} saved)", True),
        ("0", "Exit", True),
    ]

    if RICH_AVAILABLE and console:
        table = Table(show_header=False, box=None)
        table.add_column("Key", style="bold cyan")
        table.add_column("Option")
        table.add_column("Status", justify="center")
        for key, desc, available in options:
            status_str = "" if available else "[dim](unavailable)[/dim]"
            table.add_row(key, desc, status_str)
        console.print(Panel(table, title="Main Menu", border_style="green"))
        return Prompt.ask("Select option", choices=[o[0] for o in options if o[2]])

    print("\n--- Main Menu ---")
    for key, desc, available in options:
        status_str = "" if available else " [UNAVAILABLE]"
        print(f"[{key}] {desc}{status_str}")
    valid_choices = [o[0] for o in options if o[2]]
    while True:
        choice = input("\nSelect option: ").strip()
        if choice in valid_choices:
            return choice
        print(f"Invalid choice. Valid options: {', '.join(valid_choices)}")


# --- Manifest System -------------------------------------------------

TIFF_EXTS = {".tif", ".tiff"}

# Modes 2/4/5 can land two entries in the same folder (shared flat Destination,
# shared rename target, shared sibling ZIP/). Every other mode writes only
# inside each entry's own Source tree, so the collision scan is skipped there.
_COLLISION_SCAN_MODES = {2, 4, 5}

# Non-recursive modes: the manifest generator writes one entry per folder
# containing TIFFs. Recursive modes get a single entry for the root.
_PER_FOLDER_MODES = {0, 1}


def _manifest_error(msg: str) -> None:
    if RICH_AVAILABLE and console:
        console.print(f"[red]{escape(msg)}[/red]")
    else:
        print(f"ERROR: {msg}")


def _is_manifest_header_row(row) -> bool:
    """True only when the first row really is the CSV header -- not when a
    data folder happens to be named 'source'. A header names at least one of
    the other columns."""
    if not row or row[0].strip().lower() != "source":
        return False
    return any(cell.strip().lower() == expected
               for cell, expected in zip(row[1:3], ("destination", "mode")))


def _open_manifest_for_read(manifest_path):
    """Open a manifest CSV. Returns a text stream, or None if undecodable.

    'utf-8-sig' reads both what we write (UTF-8 with BOM, see
    generate_manifest) and a plain UTF-8 file, and strips the BOM.

    If the file is not valid UTF-8 at all, Excel re-saved it in the system
    ANSI codepage. We deliberately do NOT guess an encoding here: guessing
    wrong yields a path that looks plausible and points somewhere else, and
    these paths drive a converter that can delete sources in mode 8. Refuse
    and tell the user how to fix it. (Pure-ASCII manifests are valid UTF-8,
    so this only ever triggers when there really are non-ASCII paths.)
    """
    try:
        with open(manifest_path, 'r', encoding='utf-8-sig') as f:
            return io.StringIO(f.read())
    except UnicodeDecodeError:
        return None


def load_manifest_entries(manifest_path) -> Optional[List[Tuple[str, str, Optional[int]]]]:
    """Parse and validate a manifest CSV. Returns (source, dest, mode) entries
    -- mode is None for rows without a Mode cell -- or None (with an
    explanation already printed) when the file cannot be trusted.

    Shared by the wizard and by "Repeat last workflow" so both get the same
    Mode/traversal guards -- mode 8 deletes sources, so a repeat must never
    take a laxer path than the wizard did."""
    entries = []
    stream = _open_manifest_for_read(manifest_path)
    if stream is None:
        _manifest_error(
            f"Manifest is not UTF-8: {manifest_path}\n"
            f"Excel re-saved it in the system ANSI codepage, so the paths cannot be "
            f"decoded reliably -- and a wrongly-decoded path is worse than no run at all. "
            f"Re-open it in Excel and use 'Save As' -> 'CSV UTF-8 (comma delimited)', "
            f"or re-generate the manifest.")
        return None
    with stream as f:
        reader = csv.reader(f)
        first_row = next(reader, None)
        # Excel in pt-BR/de-DE locales saves "CSV" delimited by ';'. Parsed with ','
        # each line collapses into one cell: the header check fails and the whole
        # line becomes a "source" path containing ';' -- which the PowerShell backend
        # then SPLITS into two roots (it splits -InputDir on ';'). Detect and refuse.
        if first_row is not None and len(first_row) == 1 and ';' in first_row[0]:
            _manifest_error(
                f"Manifest appears to be ';'-delimited: {manifest_path}\n"
                f"This Excel locale saves CSV with ';' instead of ','. Re-save it as "
                f"'CSV UTF-8 (comma delimited)' or re-generate the manifest.")
            return None
        # Only skip the first row if it really is the header; a manifest
        # whose header line was deleted must not silently lose entry #1.
        rows = []
        if first_row is not None:
            if _is_manifest_header_row(first_row):
                rows = list(reader)
            else:
                rows = [first_row] + list(reader)
        for row in rows:
            if row and len(row) >= 1:
                source = row[0].strip()
                is_comment = not source or source.startswith('#')
                # Comment rows are ignored whole: parse NOTHING on them. An invalid
                # Mode cell on a line that would be skipped anyway used to refuse
                # the entire manifest.
                if is_comment:
                    continue
                # Empty Destination cell must fall back to Source, not to
                # "" -- Path("") becomes "." (the CWD) downstream.
                dest = (row[1].strip() or source) if len(row) > 1 else source
                # Mode cell: Excel often formats integers as "7.0" -- a
                # naive isdigit() check would silently turn that into
                # mode 0 and scatter outputs next to the sources. For the
                # same reason a FRACTION ("7.5") is refused outright rather
                # than truncated to 7.
                entry_mode = None
                if len(row) > 2 and row[2].strip():
                    try:
                        _mode_f = float(row[2].strip())
                        if not _mode_f.is_integer():
                            raise ValueError
                        entry_mode = int(_mode_f)
                    except ValueError:
                        _manifest_error(f"Invalid Mode value in manifest: {row[2].strip()!r} (row: {source})")
                        return None
                    if not 0 <= entry_mode <= 9:
                        _manifest_error(f"Mode out of range (0-9) in manifest: {entry_mode} (row: {source})")
                        return None
                # ';' is legal in Windows folder names, but the backend splits
                # -InputDir on ';', so one entry would arrive as two roots --
                # and in mode 8 the wrong root gets its sources deleted.
                if ';' in source or ';' in dest:
                    _manifest_error(
                        f"';' is not allowed in manifest paths: {source} -> {dest}\n"
                        f"The backend splits folder lists on ';', so this path would be "
                        f"processed as two different folders.")
                    return None
                # Validate paths to prevent directory traversal. Check
                # path PARTS (not a substring match, which false-
                # positives on legitimate names like "2024..final").
                if '..' in Path(source).parts or '..' in Path(dest).parts:
                    # Refuse the whole manifest, do not skip the row.
                    # Skipping shrank the run silently: the summary
                    # counts only the entries that LOADED, so a
                    # dropped folder looks exactly like a folder that
                    # compressed cleanly. Same fail-closed rule as the
                    # Mode checks above.
                    _manifest_error(
                        f"Path traversal in manifest entry: {source} -> {dest}\n"
                        f"Use absolute paths (or paths without '..') and run it again.")
                    return None
                entries.append((source, dest, entry_mode))

    if not entries:
        if RICH_AVAILABLE and console:
            console.print("[yellow]Manifest is empty or only has comments.[/yellow]")
        else:
            print("Manifest is empty or only has comments.")
        return None

    return entries


def generate_manifest(root: Path, mode: int) -> Optional[str]:
    """Generate a manifest CSV for `root` and return its path.

    Modes 0/1 are non-recursive, so they get one entry per folder containing
    TIFFs; recursive modes get a single entry for the root. Mode 2 pre-fills
    the Destination column with <root>/ZIP_flat; every other mode writes
    Destination == Source (ignored by the backend, edited by hand if needed).
    """
    from datetime import datetime

    root = Path(root)
    tiff_dirs = set()
    # Same exclusions as run_diagnose_tiffs: OLD_TIFFs holds the pristine originals the
    # compression modes archived -- a manifest entry pointing there would recompress the
    # backups, and in mode 8 DELETE them. Relative to the root, via _is_tool_output_path.
    for p in root.rglob("*"):
        try:
            if p.is_file() and p.suffix.lower() in TIFF_EXTS:
                if _is_tool_output_path(p, root):
                    continue
                tiff_dirs.add(p.parent)
        except OSError:
            continue
    if not tiff_dirs:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No TIFF files found -- nothing to add to the manifest.[/yellow]")
        else:
            print("No TIFF files found -- nothing to add to the manifest.")
        return None

    if mode in _PER_FOLDER_MODES:
        folders = sorted(tiff_dirs, key=lambda d: str(d).lower())
    else:
        folders = [root]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_dir = SCRIPT_DIR / "manifests"
    manifest_dir.mkdir(exist_ok=True)
    manifest_path = manifest_dir / f"manifest_{timestamp}.csv"

    # utf-8-sig, not plain utf-8: without the BOM, Excel opens a .csv using
    # the system ANSI codepage, so a path with non-ASCII characters comes out
    # as mojibake -- and saving from there would write those broken bytes
    # back. The BOM costs 3 bytes and makes Excel detect UTF-8 correctly.
    with open(manifest_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(["Source", "Destination", "Mode"])
        for folder in folders:
            dest = str(root / "ZIP_flat") if mode == 2 else str(folder)
            writer.writerow([str(folder), dest, mode])

    return str(manifest_path)


def get_latest_manifest() -> Optional[str]:
    """Get the most recent manifest file."""
    manifest_dir = SCRIPT_DIR / "manifests"
    if not manifest_dir.exists():
        return None
    manifests = list(manifest_dir.glob("manifest_*.csv"))
    if not manifests:
        return None
    return str(sorted(manifests, key=lambda p: p.stat().st_mtime, reverse=True)[0])


def pick_manifest() -> Optional[str]:
    """Pick a manifest from manifests/ (newest first). With several files
    the user chooses; with one it is used directly."""
    manifest_dir = SCRIPT_DIR / "manifests"
    if not manifest_dir.exists():
        return None
    manifests = sorted(manifest_dir.glob("manifest_*.csv"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        return None
    if len(manifests) == 1:
        return str(manifests[0])
    shown = manifests[:10]
    if RICH_AVAILABLE and console:
        console.print("[bold]Available manifests (newest first):[/bold]")
        for i, m in enumerate(shown, 1):
            console.print(f"  [{i}] {m.name}")
        choice = Prompt.ask("Which manifest?",
                            choices=[str(i) for i in range(1, len(shown) + 1)], default="1")
        return str(shown[int(choice) - 1])
    print("Available manifests (newest first):")
    for i, m in enumerate(shown, 1):
        print(f"  [{i}] {m.name}")
    raw = input("Which manifest? [1]: ").strip() or "1"
    try:
        idx = max(1, min(int(raw), len(shown))) - 1
    except ValueError:
        idx = 0
    return str(shown[idx])


def view_manifest(manifest_path) -> None:
    """View manifest contents in a table."""
    if not Path(manifest_path).exists():
        return

    entries = []
    stream = _open_manifest_for_read(manifest_path)
    if stream is None:
        _manifest_error(
            f"Manifest is not UTF-8: {manifest_path}\n"
            f"Excel re-saved it in the system ANSI codepage. Re-open it and use "
            f"'Save As' -> 'CSV UTF-8 (comma delimited)', or re-generate it.")
        return
    with stream as f:
        reader = csv.reader(f)
        first_row = next(reader, None)
        rows = []
        if first_row is not None:
            if _is_manifest_header_row(first_row):
                rows = list(reader)
            else:
                rows = [first_row] + list(reader)
        for row in rows:
            if row and not (len(row) == 1 and row[0].strip().startswith('#')):
                source = row[0].strip()
                dest = row[1].strip() if len(row) > 1 else source
                mode = row[2].strip() if len(row) > 2 else "?"
                if not source.startswith('#'):
                    entries.append((source, dest, mode))

    if not entries:
        if RICH_AVAILABLE and console:
            console.print("[yellow]Manifest is empty.[/yellow]")
        else:
            print("Manifest is empty.")
        return

    if RICH_AVAILABLE and console:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Source", style="red")
        table.add_column("Destination", style="green")
        table.add_column("Mode", style="magenta")
        for i, (src, dst, mode) in enumerate(entries, 1):
            table.add_row(str(i), truncate_path(Path(src)), truncate_path(Path(dst)), mode)
        console.print(Panel(table, title=f"[bold]Manifest[/bold] -- {manifest_path}", border_style="blue"))
        console.print(f"[dim]Total: {len(entries)} entry(ies)[/dim]")
    else:
        print(f"\n=== Manifest: {manifest_path} ===")
        print(f"Total: {len(entries)} entry(ies)\n")
        for i, (src, dst, mode) in enumerate(entries, 1):
            print(f"  {i}. {src}")
            print(f"     -> {dst} (mode {mode})")
            print()


def confirm_manifest_entries(manifest_path: str, entries: List[Tuple]) -> bool:
    """Preview the entries and ask for a go-ahead."""
    if RICH_AVAILABLE and console:
        console.print(f"\n[bold cyan]Manifest:[/bold cyan] {escape(manifest_path)}")
        console.print(f"[bold]Entries to process:[/bold] {len(entries)}")
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Source", style="red")
        table.add_column("Destination", style="green")
        table.add_column("Mode", style="magenta")
        for i, (src, dst, mode) in enumerate(entries[:15], 1):
            table.add_row(str(i), truncate_path(Path(src)), truncate_path(Path(dst)), str(mode))
        console.print(table)
        if len(entries) > 15:
            console.print(f"[dim]... and {len(entries) - 15} more entries[/dim]")
        console.print()
        return Confirm.ask("Proceed with manifest?", default=True)

    print(f"\nManifest: {manifest_path}")
    print(f"Entries to process: {len(entries)}\n")
    for i, (src, dst, mode) in enumerate(entries[:15], 1):
        print(f"  {i}. {src}")
        print(f"     -> {dst} (mode {mode})")
    if len(entries) > 15:
        print(f"  ... and {len(entries) - 15} more entries")
    print()
    return not input("Proceed with manifest? [Y/n]: ").strip().lower().startswith('n')


def manifest_source_overlaps(entries: List[Tuple]) -> List[Tuple[str, str]]:
    """Find manifest entries whose Source folders are equal or nested.

    Two entries covering the same tree re-process the same files as two
    SEPARATE child processes: same outputs written twice, and the collision
    guard deliberately ignores a file compared with itself, so it cannot see
    this. Returns a list of (source_a, source_b) tuples."""
    roots = [(source, os.path.normcase(os.path.abspath(source)))
             for source, _dest, _mode in entries]
    overlaps = []
    for i, (src_a, a) in enumerate(roots):
        for src_b, b in roots[i + 1:]:
            if a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep):
                overlaps.append((src_a, src_b))
    return overlaps


def _resolve_output_folder(f: Path, mode: int, src_root: Path, dest_cell: str) -> Optional[Path]:
    """Mirror of Resolve-Output in compress_tiff_zip.ps1, returning only the
    output FOLDER. Only modes 2/4/5 are resolved -- every other mode writes
    inside the Source tree and never reaches the collision scan."""
    parent = f.parent
    if mode == 2:
        # Mode 2 is recursive-FLAT: every file under the source lands in the
        # Destination (or the input root when the cell is empty).
        return Path(dest_cell) if dest_cell else src_root
    if mode == 4:
        name = parent.name
        if re.search(r"_tiff$", name, re.IGNORECASE):
            new_name = re.sub(r"_tiff$", "_ZIP", name, flags=re.IGNORECASE)
        elif name.lower() == "tiff":
            # Mirrors Resolve-Output: a folder named exactly "TIFF" has no stem, so the
            # suffix rule would produce a leading-underscore "_ZIP".
            new_name = "ZIP"
        elif re.search(r".tiff$", name, re.IGNORECASE):
            new_name = re.sub(r"tiff$", "_ZIP", name, flags=re.IGNORECASE)
        else:
            new_name = name + "_ZIP"
        return parent.parent / new_name
    if mode == 5:
        grandparent = parent.parent
        # Never climb above the input root (same rule as the backend). A plain
        # startswith() matches "C:\database" for root "C:\data" -- compare on
        # path parts instead of raw prefixes.
        try:
            inside = os.path.commonpath([str(grandparent), str(src_root)]).lower() == str(src_root).rstrip("/\\").lower()
        except ValueError:
            inside = False  # different drives
        if not inside:
            grandparent = src_root
        return grandparent / "ZIP"
    return None


def manifest_output_collisions(entries: List[Tuple]) -> List[Tuple]:
    """Find files that would be written to the same output folder with the
    same stem -- across different manifest entries OR within a single entry
    (mode 2 flattens recursively, so <src>/a/foto.tif and <src>/b/foto.tif
    share one destination). Within one entry the backend would apply
    DuplicateAction, but with Skip/Overwrite that means a silently dropped
    or overwritten image, so flagging is deliberate.

    Only modes 2/4/5 are scanned -- the others write inside each Source's
    own tree (and overlapping Sources were already refused/warned by
    manifest_source_overlaps).

    Returns a list of (file_a, file_b, dest_folder) tuples."""
    by_dest: Dict[str, Dict[str, Path]] = {}
    collisions = []
    for source, dest_path, mode in entries:
        if mode not in _COLLISION_SCAN_MODES:
            continue
        src_root = Path(source)
        try:
            if not src_root.is_dir():
                continue
        except OSError:
            continue
        # This walk is the slow part on large trees (recursive glob + per-file
        # stat) -- say so.
        if RICH_AVAILABLE and console:
            console.print(f"[dim]Collision check: scanning {escape(str(src_root))} ...[/dim]")
        else:
            print(f"Collision check: scanning {src_root} ...")
        try:
            files = sorted(src_root.rglob("*"))
        except OSError:
            continue
        for f in files:
            try:
                if not f.is_file() or f.suffix.lower() not in TIFF_EXTS:
                    continue
            except OSError:
                continue
            out_folder = _resolve_output_folder(f, mode, src_root, dest_path)
            if out_folder is None:
                continue
            key = os.path.normcase(str(out_folder))
            seen = by_dest.setdefault(key, {})
            # Outputs are named from the stem, so a stem clash is a clash.
            # normcase (not .lower()): case-sensitive filesystems treat
            # Foto.tif/foto.tif as distinct files and must NOT collide.
            stem = os.path.normcase(f.stem)
            prev = seen.get(stem)
            if prev is None:
                seen[stem] = f
            elif os.path.normcase(str(prev)) != os.path.normcase(str(f)):
                collisions.append((prev, f, out_folder))
    return collisions


def build_manifest_entry_cmd(source: str, dest_path: str, mode: int,
                             workflow: Dict, ps_name: str = "pwsh") -> List[str]:
    """Build the compress command for a single manifest entry, reusing
    build_compress_command with a per-entry workflow dict.

    The Destination column is only honored by mode 2 (-OutputDir); every
    other mode computes its own output location. -DeleteSource goes only to
    mode-8 entries, and never in a dry run."""
    entry_wf = dict(workflow)
    entry_wf["mode"] = mode
    entry_wf["output_dir"] = dest_path if mode == 2 else None
    entry_wf["delete_source"] = (mode == 8 and not workflow.get("dry_run"))
    return build_compress_command(entry_wf, folders=[Path(source)], ps_name=ps_name)


def execute_manifest_workflow(cfg, entries: List[Tuple], workflow: Dict,
                              unattended: bool = False) -> bool:
    """Execute a compress workflow from manifest entries.

    `entries` must be mode-resolved (no None modes -- the caller replaced
    them with the run's default mode). One child process per entry; a failed
    entry does not stop the rest. Returns True only when every entry exited
    cleanly.

    `unattended` (--run-preset) never asks: overlapping Sources refuse instead
    of offering "Run anyway?", each child gets -NonInteractive and a closed
    stdin, and the mode-8 gate below stays as a defensive refusal (the runner
    already refused a deleting preset before calling this)."""
    if not entries:
        _manifest_error("No manifest entries found!")
        return False

    dry_run = workflow.get("dry_run", False)
    ps_name = cfg.config.ps_name

    # The Destination column is only honored by mode 2 -- every other mode
    # computes its own output location from the backend's rules.
    ignored_dests = [(s, d, m) for s, d, m in entries
                     if m != 2 and d and Path(d) != Path(s)]
    if ignored_dests:
        warn = (f"Note: {len(ignored_dests)} manifest entry(ies) use modes that ignore the "
                f"Destination column (only mode 2 honors it) -- outputs follow the mode rules.")
        if RICH_AVAILABLE and console:
            console.print(f"[yellow]{warn}[/yellow]")
        else:
            print(f"WARNING: {warn}")
        for s, d, m in ignored_dests[:5]:
            print(f"  mode {m}: {d} (ignored)")

    # Mode 2 with Destination == Source flattens every TIFF into the input
    # root, mixing outputs with sources (same caveat as the wizard's mode 2).
    root_mix = [s for s, d, m in entries if m == 2 and Path(d) == Path(s)]
    if root_mix:
        warn = (f"Note: {len(root_mix)} mode-2 entry(ies) have Destination == Source: "
                f"all TIFFs land flattened in the input folder itself.")
        if RICH_AVAILABLE and console:
            console.print(f"[yellow]{warn}[/yellow]")
        else:
            print(f"WARNING: {warn}")

    # Overlapping source trees: one entry's Source inside another's means the
    # same files are processed twice by SEPARATE child processes.
    overlaps = manifest_source_overlaps(entries)
    if overlaps:
        head = (f"{len(overlaps)} manifest entry pair(s) have duplicate or nested "
                f"Source folders -- the same files would be processed twice:")
        if RICH_AVAILABLE and console:
            console.print(f"[yellow]{head}[/yellow]")
        else:
            print(f"WARNING: {head}")
        for a, b in overlaps[:10]:
            print(f"  {a}  <->  {b}")
        if unattended:
            msg = ("Refused: overlapping sources need a confirmation, which cannot be "
                   "given unattended.")
            if RICH_AVAILABLE and console:
                console.print(f"[red]{escape(msg)}[/red]")
            else:
                print(f"ERROR: {msg}")
            return False
        if RICH_AVAILABLE and console:
            ok_overlap = Confirm.ask("Run anyway?", default=False)
        else:
            ok_overlap = input("Run anyway? [y/N]: ").strip().lower().startswith('y')
        if not ok_overlap:
            return False

    # Cross-entry duplicate outputs: refuse loudly instead of silently
    # compressing only one of the two sources.
    entry_modes = {m for _, _, m in entries}
    if entry_modes & _COLLISION_SCAN_MODES:
        collisions = manifest_output_collisions(entries)
        if collisions:
            _manifest_error(
                "Aborting: manifest entries would write different files to the same output.")
            for a, b, d in collisions[:10]:
                msg = f"  {a} + {b}  ->  same name in {d}"
                if RICH_AVAILABLE and console:
                    console.print(f"[red]{escape(msg)}[/red]")
                else:
                    print(msg)
            if len(collisions) > 10:
                print(f"  ... and {len(collisions) - 10} more")
            _manifest_error(
                "Rename the inputs, pick another mode, or run the folder directly.")
            return False
    else:
        if RICH_AVAILABLE and console:
            console.print("[dim]Collision check: skipped (per-source output modes).[/dim]")
        else:
            print("Collision check: skipped (per-source output modes).")

    # A missing Source must fail closed: a folder that silently vanishes from
    # the run looks exactly like a folder that compressed cleanly.
    missing = [s for s, _, _ in entries if not Path(s).is_dir()]
    if missing:
        _manifest_error(f"{len(missing)} manifest Source folder(s) do not exist:")
        for s in missing[:10]:
            print(f"  {s}")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")
        return False

    # Mode 8 deletes sources. Gate the whole run with the same confirmation
    # the wizard enforces, ONCE, before anything runs. Without it,
    # build_manifest_entry_cmd never emits -DeleteSource.
    if not dry_run and any(m == 8 for _, _, m in entries):
        if unattended:
            # Defensive only: _run_journal refuses a deleting preset before it
            # reaches this call, because there is nobody to confirm. Fail closed
            # if a future caller ever gets here unattended.
            return False
        if RICH_AVAILABLE and console:
            if not Confirm.ask("[red]Manifest contains mode-8 entries: source TIFFs will be DELETED after compression. Are you sure?[/red]", default=False):
                console.print("[dim]Cancelled.[/dim]")
                return False
        else:
            confirm = input("Manifest contains mode-8 entries: source TIFFs will be DELETED. Confirm? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Cancelled.")
                return False

    total_entries = len(entries)
    ok_count = 0
    error_count = 0
    entry_reports: List[Dict] = []

    if RICH_AVAILABLE and console:
        console.print(f"\n[bold cyan]Executing manifest: {total_entries} entry(ies)[/bold cyan]")
        if dry_run:
            console.print("[yellow]DRY RUN MODE[/yellow]")
        console.print()
    else:
        print(f"\nExecuting manifest: {total_entries} entry(ies)")
        if dry_run:
            print("DRY RUN MODE")
        print()

    for i, (source, dest_path, mode) in enumerate(entries, 1):
        if RICH_AVAILABLE and console:
            console.print(f"[{i}/{total_entries}] [bold]Mode {mode}[/bold] | {escape(truncate_path(Path(source), 40))} -> {escape(truncate_path(Path(dest_path), 40))}")
        else:
            print(f"[{i}/{total_entries}] Mode {mode} | {source} -> {dest_path}")

        cmd = build_manifest_entry_cmd(source, dest_path, mode, workflow, ps_name)
        if unattended:
            cmd = cmd[:1] + ["-NonInteractive"] + cmd[1:]
            rc = run_subprocess(cmd, stdin=subprocess.DEVNULL)
        else:
            rc = run_subprocess(cmd)
        if rc == 0:
            ok_count += 1
            state = "ok"
        else:
            error_count += 1
            state = "failed"

        entry_reports.append({
            "index": i, "mode": mode, "source": source, "dest": dest_path,
            "state": state, "rc": rc,
        })

    _render_manifest_summary(entry_reports, ok_count, error_count, dry_run)

    return error_count == 0


def _render_manifest_summary(entry_reports: List[Dict], ok_count: int,
                             error_count: int, dry_run: bool) -> None:
    """Print the end-of-manifest block and write the combined wrapper log.

    A manifest can run for hours and the user walks away -- coming back to a
    single "3 OK" line meant opening N child logs to learn whether anything
    broke in the middle."""
    from datetime import datetime

    width = 75
    rule = "-" * width
    lines = ["=" * width]

    head = (f"Manifest complete: {len(entry_reports)} entries - "
            f"{ok_count} ok, {error_count} with failures")
    if dry_run:
        head = "[DRY RUN] " + head
    lines.append(head)
    lines.append(rule)
    lines.append(f"  {'#':<3}{'mode':<5}{'state':<8}folder")
    for rep in entry_reports:
        folder = rep["source"]
        max_folder = width - 18
        if len(folder) > max_folder:
            folder = "..." + folder[-(max_folder - 3):]
        lines.append(f"  {rep['index']:<3}{rep['mode']:<5}{rep['state']:<8}{folder}")

    failed = [r for r in entry_reports if r["state"] == "failed"]
    if failed:
        lines.append(rule)
        lines.append("Failed entries:")
        for r in failed:
            lines.append(f"  [{r['index']}] {r['source']} (exit {r['rc']})")
    lines.append("=" * width)

    print()
    for line in lines:
        print(line)

    # Combined wrapper log: the per-entry child logs live in
    # Logs/compress_tiff_zip/, so before this file there was no single place
    # holding the run's per-entry outcome.
    try:
        log_dir = SCRIPT_DIR / "Logs" / "convert_tiff"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))
            f.write("\n\nChild logs: Logs/compress_tiff_zip/ (one per entry)\n")
        print(f"Combined log: {log_path}")
    except OSError:
        pass


def run_manifest_workflow(cfg) -> bool:
    """Menu option 3: run the compress workflow from a manifest CSV."""
    manifest_path = pick_manifest()
    if not manifest_path or not Path(manifest_path).exists():
        if RICH_AVAILABLE and console:
            console.print("[yellow]No manifest found. Generate one from workflow [1] (Compress TIFFs).[/yellow]")
        else:
            print("No manifest found. Generate one from workflow [1] (Compress TIFFs).")
        return False

    entries = load_manifest_entries(manifest_path)
    if entries is None:
        return False

    # Rows without a Mode cell inherit a default mode asked once per run.
    default_mode = None
    if any(m is None for _, _, m in entries):
        if RICH_AVAILABLE and console:
            raw = Prompt.ask(
                "[cyan]Default mode for manifest rows without a Mode cell[/cyan]",
                choices=[str(m) for m in MODE_NAMES],
                default=str(cfg.config.last_mode or 0))
            default_mode = int(raw)
        else:
            raw = input(f"Default mode for rows without a Mode cell [{cfg.config.last_mode or 0}]: ").strip()
            try:
                default_mode = int(raw) if raw else (cfg.config.last_mode or 0)
            except ValueError:
                default_mode = cfg.config.last_mode or 0
            if not 0 <= default_mode <= 9:
                default_mode = 0
    if default_mode is not None:
        entries = [(s, d, m if m is not None else default_mode) for s, d, m in entries]

    workflow = {
        "origin": "free_compress",
        "dest": "zip",
        "workers": cfg.config.default_workers,
        "staging": "",
        "dry_run": False,
    }
    step_basic_params(cfg, workflow)

    # Collision policy for mode-2 (flattened) entries, same as the wizard.
    if any(m == 2 for _, _, m in entries):
        dup_default = _backend_default("compress_tiff_zip.ps1", "DuplicateAction")
        if RICH_AVAILABLE and console:
            workflow["duplicate_action"] = Prompt.ask(
                "[cyan]Duplicate filenames (mode 2)[/cyan]",
                choices=["Numbered", "Skip", "Overwrite"],
                default=dup_default,
            )
        else:
            dup = input(f"Duplicate filenames (Numbered/Skip/Overwrite) [{dup_default}]: ").strip() or dup_default
            workflow["duplicate_action"] = dup if dup in ("Numbered", "Skip", "Overwrite") else dup_default

    if not confirm_manifest_entries(manifest_path, entries):
        return False

    cfg.config.last_workers = workflow["workers"]
    cfg.config.last_manifest_path = manifest_path
    cfg.save_config()

    label = f"Manifest: {Path(manifest_path).name} ({len(entries)} entries)"
    if any(m == 8 for _, _, m in entries):
        label += " [DELETES SOURCES]"
    _save_last_run(cfg, "manifest", label, None,
                   manifest_workflow=workflow, manifest_default_mode=default_mode)

    return execute_manifest_workflow(cfg, entries, workflow)


# --- Workflow Runners -----------------------------------------------

def run_free_compress(cfg: ToolConfig) -> bool:
    """Workflow 1: Free compress with modes 0-9."""
    workflow = {
        "origin": "free_compress",
        "dest": "zip",
        "mode": None,
        "folders": [],
        "workers": cfg.config.default_workers,
        "staging": "",
        "dry_run": False,
    }

    # Mode selection
    mode = step_mode(cfg)
    if mode is None:
        return False
    workflow["mode"] = mode
    cfg.config.last_mode = mode

    # Folder
    folder = step_folder(cfg)
    if folder is None:
        return False
    workflow["input_dir"] = str(folder)

    # Offer to generate a manifest CSV of subfolders instead of running now.
    # The user edits it in Excel, then runs it from the main menu option [3].
    if RICH_AVAILABLE and console:
        gen_manifest = Confirm.ask(
            "[cyan]Generate a manifest CSV for this folder instead of running now?[/cyan]",
            default=False)
    else:
        gen_manifest = input("Generate a manifest CSV for this folder instead of running now? [y/N]: ").strip().lower() == "y"
    if gen_manifest:
        manifest_path = generate_manifest(folder, mode)
        if manifest_path:
            cfg.config.last_manifest_path = manifest_path
            cfg.save_config()
            if RICH_AVAILABLE and console:
                console.print(f"[green]Manifest saved to:[/green] {escape(manifest_path)}")
                console.print("[dim]Edit in Excel, then run it from the main menu: [3] Run from manifest[/dim]")
            else:
                print(f"Manifest saved to: {manifest_path}")
                print("Edit in Excel, then run it from the main menu: [3] Run from manifest")
        return True

    # Mode 2 flattens every TIFF into one folder. Without an explicit output folder the
    # backend falls back to the input root, mixing outputs with sources and recompressing
    # root-level TIFFs in place -- so ask for it here.
    if mode == 2:
        default_out = str(folder / "ZIP_flat")
        if RICH_AVAILABLE and console:
            out_dir = Prompt.ask(
                "[cyan]Output folder (mode 2 merges all TIFFs here)[/cyan]",
                default=default_out,
            ).strip()
        else:
            out_dir = input(f"Output folder (mode 2 merges all TIFFs here) [{default_out}]: ").strip() or default_out
        if len(out_dir) >= 2 and out_dir[0] == out_dir[-1] and out_dir[0] in ("'", '"'):
            out_dir = out_dir[1:-1]
        if ";" in out_dir:
            msg = f"Output path contains ';' which is not supported: {out_dir}"
            if RICH_AVAILABLE and console:
                console.print(f"[red]{escape(msg)}[/red]")
            else:
                print(f"ERROR: {msg}")
            return False
        if Path(out_dir).resolve() == folder.resolve():
            warn = "Output folder is the input folder: TIFFs will be recompressed in place, without a backup."
            if RICH_AVAILABLE and console:
                if not Confirm.ask(f"[yellow]{warn} Continue?[/yellow]", default=False):
                    console.print("[dim]Cancelled.[/dim]")
                    return False
            else:
                if input(f"{warn} Continue? [y/N]: ").strip().lower() != "y":
                    print("Cancelled.")
                    return False
        else:
            workflow["output_dir"] = out_dir

        # Collision policy for the flattened output
        dup_default = _backend_default("compress_tiff_zip.ps1", "DuplicateAction")
        if RICH_AVAILABLE and console:
            workflow["duplicate_action"] = Prompt.ask(
                "[cyan]Duplicate filenames[/cyan]",
                choices=["Numbered", "Skip", "Overwrite"],
                default=dup_default,
            )
        else:
            dup = input(f"Duplicate filenames (Numbered/Skip/Overwrite) [{dup_default}]: ").strip() or dup_default
            workflow["duplicate_action"] = dup if dup in ("Numbered", "Skip", "Overwrite") else dup_default

    # Basic params
    step_basic_params(cfg, workflow)

    # Thumbnail generation option. The defaults come from the backend's own param()
    # block; "Configure? No" adds no thumb_* keys at all, so the backend runs on its
    # own settings (audit round 17 -- the wizard used to force 256/"85"/jpg always).
    thumb_size_default = _backend_default("compress_tiff_zip.ps1", "ThumbSize")
    thumb_quality_default = _backend_default("compress_tiff_zip.ps1", "ThumbQuality")
    thumb_format_default = _backend_default("compress_tiff_zip.ps1", "ThumbFormat")
    if RICH_AVAILABLE and console:
        if Confirm.ask("[cyan]Generate embedded thumbnails?[/cyan]", default=False):
            workflow["generate_thumbnail"] = True
            if Confirm.ask("[cyan]Configure thumbnail settings?[/cyan]", default=False):
                # Thumbnail size
                size_str = Prompt.ask("[cyan]Thumbnail size (px)[/cyan]", default=str(thumb_size_default))
                workflow["thumb_size"] = int(size_str) if size_str.isdigit() else thumb_size_default
                # Quality
                quality_str = Prompt.ask("[cyan]JPEG quality[/cyan]", default=thumb_quality_default)
                workflow["thumb_quality"] = quality_str
                # Format
                workflow["thumb_format"] = Prompt.ask("[cyan]Format[/cyan]", choices=["jpg", "png", "tif"], default=thumb_format_default)
            # Option to skip compressed TIFFs with existing thumbnails
            workflow["skip_compressed_with_thumb"] = Confirm.ask("[cyan]Skip already-compressed TIFFs that have thumbnails?[/cyan]", default=False)
    else:
        gen_thumb = input("Generate embedded thumbnails? [y/N]: ").strip().lower()
        if gen_thumb == "y":
            workflow["generate_thumbnail"] = True
            config_thumb = input("Configure thumbnail settings? [y/N]: ").strip().lower()
            if config_thumb == "y":
                size_str = input(f"Thumbnail size (px) [{thumb_size_default}]: ").strip() or str(thumb_size_default)
                workflow["thumb_size"] = int(size_str) if size_str.isdigit() else thumb_size_default
                quality_str = input(f"JPEG quality [{thumb_quality_default}]: ").strip() or thumb_quality_default
                workflow["thumb_quality"] = quality_str
                fmt = input(f"Format (jpg/png/tif) [{thumb_format_default}]: ").strip().lower() or thumb_format_default
                # audit round 16: the Rich path constrains choices, but the plain-text
                # path passed any string straight to -ThumbFormat and the backend failed
                # at parameter binding. Fall back to the default with a warning instead.
                if fmt not in ("jpg", "jpeg", "png", "tif", "tiff"):
                    print(f"Invalid format '{fmt}', using {thumb_format_default}.")
                    fmt = thumb_format_default
                workflow["thumb_format"] = fmt
            skip_existing = input("Skip already-compressed TIFFs that have thumbnails? [y/N]: ").strip().lower()
            workflow["skip_compressed_with_thumb"] = (skip_existing == "y")

    # For mode 8: confirm delete
    if mode == 8:
        if RICH_AVAILABLE and console:
            if not Confirm.ask("[red]Mode 8 will DELETE source TIFFs after compression. Are you sure?[/red]", default=False):
                console.print("[dim]Cancelled.[/dim]")
                return False
        else:
            confirm = input("Mode 8 will DELETE source TIFFs. Confirm? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Cancelled.")
                return False
        workflow["delete_source"] = True
    else:
        workflow["delete_source"] = False

    # Summary + confirm
    if not step_confirm(workflow, cfg):
        return False

    cfg.config.last_workers = workflow["workers"]
    cfg.save_config()

    cmd = build_compress_command(workflow, folders=[folder], ps_name=cfg.config.ps_name)
    journal_wf = dict(workflow)
    journal_wf["dry_run"] = False
    journal_cmd = build_compress_command(journal_wf, folders=[folder], ps_name=cfg.config.ps_name)
    label = f"Compress TIFFs -- mode {mode} -- {truncate_path(folder, 60)}"
    if workflow.get("delete_source"):
        label += " [DELETES SOURCES]"
    _save_last_run(cfg, "compress", label, [journal_cmd])
    if RICH_AVAILABLE and console:
        console.print(f"\n[dim]Running: {' '.join(cmd)}[/dim]\n")
    else:
        print(f"\nRunning: {' '.join(cmd)}\n")

    result = run_subprocess(cmd)

    if workflow.get("dry_run"):
        if result != 0:
            if RICH_AVAILABLE and console:
                console.print("[yellow]Dry-run completed with errors. Review output above before running for real.[/yellow]")
            else:
                print("Dry-run completed with errors. Review output above before running for real.")
        if RICH_AVAILABLE and console:
            if Confirm.ask("\n[yellow]Dry-run complete. Run for real now?[/yellow]", default=False):
                console.print("[cyan]Running for real (DryRun disabled)...[/cyan]\n")
                workflow["dry_run"] = False
                cmd_real = build_compress_command(workflow, folders=[folder], ps_name=cfg.config.ps_name)
                run_subprocess(cmd_real)
            else:
                console.print("[dim]Skipped.[/dim]")
        else:
            resp = input("\nDry-run complete. Run for real now? [y/N]: ").strip().lower()
            if resp == "y":
                print("Running for real (DryRun disabled)...\n")
                workflow["dry_run"] = False
                cmd_real = build_compress_command(workflow, folders=[folder], ps_name=cfg.config.ps_name)
                run_subprocess(cmd_real)
            else:
                print("Skipped.")

    return True


def _run_exif_or_compress(cfg: ToolConfig, workflow_type: str) -> bool:
    """
    Shared implementation for Copy EXIF (2), Compress ZIP (3), Both (4).
    workflow_type: "copy_exif" | "compress" | "both"
    """
    workflow = {
        "origin": workflow_type,
        "dest": "zip" if workflow_type in ("compress", "both") else "tiff",
        "folders": [],
        "workers": cfg.config.default_workers,
        "staging": "",
        "dry_run": False,
        "pattern": None,
        "mode": 9 if workflow_type in ("compress", "both") else 0,
    }

    # Pattern selection
    patterns = step_pattern(cfg)
    if patterns is None:
        return False
    workflow["pattern"] = patterns

    # Root folder for AutoFind
    root = step_folder(cfg)
    if root is None:
        return False
    workflow["input_dir"] = str(root)

    # AutoFind
    found_folders = step_autofind(cfg, patterns, root)
    if not found_folders:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No folders found matching pattern. Workflow cancelled.[/yellow]")
        else:
            print("No folders found matching pattern. Workflow cancelled.")
        return False
    workflow["folders"] = found_folders

    # Basic params
    step_basic_params(cfg, workflow)

    # Summary + confirm
    if not step_confirm(workflow, cfg):
        return False

    cfg.config.last_workers = workflow["workers"]
    cfg.config.last_pattern = ";".join(patterns)
    cfg.save_config()

    def run_and_prompt(cmd_list, folders):
        if RICH_AVAILABLE and console:
            console.print(f"\n[dim]Running: {' '.join(cmd_list)}[/dim]\n")
        else:
            print(f"\nRunning: {' '.join(cmd_list)}\n")
        run_result = run_subprocess(cmd_list)
        if workflow.get("dry_run"):
            if run_result != 0:
                if RICH_AVAILABLE and console:
                    console.print("[yellow]Dry-run completed with errors. Review output above before running for real.[/yellow]")
                else:
                    print("Dry-run completed with errors. Review output above before running for real.")
            if RICH_AVAILABLE and console:
                if Confirm.ask("\n[yellow]Dry-run complete. Run for real now?[/yellow]", default=False):
                    console.print("[cyan]Running for real...[/cyan]\n")
                    workflow["dry_run"] = False
                    real_cmd = build_compress_command(workflow, folders=folders, ps_name=cfg.config.ps_name) if "compress" in workflow_type or workflow_type == "both" else build_copy_exif_command(workflow, folders=folders, ps_name=cfg.config.ps_name)
                    run_subprocess(real_cmd)
                else:
                    console.print("[dim]Skipped.[/dim]")
            else:
                resp = input("\nDry-run complete. Run for real now? [y/N]: ").strip().lower()
                if resp == "y":
                    print("Running for real...\n")
                    workflow["dry_run"] = False
                    real_cmd = build_compress_command(workflow, folders=folders, ps_name=cfg.config.ps_name) if "compress" in workflow_type or workflow_type == "both" else build_copy_exif_command(workflow, folders=folders, ps_name=cfg.config.ps_name)
                    run_subprocess(real_cmd)
                else:
                    print("Skipped.")

    journal_wf = dict(workflow)
    journal_wf["dry_run"] = False
    wf_names = {"copy_exif": "Fuji: Copy EXIF", "compress": "Fuji: Compress", "both": "Fuji: Copy + Compress"}
    label = (f"{wf_names[workflow_type]} -- pattern '{';'.join(patterns)}' under "
             f"{truncate_path(root, 60)} ({len(found_folders)} folders)")
    if workflow_type == "copy_exif":
        journal_cmds = [build_copy_exif_command(journal_wf, folders=found_folders, ps_name=cfg.config.ps_name)]
    elif workflow_type == "compress":
        journal_cmds = [build_compress_command(journal_wf, folders=found_folders, ps_name=cfg.config.ps_name)]
    else:
        journal_step1 = dict(journal_wf)
        journal_step1["compress_zip"] = False
        journal_cmds = [
            build_copy_exif_command(journal_step1, folders=found_folders, ps_name=cfg.config.ps_name),
            build_compress_command(journal_wf, folders=found_folders, ps_name=cfg.config.ps_name),
        ]
    _save_last_run(cfg, workflow_type, label, journal_cmds,
                   autofind={"root": str(root), "patterns": list(patterns)})

    if workflow_type == "copy_exif":
        cmd = build_copy_exif_command(workflow, folders=found_folders, ps_name=cfg.config.ps_name)
        run_and_prompt(cmd, found_folders)
        return True
    elif workflow_type == "compress":
        cmd = build_compress_command(workflow, folders=found_folders, ps_name=cfg.config.ps_name)
        run_and_prompt(cmd, found_folders)
        return True
    else:  # both
        step1_workflow = workflow.copy()
        step1_workflow["compress_zip"] = False
        cmd_copy = build_copy_exif_command(step1_workflow, folders=found_folders, ps_name=cfg.config.ps_name)
        if RICH_AVAILABLE and console:
            console.print(f"\n[cyan]=== Step 1/2: Copy EXIF ===[/cyan]")
            console.print(f"[dim]Running: {' '.join(cmd_copy)}[/dim]\n")
        else:
            print(f"\n=== Step 1/2: Copy EXIF ===")
            print(f"Running: {' '.join(cmd_copy)}\n")
        step1_result = run_subprocess(cmd_copy)

        if step1_result != 0:
            if RICH_AVAILABLE and console:
                console.print("[red]Step 1 (Copy EXIF) failed. Skipping Step 2 (Compress).[/red]")
            else:
                print("ERROR: Step 1 (Copy EXIF) failed. Skipping Step 2 (Compress).")
            return False

        cmd = build_compress_command(workflow, folders=found_folders, ps_name=cfg.config.ps_name)
        if RICH_AVAILABLE and console:
            console.print(f"\n[cyan]=== Step 2/2: Fuji: Compress ===[/cyan]")
            console.print(f"[dim]Running: {' '.join(cmd)}[/dim]\n")
        else:
            print(f"\n=== Step 2/2: Fuji: Compress ===")
            print(f"Running: {' '.join(cmd)}\n")
        step2_result = run_subprocess(cmd)

        if workflow.get("dry_run"):
            if step1_result != 0 or step2_result != 0:
                if RICH_AVAILABLE and console:
                    console.print("[yellow]Dry-run completed with errors. Review output above before running for real.[/yellow]")
                else:
                    print("Dry-run completed with errors. Review output above before running for real.")
            if RICH_AVAILABLE and console:
                if Confirm.ask("\n[yellow]Dry-run complete. Run both steps for real now?[/yellow]", default=False):
                    console.print("[cyan]Running for real...[/cyan]\n")
                    workflow["dry_run"] = False
                    copy_workflow = workflow.copy()
                    copy_workflow["compress_zip"] = False
                    cmd_copy_real = build_copy_exif_command(copy_workflow, folders=found_folders, ps_name=cfg.config.ps_name)
                    cmd_real = build_compress_command(workflow, folders=found_folders, ps_name=cfg.config.ps_name)
                    if RICH_AVAILABLE and console:
                        console.print(f"\n[cyan]=== Step 1/2: Copy EXIF ===[/cyan]")
                        console.print(f"[dim]Running: {' '.join(cmd_copy_real)}[/dim]\n")
                    else:
                        print(f"\n=== Step 1/2: Copy EXIF ===")
                        print(f"Running: {' '.join(cmd_copy_real)}\n")
                    # Same gate as the first pass above: a failed Copy EXIF must not fall
                    # through to Compress, which would ZIP TIFFs whose metadata never landed.
                    if run_subprocess(cmd_copy_real) != 0:
                        console.print("[red]Step 1 (Copy EXIF) failed. Skipping Step 2 (Compress).[/red]")
                        return False
                    if RICH_AVAILABLE and console:
                        console.print(f"\n[cyan]=== Step 2/2: Fuji: Compress ===[/cyan]")
                        console.print(f"[dim]Running: {' '.join(cmd_real)}[/dim]\n")
                    else:
                        print(f"\n=== Step 2/2: Fuji: Compress ===")
                        print(f"Running: {' '.join(cmd_real)}\n")
                    run_subprocess(cmd_real)
                else:
                    console.print("[dim]Skipped.[/dim]")
            else:
                resp = input("\nDry-run complete. Run both steps for real now? [y/N]: ").strip().lower()
                if resp == "y":
                    print("Running for real...\n")
                    workflow["dry_run"] = False
                    copy_workflow = workflow.copy()
                    copy_workflow["compress_zip"] = False
                    cmd_copy_real = build_copy_exif_command(copy_workflow, folders=found_folders, ps_name=cfg.config.ps_name)
                    cmd_real = build_compress_command(workflow, folders=found_folders, ps_name=cfg.config.ps_name)
                    print("=== Step 1/2: Copy EXIF ===")
                    if run_subprocess(cmd_copy_real) != 0:
                        print("ERROR: Step 1 (Copy EXIF) failed. Skipping Step 2 (Compress).")
                        return False
                    print("=== Step 2/2: Fuji: Compress ===")
                    run_subprocess(cmd_real)
                else:
                    print("Skipped.")
        return True


def run_generate_thumbnails(cfg: ToolConfig) -> bool:
    """Workflow 8: Generate sRGB thumbnails from TIFFs."""
    script = SCRIPT_DIR / "generate_thumbnails.ps1"
    if not script.exists():
        if RICH_AVAILABLE and console:
            console.print("[red]generate_thumbnails.ps1 not found.[/red]")
        else:
            print("ERROR: generate_thumbnails.ps1 not found.")
        return False
    
    # Input directory
    if RICH_AVAILABLE and console:
        input_dir = Prompt.ask("[cyan]Input directory[/cyan]", default=str(cfg.config.last_input_dir or "."))
    else:
        input_dir = input(f"Input directory [{cfg.config.last_input_dir or '.'}]: ").strip() or (cfg.config.last_input_dir or ".")
    input_dir = (input_dir or "").strip().strip('"').strip("'")
    p = Path(input_dir)
    if not p.is_dir():
        msg = f"Directory does not exist: {input_dir}"
        if RICH_AVAILABLE and console:
            console.print(f"[red]{escape(msg)}[/red]")
        else:
            print(msg)
        return False
    # The backend splits -InputDir on ';' like the other three, so a path containing one
    # would silently become two bogus roots. step_folder rejects it for every other workflow.
    if ";" in str(p):
        msg = f"Folder path contains ';' which is not supported: {p}"
        if RICH_AVAILABLE and console:
            console.print(f"[red]{escape(msg)}[/red]")
        else:
            print(f"ERROR: {msg}")
        return False
    cfg.config.last_input_dir = str(p.resolve())
    cfg.save_config()

    # Remove mode: skip the generation questions entirely
    if RICH_AVAILABLE and console:
        remove_mode = Confirm.ask("[yellow]Remove existing thumbnails instead of creating them?[/yellow]", default=False)
    else:
        remove_mode = input("Remove existing thumbnails instead of creating them? [y/N]: ").strip().lower() == "y"

    # Recursive / dry-run apply to both modes
    def _ask_recursive_and_dry():
        if RICH_AVAILABLE and console:
            return (
                Confirm.ask("[cyan]Recursive?[/cyan]", default=False),
                Confirm.ask("[cyan]Dry-run?[/cyan]", default=False),
            )
        return (
            input("Recursive? [y/N]: ").strip().lower() == "y",
            input("Dry-run? [y/N]: ").strip().lower() == "y",
        )

    def _ask_exclude_folders():
        """Same question the compress/Copy EXIF workflows ask. The backend used to carry a
        hardcoded, silent exclusion list instead (ZIP and _EXPORT included, which are exactly
        where the compressed library and the export trees live)."""
        default = cfg.config.last_exclude_folders or ""
        if RICH_AVAILABLE and console:
            value = Prompt.ask(
                "[cyan]Exclude folders?[/cyan] (';'-separated folder names, '-' = none)",
                default=default or "-").strip()
        else:
            value = input(f"Exclude folders? (';'-separated names, '-' = none) [{default or '-'}]: ").strip()
            if value == "":
                value = default
        if value in ("-", "", "none"):
            cfg.config.last_exclude_folders = None
            return ""
        cfg.config.last_exclude_folders = value
        return value

    def _ask_output_dir():
        if RICH_AVAILABLE and console:
            value = Prompt.ask("[cyan]Output folder (empty = next to each TIFF)[/cyan]", default="").strip()
        else:
            value = input("Output folder (empty = next to each TIFF) []: ").strip()
        value = value.strip('"').strip("'")
        if ";" in value:
            msg = f"Output path contains ';' which is not supported: {value}"
            if RICH_AVAILABLE and console:
                console.print(f"[red]{escape(msg)}[/red]")
            else:
                print(f"ERROR: {msg}")
            return None
        return value

    if remove_mode:
        out_dir = _ask_output_dir()
        if out_dir is None:
            return False
        recursive, dry_run = _ask_recursive_and_dry()
        cmd = [cfg.config.ps_name, "-NoProfile", "-File", str(script), "-InputDir", input_dir, "-Remove"]
        if out_dir:
            cmd += ["-OutputDir", out_dir]
        if recursive:
            cmd += ["-Recursive"]
        if dry_run:
            cmd += ["-DryRun"]
        _save_last_run(cfg, "thumbnails_remove", f"Thumbnails: REMOVE under {truncate_path(p, 60)}",
                       [[a for a in cmd if a != "-DryRun"]])
        if RICH_AVAILABLE and console:
            console.print(f"\n[dim]Running: {escape(' '.join(cmd))}[/dim]\n")
        else:
            print(f"\nRunning: {' '.join(cmd)}\n")
        return run_subprocess(cmd) == 0

    # The four prompt defaults come from generate_thumbnails.ps1's own param() block
    # (audit round 17): a literal here silently overrode an edited backend setting.
    size_default = _backend_default("generate_thumbnails.ps1", "Size")
    quality_default = _backend_default("generate_thumbnails.ps1", "Quality")
    format_default = _backend_default("generate_thumbnails.ps1", "Format")
    page_default = _backend_default("generate_thumbnails.ps1", "Page")

    # Thumbnail size (backend accepts 32-4096)
    if RICH_AVAILABLE and console:
        size_str = Prompt.ask("[cyan]Thumbnail size (px, 32-4096)[/cyan]", default=str(size_default))
    else:
        size_str = input(f"Thumbnail size (px, 32-4096) [{size_default}]: ").strip() or str(size_default)
    try:
        size = int(size_str)
    except ValueError:
        size = size_default
    if not (32 <= size <= 4096):
        msg = f"Size {size} out of range (32-4096), using {size_default}."
        if RICH_AVAILABLE and console:
            console.print(f"[yellow]{msg}[/yellow]")
        else:
            print(msg)
        size = size_default

    # Quality: the backend validates 1-100 and refuses to start otherwise
    if RICH_AVAILABLE and console:
        quality_str = Prompt.ask("[cyan]JPEG quality (1-100)[/cyan]", default=quality_default)
    else:
        quality_str = input(f"JPEG quality (1-100) [{quality_default}]: ").strip() or quality_default
    if not (quality_str.isdigit() and 1 <= int(quality_str) <= 100):
        msg = f"Invalid quality '{quality_str}' (must be 1-100), using {quality_default}."
        if RICH_AVAILABLE and console:
            console.print(f"[yellow]{escape(msg)}[/yellow]")
        else:
            print(msg)
        quality_str = quality_default

    # Format
    if RICH_AVAILABLE and console:
        fmt = Prompt.ask("[cyan]Format[/cyan]", choices=["jpg", "png", "tif"], default=format_default)
    else:
        fmt = input(f"Format (jpg/png/tif) [{format_default}]: ").strip().lower() or format_default
        if fmt not in ("jpg", "jpeg", "png", "tif", "tiff"):
            print(f"Invalid format '{fmt}', using {format_default}.")
            fmt = format_default

    # Page: 0 (first) or all
    if RICH_AVAILABLE and console:
        page = Prompt.ask("[cyan]Page[/cyan]", choices=["0", "all"], default=page_default)
    else:
        page = input(f"Page (0=first, all) [{page_default}]: ").strip().lower() or page_default
        if page != "all" and not page.isdigit():
            print(f"Invalid page '{page}', using {page_default}.")
            page = page_default

    out_dir = _ask_output_dir()
    if out_dir is None:
        return False
    recursive, dry_run = _ask_recursive_and_dry()
    exclude_folders = _ask_exclude_folders() if recursive else ""

    cmd = [
        cfg.config.ps_name, "-NoProfile", "-File", str(script),
        "-InputDir", input_dir,
        "-Size", str(size),
        "-Quality", quality_str,
        "-Format", fmt,
        "-Page", page,
        "-Workers", str(clamp_workers(cfg.config.last_workers or cfg.config.default_workers)),
    ]
    if out_dir:
        cmd += ["-OutputDir", out_dir]
    if exclude_folders:
        cmd += ["-ExcludeFolders", exclude_folders]
    if recursive:
        cmd += ["-Recursive"]
    if dry_run:
        cmd += ["-DryRun"]

    _save_last_run(cfg, "thumbnails", f"Thumbnails: {size}px {fmt} under {truncate_path(p, 60)}",
                   [[a for a in cmd if a != "-DryRun"]])

    if RICH_AVAILABLE and console:
        console.print(f"\n[dim]Running: {escape(' '.join(cmd))}[/dim]\n")
    else:
        print(f"\nRunning: {' '.join(cmd)}\n")

    return run_subprocess(cmd) == 0

# --- Main ---------------------------------------------------------


def _run_journal(cfg, entry: Dict, title: str, unattended: bool = False,
                 dry_run: bool = False) -> int:
    """Run a journal entry -- the last run (Repeat) or a preset. 0 = ran (or nothing
    to do), 1 = failed or refused. `unattended` (--run-preset): no prompt at all,
    stdin closed, -NonInteractive; a run that deletes sources is refused unless
    dry_run. `dry_run` comes only from the command line, never from the entry."""
    if not _journal_runnable(entry):
        _say("Nothing saved to run.", "yellow")
        return 1

    if entry.get("kind") == "diagnose":
        return _run_diagnose_journal(cfg, entry, title, unattended=unattended,
                                     dry_run=dry_run)

    if entry.get("kind") == "manifest":
        manifest_path = entry.get("manifest_path")
        if not manifest_path or not Path(manifest_path).exists():
            _say(f"Manifest CSV no longer available: {manifest_path or '(not saved)'}",
                 "yellow")
            return 1
        entries = load_manifest_entries(manifest_path)
        if entries is None:
            return 1
        workflow = entry.get("manifest_workflow")
        old_manifest_msg = ("This repeat was saved by an older version without the run's "
                            "settings -- run the manifest once more from menu 3, then "
                            "repeat or save it as a preset.")
        if workflow is None:
            _say(old_manifest_msg, "red")
            return 1
        default_mode = entry.get("manifest_default_mode")
        if any(m is None for _, _, m in entries):
            if default_mode is None:
                _say(old_manifest_msg, "red")
                return 1
            entries = [(s, d, m if m is not None else default_mode) for s, d, m in entries]
        workflow = dict(workflow)
        workflow["dry_run"] = dry_run
        deletes = (not dry_run) and any(m == 8 for _, _, m in entries)
        if unattended and deletes:
            _say("Refused: this preset deletes source files (mode 8). That needs a "
                 "confirmation, which cannot be given unattended -- run it from the "
                 "menu, or pass --dry-run.", "red")
            return 1
        if not unattended:
            if RICH_AVAILABLE and console:
                console.print(f"\n[bold cyan]{escape(title)}[/bold cyan]: "
                              f"{escape(Path(manifest_path).name)}")
                console.print(f"  {len(entries)} entry(ies)")
                if any(m == 8 for _, _, m in entries):
                    console.print("[red]WARNING: manifest contains mode-8 entries (DELETES sources).[/red]")
                if not Confirm.ask("Run it now?", default=False):
                    console.print("[dim]Cancelled.[/dim]")
                    return 1
            else:
                print(f"\n--- {title}: {Path(manifest_path).name} ---")
                print(f"  {len(entries)} entry(ies)")
                if any(m == 8 for _, _, m in entries):
                    print("WARNING: manifest contains mode-8 entries (DELETES sources).")
                if input("Run it now? [y/N]: ").strip().lower() != "y":
                    print("Cancelled.")
                    return 1
        ok = execute_manifest_workflow(cfg, entries, workflow, unattended=unattended)
        return 0 if ok else 1

    # Command-driven journals (1-4, 8): rebuild for the PowerShell detected NOW
    # instead of swapping argv[0] -- the journal may have been written by the other
    # shell, and the two invocation forms are not interchangeable (see _journal_parts).
    parts_list = [_journal_parts([str(a) for a in c]) for c in entry["commands"]]
    if any(p is None for p in parts_list):
        _say("Saved workflow cannot be replayed: its command line was recorded in a "
             "form this version does not recognise. Run the workflow again from the "
             "menu -- replaying it as-is could bind switches the wrong way.", "red")
        return 1

    # AutoFind is redone on every Fuji journal run: the root and the patterns were
    # saved with it, so a scheduled preset sees the sessions added since it was saved
    # (a journal from an older version has no such data and replays what it saved).
    # Only the Fuji workflows (2/3/4) search; their labels all start with "Fuji:".
    # Menu 1 also journals kind "compress" but has a fixed folder and no AutoFind.
    af = entry.get("autofind")
    is_fuji = (entry.get("kind") in ("copy_exif", "compress", "both")
               and str(entry.get("label") or "").startswith("Fuji:"))
    if is_fuji:
        if af:
            root = Path(af["root"])
            if not root.is_dir():
                _say(f"AutoFind root is gone: {root}", "red")
                return 1
            folders = sorted(find_folders_by_pattern(root, af["patterns"]))
            if not folders:
                _say(f"AutoFind: no matching folders under {root} -- nothing to do.",
                     "yellow")
                return 0
            folder_str = ";".join(str(f) for f in folders)
            for parts in parts_list:
                if "-InputDir" in parts:
                    i = parts.index("-InputDir")
                    if i + 1 < len(parts):
                        parts[i + 1] = folder_str
            _say(f"AutoFind: {len(folders)} session(s) under {root}")
        else:
            _say(f"Folders as saved on {entry.get('time')} (saved by an older version: "
                 f"sessions added since are not included)", "yellow")

    for parts in parts_list:
        # Validate the script that will actually run, which _journaled_script_path may have
        # swapped for the other Copy EXIF backend.
        script = _journaled_script_path(parts, cfg.config.ps_name)
        if not script.exists():
            _say(f"Backend script no longer exists: {script}", "red")
            return 1

    # Read the flag from the neutral parts: under powershell.exe every argument is folded
    # into one -Command string, so a membership test on the command list never matched and
    # the DELETES-SOURCES warning was silently skipped on PS5.
    deletes = (not dry_run) and any("-DeleteSource" in p for p in parts_list)
    if dry_run:
        for parts in parts_list:
            if "-DryRun" not in parts:
                parts.append("-DryRun")
    if unattended and deletes:
        _say("Refused: this preset deletes source files (-DeleteSource). That needs a "
             "confirmation, which cannot be given unattended -- run it from the "
             "menu, or pass --dry-run.", "red")
        return 1

    run_cmds = [_rebuild_journaled_command(p, cfg.config.ps_name, non_interactive=unattended)
                for p in parts_list]

    if RICH_AVAILABLE and console:
        console.print(f"\n[bold cyan]{escape(title)}[/bold cyan]")
        console.print(f"  {escape(entry.get('label') or '')}")
        console.print(f"  [dim]saved {entry.get('time')}[/dim]")
        for c in run_cmds:
            console.print(f"\n[dim]{escape(' '.join(c))}[/dim]")
        if dry_run:
            console.print("[yellow]Note: --dry-run requested -- this is a simulation (-DryRun).[/yellow]")
        else:
            console.print("[yellow]Note: dry-run is never inherited -- this runs for real.[/yellow]")
        if deletes:
            console.print("[red]WARNING: this workflow DELETES source files (-DeleteSource).[/red]")
        if not unattended and not Confirm.ask("Run it now?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return 1
    else:
        print(f"\n--- {title} ---")
        print(f"  {entry.get('label') or ''}")
        print(f"  saved {entry.get('time')}")
        for c in run_cmds:
            print(f"\n  {' '.join(c)}")
        if dry_run:
            print("Note: --dry-run requested -- this is a simulation (-DryRun).")
        else:
            print("Note: dry-run is never inherited -- this runs for real.")
        if deletes:
            print("WARNING: this workflow DELETES source files (-DeleteSource).")
        if not unattended and input("Run it now? [y/N]: ").strip().lower() != "y":
            print("Cancelled.")
            return 1

    for i, c in enumerate(run_cmds):
        if len(run_cmds) > 1:
            if RICH_AVAILABLE and console:
                console.print(f"\n[cyan]=== Step {i + 1}/{len(run_cmds)} ===[/cyan]")
            else:
                print(f"\n=== Step {i + 1}/{len(run_cmds)} ===")
        result = run_subprocess(c, stdin=subprocess.DEVNULL) if unattended else run_subprocess(c)
        if result != 0:
            _say(f"Step {i + 1} failed (exit {result}). Remaining steps skipped.", "red")
            return 1
    return 0


def run_repeat_last(cfg) -> bool:
    """Re-run the last workflow (menu option 2), through the shared journal runner.

    The stored commands were built with dry-run stripped, so a repeat always
    runs for real. Manifest runs take their own path inside _run_journal: the CSV
    is re-read through the loader guards and re-executed, never replayed blind.
    """
    return _run_journal(cfg, _journal_snapshot(cfg), "Repeat last workflow") == 0


def _clean_preset_name(raw: str) -> Optional[str]:
    """Reject names that would be unusable or would corrupt the config: the
    key ends up in JSON and in a menu row, so control characters and
    runaway lengths are cut here rather than at display time."""
    name = (raw or "").strip().strip('"\'')
    if not name or len(name) > 40:
        return None
    if any(ord(ch) < 32 for ch in name):
        return None
    return name


def presets_menu(cfg) -> None:
    """Named snapshots of the last workflow (main menu option 5), so several
    recurring jobs (a nightly manifest sync, a one-off shoot) can coexist
    instead of overwriting the single 'last workflow' slot. Saving stores a copy
    of the journal; running goes through the same _run_journal as 'Repeat last
    workflow', so the two cannot drift apart."""
    while True:
        presets = dict(cfg.config.presets or {})
        names = sorted(presets)

        if RICH_AVAILABLE and console:
            if names:
                table = Table(show_header=True, header_style="bold cyan", box=BOX_SIMPLE)
                table.add_column("#", justify="right", style="dim")
                table.add_column("Name", style="bold")
                table.add_column("Workflow")
                table.add_column("Saved", style="dim")
                for i, name in enumerate(names, 1):
                    table.add_row(str(i), name, presets[name].get("label", ""),
                                  presets[name].get("saved_at", ""))
                console.print(Panel(table, title="[bold]Presets[/bold]", border_style="green"))
            else:
                console.print("\n[dim]No presets saved yet.[/dim]")
            console.print("[bold]\\[number][/bold] run  "
                          "[bold]\\[S][/bold] save last workflow as preset  "
                          "[bold]\\[D][/bold] delete  [bold]\\[B][/bold] back")
            choice = Prompt.ask("Presets").strip()
        else:
            print("\n--- Presets ---")
            if names:
                for i, name in enumerate(names, 1):
                    print(f"  {i}. {name}")
                    print(f"     {presets[name].get('label', '')}  "
                          f"(saved {presets[name].get('saved_at', '')})")
            else:
                print("  (none saved yet)")
            print("[number] run | [S] save last workflow as preset | [D] delete | [B] back")
            choice = input("Presets: ").strip()

        upper = choice.upper()
        if not choice or upper == "B":
            return

        if upper == "S":
            snapshot = _journal_snapshot(cfg)
            if not _journal_runnable(snapshot):
                _say("Nothing to save yet: run a workflow first.", "yellow")
                continue
            if RICH_AVAILABLE and console:
                console.print(f"[dim]{escape(snapshot.get('label') or '')}[/dim]")
                raw_name = Prompt.ask("Preset name")
            else:
                print(f"  {snapshot.get('label') or ''}")
                raw_name = input("Preset name: ")
            name = _clean_preset_name(raw_name)
            if not name:
                _say("Invalid name (empty, too long, or contains control characters).", "red")
                continue
            if name in presets:
                if RICH_AVAILABLE and console:
                    overwrite = Confirm.ask("Replace it?", default=False)
                else:
                    overwrite = input("Replace it? [y/N]: ").strip().lower().startswith("y")
                if not overwrite:
                    continue
            presets[name] = {**snapshot, "saved_at": time.strftime("%Y-%m-%d %H:%M")}
            cfg.config.presets = presets
            cfg.save_config()
            _say(f"Preset '{name}' saved.", "green")
            continue

        if upper == "D":
            if not names:
                continue
            if RICH_AVAILABLE and console:
                target = Prompt.ask("Delete which number?").strip()
            else:
                target = input("Delete which number?: ").strip()
            if not target.isdigit() or not 1 <= int(target) <= len(names):
                _say("No preset with that number.", "red")
                continue
            name = names[int(target) - 1]
            if RICH_AVAILABLE and console:
                sure = Confirm.ask(f"Delete preset '{name}'?", default=False)
            else:
                sure = input(f"Delete preset '{name}'? [y/N]: ").strip().lower().startswith("y")
            if sure:
                del presets[name]
                cfg.config.presets = presets
                cfg.save_config()
                _say(f"Preset '{name}' deleted.", "green")
            continue

        if choice.isdigit() and 1 <= int(choice) <= len(names):
            name = names[int(choice) - 1]
            _run_journal(cfg, presets[name], f"Preset: {name}")
            continue

        _say("Not a valid option.", "red")


def main():
    # Plain print() on a legacy Windows console (cp850/cp1252) raises
    # UnicodeEncodeError on non-ANSI paths (e.g. 横浜) -- a crash in the middle of a
    # workflow. Rich's console handles this itself; the plain-text path needs it here.
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(
        description="TIFF Workflow Manager -- interactive wizard for TIFF compression, "
                    "EXIF copy, diagnostics and cleanup.",
        epilog="Run without arguments to start the interactive wizard. "
               "See README.md and docs/README_convert_tiff_py.md for full documentation.",
    )
    parser.add_argument("--run-preset", metavar="NAME",
                        help="Run a saved preset without the menu (Task Scheduler) and exit")
    parser.add_argument("--list-presets", action="store_true",
                        help="List saved presets and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --run-preset: simulate (-DryRun) instead of running")
    args = parser.parse_args()

    # A stray --dry-run would otherwise be ignored -- the worst outcome is a real
    # run for someone who asked for a simulation. Same rule as jxl-photo.
    if args.dry_run and not args.run_preset:
        parser.error("--dry-run only applies together with --run-preset")

    cfg = ConfigManager()

    # Read-only listing: no PowerShell detection, no environment panel, so it works
    # even on a machine where the shells are not on PATH.
    if args.list_presets:
        presets = cfg.config.presets or {}
        if not presets:
            print("No presets saved. Run a workflow, then save it from menu 5.")
        for name in sorted(presets):
            preset = presets[name]
            saved = preset.get("saved_at", "")
            line = f"    {preset.get('label', '')}" + (f"  (saved {saved})" if saved else "")
            print(f"{name}\n{line}")
        sys.exit(0)

    # Detect PowerShell version at startup
    ps_major, ps_name, ps_version = detect_powershell_version()
    cfg.config.ps_major = ps_major
    cfg.config.ps_name = ps_name  # store for use in command builders

    if args.run_preset:
        presets = cfg.config.presets or {}
        if args.run_preset not in presets:
            available = ", ".join(sorted(presets)) or "(none saved)"
            print(f"ERROR: no preset named '{args.run_preset}'. Available: {available}")
            sys.exit(2)
        sys.exit(_run_journal(cfg, presets[args.run_preset], f"Preset: {args.run_preset}",
                              unattended=True, dry_run=args.dry_run))

    show_environment_panel(cfg)

    while True:
        choice = show_main_menu(cfg)

        if choice == "0":
            if RICH_AVAILABLE and console:
                console.print("[dim]Done.[/dim]")
            else:
                print("Done.")
            break

        if choice == "4":
            ps_major, ps_name, ps_version = detect_powershell_version()
            cfg.config.ps_major = ps_major
            cfg.config.ps_name = ps_name
            show_environment_panel(cfg)
            continue

        if choice == "5":
            presets_menu(cfg)
            continue

        if choice == "3":
            run_manifest_workflow(cfg)
            continue

        if choice == "2":
            run_repeat_last(cfg)
            continue

        # choice == "1": New workflow -- pick the type
        wf = show_menu()
        if wf == "0":
            continue

        if wf == "1":
            run_free_compress(cfg)
        elif wf == "2":
            _run_exif_or_compress(cfg, "copy_exif")
        elif wf == "3":
            _run_exif_or_compress(cfg, "compress")
        elif wf == "4":
            _run_exif_or_compress(cfg, "both")
        elif wf == "5":
            run_undo_old_tiffs(cfg)
        elif wf == "6":
            run_purge_old_tiffs(cfg)
        elif wf == "7":
            run_diagnose_tiffs(cfg)
        elif wf == "8":
            run_generate_thumbnails(cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if RICH_AVAILABLE and console:
            console.print("\n[dim]Interrupted.[/dim]")
        else:
            print("\nInterrupted.")
        sys.exit(1)
