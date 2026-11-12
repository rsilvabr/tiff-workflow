#!/usr/bin/env python3
"""generate_thumbnails.py -- sRGB thumbnails next to TIFFs (or in --output-dir), and their
removal.

    python generate_thumbnails.py --input-dir <folder> [--recursive] [--size 256] [--format jpg]
    python generate_thumbnails.py --input-dir <folder> --remove [--dry-run]

The command line (v3.1): `--name value`, every yes/no as `--x` / `--no-x`, also as
`convert_tiff.py thumbnails ...`; `--help` lists the options. tools/legacy_args.py translates
a line of v3.0.0 or of the .ps1 (`-Name value`, `-SafeMode:$false`). Exit code 0 when
every file went right, 1 when any file failed or an input folder does not exist, 2 when
the command line cannot run as written. docs/README_generate_thumbnails.md has the
reference.
"""

import os
import re
import sys
from typing import NamedTuple, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tiff_workflow_common as tw  # noqa: E402
from tiff_workflow_common import P  # noqa: E402

# -- CLI PARAMETERS -----------------------------------------------
# The defaults are the settings of this script: edit them here, or leave the file alone
# and put them in .tiff_workflow_settings.json (the names in OVERLAY_SPECS).
PARAMS = (
    P("input_dir", "str", "."),
    P("size", "int", 256),
    P("output_dir", "str", ""),
    P("remove", "switch"),
    P("dry_run", "switch"),
    P("recursive", "switch"),
    P("workers", "int", 4, lo=1, hi=64),
    P("page", "str", "0", pattern=r"^(all|\d+)$"),
    P("quality", "str", "85", pattern=r"^(100|[1-9][0-9]?)$"),
    P("format", "str", "jpg"),
    P("srgb_profile", "str", "", help='ICC profile used to convert wide-gamut sources; "" = auto-detect'),
    P("exclude_folders", "str", "", help="';'-separated folder NAMES to skip during discovery"),
)

# Settings overlay (.tiff_workflow_settings.json): the ONLY names it can change in this script,
# each with the type/range it must have. Never a safety or per-run setting (input_dir,
# output_dir, workers, dry_run, remove, ...): tests/test_settings_overlay.py pins that.
# OVERLAY-SPECS-BEGIN
OVERLAY_SPECS = {
    "size": "int:1:",
    "quality": "intstr:1:100",
    "format": "name",
    "srgb_profile": "path?",
}
# OVERLAY-SPECS-END

SCRIPT_NAME = "generate_thumbnails"
FORMATS = ("jpg", "jpeg", "png", "tif", "tiff")

# Every thumbnail this script can produce, in one pattern:
#   name_thumb        plain
#   name_thumb-0      one frame per page (--page all)
#   name_thumb_v2     collision rename under --output-dir
#   name_thumb_v2-0   both
# Matching only '_thumb(-\d+)?$' left the _v2 variants out of BOTH the self-exclusion and
# the --remove filter, so a renamed .tif thumbnail was rescanned as a source on the next run
# (producing _thumb_v2_thumb) and could never be cleaned up.
THUMB_NAME_RE = re.compile(r"_thumb(_v\d+)?(-\d+)?$", re.I)

# Folder names skipped during discovery, matched RELATIVE to the root each file was found
# under. `ZIP` and `_EXPORT` are deliberately NOT here: `_EXPORT` is what compress modes 6/7
# exist to process, and `ZIP/` is where modes 1/3/5/6/7 PUT the compressed library.
TOOL_OUTPUT_NAMES = ("OLD_TIFFs", "OLD_TIFF", "converted_zip")

# The thumbnails made at once, whatever --workers asks for (it accepts up to 64, as the
# other backends do): each one is a magick process that reads a whole TIFF. A run that asks
# for more says so -- until v3.0.0 the limit was applied without a word.
MAX_PARALLEL = 16


class Counters:
    def __init__(self):
        self.ok = self.skip = self.err = self.counter = self.total = 0


