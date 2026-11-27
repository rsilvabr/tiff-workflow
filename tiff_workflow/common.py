"""Shared code of the tiff-workflow backends (compress_tiff_zip, copy_exif_to_tiff,
generate_thumbnails, in this package): the command line, the settings overlay, the log, the
calls to ImageMagick and exiftool, the gates that decide whether a rewritten TIFF may replace
its original, and the moves that put it in place. Until v3.1 it was
tiff_workflow_common.py, next to the scripts.

Until v2.9.1 the backends were PowerShell scripts and every helper here existed in up to
four copies (three worker paths in compress_tiff_zip.ps1, two copy_exif scripts). The
behaviour is the same; there is now one copy of each.

Rules this module keeps (docs/bugs_fixed.md has the history of each):
  - exiftool reads its command line in the ANSI code page: every call sends a UTF-8
    argfile on stdin (`exiftool -@ -`, `-charset filename=utf8`).
  - exiftool expands '%' in `-tagsfromfile` and `-o`: a name with '%' goes through a
    container with a '%'-free name (backup_tiff_metadata).
  - ImageMagick expands '%' in every output name and in an input with a '[n]' page suffix
    (magick_path_spec), and picks the output channel layout from the pixels
    (magick_keep_type_args + tiff_sample_layout_kept).
  - A gate that authorises replacing or deleting a file fails closed: a timeout, a
    non-zero exit or output that cannot be parsed is a failure.
  - The swap that replaces a file is one rename on one volume and comes last
    (move_file_replacing, move_file_into_place).
"""

import argparse
import contextlib
import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from enum import Enum
from typing import NamedTuple, Optional

from . import TOOLKIT_URL, TOOLKIT_VERSION  # noqa: F401  (the backends read them here)

#: The folder of an installation: convert_tiff.py and this package. The settings file
#: ".tiff_workflow_settings.json" is looked for here ("next to the scripts").
INSTALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IS_WINDOWS = os.name == "nt"

# Peak memory of one ImageMagick process per pixel of the main page, measured on an
# 84 MP 16-bit scan (IM 7.1.2 Q16-HDRI): compare 68 B/px, conversion/decode 12.7 B/px.
# +~10% margin.
COMPARE_BYTES_PER_PIXEL = 75
CONVERT_BYTES_PER_PIXEL = 15
UNKNOWN_IMAGE_PIXELS = 150000000

# Two file shapes this toolkit writes are NOT photos, both named `*.tif`:
#   <8 hex>_<12 hex>_<name>.tif   a staged file
#   <name>.<8 hex>_<12 hex>.tif   the temp sibling of an in-place write
# A run interrupted between the write and the move leaves one behind. Matched by NAME so a
# CONCURRENT run's in-flight files are skipped too, never deleted.
LEFTOVER_NAME_RE = re.compile(
    r"^[0-9a-f]{8}_[0-9a-f]{12}_.+\.tiff?$|\.[0-9a-f]{8}_[0-9a-f]{12}\.tiff?$", re.I)

TIFF_EXT_RE = re.compile(r"^\.(tif|tiff)$", re.I)

# Where the wizard's padded conversion (Diagnose, workflow 7) keeps each 16-bit original it
# replaced with an 8-bit copy. Not a backend's own backup folder, but the backends' discovery
# skips it as compress skips OLD_TIFFs: matched as a folder name BELOW the scan root, so
# pointing a backend AT it on purpose still works (audit round 31: compress mode 8 rewrote
# the originals kept there in place, and modes 0/9/1 wrote their own output into it).
PADDED_BACKUP_DIR = "OLD_PADDED"

# The page markers that make an extra page a thumbnail, for safe_mode and --generate-thumbnail.
# MASK and PAGE are NOT in it: they are real pages (a scanner's IR channel, a second photo),
# and with the default list of tiff_has_only_subfile_pages, which has them, an RGB+IR scan
# was accepted and rewritten (audit round 9).
THUMBNAIL_PAGE_TYPES = ("REDUCEDIMAGE", "REDUCED")


# --- Command line ---------------------------------------------------

class P:
    """One command-line parameter of a backend.

    kind: "int", "float", "str", "bool" or "switch". Both yes/no kinds are --x / --no-x on
    the command line; a switch is off unless given (--dry-run), a bool has a default of its
    own (--safe-mode is on). In v3.0.0's command line a bool took a value (-SafeMode:$false)
    and a switch stood alone (-DryRun): the old reader, kept for the translation, still
    tells them apart. lo/hi: allowed range of a number. choices: allowed
    values of a string, matched without case. pattern: a regex the value must match.
    `help` is the one-line text the wizard's Script settings screen shows.

    The PARAMS table of each backend is written with literal arguments only:
    convert_tiff.py reads it with `ast`, without running the file."""

    __slots__ = ("name", "kind", "default", "lo", "hi", "choices", "pattern", "help")

    def __init__(self, name, kind, default=None, lo=None, hi=None, choices=None,
                 pattern=None, help=""):
        self.name = name
        self.kind = kind
        self.default = False if (kind == "switch" and default is None) else default
        self.lo = lo
        self.hi = hi
        self.choices = choices
        self.pattern = pattern
        self.help = help


class UsageError(Exception):
    """A command line that cannot be bound to the parameters."""


#: The exit code of a command line that cannot run as written (v3.1, T5: argparse's
#: convention): an option that does not bind, a missing --mode, two options that exclude each
#: other -- what the line alone shows, before anything is touched. 1 stays every other
#: failure, also what depends on the disk (a folder that does not exist, an --output-dir that
#: is a file); 0 is a clean run. The wizard gates a step on "not 0".
EXIT_USAGE = 2


class ToolError(Exception):
    """A tool did not do what was asked: it could not be started, it exited non-zero, or
    what it printed cannot be read. `output` is what it printed (the OSError text when it
    could not be started); `exit_code` its exit code, -1 when it could not be started, 0
    when it exited 0 and printed nothing usable.

    FAIL-CLOSED: a caller that decides whether a file may be written over or replaced
    catches it where it would have tested the exit code, and refuses -- never a pass."""

    def __init__(self, message, output="", exit_code=-1):
        super().__init__(message)
        self.output = output
        self.exit_code = exit_code


def _convert_param(p, raw):
    """The value `raw` gives `p` (int, float or str; a yes/no takes no value): checked against
    its kind, range, choices (matched without case) and pattern. The UsageError says what is
    wrong without naming the option -- each reader names it its own way ("argument --workers:
    must be from 1 to 64, got 65"; tools/legacy_args.py's old reader, "-Workers ...")."""
    if p.kind == "int":
        if not re.fullmatch(r"\s*[+-]?\d+\s*", raw):
            raise UsageError(f"needs a whole number, got '{raw}'")
        val = int(raw)
    elif p.kind == "float":
        try:
            val = float(raw)
        except ValueError:
            raise UsageError(f"needs a number, got '{raw}'") from None
        if not math.isfinite(val):
            raise UsageError(f"needs a number, got '{raw}'")
    else:
        val = raw
    if p.kind in ("int", "float"):
        if (p.lo is not None and val < p.lo) or (p.hi is not None and val > p.hi):
            raise UsageError(f"must be from {p.lo} to {p.hi}, got {raw}")
    if p.choices:
        for c in p.choices:
            if c.lower() == str(val).lower():
                return c
        raise UsageError(f"must be one of {', '.join(p.choices)}, got '{raw}'")
    if p.pattern and not re.search(p.pattern, str(val)):
        raise UsageError(f"'{raw}' is not a valid value")
    return val


def cli_name(name: str) -> str:
    """The long option of a PARAMS name: safe_mode -> --safe-mode, input_dir_file ->
    --input-dir-file (v3.1, D3; T7: the one name of an option everywhere else)."""
    return "--" + name.replace("_", "-")


def cli_switch(name: str, on: bool) -> str:
    """A yes/no on the command line: --safe-mode or --no-safe-mode."""
    return cli_name(name) if on else "--no-" + cli_name(name)[2:]


def cli_value(name: str, value) -> list:
    """An option and its value: [--input-dir, D:\\x]. A value that starts with '-' goes in one
    piece, [--exclude-folders=-old]: argparse would read it as an option of its own."""
    value = str(value)
    if value.startswith("-"):
        return [f"{cli_name(name)}={value}"]
    return [cli_name(name), value]


class _ArgParser(argparse.ArgumentParser):
    """argparse that raises UsageError instead of printing and exiting: the backend says the
    error its own way and decides the exit code."""

    def error(self, message):
        raise UsageError(message)


def _arg_type(p):
    """argparse's type for a PARAMS entry: the same checks as the old reader (range, choices
    matched without case, pattern). argparse names the option itself: "argument --workers:
    must be from 1 to 64, got 65"."""
    def convert(raw):
        try:
            return _convert_param(p, raw)
        except UsageError as e:
            raise argparse.ArgumentTypeError(str(e)) from None
    convert.__name__ = p.kind
    return convert


def _new_parser(params) -> argparse.ArgumentParser:
    """The parser of the new command line: --kebab-case, every yes/no as --x / --no-x, no
    abbreviation, names with their exact case (T6)."""
    ap = _ArgParser(add_help=False, allow_abbrev=False, argument_default=argparse.SUPPRESS)
    for p in params:
        if p.kind in ("bool", "switch"):
            ap.add_argument(cli_name(p.name), dest=p.name, action=argparse.BooleanOptionalAction,
                            help=p.help)
        else:
            ap.add_argument(cli_name(p.name), dest=p.name, type=_arg_type(p), help=p.help)
    return ap


def asks_for_help(argv) -> bool:
    """`--help` or `-h` anywhere on the command line."""
    return any(str(a) in ("--help", "-h") for a in argv)


