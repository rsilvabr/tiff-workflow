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

Supports AutoFind for S3/S5 Pro folders, persistent config,
and streaming output from PowerShell backends.
"""

DEBUG_TIMING = False  # Set True to benchmark compression modes

import argparse
import concurrent.futures
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from rich.console import Console
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

PROMPT_TOOLKIT_AVAILABLE = False


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
            with open(self.config_path, 'w') as f:
                json.dump(asdict(self.config), f, indent=2)
        except Exception:
            pass


# --- PowerShell Version Detection -----------------------------------

def detect_powershell_version():
    """Detect PowerShell version. Returns (major, exe_path)."""
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


# --- Helpers ------------------------------------------------------

def find_folders_by_pattern(root: Path, patterns: List[str]) -> Dict[Path, int]:
    """
    Recursively find folders matching any of the patterns.
    Returns dict of {folder_path: tiff_count}
    """
    results = {}
    exclude_names = {"logs", "converted_zip", "zip", "_export", "old_tiffs"}
    for path in root.rglob("*/"):
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
    """Truncate long paths for display."""
    s = str(p)
    if len(s) <= max_len:
        return s
    parts = s.split(os.sep)
    if len(parts) <= 3:
        return s
    return f"{parts[0]}{os.sep}...{os.sep}{parts[-2]}{os.sep}{parts[-1]}"


def _safe_move(src: Path, dst: Path) -> None:
    """Move file, overwriting destination if it exists (Windows-safe)."""
    if dst.exists():
        if dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
    shutil.move(str(src), str(dst))


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
    p = Path(folder)
    if not p.exists():
        if RICH_AVAILABLE and console:
            console.print(f"[red]Folder not found: {folder}[/red]")
        else:
            print(f"ERROR: Folder not found: {folder}")
        return None
    if not p.is_dir():
        if RICH_AVAILABLE and console:
            console.print(f"[red]Not a directory: {folder}[/red]")
        else:
            print(f"ERROR: Not a directory: {folder}")
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
            workflow["workers"] = workers
        except ValueError:
            console.print("[red]Invalid, using default.[/red]")
            workflow["workers"] = cfg.config.default_workers

        staging = Prompt.ask(
            "[cyan]Staging folder (SSD for faster I/O)[/cyan]",
            default=cfg.config.last_staging or cfg.config.staging_dir or ""
        ).strip()
        workflow["staging"] = staging
        cfg.config.last_staging = staging

        workflow["dry_run"] = Confirm.ask("Dry-run mode?", default=False)
        
        # SafeMode and SkipLzw options
        workflow["safe_mode"] = Confirm.ask("[cyan]Safe mode?[/cyan] (cap workers, extra checks)", default=True)
        workflow["skip_lzw"] = Confirm.ask("[cyan]Skip LZW as compressed?[/cyan] (treat LZW as uncompressed)", default=False)

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
            workflow["workers"] = int(workers_str) if workers_str else (cfg.config.last_workers or cfg.config.default_workers)
        except ValueError:
            workflow["workers"] = cfg.config.last_workers or cfg.config.default_workers
        staging = input(f"Staging folder (empty=disabled) []: ").strip()
        workflow["staging"] = staging
        cfg.config.last_staging = staging
        dry = input("Dry-run? [y/N]: ").strip().lower()
        workflow["dry_run"] = (dry == "y")
        safe = input("Safe mode? (cap workers, extra checks) [Y/n]: ").strip().lower()
        workflow["safe_mode"] = (safe != "n")
        skip_lzw = input("Skip LZW as compressed? [y/N]: ").strip().lower()
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

    if workflow.get("staging"):
        cmd += ["-StagingDir", workflow["staging"]]
    if workflow.get("workers"):
        cmd += ["-Workers", str(workflow["workers"])]
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
        cmd += ["-Workers", str(workflow["workers"])]
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

    return cmd


# --- Subprocess Runner ----------------------------------------------

def run_subprocess(cmd: List[str], timeout: int = 3600) -> int:
    """Run command, stream output with Rich coloring. Has configurable timeout."""
    import time
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace"
    )

    start_time = time.time()
    try:
        while True:
            # Check for timeout
            if time.time() - start_time > timeout:
                process.kill()
                if RICH_AVAILABLE and console:
                    console.print(f"[red]ERROR: Process timed out after {timeout}s[/red]")
                else:
                    print(f"ERROR: Process timed out after {timeout}s")
                return -1

            # Poll for output (cross-platform, works on Windows)
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            line = line.strip()
            if line:
                if RICH_AVAILABLE and console:
                    if " OK " in line or "+ZIP" in line:
                        console.print(f"  [green]{line}[/green]")
                    elif " | ERROR |" in line or " ERROR " in line:
                        console.print(f"  [red]{line}[/red]")
                    elif " | WARN |" in line or "WARNING" in line:
                        console.print(f"  [yellow]{line}[/yellow]")
                    elif "DRY" in line:
                        console.print(f"  [blue]{line}[/blue]")
                    else:
                        console.print(f"  {line}")
                else:
                    print(f"  {line}")
    except Exception as e:
        process.kill()
        if RICH_AVAILABLE and console:
            console.print(f"[red]ERROR: {e}[/red]")
        else:
            print(f"ERROR: {e}")
        return -1

    process.wait()
    try:
        process.stdout.close()
    except (OSError, ValueError):
        pass
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

    # Count total files
    total_files = 0
    for od in old_dirs:
        total_files += len([f for f in Path(od).glob("*") if f.is_file()])

    if RICH_AVAILABLE and console:
        console.print(f"\n[cyan]Found {len(old_dirs)} OLD_TIFFs folder(s) with {total_files} file(s)[/cyan]")
        for od in old_dirs:
            count = len([f for f in Path(od).glob("*") if f.is_file()])
            console.print(f"  {count:>4} files: {od}")
    else:
        print(f"\nFound {len(old_dirs)} OLD_TIFFs folder(s) with {total_files} file(s):")
        for od in old_dirs:
            count = len([f for f in Path(od).glob("*") if f.is_file()])
            print(f"  {count:>4} files: {od}")

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
    for od in old_dirs:
        old_path = Path(od)
        parent = old_path.parent
    for f in old_path.glob("*"):
        if not f.exists():
            continue
        dest = parent / f.name
        if dest.exists():
            if overwrite:
                _safe_move(f, dest)
                moved += 1
                if RICH_AVAILABLE and console:
                    console.print(f"  [green]OVERWRITE: {f.name}[/green]")
                else:
                    print(f"  OVERWRITE: {f.name}")
            else:
                skipped += 1
                if RICH_AVAILABLE and console:
                    console.print(f"  [yellow]SKIP (exists): {f.name}[/yellow]")
                else:
                    print(f"  SKIP (exists): {f.name}")
        else:
            _safe_move(f, dest)
            moved += 1
            if RICH_AVAILABLE and console:
                console.print(f"  [green]MOVED: {f.name}[/green]")
            else:
                print(f"  MOVED: {f.name}")

    # Ask to delete empty OLD_TIFFs folders
    if RICH_AVAILABLE and console:
        if Confirm.ask("\n[cyan]Delete empty OLD_TIFFs folders?[/cyan]", default=False):
            for od in old_dirs:
                if not any(Path(od).glob("*")):
                    Path(od).rmdir()
                    if RICH_AVAILABLE and console:
                        console.print(f"  [green]Removed: {od}[/green]")
                    else:
                        print(f"  Removed: {od}")
    else:
        resp = input("\nDelete empty OLD_TIFFs folders? [y/N]: ").strip().lower()
        if resp == "y":
            for od in old_dirs:
                if not any(Path(od).glob("*")):
                    Path(od).rmdir()
                    print(f"  Removed: {od}")

    if skipped > 0:
        msg = f"\n[green]Done. Moved {moved} file(s), skipped {skipped}.[/green]"
    else:
        msg = f"\n[green]Done. Moved {moved} file(s).[/green]"
    if RICH_AVAILABLE and console:
        console.print(msg)
    else:
        print(msg.replace("[green]", "").replace("[/green]", ""))

    return True


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
                ["magick", "identify", "-format", "%w %h", str(path)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if len(parts) == 2:
                    return int(parts[0]), int(parts[1])
            return None, None
        except FileNotFoundError:
            return None, None
        except Exception:
            return None, None

    old_w, old_h = get_dimensions(old_path)
    new_w, new_h = get_dimensions(new_path)

    if old_w is None or new_w is None:
        return False, "magick identify failed"

    if old_w != new_w or old_h != new_h:
        return False, f"DIMENSION_MISMATCH {old_w}x{old_h} vs {new_w}x{new_h}"

    try:
        result = subprocess.run(
            ["magick", "compare", "-metric", "RMSE", str(old_path), str(new_path), "null:"],
            capture_output=True, text=True, timeout=120
        )
        output = result.stdout.strip() if result.stdout else result.stderr.strip() if result.stderr else ""

        if result.returncode not in (0, 1):
            return False, f"compare failed: {output}"

        import re
        match = re.search(r"(\d+\.?\d*)\s*\((\d+\.?\d*e?[+-]?\d*)\)", output)
        if not match:
            return False, f"parse failed: '{output}'"

        rmse = float(match.group(1))
        if rmse == 0.0:
            return True, f"IDENTICAL ({old_w}x{old_h})"
        else:
            return False, f"PIXEL_DIFF RMSE={rmse}"

    except subprocess.TimeoutExpired:
        return False, "compare timeout (>120s)"
    except FileNotFoundError:
        return False, "ImageMagick not found (magick command missing)"
    except Exception as e:
        return False, f"compare error: {e}"


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
                ["magick", "identify", "-format", "%z", str(path)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return int(result.stdout.strip()), ""
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
        return False, 0.0, f"{depth}-bit (neither 8-bit nor 16-bit)"

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
            result = subprocess.run(
                ["magick", str(tiff_path), "-depth", "8"] + (compress_8.split() if compress_8 else []) + [str(tmp8)],
                capture_output=True, timeout=60
            )
        except FileNotFoundError:
            return True, 1.0, "ImageMagick not found"
        t1 = time.time() if DEBUG_TIMING else None
        if result.returncode != 0:
            return True, 1.0, "real 16-bit (8-bit conversion failed)"

        try:
            result = subprocess.run(
                ["magick", str(tmp8), "-depth", "16"] + (compress_16.split() if compress_16 else []) + [str(tmp16)],
                capture_output=True, timeout=60
            )
        except FileNotFoundError:
            return True, 1.0, "ImageMagick not found"
        t2 = time.time() if DEBUG_TIMING else None
        if result.returncode != 0:
            return True, 1.0, "real 16-bit (16-bit back conversion failed)"

        if not tmp16.exists():
            return True, 1.0, "real 16-bit (round-trip file missing)"

        try:
            result = subprocess.run(
                ["magick", "compare", "-metric", "RMSE", str(tiff_path), str(tmp16), "null:"],
                capture_output=True, text=True, timeout=120
            )
        except FileNotFoundError:
            return True, 1.0, "ImageMagick not found (compare failed)"
        t3 = time.time() if DEBUG_TIMING else None

        if DEBUG_TIMING and t3 is not None:
            import os
            sz8 = os.path.getsize(tmp8) if tmp8.exists() else 0
            sz16 = os.path.getsize(tmp16) if tmp16.exists() else 0
            print(f"[DEBUG] {tiff_path.name} | 8bit:{(t1-t0):.2f}s({sz8//1024}KB) 16bit:{(t2-t1):.2f}s({sz16//1024}KB) compare:{(t3-t2):.2f}s")

        output = result.stdout.strip() if result.stdout else result.stderr.strip() if result.stderr else ""

        if result.returncode not in (0, 1):
            return True, 1.0, f"real 16-bit (compare failed: {output})"

        import re
        match = re.search(r"(\d+\.?\d*)\s*\((\d+\.?\d*e?[+-]?\d*)\)", output)
        if not match:
            return True, 1.0, f"real 16-bit (parse failed: '{output}')"

        rmse = float(match.group(1))

        if rmse == 0.0:
            return False, 0.0, "padded 8-bit (round-trip RMSE=0)"
        else:
            return True, rmse, f"real 16-bit (RMSE={rmse})"

    except subprocess.TimeoutExpired:
        return True, 1.0, "real 16-bit (timeout)"
    except Exception as e:
        return True, 1.0, f"real 16-bit (error: {e})"
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
        and not any(p.lower() in ("old_tiffs", "logs", "zip", "_export", "converted_zip") for p in f.parts)
    ])

    if not tiff_files:
        if RICH_AVAILABLE and console:
            console.print("[yellow]No TIFF files found.[/yellow]")
        else:
            print("No TIFF files found.")
        return True

    workers = cfg.config.default_workers
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
                is_real = True
                stddev = 1.0
                detail = f"error: {e}"
            results.append((tiff_path, is_real, stddev, detail))

            if not is_real and "padded" in detail.lower():
                padded_count += 1
                padded_files.append(tiff_path)
                status = "PADDED"
            elif is_real:
                real_count += 1
                status = "real 16-bit"
            elif "neither 8-bit nor 16-bit" in detail:
                other_count += 1
                status = detail
            else:
                error_count += 1
                status = detail

            completed += 1
            progress = f"[{completed}/{total}]"

            if RICH_AVAILABLE and console:
                if not is_real and "padded" in detail.lower():
                    console.print(f"  {progress} [yellow]{status}[/yellow] {tiff_path.name}")
                elif not is_real:
                    console.print(f"  {progress} [red]{status}[/red] {tiff_path.name}")
                else:
                    console.print(f"  {progress} [dim]{status}[/dim] {tiff_path.name}")
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
                capture_output=True, timeout=60
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

        if tmp8.exists():
            size_orig = tiff_path.stat().st_size
            size_zip = tmp8.stat().st_size
            ratio = (1 - size_zip / size_orig) * 100 if size_orig > 0 else 0
            status = "ok"
            return (name, parent, status, size_orig, size_zip, ratio, exif_ok, None, tmp8)
        else:
            status = "missing"
            return (name, parent, status, None, None, None, False, None, tmp8)
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
    Uses parallel processing with ThreadPoolExecutor.
    """
    if temp_dir is None:
        temp_dir = Path(tempfile.gettempdir())
    staging = temp_dir / "compress_staging"
    staging.mkdir(parents=True, exist_ok=True)

    def _process_one(tiff_path):
        return _process_single_padded(tiff_path, staging)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_one, f): i for i, f in enumerate(padded_files)}
        for future in concurrent.futures.as_completed(futures):
            results.append((futures[future], future.result()))

    results.sort(key=lambda x: x[0])

    for i, (idx, result) in enumerate(results):
        name, parent, status, size_orig, size_zip, ratio, exif_ok, error_msg, tmp8 = result
        final_dst = parent / name

        if RICH_AVAILABLE and console:
            console.print(f"  [{i+1}/{len(padded_files)}] {name}")
        else:
            print(f"  [{i+1}/{len(padded_files)}] {name}")

        if status == "magick_not_found":
            if RICH_AVAILABLE and console:
                console.print(f"    [red]FAILED: magick not found[/red]")
            else:
                print(f"    FAILED: magick not found")
        elif status == "error":
            if RICH_AVAILABLE and console:
                console.print(f"    [red]FAILED: {error_msg}[/red]")
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
                _safe_move(tmp8, final_dst)
            else:
                if RICH_AVAILABLE and console:
                    console.print(f"    [dim]SKIPPED (ZIP larger than original)[/dim]")
                else:
                    print(f"    SKIPPED (ZIP larger than original)")
                tmp8.unlink(missing_ok=True)

    if staging.exists():
        for f in staging.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass
        try:
            staging.rmdir()
        except Exception:
            pass

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

    # Verify each file
    for old_file, new_file, old_size in items:
        if not new_file.exists():
            mismatches.append((old_file, new_file, "parent file missing"))
            continue
        match, detail = _compare_tiff_metadata(old_file, new_file)
        if not match:
            mismatches.append((old_file, new_file, detail))

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
            console.print(f"  [{status}] {old_file.relative_to(folder)}  {size_info}{detail}")
        else:
            status_str = status.replace("[red]", "").replace("[/red]", "")
            print(f"  [{status_str}] {old_file.relative_to(folder)}  {size_info}{detail}")

    if mismatches:
        if RICH_AVAILABLE and console:
            console.print(f"\n[red]{len(mismatches)} file(s) have issues -- cannot purge.[/red]")
            for old_file, new_file, reason in mismatches:
                console.print(f"  [red]! {old_file.relative_to(folder)}: {reason}[/red]")
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
            console.print("[dim]Cancelled.[/dim]") if RICH_AVAILABLE and console else print("Cancelled.")
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
                console.print(f"  [green]DELETED: {old_file.relative_to(folder)}[/green]")
            else:
                print(f"  DELETED: {old_file.relative_to(folder)}")
        except Exception as e:
            if RICH_AVAILABLE and console:
                console.print(f"  [red]ERROR deleting {old_file.name}: {e}[/red]")
            else:
                print(f"  ERROR deleting {old_file.name}: {e}")

    # Try to remove empty directories
    for od in sorted(old_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if od.exists() and not any(od.iterdir()):
                od.rmdir()
                if RICH_AVAILABLE and console:
                    console.print(f"  [green]Removed folder: {od.relative_to(folder)}[/green]")
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
    """Show main menu, return choice."""
    if RICH_AVAILABLE and console:
        console.print()
        table = Table(title="TIFF Workflow Manager -- convert_tiff", box=BOX_SIMPLE, header_style="bold cyan")
        table.add_column("#", justify="center", style="cyan", width=4)
        table.add_column("Workflow", style="green")
        table.add_column("Description", style="dim")
        for key, name, desc in WORKFLOW_OPTIONS:
            table.add_row(key, name, desc)
        console.print(table)
        choice = Prompt.ask("\n[cyan]Select workflow[/cyan]", choices=["1", "2", "3", "4", "5", "6", "7", "8"], default="1")
    else:
        print("\n============================================")
        print("  TIFF Workflow Manager -- convert_tiff")
        print("============================================")
        for key, name, desc in WORKFLOW_OPTIONS:
            print(f"  [{key}] {name:<16} -- {desc}")
        print("============================================")
        choice = input("Select [1]: ").strip() or "1"

    return choice if choice in ("1", "2", "3", "4", "5", "6", "7", "8") else None


# --- Workflow Runners -----------------------------------------------

def run_free_compress(cfg: ToolConfig) -> bool:
    """Workflow 1: Free compress with modes 0-8."""
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

    # Basic params
    step_basic_params(cfg, workflow)

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
                    run_subprocess(cmd_copy_real)
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
                    run_subprocess(cmd_copy_real)
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
    cfg.config.last_input_dir = input_dir
    
    # Thumbnail size
    if RICH_AVAILABLE and console:
        size_str = Prompt.ask("[cyan]Thumbnail size (px)[/cyan]", default="256")
    else:
        size_str = input("Thumbnail size (px) [256]: ").strip() or "256"
    try:
        size = int(size_str)
    except ValueError:
        size = 256
    
    # Quality
    if RICH_AVAILABLE and console:
        quality_str = Prompt.ask("[cyan]JPEG quality[/cyan]", default="85")
    else:
        quality_str = input("JPEG quality [85]: ").strip() or "85"
    
    # Format
    if RICH_AVAILABLE and console:
        fmt = Prompt.ask("[cyan]Format[/cyan]", choices=["jpg", "png", "tif"], default="jpg")
    else:
        fmt = input("Format (jpg/png/tif) [jpg]: ").strip().lower() or "jpg"
    
    # Recursive
    if RICH_AVAILABLE and console:
        recursive = Confirm.ask("[cyan]Recursive?[/cyan]", default=False)
    else:
        rec = input("Recursive? [y/N]: ").strip().lower()
        recursive = (rec == "y")
    
    # Dry run
    if RICH_AVAILABLE and console:
        dry_run = Confirm.ask("[cyan]Dry-run?[/cyan]", default=False)
    else:
        dry = input("Dry-run? [y/N]: ").strip().lower()
        dry_run = (dry == "y")
    
    cmd = [
        cfg.config.ps_name, "-NoProfile", "-File", str(script),
        "-InputDir", input_dir,
        "-Size", str(size),
        "-Quality", quality_str,
        "-Format", fmt,
    ]
    if recursive:
        cmd += ["-Recursive"]
    if dry_run:
        cmd += ["-DryRun"]
    
    if RICH_AVAILABLE and console:
        console.print(f"\n[dim]Running: {' '.join(cmd)}[/dim]\n")
    else:
        print(f"\nRunning: {' '.join(cmd)}\n")
    
    return run_subprocess(cmd) == 0


# --- Main ---------------------------------------------------------

def main():
    cfg = ConfigManager()

    # Detect PowerShell version at startup
    ps_major, ps_name, ps_version = detect_powershell_version()
    ps_label = f"PS{ps_major} ({ps_name})" if ps_major > 0 else "Unknown"
    cfg.config.ps_major = ps_major
    cfg.config.ps_name = ps_name  # store for use in command builders

    while True:
        if RICH_AVAILABLE and console:
            console.print("\n[bold cyan]========================================[/bold cyan]")
            console.print("[bold cyan]  TIFF Workflow Manager -- convert_tiff  [/bold cyan]")
            console.print("[bold cyan]========================================[/bold cyan]")
            # Show PS version
            ps_color = "green" if ps_major >= 7 else "yellow"
            console.print(f"[dim]PowerShell: [bold {ps_color}]{ps_label}[/bold {ps_color}] -- "
                          f"{'parallelism ENABLED' if ps_major >= 7 else 'sequential (PS5.1) -- parallelism DISABLED'}[/dim]")
        else:
            print("\n========================================")
            print("  TIFF Workflow Manager -- convert_tiff")
            print("========================================")
            print(f"PowerShell: {ps_label} -- {'parallelism ENABLED' if ps_major >= 7 else 'sequential -- parallelism DISABLED'}")

        choice = show_menu()
        if choice is None:
            if RICH_AVAILABLE and console:
                console.print("[red]Invalid choice.[/red]")
            else:
                print("Invalid choice.")
            continue

        if choice == "1":
            run_free_compress(cfg)
        elif choice == "2":
            _run_exif_or_compress(cfg, "copy_exif")
        elif choice == "3":
            _run_exif_or_compress(cfg, "compress")
        elif choice == "4":
            _run_exif_or_compress(cfg, "both")
        elif choice == "5":
            run_undo_old_tiffs(cfg)
        elif choice == "6":
            run_purge_old_tiffs(cfg)
        elif choice == "7":
            run_diagnose_tiffs(cfg)
        elif choice == "8":
            run_generate_thumbnails(cfg)

        if RICH_AVAILABLE and console:
            if not Confirm.ask("\n[cyan]Run another workflow?[/cyan]", default=False):
                console.print("[dim]Done.[/dim]")
                break
        else:
            again = input("\nRun another workflow? [y/N]: ").strip().lower()
            if not again.startswith("y"):
                print("Done.")
                break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if RICH_AVAILABLE and console:
            console.print("\n[dim]Interrupted.[/dim]")
        else:
            print("\nInterrupted.")
        sys.exit(1)