def frame_files(dest_path):
    """The `name-0.jpg, name-1.jpg, ...` files magick writes for --page all with a
    single-frame format. The names are compared as text, never as a pattern: a '[' in the
    basename is not a wildcard. Digits only between the dash and the extension: a
    name_thumb-old.jpg saved by hand is not a frame."""
    d = os.path.dirname(dest_path)
    base, ext = os.path.splitext(os.path.basename(dest_path))
    prefix = (base + "-").lower()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        low = n.lower()
        if low.startswith(prefix) and low.endswith(ext.lower()):
            mid = n[len(prefix):len(n) - len(ext)]
            if re.fullmatch(r"\d+", mid) and os.path.isfile(os.path.join(d, n)):
                out.append(n)
    return out


def thumb_exists(dest_path, frame_names):
    """True when a thumbnail for `dest_path` is already on disk. The frame set is only
    looked for with --page all: counted always, a `--page all` run left `--page 0` unable to
    ever write name.jpg."""
    if os.path.exists(dest_path):
        return True
    if not frame_names:
        return False
    return len(frame_files(dest_path)) > 0


class ThumbSettings(NamedTuple):
    """What every thumbnail of a run is made with."""
    size: int
    quality: str
    format: str
    page: str
    dry_run: bool
    srgb_profile: Optional[str]


def make_thumbnail(src, dest, cfg):
    """One thumbnail; returns the result line."""
    name = os.path.basename(src)
    if thumb_exists(dest, cfg.page == "all") and not cfg.dry_run:
        return f"SKIP (exists) | {name}"
    if cfg.dry_run:
        return f"DRY-RUN | {name} -> {os.path.basename(dest)}"
    try:
        dest_dir = os.path.dirname(dest)
        os.makedirs(dest_dir, exist_ok=True)
        # A '[n]' suffix turns the input into a filename SPEC, where '%' is expanded; a
        # plain input (--page all) is opened literally and must stay unescaped.
        if cfg.page == "all":
            source = src
        else:
            source = f"{tw.magick_path_spec(src)}[{cfg.page}]"
        args = [source] + tw.thumbnail_magick_args(cfg.srgb_profile, cfg.size)
        # -quality only where it means JPEG quality. ImageMagick reinterprets it for PNG as
        # zlib level + filter (85 -> level 8, filter 5); TIFF ignores it.
        fmt = cfg.format.lower()
        if fmt in ("jpg", "jpeg"):
            args += ["-quality", cfg.quality, "-interlace", "Plane"]
        elif fmt in ("tif", "tiff"):
            args += ["-compress", "zip"]
        # The output path is always escaped: a '%' in the name would send the thumbnail
        # somewhere else while magick still exits 0.
        args.append(tw.magick_path_spec(dest))
        try:
            tw.magick_run(args)
        except tw.ToolError as e:
            first = next((ln.strip() for ln in e.output.splitlines() if ln.strip()), "")
            return f"ERROR (magick failed) | {name} | {first}"
        if os.path.exists(dest):
            return f"OK | {name} -> {os.path.basename(dest)} ({os.path.getsize(dest)} bytes)"
        frames = frame_files(dest)
        if frames:
            base, ext = os.path.splitext(os.path.basename(dest))
            return f"OK | {name} -> {base}-*{ext} ({len(frames)} frames)"
        return f"ERROR (output not created) | {name}"
    except Exception as e:
        return f"ERROR | {name} | {e}"


def _input_roots(opt):
    """(the --input-dir entries that exist, the ones that do not). A ';' separated list like
    the other backends; a bad path is an ERROR, not a WARN: a typo used to look identical
    to "folder has no TIFFs" and exited 0."""
    return tw.resolve_input_dirs(opt["input_dir"].split(";"), allow_files=True)