def help_text(params, prog: str, doc: str = "") -> str:
    """What --help prints: the script's own description (its docstring) and every option with
    its default. Built apart from the parser that reads the command line, which has no --help
    of its own: the wizard binds saved command lines with that one, and it must never print
    and exit."""
    ap = argparse.ArgumentParser(prog=prog, usage=f"{prog} [options]", add_help=False,
                                 description=doc.strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for p in params:
        yes_no = p.kind in ("bool", "switch")
        shown = ("on" if p.default else "off") if yes_no else (
            p.default if p.default not in ("", None) else "none")
        text = (f"{p.help} (default: {shown})" if p.help else f"default: {shown}").replace("%", "%%")
        if yes_no:
            ap.add_argument(cli_name(p.name), "--no-" + cli_name(p.name)[2:], action="store_true",
                            help=text)
        else:
            metavar = ("{" + ",".join(p.choices) + "}" if p.choices
                       else {"int": "N", "float": "NUMBER"}.get(p.kind, "TEXT"))
            ap.add_argument(cli_name(p.name), metavar=metavar, help=text)
    ap.add_argument("--help", "-h", action="store_true", help="this text")
    return ap.format_help()


# A word of the command line of v3.0.0: -Name, one dash and a letter -- v3.0.0 took names in
# any case, -mode too (v3.1 writes --name, and a value that starts with '-' goes as --name=value).
_OLD_FORM = re.compile(r"-[A-Za-z]")


def parse_args(argv, params):
    """Bind `argv` to `params`: ({name: value}, {names given on the command line}). The
    command line is `--kebab-case value`, `--switch` / `--no-switch` (v3.1, D3). Raises
    UsageError for anything it cannot bind; a line in the form of v3.0.0 (-Name value) is one
    of those, and the error says where its translation is (tools/legacy_args.py)."""
    argv = [str(a) for a in argv]
    try:
        given = vars(_new_parser(params).parse_args(argv))
    except UsageError as e:
        # A value that starts with '-' right after an option that takes one (--exclude-folders
        # -old) is not the line of v3.0.0: say how to write it (audit round 29).
        takes_value = {cli_name(p.name) for p in params if p.kind not in ("switch", "bool")}
        for prev, word in zip(argv, argv[1:]):
            if prev in takes_value and _OLD_FORM.match(word):
                raise UsageError(f"{e} -- a value that starts with '-' is written "
                                 f"{prev}={word}") from None
        if any(_OLD_FORM.match(a) for a in argv):
            raise UsageError(f"{e} -- the command line of v3.0.0 (-Name value) is written "
                             f"--name value since v3.1: tools/legacy_args.py translates a "
                             f"line, docs/command_line.md has the table") from None
        raise
    values = {p.name: p.default for p in params}
    values.update(given)
    return values, set(given)


# --- Settings overlay -----------------------------------------------
#
# `.tiff_workflow_settings.json` changes the settings at the top of the scripts without
# editing them. Only the names in a script's OVERLAY_SPECS are taken, each checked against its
# spec; nothing that deletes, overwrites or is chosen per run is ever in a spec table. A
# --param on the command line wins; a bad file or value changes nothing and warns.
# The wizard reads the overlay with these same functions (one copy since v3.1 phase 5): it
# plans with the values the backend will run with.

SETTINGS_OVERLAY_FILE = ".tiff_workflow_settings.json"
SETTINGS_OVERLAY_ENV = "TIFFWORKFLOW_SETTINGS"
_OVERLAY_INT_MAX = 2147483647
_OVERLAY_NAME_FORBIDDEN = '/\\:*?"<>|'


def _overlay_object_pairs(pairs):
    """json object_pairs_hook for the overlay file. audit round 28: the PowerShell readers
    refuse the WHOLE file when an object has an empty "" key or two keys that differ only
    in case (a PowerShell object cannot hold them), so the wizard planned with a value the
    backends never applied. Refuse the same files here. Exact duplicates keep the last
    value in every reader."""
    seen = {}
    for k, _v in pairs:
        if k == "":
            raise ValueError('an empty "" key')
        low = k.casefold()
        if low in seen and seen[low] != k:
            raise ValueError(f"keys that differ only in case: '{seen[low]}' and '{k}'")
        seen.setdefault(low, k)
    return dict(pairs)


def _overlay_bound(text: str):
    return None if text.strip() == "" else float(text) if "." in text else int(text)


def _overlay_value(spec: str, value):
    """`value` (from JSON) checked against an overlay spec: (True, the value to use) or
    (False, what it should have been). Grammar: int:lo:hi | float:lo:hi | intstr:lo:hi (a
    JSON int; the value used is its string) | bool | name | path? | choice:a|b|c. A bool is
    never a number; an int must fit 32 bits. The wizard (convert_tiff.py) reads the overlay
    with this same function (v3.1: until then it carried a copy), so it plans with the value
    the backend will run with."""
    kind, _, rest = spec.partition(":")
    if kind == "choice":
        options = rest.split("|")
        if isinstance(value, str) and value in options:
            return True, value
        return False, "one of " + ", ".join(options)
    if kind == "bool":
        return (True, value) if isinstance(value, bool) else (False, "true or false")
    if kind in ("int", "float", "intstr"):
        lo_t, _, hi_t = rest.partition(":")
        lo, hi = _overlay_bound(lo_t), _overlay_bound(hi_t)
        if isinstance(value, bool):
            ok = False
        elif kind == "float":
            try:
                ok = isinstance(value, (int, float)) and math.isfinite(value)
            except OverflowError:    # an int too large for a float
                ok = False
        else:
            ok = isinstance(value, int) and -_OVERLAY_INT_MAX - 1 <= value <= _OVERLAY_INT_MAX
        if ok and (lo is None or value >= lo) and (hi is None or value <= hi):
            if kind == "float":
                return True, float(value)
            return True, (str(value) if kind == "intstr" else value)
        what = "a number" if kind == "float" else "a whole number"
        if lo is not None and hi is not None:
            what += f" from {lo_t} to {hi_t}"
        elif lo is not None:
            what += f" of at least {lo_t}"
        return False, what
    if kind == "name":
        if isinstance(value, str):
            v = value.strip()
            if v and v not in (".", "..") and not any(ch in v for ch in _OVERLAY_NAME_FORBIDDEN):
                return True, v
        return False, "a folder or file name (no path separators)"
    if kind == "path?":
        if value is None:
            return True, ""
        if isinstance(value, str) and value.strip():
            return True, value.strip()
        return False, "a path, or null"
    return False, "a setting the overlay cannot change"


def overlay_checked(spec: str, value, p=None):
    """_overlay_value, then the range of the parameter `p` (its P's lo/hi) for a number: the
    overlay refuses what the command line refuses, where the file is read (audit round 34: a
    thumbnail size of 16 in the file passed "int:1:" and stopped every run of the backend with
    exit 2). The spec stays as it is; the limit's one copy is the P."""
    ok, checked = _overlay_value(spec, value)
    if not ok or p is None or p.kind not in ("int", "float") or isinstance(checked, str):
        return ok, checked
    if (p.lo is not None and checked < p.lo) or (p.hi is not None and checked > p.hi):
        what = "a whole number" if p.kind == "int" else "a number"
        if p.lo is not None and p.hi is not None:
            return False, f"{what} from {p.lo} to {p.hi}"
        if p.lo is not None:
            return False, f"{what} of at least {p.lo}"
        return False, f"{what} of at most {p.hi}"
    return ok, checked


def user_profile_dir():
    """The user's profile folder (USERPROFILE on Windows, else the home folder): where the
    settings overlay is looked for last and where compress keeps its no-gain memory."""
    return (os.environ.get("USERPROFILE") if IS_WINDOWS else None) or os.path.expanduser("~")


def settings_overlay_path(script_root):
    """The overlay file to read (it may not exist), or None when TIFFWORKFLOW_SETTINGS is
    set but blank (overlay OFF): the variable, then next to the scripts if the file is
    there, then the user profile."""
    env = os.environ.get(SETTINGS_OVERLAY_ENV)
    if env is not None:
        return env if env.strip() else None
    if script_root:
        portable = os.path.join(script_root, SETTINGS_OVERLAY_FILE)
        if os.path.isfile(portable):
            return portable
    return os.path.join(user_profile_dir(), SETTINGS_OVERLAY_FILE)


def read_settings_overlay(section, specs, script_root, params=()):
    """{"path", "values": {name: checked value}, "warnings": [text]} of `section` of the
    overlay file. NEVER raises: a file that cannot be read changes nothing and says so.
    `params`: the script's PARAMS, whose ranges a number must also be in (overlay_checked)."""
    result = {"path": "", "values": {}, "warnings": []}
    by_name = {p.name: p for p in params}
    try:
        path = settings_overlay_path(script_root)
        if path is None:
            return result
        result["path"] = path
        if not os.path.isfile(path):
            return result
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.loads(fh.read(), object_pairs_hook=_overlay_object_pairs)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        sect = data.get(section)
        if sect is None:
            return result
        if not isinstance(sect, dict):
            raise ValueError(f"the '{section}' entry is not an object")
        for name, value in sect.items():
            spec = specs.get(name)
            if spec is None:
                result["warnings"].append(
                    f"{name} is not a setting the overlay can change in {section}; ignored")
                continue
            ok, checked = overlay_checked(spec, value, by_name.get(name))
            if not ok:
                result["warnings"].append(
                    f"{name} = {json.dumps(value, ensure_ascii=False)} is not valid "
                    f"({checked}); the script's value is kept")
                continue
            result["values"][name] = checked
    except Exception as e:
        result["values"] = {}
        result["warnings"] = [f"{result['path']}: not read ({e}); no setting changed"]
    return result


def apply_settings_overlay(section, specs, values, bound, script_root, params=()):
    """Apply the overlay to `values` (the parsed parameters), before anything reads one. A
    parameter given on the command line (`bound`) always beats it. `params`: the script's
    PARAMS (their ranges). Returns {"path", "applied": [names], "warnings": [text], "info": the
    line to print or ""}."""
    overlay = read_settings_overlay(section, specs, script_root, params)
    applied = []
    for name, value in overlay["values"].items():
        if name in bound or name not in values:
            continue
        values[name] = value
        applied.append(name)
    info = ""
    if applied:
        info = (f"Script settings overlay: {overlay['path']}: {len(applied)} value(s): "
                + ", ".join(sorted(applied, key=str.lower)))
    return {"path": overlay["path"], "applied": applied,
            "warnings": list(overlay["warnings"]), "info": info}


# --- Console and log ------------------------------------------------

def setup_console():
    """UTF-8 output, one line at a time, when it goes to a pipe (the wizard reads it):
    the ANSI code page turned every Japanese folder name into '?', and a block-buffered
    pipe showed nothing until the run ended. An interactive console is left alone."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            elif stream is not None:
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def say(text=""):
    """Print one line to the console (not to the log file)."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


class RunLog:
    """`HH:MM:SS | LEVEL | message` on the console and in Logs/<script>/<timestamp>.log
    under the current folder (UTF-8)."""

    def __init__(self, script_name):
        self.dir = os.path.join(os.getcwd(), "Logs", script_name)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
        self._lock = threading.Lock()

    def __call__(self, msg="", level="INFO"):
        line = f"{datetime.now().strftime('%H:%M:%S')} | {level} | {msg}"
        with self._lock:
            say(line)
            try:
                with open(self.path, "a", encoding="utf-8", newline="") as fh:
                    fh.write(line + os.linesep)
            except OSError:
                pass


class Kind(Enum):
    """The kind of a per-file result line: its first word, as printed ("OK | ...",
    "ERROR (magick) | ...", "OK+ZIP | ...", "DRY-RUN | ..."). The three backends tally
    their lines by it (result_kind); tests/test_backend_invariants.py checks that every
    result line the backends write starts with one."""
    OK = "OK"
    SKIP = "SKIP"
    MULTI = "MULTI"
    MISS = "MISS"
    WARN = "WARN"
    DRY = "DRY"
    ERROR = "ERROR"

    @property
    def level(self):
        """The log level of a line of this kind."""
        if self is Kind.ERROR:
            return "ERROR"
        if self in (Kind.WARN, Kind.MULTI, Kind.MISS):
            return "WARN"
        return "INFO"


_KIND_WORD = re.compile(r"[A-Z]+")


def result_kind(line):
    """The Kind of a result line. A line whose first word is no Kind is an ERROR: a
    misspelt kind must end the run with exit 1, not pass uncounted."""
    m = _KIND_WORD.match(line)
    try:
        return Kind(m.group(0)) if m else Kind.ERROR
    except ValueError:
        return Kind.ERROR


def python_label():
    return "Python " + sys.version.split()[0]


# --- Paths ----------------------------------------------------------

def same_path(a, b):
    """Two paths compared the way Windows names files: without case."""
    return str(a).lower() == str(b).lower()


def norm_sep(path):
    return str(path).replace("/", "\\") if IS_WINDOWS else str(path)


def join_path(a, b):
    """`a` and `b` joined with ONE separator, whatever `b` starts with (os.path.join drops
    `a` when `b` is rooted)."""
    a = str(a)
    b = str(b)
    if not a:
        return b
    if not b:
        return a
    return a.rstrip("\\/") + os.sep + b.lstrip("\\/")


def parent_dir(path):
    """The folder a path is in; "" for a drive or share root (os.path.dirname returns the
    root itself there)."""
    p = str(path)
    stripped = p.rstrip("\\/")
    drive, tail = os.path.splitdrive(stripped)
    if not tail.strip("\\/"):
        return ""
    return os.path.dirname(stripped)


def full_path(path):
    """Absolute, normalised path ('.' and '..' resolved, one kind of separator)."""
    return os.path.abspath(str(path))


def resolve_input_dirs(entries, allow_files=False):
    """The entries of --input-dir / --input-dir-file as (the ones that exist, the ones that do
    not): each made absolute ("." or "a\\..\\b" once broke the root compare of the folder
    exclusion), blank ones dropped, and a repeated one taken ONCE -- "A;A" used to process a
    folder twice, and a missing folder listed twice was two errors in one backend and one
    in another. An entry must be a folder; `allow_files` also takes a file
    (generate_thumbnails: a TIFF passed directly is its own root)."""
    found, missing, seen = [], [], set()
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        entry = full_path(entry)
        if entry.lower() in seen:
            continue
        seen.add(entry.lower())
        exists = os.path.exists(entry) if allow_files else os.path.isdir(entry)
        (found if exists else missing).append(entry)
    return found, missing


def output_dir_error(path):
    """The usage error for an --output-dir that names an existing FILE, or "". Until v3.0.0
    compress ended in `FATAL (unhandled error) | FileExistsError` when it tried to make the
    folder; the answer is the same (exit code 1, nothing touched), said before anything
    starts. A relative path is checked against the current folder."""
    path = str(path).strip()
    if path and os.path.isfile(full_path(path)):
        return f"--output-dir is a file, not a folder: {full_path(path)}"
    return ""


def numbered_name(directory, stem, ext, taken):
    """<stem>_v2<ext>, _v3, ...: the first name `taken(name, path)` does not refuse.
    Returns (name, its path in `directory`)."""
    n = 2
    while True:
        name = f"{stem}_v{n}{ext}"
        path = join_path(directory, name)
        n += 1
        if not taken(name, path):
            return name, path


def long_path(path):
    """`path` behind the \\\\?\\ prefix, which lifts Windows' 260-character limit for the
    file calls of this process (ImageMagick and exiftool are not long-path aware: the
    backends refuse an over-long output name instead)."""
    p = str(path)
    if not IS_WINDOWS or p.startswith("\\\\"):
        return p
    return "\\\\?\\" + p


def segments_below(directory, root):
    """The folder segments of `directory` below `root`: [] for the root itself, None when
    it is not under it. Without case, '/' and '\\' alike, and on a separator boundary:
    "C:\\data" is also a prefix of "C:\\database", which is not below it."""
    dir_n = str(directory).replace("/", "\\").rstrip("\\")
    root_n = str(root).replace("/", "\\").rstrip("\\")
    if dir_n.lower() == root_n.lower():
        return []
    if dir_n.lower().startswith(root_n.lower() + "\\"):
        return dir_n[len(root_n) + 1:].split("\\")
    return None


def path_excluded(directory, root, names):
    """True when any folder segment BELOW `root` matches one of `names` (without case).

    Matching is relative to the scan root, never absolute: an absolute match also matched
    every folder ABOVE the one the user pointed at -- an entry that happened to match one of
    those (`Temp`, a year, the root's own name) excluded the ENTIRE run and exited 0 (audit
    round 14). Pointing the tool AT a folder named `_EXPORT` therefore works: only folders
    nested below the root are considered. A directory that is not under the root falls back
    to the whole path: in doubt, skip the file."""
    if not names:
        return False
    parts = segments_below(directory, root)
    if parts is None:
        parts = str(directory).replace("/", "\\").rstrip("\\").split("\\")
    lowered = {str(n).lower() for n in names}
    return any(seg and seg.lower() in lowered for seg in parts)


def path_included(directory, root, names):
    """--include-folders: True when `names` is empty, or when the scan root's own folder or
    a folder BELOW it is named one of `names` (without case). The root's own name counts, so
    pointing the tool AT a TIFF16 folder with --include-folders TIFF16 takes its files; the
    folders ABOVE the root never count (the reason of round 14's exclusion rule: a name the
    user did not point at must not decide a whole run). A directory that is not under the
    root is left out: in doubt, the file is not touched."""
    if not names:
        return True
    parts = segments_below(directory, root)
    if parts is None:
        return False
    root_name = str(root).replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1]
    lowered = {str(n).lower() for n in names}
    return any(seg and seg.lower() in lowered for seg in [root_name] + parts)


