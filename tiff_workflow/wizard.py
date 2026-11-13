"""
tiff_workflow.wizard -- TIFF Workflow Manager, the wizard `python convert_tiff.py` starts

Unified wizard for TIFF processing workflows:
  [1] Compress TIFFs  → Zip/Deflate, modes 0-9 (any folder)
  [2] Fuji: Copy EXIF from JPEG to TIFF (AutoFind, S3/S5 Pro)
  [3] Fuji: Compress → Zip/Deflate (AutoFind, S3/S5 Pro)
  [4] Fuji: Copy+Compress, one after the other (AutoFind, S3/S5 Pro)
  [5] Restore OLD_TIFFs move TIFFs back to parent folder
  [6] Delete OLD_TIFFs verify copy, then purge
  [7] Diagnose TIFFs check if 16-bit is real or padded
  [8] Generate Thumbnails create sRGB thumbnails from TIFFs

Supports AutoFind for S3/S5 Pro folders, persistent config,
and streaming output from the backend scripts.
"""

# The release, written into xmpMM:History by the padded 16 -> 8-bit conversion. Not a
# setting: one copy, in tiff_workflow/__init__.py (tests/test_diagnose_preset.py pins it to
# the newest docs/CHANGELOG.md section).
from . import TOOLKIT_NAME, TOOLKIT_URL, TOOLKIT_VERSION

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
import ast
import atexit
import concurrent.futures
import contextlib
import csv
import fnmatch
import functools
import hashlib
import io
import json
import locale
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# --- Yes / no -------------------------------------------------------
# One reader for every yes/no question, the same words on both screens (v3.1 phase 6, T2):
# until then six readers disagreed -- "yes" typed out counted as NO in 22 questions of the
# plain screen, and the Portuguese "s"/"sim" the owner types was NO nearly everywhere.
_YES_WORDS = ("y", "yes", "s", "sim")
_NO_WORDS = ("n", "no", "nao", "não")


def _yes(answer, default: bool) -> bool:
    """A typed answer read as yes/no; anything else (Enter included) is the question's
    default -- "no" for every question that deletes or replaces."""
    a = (answer or "").strip().lower()
    if a in _YES_WORDS:
        return True
    if a in _NO_WORDS:
        return False
    return default


try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.box import SIMPLE as BOX_SIMPLE
    from rich.prompt import Prompt, InvalidResponse
    from rich.prompt import Confirm as _RichConfirm

    class Confirm(_RichConfirm):
        """Rich's yes/no question with the plain screen's words (y/yes/s/sim, n/no/nao/não);
        another answer is asked again, as Rich does."""

        def process_response(self, value: str) -> bool:
            a = value.strip().lower()
            if a in _YES_WORDS:
                return True
            if a in _NO_WORDS:
                return False
            raise InvalidResponse(self.validate_error_message)

    RICH_AVAILABLE = True
    console: Optional[Console] = Console(force_terminal=True)
except ImportError:
    RICH_AVAILABLE = False
    console = None


# --- One screen (v3.1 phase 7, T4) ----------------------------------------------
# What the wizard shows and asks goes through these functions; only they know whether Rich is
# there (154 `if RICH_AVAILABLE` forks said most things twice, and the two halves drifted
# apart). A text is given plain -- or as a list of parts, each a text or a (text, style)
# pair. On the Rich screen it becomes a Text, in which Rich reads no markup: a folder named
# "[bold]", "[/b]" or "G:\" shows as it is, with nothing to escape.

def _plain(text) -> str:
    """`text` (a text or a list of parts) as the plain screen prints it."""
    if isinstance(text, str):
        return text
    return "".join(p[0] if isinstance(p, tuple) else p for p in text)


def _rich_text(text, style: str = ""):
    """`text` (a text or a list of parts) as a Rich Text, in `style`; a part's own style wins
    over `style`, as an inner markup tag did."""
    out = Text()
    for p in ([text] if isinstance(text, str) else text):
        if isinstance(p, tuple):
            out.append(p[0], style=p[1] or None)
        else:
            out.append(p)
    if style:
        out.stylize_before(style)
    return out


def _highlighted(t):
    """`t` over the console's highlighting -- what console.print does to a string (numbers,
    paths, ...), so the screen keeps its colours."""
    highlighter = getattr(console, "highlighter", None)
    if highlighter is None:
        return t
    out = highlighter(t.plain)
    out.copy_styles(t)
    return out


def _say(text, style: str = "") -> None:
    """Show `text` (a text or a list of parts), in `style` on the Rich screen."""
    if RICH_AVAILABLE and console:
        console.print(_highlighted(_rich_text(text, style)))
    else:
        print(_plain(text))


def _error(text) -> None:
    """An error: red on the Rich screen, `ERROR: ` in front on the plain one."""
    if RICH_AVAILABLE and console:
        console.print(_highlighted(_rich_text(text, "red")))
    else:
        print(f"ERROR: {_plain(text)}")


def _warn(text) -> None:
    """A warning: yellow on the Rich screen, `WARNING: ` in front on the plain one."""
    if RICH_AVAILABLE and console:
        console.print(_highlighted(_rich_text(text, "yellow")))
    else:
        print(f"WARNING: {_plain(text)}")


def _ask(question, default: Optional[str] = None, style: str = "cyan",
         choices: Optional[List[str]] = None) -> str:
    """A question answered with a line of text, stripped. Enter gives `default` (shown
    after the question, also when empty; None = nothing shown, Enter gives ""). With
    `choices`, an answer that is not one of them is asked again, on both screens."""
    if RICH_AVAILABLE and console:
        kw: Dict[str, Any] = {} if default is None else {"default": default}
        if choices is not None:
            kw["choices"] = choices
        return Prompt.ask(_rich_text(question, style), **kw).strip()
    shown = _plain(question) + (f" ({'/'.join(choices)})" if choices else "")
    prompt = f"{shown}: " if default is None else f"{shown} [{default}]: "
    while True:
        answer = input(prompt).strip()
        if not answer and default is not None:
            return default            # unchecked against the choices, as Rich does
        if choices is None or answer in choices:
            return answer             # Enter without a default: "" -- unless there are choices
        print("Please select one of the available options")


def _ask_yes_no(question, default: bool, style: str = "") -> bool:
    """A yes/no question; the words are _yes's on both screens."""
    if RICH_AVAILABLE and console:
        return Confirm.ask(_rich_text(question, style), default=default)
    return _yes(input(f"{_plain(question)} [{'Y/n' if default else 'y/N'}]: "), default)


def _table(columns: List[Dict], rows: List[list], panel: Optional[Dict] = None, **table_kw) -> None:
    """A table from one description, for both screens. `columns`: dicts with "name" and
    add_column's keywords (style, justify, width); a cell -- and a title -- is a text or a
    (text, style) pair; `table_kw` go to Rich's Table, `panel` (Panel's keywords) puts it in
    a panel. The plain screen prints the title and the columns aligned, two spaces apart.
    A title given as plain text stays Rich markup -- the wizard's own titles, no user text --
    so a Table title keeps Rich's own title style."""
    def text(x):
        if isinstance(x, tuple):
            return Text(str(x[0]), style=x[1] or None)
        return Text("" if x is None else str(x))

    def cell(x):
        return str(x[0]) if isinstance(x, tuple) else ("" if x is None else str(x))

    if RICH_AVAILABLE and console:
        kw = dict(table_kw)
        if isinstance(kw.get("title"), tuple):
            kw["title"] = text(kw["title"])
        table = Table(**kw)
        for c in columns:
            c = dict(c)
            table.add_column(c.pop("name", ""), **c)
        for row in rows:
            table.add_row(*(text(x) for x in row))
        if panel:
            pk = dict(panel)
            if isinstance(pk.get("title"), tuple):
                pk["title"] = text(pk["title"])
            console.print(Panel(table, **pk))
        else:
            console.print(table)
        return
    if panel and panel.get("title"):
        print(f"--- {cell(panel['title'])} ---")
    if table_kw.get("title"):
        print(cell(table_kw["title"]))
    grid = ([[c.get("name", "") for c in columns]] if table_kw.get("show_header", True) else [])
    grid += [[cell(x) for x in row] for row in rows]
    if not grid:
        return
    widths = [max(len(r[i]) for r in grid) for i in range(len(columns))]
    for r in grid:
        cells = []
        for i, value in enumerate(r):
            how = columns[i].get("justify", "left")
            cells.append(value.rjust(widths[i]) if how == "right" else
                         value.center(widths[i]) if how == "center" else value.ljust(widths[i]))
        print(" " + "  ".join(cells).rstrip())


def _panel(text, title: str, style: str = "") -> None:
    """`text` (a text or a list of parts) in a framed panel titled `title`; on the plain
    screen the title over the text."""
    if RICH_AVAILABLE and console:
        console.print(Panel(_highlighted(_rich_text(text)), title=title, border_style=style or "none"))
    else:
        print(f"--- {title} ---")
        print(_plain(text))


# --- Paths ---------------------------------------------------------

#: The package: this wizard, the three backends and what they share (v3.1 phase 9).
PACKAGE_DIR = Path(__file__).parent.resolve()
#: The installation: the entry point convert_tiff.py and the package. The settings overlay
#: (".tiff_workflow_settings.json", "next to the scripts") and manifests/ are here.
INSTALL_DIR = PACKAGE_DIR.parent
#: What runs a backend: the entry point, with the backend's subcommand.
ENTRY = INSTALL_DIR / "convert_tiff.py"

# What the three backends share; the wizard uses its helpers instead of carrying copies of
# them (v3.1 phase 5). The subcommands are the entry point's.
from . import common as tw  # noqa: E402
from .cli import SUBCOMMANDS  # noqa: E402


# --- Settings overlay ----------------------------------------------
#
# `.tiff_workflow_settings.json` changes the settings at the top of the scripts WITHOUT
# editing them (jxl-photo's overlay, same rules). Sections: compress_tiff_zip, copy_exif,
# generate_thumbnails, convert_tiff. Only the names in a script's OVERLAY_SPECS table (or
# _OVERLAY_SETTINGS below) are ever taken: nothing that
# deletes, overwrites or is chosen per run (mode, safe_mode, delete_source, ...). A value that
# does not pass its spec, an unreadable file, an unknown name: the script's own value stays and
# a warning says so; it never stops a run. A `-Param` on the command line always beats the file.

_SETTINGS_OVERLAY_FILE = tw.SETTINGS_OVERLAY_FILE
_SETTINGS_OVERLAY_ENV = tw.SETTINGS_OVERLAY_ENV

#: script file -> its section of the overlay file
_OVERLAY_SECTIONS = {
    "compress_tiff_zip.py": "compress_tiff_zip",
    "copy_exif_to_tiff.py": "copy_exif",
    "generate_thumbnails.py": "generate_thumbnails",
}


def _settings_overlay_path() -> Optional[Path]:
    """The overlay file to read (it may not exist), or None when TIFFWORKFLOW_SETTINGS is
    set but empty (overlay OFF): tw.settings_overlay_path, looking next to this script --
    the variable, then next to the scripts if the file is there, then the user profile."""
    path = tw.settings_overlay_path(str(INSTALL_DIR))
    return Path(path) if path is not None else None

# The overlay grammar is the backends' own (one reader: the wizard plans with the value the
# backend will run with).
_overlay_object_pairs = tw._overlay_object_pairs
_overlay_value = tw._overlay_value


def _settings_overlay_section(path: Optional[Path], section: str) -> Optional[dict]:
    """{name: value} of `section` of the overlay file at `path`; None when there is no path,
    no file or no such section. Raises ValueError/OSError for a file that cannot be read or
    whose root or section is not a JSON object."""
    if path is None or not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"),
                      object_pairs_hook=_overlay_object_pairs)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    sect = data.get(section)
    if sect is None:
        return None
    if not isinstance(sect, dict):
        raise ValueError(f"the '{section}' entry is not an object")
    return sect


def _parse_setting_text(spec: str, text: str):
    """What the user typed for a setting of this spec, as the JSON value _overlay_value then
    checks; raises ValueError for text that is not that kind at all. An empty answer (or
    none/null) for a `path?` is null."""
    kind = spec.partition(":")[0]
    t = text.strip()
    if kind == "path?" and (t == "" or t.lower() in ("none", "null")):
        return None
    if kind == "bool":
        if t.lower() in ("y", "yes", "true", "on", "1"):
            return True
        if t.lower() in ("n", "no", "false", "off", "0"):
            return False
        raise ValueError("answer yes or no")
    if kind in ("int", "intstr"):
        return int(t)
    if kind == "float":
        return float(t)
    return t


# --- Reading the backend scripts -----------------------------------
#
# The backends declare their parameters in a PARAMS table (name, kind, default, help) and
# what the overlay may change in an OVERLAY_SPECS table, both written with literals. They are
# read here with `ast`, never imported: reading a default must not run the script, and an
# edited default (a user's own setting) is read exactly as written.

@functools.lru_cache(maxsize=None)
def _backend_tables(script_name: str) -> Tuple[Tuple["tw.P", ...], Tuple[Tuple[str, str], ...]]:
    """(params, overlay specs) of a backend next to this file: params as tw.P in the script's
    order -- the very table the backend binds its command line with (it is written with
    literals only) --, specs as ((name, spec), ...). ((), ()) when the file cannot be read or
    parsed; an entry that does not make a tw.P is left out."""
    try:
        text = (PACKAGE_DIR / script_name).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return (), ()
    params: List["tw.P"] = []
    specs: List[Tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "PARAMS" in names and isinstance(node.value, (ast.Tuple, ast.List)):
            for call in node.value.elts:
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "P"):
                    continue
                try:
                    p = tw.P(*[ast.literal_eval(a) for a in call.args],
                             **{k.arg: ast.literal_eval(k.value) for k in call.keywords if k.arg})
                except (ValueError, SyntaxError, TypeError):
                    continue
                if isinstance(p.name, str) and isinstance(p.kind, str):
                    p.help = str(p.help or "")
                    params.append(p)
        elif "OVERLAY_SPECS" in names:
            try:
                table = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                table = {}
            if isinstance(table, dict):
                specs = [(str(k), str(v)) for k, v in table.items()]
    return tuple(params), tuple(specs)


def _backend_overlay_specs(script_name: str) -> Dict[str, str]:
    """{name: spec} of a backend's OVERLAY_SPECS table; {} when the file cannot be read or
    has none."""
    return dict(_backend_tables(script_name)[1])


def _overlay_scan(script_name: str):
    """(path, {name: checked value}, [warning, ...]) of the overlay for a backend. Never
    raises: an unreadable overlay is ([], no values, one warning)."""
    path = None
    try:
        path = _settings_overlay_path()
        section = _settings_overlay_section(path, _OVERLAY_SECTIONS[script_name])
    except Exception as e:
        return path, {}, [f"{path}: not read ({e}); no setting changed"]
    values: Dict[str, object] = {}
    warnings: List[str] = []
    specs = _backend_overlay_specs(script_name)
    for name, value in (section or {}).items():
        spec = specs.get(name)
        if spec is None:
            warnings.append(f"{name} is not a setting the overlay can change in "
                            f"{_OVERLAY_SECTIONS[script_name]}; ignored")
            continue
        ok, checked = _overlay_value(spec, value)
        if not ok:
            warnings.append(f"{name} = {json.dumps(value, ensure_ascii=False)} is not "
                            f"valid ({checked}); the script's value is kept")
            continue
        values[name] = checked
    return path, values, warnings


def _overlay_values(script_name: str) -> Dict[str, object]:
    """{name: checked value} the overlay gives a backend. No cache (a save from the Script
    settings menu counts at once). Never raises."""
    try:
        return _overlay_scan(script_name)[1]
    except Exception:
        return {}


#: What the overlay may change in this file's own settings: name -> spec. A setting that is
#: not here keeps the value written above.
_OVERLAY_SETTINGS = {
    "WORKER_MEMORY_FRACTION": "float:0:2",   # 0 = no cap; above 1 trusts the estimate (jxl-photo: 0-2)
    "WORKER_MEMORY_LIMIT": "choice:both|commit|physical",
    "DIAGNOSE_CACHE": "bool",
    "CONVERT_TIMEOUT_S": "int:1:",
    "COMPARE_TIMEOUT_S": "int:1:",
    "TIMEOUT_S_PER_MB": "float:0.01:",
}


def _apply_settings_overlay(namespace: dict) -> dict:
    """Apply the `convert_tiff` section to `namespace` (this module's globals(), right here:
    the settings above are already defined and nothing has read them yet), read by
    tw.read_settings_overlay with _OVERLAY_SETTINGS as its table. Returns {"path", "applied",
    "warnings"} for the start-up notice. Never raises."""
    overlay = tw.read_settings_overlay("convert_tiff", _OVERLAY_SETTINGS, str(INSTALL_DIR))
    namespace.update(overlay["values"])
    return {"path": Path(overlay["path"]) if overlay["path"] else None,
            "applied": list(overlay["values"]), "warnings": list(overlay["warnings"])}

_SETTINGS_OVERLAY = _apply_settings_overlay(globals())


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
    last_mode: Optional[int] = None
    # Per-machine default for --exclude-folders (';'-separated folder NAMES).
    # Lives in the local config, never hardcoded: a fresh install defaults to
    # empty so nobody silently skips folders they did not choose to skip.
    last_exclude_folders: Optional[str] = None
    # Journal of the last executed workflow (for "Repeat last workflow").
    # Stores each command line as [subcommand, *args] (_journal_line) -- repeating
    # runs those arguments under this Python with the backend next to this wizard,
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
    # The menu's "a new release is out" line (see _update_check_start): GitHub is asked at
    # most once a day, and only when this is true. Also off with the environment variable
    # TIFFWORKFLOW_NO_UPDATE_CHECK (any non-empty value). Edited in the config file.
    check_for_updates: bool = True
    update_checked_on: str = ""       # YYYY-MM-DD of the last answer GitHub gave
    update_latest_seen: str = ""      # the tag it answered


def _write_json_file(path: Path, data, prefix: str, ensure_ascii: bool = True) -> None:
    """`data` as the JSON file `path`, in UTF-8: a temp file in the same folder, then one
    replacing rename (tw.move_file_replacing). The file is whole at every moment -- a run
    killed mid-write leaves the old one. Raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=ensure_ascii)
            f.write("\n")
        tw.move_file_replacing(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_config_json(path: Path):
    """The JSON of the config file. Read as UTF-8 (with or without a BOM: what the wizard writes,
    and what an editor saves today); a file saved by hand in the ANSI code page still reads.
    Until the review of v3.1 (V3) it was read in the ANSI code page only, so a hand-edited UTF-8
    file with a Japanese name was "unreadable". Raises what reading or parsing raises."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode(locale.getpreferredencoding(False), errors="strict")
    return json.loads(text)


class ConfigManager:
    """Persistent JSON config for convert_tiff."""

    def __init__(self):
        self.config_path = self._get_config_path()
        self.config = ToolConfig()
        # Every write of the config file holds this lock: the release check answers from a
        # background thread (save_update_answer) while the menu thread may save too.
        self._save_lock = threading.Lock()
        # Release-notice state of this run (see _update_check_start): the poll function and
        # the (tag, page URL) of a newer release, when there is one.
        self.update_poll = None
        self.update_available = None
        # A config file that exists and could not be read: the first save keeps it aside before
        # writing over it (_keep_unreadable_config) -- it holds the presets.
        self._unreadable = False
        self._load_config()

    def _get_config_path(self) -> Path:
        return _config_base() / ".convert_tiff_config.json"

    def _load_config(self) -> None:
        if self.config_path.exists():
            try:
                data = _read_config_json(self.config_path)
                for k, v in data.items():
                    if hasattr(self.config, k):
                        setattr(self.config, k, v)
                self._sanitise_presets()
            except Exception as e:
                # audit round 16: a corrupt config was swallowed silently and the
                # wizard ran on defaults with no hint why the saved state vanished.
                self._unreadable = True
                print(f"WARNING: ignoring unreadable config file {self.config_path}: {e}",
                      file=sys.stderr)

    def _keep_unreadable_config(self) -> None:
        """Before the first save over a config file that could not be read: a copy of it next
        to it (`.convert_tiff_config.json.unreadable`, or with the time when that exists), so
        the presets in it are not lost to a save of the defaults. Once per run."""
        self._unreadable = False
        if not self.config_path.exists():
            return
        keep = self.config_path.with_name(self.config_path.name + ".unreadable")
        if keep.exists():
            keep = keep.with_name(f"{keep.name}.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(str(self.config_path), str(keep))
        print(f"WARNING: the unreadable config file was kept as {keep}", file=sys.stderr)

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
        """Write the whole config -- presets included -- through a temp file in its folder and
        one replacing rename, in UTF-8 (v3.1 phase 6, W-O9: it truncated the file and wrote it
        in place, so a run killed mid-write lost every preset)."""
        try:
            with self._save_lock:
                if self._unreadable:
                    self._keep_unreadable_config()
                _write_json_file(self.config_path, asdict(self.config), ".convert_tiff_config.")
        except Exception as e:
            # audit round 16: same story on save -- a config that silently never
            # persisted looked exactly like one that did.
            print(f"WARNING: could not save config file {self.config_path}: {e}",
                  file=sys.stderr)

    def save_update_answer(self, checked_on: str, tag: Optional[str]) -> None:
        """Keep a release lookup's answer, from any thread: set the two fields in memory,
        then rewrite ONLY those two keys in the file on disk (read, change, write through a
        temp file + os.replace, under the save lock). The background thread must never
        serialise the whole config while the menu thread changes it -- it would write a stale
        copy of everything else (presets, the journal). A file that exists but does not parse
        is left alone. Raises OSError when the file cannot be written; the caller decides."""
        with self._save_lock:
            self.config.update_checked_on = checked_on
            if tag:
                self.config.update_latest_seen = tag
            data: Dict = {}
            if self.config_path.exists():
                try:
                    data = _read_config_json(self.config_path)
                except Exception:
                    return
                if not isinstance(data, dict):
                    return
            data["update_checked_on"] = self.config.update_checked_on
            data["update_latest_seen"] = self.config.update_latest_seen
            _write_json_file(self.config_path, data, ".convert_tiff_config.")


# --- New-release notice -------------------------------------------------
# Model: jxl-photo's _update_notice family (v2.25.1), without the jxl-thumb part. GitHub is
# asked for the latest release with one anonymous request: a generic User-Agent, no token,
# nothing else sent. Off with check_for_updates = false in the config or the environment
# variable TIFFWORKFLOW_NO_UPDATE_CHECK. Only the interactive menu asks: --run-preset,
# --list-presets and --version never touch the network.

_UPDATE_OWNER = "rsilvabr"
_UPDATE_TIMEOUT = 3          # seconds: a slow or absent network stays silent
_UPDATE_ENV = "TIFFWORKFLOW_NO_UPDATE_CHECK"


def _update_check_off(cfg) -> bool:
    return (not cfg.config.check_for_updates) or bool(os.environ.get(_UPDATE_ENV))


def _update_repo() -> str:
    """The repository whose releases this copy follows: the Windows program (a frozen
    build) follows tiff-workflow-exe, the scripts follow tiff-workflow."""
    return "tiff-workflow-exe" if getattr(sys, "frozen", False) else "tiff-workflow"


def _version_key(text: Optional[str]):
    """(major, minor, patch, final) of "v2.7.4", "2.7.4_beta1" or "v2.7.4-exe1" (the exe's
    package revision is ignored: exe2 of the same release is not news here), or None. A
    beta sorts before its release."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(_beta\d+)?(?:-exe\d+)?$", (text or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1)


def _latest_release_tag(repo: str, timeout: float = _UPDATE_TIMEOUT) -> Optional[str]:
    """The tag of the repository's latest release (GitHub leaves pre-releases out of
    "latest"), or None on any failure: no network, a timeout, a rate limit, an answer that
    is not what it should be. Sends no personal data: a generic User-Agent, no token."""
    import urllib.request
    url = f"https://api.github.com/repos/{_UPDATE_OWNER}/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "tiff-workflow-update-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(65536).decode("utf-8"))
        tag = data.get("tag_name") if isinstance(data, dict) else None
        return tag if isinstance(tag, str) and _version_key(tag) else None
    except Exception:
        return None


def _kept_update_notice(cfg, checked_before: bool = False) -> Optional[Tuple[str, str]]:
    """(the menu line, the release page URL) from the answer kept in the config (no network),
    or None. checked_before: the answer came from an earlier run, so the line says when."""
    if _update_check_off(cfg):
        return None
    latest = cfg.config.update_latest_seen
    mine, theirs = _version_key(TOOLKIT_VERSION), _version_key(latest)
    if not mine or not theirs or theirs <= mine:
        return None
    url = f"https://github.com/{_UPDATE_OWNER}/{_update_repo()}/releases/latest"
    line = (f"A new release is out: {latest} (this copy is {TOOLKIT_VERSION}) -- "
            f"[U] in the menu opens its page.")
    if checked_before and cfg.config.update_checked_on:
        line += f" (Checked by an earlier run on {cfg.config.update_checked_on}.)"
    return (line, url)


def _update_check_start(cfg):
    """The daily release lookup without making the menu wait: GitHub is asked in a background
    thread, which KEEPS the answer itself (save_update_answer) as soon as it is in, so the
    next run shows it even when this one is closed first. A day counts only once GitHub
    answered: no network, a timeout or a rate limit leave the day unmarked and the next run
    asks again. Returns poll(wait=0.0) -> the notice or None: until the answer is in, the one
    an earlier run kept (with its date); after it, the fresh one. Off: no thread, no notice.
    Already asked today: no thread."""
    if _update_check_off(cfg):
        return lambda wait=0.0: None
    today = date.today().isoformat()
    if cfg.config.update_checked_on == today:
        notice = _kept_update_notice(cfg, checked_before=True)
        return lambda wait=0.0: notice
    fresh: Dict[str, bool] = {"answered": False}

    def _ask():
        tag = _latest_release_tag(_update_repo())
        if tag:
            try:
                cfg.save_update_answer(today, tag)
            except Exception:
                pass
            fresh["answered"] = True

    worker = threading.Thread(target=_ask, name="update-check", daemon=True)
    worker.start()

    def poll(wait: float = 0.0):
        if worker.is_alive():
            worker.join(wait)
        answered = not worker.is_alive() and fresh["answered"]
        return _kept_update_notice(cfg, checked_before=not answered)
    return poll


def _update_check_now(cfg) -> Tuple[str, Optional[Tuple[str, str]]]:
    """Menu 4: ask GitHub now (whatever was asked today) and keep the answer. Returns
    (the line to print, the notice or None)."""
    if _update_check_off(cfg):
        return ("Release check: off (check_for_updates in the config file, or "
                f"{_UPDATE_ENV}).", None)
    tag = _latest_release_tag(_update_repo())
    if not tag:
        return (f"Release check: GitHub did not answer (this copy is {TOOLKIT_VERSION}).", None)
    try:
        cfg.save_update_answer(date.today().isoformat(), tag)
    except Exception:
        pass
    notice = _kept_update_notice(cfg)
    if notice:
        return (notice[0], notice)
    return (f"Release check: up to date ({tag} is the latest release; this copy is "
            f"{TOOLKIT_VERSION}).", None)


# --- Dependency check ----------------------------------------------

#: The backends this wizard runs (in PACKAGE_DIR).
BACKEND_SCRIPTS = [
    "compress_tiff_zip.py",
    "generate_thumbnails.py",
    "copy_exif_to_tiff.py",
]
#: ...and the module the three of them import.
BACKEND_SHARED = "common.py"
#: The name of each backend on the entry point's command line and in a journal (v3.1, T8:
#: [subcommand, *args]) is SUBCOMMANDS, imported from tiff_workflow/cli.py above.

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
    missing = [s for s in BACKEND_SCRIPTS + [BACKEND_SHARED, "__init__.py"]
               if not (PACKAGE_DIR / s).exists()]
    results.append(("backend scripts", not missing,
                    "all present" if not missing else "missing: " + ", ".join(missing)))
    results.append(("rich", RICH_AVAILABLE,
                    "installed" if RICH_AVAILABLE else "plain-text UI (pip install rich)"))
    return results


def show_environment_panel(cfg) -> None:
    """Print the dependency status panel shown at startup (jxl_photo style)."""
    checks = check_dependencies(cfg)
    parts: List[object] = []
    for name, ok, _ in checks:
        if parts:
            parts.append(" | ")
        parts += [("[OK]", "green") if ok else ("[!!]", "red"), f" {name}"]
    _panel(parts, "TIFF Workflow Environment", "cyan")
    for name, ok, detail in checks:
        if not ok:
            _say(f"  {name}: {detail}", "yellow")


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
    # own settings (a literal copy here let an edited --zip-subfolder-name be walked into).
    exclude_names = _TOOL_OUTPUT_PARTS
    for path in root.rglob("*/"):
        if not path.is_dir():
            continue  # Python < 3.11: rglob("*/") also yields files
        if path.name.startswith("."):
            continue
        # Every folder level below the root counts, as in copy_exif's AutoFind: a session
        # INSIDE OLD_TIFFs (S5pro 01\OLD_TIFFs\S5pro bak\) came back as a session to process
        # when only the folder's own name was checked (v3.1 phase 6, W-O1).
        if tw.path_excluded(str(path), str(root), exclude_names):
            continue
        for pat in patterns:
            if pat.lower() in path.name.lower():
                tiffs = [f for f in path.glob("*.tif") if f.stat().st_size > 0] + \
                        [f for f in path.glob("*.tiff") if f.stat().st_size > 0]
                if tiffs:
                    if ";" in str(path):
                        # audit round 28: the folder list reaches the backends ';'-joined,
                        # so '2019;S5pro' was split and ANOTHER folder ('2019') compressed,
                        # one the screen never showed. step_folder refuses ';' too.
                        _say(f"  SKIPPED (';' in the path cannot be passed to the backends): "
                             f"{path}", "yellow")
                    else:
                        results[path] = len(tiffs)
                break
    # ONE order, the one the screen shows: folder name, then the full path for two folders
    # of one name (v3.1 phase 6, W-O13: the first run sorted by name, a preset re-sorted by
    # path, and the --input-dir order of the two differed).
    return dict(sorted(results.items(), key=lambda x: (x[0].name.lower(), str(x[0]).lower())))


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


#: The backends accept --workers from 1 to 64; anything outside that range is refused
#: when the command line is read and the backend never starts.
MAX_WORKERS = 64


def clamp_workers(value: int, default: Optional[int] = None) -> int:
    """Keep --workers inside the range the backends accept. A value that is no number gives
    `default`, or the config's default_workers (ToolConfig) -- never a second literal."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ToolConfig.default_workers if default is None else default
    if n < 1:
        return 1
    if n > MAX_WORKERS:
        return MAX_WORKERS
    return n