def _discover(log, input_roots, recurse, tool_output_names):
    """The TIFFs to make thumbnails of, in path order.

    Each file carries the root it was discovered under, so the exclusion is evaluated
    relative to that root. A file passed directly as --input-dir is its own root: an
    explicit path always wins."""
    unreadable = []
    by_path = {}
    for root in input_roots:
        if os.path.isfile(root):
            if tw.is_tiff_name(root):
                st = os.stat(root)
                f = tw.FoundFile(root, os.path.basename(root), os.path.dirname(root),
                                 st.st_size, st.st_mtime_ns)
                f.input_root = f.dir
                by_path.setdefault(root.lower(), f)
            continue
        for f in tw.list_files(root, recurse, unreadable):
            if tw.is_tiff_name(f.name):
                f.input_root = root
                # Dedupe: a repeated --input-dir entry (A;A) or a root nested under a
                # --recursive root lists the same source twice.
                by_path.setdefault(f.path.lower(), f)
    for d in unreadable:
        log(f"cannot read folder (skipped): {d}", "WARN")
    # Ordered: which of two same-name sources claims photo_thumb.jpg must not vary by run.
    all_files = [by_path[k] for k in sorted(by_path)]
    files = [f for f in all_files
             if not tw.path_excluded(f.dir, f.input_root, tool_output_names)
             and not THUMB_NAME_RE.search(f.stem)]
    excluded = len(all_files) - len(files)
    if excluded > 0:
        log(f"Excluded {excluded} file(s): existing thumbnails, or under {'; '.join(tool_output_names)}")
    return files


def _plan(log, files, output_dir, fmt):
    """[(source, thumbnail path)] for `files`.

    claimed: with --output-dir + --recursive, a\\photo.tif and b\\photo.tif both map to
    photo_thumb.jpg. The second one used to report "SKIP (exists)" and never be generated,
    so later claimants are numbered instead."""
    claimed = {}
    pairs = []
    for f in files:
        if output_dir:
            out_dir = output_dir if os.path.isabs(output_dir) else tw.join_path(f.dir, output_dir)
        else:
            out_dir = f.dir
        dest = tw.join_path(out_dir, f"{f.stem}_thumb.{fmt}")
        if dest.lower() in claimed:
            cand_name, cand = tw.numbered_name(out_dir, f"{f.stem}_thumb", f".{fmt}",
                                               lambda name, path: path.lower() in claimed)
            log(f"RENAME (thumbnail name already taken) | {f.name} -> {cand_name}", "WARN")
            dest = cand
        claimed[dest.lower()] = f.path
        pairs.append((f.path, dest))
    return pairs


