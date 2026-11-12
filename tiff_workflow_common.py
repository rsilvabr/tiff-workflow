#!/usr/bin/env python3
"""Shared code of the tiff-workflow backends (compress_tiff_zip.py, copy_exif_to_tiff.py,
generate_thumbnails.py): the command line, the settings overlay, the log, the calls to
ImageMagick and exiftool, the gates that decide whether a rewritten TIFF may replace its
original, and the moves that put it in place.

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
import uuid
from datetime import datetime
from enum import Enum
from typing import NamedTuple, Optional

# The tiff-workflow release this file belongs to. Not a setting: tests/test_version_parity.py
# requires the same value in convert_tiff.py's TOOLKIT_VERSION.
TOOLKIT_VERSION = "3.0.0"
TOOLKIT_URL = "https://github.com/rsilvabr/tiff-workflow"

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

# The page markers that make an extra page a thumbnail, for SafeMode and -GenerateThumbnail.
# MASK and PAGE are NOT in it: they are real pages (a scanner's IR channel, a second photo),
# and with the default list of tiff_has_only_subfile_pages, which has them, an RGB+IR scan
# was accepted and rewritten (audit round 9).
THUMBNAIL_PAGE_TYPES = ("REDUCEDIMAGE", "REDUCED")


# --- Command line ---------------------------------------------------

class P:
    """One command-line parameter of a backend.

    kind: "int", "float", "str", "bool" (takes a value: -SafeMode:$false) or "switch"
    (-DryRun; -DryRun:$false is off). lo/hi: allowed range of a number. choices: allowed
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


_BOOL_WORDS = {"$true": True, "true": True, "1": True,
               "$false": False, "false": False, "0": False}
_PARAM_TOKEN = re.compile(r"-([A-Za-z][A-Za-z0-9]*)(?::(.*))?", re.S)