#: Shipped defaults of the backend parameters the wizard offers or passes. Used ONLY
#: when the script cannot be read or the parameter is missing -- the source of truth is
#: the PARAMS table of the script itself (tests pin these to it).
_BACKEND_PARAM_FALLBACKS = {
    ("compress_tiff_zip.py", "safe_mode"): True,
    ("compress_tiff_zip.py", "skip_lzw_as_compressed"): False,
    ("compress_tiff_zip.py", "cap_workers"): 0,
    ("compress_tiff_zip.py", "duplicate_action"): "Numbered",
    ("compress_tiff_zip.py", "thumb_size"): 256,
    ("compress_tiff_zip.py", "thumb_quality"): "85",
    ("compress_tiff_zip.py", "thumb_format"): "jpg",
    ("compress_tiff_zip.py", "zip_suffix"): "_ZIP",
    ("compress_tiff_zip.py", "zip_subfolder_name"): "ZIP",
    ("compress_tiff_zip.py", "export_zip_subfolder"): "ZIP",
    ("compress_tiff_zip.py", "export_marker"): "_EXPORT",
    ("copy_exif_to_tiff.py", "safe_mode"): True,
    ("generate_thumbnails.py", "size"): 256,
    ("generate_thumbnails.py", "quality"): "85",
    ("generate_thumbnails.py", "format"): "jpg",
    ("generate_thumbnails.py", "page"): "0",
}


def _backend_param_defaults(script_name: str) -> Dict[str, object]:
    """name -> default of every parameter of the script's PARAMS table that takes a value
    (a switch has none). {} when the file cannot be read."""
    return {p.name: p.default for p in _backend_tables(script_name)[0]
            if p.kind != "switch" and p.default is not None}


def _backend_param_names(script_name: str) -> Tuple[str, ...]:
    """Every parameter name of the script's PARAMS table, in order -- the switches too.
    () when the file cannot be read."""
    return tuple(p.name for p in _backend_tables(script_name)[0])


def _backend_param_help(script_name: str) -> Dict[str, str]:
    """name -> the `help` text of the parameter in the PARAMS table (the one-line help the
    Script settings screen shows); {} when the file cannot be read."""
    return {p.name: p.help for p in _backend_tables(script_name)[0] if p.help}


def _backend_default(script_name: str, param: str):
    """The default of `param` as written in `script_name`'s PARAMS table; the
    shipped value from _BACKEND_PARAM_FALLBACKS only when it cannot be read or has
    another type than the fallback (a typo must not reach a command line). A value the
    settings overlay gives that backend (it passed its spec) wins over the PARAMS table, as
    it does when the script runs -- but only with the fallback's type."""
    fallback = _BACKEND_PARAM_FALLBACKS[(script_name, param)]
    if script_name in _OVERLAY_SECTIONS:
        over = _overlay_values(script_name).get(param)
        if over is not None and type(over) is type(fallback):
            return over
    val = _backend_param_defaults(script_name).get(param)
    if val is None or type(val) is not type(fallback):
        return fallback
    return val


def _tool_output_names() -> set:
    """Lower-cased folder names that hold this toolkit's own output (discovery skips
    them below the scan root). The ZIP/_EXPORT names come from compress_tiff_zip.py's
    own settings: a copy here let an edited --zip-subfolder-name be walked into."""
    names = {"old_tiffs", "old_padded", "logs", "converted_zip"}
    for p in ("zip_subfolder_name", "export_zip_subfolder", "export_marker"):
        names.add(str(_backend_default("compress_tiff_zip.py", p)).lower())
    return names


#: Folder names that hold this toolkit's own output. Discovery skips them, but only BELOW
#: the folder the user pointed at -- see _is_tool_output_path.
_TOOL_OUTPUT_PARTS = _tool_output_names()


def _refresh_tool_output_parts() -> None:
    """Recompute _TOOL_OUTPUT_PARTS (the Script settings menu calls it after a save: a changed
    zip_subfolder_name / export_marker / export_zip_subfolder must be skipped by discovery too)."""
    global _TOOL_OUTPUT_PARTS
    _TOOL_OUTPUT_PARTS = _tool_output_names()


def _is_tool_output_path(path: Path, root: Path) -> bool:
    """True when `path` sits inside one of this toolkit's own output folders BELOW `root`.

    Evaluated RELATIVE to the scan root, never against the absolute path. Matching the
    absolute path also matched every ancestor: a library under `D:\\Backup\\zip\\2019\\`
    reported "No TIFF files found" and exited 0, and pointing a workflow straight AT a
    folder named `ZIP` or `OLD_TIFFs` could never work. That is the same bug the
    backends fixed in audit round 14 (path_excluded); the diagnose workflow kept the
    absolute form, while the manifest generator already did it relative.

    A path that is not under the root at all falls back to the whole path -- in doubt, skip.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return any(part.lower() in _TOOL_OUTPUT_PARTS for part in rel.parts)


# Captured at import. The wizard's tests replace subprocess.run (the module is shared with
# tiff_workflow.common, whose run_captured the wizard's _run_captured calls).
_CompletedProcess = subprocess.CompletedProcess


def _run_captured(cmd, timeout, text=False, input=None):
    """subprocess.run with stdout/stderr captured through temp FILES.

    capture_output=True makes communicate() start two reader threads per call on
    Windows. Under memory exhaustion a thread can fail in its bootstrap
    (MemoryError) before it signals "started", and Thread.start() then waits
    forever -- before the timeout is even armed (jxl-photo, 2026-10-04: one
    worker hung 60 min). With files there is no thread: a failed spawn raises
    OSError, which the callers already handle. It is tw.run_captured (v3.1: one runner
    for the wizard and the backends); this adapter gives the wizard's callers a
    CompletedProcess. text=True decodes as UTF-8 (what magick and exiftool print), where
    subprocess's text mode used the console code page."""
    rc, o, e = tw.run_captured(cmd, timeout=timeout, input=input)
    if text:
        if isinstance(o, bytes):
            o = o.decode("utf-8", "replace").replace("\r\n", "\n")
        if isinstance(e, bytes):
            e = e.decode("utf-8", "replace").replace("\r\n", "\n")
    return _CompletedProcess(cmd, rc, o, e)


def _run_exiftool(args, timeout, text=False):
    """exiftool with its arguments in a UTF-8 argfile (`-charset filename=utf8`).

    On Windows exiftool reads its command line in the ANSI code page, so a path with
    characters outside it -- `G:\\2019\\190815_Film_伊豆沼_夏\\...` -- arrived mangled
    and could not be opened. Every write gate then failed closed: the padded conversion
    refused every file under a Japanese folder name (`subfile_restore_failed`). An
    argfile is read as UTF-8 and passes any name through; it is how _image_pixels has
    always called exiftool.

    The argfile goes on STDIN (`-@ -`), not as a temp file (audit round 27): its PATH on
    the command line is read in the ANSI code page too, and a %TEMP% outside it failed."""
    data = ("\n".join(["-charset", "filename=utf8"] + [str(a) for a in args]) + "\n").encode("utf-8")
    return _run_captured(["exiftool", "-@", "-"], timeout, text=text, input=data)


# Memory helpers copied from jxl-photo's jxl_recompressor.py (v2.9.0); keep in step.

def _available_commit_bytes():
    """Memory the system can still commit, in bytes, or None if unknown
    (tw.memory_readings)."""
    return tw.memory_readings()[1]


def _available_physical_bytes():
    """Physical RAM still available (free + standby cache), in bytes, or None if unknown
    (tw.memory_readings)."""
    return tw.memory_readings()[0]

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


def _max_image_pixels(paths) -> int:
    """Largest IFD0 width*height among `paths` (see _image_pixels)."""
    return max(_image_pixels(paths).values(), default=0)


def _image_pixels(paths) -> Dict[str, int]:
    """{normcased absolute path: IFD0 width*height} for `paths`, read in batches of 400
    by exiftool (-j). A file exiftool does not report, or reports without a size,
    counts as tw.UNKNOWN_IMAGE_PIXELS -- an unknown size must never shrink the estimate."""
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
        try:
            # The argfile goes on stdin (`-@ -`), see _run_exiftool (audit round 27).
            data = ("\n".join(["-charset", "filename=utf8", "-j", "-n",
                               "-IFD0:ImageWidth", "-IFD0:ImageHeight"] + chunk) + "\n").encode("utf-8")
            r = _run_captured(["exiftool", "-@", "-"], _work_timeout(60), text=True, input=data)
            for entry in json.loads(r.stdout or "[]"):
                try:
                    px = int(entry.get("ImageWidth") or 0) * int(entry.get("ImageHeight") or 0)
                except (TypeError, ValueError):
                    px = 0
                seen[os.path.normcase(os.path.abspath(str(entry.get("SourceFile", ""))))] = px
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        for p in chunk:
            key = os.path.normcase(os.path.abspath(p))
            px = seen.get(key, 0)
            sizes[key] = px if px > 0 else tw.UNKNOWN_IMAGE_PIXELS
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


@contextlib.contextmanager
def _timeout_load(workers: int):
    """While a pool of `workers` runs, every size-scaled timeout counts them (they share the
    disk). Not reentrant: it goes back to 1, not to what it was -- no pool runs inside
    another."""
    global _TIMEOUT_LOAD
    _TIMEOUT_LOAD = workers
    try:
        yield
    finally:
        _TIMEOUT_LOAD = 1