def resolve_output(f, mode, input_root, opt):
    """Where compress_tiff_zip writes the compressed copy of `f` (a FoundFile) in `mode`;
    None when the mode has no place for it (reported, never dropped silently). The output is
    always <stem>.tif. `opt` needs zip_subfolder_name, zip_suffix, output_dir, export_marker,
    export_tiff_subfolder and export_zip_subfolder. The wizard's manifest collision scan asks
    here too: one rule for where a mode writes (v3.1 phase 6, W-O4)."""
    parent = f.dir
    out_name = f.stem + ".tif"
    # One separator: '/' is legal on Windows, and --input-dir given that way used to defeat
    # the mode 5 prefix compare and collapse the whole tree into <root>\ZIP.
    root_p = input_root.replace("/", "\\").rstrip("\\")

    if mode in (0, 8, 9):
        return join_path(parent, out_name)
    if mode in (1, 3):
        return join_path(join_path(parent, opt["zip_subfolder_name"]), out_name)
    if mode == 2:
        return join_path(opt["output_dir"] or root_p, out_name)
    if mode == 4:
        folder = os.path.basename(parent.rstrip("\\/"))
        grandparent = parent_dir(parent)
        if not grandparent or not folder:
            return None
        suffix = opt["zip_suffix"]
        if re.search(r"_(TIFF)$", folder, re.I):
            new = re.sub(r"_(TIFF)$", lambda m: suffix, folder, flags=re.I)
        elif folder.lower() == "tiff":
            # A folder named exactly "TIFF" has no stem: the suffix rule made it "_ZIP".
            new = opt["zip_subfolder_name"]
        elif re.search(r"^(.+)(TIFF)$", folder, re.I):
            new = re.sub(r"(TIFF)$", lambda m: suffix, folder, flags=re.I)
        else:
            new = folder + suffix
        return join_path(join_path(grandparent, new), out_name)
    if mode == 5:
        grandparent = parent_dir(parent)
        # Never climb above the input root: a TIFF sitting directly in the root would land
        # in the drive root, outside the chosen tree. The prefix compare needs a separator
        # boundary: "C:\data" also prefixes "C:\database".
        if not grandparent or segments_below(grandparent, root_p) is None:
            grandparent = root_p
        return join_path(join_path(grandparent, opt["zip_subfolder_name"]), out_name)
    if mode in (6, 7):
        # The marker only counts AT/BELOW the input root: an _EXPORT folder ABOVE the
        # chosen --input-dir once rebuilt the output outside the selected tree.
        parts = segments_below(parent, root_p)
        if parts is None:
            return None
        marker = opt["export_marker"].lower()
        export_idx = -1
        tail_from = -1
        for i, seg in enumerate(parts):
            if export_idx < 0 and seg.lower() == marker:
                export_idx = i
                if mode == 6:
                    tail_from = i + 1
                    break
                continue
            # mode 7: the TIFF subfolder only counts when it sits below the marker
            if mode == 7 and export_idx >= 0 and seg.lower() == opt["export_tiff_subfolder"].lower():
                tail_from = i + 1
                break
        if export_idx < 0 or tail_from < 0:
            return None
        # Inside the _EXPORT folder the file actually came from (<root>\_EXPORT would
        # merge every job into one folder).
        export_root = join_path(root_p, "\\".join(parts[:export_idx + 1]))
        new_parent = join_path(export_root, opt["export_zip_subfolder"])
        rel = [p for p in parts[tail_from:] if p]
        if rel:
            new_parent = join_path(new_parent, "\\".join(rel))
        return join_path(new_parent, out_name)
    return None


#: A backup renamed by its writer because one of that name was already there, in the stem
#: (group "base"): compress modes 0/9 add a date stamp in OLD_TIFFs (complete_keep_original:
#: photo_20260805_101500[_1].tif), the wizard's padded conversion adds _vN in OLD_PADDED
#: (_finalize_padded: photo_v2.tif). A name of that shape is a RENAME only while its base
#: backup <base>.tif/.tiff is in the same folder (backup_base_stem): otherwise the suffix is part
#: of the photo's own name (IMG_20190815_143000.tif from a phone, portrait_v2.tif). One rule for
#: the stranded check, the wizard's Delete OLD_TIFFs and tools/move_old_backups.py (audit
#: round 34).
STAMPED_BACKUP_RE = re.compile(r"^(?P<base>.+)_\d{8}_\d{6}(?:_\d+)?$")
PADDED_RENAMED_RE = re.compile(r"^(?P<base>.+)_v\d+$")
_TIFF_EXTS = (".tif", ".tiff")


def backup_base_stem(backup_dir, stem, renamed_re):
    """The stem of the backup a renamed backup collided with -- `stem` without its rename
    suffix -- when `renamed_re` matches it AND <base>.tif or <base>.tiff is a file in
    `backup_dir`; else None."""
    m = renamed_re.match(stem)
    if not m:
        return None
    base = m.group("base")
    if any(os.path.isfile(long_path(os.path.join(backup_dir, base + e))) for e in _TIFF_EXTS):
        return base
    return None


def backup_counterparts(backup_path, renamed_re):
    """The places, in the folder above the backup folder, where the file this backup was made
    from can be, in order: the same name; for a TIFF the same stem with the other extension
    too (a .tiff source is written as .tif), then the base stem of a renamed backup
    (backup_base_stem) with both. A sidecar (not a TIFF) has only its own name."""
    backup_dir = os.path.dirname(backup_path)
    parent = os.path.dirname(backup_dir)
    name = os.path.basename(backup_path)
    stem, ext = os.path.splitext(name)
    if not TIFF_EXT_RE.match(ext):
        return [os.path.join(parent, name)]
    other = ".tiff" if ext.lower() == ".tif" else ".tif"
    places = [os.path.join(parent, name), os.path.join(parent, stem + other)]
    base = backup_base_stem(backup_dir, stem, renamed_re)
    if base:
        places += [os.path.join(parent, base + e) for e in _TIFF_EXTS]
    return places


def find_stranded_originals(root, recurse, backup_names, exclude_names):
    """Backups in OLD_TIFFs/ whose original place is EMPTY: the only copy of that image.

    Modes 0/9 once moved every original of a folder to OLD_TIFFs/ before its compressed
    copy was written; a run that died in between left those originals there with nothing in
    the parent folder, and every later run walks past them because discovery excludes
    OLD_TIFFs/ (audit round 25: 365 originals on 2026-08-03).

    A backup counts as stranded when none of its possible counterparts exists in the parent
    folder: <stem>.tif / <stem>.tiff (a .tiff source is written as .tif), and for a
    timestamped backup (<stem>_yyyyMMdd_HHmmss[_n], made when the name was taken) the same
    for the base stem -- but only when OLD_TIFFs/ also holds <base> itself: otherwise the
    stamp is part of the photo's own name (Scan_20190815_143000.tif). Returns
    [{"backup", "target"}]; target = where a restore puts it (that stem with the backup's
    extension).

    Folders only are walked, never INTO a backup folder (a nested one is a backup of a
    backup), links are not followed, and every file call goes through the long-path prefix:
    past 260 characters "no original here" was once said about a file that was there.

    The wizard's Restore asks here too (v3.1 phase 6, W-O5: its own copy walked INTO backup
    folders and would have restored OLD_TIFFs\\sub\\OLD_TIFFs\\deep.tif into OLD_TIFFs\\sub\\)."""
    found = []
    full = full_path(root)
    backup_lower = {n.lower() for n in backup_names}
    backup_dirs = []
    stack = [full]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(long_path(cur)) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            plain = os.path.join(cur, e.name)
            if e.name.lower() in backup_lower:
                backup_dirs.append(plain)
                continue
            if not recurse:
                continue
            if entry_flags(e)[1]:
                continue
            stack.append(plain)
    backup_dirs.sort(key=str.lower)

    for d in backup_dirs:
        parent = os.path.dirname(d)
        if exclude_names and path_excluded(parent, root, exclude_names):
            continue
        try:
            with os.scandir(long_path(d)) as it:
                names = sorted((e.name for e in it if e.is_file()), key=str.lower)
        except OSError:
            continue
        for b_name in names:
            stem, ext = os.path.splitext(b_name)
            if not TIFF_EXT_RE.match(ext):
                continue
            backup = os.path.join(d, b_name)
            present = any(os.path.isfile(long_path(c))
                          for c in backup_counterparts(backup, STAMPED_BACKUP_RE))
            if not present:
                base = backup_base_stem(d, stem, STAMPED_BACKUP_RE)
                found.append({"backup": backup,
                              "target": os.path.join(parent, (base or stem) + ext)})
    return found


