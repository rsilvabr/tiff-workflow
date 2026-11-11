#!/usr/bin/env python3
"""generate_thumbnails.py -- sRGB thumbnails next to TIFFs (or in -OutputDir), and their
removal.

    python generate_thumbnails.py -InputDir <folder> [-Recursive] [-Size 256] [-Format jpg]
    python generate_thumbnails.py -InputDir <folder> -Remove [-DryRun]

The command line is the one generate_thumbnails.ps1 had (tiff-workflow up to v2.9.1):
`-Name value`, `-Switch`, names without case. Exit code 1 when any file failed or an
input folder does not exist, else 0. docs/README_generate_thumbnails.md has the reference.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tiff_workflow_common as tw  # noqa: E402
from tiff_workflow_common import P  # noqa: E402

# -- CLI PARAMETERS -----------------------------------------------
# The defaults are the settings of this script: edit them here, or leave the file alone
# and put them in .tiff_workflow_settings.json (the names in OVERLAY_SPECS).
PARAMS = (
    P("InputDir", "str", "."),
    P("Size", "int", 256),
    P("OutputDir", "str", ""),
    P("Remove", "switch"),
    P("DryRun", "switch"),
    P("Recursive", "switch"),
    P("Workers", "int", 4, lo=1, hi=64),
    P("Page", "str", "0", pattern=r"^(all|\d+)$"),
    P("Quality", "str", "85", pattern=r"^(100|[1-9][0-9]?)$"),
    P("Format", "str", "jpg"),
    P("SrgbProfile", "str", "", help='ICC profile used to convert wide-gamut sources; "" = auto-detect'),
    P("ExcludeFolders", "str", "", help="';'-separated folder NAMES to skip during discovery"),
)

# Settings overlay (.tiff_workflow_settings.json): the ONLY names it can change in this script,
# each with the type/range it must have. Never a safety or per-run setting (InputDir,
# OutputDir, Workers, DryRun, Remove, ...): tests/test_settings_overlay.py pins that.
# OVERLAY-SPECS-BEGIN
OVERLAY_SPECS = {
    "Size": "int:1:",
    "Quality": "intstr:1:100",
    "Format": "name",
    "SrgbProfile": "path?",
}
# OVERLAY-SPECS-END

SCRIPT_NAME = "generate_thumbnails"
FORMATS = ("jpg", "jpeg", "png", "tif", "tiff")

# Every thumbnail this script can produce, in one pattern:
#   name_thumb        plain
#   name_thumb-0      one frame per page (-Page all)
#   name_thumb_v2     collision rename under -OutputDir
#   name_thumb_v2-0   both
# Matching only '_thumb(-\d+)?$' left the _v2 variants out of BOTH the self-exclusion and
# the -Remove filter, so a renamed .tif thumbnail was rescanned as a source on the next run
# (producing _thumb_v2_thumb) and could never be cleaned up.
THUMB_NAME_RE = re.compile(r"_thumb(_v\d+)?(-\d+)?$", re.I)

# Folder names skipped during discovery, matched RELATIVE to the root each file was found
# under. `ZIP` and `_EXPORT` are deliberately NOT here: `_EXPORT` is what compress modes 6/7
# exist to process, and `ZIP/` is where modes 1/3/5/6/7 PUT the compressed library.
TOOL_OUTPUT_NAMES = ("OLD_TIFFs", "OLD_TIFF", "converted_zip")


class Counters:
    def __init__(self):
        self.ok = self.skip = self.err = self.counter = self.total = 0


def frame_files(dest_path):
    """The `name-0.jpg, name-1.jpg, ...` files magick writes for -Page all with a
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
    looked for with -Page all: counted always, a `-Page all` run left `-Page 0` unable to
    ever write name.jpg."""
    if os.path.exists(dest_path):
        return True
    if not frame_names:
        return False
    return len(frame_files(dest_path)) > 0


def make_thumbnail(task):
    """One thumbnail; returns the result line."""
    src = task["src"]
    dest = task["dest"]
    name = os.path.basename(src)
    if thumb_exists(dest, task["page"] == "all") and not task["dry_run"]:
        return f"SKIP (exists) | {name}"
    if task["dry_run"]:
        return f"DRY-RUN | {name} -> {os.path.basename(dest)}"
    try:
        dest_dir = os.path.dirname(dest)
        os.makedirs(dest_dir, exist_ok=True)
        # A '[n]' suffix turns the input into a filename SPEC, where '%' is expanded; a
        # plain input (-Page all) is opened literally and must stay unescaped.
        if task["page"] == "all":
            source = src
        else:
            source = f"{tw.magick_path_spec(src)}[{task['page']}]"
        args = [source] + tw.thumbnail_magick_args(task["srgb_profile"], task["size"])
        # -quality only where it means JPEG quality. ImageMagick reinterprets it for PNG as
        # zlib level + filter (85 -> level 8, filter 5); TIFF ignores it.
        fmt = task["format"].lower()
        if fmt in ("jpg", "jpeg"):
            args += ["-quality", task["quality"], "-interlace", "Plane"]
        elif fmt in ("tif", "tiff"):
            args += ["-compress", "zip"]
        # The output path is always escaped: a '%' in the name would send the thumbnail
        # somewhere else while magick still exits 0.
        args.append(tw.magick_path_spec(dest))
        rc, out = tw.magick_run(args)
        if rc != 0:
            first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
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


def run(argv):
    try:
        opt, bound = tw.parse_args(argv, PARAMS)
    except tw.UsageError as e:
        tw.say(f"ERROR: {e}")
        return 1

    # The overlay is applied before anything reads a parameter. A -Param given on the command
    # line always beats it; an invalid entry never stops the run.
    overlay = tw.apply_settings_overlay(SCRIPT_NAME, OVERLAY_SPECS, opt, bound,
                                        os.path.dirname(os.path.abspath(__file__)))
    if overlay["info"]:
        tw.say(overlay["info"])
    for w in overlay["warnings"]:
        tw.say(f"WARNING: settings overlay: {w}")

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

    size = opt["Size"]
    fmt = opt["Format"]
    page = opt["Page"]
    quality = opt["Quality"]
    output_dir = opt["OutputDir"]
    dry_run = opt["DryRun"]
    recurse = opt["Recursive"]

    if size < 32 or size > 4096:
        log(f"Invalid size: {size}. Must be between 32 and 4096.", "ERROR")
        return 1
    if fmt.lower() not in FORMATS:
        log(f"Invalid format: {fmt}. Must be jpg, png, tif, or tiff.", "ERROR")
        return 1

    srgb = tw.resolve_srgb_profile(opt["SrgbProfile"])
    # An overlay path that does not exist must not stop the run: warn and auto-detect. A
    # -SrgbProfile typed on the command line still fails.
    if opt["SrgbProfile"] and not srgb and "SrgbProfile" in overlay["applied"]:
        log(f"WARN: settings overlay: SrgbProfile not found: {opt['SrgbProfile']}; "
            f"using the auto-detected profile", "WARN")
        opt["SrgbProfile"] = ""
        srgb = tw.resolve_srgb_profile("")
    if opt["SrgbProfile"] and not srgb:
        log(f"ERROR: -SrgbProfile not found: {opt['SrgbProfile']}", "ERROR")
        return 1
    if not srgb:
        log("WARN: no sRGB ICC profile found -- wide-gamut sources (ProPhoto, AdobeRGB) will "
            "produce thumbnails with shifted colours. Pass -SrgbProfile <path.icc> to fix.", "WARN")

    # A ';' separated list like the other backends; a bad path is an ERROR, not a WARN: a
    # typo used to look identical to "folder has no TIFFs" and exited 0.
    input_roots = []
    missing_roots = []
    for entry in opt["InputDir"].split(";"):
        entry = entry.strip()
        if not entry:
            continue
        entry = tw.full_path(entry)
        if not os.path.exists(entry):
            missing_roots.append(entry)
            continue
        input_roots.append(entry)

    try:
        tool_output_names = list(TOOL_OUTPUT_NAMES) + tw.parse_exclude_folders(opt["ExcludeFolders"])
    except tw.UsageError as e:
        tw.say(f"ERROR: {e}")
        return 1

    # Each file carries the root it was discovered under, so the exclusion is evaluated
    # relative to that root. A file passed directly as -InputDir is its own root: an
    # explicit path always wins.
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
                # Dedupe: a repeated -InputDir entry (A;A) or a root nested under a
                # -Recursive root lists the same source twice.
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

    c = Counters()
    c.total = len(files)
    for m in missing_roots:
        log(f"ERROR: input directory not found: {m}", "ERROR")
    c.err += len(missing_roots)

    if not input_roots:
        log("No valid input directories specified.", "ERROR")
        log(f"Log: {log.path}")
        return 1

    if opt["Remove"]:
        return remove_thumbnails(opt, log, c, input_roots, recurse, size)

    if c.total == 0:
        log(f"No TIFF files found in: {'; '.join(input_roots)}", "WARN")
        log(f"Log: {log.path}")
        return 1 if c.err > 0 else 0

    log(f"Found: {c.total} TIFF(s)")
    log(f"Size: {size}px | Format: {fmt} | Quality: {quality} | Page: {page}")
    log("Mode: GENERATE thumbnails")

    # claimed: with -OutputDir + -Recursive, a\photo.tif and b\photo.tif both map to
    # photo_thumb.jpg. The second one used to report "SKIP (exists)" and never be generated,
    # so later claimants are numbered instead.
    claimed = {}
    tasks = []
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
        tasks.append({"src": f.path, "dest": dest, "size": size, "quality": quality,
                      "format": fmt, "page": page, "dry_run": dry_run, "srgb_profile": srgb})

    workers = min(opt["Workers"], 16)
    for _task, result in tw.run_tasks(tasks, make_thumbnail, workers):
        line = result if isinstance(result, str) else f"ERROR | {os.path.basename(_task['src'])} | {result}"
        c.counter += 1
        level = "INFO"
        if line.startswith("OK"):
            c.ok += 1
        elif line.startswith("SKIP"):
            c.skip += 1
        elif line.startswith("ERROR"):
            c.err += 1
            level = "ERROR"
        log(f"[{c.counter}/{c.total}] {line}", level)

    log("")
    log(f"Done: {c.ok} OK | {c.skip} skipped | {c.err} errors | {c.counter}/{c.total} processed")
    log(f"Log: {log.path}")
    return 1 if c.err > 0 else 0


def remove_thumbnails(opt, log, c, input_roots, recurse, size):
    """-Remove: delete the thumbnails themselves (found by name), not the ones derived from
    the TIFFs present: a thumbnail whose TIFF was deleted or moved could never be cleaned
    up otherwise."""
    output_dir = opt["OutputDir"]
    if output_dir:
        if os.path.isabs(output_dir):
            search_roots = [output_dir]
        else:
            # Generation resolves a relative -OutputDir per FILE directory, so under
            # -Recursive the thumbnails sit in <any subfolder>\<OutputDir>, not only
            # <root>\<OutputDir>. A literal tail match on a segment boundary.
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
        if opt["DryRun"]:
            c.skip += 1
            log(f"{tag} DRY-RUN (would remove) | {t.name}")
            continue
        # A .tif named like a thumbnail can be a real photo (a 1200x800 16-bit
        # "hitchhiker_thumb.tif" was deleted: audit round 28). This script never writes a
        # thumbnail larger than -Size, so a bigger or unreadable TIFF is kept and reported.
        if tw.TIFF_EXT_RE.match(t.ext):
            r = tw.magick_probe(["identify", "-ping", "-format", "%w %h\n",
                                 f"{tw.magick_path_spec(t.path)}[0]"], 120)
            w = h = 0
            parts = (r.lines[0] if r.lines else "").split()
            read_ok = (not r.timed_out and r.exit_code == 0 and len(parts) >= 2
                       and parts[0].isdigit() and parts[1].isdigit())
            if read_ok:
                w, h = int(parts[0]), int(parts[1])
            if not read_ok or max(w, h) > size:
                c.skip += 1
                why = f"{w}x{h} is larger than -Size {size}" if read_ok else "size unreadable"
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