def _run_pool(items: list, work, workers: int, on_done, error_result, verb: str) -> None:
    """`work(item)` for every item on `workers` threads (Diagnose's check, the padded
    conversion). Each result is handed to `on_done(n, result)` on THIS thread as soon as it
    is in (n counts from 1); a work that raised is replaced by `error_result(item, exc)`.
    A progress line every 25 items. On Ctrl+C the items not started yet are cancelled
    instead of all being run first."""
    total = len(items)
    t_start = time.time()
    with _timeout_load(workers):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {executor.submit(work, item): item for item in items}
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    result = future.result()
                except Exception as e:
                    result = error_result(futures[future], e)
                on_done(done, result)
                if done % 25 == 0 and done < total:
                    spent = time.time() - t_start
                    _say(f"  {done}/{total} {verb} in {_fmt_duration(spent)} -- about "
                         f"{_fmt_duration(spent / done * (total - done))} left", "cyan")
        except BaseException:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)


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

    Same rule as magick_path_spec in tiff_workflow/common.py. ImageMagick reads
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
    returns and are never touched. OUTPUT specs never come here: an output does not exist
    yet, so no alias can be made and the escape made magick write elsewhere -- with a '[ ]'
    in the temp folder every Diagnose and padded file failed (v3.1 phase 6, W-O2). An output
    goes through tw.magick_path_spec ('%' only); magick writes a '[ ]' folder literally.
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
    backup_tiff_metadata does in the backends. The source FILE OPERAND is not
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
        # audit round 28: the container does not carry IPTC:CodedCharacterSet unless it is
        # copied by name; without it UTF-8 IPTC text (伊豆沼) was stored as Latin '???'.
        # A redirect copies it only when the source has one (no IPTC block is invented).
        r = _run_exiftool(["-q", "-q", "-all:all",
                           "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet",
                           "-o", tmp, str(path)],
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
    """Move a file onto `dst`, replacing it, the way the backends land a file
    (tw.move_file_into_place): on one volume ONE rename that replaces `dst` (there is never a
    moment with nothing at `dst`); from another volume a copy next to `dst`, size-checked,
    then that rename; `src` goes only after. Raises OSError when it cannot, with `src` and
    `dst` as they were. Never shutil.move: on a locked or read-only file it turned into a copy
    and a delete (v3.1 phase 6, W-O8).

    A destination that is a directory is an error, not something to delete -- the old
    shutil.rmtree(dst) would recursively erase a whole folder that merely shared the filename.
    """
    if dst.exists() and dst.is_dir():
        raise IsADirectoryError(f"refusing to replace directory with file: {dst}")
    error = tw.move_file_into_place(str(src), str(dst), tw.new_run_id())
    if error:
        raise OSError(f"{error}: {src} -> {dst}")

# --- AutoFind: Pattern Selection ----------------------------------

def step_pattern(cfg: "ConfigManager") -> Optional[List[str]]:
    """Choose AutoFind pattern (S5, S3, Both, Custom)."""
    _say("\nAuto-Find Pattern", "bold cyan")
    _say(["  [1] ", ("S5 Pro", "bold"), " folders -- matches 'S5pro' in folder name"])
    _say(["  [2] ", ("S3 Pro", "bold"), " folders -- matches 'S3pro' in folder name"])
    _say(["  [3] ", ("Both", "bold"), "          -- matches 'S5pro' and 'S3pro'"])
    _say(["  [4] ", ("Custom", "bold"), "         -- type any pattern"])
    choice = _ask("Choice", "1", style="", choices=["1", "2", "3", "4"])

    patterns_map = {
        "1": ["S5pro"],
        "2": ["S3pro"],
        "3": ["S5pro", "S3pro"],
    }
    if choice in patterns_map:
        return patterns_map[choice]
    if choice == "4":
        pat = _ask("Custom pattern (case-insensitive, partial match)", style="")
        if not pat:
            return None
        return [pat]
    return None


# --- AutoFind: Scan and Preview ------------------------------------

def step_autofind(cfg: "ConfigManager", patterns: List[str], root: Path) -> Optional[List[Path]]:
    """Scan folders matching pattern, let user confirm."""
    _say(["\n", ("Scanning for folders matching:", "cyan"), f" {', '.join(patterns)}"])

    found = find_folders_by_pattern(root, patterns)
    if not found:
        _say("No matching folders found.", "yellow")
        return None

    _table([{"name": "#", "justify": "right", "style": "dim", "width": 4},
            {"name": "Folder", "style": "green"},
            {"name": "TIFFs", "justify": "center", "style": "yellow", "width": 6},
            {"name": "Path", "style": "dim"}],
           [[str(i), folder.name, str(count), truncate_path(folder, 40)]
            for i, (folder, count) in enumerate(found.items(), 1)],
           panel={"border_style": "green"}, title=f"Found {len(found)} session(s)",
           header_style="bold cyan")

    total_tiffs = sum(found.values())
    if not _ask_yes_no(f"Proceed with all {len(found)} session(s) ({total_tiffs} TIFFs)?", True, "green"):
        return None

    # Verify folders still exist before returning
    existing = [p for p in found.keys() if p.exists()]
    if len(existing) != len(found):
        _say(f"Note: {len(found) - len(existing)} folder(s) were removed during selection.", "yellow")
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


def step_mode(cfg: "ConfigManager") -> Optional[int]:
    """Select mode 0-9 for Free Compress."""
    _say("\nStep 2: Organization Mode (Free Compress)", "bold cyan")
    for m, name in MODE_NAMES.items():
        _say([f"[{m}] ", (name, "bold red" if m == 8 else "bold green")])
        _say(f"    {MODE_DESCS[m]}\n")
    choice = _ask("Select mode", str(cfg.config.last_mode or 0), style="",
                  choices=[str(m) for m in MODE_NAMES])

    try:
        mode = int(choice)
        if 0 <= mode <= 9:
            return mode
    except ValueError:
        pass
    return None


# --- Folder Selection ----------------------------------------------

def step_folder(cfg: "ConfigManager", prompt_text: str = "Input folder") -> Optional[Path]:
    """Choose input folder."""
    default = cfg.config.last_input_dir or str(Path.cwd())
    folder = _ask(prompt_text, default)
    # Strip surrounding quotes from pasted/dragged paths
    if len(folder) >= 2 and folder[0] == folder[-1] and folder[0] in ("'", '"'):
        folder = folder[1:-1]
    p = Path(folder)
    if not p.exists():
        _error(f"Folder not found: {folder}")
        return None
    if not p.is_dir():
        _error(f"Not a directory: {folder}")
        return None
    # ';' breaks the multi-folder contract (paths are joined/split on ';')
    if ";" in str(p):
        _error(f"Folder path contains ';' which is not supported: {p}")
        return None
    cfg.config.last_input_dir = os.path.abspath(str(p))
    # audit round 28: ABSOLUTE, never the text as typed. A relative answer ("photos") went
    # into the preset journal as --input-dir photos, and --run-preset from another folder --
    # the Task Scheduler starts in System32 -- compressed a different folder. abspath, not
    # resolve(): a mapped or SUBST drive stays the letter the user typed.
    return Path(os.path.abspath(p))


# --- Basic Parameters ---------------------------------------------

def _supports_cap_workers(workflow: Dict) -> bool:
    """True only for workflows whose backend is compress_tiff_zip.py.

    The Copy EXIF backend has no --cap-workers parameter (it takes --workers directly),
    so asking the question there produced an answer nothing could act on. --exclude-folders
    is the opposite call: it now exists on all three backends and is emitted for all of them.
    """
    return workflow.get("origin") in ("free_compress", "compress", "both")


def _read_workers(text: str, cfg: "ConfigManager") -> int:
    """The Workers answer, the same on both screens (v3.1 phase 6, T4): Enter or a text that is
    no number = the value shown ([last used or the config's default]); a number is kept inside
    the range the backends accept, and a clamp is said."""
    shown = cfg.config.last_workers or cfg.config.default_workers
    try:
        raw = int(text) if text.strip() else shown
    except ValueError:
        _say(f"Not a number: {text!r} -- using {shown}.", "yellow")
        raw = shown
    workers = clamp_workers(raw, cfg.config.default_workers)
    if workers != raw:
        _say(f"Workers clamped to {workers} (backend accepts 1-{MAX_WORKERS}).", "yellow")
    return workers


def _staging_default(cfg: "ConfigManager") -> str:
    """The staging folder Enter keeps: the last one used, else the config's staging_dir."""
    return cfg.config.last_staging or cfg.config.staging_dir or ""


def _read_staging(text: str, cfg: "ConfigManager") -> str:
    """The Staging answer, the same on both screens (T4): Enter keeps the shown folder, '-'
    (or none) turns staging off. The Rich screen had no way to turn a remembered one off; the
    plain screen forgot it on Enter."""
    t = text.strip()
    if t.lower() in ("-", "none"):
        return ""
    return t or _staging_default(cfg)


def step_basic_params(cfg: "ConfigManager", workflow: Dict) -> bool:
    """Workers, Dry-run, Staging -- then the per-run Advanced options screen, here so that no
    workflow can ask the first without the second."""
    # The backend this run will actually launch: each one's PARAMS defaults are the
    # source of the prompt defaults below (audit round 17).
    _ref = "compress_tiff_zip.py" if _supports_cap_workers(workflow) else "copy_exif_to_tiff.py"
    safe_default = _backend_default(_ref, "safe_mode")
    skip_default = (_backend_default("compress_tiff_zip.py", "skip_lzw_as_compressed")
                    if _ref == "compress_tiff_zip.py" else False)
    cap_default = _backend_default("compress_tiff_zip.py", "cap_workers")
    workflow["workers"] = _read_workers(
        _ask("Workers", str(cfg.config.last_workers or cfg.config.default_workers)), cfg)
    workflow["staging"] = _read_staging(
        _ask("Staging folder (SSD for faster I/O; '-' = none)", _staging_default(cfg) or "-"), cfg)
    cfg.config.last_staging = workflow["staging"]

    workflow["dry_run"] = _ask_yes_no("Dry-run mode?", False)

    # Safe mode and Skip LZW
    workflow["safe_mode"] = _ask_yes_no([("Safe mode?", "cyan"), " (skip multi-page TIFFs)"], safe_default)
    workflow["skip_lzw"] = _ask_yes_no([("Skip LZW as already compressed?", "cyan"),
                                        " (LZW files will be ignored)"], skip_default)

    # --cap-workers only exists on compress_tiff_zip.py; asking it for a Copy EXIF
    # workflow produced an answer that was silently dropped by the command builder.
    if _supports_cap_workers(workflow):
        cap_str = _ask([("Cap workers?", "cyan"), " (0 = no cap)"], str(cap_default), style="")
        try:
            workflow["cap_workers"] = max(0, min(int(cap_str), MAX_WORKERS))
        except ValueError:
            _say(f"Invalid cap '{cap_str}', using {cap_default}", "yellow")
            workflow["cap_workers"] = cap_default

    excl_default = cfg.config.last_exclude_folders or ""
    excl = _ask([("Exclude folders?", "cyan"), " (';'-separated folder names, '-' = none)"],
                excl_default or "-", style="")
    if excl in ("-", "", "none"):
        workflow["exclude_folders"] = ""
        cfg.config.last_exclude_folders = None
    else:
        workflow["exclude_folders"] = excl
        cfg.config.last_exclude_folders = excl

    # The backends work on several files at once; --force-sequential turns that off.
    if _ask_yes_no("Force sequential? (override parallelism)", False, "yellow"):
        workflow["force_sequential"] = True

    step_advanced_options(cfg, workflow)
    return True


# --- Advanced options (per run) -------------------------------------

def _advanced_backends(workflow: Dict) -> List[str]:
    """The REGISTRY backends this run will launch (the Advanced screen lists their
    level-"advanced" options): compress for every compress origin, copy_exif for Copy EXIF
    and the combined workflow."""
    backends: List[str] = []
    if _supports_cap_workers(workflow):
        backends.append("compress")
    if workflow.get("origin") in ("copy_exif", "both"):
        backends.append("copy_exif")
    return backends


def _backend_script(cfg: "ConfigManager", backend: str) -> str:
    return "compress_tiff_zip.py" if backend == "compress" else "copy_exif_to_tiff.py"


def _advanced_current(cfg: "ConfigManager", workflow: Dict, backend: str, opt: "Opt"):
    """(value, where) an Advanced option has right now: what this run edited, else what the
    settings file gives the backend, else the script's own default."""
    mine = _advanced_value(workflow, backend, opt.param)
    if mine is not None:
        return mine, "this run"
    script = _backend_script(cfg, backend)
    over = _overlay_values(script).get(opt.param)
    if over is not None:
        return over, "settings file"
    return _backend_param_defaults(script).get(opt.param), "script"


def _advanced_shown(value, opt: "Opt") -> str:
    if opt.value_spec == "bool":
        return "on" if value else "off"
    if value is None or value == "":
        return "(auto)" if opt.value_spec == "path?" else "-"
    return str(value)


def step_advanced_options(cfg: "ConfigManager", workflow: Dict) -> bool:
    """Per-run Advanced options: the backend parameters no other step asks (timeouts, memory
    share, ...). Default No -- then nothing is stored and the run is exactly what it was. Only
    an option the user EDITS is stored, in workflow["advanced"][backend][param], and it is then
    passed explicitly even when it equals the default (AGENTS.md, round 17); an option that
    was not edited is not passed. Each value is checked with the overlay's spec grammar."""
    backends = _advanced_backends(workflow)
    rows = [(b, o) for b in backends for o in REGISTRY[b] if o.level == "advanced"]
    if not rows:
        return True
    wanted = _ask_yes_no([("Advanced options?", "cyan"), " (timeouts, memory share, ...)"], False)
    if not wanted:
        return True
    while True:
        _say("Advanced options (only the ones you change are passed to the script):")
        for n, (backend, opt) in enumerate(rows, 1):
            value, where = _advanced_current(cfg, workflow, backend, opt)
            _say(f"  {n}. [{backend}] {opt.label} ({tw.cli_name(opt.param)}) = "
                     f"{_advanced_shown(value, opt)}  ({where})")
            _say(f"       {opt.help}")
        pick = _ask("Number to change (0 = done)", "0")
        if pick in ("", "0"):
            return True
        if not pick.isdigit() or not 1 <= int(pick) <= len(rows):
            _say(f"No option {pick!r}.")
            continue
        backend, opt = rows[int(pick) - 1]
        expected = _overlay_value(opt.value_spec, object())[1]
        text = _ask(f"New value for {tw.cli_name(opt.param)} ({expected}; '-' = leave it to "
                        f"the script; empty = no change)", "")
        if text == "":
            continue
        adv = workflow.get("advanced")
        if text == "-":
            if isinstance(adv, dict) and isinstance(adv.get(backend), dict):
                adv[backend].pop(opt.param, None)
                if not adv[backend]:
                    adv.pop(backend)
                if not adv:
                    workflow.pop("advanced")
            continue
        try:
            parsed = _parse_setting_text(opt.value_spec, text)
        except ValueError as e:
            _say(f"Not changed: {e}.")
            continue
        ok, checked = _overlay_value(opt.value_spec, parsed)
        if not ok:
            _say(f"Not changed: {tw.cli_name(opt.param)} must be {checked}.")
            continue
        if not isinstance(adv, dict):
            adv = workflow["advanced"] = {}
        if not isinstance(adv.get(backend), dict):
            adv[backend] = {}
        adv[backend][opt.param] = checked


# --- Summary Panel ------------------------------------------------

def step_confirm(workflow: Dict, cfg: "ConfigManager") -> bool:
    """Show summary and confirm."""
    origin = workflow.get("origin", "?")
    mode = workflow.get("mode", "?")
    dry = "Yes" if workflow.get("dry_run") else "No"
    folders_count = len(workflow.get("folders", []))

    rows: List[list] = []
    if origin in ("copy_exif",):
        rows.append(["Workflow:", f"Copy EXIF ({origin})"])
    elif origin == "free_compress":
        rows.append(["Workflow:", "Compress TIFFs"])
    elif origin in ("compress", "both"):
        rows.append(["Workflow:", f"Fuji: Compress ({origin})"])

    if origin != "free_compress" and folders_count > 1:
        pattern_val = workflow.get("pattern", [])
        pattern_str = ", ".join(pattern_val) if isinstance(pattern_val, list) else str(pattern_val)
        rows.append(["Pattern:", pattern_str or "?"])
        rows.append(["Sessions:", f"{folders_count} folder(s)"])
    elif origin == "free_compress":
        rows.append(["Mode:", f"{mode} - {MODE_NAMES.get(mode, '?')}"])
        rows.append(["Folder:", str(workflow.get("input_dir", "?"))[:60]])
    else:
        rows.append(["Folder:", str(workflow.get("folders", [workflow.get("input_dir")])[0].name
                                    if workflow.get("folders") else workflow.get("input_dir", "?"))])

    rows.append(["Workers:", str(workflow.get("workers", cfg.config.default_workers))])
    if workflow.get("cap_workers"):
        rows.append(["Cap workers:", str(workflow["cap_workers"])])
    if workflow.get("exclude_folders"):
        rows.append([("Excluding:", "yellow"), workflow["exclude_folders"]])
    rows.append(["Staging:", str(workflow.get("staging") or "disabled")])
    rows.append(["Dry-run:", dry])
    if workflow.get("force_parallel"):
        rows.append([("Parallelism:", "yellow"), "FORCED ON (--force-parallel)"])
    elif workflow.get("force_sequential"):
        rows.append([("Parallelism:", "yellow"), "FORCED OFF (--force-sequential)"])

    if origin == "free_compress" and mode == 8:
        rows.append([("Delete source:", "red"), "ON -- originals will be DELETED"])

    _table([{"style": "bold cyan"}, {}], rows,
           panel={"title": ("Summary", "bold"), "border_style": "green"},
           box=None, show_header=False, pad_edge=False)
    if not _ask_yes_no("Proceed?", True, "yellow"):
        _say("Cancelled.", "dim")
        return False

    return True


# --- Command Builders ----------------------------------------------

#: backend script -> its subcommand
_SUBCOMMAND_OF = {name: sub for sub, name in SUBCOMMANDS.items()}


def _backend_command(script_name: str, args: List[str]) -> List[str]:
    """The command line that runs a backend of this installation: this Python, the entry
    point convert_tiff.py, the backend's subcommand, its arguments (v3.1 phase 9: the
    backends live in the package). In the Windows program `sys.executable` is
    tiff-workflow.exe, whose launcher runs convert_tiff.py the same way."""
    return [sys.executable, str(ENTRY), _SUBCOMMAND_OF[script_name]] + [str(a) for a in args]


def _command_backend(cmd) -> Optional[str]:
    """The subcommand a command line of _backend_command runs; None for any other."""
    cmd = [str(a) for a in cmd]
    if len(cmd) > 2 and Path(cmd[1]).name == ENTRY.name and cmd[2] in SUBCOMMANDS:
        return cmd[2]
    return None


def _journal_line(cmd: List[str]) -> List[str]:
    """A backend command line as a journal keeps it: [subcommand, *args] (v3.1, T8).
    Neither the Python nor the folder is kept: a repeat runs the backend next to the wizard
    that repeats it (round 24: a preset saved from another copy ran that copy's backend).
    Anything but a command line of _backend_command is a programming error."""
    sub = _command_backend(cmd)
    if sub is None:
        raise ValueError(f"not a backend command line: {cmd!r}")
    return [sub] + [str(a) for a in cmd[3:]]


@dataclass
class JournalStep:
    """One saved step, as the backend will read it."""
    script: str                 # the backend's file name, next to this wizard
    args: List[str]
    values: Dict[str, object]   # what the backend binds `args` to (tw.parse_args)


def _journal_step(line) -> Optional[JournalStep]:
    """The step a journal line [subcommand, *args] runs; None when it is not one this
    version runs -- never a guess, and the caller refuses the whole journal:
    - another shape: what v3.0.0 saved ([<python>, <folder>/<backend>.py, -Name value]) and
      the PowerShell edition before it (pwsh -File <script>.ps1, powershell -Command);
    - an argument the backend would refuse, read by the backend's own table and reader;
    - a backend whose table cannot be read."""
    if not isinstance(line, list) or not line or line[0] not in SUBCOMMANDS:
        return None
    script = SUBCOMMANDS[line[0]]
    args = [str(a) for a in line[1:]]
    params = _backend_tables(script)[0]
    if not params:
        return None
    try:
        values, _given = tw.parse_args(args, params)
    except tw.UsageError:
        return None
    return JournalStep(script, args, values)


def _step_replaces_sources(step: JournalStep) -> bool:
    """True when a saved step deletes or replaces its source files: --delete-source, or
    compress mode 8 (which puts the ZIP over each .tif it read -- the manifest has always
    counted a mode-8 row as deleting). Read from what the backend binds (audit round 28:
    the refusal once looked for one exact spelling, and an edited preset got past it)."""
    return bool(step.values.get("delete_source")) or step.values.get("mode") == 8


def _option_at(args: List[str], param: str) -> Optional[Tuple[int, int, str]]:
    """Where the option `param` is in `args`, as (index, words, value): `--input-dir X` is
    two words, `--input-dir=X` one. The last one, which is the one a backend binds; None
    when there is none."""
    name = tw.cli_name(param)
    found = None
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            found = (i, 2, args[i + 1])
        elif a.startswith(name + "="):
            found = (i, 1, a[len(name) + 1:])
    return found


# --- Option Registry -----------------------------------------------
#
# Every parameter of every backend is described once, here; ONE generic builder
# (_emit_opt / _emit_all) turns a workflow dict into the argv. The tuple ORDER is the order the
# old hand-written builders emitted, so a command line is byte-identical for the same workflow
# (tests/test_wizard_registry.py, the golden file). A parameter that is not in REGISTRY must be
# in NOT_OFFERED with the reason -- the coverage test fails on a new backend parameter nobody
# decided about.

@dataclass(frozen=True)
class Opt:
    param: str           # the backend parameter, without the dash
    key: str             # slot in the workflow dict; "advanced.<param>" for the Advanced screen's
    kind: str            # how _emit_opt turns the value into argv (see _emit_opt)
    group: str           # "run", "files", "safety", "thumbnail", "performance"
    level: str           # "asked" (a wizard step asks it), "advanced" (the Advanced screen),
                         # "overlay" (settings file only), "internal" (set by the workflow)
    label: str
    help: str
    value_spec: str = ""  # advanced only: the overlay spec syntax (_overlay_value)
    when: Optional[Callable[[Dict], bool]] = None
    danger: bool = False  # a setting that deletes or overwrites: never from the overlay


def _thumb_on(workflow: Dict) -> bool:
    return bool(workflow.get("generate_thumbnail"))


def _advanced_value(workflow: Dict, backend: str, param: str):
    """The Advanced screen's value for `param`, or None. A malformed `advanced` entry (a
    hand-edited preset) is None, never an error."""
    adv = workflow.get("advanced")
    per_backend = adv.get(backend) if isinstance(adv, dict) else None
    return per_backend.get(param) if isinstance(per_backend, dict) else None


def _emit_opt(opt: Opt, workflow: Dict, folders: Optional[List[Path]] = None,
              backend: str = "") -> List[str]:
    """The argv words `opt` contributes for `workflow`, [] when it contributes none.

    kind: input (--input-dir from `folders`: none / one / ';'-joined) | value (when truthy) |
    notnone | int_notnone | workers (clamped to the backend's range, when truthy) | flag
    (--x, when truthy) | true_flag (when == True) | bool (--x / --no-x, when the value is a
    bool: passed even when it equals the default, audit round 17) | true_bool (--x, when
    truthy) | none (overlay-only or internal: nothing).

    An "advanced.<param>" value lives in workflow["advanced"][backend]; it is checked against
    its value_spec again here and DROPPED when it does not pass (a preset edited by hand must
    not put garbage on a command line: the script's own value then applies)."""
    if opt.when is not None and not opt.when(workflow):
        return []
    p = opt.param
    kind = opt.kind
    if kind == "none":
        return []
    if kind == "input":
        if folders and len(folders) == 1:
            return tw.cli_value(p, folders[0])
        if folders and len(folders) > 1:
            # Pass as semicolon-separated list, the script handles each
            return tw.cli_value(p, ";".join(str(f) for f in folders))
        return []
    if opt.key.startswith("advanced."):
        v = _advanced_value(workflow, backend, opt.key[len("advanced."):])
        # audit round 28: an EDITED path? option left empty (stored as "") means "auto-detect
        # for this run" -- the screen says so -- and must reach the backend as --x "" (bound,
        # so it also beats the settings file). It was dropped (a falsy "value", and "" fails
        # the spec check below), and the backend used the settings file's profile instead.
        if v == "" and opt.value_spec == "path?" and kind == "value":
            return tw.cli_value(p, "")
        if v is not None and opt.value_spec:
            ok, v = _overlay_value(opt.value_spec, v)
            if not ok:
                return []
    else:
        v = workflow.get(opt.key)
    if kind == "value":
        return tw.cli_value(p, v) if v else []
    if kind == "notnone":
        return tw.cli_value(p, v) if v is not None else []
    if kind == "int_notnone":
        return tw.cli_value(p, int(v)) if v is not None else []
    if kind == "workers":
        return tw.cli_value(p, clamp_workers(v)) if v else []
    if kind == "flag":
        return [tw.cli_switch(p, True)] if v else []
    if kind == "true_flag":
        return [tw.cli_switch(p, True)] if v == True else []  # noqa: E712 -- 1 == True is the old behaviour
    if kind == "bool":
        return [tw.cli_switch(p, v)] if isinstance(v, bool) else []
    if kind == "true_bool":
        return [tw.cli_switch(p, True)] if v else []
    raise ValueError(f"unknown option kind {kind!r} for {tw.cli_name(opt.param)}")


def _emit_all(backend: str, workflow: Dict, folders: Optional[List[Path]] = None) -> List[str]:
    """The whole argv tail of `backend` ("compress" or "copy_exif"), in REGISTRY order."""
    argv: List[str] = []
    for opt in REGISTRY[backend]:
        argv += _emit_opt(opt, workflow, folders, backend)
    return argv


_ADV_MAGICK = Opt("magick_timeout", "advanced.magick_timeout", "notnone", "performance", "advanced",
                  "ImageMagick timeout (seconds)",
                  "How long one magick call may run before it is killed. Raise it for very "
                  "large scans on a slow disk.", value_spec="int:1:")
_ADV_MEMORY = Opt("worker_memory_fraction", "advanced.worker_memory_fraction", "notnone",
                  "performance", "advanced", "Memory share for the workers (0-2)",
                  "Caps the parallel workers so their ImageMagick processes fit in this share "
                  "of the free memory; 0 = no cap; above 1 trusts the estimate.", value_spec="float:0:2")

REGISTRY: Dict[str, Tuple[Opt, ...]] = {
    "compress": (
        Opt("mode", "mode", "notnone", "run", "internal", "Mode", "The compress mode 0-9."),
        Opt("input_dir", "folders", "input", "run", "asked", "Input folder(s)",
            "Where the TIFFs are; several folders are passed ';'-joined."),
        # Mode 2 (flat) writes into the input root when --output-dir is missing, which mixes
        # the flattened copies with the sources and overwrites root-level TIFFs in place.
        Opt("output_dir", "output_dir", "value", "files", "asked", "Output folder",
            "Where mode 2 (flat) and the export modes write."),
        Opt("duplicate_action", "duplicate_action", "value", "files", "asked", "Duplicate action",
            "What to do when the output file already exists: Skip, Numbered or Overwrite."),
        Opt("staging_dir", "staging", "value", "performance", "asked", "Staging folder",
            "A fast (SSD) folder the work is done in before the result lands."),
        Opt("workers", "workers", "workers", "performance", "asked", "Workers",
            "Parallel workers (1-64)."),
        Opt("cap_workers", "cap_workers", "int_notnone", "performance", "asked", "Cap workers",
            "Clamp the parallel workers to this value; 0 = no cap."),
        Opt("exclude_folders", "exclude_folders", "value", "files", "asked", "Exclude folders",
            "';'-separated folder NAMES to skip during discovery."),
        Opt("dry_run", "dry_run", "flag", "run", "asked", "Dry-run", "Plan and report; change nothing."),
        # Passed explicitly whenever the wizard decided it -- even when it equals the
        # default: omitting it let a backend whose own default was edited run with a
        # different value than the summary showed (audit round 17, the jxl-photo #467 class).
        Opt("safe_mode", "safe_mode", "bool", "safety", "asked", "Safe mode",
            "Skip multi-page TIFFs.", danger=True),
        Opt("skip_lzw_as_compressed", "skip_lzw", "bool", "files", "asked", "Skip LZW",
            "Treat LZW files as already compressed and ignore them."),
        Opt("overwrite", "overwrite", "flag", "safety", "internal", "Overwrite",
            "Replace an existing output.", danger=True),
        Opt("delete_source", "delete_source", "flag", "safety", "asked", "Delete source",
            "Mode 8: delete the source after the output is verified.", danger=True),
        Opt("force_parallel", "force_parallel", "true_flag", "performance", "internal",
            "Force parallel", "Kept for presets saved by v2.x; parallel is the default."),
        Opt("force_sequential", "force_sequential", "true_flag", "performance", "asked",
            "Force sequential", "One file at a time."),
        Opt("generate_thumbnail", "generate_thumbnail", "flag", "thumbnail", "asked",
            "Generate thumbnail", "Embed an sRGB thumbnail page in the TIFF."),
        Opt("thumb_size", "thumb_size", "value", "thumbnail", "asked", "Thumbnail size",
            "Pixels.", when=_thumb_on),
        Opt("thumb_quality", "thumb_quality", "value", "thumbnail", "asked", "Thumbnail quality",
            "JPEG quality 1-100.", when=_thumb_on),
        Opt("thumb_format", "thumb_format", "value", "thumbnail", "asked", "Thumbnail format",
            "jpg, png, ...", when=_thumb_on),
        Opt("thumb_page", "thumb_page", "value", "thumbnail", "internal", "Thumbnail page",
            "Page position of the embedded thumbnail (>= 1).", when=_thumb_on),
        Opt("skip_compressed_with_thumb", "skip_compressed_with_thumb", "flag", "thumbnail",
            "asked", "Skip compressed with thumbnail",
            "Skip TIFFs that are already compressed AND have a thumbnail.", when=_thumb_on),
        # Advanced screen (per run, passed explicitly when edited):
        _ADV_MAGICK,
        Opt("srgb_profile", "advanced.srgb_profile", "value", "thumbnail", "advanced",
            "sRGB ICC profile", "ICC file used to convert wide-gamut sources; empty = "
            "auto-detect.", value_spec="path?"),
        _ADV_MEMORY,
        # Settings file only (decision 6: the discovery must know the folder names, so they
        # are not per-run):
        Opt("zip_suffix", "", "none", "files", "overlay", "ZIP suffix", "Mode 4 folder suffix."),
        Opt("zip_subfolder_name", "", "none", "files", "overlay", "ZIP subfolder name",
            "Modes 1/3 subfolder name."),
        Opt("export_marker", "", "none", "files", "overlay", "Export marker",
            "Modes 6/7 export folder marker."),
        Opt("export_zip_subfolder", "", "none", "files", "overlay", "Export ZIP subfolder",
            "Mode 6 subfolder inside the export folder."),
        Opt("export_tiff_subfolder", "", "none", "files", "overlay", "Export TIFF subfolder",
            "Mode 7 TIFF subfolder inside the export folder."),
    ),
    "copy_exif": (
        Opt("input_dir", "folders", "input", "run", "asked", "Input folder(s)",
            "The Fuji session folders (';'-joined)."),
        Opt("workers", "workers", "workers", "performance", "asked", "Workers",
            "Parallel workers (1-64)."),
        Opt("staging_dir", "staging", "value", "performance", "asked", "Staging folder",
            "A fast (SSD) folder the work is done in before the result lands."),
        Opt("output_dir", "output_dir", "value", "files", "asked", "Output folder",
            "Where the result is written."),
        Opt("dry_run", "dry_run", "flag", "run", "asked", "Dry-run", "Plan and report; change nothing."),
        Opt("skip_if_tiff_has_exif", "skip_exif", "flag", "files", "internal", "Skip if TIFF has EXIF",
            "Leave a TIFF that already carries EXIF alone."),
        Opt("compress_zip", "compress_zip", "flag", "run", "internal", "Compress to ZIP",
            "Compress the TIFF in the same pass."),
        Opt("safe_mode", "safe_mode", "bool", "safety", "asked", "Safe mode",
            "Skip multi-page TIFFs.", danger=True),
        # --skip-lzw-as-compressed is a switch in the Copy EXIF backend (a bool in
        # compress_tiff_zip.py): its default is OFF by type, so only "yes" is passed.
        Opt("skip_lzw_as_compressed", "skip_lzw", "true_bool", "files", "asked", "Skip LZW",
            "Treat LZW files as already compressed and ignore them."),
        Opt("overwrite", "overwrite", "flag", "safety", "internal", "Overwrite",
            "Replace an existing output.", danger=True),
        # step_basic_params asks this for every workflow. It used to be dropped here, so in
        # workflow 4 the exclusion applied to the Compress step and silently not to the
        # Copy EXIF step of the same run.
        Opt("exclude_folders", "exclude_folders", "value", "files", "asked", "Exclude folders",
            "';'-separated folder NAMES to skip during discovery."),
        _ADV_MAGICK,
        _ADV_MEMORY,
        Opt("fail_on_warn", "advanced.fail_on_warn", "flag", "run", "advanced", "Fail on warning",
            "Exit 1 when a file only produced a warning or was missing its JPEG.",
            value_spec="bool"),
    ),
    "thumbnails": (
        # Registered for the coverage test only: run_generate_thumbnails builds its two
        # command lines inline (not through the workflow dict).
        Opt("input_dir", "", "none", "run", "asked", "Input folder", "Where the TIFFs are."),
        Opt("size", "", "none", "thumbnail", "asked", "Size", "Pixels."),
        Opt("quality", "", "none", "thumbnail", "asked", "Quality", "JPEG quality 1-100."),
        Opt("format", "", "none", "thumbnail", "asked", "Format", "jpg, png, ..."),
        Opt("page", "", "none", "thumbnail", "asked", "Page", "0 = first, or all."),
        Opt("workers", "", "none", "performance", "asked", "Workers", "Parallel workers."),
        Opt("output_dir", "", "none", "files", "asked", "Output folder",
            "Empty = next to each TIFF."),
        Opt("exclude_folders", "", "none", "files", "asked", "Exclude folders",
            "';'-separated folder NAMES to skip."),
        Opt("srgb_profile", "", "none", "thumbnail", "overlay", "sRGB ICC profile",
            "ICC file used to convert wide-gamut sources; empty = auto-detect."),
    ),
}

#: Parameters the wizard deliberately never offers: {backend: {param: why}}.
NOT_OFFERED: Dict[str, Dict[str, str]] = {
    "compress": {
        "input_dir_file": "run_subprocess spills -InputDir here",
        "restore_stranded": "own flow (_offer_restore_stranded and menu option 5)",
    },
    "copy_exif": {
        "input_dir_file": "run_subprocess spills -InputDir here",
        "auto_find": "the wizard runs its own AutoFind",
        "folder_pattern": "the wizard runs its own AutoFind",
        "icc_policy": "no documented values; the backend only captures it",
    },
    "thumbnails": {
        "remove": "own flow, the remove branch of run_generate_thumbnails",
        "recursive": "asked inline by run_generate_thumbnails, not through the workflow dict",
        "dry_run": "asked inline by run_generate_thumbnails, not through the workflow dict",
    },
}

#: backend key -> the script(s) whose PARAMS tables the coverage test compares with it
REGISTRY_SCRIPTS = {
    "compress": ("compress_tiff_zip.py",),
    "copy_exif": ("copy_exif_to_tiff.py",),
    "thumbnails": ("generate_thumbnails.py",),
}


def build_compress_command(workflow: Dict, folders: Optional[List[Path]] = None) -> List[str]:
    """The command line for compress_tiff_zip.py (the argv comes from REGISTRY)."""
    return _backend_command("compress_tiff_zip.py", _emit_all("compress", workflow, folders))


def build_copy_exif_command(workflow: Dict, folders: Optional[List[Path]] = None,
                            extra_flags: Optional[List[str]] = None) -> List[str]:
    """The command line for copy_exif_to_tiff.py (the argv comes from REGISTRY)."""
    return _backend_command("copy_exif_to_tiff.py",
                            _emit_all("copy_exif", workflow, folders) + list(extra_flags or []))


# --- Subprocess Runner ----------------------------------------------

def _decode_console_line(raw: bytes) -> str:
    """Decode subprocess output: UTF-8 (what the backends write to a pipe), else the
    console OEM codepage (e.g. cp850: a tool that printed on its own)."""
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
# every run, so its ';'-joined --input-dir grows with every session (round 22).
_CMDLINE_SPILL_CHARS = 24000
_INPUTDIR_FILE_BACKENDS = ("compress_tiff_zip.py", "copy_exif_to_tiff.py")


def _spill_long_input_dir(cmd: List[str]) -> Tuple[List[str], Optional[str]]:
    """Swap a too-long --input-dir for --input-dir-file <temp file> (one folder per line).

    Returns (command, temp path or None). The caller deletes the temp file once the
    process has ended. Commands that are short enough, or that do not run one of
    _INPUTDIR_FILE_BACKENDS, or that carry no --input-dir, come back unchanged."""
    cmd = [str(a) for a in cmd]
    if len(subprocess.list2cmdline(cmd)) <= _CMDLINE_SPILL_CHARS:
        return cmd, None
    sub = _command_backend(cmd)
    if sub is None or SUBCOMMANDS[sub] not in _INPUTDIR_FILE_BACKENDS:
        return cmd, None
    args = cmd[3:]
    at = _option_at(args, "input_dir")
    if at is None:
        return cmd, None
    i, words, value = at
    folders = [f for f in value.split(";") if f.strip()]
    fd, list_path = tempfile.mkstemp(prefix="tw_inputdirs_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(folders) + "\n")
    new_args = args[:i] + tw.cli_value("input_dir_file", list_path) + args[i + words:]
    return cmd[:3] + new_args, list_path


# compress_tiff_zip.py's end-of-run line when originals are still stranded in OLD_TIFFs/
# (audit round 25: find_stranded_originals). The wizard offers --restore-stranded on it.
_STRANDED_LEFT_RE = re.compile(r"STRANDED: (\d+) original\(s\) still in OLD_TIFFs")


def _with_restore_stranded(cmd: List[str]) -> List[str]:
    """`cmd` plus --restore-stranded."""
    return list(cmd) + [tw.cli_switch("restore_stranded", True)]


def _offer_restore_stranded(cmd: List[str], stranded: int, stdin) -> Optional[int]:
    """After a compress run that left originals stranded in OLD_TIFFs/ (nothing in their
    place: an earlier run was interrupted), offer to run it again with --restore-stranded,
    which moves them back and compresses them. Returns that run's exit code, or None when
    nothing was run. Never asks on an unattended run (closed stdin, a preset) or a dry
    run: it says what to do instead."""
    if stranded <= 0 or _command_backend(cmd) != "compress":
        return None
    if tw.cli_switch("restore_stranded", True) in [str(c) for c in cmd]:
        return None
    msg = (f"{stranded} original(s) are in OLD_TIFFs with nothing in their place -- left by an "
           f"interrupted run. They are the only copy of those images.")
    interactive = stdin is None and sys.stdin is not None and sys.stdin.isatty()
    dry = tw.cli_switch("dry_run", True) in [str(c) for c in cmd]
    if not interactive or dry:
        hint = (" Run the same compress again with --restore-stranded (or answer yes to this "
                "question in an interactive, non-dry run) to put them back.")
        _say(msg + hint, "yellow")
        return None
    _say(f"\n{msg}", "yellow")
    if not _ask_yes_no("Put them back and compress them now?", True):
        return None
    return run_subprocess(_with_restore_stranded(cmd), stdin=stdin)


def run_subprocess(cmd: List[str], timeout: Optional[int] = None,
                   stdin=None) -> int:
    """Run command, stream output with Rich coloring. Has configurable timeout.

    `stdin` is passed through to Popen when not None (the unattended preset
    path closes it with DEVNULL, so no prompt can wait on the console)."""

    original_cmd = list(cmd)
    cmd, _spill_path = _spill_long_input_dir(cmd)
    stranded_left = [0]

    popen_kwargs: Dict[str, Any] = {
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
            _say(f"ERROR: Failed to start process: {e}", "red")
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
                m = _STRANDED_LEFT_RE.search(line)
                if m:
                    stranded_left[0] = int(m.group(1))
                _say(["  ", (line, "green" if " OK " in line or "+ZIP" in line else
                                 "red" if " | ERROR |" in line or " ERROR " in line else
                                 "yellow" if " | WARN |" in line or "WARNING" in line else
                                 "blue" if "DRY" in line else "")])

        try:
            while True:
                _drain_lines()

                if stop_reader.is_set() and process.poll() is not None:
                    _drain_lines()  # final drain: the reader may queue the last lines after our check
                    break

                if timeout is not None and time.time() - start_time > timeout:
                    _kill_tree()
                    _say(f"ERROR: Process timed out after {timeout}s", "red")
                    return -1

                time.sleep(0.05)
        except KeyboardInterrupt:
            _kill_tree()
            _say("Interrupted by user.", "yellow")
            return -1
        except Exception as e:
            _kill_tree()
            _say(f"ERROR: {e}", "red")
            return -1

        try:
            if process.stdout is not None:      # a PIPE: always there
                process.stdout.close()
        except (OSError, ValueError):
            pass

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree()
            return -1

        if process.returncode != 0:
            _say(f"WARNING: Process exited with code {process.returncode}", "red")
        restored_rc = _offer_restore_stranded(original_cmd, stranded_left[0], stdin)
        return process.returncode if restored_rc is None else restored_rc
    finally:
        if _spill_path:
            try:
                os.unlink(_spill_path)
            except OSError:
                pass


# --- Undo OLD_TIFFs ------------------------------------------------

_TIMESTAMPED_BACKUP_RE = re.compile(r"^(?P<base>.+)_\d{8}_\d{6}(?:_\d+)?$")


def _restore_stranded_only(stranded) -> bool:
    """Move each stranded backup to its target, never over a file; two backups claiming
    one target (an original and its timestamped namesake) are both left alone."""
    claims = Counter(str(t).lower() for _, t in stranded)
    moved = kept = failed = 0
    for f, target in stranded:
        if claims[str(target).lower()] > 1:
            kept += 1
            line = f"  KEPT (more than one backup claims {target.name}; move the right one by hand): {f}"
        elif target.exists():
            kept += 1
            line = f"  KEPT ({target.name} appeared meanwhile): {f}"
        else:
            try:
                _safe_move(f, target)
                moved += 1
                line = f"  RESTORED: {target}"
            except Exception as e:  # one unmovable file must not stop the rest
                failed += 1
                line = f"  FAILED: {f.name} -- {e}"
        _say(line, "green" if line.startswith("  RESTORED") else
             ("red" if line.startswith("  FAILED") else "yellow"))
    summary = f"Done. Restored {moved} stranded original(s)" + \
              (f", kept {kept}" if kept else "") + (f", FAILED {failed}" if failed else "") + "."
    _say(f"\n{summary}", "red" if failed else "green")
    return failed == 0


#: the backup folder names the backends write and read (compress's TOOL_OUTPUT_NAMES)
_BACKUP_DIR_NAMES = ("OLD_TIFFs", "OLD_TIFF")


def _stranded_backups(root) -> list:
    """(backup, target) Paths of the originals below `root` left in OLD_TIFFs with nothing in
    their place: tw.find_stranded_originals, the backend's own rule (round 25, round 28's
    timestamp rule) -- one rule since v3.1 (W-O5). It never walks INTO a backup folder (a
    nested one is a backup of a backup), accepts only a FILE in the original's place, and
    reads past 260 characters. Target = base stem + the backup's extension."""
    found = tw.find_stranded_originals(str(root), True, _BACKUP_DIR_NAMES, [])
    return [(Path(x["backup"]), Path(x["target"])) for x in found]


def _move_compressed_twin_aside(restored: Path) -> int:
    """After Restore put an x.tiff original back: the compressed x.tif that modes 0/9 wrote
    from it sits next to it (a .tiff source is written as .tif). It is moved into the ZIP
    subfolder of that folder (compress's zip_subfolder_name -- where mode 1 puts compressed
    copies; every tool skips it as output, none takes it for a backup), never deleted, never
    onto a file there (a taken name is numbered). Returns 1 when moved (v3.1 phase 6,
    W-O17, the owner's choice: the folder used to keep both, without a word). The original
    is back BEFORE this runs: a move that fails leaves both side by side, said."""
    twin = restored.with_suffix(".tif")
    if not twin.is_file() or tw.same_path(str(twin), str(restored)):
        return 0
    zip_dir = restored.parent / str(_backend_default("compress_tiff_zip.py", "zip_subfolder_name"))
    target = zip_dir / twin.name
    if target.exists():
        _name, cand = tw.numbered_name(str(zip_dir), twin.stem, twin.suffix,
                                       lambda n, p: os.path.exists(p))
        target = Path(cand)
    try:
        zip_dir.mkdir(exist_ok=True)
        _safe_move(twin, target)
    except Exception as e:
        _say(f"  KEPT BESIDE IT ({e}): {twin.name} -- the "
             f"compressed copy of {restored.name} could not be moved to {zip_dir.name}", "yellow")
        return 0
    _say(f"  MOVED ASIDE: {twin.name} -> {zip_dir.name}{os.sep}{target.name} "
         f"(the compressed copy of the restored {restored.name})", "dim")
    return 1


def run_undo_old_tiffs(cfg: "ConfigManager") -> bool:
    """Move TIFFs from OLD_TIFFs/ back to parent folder -- all of them, or (round 26) only
    the stranded ones: originals with nothing left in their place."""
    folder = step_folder(cfg, "Root folder to scan for OLD_TIFFs")
    if folder is None:
        return False

    # Find all OLD_TIFFs folders recursively
    # The backup folders of the backends (OLD_TIFFs, OLD_TIFF), never one nested in another
    # (a backup of a backup: restoring it would put files into a backup folder).
    names = {n.lower() for n in _BACKUP_DIR_NAMES}
    old_dirs = sorted([d for d in folder.rglob("*") if d.is_dir() and d.name.lower() in names
                       and not tw.path_excluded(str(d.parent), str(folder), _BACKUP_DIR_NAMES)])
    if not old_dirs:
        _say("No OLD_TIFFs folders found.", "yellow")
        return True

    # Count only what the move loop below will actually touch: counting every file made the
    # header promise more than the run delivered whenever sidecars sat in OLD_TIFFs/.
    def _restorable(od) -> list:
        return [f for f in Path(od).glob("*")
                if f.is_file() and tw.is_tiff_name(f.name)]

    counts = {od: len(_restorable(od)) for od in old_dirs}
    total_files = sum(counts.values())

    _say(f"\nFound {len(old_dirs)} OLD_TIFFs folder(s) with {total_files} TIFF(s)", "cyan")
    for od in old_dirs:
        _say(f"  {counts[od]:>4} TIFFs: {od}")

    # Round 26: the usual reason to come here is an interrupted mode 0/9 run. Restoring
    # EVERYTHING also brings back the originals of files that were compressed fine (and,
    # with overwrite, puts them over the compressed copies); restoring only the stranded
    # ones -- nothing in their place -- is what such a run needs.
    stranded = _stranded_backups(folder)
    if stranded:
        msg = (f"{len(stranded)} of them have NOTHING in their place (left by an interrupted "
               f"run): restore only those?")
        if _ask_yes_no(f"\n{msg}", True, "yellow"):
            return _restore_stranded_only(stranded)

    overwrite = _ask_yes_no("\nOverwrite existing files in parent folder?", False, "yellow")
    if not overwrite:
        _say("Will skip files that already exist in parent folder.", "dim")

    if not _ask_yes_no("\nMove all files back to parent folder?", True, "yellow"):
        _say("Cancelled.", "dim")
        return False

    # Move files
    moved = 0
    skipped = 0
    failed = 0
    aside = 0

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
            _say(f"  FAILED: {src.name} -- {e}", "red")
            return False
        moved += 1
        _say(f"  {label}: {src.name}", "green")
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
            if not tw.is_tiff_name(f.name):
                continue
            dest = parent / f.name
            if dest.exists():
                if overwrite:
                    _try_move(f, dest, "OVERWRITE")
                else:
                    skipped += 1
                    _say(f"  SKIP (exists): {f.name}", "yellow")
            else:
                if _try_move(f, dest, "MOVED") and f.suffix.lower() == ".tiff":
                    aside += _move_compressed_twin_aside(dest)

    # Ask to delete empty OLD_TIFFs folders
    def _remove_empty_dirs():
        for od in old_dirs:
            try:
                if any(Path(od).glob("*")):
                    continue
                Path(od).rmdir()
            except OSError as e:
                _say(f"  Could not remove {od}: {e}", "yellow")
                continue
            _say(f"  Removed: {od}", "green")

    if _ask_yes_no("\nDelete empty OLD_TIFFs folders?", False, "cyan"):
        _remove_empty_dirs()

    parts = [f"Moved {moved} file(s)"]
    if skipped > 0:
        parts.append(f"skipped {skipped}")
    if failed > 0:
        parts.append(f"FAILED {failed}")
    summary = "Done. " + ", ".join(parts) + "."
    _say(f"\n{summary}", "red" if failed else "green")

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

    One `identify` call, same format string the backends' page classifiers use. None means
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
        a magick ZIP recompress byte-for-byte),
      - audit round 28: every XMP and IPTC value (identity). The gate read EXIF only, so a
        kept file that had lost its keywords and captions -- or held them as '???' after a
        trip through the MIE container -- still purged the last good copy. The only XMP
        a rewrite by this toolkit changes is xmpMM:History (one entry appended; checked on
        real mode-9 rewrites of Lightroom and Capture One exports), so those keys are
        left out and nothing else is,
      - audit round 28: the per-page sample layout (_tiff_sample_layout): a neutral RGB
        page magick collapsed to one gray channel is pixel-identical through magick's own
        decode, so the RMSE compare passed it.
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

        r = _run_exiftool(
            ["-a", "-G1", "-s", "-j", "-XMP:all", "-IPTC:all", str(path)],
            timeout, text=True,
        )
        if r.returncode != 0:
            return None
        rows = json.loads(r.stdout or "[]")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        # XMP-x:XMPToolkit: a source without XMP gets a packet when the History entry is
        # written, and its header names the writer (exiftool).
        xmp_iptc = {k: v for k, v in rows[0].items()
                    if k not in ("SourceFile", "XMP-x:XMPToolkit")
                    and not k.startswith("XMP-xmpMM:History")}

        layout = _tiff_sample_layout(path)
        if layout is None:
            return None
    except Exception:
        return None
    return {"subfile": subfile, "exif_pages": exif_pages,
            "exif_other": exif_other, "icc": icc_hash,
            "xmp_iptc": xmp_iptc, "layout": layout}


def _compare_output(result) -> str:
    """What a finished `magick compare` printed: the metric goes to stderr."""
    return result.stdout.strip() if result.stdout else result.stderr.strip() if result.stderr else ""


# magick prints a large RMSE in scientific notation ("1.23457e+06 (0.0188)"); an older
# pattern read "06" as the RMSE.
_RMSE_RE = re.compile(r"([\d.]+(?:[eE][+-]?\d+)?)\s*\(([\d.eE+-]+)\)")


def _rmse_value(output: str) -> Tuple[Optional[float], str]:
    """(the absolute RMSE in `magick compare -metric RMSE` output, the output without magick's
    noise); the RMSE is None when the output holds no number. Used by both gates -- the purge's
    page compare and the Diagnose round trip -- so a fix to the reading reaches both."""
    output = _strip_magick_noise(output).strip()
    match = _RMSE_RE.search(output)
    return (float(match.group(1)) if match else None), output


@_scoped_magick_aliases
def _tiff_dimensions(path):
    """(width, height) of page 0; (None, None) when it cannot be read."""
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


@_scoped_magick_aliases
def _pages_identical(old_path: Path, new_path: Path, pairs) -> tuple[bool, str]:
    for old_page, new_page in pairs:
        try:
            result = _run_captured(
                ["magick", "compare", "-metric", "RMSE",
                 f"{_magick_spec(old_path)}[{old_page}]", f"{_magick_spec(new_path)}[{new_page}]", "null:"],
                _work_timeout(COMPARE_TIMEOUT_S, old_path, new_path), text=True
            )
            output = _compare_output(result)
            if result.returncode not in (0, 1):
                return False, f"compare failed on page {old_page}: {output}"
            rmse, output = _rmse_value(output)
            if rmse is None:
                return False, f"parse failed on page {old_page}: '{output}'"
            if rmse != 0.0:
                return False, f"PIXEL_DIFF page {old_page} RMSE={rmse}"

        except subprocess.TimeoutExpired:
            return False, f"compare timeout on page {old_page}"
        except FileNotFoundError:
            return False, "ImageMagick not found (magick command missing)"
        except Exception as e:
            return False, f"compare error on page {old_page}: {e}"
    return True, ""


def _metadata_matches(old_path: Path, new_path: Path, pairs) -> tuple[bool, str]:
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
    old_x, new_x = old_meta.get("xmp_iptc"), new_meta.get("xmp_iptc")
    if old_x is None or new_x is None:
        return False, "XMP/IPTC unreadable -- refusing purge"
    if old_x != new_x:
        diff = sorted(k for k in set(old_x) | set(new_x) if old_x.get(k) != new_x.get(k))
        return False, f"XMP_IPTC_MISMATCH ({', '.join(diff[:5])}{' ...' if len(diff) > 5 else ''})"
    layout_diff = _sample_layout_diff(old_meta.get("layout"), new_meta.get("layout"),
                                      pages=list(pairs))
    if layout_diff:
        return False, f"SAMPLE_LAYOUT_MISMATCH ({layout_diff})"
    return True, ""


@_scoped_magick_aliases
def _compare_tiff_metadata(old_path: Path, new_path: Path) -> tuple[bool, str]:
    """
    Compare two TIFFs pixel-by-pixel using RMSE.
    Returns (match, details).
    - If RMSE = 0 (within tolerance), images are pixel-identical
    - If dimensions differ, returns False immediately
    - If RMSE > 0, images differ
    """

    old_w, old_h = _tiff_dimensions(old_path)
    new_w, new_h = _tiff_dimensions(new_path)

    if old_w is None or new_w is None:
        return False, "magick identify failed"

    if old_w != new_w or old_h != new_h:
        return False, f"DIMENSION_MISMATCH {old_w}x{old_h} vs {new_w}x{new_h}"

    # Page-count check: RMSE below only compares page [0], so a lost extra page must block the
    # purge. This gate authorises permanent deletion, so an unreadable count fails CLOSED --
    # skipping the check on None let a page-losing conversion pass on nothing but page-0 RMSE.
    old_pages = _tiff_page_count(old_path)
    new_pages = _tiff_page_count(new_path)
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
    if old_pages == new_pages:
        strict_ok, strict_detail = _pages_identical(old_path, new_path, [(i, i) for i in range(old_pages)])
        if strict_ok:
            meta_ok, meta_detail = _metadata_matches(old_path, new_path, [(i, i) for i in range(old_pages)])
            if not meta_ok:
                return False, meta_detail
            return True, f"IDENTICAL ({old_w}x{old_h}, {old_pages} page(s))"
    else:
        strict_ok, strict_detail = False, f"PAGE_COUNT_MISMATCH {old_pages} vs {new_pages}"

    # One difference is EXPECTED and safe: --generate-thumbnail adds a thumbnail page, or
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

    img_ok, _img_detail = _pages_identical(old_path, new_path, list(zip(old_img, new_img)))
    if not img_ok:
        return False, strict_detail

    meta_ok, meta_detail = _metadata_matches(old_path, new_path, list(zip(old_img, new_img)))
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
def _is_real_16bit(tiff_path: Path, temp_dir: Optional[Path] = None, compress_tmp: str = "none") -> tuple[bool, float, str]:
    """
    Check if a TIFF is real 16-bit or padded 8-bit (stretched to 16).
    Method: Convert to 8-bit and back to 16-bit. If RMSE=0, it's padded 8-bit.
    Uses temp_dir for temporary files if provided.
    """

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

        t0 = time.time()
        try:
            # [0] on every step: multi-page TIFFs (Capture One main+thumbnail, scanner
            # RGB+IR) made the whole-file compare return garbage RMSE (~27%) even for
            # pixel-identical data, so padded files were misclassified as real 16-bit.
            result = _run_captured(
                ["magick", f"{_magick_spec(tiff_path)}[0]", "-depth", "8"] + (compress_8.split() if compress_8 else []) + [tw.magick_path_spec(tmp8)],
                _work_timeout(CONVERT_TIMEOUT_S, tiff_path)
            )
        except FileNotFoundError:
            return False, 0.0, "ERROR: ImageMagick not found"
        t1 = time.time()
        if result.returncode != 0:
            return False, 0.0, "ERROR: 8-bit conversion failed"

        try:
            result = _run_captured(
                ["magick", f"{_magick_spec(tmp8)}[0]", "-depth", "16"] + (compress_16.split() if compress_16 else []) + [tw.magick_path_spec(tmp16)],
                _work_timeout(CONVERT_TIMEOUT_S, tiff_path)
            )
        except FileNotFoundError:
            return False, 0.0, "ERROR: ImageMagick not found"
        t2 = time.time()
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
        t3 = time.time()

        if DEBUG_TIMING:
            sz8 = os.path.getsize(tmp8) if tmp8.exists() else 0
            sz16 = os.path.getsize(tmp16) if tmp16.exists() else 0
            print(f"[DEBUG] {tiff_path.name} | 8bit:{(t1-t0):.2f}s({sz8//1024}KB) 16bit:{(t2-t1):.2f}s({sz16//1024}KB) compare:{(t3-t2):.2f}s")

        output = _compare_output(result)
        if result.returncode not in (0, 1):
            return False, 0.0, f"ERROR: compare failed (exit={result.returncode}): {output}"
        rmse, output = _rmse_value(output)
        if rmse is None:
            return False, 0.0, f"ERROR: RMSE parse failed: '{output}'"

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
    found: List[Path] = []
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
                if not tw.is_tiff_name(name):
                    continue
                if any(fnmatch.fnmatchcase(low, p) for p in patterns):
                    continue
                found.append(Path(dirpath) / name)
    _say(f"  Found {len(found)} TIFF(s) in {folders} folder(s) ({time.time() - t0:.0f}s)", "dim")
    return sorted(found)


def _diagnose_status(is_real: bool, detail: str) -> str:
    """"padded", "real", "other" (not 16-bit) or "error"."""
    # audit round 28: matched on the verdict text _is_real_16bit writes, never by substring:
    # an error detail can quote the PATH, and an unreadable file in a folder named
    # "...padded..." was listed (and offered for conversion) as PADDED.
    if not is_real and (detail or "").startswith("padded 8-bit"):
        return "padded"
    if is_real:
        return "real"
    if re.fullmatch(r"\d+-bit \(not 16-bit\)", detail or ""):
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


@dataclass(frozen=True)
class DiagnoseResult:
    """One file's verdict in Diagnose: checked now (_diagnose_pool) or read from the cache
    (_split_cached)."""
    path: Path
    is_real: bool
    rmse: float
    detail: str

    @property
    def status(self) -> str:
        """"padded", "real", "other" or "error" (_diagnose_status)."""
        return _diagnose_status(self.is_real, self.detail)


def _diagnose_pool(tiff_files: List[Path], workers: int, temp_dir: Optional[Path],
                   compress_tmp: str, on_result=None, roots=None,
                   budget: Optional["_MemoryBudget"] = None,
                   pixels: Optional[Dict[str, int]] = None) -> List[DiagnoseResult]:
    """Run _is_real_16bit over `tiff_files` with `workers` threads, printing one line
    per file (its path below `roots`, see _display_path). Returns a DiagnoseResult per
    file, in completion order. `on_result` is called with
    each result as it arrives (the Diagnose cache saves from it). On Ctrl+C the files
    not started yet are cancelled instead of all being run first. With `budget`, each
    check first reserves its own estimate (`pixels` x tw.COMPARE_BYTES_PER_PIXEL)."""

    def process_one(tiff_path) -> DiagnoseResult:
        if budget is None:
            return DiagnoseResult(tiff_path, *_is_real_16bit(tiff_path, temp_dir, compress_tmp))
        key = os.path.normcase(os.path.abspath(tiff_path))
        need = (pixels or {}).get(key, tw.UNKNOWN_IMAGE_PIXELS) * tw.COMPARE_BYTES_PER_PIXEL
        held = budget.acquire(need)
        try:
            return DiagnoseResult(tiff_path, *_is_real_16bit(tiff_path, temp_dir, compress_tmp))
        finally:
            budget.release(held)

    results = []
    total = len(tiff_files)

    def show(done, r: DiagnoseResult):
        results.append(r)
        shown = {"padded": "PADDED", "real": "real 16-bit"}.get(r.status, r.detail)
        progress = f"[{done}/{total}]"
        where = _display_path(r.path, roots)
        color = {"padded": "yellow", "real": "dim"}.get(r.status, "red")
        _say([f"  {progress} ", (shown, color), f" {where}"])
        if on_result is not None:
            on_result(r)

    _run_pool(tiff_files, process_one, workers, show,
              lambda f, e: DiagnoseResult(f, False, 0.0, f"ERROR: {e}"), "checked")
    return results


def _ask_line(prompt_text: str, default: str) -> str:
    """One free-text answer; '-' (the shown default when empty) means none."""
    value = _ask(prompt_text, default or "-")
    return "" if value in ("-", "none") else value


def _usable_temp_dir(path, style: str) -> Optional[Path]:
    """The temp folder `path`, made when it is missing; None -- the system temp folder -- when
    no folder is given or it cannot be made (said on the screen, in `style`)."""
    if not path:
        return None
    temp_dir = Path(path)
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _say(f"Cannot use temp dir {temp_dir}: {e} -- using system temp.", style)
        return None
    return temp_dir


def _ask_temp_dir() -> Optional[Path]:
    """Diagnose's temp folder: ONE question, the same on both screens (v3.1 phase 6, F3/F4:
    the Rich screen asked yes/no and then the path, the plain one the path under a question
    that read like yes/no -- so a "y" made a folder named y in the current folder). A yes/no
    word is read as one: no = the system temp folder, yes = asked for the path. A relative
    path is made absolute and shown before it is used; None = the system temp folder."""
    answer = _ask("\nTemp folder for the checks (Enter = the system temp folder)", style="")
    if answer.lower() in _NO_WORDS:
        answer = ""
    elif answer.lower() in _YES_WORDS:
        answer = _ask("Temp folder path", style="")
    if not answer:
        return None
    # audit round 16: an unwritable/invalid temp dir raised out of the wizard.
    temp_dir = _usable_temp_dir(os.path.abspath(answer), "red")
    if temp_dir is not None:
        _say(f"Using temp dir: {temp_dir}", "dim")
    return temp_dir


def run_diagnose_tiffs(cfg: "ConfigManager") -> bool:
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
        _say("No TIFF files found.", "yellow")
        return True

    # the Workers answer read as everywhere else (_read_workers): it had a copy of its own
    workers = _read_workers(_ask("\nWorkers", str(cfg.config.last_workers or cfg.config.default_workers)), cfg)
    # Remember the choice like the other workflows do
    cfg.config.last_workers = workers
    cfg.save_config()

    temp_dir = _ask_temp_dir()

    _say("\nCompress temp TIFFs during comparison?", "cyan")
    _say("  [1] No      (uncompressed, fast)")
    _say("  [2] LZW     (8-bit LZW, balanced)")
    _say("  [3] ZIP     (8+16 bit Deflate, slower)")
    compress_tmp = {"1": "none", "2": "lzw", "3": "zip"}[
        _ask("Choice", "1", style="", choices=["1", "2", "3"])]

    convert_padded = _ask_yes_no("\nRepeat/preset: also convert the padded files it finds? "
                                 "(pixel-verified, originals kept in OLD_PADDED)", False, "cyan")

    settings = {"roots": [os.path.abspath(str(folder))], "scope": scope, "skip_names": skip_names,
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
        results += _diagnose_check(todo, keys, cache, workers, temp_dir, compress_tmp, [folder])
    statuses = [r.status for r in results]
    padded_files = [r.path for r in results if r.status == "padded"]
    padded_count = len(padded_files)
    real_count = statuses.count("real")
    other_count = statuses.count("other")
    error_count = statuses.count("error")

    _say("\nResults:", "bold")
    rows = [["Real 16-bit", str(real_count)],
            [("Padded 8-bit (stretched)", "yellow"), str(padded_count)] if padded_count
            else ["Padded 8-bit", "0"]]
    if other_count > 0:
        rows.append([("Other bit depth", "magenta"), str(other_count)])
    if error_count > 0:
        rows.append([("Errors/Failed", "red"), str(error_count)])
    _table([{"name": "Type", "style": "cyan"}, {"name": "Count", "style": "green"}], rows,
           box=BOX_SIMPLE)

    if DEBUG_TIMING and total_start is not None:
        _say(f"\n[DEBUG] Total time: {time.time() - total_start:.2f}s", "dim")

    if padded_count > 0:
        _say(f"\nWarning: {padded_count} file(s) are padded 8-bit (converted from 8-bit to 16-bit "
             f"without adding real data).", "yellow")
        if _ask_yes_no("\nCompress padded files to 8-bit ZIP?", False, "cyan"):
            _say(f"Compressing {len(padded_files)} file(s)...", "cyan")
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


def _split_cached(files: List[Path], cache: Dict) -> Tuple[List[DiagnoseResult], List[Path], Dict]:
    """(results already known, files to check, {file: key}). A cached verdict counts only
    while the file keeps the size and modification time it had when checked."""
    cached, todo, keys = [], [], {}
    for f in files:
        key = _file_key(f)
        keys[f] = key
        hit = cache.get(key[0]) if key else None
        if (key is not None and isinstance(hit, dict) and hit.get("size") == key[1]
                and hit.get("mtime_ns") == key[2]
                and hit.get("status") in ("real", "padded", "other")):
            cached.append(DiagnoseResult(f, hit["status"] == "real", float(hit.get("rmse") or 0.0),
                                         str(hit.get("detail") or "")))
        else:
            todo.append(f)
    return cached, todo, keys


def _check_with_cache(todo: List[Path], keys: Dict, cache: Dict, workers: int,
                      temp_dir: Optional[Path], compress_tmp: str, roots=None,
                      budget=None, pixels=None) -> List[DiagnoseResult]:
    """_diagnose_pool over `todo`, recording each verdict in `cache` and writing the
    cache every _CACHE_SAVE_EVERY results and once more at the end -- also when the
    run is interrupted -- so a stopped run keeps what it already checked."""
    pending = [0]

    def record(result: DiagnoseResult):
        status = result.status
        key = keys.get(result.path)
        if key and status != "error":
            cache[key[0]] = {"size": key[1], "mtime_ns": key[2], "status": status,
                             "rmse": result.rmse, "detail": result.detail}
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


def _diagnose_check(todo: List[Path], keys: Dict, cache: Dict, workers: int,
                    temp_dir: Optional[Path], compress_tmp: str, roots) -> List[DiagnoseResult]:
    """The files the cache could not answer, checked: each verdict kept in the cache as it
    comes in (_check_with_cache), or the pool alone with DIAGNOSE_CACHE off. Workflow 7 and
    a Diagnose preset both check through here."""
    budget, pixels = _memory_budget(todo, tw.COMPARE_BYTES_PER_PIXEL, "16-bit check")
    _say(f"\nDiagnosing {len(todo)} TIFF file(s) with {workers} workers...", "cyan")
    if DIAGNOSE_CACHE:
        return _check_with_cache(todo, keys, cache, workers, temp_dir, compress_tmp,
                                 roots=roots, budget=budget, pixels=pixels)
    return _diagnose_pool(todo, workers, temp_dir, compress_tmp, roots=roots,
                          budget=budget, pixels=pixels)


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
    # audit round 28: only the boolean the wizard saves turns conversion on -- "false",
    # "no" or 0 in an edited preset used to convert by truthiness.
    convert = d.get("convert_padded") is True
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
        if not _ask_yes_no("Run it now?", False):
            _say("Cancelled.", "dim")
            return 1

    results = list(cached)
    conv_failed = []
    workers = clamp_workers(d.get("workers") or cfg.config.default_workers,
                            cfg.config.default_workers)
    if todo:
        temp_dir = _usable_temp_dir(d.get("temp_dir"), "yellow")
        compress_tmp = d.get("compress_tmp") or "none"
        results += _diagnose_check(todo, keys, cache, workers, temp_dir, compress_tmp, roots)

    by_status: Dict[str, List[Tuple[Path, str]]] = {"padded": [], "real": [], "other": [], "error": []}
    for r in sorted(results, key=lambda r: str(r.path)):
        by_status[r.status].append((r.path, r.detail))

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
        # audit round 28: the check phase falls back to the system temp when the saved
        # temp_dir cannot be made; this phase re-read the saved value and ended in a
        # traceback. Same fallback here.
        temp_dir = _usable_temp_dir(d.get("temp_dir"), "yellow")
        done = _compress_padded_files(padded, temp_dir, workers, cfg)
        n = (done or {}).get("converted", 0) if isinstance(done, dict) else 0
        conv_failed = list((done or {}).get("failed", [])) if isinstance(done, dict) else []
        lines.insert(lines.index(f"Errors: {len(by_status['error'])}") + 1,
                     f"Converted to 8-bit now: {n} of {len(padded)} (originals in OLD_PADDED\\; "
                     f"the rest were kept -- see the lines above)")
        if conv_failed:
            # audit round 28: a failed conversion used to exit 0 and the log never said
            # which file it was.
            lines += ["", "Conversion FAILED (original kept as it was):"]
            lines += [f"  {f} -- {why}" for f, why in conv_failed]
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
            f.write(f"{TOOLKIT_NAME} {TOOLKIT_VERSION}\n")
            f.write("\n".join(lines) + "\n")
        print(f"\nReport: {log_path}")
    except OSError as e:
        _say(f"WARNING: could not write the report: {e}", "yellow")
    return 1 if (by_status["error"] or conv_failed) else 0


def _tiff_extra_pages_are_thumbnails(tiff_path) -> Optional[bool]:
    """The wizard's own tiff_has_only_subfile_pages (tiff_workflow/common.py).

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

    MASK and PAGE are deliberately NOT accepted, exactly like the backends' safe_mode
    calls: a full-size IR channel is a real page, not a preview.
    """
    try:
        r = _run_captured(
            ["magick", "identify", "-ping", "-format", "%[tiff:subfiletype]|%w|%h\n", str(tiff_path)],
            _work_timeout(30, tiff_path), text=True,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
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
    The backends' marker restore (tw.subfile_marker_args) was fixed for exactly this; this copy had been
    left behind.
    """
    try:
        r = _run_exiftool(
            ["-a", "-G1", "-s", "-s", "-n", "-SubfileType", str(src_path)],
            _work_timeout(30, src_path), text=True,
        )
        if r.returncode != 0:
            return False
    except Exception:
        return False

    src_types = tw._parse_subfile_types(r.stdout or "")

    pages = _tiff_page_count(src_path)
    if pages is None:
        return False
    # Single-page sources need no restore: magick only stamps PAGE when it writes a
    # multi-page file, and an absent NewSubfileType means 0 per the TIFF spec anyway.
    tags = tw.subfile_marker_list(pages, src_types)
    if not tags:
        return True
    try:
        r = _run_exiftool(
            ["-q", "-q", "-overwrite_original"] + tags + [str(dst_path)],
            _work_timeout(30, dst_path),
        )
        return r.returncode == 0
    except Exception:
        return False


def _tiff_sample_layout(path) -> Optional[List[Dict]]:
    """Per-page sample layout of a TIFF, or None when it cannot be read (fail closed).

    audit round 28: ImageMagick's TIFF writer silently reduces a page whose every pixel has
    R=G=B (B&W film scanned or exported as RGB) to ONE gray channel -- keeping the RGB ICC
    profile -- and writes a 4-sample neutral RGB+IR scan as 2 samples, half of each row
    lost. Every pixel gate here compares through magick's own decode, where gray and
    neutral RGB are equal, so none of them could see it. Python mirror of
    tiff_sample_layout in the backends.

    One dict per main IFD (IFD0, IFD1, ... -- SubIFDs are not pages), in order:
      cls   -- "gray" (Photometric 0/1), "rgb" (2, or 6 = YCbCr, which magick writes as
               RGB), else the Photometric number as text,
      spp   -- SamplesPerPixel (absent = 1, the TIFF default),
      alpha -- the ExtraSamples values that are alpha (1 associated, 2 unassociated);
               0 (unspecified) and an absent tag are not alpha,
      bits  -- BitsPerSample as printed (absent = "1").
    """
    try:
        r = _run_exiftool(
            ["-a", "-G1", "-s", "-s", "-n", "-SamplesPerPixel", "-PhotometricInterpretation",
             "-ExtraSamples", "-BitsPerSample", str(path)],
            _work_timeout(30, path), text=True,
        )
        if r.returncode != 0:
            return None
    except Exception:
        return None
    try:
        pages = tw._parse_layout(r.stdout or "", str(path))
    except tw.ToolError:
        return None
    out = []
    for text in pages:
        cls, spp, bits, alpha = text.split("/")
        if cls.startswith("p"):         # a Photometric the parser does not name: its number
            cls = cls[1:]
        if not cls:
            return None
        try:
            spp_n = int(spp.strip() or "1")
        except ValueError:
            return None
        out.append({"cls": cls, "spp": spp_n, "alpha": tuple(a for a in alpha.split(",") if a),
                    "bits": " ".join(bits.split(",")) or "1"})
    return out


def _magick_keep_type_args(layout: Optional[List[Dict]]) -> List[str]:
    """The magick write options that stop the TIFF writer from changing the sample layout.

    tw.magick_keep_type_args decides (one rule). `-type` is a WRITE setting that applies to
    every page of the output (per-page parentheses do not scope it -- verified on 7.1.2-27),
    so it is passed only when every page wants the same one: all RGB without alpha ->
    TrueColor (a neutral RGB+IR scan keeps its 4 samples, pixel-identical), all RGB with
    alpha -> TrueColorAlpha, plus `tiff:alpha=associated` when every alpha is associated
    (magick otherwise relabels it unassociated). A mixed file (RGB page + gray IR page)
    gets nothing; the layout gate after the write refuses it if magick changed a page.
    `-define colorspace:auto-grayscale=false` does NOT stop the reduction.
    """
    if not layout:
        return []
    return tw.magick_keep_type_args(
        [f"{p['cls']}/{p['spp']}/{','.join(str(p['bits']).split())}/{','.join(p['alpha'])}"
         for p in layout])


def _sample_layout_diff(src: Optional[List[Dict]], dst: Optional[List[Dict]],
                        pages=None, compare_bits: bool = True) -> Optional[str]:
    """None when `dst` keeps the sample layout of `src`, else a short reason (fail closed:
    an unreadable side is a difference). `pages` = list of (src_page, dst_page) pairs to
    compare; default every page, and then the page COUNTS must match too.
    `compare_bits` is False only for the padded 16 -> 8-bit conversion."""
    if src is None or dst is None:
        return "sample layout unreadable"
    if pages is None:
        if len(src) != len(dst):
            return f"{len(src)} -> {len(dst)} pages"
        pages = [(i, i) for i in range(len(src))]
    for sp, dp in pages:
        if sp >= len(src) or dp >= len(dst):
            return f"page {sp} missing"
        a, b = src[sp], dst[dp]
        keys = ("cls", "spp", "alpha", "bits") if compare_bits else ("cls", "spp", "alpha")
        for k in keys:
            if a[k] != b[k]:
                return f"page {sp} {k} {a[k]} -> {b[k]}"
    return None


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


@dataclass(frozen=True)
class PaddedResult:
    """What _process_single_padded did with one file. Only "ok" lands the 8-bit file `tmp8`
    (_compress_padded_files does it); any other status leaves the original untouched, and a
    `tmp8` it wrote is the caller's to drop."""
    name: str
    parent: Path
    status: str
    size_orig: Optional[int] = None
    size_zip: Optional[int] = None
    ratio: Optional[float] = None
    exif_ok: bool = False
    error: Optional[str] = None
    tmp8: Optional[Path] = None


@_scoped_magick_aliases
def _process_single_padded(tiff_path, staging) -> PaddedResult:
    """Convert one padded file to an 8-bit ZIP in `staging`, through every gate in order."""
    name = tiff_path.name
    parent = tiff_path.parent
    unique_id = uuid.uuid4().hex[:8]
    # Bracket-free staging name (audit round 16): tmp8 feeds an ImageMagick OUTPUT spec
    # (_magick_spec below), and no '[' escape exists on Windows -- a bracket in `name`
    # would make magick write a different file than the one we verify and move.
    tmp8 = staging / f"tmp8_{unique_id}_{name.replace('[', '(').replace(']', ')')}"
    status = None

    def res(status, error=None) -> PaddedResult:
        return PaddedResult(name, parent, status, error=error, tmp8=tmp8)

    # _is_real_16bit judges page [0] only, but the conversion below rewrites the WHOLE file.
    # On a scanner RGB+IR scan whose RGB is padded, that would down-convert an IR channel
    # that may hold real 16-bit data and was never diagnosed. Refuse those files; a Capture
    # One main+thumbnail pair is still converted (the thumbnail is already 8-bit).
    # Fail-closed: an undeterminable page layout is refused too.
    extras_ok = _tiff_extra_pages_are_thumbnails(tiff_path)
    if extras_ok is not True:
        status = "multi_page_refused" if extras_ok is False else "page_layout_unreadable"
        return res(status)

    # audit round 28: magick reduces an all-neutral RGB page to one gray channel (and writes
    # a neutral RGB+IR scan with half its samples), and the pixel gate below compares through
    # magick's own decode, which cannot tell them apart. Read the layout first (fail closed),
    # pin it with -type, and compare it again after the write.
    src_layout = _tiff_sample_layout(tiff_path)
    if src_layout is None:
        return res("layout_unreadable")
    type_args = _magick_keep_type_args(src_layout)

    try:
        try:
            result = _run_captured(
                ["magick", str(tiff_path), "-depth", "8"] + type_args
                + ["-compress", "zip", tw.magick_path_spec(tmp8)],
                _work_timeout(CONVERT_TIMEOUT_S, tiff_path)
            )
        except FileNotFoundError:
            status = "magick_not_found"
            return res(status)
        except Exception as e:
            status = "error"
            return res(status, str(e))

        if result.returncode != 0:
            status = "magick_error"
            return res(status)

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
                     "-tagsfromfile", tag_source, "-all:all",
                     # audit round 28: -all:all does not copy IPTC:CodedCharacterSet, so
                     # UTF-8 IPTC (伊豆沼) was re-encoded as Latin and came back '???'.
                     "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet", "-unsafe",
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
        # safe_mode and --skip-compressed-with-thumb on later runs. The
        # backends all restore them; this path was the only one that did not.
        # The restore runs AFTER the integrity gate below: an unreadable page count must
        # surface as page_count_mismatch from the gate, not as a restore failure.

        if not tmp8.exists():
            status = "missing"
            return res(status)

        # Integrity check: ensure the compressed file decodes and dimensions match
        try:
            verify_result = _run_captured(
                ["magick", str(tmp8), "null:"],
                _work_timeout(30, tmp8)
            )
            if verify_result.returncode != 0:
                status = "integrity_error"
                return res(status)

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
                return res(status)
            if new_dims != orig_dims:
                status = "dimension_mismatch"
                return res(status)

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
                return res(status)

            # audit round 28: same samples per pixel, colour model and alpha kind on every
            # page (the depth changes on purpose). See _tiff_sample_layout.
            if _sample_layout_diff(src_layout, _tiff_sample_layout(tmp8), compare_bits=False):
                status = "layout_changed"
                return res(status)

            # The page markers go back BEFORE the pixel gate, so the gate reads the file exactly
            # as it will land (v3.1 phase 6, W-O11, the owner's choice; the backends do the same
            # since phase 4). audit round 16: a False return is a refusal (WARN, original kept),
            # never ignored. After the page-count and layout checks, so an unreadable layout
            # still surfaces as their own status.
            if not _restore_subfile_types(tiff_path, tmp8):
                status = "subfile_restore_failed"
                return res(status)

            # The 8-bit rewrite must hold exactly the pixels of the original. Nothing above
            # looks at pixels: the verdict that called the file "padded" may be a cached one
            # (DIAGNOSE_CACHE), and a file that is really 16-bit must never be replaced by
            # its 8-bit rounding. -metric AE counts differing pixels; 0 with exit 0 passes,
            # anything else (different, timeout, unparseable) refuses.
            status = _padded_pixels_status(tiff_path, tmp8)
            if status is not None:
                return res(status)
        except Exception as e:
            status = "integrity_error"
            return res(status, str(e))

        size_orig = tiff_path.stat().st_size
        size_zip = tmp8.stat().st_size
        ratio = (1 - size_zip / size_orig) * 100 if size_orig > 0 else 0
        status = "ok"
        return PaddedResult(name, parent, status, size_orig, size_zip, ratio, exif_ok, None, tmp8)
    finally:
        if status and status != "ok" and tmp8.exists():
            try:
                tmp8.unlink()
            except Exception:
                pass


def _land_padded_conversion(tmp8: Path, final_dst: Path, backup_path: Path):
    """Put the verified 8-bit file at `final_dst` and keep the original as `backup_path`.
    Returns (error_or_None, note_or_None); on an error the original is in its place.

    audit round 28 -- the irreversible step comes last (jxl-photo's rule, the one rounds
    25-26 applied to the backends):
      1. the converted file is brought NEXT TO the photo first, under a temp name
         (<name>.<8 hex>_<12 hex>.tif, which every backend skips as a leftover); from
         another volume that is a copy, size-checked -- a kill here leaves the original
         untouched and only that sibling behind;
      2. the original gets a second name in OLD_PADDED/ -- a HARD LINK, nothing is copied
         or moved, so the photo never leaves its place;
      3. the sibling is renamed over the photo (os.replace: one atomic rename).
    At every instant the photo's name holds either the original or the converted file.
    Where a hard link is impossible (FAT/exFAT, some network shares) step 2 is a rename
    instead, and only the instant between the two renames leaves the place empty.
    Never shutil.move: on a locked file it falls back to copy + delete and left a full
    copy in OLD_PADDED/ while reporting the backup as failed.
    """
    sibling = final_dst.with_name(
        f"{final_dst.name}.{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:12]}.tif")
    try:
        try:
            os.replace(str(tmp8), str(sibling))
        except OSError:
            size = tmp8.stat().st_size
            shutil.copyfile(str(tmp8), str(sibling))
            if sibling.stat().st_size != size:
                raise OSError("size mismatch after copy") from None
            tmp8.unlink(missing_ok=True)
    except Exception as e:
        sibling.unlink(missing_ok=True)
        return f"could not bring the converted file next to the original ({e}) — original preserved", None

    # audit round 28 (review): Windows refuses to rename over -- or to unlink -- a READ-ONLY
    # file, and a hard link shares the attribute with the original. A read-only original (a
    # scan copied from a CD) therefore failed at step 3 where the old move-out / move-in had
    # converted it, and the cleanup could not remove the link: a "backup" of a conversion that
    # never happened stayed in OLD_PADDED/. Clear the attribute for the swap; the original
    # gets it back under whichever name it ends with.
    ro_mode = None
    if os.name == "nt":
        try:
            mode = os.stat(str(final_dst)).st_mode
            if not mode & stat.S_IWRITE:
                os.chmod(str(final_dst), mode | stat.S_IWRITE)
                ro_mode = mode
        except OSError as e:
            sibling.unlink(missing_ok=True)
            return f"the original is read-only and that cannot be changed ({e}) — original preserved", None

    def _read_only_again(p):
        if ro_mode is not None:
            try:
                os.chmod(str(p), ro_mode)
            except OSError:
                pass

    linked = False
    try:
        os.link(str(final_dst), str(backup_path))
        linked = True
    except OSError:
        pass
    if linked:
        try:
            os.replace(str(sibling), str(final_dst))
        except OSError as e:
            try:
                backup_path.unlink()
            except OSError:
                pass
            sibling.unlink(missing_ok=True)
            _read_only_again(final_dst)
            return f"could not move the converted file into place ({e}) — original preserved", None
        _read_only_again(backup_path)
        return None, None

    try:
        os.replace(str(final_dst), str(backup_path))
    except OSError as e:
        sibling.unlink(missing_ok=True)
        _read_only_again(final_dst)
        return f"backup to OLD_PADDED failed ({e}) — original preserved", None
    try:
        os.replace(str(sibling), str(final_dst))
    except OSError as e:
        try:
            os.replace(str(backup_path), str(final_dst))
            restored = " — original restored"
            _read_only_again(final_dst)
        except OSError:
            restored = f" — ORIGINAL LEFT IN OLD_PADDED/{backup_path.name}"
            _read_only_again(backup_path)
        sibling.unlink(missing_ok=True)
        return f"could not move the converted file into place ({e}){restored}", None
    _read_only_again(backup_path)
    return None, "(no hard link on this drive: the original was renamed into OLD_PADDED/)"


# A conversion that should have replaced the file and did not: listed as failed. A refusal
# (real 16-bit data, a multi-page scan, a bigger ZIP) is the gate doing its job, not a failure.
_PADDED_FAILED_STATUSES = ("magick_not_found", "error", "magick_error", "missing",
                           "page_layout_unreadable", "subfile_restore_failed",
                           "integrity_error", "dimension_mismatch", "dimension_unreadable",
                           "page_count_mismatch", "pixel_compare_failed",
                           "layout_unreadable", "layout_changed")


def _padded_outcome(status: str, error: Optional[str]) -> Optional[Tuple[str, str]]:
    """(line, style) for a conversion that replaced nothing; None for "ok" and for a status
    with no line of its own."""
    if status == "magick_not_found":
        return "    FAILED: magick not found", "red"
    if status == "error":
        return f"    FAILED: {error}", "red"
    if status == "magick_error":
        return "    FAILED: magick error", "red"
    if status == "missing":
        return "    FAILED: output missing", "red"
    if status in ("multi_page_refused", "page_layout_unreadable"):
        reason = ("multi-page TIFF with non-thumbnail pages (scanner IR / layers): the "
                  "diagnosis only looked at page 0, so converting the whole file could "
                  "down-convert pages that were never checked"
                  if status == "multi_page_refused"
                  else "page layout unreadable")
        return f"    SKIPPED: {reason} -- original preserved", "yellow"
    if status == "subfile_restore_failed":
        # audit round 16: _restore_subfile_types said the markers could not be
        # restored, so the rewrite's metadata is not trustworthy -- degraded, not OK.
        return "    WARN: could not restore per-page SubfileType markers -- original preserved", "yellow"
    if status == "pixel_mismatch":
        return ("    SKIPPED: the 8-bit version is not pixel-identical -- this file holds "
                "real 16-bit data; original preserved", "yellow")
    if status in ("integrity_error", "dimension_mismatch",
                  "dimension_unreadable", "page_count_mismatch",
                  "pixel_compare_failed", "layout_unreadable", "layout_changed"):
        return f"    FAILED: {status}", "red"
    return None


def _finalize_padded(n: int, total: int, result: PaddedResult, failed: list) -> bool:
    """One file of the padded conversion, on the caller's thread: its line, then, for an
    "ok" whose ZIP is smaller, the original to OLD_PADDED/ and the 8-bit file into its place
    (_land_padded_conversion). A conversion that should have landed and did not is appended
    to `failed` as (path, why). True = the file was replaced."""
    name, parent, status = result.name, result.parent, result.status
    size_orig, size_zip, ratio = result.size_orig, result.size_zip, result.ratio
    exif_ok, error_msg, tmp8 = result.exif_ok, result.error, result.tmp8
    final_dst = parent / name
    if status in _PADDED_FAILED_STATUSES:
        failed.append((final_dst, status + (f": {error_msg}" if error_msg else "")))

    _say(f"  [{n}/{total}] {parent.name}{os.sep}{name}")

    if status != "ok":
        line = _padded_outcome(status, error_msg)
        if line:
            _say(*line)
        return False
    if tmp8 is None or size_orig is None or size_zip is None or ratio is None:
        # An "ok" always carries its 8-bit file and the sizes; without them nothing lands.
        _say("    FAILED: the conversion did not report its 8-bit file — original preserved", "red")
        failed.append((final_dst, "the conversion did not report its 8-bit file"))
        if tmp8 is not None:
            tmp8.unlink(missing_ok=True)
        return False
    if not exif_ok:
        # Do NOT overwrite original if EXIF was lost
        _say("    FAILED: EXIF copy or History entry failed — original preserved", "red")
        failed.append((final_dst, "EXIF copy or History entry failed"))
        tmp8.unlink(missing_ok=True)
        return False

    _say(["    ", ("OK", "green"), f" {_format_size(size_orig)} -> {_format_size(size_zip)} "
          f"({ratio:.1f}% smaller, EXIF preserved)"])

    if size_zip >= size_orig:
        _say("    SKIPPED (ZIP larger than original)", "dim")
        tmp8.unlink(missing_ok=True)
        return False

    # Back up the original to OLD_PADDED/ and replace it (lossy 16 -> 8-bit).
    # audit round 28: the irreversible step comes last. The original used to go
    # to OLD_PADDED/ FIRST and the converted file was then moved in from the
    # staging folder -- across volumes a copy straight into the photo's name, so
    # a kill left the place empty or holding a truncated file, and nothing
    # reported it. Now: see _land_padded_conversion.
    backup_dir = parent / "OLD_PADDED"
    try:
        backup_dir.mkdir(exist_ok=True)
        backup_path = backup_dir / name
        if backup_path.exists():
            v = 2
            while (backup_dir / f"{final_dst.stem}_v{v}{final_dst.suffix}").exists():
                v += 1
            backup_path = backup_dir / f"{final_dst.stem}_v{v}{final_dst.suffix}"
    except Exception as e:
        _say(f"    FAILED: backup to OLD_PADDED failed ({e}) — original preserved", "red")
        failed.append((final_dst, f"backup to OLD_PADDED failed ({e})"))
        tmp8.unlink(missing_ok=True)
        return False
    err, note = _land_padded_conversion(tmp8, final_dst, backup_path)
    if err:
        _say(f"    FAILED: {err}", "red")
        failed.append((final_dst, err))
        tmp8.unlink(missing_ok=True)
        return False
    if note:
        _say(f"    {note}", "yellow")
    _say(f"    Original backed up: OLD_PADDED/{backup_path.name}", "dim")
    return True


def _compress_padded_files(padded_files: list, temp_dir: Optional[Path], workers: int, cfg: "ConfigManager"):
    """
    Compress padded 8-bit TIFFs to 8-bit ZIP.
    Converts 16-bit to 8-bit then ZIP compresses. Preserves EXIF via exiftool.
    Originals are backed up to an OLD_PADDED/ subfolder before being replaced.
    Uses parallel processing with ThreadPoolExecutor. Returns {"converted": n} -- the
    files actually replaced (a non-empty dict, so `if _compress_padded_files(...)` holds),
    and "failed": [(path, reason)] for every file that should have been converted and was
    not (refusals such as real 16-bit data or a multi-page scan are not failures).
    """
    converted = 0
    failed: List[Tuple[Path, str]] = []
    if temp_dir is None:
        temp_dir = Path(tempfile.gettempdir())
    # Run-scoped staging dir. A shared "compress_staging" meant the cleanup below wiped
    # every file in it, including another concurrent run's in-flight output -- if that run
    # was between the OLD_PADDED backup and the final move, the file vanished from the
    # source folder. Same reasoning as $runStagingId in compress_tiff_zip.py.
    staging = temp_dir / f"compress_staging_{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=True)

    # Same per-file memory budget as Diagnose: the workers asked for run, and a huge scan
    # waits for room instead of sizing every worker for itself.
    budget, pixels = _memory_budget(padded_files, tw.COMPARE_BYTES_PER_PIXEL, "8-bit conversion")

    def _process_one(tiff_path):
        if budget is None:
            return _process_single_padded(tiff_path, staging)
        need = pixels.get(os.path.normcase(os.path.abspath(tiff_path)),
                          tw.UNKNOWN_IMAGE_PIXELS) * tw.COMPARE_BYTES_PER_PIXEL
        held = budget.acquire(need)
        try:
            return _process_single_padded(tiff_path, staging)
        finally:
            budget.release(held)

    # Each file is finished -- printed, backed up, moved into place -- AS SOON AS its
    # conversion is done. The pool used to convert every file first and only then print
    # and replace them all: an hour of blank screen on a large batch, and every staged
    # file held on the staging disk until the end.
    def _finalize(done, result):
        nonlocal converted
        if _finalize_padded(done, len(padded_files), result, failed):
            converted += 1

    # audit round 16: a worker that raised gets no tmp8 -- _finalize may unlink that slot,
    # and the SOURCE path there risked deleting the original.
    _run_pool(padded_files, _process_one, workers, _finalize,
              lambda f, e: PaddedResult(f.name, f.parent, "error", error=str(e)), "done")

    # Safe to wipe wholesale: the directory belongs to this run only.
    shutil.rmtree(staging, ignore_errors=True)

    return {"converted": converted, "failed": failed}


def _size_and_time(path: Path):
    """(size, write time in ns) of a file, or None when it cannot be read: what Delete
    OLD_TIFFs compares right before deleting (W-O16)."""
    try:
        st = os.stat(tw.long_path(str(path)))
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


_MISSING_PARENT = "parent file missing"


def _purge_collect(folder: Path) -> Tuple[List[Path], List[Tuple[Path, Path, int]]]:
    """(the OLD_TIFFs folders below `folder`, every file in them as (backup, the file of that
    name in the parent folder, backup size))."""
    # Find all OLD_TIFFs folders recursively (case-insensitive for Unix)
    old_dirs = sorted([d for d in folder.rglob("*") if d.is_dir() and d.name.lower() == "old_tiffs"])
    items = []
    for od in old_dirs:
        parent = od.parent
        for f in sorted(od.glob("*")):
            if not f.is_file():
                continue
            items.append((f, parent / f.name, f.stat().st_size))
    return old_dirs, items


def _purge_verdict(item) -> Optional[Tuple[Path, Path, str]]:
    """Why the backup must be KEPT, as (backup, parent copy, reason); None = its copy in the
    parent folder verified and it may go. A TIFF goes through _compare_tiff_metadata; a
    sidecar needs an identical copy. No copy at all is _MISSING_PARENT: nothing to compare,
    nothing that may be deleted."""
    old_file, new_file, _ = item
    if not tw.is_tiff_name(old_file.name):
        if not new_file.exists():
            return (old_file, new_file, _MISSING_PARENT)
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
        return (old_file, new_file, _MISSING_PARENT)
    match, detail = _compare_tiff_metadata(old_file, new_file)
    if not match:
        return (old_file, new_file, detail)
    return None


def _purge_check(item) -> Tuple[Optional[Tuple[Path, Path, str]], tuple]:
    """(_purge_verdict of the pair, how the pair looked right BEFORE it was compared): the size
    and write time of both files, taken by the worker that compares them (W-O16). A pair that
    changes during its own comparison, or any time after it -- while the rest of a big tree is
    still being checked, or while the questions are open -- is then kept by _purge_delete."""
    before = (_size_and_time(item[0]), _size_and_time(item[1]))
    return _purge_verdict(item), before


def _purge_verify(items: list, cfg) -> Tuple[list, list, Dict]:
    """(orphans, mismatches, checked_as): the backups with no copy in the parent, and the ones
    whose copy did not verify -- both are KEPT --, and how each pair looked right before it was
    compared (_purge_check), for _purge_delete. The workers are capped for the memory a
    compare takes."""
    # A backup whose parent counterpart is simply ABSENT is not a mismatch: there is nothing
    # to compare and nothing that may be deleted, so it is kept and reported. It used to be
    # lumped in with real mismatches and blocked the whole purge -- which a healthy folder
    # reaches routinely, because modes 0/9 rename a backup when one of that name already
    # exists (photo_20260805_101500.tif) and a photo.tiff source produces a photo.tif output.
    orphans: List[tuple] = []
    mismatches: List[tuple] = []
    checked_as: Dict = {}
    workers = clamp_workers(cfg.config.last_workers or cfg.config.default_workers)
    workers = _memory_capped_pool_workers(workers, [it[0] for it in items if tw.is_tiff_name(str(it[0]))], tw.COMPARE_BYTES_PER_PIXEL, "copy verification")
    with _timeout_load(workers), \
            concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for item, (result, before) in zip(items, executor.map(_purge_check, items)):
            checked_as[(item[0], item[1])] = before
            if result:
                (orphans if result[2] == _MISSING_PARENT else mismatches).append(result)
    return orphans, mismatches, checked_as


def _purge_report(folder: Path, old_dirs: list, items: list, orphans: list, mismatches: list) -> None:
    """Every backup with its verdict, then the ones kept and why."""
    # Everything in the backup folders, the files kept below included (v3.1 phase 6, F2: this
    # line showed the to-delete sum, so it read as if the kept backups were not there).
    total_in_old = sum(s for _, _, s in items)

    _say(f"\nOLD_TIFFs review -- {len(items)} file(s) in {len(old_dirs)} folder(s)", "cyan")
    _say(f"Total size in OLD_TIFFs: {_format_size(total_in_old)}\n", "dim")
    _say("File comparison (OLD_TIFFs vs parent):", "bold")

    orphan_keys = {(o[0], o[1]) for o in orphans}
    mismatch_detail = {(m[0], m[1]): m[2] for m in mismatches}
    for old_file, new_file, old_size in items:
        if new_file.exists():
            new_size = new_file.stat().st_size
            size_info = f"{_format_size(old_size)} -> {_format_size(new_size)}"
        else:
            size_info = f"{_format_size(old_size)} (parent missing)"
        detail = ""
        if (old_file, new_file) in mismatch_detail:
            status = ("MISMATCH", "red")
            detail = "  <- " + mismatch_detail.get((old_file, new_file), "")
        elif (old_file, new_file) in orphan_keys:
            status = ("KEPT", "yellow")
            detail = "  <- no counterpart in the parent folder, nothing to verify against"
        else:
            status = ("OK", "")
        _say(["  [", status, f"] {old_file.relative_to(folder)}  {size_info}{detail}"])

    if mismatches:
        head = (f"{len(mismatches)} file(s) did not verify against their parent copy and will "
                f"be KEPT (nothing below is deleted for them):")
        _say(f"\n{head}", "red")
        for old_file, _new_file, reason in mismatches:
            _say(f"  ! {old_file.relative_to(folder)}: {reason}", "red")

    if orphans:
        note = (f"{len(orphans)} file(s) have no counterpart in the parent folder and will be "
                f"KEPT (a mode 0/9 backup is renamed when one of that name already exists, and "
                f"a .tiff source produces a .tif output, so the names need not line up).")
        _say(f"\n{note}", "yellow")
        for old_file, _new_file, _reason in orphans:
            _say(f"  = {old_file.relative_to(folder)}", "yellow")


def _purge_confirm() -> bool:
    """The question (default no) and the current time typed back, within a minute."""
    if not _ask_yes_no("\nDelete ALL OLD_TIFFs content? THIS CANNOT BE UNDONE.", False, "red"):
        _say("Cancelled.", "dim")
        return False
    # Time confirmation (like mode 8)
    time_str = _ask("Type current time (HH:MM) to confirm deletion", style="red")
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
            _error(f"Time mismatch: expected {current_time}, got {time_str}.")
            _say("Cancelled.", "dim")
            return False
    except Exception as e:
        _error(f"Error validating time: {e}")
        return False
    return True


def _purge_delete(folder: Path, old_dirs: list, deletable: list, checked_as: Dict) -> Tuple[int, list]:
    """Delete the verified backups -- `deletable`, never every file: an orphaned backup is the
    only copy of that image left, so it is kept even when everything else in the folder
    verified clean -- then the OLD_TIFFs folders left empty. A pair that changed since it was
    checked (size or write time of either file, `checked_as`) is kept and listed (v3.1 phase
    6, W-O16). (deleted, the backups kept for that)."""
    deleted = 0
    changed = []
    for old_file, new_file, _ in deletable:
        if not old_file.exists():
            continue
        # Changed since the check (an edit, another run, the copy gone): kept, and listed.
        if (_size_and_time(old_file), _size_and_time(new_file)) != checked_as[(old_file, new_file)] \
                or _size_and_time(new_file) is None:
            changed.append(old_file)
            _say(f"  KEPT (changed since it was checked): {old_file.relative_to(folder)}", "yellow")
            continue
        try:
            old_file.unlink()
            deleted += 1
            _say(f"  DELETED: {old_file.relative_to(folder)}", "green")
        except Exception as e:
            _say(f"  ERROR deleting {old_file.name}: {e}", "red")

    # Try to remove empty directories
    for od in sorted(old_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if od.exists() and not any(od.iterdir()):
                od.rmdir()
                _say(f"  Removed folder: {od.relative_to(folder)}", "green")
        except Exception:
            pass
    return deleted, changed


def run_purge_old_tiffs(cfg: "ConfigManager") -> bool:
    """
    Verify and delete OLD_TIFFs/ folders.
    Compares each file in OLD_TIFFs with the equivalent in the parent folder
    (same filename), confirming content matches (ignoring compression).
    Shows sizes and asks for time confirmation before deleting.
    False when something did not verify: those backups were deliberately kept, and the
    caller should not read the run as "everything checked out".
    """
    folder = step_folder(cfg, "Root folder to scan for OLD_TIFFs")
    if folder is None:
        return False

    old_dirs, items = _purge_collect(folder)
    if not old_dirs:
        _say("No OLD_TIFFs folders found.", "yellow")
        return True
    if not items:
        _say("No files found in OLD_TIFFs folders.", "yellow")
        return True

    orphans, mismatches, checked_as = _purge_verify(items, cfg)
    # A file that does not verify is KEPT -- and only that file. One mismatch used to abort
    # the purge for the entire tree, so a single bad conversion (or, before the thumbnail
    # rule above, any file compressed with --generate-thumbnail) left the user with no way to
    # clean up the backups except deleting them by hand, which is exactly what this gate
    # exists to prevent.
    kept = {(k[0], k[1]) for k in orphans + mismatches}
    deletable = [it for it in items if (it[0], it[1]) not in kept]
    # `checked_as`: what each pair looked like right before it was compared. The deletion below
    # comes after the rest of the tree and the questions -- minutes later on a big tree -- and
    # re-checks it first (v3.1 phase 6, W-O16, the owner's choice: size and write time, not a
    # second full compare; taken per pair since the review of phases 5-8, T1).

    _purge_report(folder, old_dirs, items, orphans, mismatches)
    if not deletable:
        _say("\nNothing can be purged: no OLD_TIFFs file has a verified copy in its parent folder.",
             "yellow")
        return not mismatches

    _say(f"\n{len(deletable)} of {len(items)} file(s) verified OK.", "green")
    _say(f"Total to delete: {_format_size(sum(size for _, _, size in deletable))}", "dim")
    if not _purge_confirm():
        return False

    deleted, changed = _purge_delete(folder, old_dirs, deletable, checked_as)
    _say(f"\nDone. Deleted {deleted} file(s).", "green")
    if changed:
        _say(f"{len(changed)} kept: they changed after the check -- run Delete OLD_TIFFs again to "
             "check them anew.", "yellow")
    return not mismatches



def _format_size(size_bytes: float) -> str:
    """Format bytes as human-readable string."""
    if size_bytes < 0:
        return "0B"
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"

def _save_last_run(cfg, kind: str, label: str, commands: Optional[List[List[str]]],
                   manifest_workflow: Optional[Dict] = None,
                   manifest_default_mode: Optional[int] = None,
                   autofind: Optional[Dict] = None,
                   diagnose: Optional[Dict] = None) -> None:
    """Record the last executed workflow for the 'Repeat last workflow' menu entry.

    Command-driven workflows (1-4, 8) are journaled with their command lines, each as
    [subcommand, *args] (_journal_line); Diagnose (7) with its settings (`diagnose`, no
    commands) -- a repeat of it only reports. Restore/Delete OLD_TIFFs (5/6) re-ask their
    own questions and are not repeatable.
    Commands are stored with dry-run stripped -- a repeat always runs for real.
    Manifest runs store no commands (None): a repeat re-reads the CSV through
    the loader guards instead of replaying command lines.

    `manifest_workflow`/`manifest_default_mode`/`autofind`/`diagnose` are written ALWAYS
    (None for the kinds that do not use them): keeping an older run's values
    around would let a repeat run with settings the wizard never answered.
    """
    cfg.config.last_run_kind = kind
    cfg.config.last_run_label = label
    cfg.config.last_run_commands = [_journal_line(cmd) for cmd in (commands or [])] or None
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
    """Whether a saved journal (a preset, Repeat last) has the shape _run_journal reads. A
    preset edited by hand into another shape used to reach the runner and stop it with a
    traceback (`af["root"]`, `dict(workflow)`); it is dropped at start instead, with a
    sentence (v3.1 phase 6, W-O22)."""
    if not isinstance(entry, dict) or not entry.get("label"):
        return False
    af = entry.get("autofind")
    if af is not None and not (isinstance(af, dict) and isinstance(af.get("root"), str)
                               and isinstance(af.get("patterns"), list)
                               and all(isinstance(x, str) for x in af["patterns"])):
        return False
    if entry.get("kind") == "diagnose":
        return isinstance(entry.get("diagnose"), dict) and bool(entry["diagnose"].get("roots"))
    if entry.get("kind") == "manifest":
        mw = entry.get("manifest_workflow")
        return mw is None or isinstance(mw, dict)
    cmds = entry.get("commands")
    return (isinstance(cmds, list) and bool(cmds)
            and all(isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in cmds))


WORKFLOW_OPTIONS = [
    ("1", "Compress TIFFs", "To Zip/Deflate, modes 0-9 (any folder)"),
    ("2", "Fuji: Copy EXIF", "From JPEG to TIFF (AutoFind, S3/S5 Pro)"),
    ("3", "Fuji: Compress", "To Zip/Deflate (AutoFind, S3/S5 Pro)"),
    ("4", "Fuji: Copy + Compress", "Copy EXIF, then compress (AutoFind, S3/S5 Pro)"),
    ("5", "Restore OLD_TIFFs", "Move TIFFs from OLD_TIFFs/ to parent folder"),
    ("6", "Delete OLD_TIFFs", "Verify copy matches, then delete"),
    ("7", "Diagnose TIFFs", "Check if 16-bit is real or padded 8-bit"),
    ("8", "Generate Thumbnails", "Create sRGB thumbnails from TIFFs"),
]


def show_menu() -> Optional[str]:
    """Show workflow submenu (option 1 of the main menu), return choice or "0" for back."""
    valid = [k for k, _, _ in WORKFLOW_OPTIONS] + ["0"]
    _say("")
    _table([{"name": "#", "justify": "center", "style": "cyan", "width": 4},
            {"name": "Workflow", "style": "green"}, {"name": "Description", "style": "dim"}],
           [[key, name, desc] for key, name, desc in WORKFLOW_OPTIONS] + [["0", "Back", "Return to main menu"]],
           title="New Workflow -- select type", box=BOX_SIMPLE, header_style="bold cyan")
    return _ask("\nSelect workflow", choices=valid)


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
        ("6", "Script settings", True),
    ]
    # An answer that came in while an earlier screen was busy (the check runs in the background).
    poll = getattr(cfg, "update_poll", None)
    if poll and not getattr(cfg, "update_available", None):
        late = poll()
        if late:
            _say(late[0], "yellow")
            cfg.update_available = (cfg.config.update_latest_seen, late[1])
    if getattr(cfg, "update_available", None):
        options.append(("U", f"Update available: {cfg.update_available[0]} -- open the release page",
                        True))
    options.append(("0", "Exit", True))

    _table([{"name": "Key", "style": "bold cyan"}, {"name": "Option"},
            {"name": "Status", "justify": "center"}],
           [[key, desc, "" if available else ("(unavailable)", "dim")] for key, desc, available in options],
           panel={"title": f"Main Menu -- {TOOLKIT_NAME} {TOOLKIT_VERSION}", "border_style": "green"},
           show_header=False, box=None)
    return _ask("Select option", style="", choices=[o[0] for o in options if o[2]])


# --- Manifest System -------------------------------------------------


# Modes 2/4/5 can land two entries in the same folder (shared flat Destination,
# shared rename target, shared sibling ZIP/). Every other mode writes only
# inside each entry's own Source tree, so the collision scan is skipped there.
_COLLISION_SCAN_MODES = {2, 4, 5}

# Non-recursive modes: the manifest generator writes one entry per folder
# containing TIFFs. Recursive modes get a single entry for the root.
_PER_FOLDER_MODES = {0, 1}


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
        _error(
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
        # line becomes a "source" path containing ';' -- which the backend
        # then SPLITS into two roots (it splits --input-dir on ';'). Detect and refuse.
        if first_row is not None and len(first_row) == 1 and ';' in first_row[0]:
            _error(
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
                        _error(f"Invalid Mode value in manifest: {row[2].strip()!r} (row: {source})")
                        return None
                    if not 0 <= entry_mode <= 9:
                        _error(f"Mode out of range (0-9) in manifest: {entry_mode} (row: {source})")
                        return None
                # ';' is legal in Windows folder names, but the backend splits
                # --input-dir on ';', so one entry would arrive as two roots --
                # and in mode 8 the wrong root gets its sources deleted.
                if ';' in source or ';' in dest:
                    _error(
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
                    _error(
                        f"Path traversal in manifest entry: {source} -> {dest}\n"
                        f"Use absolute paths (or paths without '..') and run it again.")
                    return None
                entries.append((source, dest, entry_mode))

    if not entries:
        _say("Manifest is empty or only has comments.", "yellow")
        return None

    return entries


def generate_manifest(root: Path, mode: int) -> Optional[str]:
    """Generate a manifest CSV for `root` and return its path.

    Modes 0/1 are non-recursive, so they get one entry per folder containing
    TIFFs; recursive modes get a single entry for the root. Mode 2 pre-fills
    the Destination column with <root>/ZIP_flat; every other mode writes
    Destination == Source (ignored by the backend, edited by hand if needed).
    """

    root = Path(root)
    tiff_dirs = set()
    # Same exclusions as run_diagnose_tiffs: OLD_TIFFs holds the pristine originals the
    # compression modes archived -- a manifest entry pointing there would recompress the
    # backups, and in mode 8 DELETE them. Relative to the root, via _is_tool_output_path.
    for p in root.rglob("*"):
        try:
            if p.is_file() and tw.is_tiff_name(p.name):
                if _is_tool_output_path(p, root):
                    continue
                tiff_dirs.add(p.parent)
        except OSError:
            continue
    if not tiff_dirs:
        _say("No TIFF files found -- nothing to add to the manifest.", "yellow")
        return None

    if mode in _PER_FOLDER_MODES:
        folders = sorted(tiff_dirs, key=lambda d: str(d).lower())
    else:
        folders = [root]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_dir = INSTALL_DIR / "manifests"
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
    manifest_dir = INSTALL_DIR / "manifests"
    if not manifest_dir.exists():
        return None
    manifests = list(manifest_dir.glob("manifest_*.csv"))
    if not manifests:
        return None
    return str(sorted(manifests, key=lambda p: p.stat().st_mtime, reverse=True)[0])


def pick_manifest() -> Optional[str]:
    """Pick a manifest from manifests/ (newest first). With several files
    the user chooses; with one it is used directly."""
    manifest_dir = INSTALL_DIR / "manifests"
    if not manifest_dir.exists():
        return None
    manifests = sorted(manifest_dir.glob("manifest_*.csv"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        return None
    if len(manifests) == 1:
        return str(manifests[0])
    shown = manifests[:10]
    _say("Available manifests (newest first):", "bold")
    for i, m in enumerate(shown, 1):
        _say(f"  [{i}] {m.name}")
    choice = _ask("Which manifest?", "1", style="", choices=[str(i) for i in range(1, len(shown) + 1)])
    return str(shown[int(choice) - 1])


def confirm_manifest_entries(manifest_path: str, entries: List[Tuple]) -> bool:
    """Preview the entries and ask for a go-ahead."""
    _say(["\n", ("Manifest:", "bold cyan"), f" {manifest_path}"])
    _say([("Entries to process:", "bold"), f" {len(entries)}"])
    _table([{"name": "#", "justify": "right", "style": "dim"}, {"name": "Source", "style": "red"},
            {"name": "Destination", "style": "green"}, {"name": "Mode", "style": "magenta"}],
           [[str(i), truncate_path(Path(src)), truncate_path(Path(dst)), str(mode)]
            for i, (src, dst, mode) in enumerate(entries[:15], 1)],
           show_header=True, header_style="bold cyan")
    if len(entries) > 15:
        _say(f"... and {len(entries) - 15} more entries", "dim")
    _say("")
    return _ask_yes_no("Proceed with manifest?", True)


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
    """The output FOLDER compress_tiff_zip will write `f` to in `mode` (2/4/5 reach the
    collision scan): tw.resolve_output, the backend's own rule, with its folder settings
    (script or settings file, never a copy of their defaults -- audit round 28). Until v3.1
    the wizard had its own copy, which differed at a drive root (W-O4)."""
    if mode not in (2, 4, 5):
        return None
    opt = {name: str(_backend_default("compress_tiff_zip.py", name))
           for name in ("zip_suffix", "zip_subfolder_name")}     # all that modes 2/4/5 read
    opt["output_dir"] = dest_cell or ""
    found = tw.FoundFile(str(f), f.name, str(f.parent), 0, 0)
    out = tw.resolve_output(found, mode, str(src_root), opt)
    return Path(os.path.dirname(out)) if out else None


def manifest_output_collisions(entries: List[Tuple], exclude_names=()) -> List[Tuple]:
    """Find files that would be written to the same output folder with the
    same stem -- across different manifest entries OR within a single entry
    (mode 2 flattens recursively, so <src>/a/foto.tif and <src>/b/foto.tif
    share one destination). Within one entry the backend would apply
    duplicate_action, but with Skip/Overwrite that means a silently dropped
    or overwritten image, so flagging is deliberate.

    Only modes 2/4/5 are scanned -- the others write inside each Source's
    own tree (and overlapping Sources were already refused/warned by
    manifest_source_overlaps).

    The walk skips what the backend never reads: the tool folders (OLD_TIFFs, ZIP, ...)
    and the run's --exclude-folders names, at any level below the Source (v3.1 phase 6, W-O3:
    a.tif beside OLD_TIFFs\\a.tif aborted a mode 2 run as a collision).

    Returns a list of (file_a, file_b, dest_folder) tuples."""
    skip = set(_TOOL_OUTPUT_PARTS) | {str(n).lower() for n in exclude_names}
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
        _say(f"Collision check: scanning {src_root} ...", "dim")
        try:
            files = sorted(src_root.rglob("*"))
        except OSError:
            continue
        for f in files:
            try:
                if not f.is_file() or not tw.is_tiff_name(f.name):
                    continue
            except OSError:
                continue
            if tw.path_excluded(str(f.parent), str(src_root), skip):
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
                             workflow: Dict) -> List[str]:
    """Build the compress command for a single manifest entry, reusing
    build_compress_command with a per-entry workflow dict.

    The Destination column is only honored by mode 2 (--output-dir); every
    other mode computes its own output location. --delete-source goes only to
    mode-8 entries, and never in a dry run."""
    entry_wf = dict(workflow)
    entry_wf["mode"] = mode
    entry_wf["output_dir"] = dest_path if mode == 2 else None
    entry_wf["delete_source"] = (mode == 8 and not workflow.get("dry_run"))
    return build_compress_command(entry_wf, folders=[Path(source)])


def _manifest_ready(entries: List[Tuple], workflow: Dict, unattended: bool, dry_run: bool) -> bool:
    """The checks before a manifest runs: notes on Destinations a mode ignores and on a mode 2
    that flattens into its own Source; refusals for Source folders that overlap (asked, never
    unattended), outputs that collide, a Source that is missing, and mode 8 without its
    confirmation. False = do not run (said why)."""
    # The Destination column is only honored by mode 2 -- every other mode
    # computes its own output location from the backend's rules.
    ignored_dests = [(s, d, m) for s, d, m in entries
                     if m != 2 and d and Path(d) != Path(s)]
    if ignored_dests:
        _warn(f"Note: {len(ignored_dests)} manifest entry(ies) use modes that ignore the "
              f"Destination column (only mode 2 honors it) -- outputs follow the mode rules.")
        for _s, d, m in ignored_dests[:5]:
            print(f"  mode {m}: {d} (ignored)")

    # Mode 2 with Destination == Source flattens every TIFF into the input
    # root, mixing outputs with sources (same caveat as the wizard's mode 2).
    root_mix = [s for s, d, m in entries if m == 2 and Path(d) == Path(s)]
    if root_mix:
        _warn(f"Note: {len(root_mix)} mode-2 entry(ies) have Destination == Source: "
              f"all TIFFs land flattened in the input folder itself.")

    # Overlapping source trees: one entry's Source inside another's means the
    # same files are processed twice by SEPARATE child processes.
    overlaps = manifest_source_overlaps(entries)
    if overlaps:
        _warn(f"{len(overlaps)} manifest entry pair(s) have duplicate or nested "
              f"Source folders -- the same files would be processed twice:")
        for a, b in overlaps[:10]:
            print(f"  {a}  <->  {b}")
        if unattended:
            _error("Refused: overlapping sources need a confirmation, which cannot be "
                   "given unattended.")
            return False
        if not _ask_yes_no("Run anyway?", False):
            return False

    # Cross-entry duplicate outputs: refuse loudly instead of silently
    # compressing only one of the two sources.
    entry_modes = {m for _, _, m in entries}
    if entry_modes & _COLLISION_SCAN_MODES:
        collisions = manifest_output_collisions(
            entries, tw.parse_exclude_folders(workflow.get("exclude_folders") or ""))
        if collisions:
            _error(
                "Aborting: manifest entries would write different files to the same output.")
            for a, b, d in collisions[:10]:
                _say(f"  {a} + {b}  ->  same name in {d}", "red")
            if len(collisions) > 10:
                print(f"  ... and {len(collisions) - 10} more")
            _error(
                "Rename the inputs, pick another mode, or run the folder directly.")
            return False
    else:
        _say("Collision check: skipped (per-source output modes).", "dim")

    # A missing Source must fail closed: a folder that silently vanishes from
    # the run looks exactly like a folder that compressed cleanly.
    missing = [s for s, _, _ in entries if not Path(s).is_dir()]
    if missing:
        _error(f"{len(missing)} manifest Source folder(s) do not exist:")
        for s in missing[:10]:
            print(f"  {s}")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")
        return False

    # Mode 8 deletes sources. Gate the whole run with the same confirmation
    # the wizard enforces, ONCE, before anything runs. Without it,
    # build_manifest_entry_cmd never emits --delete-source.
    if not dry_run and any(m == 8 for _, _, m in entries):
        if unattended:
            # Defensive only: _run_journal refuses a deleting preset before it
            # reaches this call, because there is nobody to confirm. Fail closed
            # if a future caller ever gets here unattended.
            return False
        if not _ask_yes_no("Manifest contains mode-8 entries: source TIFFs will be DELETED after "
                           "compression. Are you sure?", False, "red"):
            _say("Cancelled.", "dim")
            return False

    return True


def execute_manifest_workflow(cfg, entries: List[Tuple], workflow: Dict,
                              unattended: bool = False) -> bool:
    """Execute a compress workflow from manifest entries.

    `entries` must be mode-resolved (no None modes -- the caller replaced
    them with the run's default mode). One child process per entry; a failed
    entry does not stop the rest. Returns True only when every entry exited
    cleanly.

    `unattended` (--run-preset) never asks: overlapping Sources refuse instead
    of offering "Run anyway?", each child gets a closed stdin (a backend never asks
    without a console), and the mode-8 gate below stays as a defensive refusal (the runner
    already refused a deleting preset before calling this)."""
    if not entries:
        _error("No manifest entries found!")
        return False

    dry_run = workflow.get("dry_run", False)

    if not _manifest_ready(entries, workflow, unattended, dry_run):
        return False

    total_entries = len(entries)
    ok_count = 0
    error_count = 0
    entry_reports: List[Dict] = []

    _say(f"\nExecuting manifest: {total_entries} entry(ies)", "bold cyan")
    if dry_run:
        _say("DRY RUN MODE", "yellow")
    _say("")

    for i, (source, dest_path, mode) in enumerate(entries, 1):
        _say([f"[{i}/{total_entries}] ", (f"Mode {mode}", "bold"),
              f" | {truncate_path(Path(source), 40)} -> {truncate_path(Path(dest_path), 40)}"])

        cmd = build_manifest_entry_cmd(source, dest_path, mode, workflow)
        if unattended:
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

    # Combined wrapper log: the per-entry child logs live in Logs/compress_tiff_zip/ under the
    # current folder (where every backend and Diagnose write theirs), so before this file
    # there was no single place holding the run's per-entry outcome. It goes there too, and
    # names the children's folder by its full path (v3.1 phase 6, W-O14: it went next to the
    # scripts and pointed at a relative folder that only existed under the current one).
    try:
        log_dir = Path.cwd() / "Logs" / "convert_tiff"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(f"{TOOLKIT_NAME} {TOOLKIT_VERSION}\n")
            f.write("\n".join(lines))
            f.write(f"\n\nChild logs: {Path.cwd() / 'Logs' / 'compress_tiff_zip'} (one per entry)\n")
        print(f"Combined log: {log_path}")
    except OSError:
        pass


def run_manifest_workflow(cfg) -> bool:
    """Menu option 3: run the compress workflow from a manifest CSV."""
    manifest_path = pick_manifest()
    if not manifest_path or not Path(manifest_path).exists():
        _say("No manifest found. Generate one from workflow [1] (Compress TIFFs).", "yellow")
        return False

    entries = load_manifest_entries(manifest_path)
    if entries is None:
        return False

    # Rows without a Mode cell inherit a default mode asked once per run.
    default_mode = None
    if any(m is None for _, _, m in entries):
        default_mode = int(_ask("Default mode for manifest rows without a Mode cell",
                                str(cfg.config.last_mode or 0), choices=[str(m) for m in MODE_NAMES]))
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
        dup_default = _backend_default("compress_tiff_zip.py", "duplicate_action")
        workflow["duplicate_action"] = _ask("Duplicate filenames (mode 2)", dup_default,
                                            choices=["Numbered", "Skip", "Overwrite"])

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

def _echo_command(cmd, lead: str = "\n") -> None:
    """The line shown before a backend starts: the command line it gets."""
    _say(f"{lead}Running: {' '.join(str(a) for a in cmd)}\n", "dim")


def _offer_real_run(failed: bool, question: str, announce: str, run_real):
    """After a dry run: say so when it had errors, ask whether to run it for real (default
    no) and, if so, announce it and return what `run_real()` returns; None when skipped."""
    if failed:
        _say("Dry-run completed with errors. Review output above before running for real.", "yellow")
    if not _ask_yes_no(f"\nDry-run complete. {question}", False, "yellow"):
        _say("Skipped.", "dim")
        return None
    _say(f"{announce}\n", "cyan")
    return run_real()


def _run_exif_then_compress(cmd_copy: List[str], cmd_compress: List[str]):
    """Workflow 4's two runs, (exit code of Copy EXIF, of compress). Compress runs only when
    Copy EXIF exited 0 -- it would ZIP TIFFs whose metadata never landed; its code is then
    None. The dry run and the run for real both go through here."""
    _say("\n=== Step 1/2: Copy EXIF ===", "cyan")
    _echo_command(cmd_copy, lead="")
    rc_copy = run_subprocess(cmd_copy)
    if rc_copy != 0:
        _error("Step 1 (Copy EXIF) failed. Skipping Step 2 (Compress).")
        return rc_copy, None
    _say("\n=== Step 2/2: Fuji: Compress ===", "cyan")
    _echo_command(cmd_compress, lead="")
    return rc_copy, run_subprocess(cmd_compress)


def _ask_flat_output(folder: Path, workflow: Dict) -> bool:
    """Mode 2's own questions. Mode 2 flattens every TIFF into one folder; without an
    explicit output folder the backend falls back to the input root, mixing outputs with
    sources and recompressing root-level TIFFs in place -- so it is asked here, with the
    policy for names that collide. False = cancelled or refused."""
    default_out = str(folder / "ZIP_flat")
    out_dir = _ask("Output folder (mode 2 merges all TIFFs here)", default_out)
    if len(out_dir) >= 2 and out_dir[0] == out_dir[-1] and out_dir[0] in ("'", '"'):
        out_dir = out_dir[1:-1]
    if ";" in out_dir:
        _error(f"Output path contains ';' which is not supported: {out_dir}")
        return False
    if Path(out_dir).resolve() == folder.resolve():
        warn = "Output folder is the input folder: TIFFs will be recompressed in place, without a backup."
        if not _ask_yes_no(f"{warn} Continue?", False, "yellow"):
            _say("Cancelled.", "dim")
            return False
    else:
        workflow["output_dir"] = out_dir

    # Collision policy for the flattened output
    dup_default = _backend_default("compress_tiff_zip.py", "duplicate_action")
    workflow["duplicate_action"] = _ask("Duplicate filenames", dup_default,
                                        choices=["Numbered", "Skip", "Overwrite"])
    return True


def _ask_thumbnails(workflow: Dict) -> None:
    """The embedded-thumbnail questions. The defaults come from the backend's own PARAMS
    table; "Configure? No" adds no thumb_* keys at all, so the backend runs on its own
    settings (audit round 17 -- the wizard used to force 256/"85"/jpg always)."""
    thumb_size_default = _backend_default("compress_tiff_zip.py", "thumb_size")
    thumb_quality_default = _backend_default("compress_tiff_zip.py", "thumb_quality")
    thumb_format_default = _backend_default("compress_tiff_zip.py", "thumb_format")
    if not _ask_yes_no("Generate embedded thumbnails?", False, "cyan"):
        return
    workflow["generate_thumbnail"] = True
    if _ask_yes_no("Configure thumbnail settings?", False, "cyan"):
        size_str = _ask("Thumbnail size (px)", str(thumb_size_default))
        workflow["thumb_size"] = int(size_str) if size_str.isdigit() else thumb_size_default
        workflow["thumb_quality"] = _ask("JPEG quality", thumb_quality_default)
        # audit round 16: --thumb-format only ever gets one of the formats the backend takes
        workflow["thumb_format"] = _ask("Format", thumb_format_default, choices=["jpg", "png", "tif"])
    # Option to skip compressed TIFFs with existing thumbnails
    workflow["skip_compressed_with_thumb"] = _ask_yes_no(
        "Skip already-compressed TIFFs that have thumbnails?", False, "cyan")


def run_free_compress(cfg: "ConfigManager") -> bool:
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
    if _ask_yes_no("Generate a manifest CSV for this folder instead of running now?", False, "cyan"):
        manifest_path = generate_manifest(folder, mode)
        if manifest_path:
            cfg.config.last_manifest_path = manifest_path
            cfg.save_config()
            _say([("Manifest saved to:", "green"), f" {manifest_path}"])
            _say("Edit in Excel, then run it from the main menu: [3] Run from manifest", "dim")
        return True

    if mode == 2 and not _ask_flat_output(folder, workflow):
        return False

    # Basic params
    step_basic_params(cfg, workflow)

    _ask_thumbnails(workflow)

    # For mode 8: confirm delete
    if mode == 8:
        if not _ask_yes_no("Mode 8 will DELETE source TIFFs after compression. Are you sure?", False, "red"):
            _say("Cancelled.", "dim")
            return False
        workflow["delete_source"] = True
    else:
        workflow["delete_source"] = False

    # Summary + confirm
    if not step_confirm(workflow, cfg):
        return False

    cfg.config.last_workers = workflow["workers"]
    cfg.save_config()

    cmd = build_compress_command(workflow, folders=[folder])
    journal_wf = dict(workflow)
    journal_wf["dry_run"] = False
    journal_cmd = build_compress_command(journal_wf, folders=[folder])
    label = f"Compress TIFFs -- mode {mode} -- {truncate_path(folder, 60)}"
    if workflow.get("delete_source"):
        label += " [DELETES SOURCES]"
    _save_last_run(cfg, "compress", label, [journal_cmd])
    _echo_command(cmd)
    result = run_subprocess(cmd)

    if workflow.get("dry_run"):
        def real():
            workflow["dry_run"] = False
            return run_subprocess(build_compress_command(workflow, folders=[folder]))
        _offer_real_run(result != 0, "Run for real now?", "Running for real (DryRun disabled)...", real)

    return True


def _run_exif_or_compress(cfg: "ConfigManager", workflow_type: str) -> bool:
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
        _say("No folders found matching pattern. Workflow cancelled.", "yellow")
        return False
    workflow["folders"] = found_folders

    # Basic params
    step_basic_params(cfg, workflow)

    # Summary + confirm
    if not step_confirm(workflow, cfg):
        return False

    cfg.config.last_workers = workflow["workers"]
    cfg.save_config()

    def run_and_prompt(cmd_list, folders):
        _echo_command(cmd_list)
        run_result = run_subprocess(cmd_list)
        if workflow.get("dry_run"):
            def real():
                workflow["dry_run"] = False
                return run_subprocess(
                    build_compress_command(workflow, folders=folders)
                    if "compress" in workflow_type or workflow_type == "both"
                    else build_copy_exif_command(workflow, folders=folders))
            _offer_real_run(run_result != 0, "Run for real now?", "Running for real...", real)

    journal_wf = dict(workflow)
    journal_wf["dry_run"] = False
    wf_names = {"copy_exif": "Fuji: Copy EXIF", "compress": "Fuji: Compress", "both": "Fuji: Copy + Compress"}
    label = (f"{wf_names[workflow_type]} -- pattern '{';'.join(patterns)}' under "
             f"{truncate_path(root, 60)} ({len(found_folders)} folders)")
    if workflow_type == "copy_exif":
        journal_cmds = [build_copy_exif_command(journal_wf, folders=found_folders)]
    elif workflow_type == "compress":
        journal_cmds = [build_compress_command(journal_wf, folders=found_folders)]
    else:
        journal_step1 = dict(journal_wf)
        journal_step1["compress_zip"] = False
        journal_cmds = [
            build_copy_exif_command(journal_step1, folders=found_folders),
            build_compress_command(journal_wf, folders=found_folders),
        ]
    _save_last_run(cfg, workflow_type, label, journal_cmds,
                   autofind={"root": str(root), "patterns": list(patterns)})

    if workflow_type == "copy_exif":
        cmd = build_copy_exif_command(workflow, folders=found_folders)
        run_and_prompt(cmd, found_folders)
        return True
    elif workflow_type == "compress":
        cmd = build_compress_command(workflow, folders=found_folders)
        run_and_prompt(cmd, found_folders)
        return True
    else:  # both
        step1_workflow = workflow.copy()
        step1_workflow["compress_zip"] = False
        rc_copy, rc_compress = _run_exif_then_compress(
            build_copy_exif_command(step1_workflow, folders=found_folders),
            build_compress_command(workflow, folders=found_folders))
        if rc_compress is None:
            return False

        if workflow.get("dry_run"):
            def real():
                workflow["dry_run"] = False
                copy_workflow = workflow.copy()
                copy_workflow["compress_zip"] = False
                return _run_exif_then_compress(
                    build_copy_exif_command(copy_workflow, folders=found_folders),
                    build_compress_command(workflow, folders=found_folders))[1] is not None
            if _offer_real_run(rc_compress != 0, "Run both steps for real now?", "Running for real...",
                               real) is False:
                return False
        return True


def run_generate_thumbnails(cfg: "ConfigManager") -> bool:
    """Workflow 8: Generate sRGB thumbnails from TIFFs."""
    script = PACKAGE_DIR / "generate_thumbnails.py"
    if not script.exists():
        _error("generate_thumbnails.py not found.")
        return False

    # Input directory
    input_dir = _ask("Input directory", str(cfg.config.last_input_dir or "."))
    input_dir = (input_dir or "").strip().strip('"').strip("'")
    p = Path(input_dir)
    if not p.is_dir():
        _error(f"Directory does not exist: {input_dir}")
        return False
    # The backend splits --input-dir on ';' like the other three, so a path containing one
    # would silently become two bogus roots. step_folder rejects it for every other workflow.
    if ";" in str(p):
        _error(f"Folder path contains ';' which is not supported: {p}")
        return False
    cfg.config.last_input_dir = os.path.abspath(str(p))
    cfg.save_config()
    # audit round 28: the backend gets (and a preset saves) the absolute path, never "."
    # or a relative answer -- see step_folder.
    input_dir = os.path.abspath(p)

    # Remove mode: skip the generation questions entirely
    remove_mode = _ask_yes_no("Remove existing thumbnails instead of creating them?", False, "yellow")

    # Recursive / dry-run apply to both modes
    def _ask_recursive_and_dry():
        return (_ask_yes_no("Recursive?", False, "cyan"), _ask_yes_no("Dry-run?", False, "cyan"))

    def _ask_exclude_folders():
        """Same question the compress/Copy EXIF workflows ask. The backend used to carry a
        hardcoded, silent exclusion list instead (ZIP and _EXPORT included, which are exactly
        where the compressed library and the export trees live)."""
        default = cfg.config.last_exclude_folders or ""
        value = _ask([("Exclude folders?", "cyan"), " (';'-separated folder names, '-' = none)"],
                     default or "-", style="")
        if value in ("-", "", "none"):
            cfg.config.last_exclude_folders = None
            return ""
        cfg.config.last_exclude_folders = value
        return value

    def _ask_output_dir():
        value = _ask("Output folder (empty = next to each TIFF)", "")
        value = value.strip('"').strip("'")
        if ";" in value:
            _error(f"Output path contains ';' which is not supported: {value}")
            return None
        return value

    if remove_mode:
        out_dir = _ask_output_dir()
        if out_dir is None:
            return False
        recursive, dry_run = _ask_recursive_and_dry()
        cmd = _backend_command("generate_thumbnails.py", tw.cli_value("input_dir", input_dir)
                               + [tw.cli_switch("remove", True)])
        if out_dir:
            cmd += tw.cli_value("output_dir", out_dir)
        if recursive:
            cmd += [tw.cli_switch("recursive", True)]
        if dry_run:
            cmd += [tw.cli_switch("dry_run", True)]
        _save_last_run(cfg, "thumbnails_remove", f"Thumbnails: REMOVE under {truncate_path(p, 60)}",
                       [[a for a in cmd if a != tw.cli_switch("dry_run", True)]])
        _say(f"\nRunning: {' '.join(cmd)}\n", "dim")
        return run_subprocess(cmd) == 0

    # The four prompt defaults come from generate_thumbnails.py's own PARAMS table
    # (audit round 17): a literal here silently overrode an edited backend setting.
    size_default = _backend_default("generate_thumbnails.py", "size")
    quality_default = _backend_default("generate_thumbnails.py", "quality")
    format_default = _backend_default("generate_thumbnails.py", "format")
    page_default = _backend_default("generate_thumbnails.py", "page")

    # Thumbnail size (backend accepts 32-4096)
    size_str = _ask("Thumbnail size (px, 32-4096)", str(size_default))
    try:
        size = int(size_str)
    except ValueError:
        size = size_default
    if not (32 <= size <= 4096):
        _say(f"Size {size} out of range (32-4096), using {size_default}.", "yellow")
        size = size_default

    # Quality: the backend validates 1-100 and refuses to start otherwise
    quality_str = _ask("JPEG quality (1-100)", quality_default)
    if not (quality_str.isdigit() and 1 <= int(quality_str) <= 100):
        _say(f"Invalid quality '{quality_str}' (must be 1-100), using {quality_default}.", "yellow")
        quality_str = quality_default

    # Format and page: what the backend offers, asked again until it is one of them
    fmt = _ask("Format", format_default, choices=["jpg", "png", "tif"])
    page = _ask("Page", page_default, choices=["0", "all"])

    out_dir = _ask_output_dir()
    if out_dir is None:
        return False
    recursive, dry_run = _ask_recursive_and_dry()
    exclude_folders = _ask_exclude_folders() if recursive else ""

    cmd = _backend_command("generate_thumbnails.py",
                           tw.cli_value("input_dir", input_dir)
                           + tw.cli_value("size", size)
                           + tw.cli_value("quality", quality_str)
                           + tw.cli_value("format", fmt)
                           + tw.cli_value("page", page)
                           + tw.cli_value("workers", clamp_workers(cfg.config.last_workers
                                                                   or cfg.config.default_workers)))
    if out_dir:
        cmd += tw.cli_value("output_dir", out_dir)
    if exclude_folders:
        cmd += tw.cli_value("exclude_folders", exclude_folders)
    if recursive:
        cmd += [tw.cli_switch("recursive", True)]
    if dry_run:
        cmd += [tw.cli_switch("dry_run", True)]

    _save_last_run(cfg, "thumbnails", f"Thumbnails: {size}px {fmt} under {truncate_path(p, 60)}",
                   [[a for a in cmd if a != tw.cli_switch("dry_run", True)]])

    _say(f"\nRunning: {' '.join(cmd)}\n", "dim")
    return run_subprocess(cmd) == 0

# --- Main ---------------------------------------------------------


def _run_journal(cfg, entry: Dict, title: str, unattended: bool = False,
                 dry_run: bool = False) -> int:
    """Run a journal entry -- the last run (Repeat) or a preset. 0 = ran (or nothing
    to do), 1 = failed or refused. `unattended` (--run-preset): no prompt at all,
    stdin closed; a run that deletes sources is refused unless
    dry_run. `dry_run` comes only from the command line, never from the entry."""
    if not _journal_runnable(entry):
        _say("Nothing saved to run.", "yellow")
        return 1

    if entry.get("kind") == "diagnose":
        return _run_diagnose_journal(cfg, entry, title, unattended=unattended,
                                     dry_run=dry_run)
    if entry.get("kind") == "manifest":
        return _run_manifest_journal(cfg, entry, title, unattended, dry_run)
    return _run_command_journal(entry, title, unattended, dry_run)


def _run_manifest_journal(cfg, entry: Dict, title: str, unattended: bool, dry_run: bool) -> int:
    """A manifest repeat/preset: the CSV read again, run with the settings and the default
    mode the run saved. 0 = ran cleanly, 1 = failed, refused or cancelled."""
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
        _say(["\n", (title, "bold cyan"), f": {Path(manifest_path).name}"])
        _say(f"  {len(entries)} entry(ies)")
        if any(m == 8 for _, _, m in entries):
            _say("WARNING: manifest contains mode-8 entries (DELETES sources).", "red")
        if not _ask_yes_no("Run it now?", False):
            _say("Cancelled.", "dim")
            return 1
    ok = execute_manifest_workflow(cfg, entries, workflow, unattended=unattended)
    return 0 if ok else 1


def _redo_autofind(entry: Dict, steps: List[JournalStep]) -> Optional[int]:
    """AutoFind is redone on every Fuji journal run: the root and the patterns were saved
    with it, so a scheduled preset sees the sessions added since it was saved (a journal from
    an older version has no such data and replays what it saved). Only the Fuji workflows
    (2/3/4) search; their labels all start with "Fuji:". Menu 1 also journals kind "compress"
    but has a fixed folder and no AutoFind. The found sessions replace the --input-dir of
    each step; returns the exit code to stop with (1: the root is gone, 0: nothing found),
    or None to go on."""
    af = entry.get("autofind")
    is_fuji = (entry.get("kind") in ("copy_exif", "compress", "both")
               and str(entry.get("label") or "").startswith("Fuji:"))
    if not is_fuji:
        return None
    if not af:
        _say(f"Folders as saved on {entry.get('time')} (saved by an older version: "
             f"sessions added since are not included)", "yellow")
        return None
    root = Path(af["root"])
    if not root.is_dir():
        _say(f"AutoFind root is gone: {root}", "red")
        return 1
    folders = list(find_folders_by_pattern(root, af["patterns"]))     # its own order
    if not folders:
        _say(f"AutoFind: no matching folders under {root} -- nothing to do.", "yellow")
        return 0
    folder_str = ";".join(str(f) for f in folders)
    for step in steps:
        at = _option_at(step.args, "input_dir")
        if at is not None:
            i, words, _old = at
            step.args[i:i + words] = tw.cli_value("input_dir", folder_str)
    _say(f"AutoFind: {len(folders)} session(s) under {root}")
    return None


def _run_journal_steps(run_cmds: List[List[str]], unattended: bool) -> int:
    """The saved commands in order; a step that fails stops the rest (Copy EXIF must land
    before compress runs). 0 = every step exited 0."""
    for i, c in enumerate(run_cmds):
        if len(run_cmds) > 1:
            _say(f"\n=== Step {i + 1}/{len(run_cmds)} ===", "cyan")
        result = run_subprocess(c, stdin=subprocess.DEVNULL) if unattended else run_subprocess(c)
        if result != 0:
            _say(f"Step {i + 1} failed (exit {result}). Remaining steps skipped.", "red")
            return 1
    return 0


def _run_command_journal(entry: Dict, title: str, unattended: bool, dry_run: bool) -> int:
    """A journal of backend command lines (workflows 1-4 and 8), each [subcommand, *args],
    run by this Python with the backend next to this wizard -- the journal may have been
    written by another copy (see _journal_line)."""
    parsed = [_journal_step(c) for c in entry["commands"]]
    steps = [s for s in parsed if s is not None]
    if len(steps) != len(parsed):
        _say("Saved workflow cannot be replayed: its command line was recorded in a "
             "form this version does not recognise (an older version, or edited by hand). "
             "Run the workflow again from the menu and save it again.", "red")
        return 1

    stop = _redo_autofind(entry, steps)
    if stop is not None:
        return stop

    # What each step deletes is read from what its backend binds.
    deletes = (not dry_run) and any(_step_replaces_sources(s) for s in steps)
    if dry_run:
        for s in steps:
            if not s.values.get("dry_run"):
                s.args.append(tw.cli_switch("dry_run", True))
    if unattended and deletes:
        _say("Refused: this preset deletes or replaces source files (--delete-source or "
             "mode 8). That needs a confirmation, which cannot be given unattended -- run "
             "it from the menu, or pass --dry-run.", "red")
        return 1

    run_cmds = [_backend_command(s.script, s.args) for s in steps]

    _say(f"\n{title}", "bold cyan")
    _say(f"  {entry.get('label') or ''}")
    _say(["  ", (f"saved {entry.get('time')}", "dim")])
    for c in run_cmds:
        _say(f"\n{' '.join(c)}", "dim")
    if dry_run:
        _say("Note: --dry-run requested -- this is a simulation.", "yellow")
    else:
        _say("Note: dry-run is never inherited -- this runs for real.", "yellow")
    if deletes:
        _say("WARNING: this workflow DELETES or REPLACES source files (--delete-source / mode 8).", "red")
    if not unattended and not _ask_yes_no("Run it now?", False):
        _say("Cancelled.", "dim")
        return 1
    return _run_journal_steps(run_cmds, unattended)


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


# --- Script settings menu (main menu 6) ------------------------------

#: (overlay section, menu label, script whose OVERLAY_SPECS and PARAMS help it shows)
_OVERLAY_MENU = [
    ("compress_tiff_zip", "Compress TIFFs (compress_tiff_zip.py)", "compress_tiff_zip.py"),
    ("copy_exif", "Copy EXIF (copy_exif_to_tiff.py)", "copy_exif_to_tiff.py"),
    ("generate_thumbnails", "Generate thumbnails (generate_thumbnails.py)", "generate_thumbnails.py"),
    ("convert_tiff", "This wizard (convert_tiff.py)", None),
]

_CONVERT_SETTING_HELP = {
    "WORKER_MEMORY_FRACTION": "Share of the available memory the Python workflows' parallel workers may use; 0 = no cap.",
    "WORKER_MEMORY_LIMIT": "Which memory reading that share is taken from: both, commit or physical.",
    "DIAGNOSE_CACHE": "Diagnose remembers each TIFF's verdict between runs.",
    "CONVERT_TIMEOUT_S": "Base timeout (seconds) of a magick depth/format conversion.",
    "COMPARE_TIMEOUT_S": "Base timeout (seconds) of a magick compare (RMSE).",
    "TIMEOUT_S_PER_MB": "Seconds added to a whole-file magick/exiftool timeout per MB of input.",
}


def _convert_tiff_script_values() -> Dict[str, object]:
    """The value each overlay-able convert_tiff.py setting has WRITTEN in this file (the live
    module value may already be the overlay's)."""
    try:
        text = Path(__file__).read_text(encoding="utf-8")
    except OSError:
        return {}
    out: Dict[str, object] = {}
    for name in _OVERLAY_SETTINGS:
        m = re.search(rf"^{name} = (.+?)\s*(?:#.*)?$", text, re.M)
        if m:
            try:
                out[name] = ast.literal_eval(m.group(1))
            except (ValueError, SyntaxError):
                pass
    return out


def _read_overlay_data(path: Path):
    """(data, problem): the whole overlay file as a dict ({} when it does not exist yet), or
    ({}, why) when it exists and cannot be read as a JSON object -- never to be overwritten."""
    if not path.is_file():
        return {}, None
    try:
        # The same hook as the scripts' reader: a file they refuse (keys that differ only in
        # case, an empty key) is refused here too, never opened for editing (v3.1, W-O6).
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_overlay_object_pairs)
    except (OSError, ValueError) as e:
        return {}, str(e)
    if not isinstance(data, dict):
        return {}, "it is not a JSON object"
    return data, None


def _overlay_menu_rows(section: str, script: Optional[str]):
    """[(name, spec, script value, help)] of one section, in the script's own order."""
    if script is None:
        values = _convert_tiff_script_values()
        return [(n, s, values.get(n), _CONVERT_SETTING_HELP.get(n, ""))
                for n, s in _OVERLAY_SETTINGS.items()]
    defaults = _backend_param_defaults(script)
    helps = _backend_param_help(script)
    return [(n, s, defaults.get(n), helps.get(n, "")) for n, s in _backend_overlay_specs(script).items()]


def _v31_name(name: str) -> str:
    """The name a backend setting of v3.0.0 has since v3.1: ZipSubfolderName ->
    zip_subfolder_name (T7). Anything else comes back lower-cased, matching nothing."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _overlay_json_value(spec: str, parsed, checked):
    """What the file stores: the JSON value the user meant (an intstr is a JSON int; a path
    left empty is null), normalised the way the scripts read it."""
    kind = spec.partition(":")[0]
    if kind == "intstr":
        return parsed
    if kind == "path?":
        return None if parsed is None else checked
    return checked


def _edit_overlay_section(path: Path, section: str, label: str, script: Optional[str]) -> None:
    rows = _overlay_menu_rows(section, script)
    if not rows:
        _say(f"No setting of {label} can be changed from the settings file "
                f"(its OVERLAY-SPECS block could not be read).")
        return
    while True:
        data, problem = _read_overlay_data(path)
        if not problem and data.get(section) is not None and not isinstance(data.get(section), dict):
            problem = f"the '{section}' entry is not an object"
        if problem:
            _say(f"{path} cannot be read ({problem}). Fix or delete it first; nothing was "
                    f"written, and the scripts ignore it meanwhile.")
            return
        sect = data.get(section) or {}
        _say(f"{label} -- its settings ('script' = the value written in the script, "
                f"'file' = this settings file):")
        for n, (name, spec, script_value, help_text) in enumerate(rows, 1):
            if name in sect:
                ok, _checked = _overlay_value(spec, sect[name])
                file_part = json.dumps(sect[name], ensure_ascii=False) + ("" if ok else "  (REFUSED: the scripts keep their own value)")
            else:
                file_part = "-"
            _say(f"  {n:>2}. {name} = script: {json.dumps(script_value, ensure_ascii=False)} | "
                    f"file: {file_part} | {spec}")
            if help_text:
                _say(f"        {help_text}")
        # A name the scripts do not take is ignored by them with a warning at every run; say
        # it here too -- above all the names of v3.0.0 (T7), which look like settings.
        known = {r[0] for r in rows}
        stray = [k for k in sect if k not in known]
        if stray:
            _warn("In the file but not a setting of this script, so ignored: " + "; ".join(
                f"{k} (written {_v31_name(k)} since v3.1: change it here and the old name goes)"
                if _v31_name(k) in known else k for k in stray))
        pick = _ask("Number to change (0 = back)", "0")
        if pick in ("", "0"):
            return
        if not pick.isdigit() or not 1 <= int(pick) <= len(rows):
            _say(f"No setting {pick!r}.")
            continue
        name, spec, script_value, help_text = rows[int(pick) - 1]
        expected = _overlay_value(spec, object())[1]
        text = _ask(f"New value for {name} ({expected}; '-' = back to the script's value; "
                       f"empty = no change)", "")
        if text == "":
            continue
        # the same setting under its name of v3.0.0 goes with any change of it
        old_names = [k for k in sect if k != name and _v31_name(k) == name]
        if text == "-":
            if name not in sect and not old_names:
                continue
            new_sect = {k: v for k, v in sect.items() if k != name and k not in old_names}
            stored = None
        else:
            try:
                parsed = _parse_setting_text(spec, text)
            except ValueError as e:
                _say(f"Not changed: {e}.")
                continue
            ok, checked = _overlay_value(spec, parsed)
            if not ok:
                _say(f"Not changed: {name} must be {checked}.")
                continue
            stored = _overlay_json_value(spec, parsed, checked)
            new_sect = {k: v for k, v in sect.items() if k not in old_names}
            new_sect[name] = stored
        if new_sect:
            data[section] = new_sect
        else:
            data.pop(section, None)
        try:
            _write_json_file(path, data, ".tiff_workflow_settings_", ensure_ascii=False)
        except OSError as e:
            _say(f"Could not write {path}: {e}")
            return
        _say(f"{name}: " + ("back to the script's value" if text == "-"
                               else f"{json.dumps(stored, ensure_ascii=False)} (settings file)"))
        # discovery skips this toolkit's own folders: a changed folder name counts at once
        _refresh_tool_output_parts()
        if section == "convert_tiff":
            _say("This wizard's own settings apply the next time the wizard starts.")


def script_settings_menu(cfg) -> None:
    """Main menu 6: the scripts' own settings (folder names, timeouts, thumbnail defaults, ...)
    changed in `.tiff_workflow_settings.json` instead of the .py files. Every script
    checks each value again when it starts and never takes a safety or per-run setting from
    the file (mode, safe_mode, overwrite, delete_source, workers, ...)."""
    env = os.environ.get(_SETTINGS_OVERLAY_ENV)
    if env is not None and not env.strip():
        _say(f"The settings overlay is OFF: {_SETTINGS_OVERLAY_ENV} is set and empty. "
                f"Unset it to use a settings file.")
        return
    path = _settings_overlay_path()
    if path is None:                # the variable is set and empty: said above
        return
    data, problem = _read_overlay_data(path)
    if problem:
        _say(f"{path} cannot be read ({problem}). Fix or delete it first; nothing was "
                f"written, and the scripts ignore it meanwhile.")
        return
    _say("Script settings -- the scripts' own defaults, without editing them.")
    _say(f"File: {path}" + ("" if path.is_file() else "  (does not exist yet; the first change creates it)"))
    _say("A change applies to the next run of that script. A value typed on a command line, "
            "and everything this wizard asks per run, still wins. Deletion, overwriting, safe "
            "mode and the other safety settings are never taken from this file.")
    while True:
        for n, (_section, label, _script) in enumerate(_OVERLAY_MENU, 1):
            _say(f"  {n}. {label}")
        pick = _ask("Which script's settings (0 = back)", "0")
        if pick in ("", "0"):
            return
        if not pick.isdigit() or not 1 <= int(pick) <= len(_OVERLAY_MENU):
            _say(f"No entry {pick!r}.")
            continue
        section, label, script = _OVERLAY_MENU[int(pick) - 1]
        _edit_overlay_section(path, section, label, script)


def presets_menu(cfg) -> None:
    """Named snapshots of the last workflow (main menu option 5), so several
    recurring jobs (a nightly manifest sync, a one-off shoot) can coexist
    instead of overwriting the single 'last workflow' slot. Saving stores a copy
    of the journal; running goes through the same _run_journal as 'Repeat last
    workflow', so the two cannot drift apart."""
    while True:
        presets = dict(cfg.config.presets or {})
        names = sorted(presets)

        if names:
            _table([{"name": "#", "justify": "right", "style": "dim"}, {"name": "Name", "style": "bold"},
                    {"name": "Workflow"}, {"name": "Saved", "style": "dim"}],
                   [[str(i), name, presets[name].get("label", ""), presets[name].get("saved_at", "")]
                    for i, name in enumerate(names, 1)],
                   panel={"title": ("Presets", "bold"), "border_style": "green"},
                   show_header=True, header_style="bold cyan", box=BOX_SIMPLE)
        else:
            _say("\nNo presets saved yet.", "dim")
        _say([("[number]", "bold"), " run  ", ("[S]", "bold"), " save last workflow as preset  ",
              ("[D]", "bold"), " delete  ", ("[B]", "bold"), " back"])
        choice = _ask("Presets", style="")

        upper = choice.upper()
        if not choice or upper == "B":
            return

        if upper == "S":
            snapshot = _journal_snapshot(cfg)
            if not _journal_runnable(snapshot):
                _say("Nothing to save yet: run a workflow first.", "yellow")
                continue
            _say(snapshot.get("label") or "", "dim")
            raw_name = _ask("Preset name", style="")
            name = _clean_preset_name(raw_name)
            if not name:
                _say("Invalid name (empty, too long, or contains control characters).", "red")
                continue
            if name in presets and not _ask_yes_no("Replace it?", False):
                continue
            presets[name] = {**snapshot, "saved_at": time.strftime("%Y-%m-%d %H:%M")}
            cfg.config.presets = presets
            cfg.save_config()
            _say(f"Preset '{name}' saved.", "green")
            continue

        if upper == "D":
            if not names:
                continue
            target = _ask("Delete which number?", style="")
            if not target.isdigit() or not 1 <= int(target) <= len(names):
                _say("No preset with that number.", "red")
                continue
            name = names[int(target) - 1]
            if _ask_yes_no(f"Delete preset '{name}'?", False):
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


def _overlay_notice() -> None:
    """What the settings file changed in THIS file's own settings (applied at import), and every
    entry it refused -- said once at start-up, never when there is nothing to say."""
    result = _SETTINGS_OVERLAY
    if result.get("applied"):
        _say(f"Script settings overlay: {result['path']}: {len(result['applied'])} value(s): "
             f"{', '.join(sorted(result['applied']))}")
    for warning in result.get("warnings", ()):
        _say(f"WARNING: settings overlay: {warning}", "yellow")


def main(argv: Optional[List[str]] = None) -> None:
    """The wizard: the menu, or what --run-preset, --list-presets or --version ask (`argv`,
    sys.argv[1:] when None). A backend's subcommand never comes here: tiff_workflow/cli.py
    runs it without importing this module."""
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
        prog="convert_tiff.py",
        description="TIFF Workflow Manager -- interactive wizard for TIFF compression, "
                    "EXIF copy, diagnostics and cleanup.",
        epilog="Run without arguments to start the interactive wizard. A backend runs "
               "directly as `convert_tiff.py compress|copy-exif|thumbnails [options]` "
               "(--help after the name lists its options). "
               "See README.md and docs/README_convert_tiff_py.md for full documentation.",
    )
    parser.add_argument("--run-preset", metavar="NAME",
                        help="Run a saved preset without the menu (Task Scheduler) and exit")
    parser.add_argument("--list-presets", action="store_true",
                        help="List saved presets and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --run-preset: simulate instead of running")
    parser.add_argument("--version", action="version",
                        version=f"{TOOLKIT_NAME} {TOOLKIT_VERSION}",
                        help="Print the release and exit")
    args = parser.parse_args(argv)

    # A stray --dry-run would otherwise be ignored -- the worst outcome is a real
    # run for someone who asked for a simulation. Same rule as jxl-photo.
    if args.dry_run and not args.run_preset:
        parser.error("--dry-run only applies together with --run-preset")

    cfg = ConfigManager()

    # Read-only listing: no environment panel, so it works even on a machine where
    # the tools are not on PATH.
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

    _overlay_notice()

    if args.run_preset:
        presets = cfg.config.presets or {}
        if args.run_preset not in presets:
            available = ", ".join(sorted(presets)) or "(none saved)"
            print(f"ERROR: no preset named '{args.run_preset}'. Available: {available}")
            sys.exit(2)
        sys.exit(_run_journal(cfg, presets[args.run_preset], f"Preset: {args.run_preset}",
                              unattended=True, dry_run=args.dry_run))

    show_environment_panel(cfg)

    # A new release? Menu runs only: --run-preset, --list-presets and --version have
    # returned above and never ask the network. Asked in the background: the menu waits half
    # a second at most; until the answer is in it shows the one an earlier run kept.
    cfg.update_poll = _update_check_start(cfg)
    upd = cfg.update_poll(0.5)
    if upd:
        _say(upd[0], "yellow")
        cfg.update_available = (cfg.config.update_latest_seen, upd[1])
    _menu_loop(cfg)


# The workflows of "1 New workflow", by the key show_menu returns. Lambdas: the functions
# are looked up when the workflow starts, so a test that replaces one reaches it.
_WORKFLOWS = {
    "1": lambda cfg: run_free_compress(cfg),
    "2": lambda cfg: _run_exif_or_compress(cfg, "copy_exif"),
    "3": lambda cfg: _run_exif_or_compress(cfg, "compress"),
    "4": lambda cfg: _run_exif_or_compress(cfg, "both"),
    "5": lambda cfg: run_undo_old_tiffs(cfg),
    "6": lambda cfg: run_purge_old_tiffs(cfg),
    "7": lambda cfg: run_diagnose_tiffs(cfg),
    "8": lambda cfg: run_generate_thumbnails(cfg),
}


def _menu_loop(cfg) -> None:
    """The main menu until 0 (Exit)."""
    while True:
        choice = show_main_menu(cfg)

        if choice == "0":
            _say("Done.", "dim")
            break

        if choice == "U":
            import webbrowser
            try:
                webbrowser.open(cfg.update_available[1])
            except Exception:
                _say(f"Could not open a browser: {cfg.update_available[1]}", "yellow")
            continue

        if choice == "4":
            show_environment_panel(cfg)
            # ...and the releases, now (the daily background ask may be a day old).
            line, notice = _update_check_now(cfg)
            _say(line, "yellow" if notice else "")
            cfg.update_poll = None          # this answer stands
            cfg.update_available = ((cfg.config.update_latest_seen, notice[1])
                                    if notice else None)
            continue

        if choice == "5":
            presets_menu(cfg)
            continue

        if choice == "6":
            script_settings_menu(cfg)
            continue

        if choice == "3":
            run_manifest_workflow(cfg)
            continue

        if choice == "2":
            run_repeat_last(cfg)
            continue

        # choice == "1": New workflow -- pick the type
        wf = show_menu()
        if wf in _WORKFLOWS:
            _WORKFLOWS[wf](cfg)


def run(argv: Optional[List[str]] = None) -> int:
    """The wizard as the entry point starts it (tiff_workflow/cli.py): main() with `argv`.
    Ctrl+C at a question ends it with "Interrupted." and exit code 1. What prints and exits
    (--help, --version, --list-presets, --run-preset) ends in SystemExit with its code, as
    argparse does."""
    try:
        main(argv)
    except KeyboardInterrupt:
        _say("\nInterrupted.", "dim")
        return 1
    return 0