def parse_folder_names(text, option, example):
    """The ';'-separated folder NAMES of a folder option (--exclude-folders,
    --include-folders), or raises UsageError for a path."""
    names = []
    for entry in str(text or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if "\\" in entry or "/" in entry:
            raise UsageError(
                f"{option} takes folder NAMES, not paths: '{entry}'\n"
                f"       Use a ';'-separated list of bare names, e.g. {option} \"{example}\"")
        names.append(entry)
    return names


def parse_exclude_folders(text):
    """The ';'-separated folder NAMES of --exclude-folders, or raises UsageError for a path."""
    return parse_folder_names(text, "--exclude-folders", "_EXPORT;temp")


_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


class FoundFile:
    """One file of a directory listing: full path, name, folder, size and write time."""

    __slots__ = ("path", "name", "dir", "size", "mtime_ns", "input_root")

    def __init__(self, path, name, directory, size, mtime_ns):
        self.path = path
        self.name = name
        self.dir = directory
        self.size = size
        self.mtime_ns = mtime_ns
        self.input_root = None

    @property
    def stem(self):
        return os.path.splitext(self.name)[0]

    @property
    def ext(self):
        return os.path.splitext(self.name)[1]


def entry_flags(entry):
    """(hidden, link) of a directory entry. link = a symbolic link or a junction: a name
    for a folder that lives somewhere else. A OneDrive folder is a reparse point too, but
    not a link, and is walked like any other."""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False, False
    attrs = getattr(st, "st_file_attributes", 0)
    hidden = bool(attrs & _FILE_ATTRIBUTE_HIDDEN)
    link = entry.is_symlink()
    if not link and attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        link = getattr(st, "st_reparse_tag", 0) in (_IO_REPARSE_TAG_SYMLINK,
                                                    _IO_REPARSE_TAG_MOUNT_POINT)
    return hidden, link


def list_files(root, recurse, unreadable=None):
    """Every file in `root` (and below it with `recurse`), as FoundFile, sorted by path
    without case. Hidden files and folders are left out ($RECYCLE.BIN and System Volume
    Information at a drive root are hidden) and links to folders are not followed, like
    the directory listing the PowerShell backends used. A folder that cannot be read is
    skipped and appended to `unreadable`.

    The folders are read through the long-path prefix: a file past 260 characters is
    FOUND (and then refused by name, with the length, where it cannot be processed)
    instead of its whole folder being skipped as unreadable."""
    root = full_path(root)
    found = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(long_path(cur)) as it:
                entries = list(it)
        except OSError:
            if unreadable is not None:
                unreadable.append(cur)
            continue
        for e in entries:
            hidden, link = entry_flags(e)
            if hidden:
                continue
            path = os.path.join(cur, e.name)
            try:
                if e.is_dir():
                    if recurse and not link:
                        stack.append(path)
                    continue
                if not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            found.append(FoundFile(path, e.name, cur, st.st_size, st.st_mtime_ns))
    found.sort(key=lambda f: f.path.lower())
    return found


def list_dirs(root, recurse, unreadable=None):
    """Every folder below `root` (full paths), same rules as list_files."""
    root = full_path(root)
    found = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(long_path(cur)) as it:
                entries = list(it)
        except OSError:
            if unreadable is not None:
                unreadable.append(cur)
            continue
        for e in entries:
            hidden, link = entry_flags(e)
            if hidden:
                continue
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            path = os.path.join(cur, e.name)
            found.append(path)
            if recurse and not link:
                stack.append(path)
    found.sort(key=str.lower)
    return found


def is_tiff_name(name):
    return bool(TIFF_EXT_RE.match(os.path.splitext(name)[1]))


def read_input_dir_file(path):
    """The folders of -InputDirFile: a UTF-8 text file, one folder per line (the wizard
    writes it when --input-dir would pass the command-line limit). Raises UsageError."""
    if not os.path.isfile(path):
        raise UsageError(f"--input-dir-file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            entries = [ln.strip() for ln in fh.read().splitlines()]
    except (OSError, UnicodeError) as e:
        raise UsageError(f"cannot read --input-dir-file: {path} ({e})") from None
    entries = [e for e in entries if e]
    if not entries:
        raise UsageError(f"--input-dir-file lists no folders: {path}")
    return entries


# --- Running ImageMagick and exiftool -------------------------------

_tool_paths = {}


def find_tool(name):
    """Full path of `magick` / `exiftool` on PATH, or None."""
    if name not in _tool_paths:
        path = shutil.which(name)
        if path:
            # which() appends the PATHEXT spelling (".EXE"), and ImageMagick prints the name
            # it was started under in every message
            root, ext = os.path.splitext(path)
            path = root + ext.lower()
        _tool_paths[name] = path
    return _tool_paths[name]


def missing_tools(names):
    """The display names of the tools of `names` that are not on PATH."""
    labels = {"magick": "ImageMagick (magick)", "exiftool": "exiftool"}
    return [labels.get(n, n) for n in names if not find_tool(n)]


def run_captured(cmd, timeout=None, input=None):
    """Run `cmd` and return (exit code, stdout bytes, stderr bytes), captured through temp
    FILES. capture_output=True makes communicate() start two reader threads per call on
    Windows; under memory exhaustion a thread can fail in its bootstrap before it signals
    "started", and Thread.start() then waits forever -- before the timeout is armed
    (jxl-photo, 2026-10-04: one worker hung 60 min). With files there is no thread.
    `input` (bytes) reaches stdin through a temp FILE too: from Python 3.13 subprocess writes
    `input=` from a thread whenever a timeout is given (audit round 33).
    Raises OSError when the program cannot be started and subprocess.TimeoutExpired when
    `timeout` seconds pass (the program is killed first)."""
    if input is not None and not isinstance(input, (bytes, bytearray)):
        raise TypeError("run_captured: input must be bytes")
    with contextlib.ExitStack() as files:
        out = files.enter_context(tempfile.TemporaryFile())
        err = files.enter_context(tempfile.TemporaryFile())
        inp = None
        if input is not None:
            inp = files.enter_context(tempfile.TemporaryFile())
            inp.write(input)
            inp.flush()
            inp.seek(0)
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL if inp is None else inp,
                           stdout=out, stderr=err, timeout=timeout)
        out.seek(0)
        err.seek(0)
        # A real run leaves r.stdout None (the output is in the files); a stand-in for
        # subprocess.run in the wizard's tests hands its output back on the result.
        o = r.stdout if r.stdout is not None else out.read()
        e = r.stderr if r.stderr is not None else err.read()
        return r.returncode, o, e


def _text(data):
    return data.decode("utf-8", "replace").replace("\r\n", "\n")


def _lines(data):
    return [ln for ln in _text(data).split("\n")]


def one_line(text):
    """Tool output folded to one line, for a result line."""
    return " ".join(part.strip() for part in str(text).splitlines() if part.strip())


class MagickResult(NamedTuple):
    timed_out: bool
    exit_code: int
    lines: list


def magick_probe(args, timeout=30):
    """`magick <args>` under a timeout, for the PROBES (identify, compare, decode checks):
    MagickResult(timed_out, exit_code, stdout lines). exit_code -1 means "could not
    determine" -- every caller treats it as a failure, never as success. On a timeout the
    magick process is killed. The conversions do not come through here: 30 s is right for
    a probe and far too short for writing a 500 MB scan."""
    if not timeout or timeout <= 0:
        timeout = 30
    try:
        rc, out, _err = run_captured([find_tool("magick") or "magick"] + [str(a) for a in args],
                                     timeout=timeout)
    except subprocess.TimeoutExpired:
        return MagickResult(True, -1, [])
    except OSError:
        return MagickResult(False, -1, [])
    return MagickResult(False, rc, _lines(out))


def magick_run(args):
    """`magick <args>` with no timeout, for the conversions: what it printed (stdout +
    stderr, as text). Raises ToolError, with that text as its `output`, when magick exits
    non-zero or cannot be started (exit_code -1, the OSError text as output). A
    conversion that hangs hangs the run -- visible, interruptible and non-destructive,
    since nothing replaces an original before the gates have passed. Exit 0 is not proof
    of a file: the callers check that the output exists."""
    try:
        rc, out, err = run_captured([find_tool("magick") or "magick"] + [str(a) for a in args])
    except OSError as e:
        raise ToolError(f"magick could not be started: {e}", str(e), -1) from e
    text = _text(out) + _text(err)
    if rc != 0:
        raise ToolError(f"magick exit {rc}", text, rc)
    return text


def exiftool(args, merge_stderr=False, check=True):
    """exiftool with its arguments in a UTF-8 argfile read on STDIN (`-@ -`), one argument
    per line, `-charset filename=utf8` first: its output text. exiftool reads its command
    line in the ANSI code page -- a name outside it could not be opened, and neither could
    an argfile whose own path was outside it (audit round 27). `merge_stderr` adds stderr
    to the output, for the callers whose fail-closed 'Error opening file' check reads both.

    Raises ToolError when exiftool cannot be started (exit_code -1) and, with `check`, when
    it exits non-zero (the output in the exception). `check=False` is for a batch read
    whose output is used even when one file of the batch could not be read."""
    rc, out, err = _exiftool_process(["-charset", "filename=utf8"] + [str(a) for a in args])
    text = _text(out)
    if merge_stderr:
        text += _text(err)
    if check and rc != 0:
        raise ToolError(f"exiftool exit {rc}", text, rc)
    return text


def _exiftool_process(arg_lines):
    """One exiftool process, its arguments on stdin (`-@ -`, UTF-8, one per line): (exit
    code, stdout bytes, stderr bytes). Raises ToolError when it cannot be started."""
    data = ("\n".join(arg_lines) + "\n").encode("utf-8")
    try:
        return run_captured([find_tool("exiftool") or "exiftool", "-@", "-"], input=data)
    except OSError as e:
        raise ToolError(f"exiftool could not be started: {e}", str(e), -1) from e


class ExifResult(NamedTuple):
    status: int        # the command's own exit status; -1 when it could not be read
    output: str        # what the command printed, stdout then stderr


def exiftool_commands(commands):
    """Several exiftool commands in ONE process (`-execute`), each with its own exit status
    and output: [ExifResult] in command order. Starting exiftool costs ~0.15 s; a file's
    writes after a conversion are three commands that used to start it three times.

    Each command ends with `-echo3`/`-echo4` markers carrying `${status}` (exiftool 12.15 or
    later), on stdout and on stderr: what a command printed is what came before its marker
    and after the previous one, so the 'Error opening file' of one command is never read as
    another's. FAIL-CLOSED: a marker that is missing, or carries no number, gives status -1.
    Raises ToolError when exiftool cannot be started."""
    tag = "{tw" + uuid.uuid4().hex[:12]
    lines = []
    for i, cmd in enumerate(commands):
        if i:
            lines.append("-execute")
        marker = f"{tag} {i} " + "${status}}"
        lines += (["-charset", "filename=utf8"] + [str(a) for a in cmd]
                  + ["-echo3", marker, "-echo4", marker])
    _rc, out, err = _exiftool_process(lines)
    streams = [_split_by_marker(_text(out), tag, len(commands)),
               _split_by_marker(_text(err), tag, len(commands))]
    results = []
    for i in range(len(commands)):
        (s_out, t_out), (s_err, t_err) = streams[0][i], streams[1][i]
        status = s_out if s_out == s_err else -1
        results.append(ExifResult(status, t_out + t_err))
    return results


def exiftool_status_problem():
    """None when exiftool reports the exit status of each command of one run -- what
    exiftool_commands, and so every write after a conversion, reads (`${status}` in -echo3,
    exiftool 12.15 or later). Else the sentence the run stops with, before any file: an
    older exiftool would make every write look failed (every file an ERROR or a WARN) although
    the writes were done. Asked once per run: one exiftool start."""
    try:
        r = exiftool_commands([["-ver"]])[0]
    except ToolError as e:
        return f"exiftool could not be started: {one_line(e.output)}"
    if r.status == 0:
        return None
    version = one_line(r.output).split(" ")[0] or "(version unknown)"
    return (f"exiftool {version} does not report the result of each command it runs; "
            f"update it to 12.15 or later (https://exiftool.org)")


def _split_by_marker(text, tag, n):
    """[(status, text)] of `n` commands from one stream of exiftool_commands."""
    parts = [(-1, "") for _ in range(n)]
    pending = []
    for line in text.split("\n"):
        m = re.fullmatch(re.escape(tag) + r" (\d+) (-?\d+)\}", line.strip())
        if m and int(m.group(1)) < n:
            parts[int(m.group(1))] = (int(m.group(2)), "".join(pending))
            pending = []
        else:
            pending.append(line + "\n")
    return parts


def magick_path_spec(path):
    """Escape '%' for the filename SPECS ImageMagick expands.

    ImageMagick reads printf-style scene specifiers in a filename spec: `50%off.tif` writes
    `500ff.tif` ('%o'), `100%dpi.tif` writes `1000pi.tif`, and on a multi-page image it
    writes ONE FILE PER PAGE. magick still exits 0. '%%' is the escape.

    WHERE THIS APPLIES, exactly:
      - EVERY output path.
      - An INPUT that carries a '[n]' page suffix -- the suffix turns the argument into a
        filename spec.
    And where it must NOT be used:
      - A PLAIN input path. There ImageMagick opens the name literally, so an escaped name
        is simply not found."""
    if not path:
        return path
    return str(path).replace("%", "%%")


def temp_file_path(suffix):
    """A new name in the temp folder (the file is not created)."""
    return os.path.join(tempfile.gettempdir(), f"tw_{uuid.uuid4().hex[:16]}{suffix}")


def metadata_container_path(*near):
    """A new name for a metadata container (`tw_<16 hex>.mie`, the shape sweep_leftovers and
    sweep_temp_files know; .mie is not a TIFF, discovery never takes it) in the first folder
    whose full path has no '%': the temp folder, then the folder of each `near` path in order.
    exiftool expands '%' in `-o` and in `-tagsfromfile`, so a container there would be
    written, or read, somewhere else. Until audit round 34 only the temp folder was tried: a
    '%' in it refused every file with a '%' in its name. None fits -> ToolError (the caller
    refuses the write, as before)."""
    folders = [tempfile.gettempdir()] + [os.path.dirname(full_path(str(n))) for n in near if n]
    for folder in folders:
        if "%" not in full_path(folder):
            return os.path.join(folder, f"tw_{uuid.uuid4().hex[:16]}.mie")
    raise ToolError("no metadata container: every folder it could go in has a '%' in its "
                    "path (" + "; ".join(folders) + ")")


def remove_quietly(path):
    """Delete a file; never raises. A read-only file is deleted too."""
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        try:
            os.chmod(path, 0o666)
            os.remove(path)
        except OSError:
            pass


def remove_file(path):
    """Delete a file, a read-only one too. Raises OSError (remove_quietly never does)."""
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, 0o666)
        os.remove(path)


def file_size(path):
    return os.stat(path).st_size


# --- TIFF probes and gates ------------------------------------------

def _size_scaled_timeout(timeout, paths, load_factor=1.0):
    """A flat probe timeout false-failed huge scans: a full decode of a 700 MB film scan
    takes longer than 30 s (audit round 16). ~0.25 s/MB of the larger input, times the
    workers running at once (they share the disk), never below the caller's value. A file
    that cannot be read leaves the value as it is."""
    try:
        max_mb = max(os.stat(p).st_size for p in paths) / (1024 * 1024)
    except OSError:
        return timeout
    return max(timeout, int(math.ceil(max_mb * 0.25 * max(1.0, load_factor))))


class PageCount(NamedTuple):
    ok: bool
    count: int  # type: ignore[assignment]  # a field named like tuple.count; read as .count
    error: str


def tiff_page_count(path, timeout=30):
    """PageCount(ok, count, error) of a TIFF. error is "timeout", "failed" or
    "parse:<raw>" -- never silently 0. `%n` prints once per frame, so the first line of
    "%n\\n" is the only reliable form.

    `-ping` (v3.1): the pages are counted from the file's directories, without decoding a
    pixel. Without it identify decoded every page -- seconds on a large scan, half the time
    of a Copy EXIF run -- and under load a healthy file ran past the timeout and was
    reported as "possibly corrupted". The count never said anything about the pixels (the
    exit code is not read here): zip_integrity and pixel_identical do that."""
    r = magick_probe(["identify", "-ping", "-format", "%n\\n", path], timeout)
    if r.timed_out:
        return PageCount(False, 0, "timeout")
    lines = [ln for ln in r.lines if ln.strip()]
    if not lines:
        return PageCount(False, 0, "failed")
    val = lines[0].strip()
    if not re.fullmatch(r"[+-]?\d+", val):
        return PageCount(False, 0, "parse:" + val)
    return PageCount(True, int(val), "")


def page_count_error_line(pc, name, what="identify output"):
    """The ERROR result line for a page count that could not be read."""
    if pc.error == "timeout":
        return f"ERROR (magick timeout) | {name} | possibly corrupted"
    if pc.error.startswith("parse:"):
        return f"ERROR (magick page count parse) | {name} | unexpected {what}: {pc.error[6:]}"
    return f"ERROR (magick page count failed) | {name} | possibly corrupted"


def zip_integrity(path, timeout=30, load_factor=1.0):
    """True only when the TIFF decodes cleanly, every pixel (`magick <file> null:`).
    A timeout, a non-zero exit or a failure to run is False: this gate authorises
    replacing or deleting a source."""
    timeout = _size_scaled_timeout(timeout, [path], load_factor)
    r = magick_probe([path, "null:"], timeout)
    if r.timed_out:
        return False
    return r.exit_code == 0


def _distortion_is_zero(r):
    # compare exits 1 when the images differ -- that is data, not an error. Only a timeout
    # or an undetermined/error exit code fails directly; the parsed value has the last word.
    if r.timed_out or r.exit_code < 0 or r.exit_code > 1:
        return False
    lines = [ln for ln in r.lines if ln.strip()]
    if not lines:
        return False
    try:
        val = float(lines[0].strip())
    except ValueError:
        return False
    return val == 0


def pixel_identical(src, dst, timeout=30, load_factor=1.0):
    """True only when two TIFFs hold pixel-identical data: the same page count and RMSE 0
    on every page. A decode check cannot see a truncated page, a wrong bit depth or a
    colorspace shift -- all of those still decode -- so this is the gate that authorises
    destroying the source. `magick compare A B` on multi-page files pairs pages across the
    two lists and returns nonsense, so pages are compared one by one. Anything unreadable
    fails CLOSED."""
    timeout = _size_scaled_timeout(timeout, [src, dst], load_factor)
    ps = tiff_page_count(src, timeout)
    pd = tiff_page_count(dst, timeout)
    if not ps.ok or not pd.ok:
        return False
    if ps.count != pd.count or ps.count < 1:
        return False        # no page compared is no proof (audit round 29)
    # '[i]' turns these into filename SPECS: '%' has to be escaped or magick opens another file
    src_spec = magick_path_spec(src)
    dst_spec = magick_path_spec(dst)
    for i in range(ps.count):
        r = magick_probe(["compare", "-metric", "RMSE", f"{src_spec}[{i}]", f"{dst_spec}[{i}]",
                          "-format", "%[distortion]\n", "info:"], timeout)
        if not _distortion_is_zero(r):
            return False
    return True