def run(argv):
    if tw.asks_for_help(argv):
        tw.say(tw.help_text(PARAMS, os.path.basename(__file__), __doc__))
        return 0
    try:
        opt, bound = tw.parse_args(argv, PARAMS)
    except tw.UsageError as e:
        tw.say(f"ERROR: {e}")
        return tw.EXIT_USAGE

    # The overlay is applied before anything reads a parameter. A --param given on the command
    # line always beats it; an invalid entry never stops the run.
    overlay = tw.apply_settings_overlay(SCRIPT_NAME, OVERLAY_SPECS, opt, bound,
                                        os.path.dirname(os.path.abspath(__file__)))
    if overlay["info"]:
        tw.say(overlay["info"])
    for w in overlay["warnings"]:
        tw.say(f"WARNING: settings overlay: {w}")

    # What the command line alone shows (v3.1, T5: exit 2, before a log exists).
    if opt["size"] < 32 or opt["size"] > 4096:
        tw.say(f"ERROR: thumbnail size {opt['size']} is not supported (--size): "
               f"it must be from 32 to 4096")
        return tw.EXIT_USAGE
    if opt["format"].lower() not in FORMATS:
        tw.say(f"ERROR: thumbnail format {opt['format']} is not supported (--format): "
               f"jpg, png, tif or tiff")
        return tw.EXIT_USAGE
    try:
        exclude_names = tw.parse_exclude_folders(opt["exclude_folders"])
    except tw.UsageError as e:
        tw.say(f"ERROR: {e}")
        return tw.EXIT_USAGE

    # A relative --output-dir is resolved per file folder here, so only an absolute one can be
    # judged before the run.
    if os.path.isabs(opt["output_dir"].strip()) and tw.output_dir_error(opt["output_dir"]):
        tw.say(f"ERROR: {tw.output_dir_error(opt['output_dir'])}")
        return 1

    log = tw.RunLog(SCRIPT_NAME)
    log(f"tiff-workflow {tw.TOOLKIT_VERSION} | generate_thumbnails.py | {tw.python_label()}")
    if overlay["info"]:
        log(overlay["info"])
    for w in overlay["warnings"]:
        log(f"settings overlay: {w}", "WARN")

    missing = tw.missing_tools(["magick"])
    if missing:
        tw.say(f"ERROR: Required tools not found in PATH: {', '.join(missing)}")
        tw.say("Please install the missing tools and try again.")
        return 1

    size = opt["size"]
    fmt = opt["format"]
    page = opt["page"]
    quality = opt["quality"]
    output_dir = opt["output_dir"]
    dry_run = opt["dry_run"]
    recurse = opt["recursive"]

    srgb = tw.resolve_srgb_profile(opt["srgb_profile"])
    # An overlay path that does not exist must not stop the run: warn and auto-detect. A
    # --srgb-profile typed on the command line still fails.
    if opt["srgb_profile"] and not srgb and "srgb_profile" in overlay["applied"]:
        log(f"WARN: settings overlay: srgb_profile not found: {opt['srgb_profile']}; "
            f"using the auto-detected profile", "WARN")
        opt["srgb_profile"] = ""
        srgb = tw.resolve_srgb_profile("")
    if opt["srgb_profile"] and not srgb:
        log(f"ERROR: --srgb-profile not found: {opt['srgb_profile']}", "ERROR")
        return 1
    if not srgb:
        log("WARN: no sRGB ICC profile found -- wide-gamut sources (ProPhoto, AdobeRGB) will "
            "produce thumbnails with shifted colours. Pass --srgb-profile <path.icc> to fix.", "WARN")

    input_roots, missing_roots = _input_roots(opt)
    tool_output_names = list(TOOL_OUTPUT_NAMES) + exclude_names

    files = _discover(log, input_roots, recurse, tool_output_names)

    c = Counters()
    c.total = len(files)
    for m in missing_roots:
        log(f"ERROR: input directory not found: {m}", "ERROR")
    c.err += len(missing_roots)

    if not input_roots:
        log("No valid input directories specified.", "ERROR")
        log(f"Log: {log.path}")
        return 1

    if opt["remove"]:
        return remove_thumbnails(opt, log, c, input_roots, recurse, size)

    if c.total == 0:
        log(f"No TIFF files found in: {'; '.join(input_roots)}", "WARN")
        log(f"Log: {log.path}")
        return 1 if c.err > 0 else 0

    log(f"Found: {c.total} TIFF(s)")
    log(f"Size: {size}px | Format: {fmt} | Quality: {quality} | Page: {page}")
    log("Mode: GENERATE thumbnails")

    pairs = _plan(log, files, output_dir, fmt)
    cfg = ThumbSettings(size, quality, fmt, page, dry_run, srgb)

    workers = min(opt["workers"], MAX_PARALLEL)
    if opt["workers"] > MAX_PARALLEL:
        log(f"Workers: {opt['workers']} asked, {MAX_PARALLEL} used "
            f"(generate_thumbnails makes at most {MAX_PARALLEL} thumbnails at once)")
    for pair, result in tw.run_tasks(pairs, lambda p: make_thumbnail(p[0], p[1], cfg), workers):
        line = result if isinstance(result, str) else f"ERROR | {os.path.basename(pair[0])} | {result}"
        c.counter += 1
        kind = tw.result_kind(line)
        if kind is tw.Kind.OK:
            c.ok += 1
        elif kind is tw.Kind.SKIP:
            c.skip += 1
        elif kind is tw.Kind.ERROR:
            c.err += 1
        log(f"[{c.counter}/{c.total}] {line}", kind.level)

    log("")
    log(f"Done: {c.ok} OK | {c.skip} skipped | {c.err} errors | {c.counter}/{c.total} processed")
    log(f"Log: {log.path}")
    return 1 if c.err > 0 else 0


