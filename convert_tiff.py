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

DEBUG_TIMING = False  # Set True to benchmark compression modes

# Subprocess timeouts (seconds)
CONVERT_TIMEOUT_S = 60   # magick depth/format conversions
COMPARE_TIMEOUT_S = 120  # magick compare (RMSE)

import argparse
import concurrent.futures
import csv
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

@dataclass
class ToolConfig:
    """Configuration for convert_tiff tools."""
    staging_dir: Optional[str] = None
    default_workers: int = 8
    export_marker: str = "_EXPORT"

    last_input_dir: Optional[str] = None
    last_workers: Optional[int] = None
    last_staging: Optional[str] = None
    last_pattern: Optional[str] = None
    last_mode: Optional[int] = None
    last_origin: Optional[str] = None
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
    ps_major: int = 0  # detected at startup, not persisted
    ps_name: str = "powershell"  # "pwsh" or "powershell", not persisted


class ConfigManager:
    """Persistent JSON config for convert_tiff."""

    def __init__(self):
        self.config_path = self._get_config_path()
        self.config = ToolConfig()
        self._load_config()

    def _get_config_path(self) -> Path:
        if platform.system() == "Windows":
            base = Path(os.environ.get("USERPROFILE", Path.home()))
        else:
            base = Path.home()
        return base / ".convert_tiff_config.json"

    def _load_config(self) -> None:
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if hasattr(self.config, k):
                            setattr(self.config, k, v)
            except Exception:
                pass

    def save_config(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(self.config)
            # Runtime-detected fields, not persisted
            data.pop("ps_major", None)
            data.pop("ps_name", None)
            with open(self.config_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass


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
    exclude_names = {"logs", "converted_zip", "zip", "_export", "old_tiffs"}
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
        choice = input(f"Mode [0]: ").strip() or "0"

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

def step_basic_params(cfg: ToolConfig, workflow: Dict) -> bool:
    """Workers, DryRun, Staging."""
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
        workflow["safe_mode"] = Confirm.ask("[cyan]Safe mode?[/cyan] (skip multi-page TIFFs, cap workers)", default=True)
        workflow["skip_lzw"] = Confirm.ask("[cyan]Skip LZW as already compressed?[/cyan] (LZW files will be ignored)", default=False)

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
        safe = input("Safe mode? (skip multi-page TIFFs, cap workers) [Y/n]: ").strip().lower()
        workflow["safe_mode"] = (safe != "n")
        skip_lzw = input("Skip LZW as already compressed? (LZW files will be ignored) [y/N]: ").strip().lower()
        workflow["skip_lzw"] = (skip_lzw == "y")
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
    if workflow.get("dry_run"):
        cmd += ["-DryRun"]
    if workflow.get("safe_mode") is False:
        cmd += ["-SafeMode:$false"]
    if workflow.get("skip_lzw"):
        cmd += ["-SkipLzwAsCompressed:$true"]
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
    if workflow.get("safe_mode") is False:
        cmd += ["-SafeMode:$false"]
    if workflow.get("skip_lzw"):
        cmd += ["-SkipLzwAsCompressed:$true"]
    if workflow.get("overwrite"):
        cmd += ["-Overwrite"]

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


def run_subprocess(cmd: List[str], timeout: Optional[int] = None) -> int:
    """Run command, stream output with Rich coloring. Has configurable timeout."""
    import time
    import sys
    import threading

    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        # Binary mode: lines are decoded manually in the reader thread
        # (UTF-8 with locale fallback, see _decode_console_line).
    }
    if sys.platform == "win32":
        # Keep the child in the same console process group so Ctrl+C propagates.
        popen_kwargs["creationflags"] = 0

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
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=10,
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
            if not f.exists():
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
            result = subprocess.run(
                ["magick", "identify", "-format", "%w %h", f"{path}[0]"],
                capture_output=True, text=True, timeout=30
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
            result = subprocess.run(
                ["magick", "identify", "-format", "%n\n", str(path)],
                capture_output=True, text=True, timeout=30
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
    if old_pages != new_pages:
        return False, f"PAGE_COUNT_MISMATCH {old_pages} vs {new_pages}"

    # Every page is compared pairwise. `magick compare A B` on multi-page files pairs
    # pages across the two lists and returns garbage RMSE (~27% for identical data), so
    # a single whole-file compare cannot be trusted; comparing only [0] would miss a
    # corrupted extra page. This gate authorises permanent deletion, so any page that
    # differs (or cannot be compared) blocks the purge.
    for page in range(old_pages):
        try:
            result = subprocess.run(
                ["magick", "compare", "-metric", "RMSE", f"{old_path}[{page}]", f"{new_path}[{page}]", "null:"],
                capture_output=True, text=True, timeout=COMPARE_TIMEOUT_S
            )
            output = result.stdout.strip() if result.stdout else result.stderr.strip() if result.stderr else ""

            if result.returncode not in (0, 1):
                return False, f"compare failed on page {page}: {output}"

            import re
            # Same pattern as _is_real_16bit: magick prints RMSE in scientific notation for
            # large values ("1.23457e+06 (0.0188)"), which the old pattern mis-parsed --
            # it matched "06" as the RMSE.
            match = re.search(r"([\d.]+(?:[eE][+-]?\d+)?)\s*\(([\d.eE+-]+)\)", output)
            if not match:
                return False, f"parse failed on page {page}: '{output}'"

            rmse = float(match.group(1))
            if rmse != 0.0:
                return False, f"PIXEL_DIFF page {page} RMSE={rmse}"

        except subprocess.TimeoutExpired:
            return False, f"compare timeout on page {page} (>{COMPARE_TIMEOUT_S}s)"
        except FileNotFoundError:
            return False, "ImageMagick not found (magick command missing)"
        except Exception as e:
            return False, f"compare error on page {page}: {e}"

    return True, f"IDENTICAL ({old_w}x{old_h}, {old_pages} page(s))"


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
            result = subprocess.run(
                ["magick", "identify", "-format", "%z", f"{path}[0]"],
                capture_output=True, text=True, timeout=30
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
            result = subprocess.run(
                ["magick", f"{tiff_path}[0]", "-depth", "8"] + (compress_8.split() if compress_8 else []) + [str(tmp8)],
                capture_output=True, timeout=CONVERT_TIMEOUT_S
            )
        except FileNotFoundError:
            return False, 0.0, "ERROR: ImageMagick not found"
        t1 = time.time() if DEBUG_TIMING else None
        if result.returncode != 0:
            return False, 0.0, "ERROR: 8-bit conversion failed"

        try:
            result = subprocess.run(
                ["magick", f"{tmp8}[0]", "-depth", "16"] + (compress_16.split() if compress_16 else []) + [str(tmp16)],
                capture_output=True, timeout=CONVERT_TIMEOUT_S
            )
        except FileNotFoundError:
            return False, 0.0, "ERROR: ImageMagick not found"
        t2 = time.time() if DEBUG_TIMING else None
        if result.returncode != 0:
            return False, 0.0, "ERROR: 16-bit back conversion failed"

        if not tmp16.exists():
            return False, 0.0, "ERROR: round-trip file missing"

        try:
            result = subprocess.run(
                ["magick", "compare", "-metric", "RMSE", f"{tiff_path}[0]", f"{tmp16}[0]", "null:"],
                capture_output=True, text=True, timeout=COMPARE_TIMEOUT_S
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


def run_diagnose_tiffs(cfg: ToolConfig) -> bool:
    """
    Workflow 7: Diagnose 16-bit TIFFs.
    Check if TIFFs marked as 16-bit are real 16-bit data or padded 8-bit.
    Uses parallel processing with configurable temp directory.
    """
    folder = step_folder(cfg, "Root folder to scan for TIFFs")
    if folder is None:
        return False

    tiff_files = sorted([
        f for f in folder.rglob("*")
        if f.suffix.lower() in (".tif", ".tiff") and f.is_file()
        and not any(p.lower() in ("old_tiffs", "old_padded", "logs", "zip", "_export", "converted_zip") for p in f.parts)
    ])

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
                temp_dir.mkdir(parents=True, exist_ok=True)
                console.print(f"[dim]Using temp dir: {temp_dir}[/dim]")
    else:
        resp = input("\nUse a specific temp directory? (leave empty for system temp): ").strip()
        if resp:
            temp_dir = Path(resp)
            temp_dir.mkdir(parents=True, exist_ok=True)
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

    if RICH_AVAILABLE and console:
        console.print(f"\n[cyan]Diagnosing {len(tiff_files)} TIFF file(s) with {workers} workers...[/cyan]")
    else:
        print(f"\nDiagnosing {len(tiff_files)} TIFF file(s) with {workers} workers...")

    results = []
    padded_files = []
    padded_count = 0
    real_count = 0
    other_count = 0
    error_count = 0

    def process_one(tiff_path):
        return tiff_path, _is_real_16bit(tiff_path, temp_dir, compress_tmp)

    total = len(tiff_files)
    completed = 0
    total_start = time.time() if DEBUG_TIMING else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
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

            if not is_real and "padded" in detail.lower():
                padded_count += 1
                padded_files.append(tiff_path)
                status = "PADDED"
            elif is_real:
                real_count += 1
                status = "real 16-bit"
            elif "not 16-bit" in detail:
                other_count += 1
                status = detail
            else:
                error_count += 1
                status = detail

            completed += 1
            progress = f"[{completed}/{total}]"

            if RICH_AVAILABLE and console:
                if not is_real and "padded" in detail.lower():
                    console.print(f"  {progress} [yellow]{escape(status)}[/yellow] {escape(tiff_path.name)}")
                elif not is_real:
                    console.print(f"  {progress} [red]{escape(status)}[/red] {escape(tiff_path.name)}")
                else:
                    console.print(f"  {progress} [dim]{escape(status)}[/dim] {escape(tiff_path.name)}")
            else:
                print(f"  {progress} {status} - {tiff_path.name}")

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


def _process_single_padded(tiff_path, staging):
    """Process a single padded file. Returns (name, parent, status, size_orig, size_zip, ratio, exif_ok, error_msg, tmp8_path)."""
    name = tiff_path.name
    parent = tiff_path.parent
    unique_id = uuid.uuid4().hex[:8]
    tmp8 = staging / f"tmp8_{unique_id}_{name}"
    final_dst = parent / name
    status = None

    try:
        try:
            result = subprocess.run(
                ["magick", str(tiff_path), "-depth", "8", "-compress", "zip", str(tmp8)],
                capture_output=True, timeout=CONVERT_TIMEOUT_S
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

        try:
            exif_result = subprocess.run(
                ["exiftool", "-q", "-overwrite_original",
                 "-tagsfromfile", str(tiff_path), "-all:all", "-unsafe",
                 "--Compression", "--BitsPerSample", "--SampleFormat",
                 str(tmp8)],
                capture_output=True, timeout=30
            )
            exif_ok = exif_result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            exif_ok = False

        if not tmp8.exists():
            status = "missing"
            return (name, parent, status, None, None, None, False, None, tmp8)

        # Integrity check: ensure the compressed file decodes and dimensions match
        try:
            verify_result = subprocess.run(
                ["magick", str(tmp8), "null:"],
                capture_output=True, timeout=30
            )
            if verify_result.returncode != 0:
                status = "integrity_error"
                return (name, parent, status, None, None, None, False, None, tmp8)

            # This gate authorises replacing the original with a LOSSY 16 -> 8-bit conversion,
            # so every branch fails CLOSED. Previously an unreadable identify (non-zero exit or
            # unparseable output) fell through to status = "ok" and the original was replaced
            # with nothing verified.
            def _read_dims(path) -> Optional[tuple]:
                r = subprocess.run(
                    ["magick", "identify", "-format", "%w %h\n", f"{path}[0]"],
                    capture_output=True, text=True, timeout=30
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
                r = subprocess.run(
                    ["magick", "identify", "-format", "%n\n", str(path)],
                    capture_output=True, text=True, timeout=30
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
        except Exception as e:
            status = "integrity_error"
            return (name, parent, status, None, None, None, False, str(e), tmp8)

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
    Uses parallel processing with ThreadPoolExecutor.
    """
    if temp_dir is None:
        temp_dir = Path(tempfile.gettempdir())
    # Run-scoped staging dir. A shared "compress_staging" meant the cleanup below wiped
    # every file in it, including another concurrent run's in-flight output -- if that run
    # was between the OLD_PADDED backup and the final move, the file vanished from the
    # source folder. Same reasoning as $runStagingId in compress_tiff_zip.ps1.
    staging = temp_dir / f"compress_staging_{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=True)

    def _process_one(tiff_path):
        return _process_single_padded(tiff_path, staging)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_one, f): i for i, f in enumerate(padded_files)}
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append((futures[future], future.result()))
            except Exception as e:
                f = padded_files[futures[future]]
                results.append((futures[future], (f.name, f.parent, "error", None, None, None, False, str(e), f)))

    results.sort(key=lambda x: x[0])

    for i, (idx, result) in enumerate(results):
        name, parent, status, size_orig, size_zip, ratio, exif_ok, error_msg, tmp8 = result
        final_dst = parent / name

        if RICH_AVAILABLE and console:
            console.print(f"  [{i+1}/{len(padded_files)}] {escape(name)}")
        else:
            print(f"  [{i+1}/{len(padded_files)}] {name}")

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
        elif status in ("integrity_error", "dimension_mismatch",
                        "dimension_unreadable", "page_count_mismatch"):
            if RICH_AVAILABLE and console:
                console.print(f"    [red]FAILED: {status}[/red]")
            else:
                print(f"    FAILED: {status}")
        elif status == "ok":
            if not exif_ok:
                # Do NOT overwrite original if EXIF was lost
                if RICH_AVAILABLE and console:
                    console.print(f"    [red]FAILED: EXIF copy failed — original preserved[/red]")
                else:
                    print(f"    FAILED: EXIF copy failed — original preserved")
                tmp8.unlink(missing_ok=True)
                continue

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
                    continue
                _safe_move(tmp8, final_dst)
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

    # Safe to wipe wholesale: the directory belongs to this run only.
    shutil.rmtree(staging, ignore_errors=True)

    return True


def run_purge_old_tiffs(cfg: ToolConfig) -> bool:
    """
    Verify and delete OLD_TIFFs/ folders.
    Compares each file in OLD_TIFFs with the equivalent in the parent folder
    (same filename), confirming content matches (ignoring compression).
    Shows sizes and asks for time confirmation before deleting.
    """
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

    # Verify each image file; non-TIFF sidecars must at least have an identical-size copy in the parent
    def _verify(item):
        old_file, new_file, _ = item
        if old_file.suffix.lower() not in (".tif", ".tiff"):
            if not new_file.exists():
                return (old_file, new_file, "parent file missing")
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
            return (old_file, new_file, "parent file missing")
        match, detail = _compare_tiff_metadata(old_file, new_file)
        if not match:
            return (old_file, new_file, detail)
        return None

    workers = cfg.config.last_workers or cfg.config.default_workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(_verify, items):
            if result:
                mismatches.append(result)

    # Display results
    total_old_size = sum(s for _, _, s in items)

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
        if new_file.exists():
            new_size = new_file.stat().st_size
            size_info = f"{_format_size(old_size)} -> {_format_size(new_size)}"
        else:
            size_info = f"{_format_size(old_size)} (parent missing)"
        detail = ""
        if is_mismatch:
            status = "[red]MISMATCH[/red]"
            detail = "  <- " + mismatch_detail.get((old_file, new_file), "")
        else:
            status = "OK"
        if RICH_AVAILABLE and console:
            console.print(f"  [{status}] {escape(str(old_file.relative_to(folder)))}  {size_info}{escape(detail)}")
        else:
            status_str = status.replace("[red]", "").replace("[/red]", "")
            print(f"  [{status_str}] {old_file.relative_to(folder)}  {size_info}{detail}")

    if mismatches:
        if RICH_AVAILABLE and console:
            console.print(f"\n[red]{len(mismatches)} file(s) have issues -- cannot purge.[/red]")
            for old_file, new_file, reason in mismatches:
                console.print(f"  [red]! {escape(str(old_file.relative_to(folder)))}: {escape(reason)}[/red]")
        else:
            print(f"\n{len(mismatches)} file(s) have issues -- cannot purge.")
            for old_file, new_file, reason in mismatches:
                print(f"  ! {old_file.relative_to(folder)}: {reason}")
        return False

    # Summary
    if RICH_AVAILABLE and console:
        console.print(f"\n[green]All {len(items)} file(s) verified OK.[/green]")
        console.print(f"[dim]Total to delete: {_format_size(total_old_size)}[/dim]")
    else:
        print(f"\nAll {len(items)} file(s) verified OK.")
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

    deleted = 0
    for old_file, _, _ in items:
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

    return True


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    if size_bytes < 0:
        return "0B"
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"

def _save_last_run(cfg, kind: str, label: str, commands: List[List[str]]) -> None:
    """Record the last executed workflow for the 'Repeat last workflow' menu entry.

    Only command-driven workflows (1-4, 8) are journaled: the interactive
    Python workflows (5/6/7) re-ask their own questions and are not repeatable.
    Commands are stored with dry-run stripped -- a repeat always runs for real.
    Manifest runs store no commands (None): a repeat re-reads the CSV through
    the loader guards instead of replaying command lines.
    """
    cfg.config.last_run_kind = kind
    cfg.config.last_run_label = label
    cfg.config.last_run_commands = [[str(a) for a in cmd] for cmd in (commands or [])] or None
    cfg.config.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
    cfg.save_config()


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
    has_last_run = bool(last_label and (cfg.config.last_run_commands
                                        or cfg.config.last_run_kind == "manifest"))
    if has_last_run:
        repeat_text = f"Repeat last workflow ({last_label} -- {last_time})"
        if len(repeat_text) > 100:
            repeat_text = repeat_text[:97] + "..."
    else:
        repeat_text = "Repeat last workflow (none saved)"

    has_manifests = get_latest_manifest() is not None

    options = [
        ("1", "New workflow", True),
        ("2", repeat_text, has_last_run),
        ("3", "Run from manifest (CSV)", has_manifests),
        ("4", "Check dependencies again", True),
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
    # backups, and in mode 8 DELETE them.
    _excluded_parts = {"old_tiffs", "old_padded", "logs", "zip", "_export", "converted_zip"}
    for p in root.rglob("*"):
        try:
            if p.is_file() and p.suffix.lower() in TIFF_EXTS:
                if any(part.lower() in _excluded_parts for part in p.relative_to(root).parts):
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
        elif re.search(r"tiff$", name, re.IGNORECASE):
            new_name = re.sub(r"tiff$", "_ZIP", name, flags=re.IGNORECASE)
        else:
            new_name = name + "_ZIP"
        return parent.parent / new_name
    if mode == 5:
        grandparent = parent.parent
        # Never climb above the input root (same rule as the backend).
        if not str(grandparent).lower().startswith(str(src_root).rstrip("/\\").lower()):
            grandparent = src_root
        return grandparent / "ZIP"
    return None


def manifest_output_collisions(entries: List[Tuple]) -> List[Tuple]:
    """Find files from DIFFERENT manifest entries that would be written to
    the same output folder with the same stem.

    Each manifest entry runs as a SEPARATE child process, so no child can see
    a conflict that spans two entries. Only modes 2/4/5 are scanned -- the
    others write inside each Source's own tree (and overlapping Sources were
    already refused/warned by manifest_source_overlaps).

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


def execute_manifest_workflow(cfg, entries: List[Tuple], workflow: Dict) -> bool:
    """Execute a compress workflow from manifest entries.

    `entries` must be mode-resolved (no None modes -- the caller replaced
    them with the run's default mode). One child process per entry; a failed
    entry does not stop the rest. Returns True only when every entry exited
    cleanly."""
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
        if RICH_AVAILABLE and console:
            workflow["duplicate_action"] = Prompt.ask(
                "[cyan]Duplicate filenames (mode 2)[/cyan]",
                choices=["Numbered", "Skip", "Overwrite"],
                default="Numbered",
            )
        else:
            dup = input("Duplicate filenames (Numbered/Skip/Overwrite) [Numbered]: ").strip() or "Numbered"
            workflow["duplicate_action"] = dup if dup in ("Numbered", "Skip", "Overwrite") else "Numbered"

    if not confirm_manifest_entries(manifest_path, entries):
        return False

    cfg.config.last_workers = workflow["workers"]
    cfg.config.last_manifest_path = manifest_path
    cfg.save_config()

    label = f"Manifest: {Path(manifest_path).name} ({len(entries)} entries)"
    if any(m == 8 for _, _, m in entries):
        label += " [DELETES SOURCES]"
    _save_last_run(cfg, "manifest", label, None)

    return execute_manifest_workflow(cfg, entries, workflow)


def _repeat_manifest(cfg) -> bool:
    """Repeat path for a manifest run: re-read the CSV through the same
    loader guards the wizard used, then re-execute with saved settings.
    Never replays journaled commands blindly -- the CSV may have been edited
    (or replaced) since the last run."""
    manifest_path = cfg.config.last_manifest_path
    if not manifest_path or not Path(manifest_path).exists():
        if RICH_AVAILABLE and console:
            console.print("[yellow]Manifest CSV no longer available.[/yellow]")
        else:
            print("Manifest CSV no longer available.")
        return False

    entries = load_manifest_entries(manifest_path)
    if entries is None:
        return False

    default_mode = cfg.config.last_mode or 0
    entries = [(s, d, m if m is not None else default_mode) for s, d, m in entries]

    workflow = {
        "origin": "free_compress",
        "dest": "zip",
        "workers": clamp_workers(cfg.config.last_workers or cfg.config.default_workers),
        "staging": cfg.config.last_staging or "",
        "dry_run": False,
    }
    if any(m == 2 for _, _, m in entries):
        workflow["duplicate_action"] = "Numbered"

    if RICH_AVAILABLE and console:
        console.print(f"\n[bold cyan]Repeat manifest[/bold cyan]: {escape(Path(manifest_path).name)}")
        console.print(f"  {len(entries)} entry(ies)")
        if any(m == 8 for _, _, m in entries):
            console.print("[red]WARNING: manifest contains mode-8 entries (DELETES sources).[/red]")
        if not Confirm.ask("Run it now?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return False
    else:
        print(f"\n--- Repeat manifest: {Path(manifest_path).name} ---")
        print(f"  {len(entries)} entry(ies)")
        if any(m == 8 for _, _, m in entries):
            print("WARNING: manifest contains mode-8 entries (DELETES sources).")
        if input("Run it now? [y/N]: ").strip().lower() != "y":
            print("Cancelled.")
            return False

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
        if RICH_AVAILABLE and console:
            workflow["duplicate_action"] = Prompt.ask(
                "[cyan]Duplicate filenames[/cyan]",
                choices=["Numbered", "Skip", "Overwrite"],
                default="Numbered",
            )
        else:
            dup = input("Duplicate filenames (Numbered/Skip/Overwrite) [Numbered]: ").strip() or "Numbered"
            workflow["duplicate_action"] = dup if dup in ("Numbered", "Skip", "Overwrite") else "Numbered"

    # Basic params
    step_basic_params(cfg, workflow)

    # Thumbnail generation option
    if RICH_AVAILABLE and console:
        if Confirm.ask("[cyan]Generate embedded thumbnails?[/cyan]", default=False):
            workflow["generate_thumbnail"] = True
            if Confirm.ask("[cyan]Configure thumbnail settings?[/cyan]", default=False):
                # Thumbnail size
                size_str = Prompt.ask("[cyan]Thumbnail size (px)[/cyan]", default="256")
                workflow["thumb_size"] = int(size_str) if size_str.isdigit() else 256
                # Quality
                quality_str = Prompt.ask("[cyan]JPEG quality[/cyan]", default="85")
                workflow["thumb_quality"] = quality_str
                # Format
                workflow["thumb_format"] = Prompt.ask("[cyan]Format[/cyan]", choices=["jpg", "png", "tif"], default="jpg")
            else:
                workflow["thumb_size"] = 256
                workflow["thumb_quality"] = "85"
                workflow["thumb_format"] = "jpg"
            # Option to skip compressed TIFFs with existing thumbnails
            workflow["skip_compressed_with_thumb"] = Confirm.ask("[cyan]Skip already-compressed TIFFs that have thumbnails?[/cyan]", default=False)
    else:
        gen_thumb = input("Generate embedded thumbnails? [y/N]: ").strip().lower()
        if gen_thumb == "y":
            workflow["generate_thumbnail"] = True
            config_thumb = input("Configure thumbnail settings? [y/N]: ").strip().lower()
            if config_thumb == "y":
                size_str = input("Thumbnail size (px) [256]: ").strip() or "256"
                workflow["thumb_size"] = int(size_str) if size_str.isdigit() else 256
                quality_str = input("JPEG quality [85]: ").strip() or "85"
                workflow["thumb_quality"] = quality_str
                fmt = input("Format (jpg/png/tif) [jpg]: ").strip().lower() or "jpg"
                workflow["thumb_format"] = fmt
            else:
                workflow["thumb_size"] = 256
                workflow["thumb_quality"] = "85"
                workflow["thumb_format"] = "jpg"
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
    _save_last_run(cfg, workflow_type, label, journal_cmds)

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

    # Thumbnail size (backend accepts 32-4096)
    if RICH_AVAILABLE and console:
        size_str = Prompt.ask("[cyan]Thumbnail size (px, 32-4096)[/cyan]", default="256")
    else:
        size_str = input("Thumbnail size (px, 32-4096) [256]: ").strip() or "256"
    try:
        size = int(size_str)
    except ValueError:
        size = 256
    if not (32 <= size <= 4096):
        msg = f"Size {size} out of range (32-4096), using 256."
        if RICH_AVAILABLE and console:
            console.print(f"[yellow]{msg}[/yellow]")
        else:
            print(msg)
        size = 256

    # Quality: the backend validates 1-100 and refuses to start otherwise
    if RICH_AVAILABLE and console:
        quality_str = Prompt.ask("[cyan]JPEG quality (1-100)[/cyan]", default="85")
    else:
        quality_str = input("JPEG quality (1-100) [85]: ").strip() or "85"
    if not (quality_str.isdigit() and 1 <= int(quality_str) <= 100):
        msg = f"Invalid quality '{quality_str}' (must be 1-100), using 85."
        if RICH_AVAILABLE and console:
            console.print(f"[yellow]{escape(msg)}[/yellow]")
        else:
            print(msg)
        quality_str = "85"

    # Format
    if RICH_AVAILABLE and console:
        fmt = Prompt.ask("[cyan]Format[/cyan]", choices=["jpg", "png", "tif"], default="jpg")
    else:
        fmt = input("Format (jpg/png/tif) [jpg]: ").strip().lower() or "jpg"
        if fmt not in ("jpg", "jpeg", "png", "tif", "tiff"):
            print(f"Invalid format '{fmt}', using jpg.")
            fmt = "jpg"

    # Page: 0 (first) or all
    if RICH_AVAILABLE and console:
        page = Prompt.ask("[cyan]Page[/cyan]", choices=["0", "all"], default="0")
    else:
        page = input("Page (0=first, all) [0]: ").strip().lower() or "0"
        if page != "all" and not page.isdigit():
            print(f"Invalid page '{page}', using 0.")
            page = "0"

    out_dir = _ask_output_dir()
    if out_dir is None:
        return False
    recursive, dry_run = _ask_recursive_and_dry()

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


def run_repeat_last(cfg) -> bool:
    """Re-run the journaled commands of the last workflow (menu option 2).

    The stored commands were built with dry-run stripped, so a repeat always
    runs for real. The PowerShell executable is swapped for the currently
    detected one, and the backend script path is re-validated before running.
    Manifest runs take their own path: the CSV is re-read through the loader
    guards and re-executed, never replayed blind.
    """
    if cfg.config.last_run_kind == "manifest":
        return _repeat_manifest(cfg)

    label = cfg.config.last_run_label
    commands = cfg.config.last_run_commands
    if not label or not commands:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No saved workflow to repeat.[/yellow]")
        else:
            print("No saved workflow to repeat.")
        return False

    run_cmds = [list(c) for c in commands]
    for c in run_cmds:
        if c:
            c[0] = cfg.config.ps_name

    for c in run_cmds:
        try:
            script = Path(c[c.index("-File") + 1])
        except (ValueError, IndexError):
            continue
        if not script.exists():
            msg = f"Backend script no longer exists: {script}"
            if RICH_AVAILABLE and console:
                console.print(f"[red]{escape(msg)}[/red]")
            else:
                print(f"ERROR: {msg}")
            return False

    deletes_sources = any("-DeleteSource" in c for c in run_cmds)

    if RICH_AVAILABLE and console:
        console.print(f"\n[bold cyan]Repeat last workflow[/bold cyan]")
        console.print(f"  {escape(label)}")
        console.print(f"  [dim]saved {cfg.config.last_run_time}[/dim]")
        for c in run_cmds:
            console.print(f"\n[dim]{escape(' '.join(c))}[/dim]")
        console.print("[yellow]Note: dry-run is never inherited -- this runs for real.[/yellow]")
        if deletes_sources:
            console.print("[red]WARNING: this workflow DELETES source files (-DeleteSource).[/red]")
        if not Confirm.ask("Run it now?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return False
    else:
        print("\n--- Repeat last workflow ---")
        print(f"  {label}")
        print(f"  saved {cfg.config.last_run_time}")
        for c in run_cmds:
            print(f"\n  {' '.join(c)}")
        print("Note: dry-run is never inherited -- this runs for real.")
        if deletes_sources:
            print("WARNING: this workflow DELETES source files (-DeleteSource).")
        if input("Run it now? [y/N]: ").strip().lower() != "y":
            print("Cancelled.")
            return False

    for i, c in enumerate(run_cmds):
        if len(run_cmds) > 1:
            if RICH_AVAILABLE and console:
                console.print(f"\n[cyan]=== Step {i + 1}/{len(run_cmds)} ===[/cyan]")
            else:
                print(f"\n=== Step {i + 1}/{len(run_cmds)} ===")
        result = run_subprocess(c)
        if result != 0:
            msg = f"Step {i + 1} failed (exit {result}). Remaining steps skipped."
            if RICH_AVAILABLE and console:
                console.print(f"[red]{msg}[/red]")
            else:
                print(f"ERROR: {msg}")
            return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="TIFF Workflow Manager -- interactive wizard for TIFF compression, "
                    "EXIF copy, diagnostics and cleanup.",
        epilog="Run without arguments to start the interactive wizard. "
               "See README.md and docs/README_convert_tiff_py.md for full documentation.",
    )
    parser.parse_args()

    cfg = ConfigManager()

    # Detect PowerShell version at startup
    ps_major, ps_name, ps_version = detect_powershell_version()
    cfg.config.ps_major = ps_major
    cfg.config.ps_name = ps_name  # store for use in command builders

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