def thumb_output_matches_source(src, dst, expected_pages, timeout=30):
    """The gate for a --generate-thumbnail output: True only when the output has exactly
    `expected_pages` pages (2 with the thumbnail, 1 on the [no thumb] fallback) and its
    page 0 is PIXEL-IDENTICAL to the source's page 0. pixel_identical cannot serve: the
    thumbnail path builds a new page layout on purpose, and the thumbnail is derived data
    that is allowed to differ. Anything unreadable fails CLOSED."""
    timeout = _size_scaled_timeout(timeout, [src, dst])
    pd = tiff_page_count(dst, timeout)
    if not pd.ok:
        return False
    if expected_pages > 0 and pd.count != expected_pages:
        return False
    r = magick_probe(["compare", "-metric", "RMSE", f"{magick_path_spec(src)}[0]",
                      f"{magick_path_spec(dst)}[0]", "-format", "%[distortion]\n", "info:"],
                     timeout)
    return _distortion_is_zero(r)


def backup_tiff_metadata(src, write_dst):
    """Park the metadata of `src` in a temp MIE container when `src` cannot be the
    `-tagsfromfile` source itself. Returns the container's path, or None when `src` can be
    the source itself.

    Two cases need it: `write_dst` IS `src` (an in-place write destroys the original before
    the restore runs), and a '%' in `src` (exiftool expands printf codes in the
    -tagsfromfile argument: `50%off.tif` is read as `50ff.tif`, it warns and STILL EXITS 0;
    doubling the '%' does not escape it). The file operand is not expanded, so dumping FROM
    the '%' name works. The caller deletes the container.

    Where: metadata_container_path -- the temp folder, else the folder of `write_dst` (a
    staging folder before the photo's), else the folder of `src`.

    FAIL-CLOSED: raises ToolError when the container is needed and could not be written;
    the caller must refuse the write, which would strip every tag."""
    if not same_path(write_dst, src) and "%" not in str(src):
        return None
    meta = metadata_container_path(write_dst, src)
    try:
        out = exiftool(["-q", "-q", "-all:all",
                        "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet", "-o", meta, src])
    except ToolError:
        # a failed dump can leave a PARTIAL container behind
        remove_quietly(meta)
        raise
    except Exception as e:
        remove_quietly(meta)
        raise ToolError(f"no metadata container for {src}: {e}") from e
    if not os.path.exists(meta):
        raise ToolError(f"exiftool wrote no metadata container for {src}", out, 0)
    return meta


#: The IPTC times. exiftool writes each of them through IptcTime, which completes a time without
#: a zone with the zone of the COMPUTER that runs the rewrite (WriteIPTC.pl, "use local system
#: timezone by default"; no option turns it off): a Capture One export's 15:02:12 came out
#: 15:02:12+09:00 in Japan and -03:00 in Brazil, wherever the photo was taken (audit round 32).
#: ImageMagick carries the IPTC block byte for byte, so a copy that leaves these tags out keeps
#: them exactly as they were: a time without a zone stays without one (the camera recorded none),
#: one with the camera's zone keeps it (owner's decision, 15/11/2026).
IPTC_TIME_TAGS = ("TimeSent", "ReleaseTime", "ExpirationTime", "TimeCreated", "DigitalCreationTime")
IPTC_TIME_READ_ARGS = [f"-IPTC:{t}#" for t in IPTC_TIME_TAGS]
_IPTC_TIME_LINE = re.compile(r"^\[IPTC\]\s+(" + "|".join(IPTC_TIME_TAGS) + r"):\s*(.*?)\s*$")


def parse_iptc_times(out):
    """The IPTC times in what exiftool -a -G1 -s -s printed: ((tag, value), ...) sorted by tag,
    the value as exiftool converts the stored digits (15:02:12, 15:02:12+09:00); () for none."""
    found = {}
    for line in out.split("\n"):
        m = _IPTC_TIME_LINE.match(line)
        if m:
            found.setdefault(m.group(1), m.group(2))
    return tuple(sorted(found.items()))


def iptc_times(path):
    """The IPTC times of `path` (parse_iptc_times). Raises ToolError when exiftool fails."""
    return parse_iptc_times(exiftool(["-a", "-G1", "-s", "-s"] + IPTC_TIME_READ_ARGS + [path]))


def iptc_times_kept_args(src_times, dst_times):
    """The exclusions that leave the IPTC times of a written file as they are: when the file
    already holds every one of them exactly as its source (ImageMagick carried the block), the
    metadata copy must not rewrite them -- exiftool would add the computer's zone to a time that
    has none. [] when the source has none, or the written file lacks or changed one: then they
    are copied as before, so a time is never lost."""
    if src_times and tuple(dst_times) == tuple(src_times):
        return [f"--IPTC:{t}" for t in IPTC_TIME_TAGS]
    return []


def _restore_metadata_args(tag_source, dst, keep=()):
    """The exiftool command that copies every tag of `tag_source` (the source TIFF, or the
    container backup_tiff_metadata made) onto `dst`, a TIFF magick just wrote. `keep`: the
    exclusions of the tags `dst` already holds as they must stay (iptc_times_kept_args).

    ONE -q, not two: the second suppresses WARNINGS, and a warning is the only signal
    exiftool gives when the -tagsfromfile source could not be opened -- it still exits 0.
    IPTC:CodedCharacterSet is not part of -all:all: without it UTF-8 IPTC text was written
    back as Latin, every Japanese keyword a '?' (audit round 28)."""
    return (["-q", "-overwrite_original", "-tagsfromfile", tag_source, "-all:all"] + list(keep)
            + ["-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet", "-unsafe", dst])


def _metadata_restored(r):
    """Whether the restore command (an ExifResult) wrote the tags. Only 'Error opening file'
    is a failure besides the exit status: the SOURCE was never opened. A tag-less source
    legitimately reports "No writable tags set"."""
    return r.status == 0 and not re.search("Error opening file", r.output, re.I)

def tiff_has_only_subfile_pages(path, page_count, allowed=("REDUCEDIMAGE", "REDUCED", "MASK", "PAGE"),
                                timeout=30):
    """True when every page after the first is a thumbnail/preview: its subfiletype is in
    `allowed`, OR it is strictly smaller than the main image (the tag alone is unreliable:
    ImageMagick stamps PAGE on every page it rewrites). A full-size extra page with an
    empty or unlisted tag is a real page (scanner IR, a layer, a second photo). A page that
    cannot be classified fails closed.

    `-ping` (v3.1.1), as tiff_page_count since v3.1: each page's marker and size come from
    its directory. The full identify decoded every page -- 0.3 s for a 45 MP Capture One
    export, which is always multi-page (its thumbnail page) -- for the same answers (95 real
    TIFFs read both ways). Whether the pixels decode is not this probe's question:
    zip_integrity and pixel_identical answer it."""
    if page_count <= 1:
        return True
    r = magick_probe(["identify", "-ping", "-format", "%[tiff:subfiletype]|%w|%h\n", path], timeout)
    if r.timed_out or r.exit_code != 0:
        return False
    lines = [ln for ln in r.lines if ln.strip()]
    if len(lines) < page_count:
        return False

    def _wh(line):
        parts = line.strip().split("|")
        w = h = 0
        if len(parts) >= 3:
            if re.fullmatch(r"\s*[+-]?\d+\s*", parts[1]):
                w = int(parts[1])
            if re.fullmatch(r"\s*[+-]?\d+\s*", parts[2]):
                h = int(parts[2])
        return parts[0].strip(), w, h

    _st0, main_w, main_h = _wh(lines[0])
    allowed_u = {a.upper() for a in allowed}
    for i in range(1, page_count):
        st, w, h = _wh(lines[i])
        if st.upper() in allowed_u:
            continue
        reduced = (main_w > 0 and main_h > 0 and w > 0 and h > 0 and w < main_w and h < main_h)
        if not reduced:
            return False
    return True


def tiff_has_thumbnail_page(path, timeout=30):
    """True when a page of the TIFF is tagged REDUCEDIMAGE (an embedded thumbnail). The
    markers come from the directories (`-ping`, v3.1.1): no page is decoded."""
    r = magick_probe(["identify", "-ping", "-format", "%[tiff:subfiletype]\\n", path], timeout)
    return any(ln.strip().upper() in THUMBNAIL_PAGE_TYPES for ln in r.lines)


_SUBFILE_LINE = re.compile(r"^\[IFD(\d+)\]\s+SubfileType:\s*(\d+)\s*$")


def _parse_subfile_types(out):
    """{ifd index: numeric SubfileType} of the IFDs whose line exiftool printed (`-a -G1 -s
    -s -n -SubfileType`). Read with exiftool: magick's `%[tiff:subfiletype]` is empty both
    when the tag is absent and when it is 0, and every scanner TIFF carries an explicit 0."""
    types = {}
    for line in out.split("\n"):
        m = _SUBFILE_LINE.match(line)
        if m:
            types[int(m.group(1))] = int(m.group(2))
    return types


def subfile_marker_args(src, types, timeout=30):
    """The exiftool assignments that put the source's per-page NewSubfileType markers back
    on a TIFF magick wrote from it (finish_written_tiff writes them). ImageMagick rewrites
    every page and does not keep the tag: a REDUCEDIMAGE thumbnail comes back as PAGE, and
    an IR MASK page as what viewers take for a regular image. A page that had no marker gets
    whatever magick stamped on it cleared; an explicit 0 is written back, not deleted.

    `types`: the source's markers as tiff_facts read them. [] when there is nothing to
    restore (one page), None when the source's page count cannot be read (fail closed: the
    restore counts as failed)."""
    pc = tiff_page_count(src, timeout)
    if not pc.ok:
        return None
    if pc.count <= 1:
        return []
    if types is None:
        return None
    return subfile_marker_list(pc.count, types)


def subfile_marker_list(page_count, types):
    """The marker assignments for a file of `page_count` pages whose source carried `types`
    ({ifd: SubfileType}): the value back where the source had one (an explicit 0 included),
    the tag cleared where it had none. [] for one page: magick only stamps PAGE on a
    multi-page write. The wizard's padded conversion builds its list here too."""
    if page_count <= 1:
        return []
    return [f"-IFD{i}:SubfileType#={types[i]}" if i in types else f"-IFD{i}:SubfileType="
            for i in range(page_count)]

def thumbnail_marker_list(types):
    """The page markers of a --generate-thumbnail output (finish_written_tiff writes them
    with the metadata): page 0 keeps the marker the SOURCE had on its main image (absent
    stays absent), page 1 is stamped REDUCEDIMAGE (1). magick stamps PAGE on every page of a
    multi-page write, so both have to be written. `types`: the source's markers as tiff_facts
    read them. Until v3.1.1-rc1 a read and a rewrite of their own (set_tiff_thumb_markers)."""
    main_tag = f"-IFD0:SubfileType#={types[0]}" if 0 in types else "-IFD0:SubfileType="
    return [main_tag, "-IFD1:SubfileType#=1"]


def history_agent():
    return f"tiff-workflow {TOOLKIT_VERSION} ({TOOLKIT_URL})"


def _history_assignment(parameters):
    """The exiftool assignment that adds this rewrite to xmpMM:History (Action, When,
    SoftwareAgent, Parameters); earlier entries (Capture One, scanner software) are kept.

    `=`, not `+=`, and in the SAME command as the metadata copy (finish_written_tiff): there
    the copied list and this entry are queued together and REPLACE the list the file has --
    the source's entries once, this one after them. `+=` in that command keeps the list
    magick carried over and adds the copied one to it: every earlier entry doubled
    (jxl-photo #531; measured again in v3.1.1). The values must stay free of the struct
    characters , { } [ ] |."""
    now = datetime.now().astimezone()
    off = now.strftime("%z")
    when = now.strftime("%Y-%m-%dT%H:%M:%S") + (off[:3] + ":" + off[3:5] if off else "")
    return (f"-XMP-xmpMM:History={{Action=converted,When={when},"
            f"SoftwareAgent={history_agent()},Parameters={parameters}}}")


class Finished(NamedTuple):
    metadata: bool      # every tag of the source is on the written file
    markers: bool       # the page markers are back (True when there were none to put back)
    history: bool       # the xmpMM:History entry is written (True when none was asked for)