def _search_roots(opt, input_roots, recurse):
    """The folders --remove looks for thumbnails in, each once."""
    output_dir = opt["output_dir"]
    if output_dir:
        if os.path.isabs(output_dir):
            search_roots = [output_dir]
        else:
            # Generation resolves a relative --output-dir per FILE directory, so under
            # --recursive the thumbnails sit in <any subfolder>\<output_dir>, not only
            # <root>\<output_dir>. A literal tail match on a segment boundary.
            suffix = output_dir.rstrip("\\/").replace("/", "\\")
            tail = ("\\" + suffix).lower()
            search_roots = []
            for r in input_roots:
                search_roots.append(tw.join_path(r, suffix))
                if recurse and os.path.isdir(r):
                    for d in tw.list_dirs(r, True):
                        if d.replace("/", "\\").rstrip("\\").lower().endswith(tail):
                            search_roots.append(d)
    else:
        search_roots = list(input_roots)

    seen_roots = set()
    unique_roots = []
    for r in search_roots:
        if r.lower() not in seen_roots:
            seen_roots.add(r.lower())
            unique_roots.append(r)
    return unique_roots


def _own_thumbnail(path, size):
    """(True, "") when --remove may delete `path`, else (False, why it is kept).

    A .tif named like a thumbnail can be a real photo (a 1200x800 16-bit
    "hitchhiker_thumb.tif" was deleted: audit round 28). This script never writes a
    thumbnail larger than --size, so a bigger or unreadable TIFF is kept and reported."""
    if not tw.TIFF_EXT_RE.match(os.path.splitext(path)[1]):
        return True, ""
    r = tw.magick_probe(["identify", "-ping", "-format", "%w %h\n",
                         f"{tw.magick_path_spec(path)}[0]"], 120)
    w = h = 0
    parts = (r.lines[0] if r.lines else "").split()
    read_ok = (not r.timed_out and r.exit_code == 0 and len(parts) >= 2
               and parts[0].isdigit() and parts[1].isdigit())
    if read_ok:
        w, h = int(parts[0]), int(parts[1])
    if not read_ok or max(w, h) > size:
        return False, f"{w}x{h} is larger than --size {size}" if read_ok else "size unreadable"
    return True, ""


def remove_thumbnails(opt, log, c, input_roots, recurse, size):
    """-Remove: delete the thumbnails themselves (found by name), not the ones derived from
    the TIFFs present: a thumbnail whose TIFF was deleted or moved could never be cleaned
    up otherwise."""
    unique_roots = _search_roots(opt, input_roots, recurse)

    # Search roots can nest (root\thumbs and root\sub\thumbs), so the same file can be
    # listed twice -- deleting it twice would count a phantom error.
    thumbs = {}
    for root in unique_roots:
        if not os.path.exists(root):
            continue
        if os.path.isfile(root):
            listing = [tw.FoundFile(root, os.path.basename(root), os.path.dirname(root), 0, 0)]
        else:
            listing = tw.list_files(root, recurse)
        for f in listing:
            if THUMB_NAME_RE.search(f.stem) and f.ext.lstrip(".").lower() in FORMATS:
                thumbs.setdefault(f.path.lower(), f)
    thumbs = [thumbs[k] for k in sorted(thumbs)]

    c.total = len(thumbs)
    log(f"Found: {c.total} thumbnail(s) to remove")
    if c.total == 0:
        log(f"No thumbnails found in: {'; '.join(unique_roots)}", "WARN")
        log(f"Log: {log.path}")
        return 1 if c.err > 0 else 0

    for t in thumbs:
        c.counter += 1
        tag = f"[{c.counter}/{c.total}]"
        if opt["dry_run"]:
            c.skip += 1
            log(f"{tag} DRY-RUN (would remove) | {t.name}")
            continue
        own, why = _own_thumbnail(t.path, size)
        if not own:
            c.skip += 1
            log(f"{tag} SKIP (not a thumbnail this script wrote: {why} - kept) | {t.name}", "WARN")
            continue
        try:
            tw.remove_file(t.path)     # a read-only thumbnail is removed too
            c.ok += 1
            log(f"{tag} REMOVED | {t.name}")
        except OSError as e:
            c.err += 1
            log(f"{tag} ERROR (remove failed) | {t.name} | {e}", "ERROR")

    log("")
    log(f"Done: {c.ok} removed | {c.skip} skipped | {c.err} errors | {c.counter}/{c.total} processed")
    log(f"Log: {log.path}")
    return 1 if c.err > 0 else 0


def main(argv=None):
    tw.setup_console()
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        tw.say("Interrupted.")
        return 130
    except Exception as e:    # an unhandled error is a failed run, never a clean exit
        tw.say(f"FATAL (unhandled error) | {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