def _resolve_param(word, params):
    low = word.lower()
    for p in params:
        if p.name.lower() == low:
            return p
    hits = [p for p in params if p.name.lower().startswith(low)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise UsageError(f"unknown parameter -{word}")
    raise UsageError(f"-{word} is ambiguous: " + ", ".join("-" + p.name for p in hits))


def _convert_param(p, raw):
    if p.kind == "int":
        if not re.fullmatch(r"\s*[+-]?\d+\s*", raw):
            raise UsageError(f"-{p.name} needs a whole number, got '{raw}'")
        val = int(raw)
    elif p.kind == "float":
        try:
            val = float(raw)
        except ValueError:
            raise UsageError(f"-{p.name} needs a number, got '{raw}'")
        if not math.isfinite(val):
            raise UsageError(f"-{p.name} needs a number, got '{raw}'")
    elif p.kind == "bool":
        if raw.strip().lower() not in _BOOL_WORDS:
            raise UsageError(f"-{p.name} needs $true or $false, got '{raw}'")
        return _BOOL_WORDS[raw.strip().lower()]
    else:
        val = raw
    if p.kind in ("int", "float"):
        if (p.lo is not None and val < p.lo) or (p.hi is not None and val > p.hi):
            raise UsageError(f"-{p.name} must be from {p.lo} to {p.hi}, got {raw}")
    if p.choices:
        for c in p.choices:
            if c.lower() == str(val).lower():
                return c
        raise UsageError(f"-{p.name} must be one of {', '.join(p.choices)}, got '{raw}'")
    if p.pattern and not re.search(p.pattern, str(val)):
        raise UsageError(f"-{p.name}: '{raw}' is not a valid value")
    return val


def parse_args(argv, params):
    """Bind `argv` the way the PowerShell backends bound theirs, so a command line written
    for a .ps1 (a saved preset, a .bat) still means the same: `-Name value`, `-Name:value`,
    `-Switch`, `-Switch:$false`; names without case; an unambiguous prefix of a name is
    that name. Returns ({name: value}, {names given on the command line}). Raises
    UsageError for anything it cannot bind -- a value without its -Parameter included."""
    values = {p.name: p.default for p in params}
    bound = set()
    i = 0
    while i < len(argv):
        tok = str(argv[i])
        i += 1
        m = _PARAM_TOKEN.fullmatch(tok)
        if not m:
            raise UsageError(f"unexpected argument '{tok}' (every value needs its -Parameter)")
        p = _resolve_param(m.group(1), params)
        inline = m.group(2)
        if p.kind == "switch":
            if inline is None:
                val = True
            elif inline.strip().lower() in _BOOL_WORDS:
                val = _BOOL_WORDS[inline.strip().lower()]
            else:
                raise UsageError(f"-{p.name} is a switch: -{p.name} or -{p.name}:$false")
        else:
            if inline is not None:
                raw = inline
            else:
                if i >= len(argv):
                    raise UsageError(f"-{p.name} needs a value")
                raw = str(argv[i])
                if p.kind == "str":
                    m2 = _PARAM_TOKEN.fullmatch(raw)
                    if m2 and m2.group(2) is None:
                        try:
                            _resolve_param(m2.group(1), params)
                            is_param = True
                        except UsageError:
                            is_param = False
                        if is_param:
                            raise UsageError(f"-{p.name} needs a value")
                i += 1
            val = _convert_param(p, raw)
        values[p.name] = val
        bound.add(p.name)
    return values, bound


# --- Settings overlay -----------------------------------------------
#
# `.tiff_workflow_settings.json` changes the settings at the top of the scripts without
# editing them. Only the names in a script's OVERLAY_SPECS are taken, each checked against its
# spec; nothing that deletes, overwrites or is chosen per run is ever in a spec table. A
# -Param on the command line wins; a bad file or value changes nothing and warns.
# The three functions below are copies of convert_tiff.py's (tests/test_settings_overlay.py
# keeps them identical): the wizard plans with the values the backend will run with.

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
    home = (os.environ.get("USERPROFILE") if IS_WINDOWS else None) or os.path.expanduser("~")
    return os.path.join(home, SETTINGS_OVERLAY_FILE)


def read_settings_overlay(section, specs, script_root):
    """{"path", "values": {name: checked value}, "warnings": [text]} of `section` of the
    overlay file. NEVER raises: a file that cannot be read changes nothing and says so."""
    result = {"path": "", "values": {}, "warnings": []}
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
            ok, checked = _overlay_value(spec, value)
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


def apply_settings_overlay(section, specs, values, bound, script_root):
    """Apply the overlay to `values` (the parsed parameters), before anything reads one. A
    parameter given on the command line (`bound`) always beats it. Returns
    {"path", "applied": [names], "warnings": [text], "info": the line to print or ""}."""
    overlay = read_settings_overlay(section, specs, script_root)
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
    """The entries of -InputDir / -InputDirFile as (the ones that exist, the ones that do
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
    """The usage error for an -OutputDir that names an existing FILE, or "". Until v3.0.0
    compress ended in `FATAL (unhandled error) | FileExistsError` when it tried to make the
    folder; the answer is the same (exit code 1, nothing touched), said before anything
    starts. A relative path is checked against the current folder."""
    path = str(path).strip()
    if path and os.path.isfile(full_path(path)):
        return f"-OutputDir is a file, not a folder: {full_path(path)}"
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


def parse_exclude_folders(text):
    """The ';'-separated folder NAMES of -ExcludeFolders, or raises UsageError for a path."""
    names = []
    for entry in str(text or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if "\\" in entry or "/" in entry:
            raise UsageError(
                f"-ExcludeFolders takes folder NAMES, not paths: '{entry}'\n"
                "       Use a ';'-separated list of bare names, e.g. -ExcludeFolders '_EXPORT;temp'")
        names.append(entry)
    return names


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
    writes it when -InputDir would pass the command-line limit). Raises UsageError."""
    if not os.path.isfile(path):
        raise UsageError(f"-InputDirFile not found: {path}")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            entries = [ln.strip() for ln in fh.read().splitlines()]
    except (OSError, UnicodeError) as e:
        raise UsageError(f"cannot read -InputDirFile: {path} ({e})")
    entries = [e for e in entries if e]
    if not entries:
        raise UsageError(f"-InputDirFile lists no folders: {path}")
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
    Raises OSError when the program cannot be started and subprocess.TimeoutExpired when
    `timeout` seconds pass (the program is killed first)."""
    stdin_kw = {"stdin": subprocess.DEVNULL} if input is None else {"input": input}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        r = subprocess.run(cmd, stdout=out, stderr=err, timeout=timeout, **stdin_kw)
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
    count: int
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
    if ps.count != pd.count:
        return False
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
    """The gate for a -GenerateThumbnail output: True only when the output has exactly
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

    FAIL-CLOSED: raises ToolError when the container is needed and could not be written;
    the caller must refuse the write, which would strip every tag."""
    if not same_path(write_dst, src) and "%" not in str(src):
        return None
    meta = temp_file_path(".mie")
    # `-o` is format-expanded like `-tagsfromfile`: a '%' in the temp folder would send the
    # container somewhere else.
    if "%" in meta:
        raise ToolError(f"no metadata container: the temp folder has a '%' in its name ({meta})")
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


def _restore_metadata_args(tag_source, dst):
    """The exiftool command that copies every tag of `tag_source` (the source TIFF, or the
    container backup_tiff_metadata made) onto `dst`, a TIFF magick just wrote.

    ONE -q, not two: the second suppresses WARNINGS, and a warning is the only signal
    exiftool gives when the -tagsfromfile source could not be opened -- it still exits 0.
    IPTC:CodedCharacterSet is not part of -all:all: without it UTF-8 IPTC text was written
    back as Latin, every Japanese keyword a '?' (audit round 28)."""
    return ["-q", "-overwrite_original", "-tagsfromfile", tag_source, "-all:all",
            "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet", "-unsafe", dst]


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
    cannot be classified fails closed."""
    if page_count <= 1:
        return True
    r = magick_probe(["identify", "-format", "%[tiff:subfiletype]|%w|%h\n", path], timeout)
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
    """True when a page of the TIFF is tagged REDUCEDIMAGE (an embedded thumbnail)."""
    r = magick_probe(["identify", "-format", "%[tiff:subfiletype]\\n", path], timeout)
    return any(ln.strip().upper() in THUMBNAIL_PAGE_TYPES for ln in r.lines)


_SUBFILE_LINE = re.compile(r"^\[IFD(\d+)\]\s+SubfileType:\s*(\d+)\s*$")


def _read_subfile_types(path):
    """{ifd index: numeric SubfileType} of the IFDs that HAVE the tag, or None on failure.
    Read with exiftool: magick's `%[tiff:subfiletype]` is empty both when the tag is absent
    and when it is 0, and every scanner TIFF carries an explicit 0."""
    try:
        out = exiftool(["-a", "-G1", "-s", "-s", "-n", "-SubfileType", path])
    except ToolError:
        return None
    return _parse_subfile_types(out)


def _parse_subfile_types(out):
    """{ifd index: numeric SubfileType} of the IFDs whose line exiftool printed."""
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

def set_tiff_thumb_markers(src, dst):
    """The page markers of a -GenerateThumbnail output: page 0 keeps the marker the SOURCE
    had on its main image (absent stays absent), page 1 is stamped REDUCEDIMAGE (1). magick
    stamps PAGE on every page of a multi-page write, so both have to be written."""
    types = _read_subfile_types(src)
    if types is None:
        return False
    main_tag = f"-IFD0:SubfileType#={types[0]}" if 0 in types else "-IFD0:SubfileType="
    try:
        exiftool(["-q", "-q", "-overwrite_original", main_tag, "-IFD1:SubfileType#=1", dst],
                 merge_stderr=True)
    except ToolError:
        return False
    return True


def history_agent():
    return f"tiff-workflow {TOOLKIT_VERSION} ({TOOLKIT_URL})"


def _history_args(path, parameters):
    """The exiftool command that appends this rewrite to xmpMM:History (Action, When,
    SoftwareAgent, Parameters); earlier entries (Capture One, scanner software) are kept.
    A command of its OWN on purpose: in the same command as `-tagsfromfile -all:all`, `+=`
    doubles every copied entry (jxl-photo #531) -- an `-execute` of the same process is a
    command of its own. The values must stay free of the struct characters , { } [ ] |."""
    now = datetime.now().astimezone()
    off = now.strftime("%z")
    when = now.strftime("%Y-%m-%dT%H:%M:%S") + (off[:3] + ":" + off[3:5] if off else "")
    assign = (f"-XMP-xmpMM:History+={{Action=converted,When={when},"
              f"SoftwareAgent={history_agent()},Parameters={parameters}}}")
    return ["-q", "-overwrite_original", assign, path]


class Finished(NamedTuple):
    metadata: bool      # every tag of the source is on the written file
    markers: bool       # the page markers are back (True when there were none to put back)
    history: bool       # the xmpMM:History entry is written (True when none was asked for)


def finish_written_tiff(dst, tag_source, markers, history=None):
    """The writes onto a TIFF magick just wrote, in ONE exiftool (v3.1 phase 4: three
    processes until then), each a command of its own with its own verdict: the metadata
    from `tag_source`, the page `markers` (subfile_marker_args: [] = none to write, None =
    could not be read, so failed), and, with `history`, this rewrite in xmpMM:History
    (`history` is its Parameters text). Run before the gates, so they check the final bytes.
    FAIL-CLOSED: exiftool that cannot be started fails all three; never raises for it."""
    commands = [_restore_metadata_args(tag_source, dst)]
    if markers:
        commands.append(["-q", "-q", "-overwrite_original"] + list(markers) + [dst])
    if history is not None:
        commands.append(_history_args(dst, history))
    try:
        results = exiftool_commands(commands)
    except ToolError:
        results = [ExifResult(-1, "")] * len(commands)
    metadata = _metadata_restored(results[0])
    done_markers = markers is not None
    i = 1
    if markers:
        done_markers = results[i].status == 0
        i += 1
    done_history = True
    if history is not None:
        r = results[i]
        done_history = r.status == 0 and not re.search("error", r.output, re.I)
    return Finished(metadata, done_markers, done_history)

class ThumbSafe(NamedTuple):
    ok: bool
    page_count: int
    reason: str


def thumbnail_safe_source(path, timeout=30):
    """Whether -GenerateThumbnail may rewrite `path`: the thumbnail path rebuilds the file
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
#: compression as the text the SKIP lines print.
_FACT_TAGS = ["-IFD0:Compression", "-SamplesPerPixel#", "-PhotometricInterpretation#",
              "-ExtraSamples#", "-BitsPerSample#", "-SubfileType#"]
_COMPRESSION_LINE = re.compile(r"^\[IFD0\]\s+Compression:\s*(.*?)\s*$")


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
    for line in out.split("\n"):
        m = _COMPRESSION_LINE.match(line)
        if m:
            comp = m.group(1)
            break
    return TiffFacts(comp, pages, _parse_subfile_types(out))


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
    kind. `main_page_only` compares page 0 alone (the -GenerateThumbnail path builds a new
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


def get_compression_map(paths, chunk_size=400):
    """{lower-case full path: IFD0 compression or "-"}: one exiftool run per `chunk_size`
    files instead of one per file. A file MISSING from the map is probed on its own by the
    caller: a failed chunk never changes a SKIP/compress decision."""
    result = {}
    for off in range(0, len(paths), chunk_size):
        chunk = paths[off:off + chunk_size]
        try:
            out = exiftool(["-T", "-Directory", "-FileName", "-IFD0:Compression"] + chunk)
        except ToolError:
            continue
        for line in out.split("\n"):
            cols = line.split("\t", 2)
            if len(cols) < 2:
                continue
            full = norm_sep(join_path(cols[0], cols[1]))
            comp = cols[2].strip() if len(cols) >= 3 else "-"
            result[full.lower()] = comp
    return result


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
            f"~{per / gb:,.1f} GB each would not fit (WorkerMemoryFraction={fraction:g})", "WARN")
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


def new_temp_sibling_path(path, run_id):
    """A temporary name next to `path` (same folder, so same volume):
    <path>.<run id>_<12 hex>.tif. LEFTOVER_NAME_RE matches it, so a copy an interrupted run
    leaves behind is never taken for a photo (nor for a stranded original)."""
    return f"{path}.{run_id}_{uuid.uuid4().hex[:12]}.tif"


def staged_name(run_id, name):
    """<run id>_<12 hex>_<name>: the name of a file in the staging folder."""
    return f"{run_id}_{uuid.uuid4().hex[:12]}_{name}"


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