def finish_written_tiff(dst, tag_source, markers, history=None, src_iptc_times=()):
    """The writes onto a TIFF magick just wrote, in ONE exiftool command, so the file is
    rewritten ONCE: the metadata from `tag_source`, then the page `markers`
    (subfile_marker_args: [] = none to write, None = could not be read, so failed), then,
    with `history`, this rewrite in xmpMM:History (`history` is its Parameters text;
    _history_assignment says why it is `=`). Run before the gates, so they check the final
    bytes.

    `src_iptc_times`: the IPTC times of the ORIGINAL source (tiff_facts) -- not of
    `tag_source`, which may be the container, where exiftool already added the computer's
    zone. When `dst` holds them unchanged they are left out of the copy (audit round 32): one
    exiftool READ of `dst` more, never a write.

    Until v3.1.1-rc1 these were three commands, and exiftool writes the WHOLE file for each:
    a 215 MB ZIP of a 45 MP scan was written four times (magick + three), 861 MB on the
    staging disk. The one command gives the same bytes (84 of 87 varied real TIFFs, 7 of 8
    full size; the others keep an XMP namespace prefix the three commands in one exiftool
    process renamed).

    Verdicts, all from the one command: the metadata as before (status 0 and the source was
    opened), the markers by the status, the History by the status and no "error" in what
    exiftool printed -- a warning about any part now fails the History too (the safe side).
    FAIL-CLOSED: exiftool that cannot be started fails all three; never raises for it."""
    keep = []
    if src_iptc_times:
        try:
            keep = iptc_times_kept_args(src_iptc_times, iptc_times(dst))
        except ToolError:
            keep = []       # cannot tell: copied as before, never lost
    command = _restore_metadata_args(tag_source, dst, keep)
    extra = list(markers or [])
    if history is not None:
        extra.append(_history_assignment(history))
    command = command[:-1] + extra + command[-1:]
    try:
        r = exiftool_commands([command])[0]
    except ToolError:
        r = ExifResult(-1, "")
    metadata = _metadata_restored(r)
    done_markers = markers is not None and (not markers or r.status == 0)
    done_history = history is None or (r.status == 0 and not re.search("error", r.output, re.I))
    return Finished(metadata, done_markers, done_history)

class ThumbSafe(NamedTuple):
    ok: bool
    page_count: int
    reason: str


def thumbnail_safe_source(path, timeout=30):
    """Whether --generate-thumbnail may rewrite `path`: the thumbnail path rebuilds the file
    from page 0 only, so EVERY other page of the source is dropped. Replacing an existing
    REDUCEDIMAGE thumbnail is the intent (reason "thumb-replaced"); dropping a scanner IR
    (MASK) page, a layer or a second photo is data loss, and no later gate can see it (a
    page-short file still decodes). An unreadable page count or page fails closed."""
    pc = tiff_page_count(path, timeout)
    if not pc.ok:
        return ThumbSafe(False, 0, f"page count unreadable ({pc.error})")
    if pc.count <= 1:
        return ThumbSafe(True, pc.count, "")
    if tiff_has_only_subfile_pages(path, pc.count, THUMBNAIL_PAGE_TYPES, timeout):
        return ThumbSafe(True, pc.count, "thumb-replaced")
    return ThumbSafe(False, pc.count,
                     f"would drop {pc.count - 1} extra page(s), not all of them thumbnails "
                     f"(scanner IR / layers)")


_LAYOUT_LINE = re.compile(
    r"^\[IFD(\d+)\]\s+(SamplesPerPixel|PhotometricInterpretation|ExtraSamples|BitsPerSample):"
    r"\s*(.*?)\s*$")


def tiff_sample_layout(path):
    """The sample layout of every page of a TIFF, read with exiftool, one
    "<class>/<spp>/<bits>/<alpha>" per IFD in page order. class: gray (Photometric 0/1),
    rgb (2, and 6 = YCbCr, which decodes to RGB), else "p<n>". alpha: the ExtraSamples
    values that are alpha (1 associated, 2 unassociated); 0 (a scanner's IR channel) and an
    absent tag both count as no alpha.

    ImageMagick's TIFF writer picks the output layout from the PIXELS, not from the source
    tags: an RGB file whose every pixel has R=G=B (black-and-white film scanned in RGB) was
    written as a ONE-channel grayscale TIFF with the RGB ICC profile still embedded, and
    the pixel gates decode both through magick, where gray and neutral RGB are equal (audit
    round 28). FAIL-CLOSED: any read or parse failure raises ToolError."""
    return _parse_layout(_read_tags(path, "the sample layout"), path)


#: what tiff_facts and tiff_sample_layout read: the layout tags as numbers (`#`), the
#: compression as the text the SKIP lines print, the size of page 0 (--zip-predictor's probe),
#: the IPTC times (the metadata copy leaves them as they are, audit round 32).
_FACT_TAGS = ["-IFD0:Compression", "-SamplesPerPixel#", "-PhotometricInterpretation#",
              "-ExtraSamples#", "-BitsPerSample#", "-SubfileType#", "-IFD0:ImageWidth#",
              "-IFD0:ImageHeight#"] + IPTC_TIME_READ_ARGS
_COMPRESSION_LINE = re.compile(r"^\[IFD0\]\s+Compression:\s*(.*?)\s*$")
_DIMENSION_LINE = re.compile(r"^\[IFD0\]\s+(ImageWidth|ImageHeight):\s*(\d+)\s*$")


def _read_tags(path, what):
    """The _FACT_TAGS of every IFD of `path` (exiftool -a -G1 -s -s); ToolError when
    exiftool fails."""
    try:
        return exiftool(["-a", "-G1", "-s", "-s"] + _FACT_TAGS + [path])
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"cannot read {what} of {path}: {e}") from e


class TiffFacts(NamedTuple):
    compression: str        # IFD0:Compression as text; "" when IFD0 has none
    pages: Optional[list]   # tiff_sample_layout's answer; None when it cannot vouch for one
    subfile_types: dict     # {ifd: SubfileType} of the IFDs that carry the tag
    width: int = 0          # page 0's size in pixels; 0 when it was not read
    height: int = 0
    iptc_times: tuple = ()  # ((tag, value), ...): parse_iptc_times


def tiff_facts(path):
    """What a rewrite needs to know of its source, in ONE exiftool read (v3.1: three reads
    until then): the IFD0 compression, the sample layout of every page and the per-page
    SubfileType markers. Raises ToolError when exiftool fails; a layout it cannot vouch for
    is pages=None, which the caller refuses as before -- a file it never rewrites (already
    compressed) is not refused for it."""
    out = _read_tags(path, "the tags")
    try:
        pages = _parse_layout(out, path)
    except ToolError:
        pages = None
    comp = ""
    dims = {}
    for line in out.split("\n"):
        m = _COMPRESSION_LINE.match(line)
        if m and not comp:
            comp = m.group(1)
        d = _DIMENSION_LINE.match(line)
        if d:
            dims.setdefault(d.group(1), int(d.group(2)))
    return TiffFacts(comp, pages, _parse_subfile_types(out), dims.get("ImageWidth", 0),
                     dims.get("ImageHeight", 0), parse_iptc_times(out))


def _parse_layout(out, path):
    """tiff_sample_layout's pages from what _read_tags printed; ToolError when the IFDs
    do not describe a layout this can vouch for."""
    ifds = {}
    for line in out.split("\n"):
        m = _LAYOUT_LINE.match(line)
        if m:
            ifds.setdefault(int(m.group(1)), {})[m.group(2)] = m.group(3)
    pages = []
    i = 0
    while i in ifds:
        t = ifds[i]
        if "PhotometricInterpretation" not in t:
            raise ToolError(f"IFD{i} of {path} has no PhotometricInterpretation", out, 0)
        ph = t["PhotometricInterpretation"].strip()
        cls = "p" + ph
        if ph in ("0", "1"):
            cls = "gray"
        elif ph in ("2", "6"):
            cls = "rgb"
        spp = t["SamplesPerPixel"].strip() if "SamplesPerPixel" in t else "1"
        bits = ",".join(t["BitsPerSample"].split()) if "BitsPerSample" in t else "1"
        alpha = []
        if "ExtraSamples" in t:
            alpha = [v for v in t["ExtraSamples"].split() if v in ("1", "2")]
        pages.append(f"{cls}/{spp}/{bits}/{','.join(alpha)}")
        i += 1
    # a gap in the IFD numbering (or no IFD0 at all) is not a layout this can vouch for
    if not pages or len(pages) != len(ifds):
        raise ToolError(f"the IFDs of {path} are not numbered 0..n", out, 0)
    return pages


def magick_keep_type_args(pages):
    """The magick write options that keep the source's channel layout: `-type TrueColor`
    when every page is RGB without alpha (a scanner IR channel included), `-type
    TrueColorAlpha` when every page is RGB with alpha, plus `-define tiff:alpha=associated`
    when all that alpha is associated. Anything else -- a gray page, a mixed file -- gets no
    options: -type applies to every page of the write, and tiff_sample_layout_kept refuses
    the result instead."""
    if not pages:
        return []
    with_alpha = 0
    associated_only = True
    for p in pages:
        parts = str(p).split("/")
        if parts[0] != "rgb":
            return []
        alpha = parts[3] if len(parts) >= 4 else ""
        if alpha:
            with_alpha += 1
            if any(a != "1" for a in alpha.split(",")):
                associated_only = False
    if with_alpha == 0:
        return ["-type", "TrueColor"]
    if with_alpha != len(pages):
        return []
    args = ["-type", "TrueColorAlpha"]
    if associated_only:
        args += ["-define", "tiff:alpha=associated"]
    return args


def associated_alpha_rewritable(path, pages, timeout=30):
    """False when a page carries ASSOCIATED alpha and the image is not fully opaque:
    ImageMagick un-premultiplies on read and re-premultiplies on write, and the alpha
    samples come back changed, even with -define tiff:alpha=associated; no gate that
    decodes through magick sees it. Opaque associated alpha comes back identical.
    Unreadable fails CLOSED."""
    assoc = False
    for p in pages:
        parts = str(p).split("/")
        if len(parts) >= 4 and "1" in parts[3].split(","):
            assoc = True
    if not assoc:
        return True
    r = magick_probe(["identify", "-format", "%[opaque]\n", path], timeout)
    if r.timed_out or r.exit_code != 0:
        return False
    lines = [ln for ln in r.lines if ln.strip()]
    if not lines:
        return False
    return all(ln.strip() == "True" for ln in lines)


def tiff_sample_layout_kept(src_pages, dst, main_page_only=False):
    """True only when `dst` has the sample layout `src_pages` describe: the same number of
    pages and, per page, the same color class, samples per pixel, bits per sample and alpha
    kind. `main_page_only` compares page 0 alone (the --generate-thumbnail path builds a new
    page layout on purpose). Anything unreadable fails CLOSED."""
    if not src_pages:
        return False
    try:
        pages = tiff_sample_layout(dst)
    except ToolError:
        return False
    if main_page_only:
        return pages[0] == src_pages[0]
    if len(pages) != len(src_pages):
        return False
    return all(pages[i] == src_pages[i] for i in range(len(src_pages)))


def ifd0_compression(path):
    """IFD0:Compression as text ("Uncompressed", "Adobe Deflate", "LZW", ...). The main
    image is IFD0: plain -Compression prints one line per IFD, and a file whose thumbnail
    page was the compressed one was skipped as already compressed. Raises ToolError when
    exiftool fails or prints nothing."""
    out = exiftool(["-s", "-s", "-s", "-IFD0:Compression", path])
    lines = [ln for ln in out.split("\n") if ln.strip()]
    if not lines:
        raise ToolError(f"cannot read IFD0:Compression of {path}", out, 0)
    return lines[0].strip()


def is_compressed(comp, skip_lzw):
    """Whether an IFD0:Compression value counts as already compressed."""
    return bool(re.search("Deflate|ZIP|Adobe|LZW" if skip_lzw else "Deflate|ZIP|Adobe", comp, re.I))


def _batch_tags(paths, tags, chunk_size=400):
    """{lower-case full path: [the value of each of `tags`, "-" when absent]}: one exiftool
    -T run per `chunk_size` files instead of one per file. A chunk exiftool fails on is left
    out: its files are MISSING from the map, and the caller reads each one alone."""
    result = {}
    for off in range(0, len(paths), chunk_size):
        chunk = paths[off:off + chunk_size]
        try:
            out = exiftool(["-T", "-Directory", "-FileName"] + list(tags) + chunk)
        except ToolError:
            continue
        for line in out.split("\n"):
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            full = norm_sep(join_path(cols[0], cols[1]))
            values = [c.strip() for c in cols[2:2 + len(tags)]]
            result[full.lower()] = values + ["-"] * (len(tags) - len(values))
    return result


def get_compression_map(paths, chunk_size=400):
    """{lower-case full path: IFD0 compression or "-"}: one exiftool run per `chunk_size`
    files instead of one per file. A file MISSING from the map is probed on its own by the
    caller: a failed chunk never changes a SKIP/compress decision."""
    return {k: v[0] for k, v in _batch_tags(paths, ["-IFD0:Compression"], chunk_size).items()}


# --- --recompress-zip ---------------------------------------------------------------
#
# A TIFF already compressed with Deflate but WITHOUT a predictor (Capture One's exports:
# level 6, no predictor, measured 15/11/2026) comes out about 10 % smaller when ImageMagick
# writes it again: it adds the horizontal predictor for 8 and 16 bits. Some images grow
# instead (one Capture One export, +39 %), so compress keeps a recompressed file only when it
# is smaller (its size gate).

class RecompressFacts(NamedTuple):
    """What decides whether a ZIP TIFF is worth recompressing: page 0's predictor, bits and
    photometric as exiftool prints the numbers ("-" when the tag is absent), and the file's LAST
    xmpMM:History entry (its agent and parameters: the zipcheck record, zipcheck_recorded)."""
    predictor: str          # 1 none, 2 horizontal, 3 floating point
    bits: str               # BitsPerSample, one number per sample: "16 16 16"
    photometric: str        # 0/1 gray, 2 RGB, ...
    last_agent: str = ""
    last_params: str = ""


#: JSON, not -T: the History is a list of structures, and a -T column joins every entry's
#: fields with ", " -- other programs write commas in their Parameters.
_RECOMPRESS_ARGS = ["-j", "-struct", "-IFD0:Predictor#", "-IFD0:BitsPerSample#",
                    "-IFD0:PhotometricInterpretation#", "-XMP-xmpMM:History"]


def is_zip_compression(comp):
    """An IFD0:Compression value that is Deflate (8 "Adobe Deflate", 32946 "Deflate")."""
    return bool(re.search("Deflate|ZIP", str(comp), re.I))


def _recompress_facts_of(entry):
    def text(key):
        v = entry.get(key)
        return "-" if v is None else str(v)
    history = entry.get("History")
    if isinstance(history, dict):
        history = [history]
    last = history[-1] if isinstance(history, list) and history and isinstance(history[-1], dict) else {}
    return RecompressFacts(text("Predictor"), text("BitsPerSample"), text("PhotometricInterpretation"),
                           str(last.get("SoftwareAgent") or ""), str(last.get("Parameters") or ""))


def get_recompress_facts(paths, chunk_size=400):
    """{lower-case full path: RecompressFacts}, one exiftool run per `chunk_size` files. A file
    missing from the map (a chunk exiftool could not answer as JSON) is read alone
    (recompress_facts)."""
    result = {}
    for off in range(0, len(paths), chunk_size):
        chunk = paths[off:off + chunk_size]
        try:
            # one unreadable file must not lose the answers of the others
            data = json.loads(exiftool(_RECOMPRESS_ARGS + chunk, check=False) or "[]")
        except (ToolError, ValueError):
            continue
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict) and entry.get("SourceFile") and not entry.get("Error"):
                result[norm_sep(entry["SourceFile"]).lower()] = _recompress_facts_of(entry)
    return result


def recompress_facts(path):
    """RecompressFacts of one file. Raises ToolError when exiftool fails or prints nothing
    usable: the caller refuses the file, it never guesses a predictor."""
    out = exiftool(_RECOMPRESS_ARGS + [path])
    try:
        data = json.loads(out)
        entry = data[0]
        if not isinstance(entry, dict) or entry.get("Error"):
            raise ValueError("no answer for the file")
    except (ValueError, IndexError, TypeError) as e:
        raise ToolError(f"cannot read the predictor of {path}: {e}", out, 0) from e
    return _recompress_facts_of(entry)


# --- The zipcheck record and the predictor probe ------------------------------------
#
# A file whose predictor tiff-workflow CHOSE (--zip-predictor auto, or a recompression kept
# because it was smaller than the original without one) says so in the xmpMM:History entry the
# rewrite writes anyway: its Parameters end in " - zipcheck 1 predictor 2" (or 1). No extra
# write, and the way jxl-photo keeps its own encode record (in its History entry; its reader
# skips other programs' entries). The record counts only while that entry is the file's LAST
# one: a later program's entry -- jxl-photo's encode and decode included, which carry the
# History into the JXL and back -- means the file was rewritten since.

ZIPCHECK_VERSION = 1
_ZIPCHECK_RE = re.compile(r"\bzipcheck (\d+) predictor ([12])\b")


def zipcheck_segment(predictor):
    """The end of the History Parameters of a file whose predictor was chosen. No struct
    character in it (see _history_assignment)."""
    return f" - zipcheck {ZIPCHECK_VERSION} predictor {int(predictor)}"


def zipcheck_recorded(facts):
    """The predictor (1 or 2) a zipcheck record of this version names, when the file's LAST
    History entry is tiff-workflow's and carries one; else None."""
    if facts is None or not str(facts.last_agent).startswith("tiff-workflow "):
        return None
    m = _ZIPCHECK_RE.search(str(facts.last_params))
    if not m or int(m.group(1)) != ZIPCHECK_VERSION:
        return None
    return int(m.group(2))


#: The sample of page 0 the predictor probe writes: four full-width bands of 16 rows at
#: 20/40/60/80 % of the height (measured 15/11/2026 on 31 varied scans and 22 Capture One
#: exports, the whole files written as compress writes them: no choice against the whole file
#: beyond the 2 % margin; on the 31 scans auto is 0.01 % from the best possible).
_PROBE_BANDS = (0.2, 0.4, 0.6, 0.8)
_PROBE_BAND_ROWS = 16


def predictor_probe(src, width, height, timeout=30, load_factor=1.0, write_args=()):
    """(bytes with the predictor, bytes without) of a SAMPLE of `src`'s page 0 written as ZIP:
    one magick reads the page once and writes the sample twice, two small files in the temp
    folder (a few MB for a 45 MP export), removed. The decision is taken on these so that the
    TIFF itself is written ONCE -- writing it both ways and keeping the smaller would add a full
    temporary TIFF per file. `write_args` are the write's own options (magick_keep_type_args):
    the sample must have the channels the file will have -- without `-type TrueColor` a
    scanner's IR channel is written another way and the sample measures another image (a
    VueScan RGB+IR scan: -47 % without the predictor in such a sample, -5.6 % in the file).
    None when the size is unknown or anything fails: the caller keeps the predictor, today's
    choice."""
    if width <= 0 or height <= 0:
        return None
    with_p, without_p = temp_file_path(".tif"), temp_file_path(".tif")
    page0 = f"{magick_path_spec(src)}[0]"
    if height <= _PROBE_BAND_ROWS * len(_PROBE_BANDS) * 2:
        sample = [page0]
    else:
        sample = [page0, "-write", "mpr:twprobe", "+delete"]
        for share in _PROBE_BANDS:
            sample += ["(", "mpr:twprobe", "-crop",
                       f"{width}x{_PROBE_BAND_ROWS}+0+{int(height * share)}", "+repage", ")"]
        sample.append("-append")
    args = (["-quiet"] + sample + ["+repage", "-compress", "zip"] + [str(a) for a in write_args]
            + ["-define", "tiff:predictor=2", "-write", magick_path_spec(with_p),
               "-define", "tiff:predictor=1", magick_path_spec(without_p)])
    try:
        r = magick_probe(args, _size_scaled_timeout(timeout, [src], load_factor))
        if r.timed_out or r.exit_code != 0:
            return None
        return os.path.getsize(with_p), os.path.getsize(without_p)
    except OSError:
        return None
    finally:
        remove_quietly(with_p)
        remove_quietly(without_p)


def predictor_choice_applies(pages):
    """Whether --zip-predictor auto has a choice to make for a file of this layout (`pages`, as
    tiff_sample_layout gives them; page 0 decides): ImageMagick writes ZIP with the horizontal
    predictor on its own for 8- and 16-bit gray, RGB and CMYK pages (min-is-white included: it
    writes those as min-is-black), and that is what the sample weighs against none. A float page
    is written with the floating-point predictor (3), a palette or a 1-bit page with none: auto
    leaves them as `on` does, with no probe and no record (measured 15/11/2026: a float file's
    History said "predictor 2" while it had 3)."""
    if not pages:
        return False
    parts = str(pages[0]).split("/")
    if len(parts) < 3 or parts[0] not in ("gray", "rgb", "p5"):
        return False
    bits = parts[2].split(",")
    return all(b in ("8", "16") for b in bits)


def recompress_candidate(comp, facts):
    """Whether --recompress-zip rewrites a file: Deflate on page 0, no predictor, 8 or 16
    bits in every sample, gray or RGB (the layouts compress keeps; ImageMagick adds the
    predictor only to those). Anything else stays the SKIP of an already compressed file."""
    if not is_zip_compression(comp) or facts is None:
        return False
    if facts.predictor not in ("-", "", "1"):
        return False
    bits = facts.bits.split()
    if not bits or any(b not in ("8", "16") for b in bits):
        return False
    return facts.photometric in ("0", "1", "2")


# --- Memory ---------------------------------------------------------

def memory_readings():
    """(physical RAM still available, memory the system can still commit), in bytes; an
    entry is None when it cannot be read. Windows: ullAvailPhys and ullAvailPageFile of
    GlobalMemoryStatusEx (RAM + pagefile not yet committed -- the limit WinError 1455
    reports). Elsewhere both are /proc/meminfo MemAvailable (no commit limit by default).
    The wizard's memory budget reads them too."""
    if IS_WINDOWS:
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        try:
            st = _MemoryStatusEx()
            st.dwLength = ctypes.sizeof(st)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None, None
            return int(st.ullAvailPhys), int(st.ullAvailPageFile)
        except Exception:
            return None, None
    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    v = int(line.split()[1]) * 1024
                    return v, v
    except (OSError, ValueError, IndexError):
        pass
    return None, None


def available_memory_bytes():
    """The smaller of the memory the system can still commit and the physical RAM still
    available, in bytes; None when it cannot be read."""
    phys, commit = memory_readings()
    if not IS_WINDOWS:
        return phys
    if phys is None or commit is None or commit <= 0 or phys <= 0:
        return None
    return min(commit, phys)

def max_image_pixels(paths, chunk_size=400):
    """Largest IFD0 width*height among the files, read by exiftool in chunks. A file
    exiftool does not report, or reports without a size, counts as UNKNOWN_IMAGE_PIXELS: an
    unknown size must never shrink the estimate."""
    best = 0.0
    for off in range(0, len(paths), chunk_size):
        chunk = paths[off:off + chunk_size]
        seen = {}
        try:
            out = exiftool(["-T", "-n", "-Directory", "-FileName",
                            "-IFD0:ImageWidth", "-IFD0:ImageHeight"] + chunk, check=False)
        except ToolError:
            out = ""            # not started: every size of the chunk is unknown
        for line in out.split("\n"):
            cols = line.split("\t")
            if len(cols) < 4:
                continue
            full = norm_sep(join_path(cols[0], cols[1])).lower()
            w, h = cols[2].strip(), cols[3].strip()
            if re.fullmatch(r"\d+", w) and re.fullmatch(r"\d+", h):
                seen[full] = float(int(w)) * float(int(h))
        for p in chunk:
            px = seen.get(norm_sep(p).lower())
            if not px or px <= 0:
                px = float(UNKNOWN_IMAGE_PIXELS)
            if px > best:
                best = px
    return best


def memory_capped_workers(requested, paths, bytes_per_pixel, fraction, log, quiet=False):
    """`requested` lowered so that many parallel ImageMagick processes fit in `fraction`
    of the free memory. Logs the estimate; `quiet` logs only when it lowers the count.
    A fraction of 0 or less = no cap."""
    if fraction <= 0 or requested <= 1 or not paths:
        return requested
    avail = available_memory_bytes()
    if avail is None:
        log("Memory: could not read the available memory -- workers not capped", "WARN")
        return requested
    px = max_image_pixels(paths)
    per = px * bytes_per_pixel
    cap = max(1, int(math.floor(avail * fraction / per)))
    w = min(requested, cap)
    gb = 1024.0 ** 3
    if not quiet or cap < requested:
        log(f"Memory: ~{per / gb:,.1f} GB per worker ({px / 1e6:,.0f} MP) | "
            f"{avail / gb:,.1f} GB available | workers {w}")
    if cap < requested:
        log(f"Workers reduced from {requested} to {w}: {requested} ImageMagick processes at "
            f"~{per / gb:,.1f} GB each would not fit (worker_memory_fraction={fraction:g})", "WARN")
    return w


# --- sRGB profile for colour-managed thumbnails ---------------------
# `-colorspace sRGB` converts between COLORSPACES, not between ICC profiles: on a TIFF that
# ImageMagick already reads as RGB it is a no-op, and `-strip` then throws the source profile
# away, so a ProPhoto/AdobeRGB export produced a thumbnail whose numbers were reinterpreted
# as sRGB. `-profile <icc>` does the real conversion; on an untagged source ImageMagick
# treats it as "assign".

def resolve_srgb_profile(explicit):
    """The sRGB ICC file to use: `explicit` when it exists (None when it does not: the
    caller reports it, an explicit bad path is never silently ignored), else Windows' own
    sRGB profile, else None."""
    if explicit:
        return full_path(explicit) if os.path.isfile(explicit) else None
    win_dir = os.environ.get("SystemRoot") or "C:\\Windows"
    for name in ("sRGB Color Space Profile.icm", "sRGB_ICC_v4_Appearance.icc"):
        cand = os.path.join(win_dir, "System32", "spool", "drivers", "color", name)
        if os.path.isfile(cand):
            return cand
    return None


def zip_level_args(level):
    """The ImageMagick options that write a ZIP (Deflate) TIFF at zlib `level` (1-9): its
    -quality is level x 10 for a ZIP TIFF. Passed on EVERY ZIP write, also at 7: -quality 70 is
    byte for byte what magick writes without it (measured 27/11/2026, 8 and 16 bits), so the
    default changes nothing, and a level set anywhere (the PARAMS table, the settings file, the
    command line) is the level written."""
    return ["-quality", str(int(level) * 10)]


def thumbnail_magick_args(srgb_profile, size):
    """The magick options that turn a page into an sRGB thumbnail no larger than `size`."""
    args = []
    if srgb_profile:
        args += ["-profile", srgb_profile]
    # -colorspace after -profile is a no-op for RGB but still normalises grayscale/CMYK
    args += ["-colorspace", "sRGB", "-strip", "-thumbnail", f"{size}x{size}>"]
    return args


# --- Moves ----------------------------------------------------------

def new_run_id():
    """8 hex characters that separate this run's staged and temp files from another run's.
    Short on purpose: it is prepended to every staged name, and Windows still refuses paths
    over 260 characters."""
    return uuid.uuid4().hex[:8]


def _name_hex(run_id):
    """The 12 hex characters of a staged or temp-sibling name: never, together with the run id,
    decimal digits only. A name of digits only is a date or a counter, and sweep_leftovers
    never removes one (review of v3.3.0: `20190815_201908151430_scan.tif` is a photo), so a
    leftover of this toolkit must not have that shape."""
    while True:
        h = uuid.uuid4().hex[:12]
        if not (run_id + h).isdigit():
            return h


def new_temp_sibling_path(path, run_id):
    """A temporary name next to `path` (same folder, so same volume):
    <path>.<run id>_<12 hex>.tif. LEFTOVER_NAME_RE matches it, so a copy an interrupted run
    leaves behind is never taken for a photo (nor for a stranded original)."""
    return f"{path}.{run_id}_{_name_hex(run_id)}.tif"


def staged_name(run_id, name):
    """<run id>_<12 hex>_<name>: the name of a file in the staging folder."""
    return f"{run_id}_{_name_hex(run_id)}_{name}"


def is_read_only(path):
    try:
        return not os.stat(path).st_mode & 0o200
    except OSError:
        return False


def move_file_replacing(src, dst):
    """Rename `src` onto `dst`, replacing `dst` in ONE step: there is never a moment with
    nothing at `dst` (MoveFileEx REPLACE_EXISTING; a delete followed by a rename left the
    place of a photo empty when the run was killed in between -- audit round 28). Both paths
    must be on one volume. Raises OSError.

    A read-only `dst` is replaced too: a scan copied from a CD keeps that attribute and the
    rename refuses it. It is cleared for the swap and put back when the swap fails, so a
    refused swap leaves the original exactly as it was; the new file is not read-only."""
    was_read_only = os.path.isfile(dst) and is_read_only(dst)
    if was_read_only:
        os.chmod(dst, 0o666)
    try:
        os.replace(src, dst)
    except OSError:
        if was_read_only:
            try:
                os.chmod(dst, 0o444)
            except OSError:
                pass
        raise


def _same_volume(a, b):
    da = os.path.splitdrive(full_path(a))[0].lower()
    db = os.path.splitdrive(full_path(b))[0].lower()
    if IS_WINDOWS:
        return da == db
    try:
        return os.stat(os.path.dirname(full_path(a))).st_dev == os.stat(os.path.dirname(full_path(b))).st_dev
    except OSError:
        return False


def move_file_into_place(src, dst, run_id):
    """Put `src` at `dst` so that `dst` is never a half-written file. Returns None, or the
    error text.

    A move between two volumes is a copy then a delete, and a copy straight onto `dst` left
    a truncated file where the only copy of the image had been when the run died mid-copy
    (audit round 26). The irreversible step comes last, as one rename on the destination
    volume: a file from another volume is first copied to a temp sibling of `dst` (size
    checked), and only that sibling is renamed over `dst`. `src` is removed after."""
    if _same_volume(src, dst):
        try:
            move_file_replacing(src, dst)
            return None
        except OSError as e:
            return f"move failed: {e}"
    tmp = new_temp_sibling_path(dst, run_id)
    try:
        size = os.stat(src).st_size
        shutil.copy2(src, tmp)
        if os.stat(tmp).st_size != size:
            remove_quietly(tmp)
            return "size mismatch after copy"
        if is_read_only(tmp):
            os.chmod(tmp, 0o666)
        move_file_replacing(tmp, dst)
    except OSError as e:
        remove_quietly(tmp)
        return f"copy failed: {e}"
    remove_quietly(src)
    return None


def land_staged_file(stage_path, dest_path, run_id):
    """Move a staged file to its destination and check that it is there: the one way a
    staging loop lands a file. Never a copy straight onto the destination (a staging folder
    on another volume): move_file_into_place. Returns None when `dest_path` now holds the
    staged file, else (kind, detail): ("missing", "") -- the move reported success and
    nothing is there; ("size", "") -- what is there has another size than the staged file;
    ("error", text) -- the move, or reading either file, failed. The caller words the
    error line. The destination folder is the caller's to make."""
    try:
        stage_size = os.stat(stage_path).st_size
        err = move_file_into_place(stage_path, dest_path, run_id)
        if err:
            raise OSError(err)
        if not os.path.exists(dest_path):
            return "missing", ""
        if os.stat(dest_path).st_size != stage_size:
            return "size", ""
    except OSError as e:
        return "error", str(e)
    return None


def cleanup_run_files(dirs, run_id):
    """Remove from `dirs` the staged files THIS run created (names starting with its id).
    Another run sharing the folder keeps its own."""
    prefix = run_id + "_"
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name.startswith(prefix):
                target = os.path.join(d, name)
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    remove_quietly(target)


# --- What a killed run left behind (audit round 34) -------------------
#
# A run that is KILLED (not Ctrl+C: cleanup_run_files never runs) leaves its files where they
# were: a staged file, a temp sibling next to the photo, exiftool's `<name>_exiftool_tmp` (with
# it there, every later exiftool write of that photo fails: "Temporary file already exists"),
# and the temp files of the temp folder. None of them is ever the only copy of a photo: an
# original leaves its place only by the rename that consumes its staged or temp file. The
# risks are a CONCURRENT run's file in flight and a photo that merely looks like a leftover,
# so a name is removed only when ALL of these hold (sweep_leftovers):
#   1. it is exactly one of the shapes built from the name of a file THIS run is about to
#      write in that folder (or a metadata container `tw_<16 hex>.mie` in such a folder);
#   2. its run id is not this run's;
#   3. it is a regular file, not a link;
#   4. it is old: LEFTOVER_MIN_AGE_S since its last write AND its creation (a staged file is
#      given its original's old write time);
#   5. the source of that file is a regular file right now;
#   6. its run id and 12 hex characters are not decimal digits only (review of v3.3.0): a
#      photo named `20190815_201908151430_scan.tif` next to `scan.tif` -- a date and a time
#      put in front by a renaming tool -- has the staged shape. This toolkit never writes such
#      a name (_name_hex); one is not a leftover, and is neither removed nor reported.
# exiftool's `_exiftool_tmp` of a staged file or a temp sibling (exiftool killed while writing
# it) is that file's leftover: the same conditions, with that file's run id.
# A SAFETY constant, not a setting: never in an OVERLAY_SPECS table.
LEFTOVER_MIN_AGE_S = 3600

_SWEEP_TEMP_SIBLING = re.compile(
    r"^(?P<target>.+)\.(?P<id>[0-9a-f]{8})_(?P<hex>[0-9a-f]{12})\.tif$", re.I)
_SWEEP_STAGED = re.compile(r"^(?P<id>[0-9a-f]{8})_(?P<hex>[0-9a-f]{12})_(?P<target>.+)$", re.I)
_SWEEP_EXIFTOOL_TMP = re.compile(r"^(?P<target>.+)_exiftool_tmp$", re.I)


def _sweep_match(name, targets):
    """(the source, the run id or None) when `name` is a leftover shape built from the name of
    a target (`targets`: {lower-case name: source}); else None. Condition 6 is here: a name
    whose id and hex are digits only is no leftover."""
    m = _SWEEP_EXIFTOOL_TMP.match(name)
    if m:
        inner = m.group("target")
        if inner.lower() in targets:
            return targets[inner.lower()], None
        return _sweep_match_named(inner, targets)
    return _sweep_match_named(name, targets)


def _sweep_match_named(name, targets):
    for rx in (_SWEEP_TEMP_SIBLING, _SWEEP_STAGED):
        m = rx.match(name)
        if m and m.group("target").lower() in targets:
            if (m.group("id") + m.group("hex")).isdigit():
                return None
            return targets[m.group("target").lower()], m.group("id")
    return None
#: The temp files of this toolkit (temp_file_path): metadata containers, predictor samples,
#: thumbnails.
TEMP_FILE_RE = re.compile(r"^tw_[0-9a-f]{16}\.(mie|tif|tiff|jpg|jpeg|png)$", re.I)


class Swept(NamedTuple):
    """What sweep_leftovers did: the paths removed (or, in a dry run, that would be), and the
    leftover-shaped ones kept, as (path, why)."""
    removed: list
    kept: list


def _leftover_old_enough(st, now):
    return now - max(st.st_mtime, st.st_ctime) >= LEFTOVER_MIN_AGE_S


def sweep_leftovers(pairs, run_id, dry_run):
    """Remove what a killed run left next to the files this run is about to write. `pairs`:
    (the file this run writes, the source it is made from). One listing per folder of a
    target. The six conditions are in the comment above; anything else of a leftover's shape
    is kept and reported. Never raises: a delete that fails is "kept"."""
    folders = {}
    for target, source in pairs:
        d = os.path.dirname(str(target))
        entry = folders.setdefault(d.lower(), (d, {}))
        entry[1].setdefault(os.path.basename(str(target)).lower(), str(source))
    removed, kept = [], []
    now = time.time()
    for d, targets in folders.values():
        try:
            with os.scandir(long_path(d)) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            source = None
            other_run = True
            found = _sweep_match(e.name, targets)
            if found is not None:
                source, its_id = found
                other_run = its_id is None or its_id.lower() != run_id.lower()
            if source is None and e.name.lower().endswith(".mie") and TEMP_FILE_RE.match(e.name):
                # a metadata container (metadata_container_path) next to the photos: any
                # source of this folder that is there
                source = next((s for s in targets.values()
                               if os.path.isfile(long_path(s))), next(iter(targets.values())))
            if source is None:
                continue
            path = os.path.join(d, e.name)
            why = _leftover_kept_why(e, now, other_run, source)
            if why:
                kept.append((path, why))
                continue
            if not dry_run:
                try:
                    os.remove(long_path(path))
                except OSError as err:
                    kept.append((path, f"not removed: {err}"))
                    continue
            removed.append(path)
    return Swept(removed, kept)


def report_swept(log, swept, dry_run):
    """The lines of a sweep_leftovers result, on the caller's thread: what was removed (up to
    ten names) and what of that shape was kept."""
    if swept.removed:
        verb = "Would remove" if dry_run else "Removed"
        log(f"{verb} {len(swept.removed)} leftover file(s) of an interrupted run")
        for path in swept.removed[:10]:
            log(f"  {path}")
        if len(swept.removed) > 10:
            log(f"  ... and {len(swept.removed) - 10} more")
    kept = [(p, why) for p, why in swept.kept if why != "this run's own"]
    if kept:
        log(f"Kept {len(kept)} file(s) shaped like the leftovers of an interrupted run (too "
            f"recent, or their original is missing): {kept[0][0]} ({kept[0][1]})"
            + (f" and {len(kept) - 1} more" if len(kept) > 1 else ""), "WARN")


def _leftover_kept_why(entry, now, other_run, source):
    """Why a leftover-shaped entry stays, or "" when it may go."""
    if not other_run:
        return "this run's own"
    try:
        if not entry.is_file(follow_symlinks=False) or entry_flags(entry)[1]:
            return "not a regular file"
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return "unreadable"
    if not _leftover_old_enough(st, now):
        return "too recent"
    if not os.path.isfile(long_path(source)):
        return "its original is missing"
    return ""


def sweep_temp_files(dry_run=False):
    """Remove this toolkit's own temp files (TEMP_FILE_RE) older than LEFTOVER_MIN_AGE_S from
    the temp folder: what a killed run left there. Returns how many. Never raises."""
    folder = tempfile.gettempdir()
    count = 0
    now = time.time()
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except OSError:
        return 0
    for e in entries:
        if not TEMP_FILE_RE.match(e.name):
            continue
        try:
            if not e.is_file(follow_symlinks=False) or entry_flags(e)[1]:
                continue
            if not _leftover_old_enough(e.stat(follow_symlinks=False), now):
                continue
            if not dry_run:
                os.remove(os.path.join(folder, e.name))
            count += 1
        except OSError:
            continue
    return count


def staged_leftovers(staging_dir, run_id):
    """(how many, bytes) of the files in a --staging-dir that an EARLIER run left there: the
    staged shape, another run id, old. REPORTED, never removed: a staging folder is shared by
    presets of different folders, and a staged file waits there until its group ends -- hours
    on a big folder -- so age alone proves nothing (audit round 34)."""
    count = size = 0
    now = time.time()
    try:
        with os.scandir(long_path(staging_dir)) as it:
            entries = list(it)
    except OSError:
        return 0, 0
    for e in entries:
        m = _SWEEP_STAGED.match(e.name)
        if (not m or m.group("id").lower() == run_id.lower()
                or (m.group("id") + m.group("hex")).isdigit()):
            continue
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if _leftover_old_enough(st, now):
            count += 1
            size += st.st_size
    return count, size


def size_text(n):
    """A file size for the log: 234.8 MB, 2.45 GB (powers of ten, like the measurements of the
    docs)."""
    return f"{n / 1e9:.2f} GB" if abs(n) >= 1e9 else f"{n / 1e6:.1f} MB"


def report_staged_leftovers(log, staging_dir, run_id):
    """The NOTE both backends give for what an earlier run left in a --staging-dir
    (staged_leftovers): reported, never removed."""
    count, size = staged_leftovers(staging_dir, run_id)
    if count:
        log(f"NOTE: {count} file(s) ({size_text(size)}) left in the staging folder by an "
            f"earlier run: {staging_dir}", "WARN")


# --- Running the per-file work --------------------------------------

def run_tasks(tasks, worker, workers):
    """Run `worker(task)` for every task, `workers` at a time, and yield (task, result)
    as each finishes, on the caller's thread: the caller logs, counts and moves files; the
    workers only run magick and exiftool. With one worker everything runs on the caller's
    thread, in order. An exception in a worker is yielded as its result."""
    if workers <= 1 or len(tasks) <= 1:
        for t in tasks:
            try:
                yield t, worker(t)
            except Exception as e:      # reported by the caller as that file's error
                yield t, e
        return
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(worker, t): t for t in tasks}
        pending = set(futures)
        while pending:
            # A short timeout, not a plain wait: on Windows a wait on a lock cannot be
            # interrupted, and Ctrl+C would only be seen when the next file finished.
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for fut in done:
                t = futures[fut]
                try:
                    yield t, fut.result()
                except Exception as e:
                    yield t, e
    finally:
        # also on Ctrl+C: nothing new starts, and the files in flight finish their step
        pool.shutdown(wait=True, cancel_futures=True)
