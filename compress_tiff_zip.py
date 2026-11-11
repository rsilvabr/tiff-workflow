#!/usr/bin/env python3
"""compress_tiff_zip.py -- TIFF -> ZIP/Deflate, lossless and verified.

    python compress_tiff_zip.py -Mode 9 -InputDir <folder> [-DryRun]
    python compress_tiff_zip.py                (legacy: the current folder, in place)

Modes: 0 in place (one folder, originals kept in OLD_TIFFs/), 1 ZIP/ subfolder, 2 flat
output, 3 ZIP/ subfolder (recursive), 4 sibling folder (_TIFF -> _ZIP), 5 ZIP/ next to the
parent, 6/7 inside an _EXPORT folder, 8 replace in place (no backup, pixel-verified),
9 in place recursive (originals kept in OLD_TIFFs/).

The command line is the one compress_tiff_zip.ps1 had (tiff-workflow up to v2.9.1):
`-Name value`, `-Switch`, `-SafeMode:$false`, names without case. Exit code 1 when any file
failed or an input folder does not exist, else 0. docs/README_compress_tiff_zip.md has the
reference.

The irreversible step comes last: an original leaves its place only after its replacement
is written, checked and sitting next to it on the same volume, and the swap is one rename.
"""

import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tiff_workflow_common as tw  # noqa: E402
from tiff_workflow_common import P  # noqa: E402

# -- CLI PARAMETERS -----------------------------------------------
# The defaults are the settings of this script: edit them here, or leave the file alone
# and put them in .tiff_workflow_settings.json (the names in OVERLAY_SPECS).
PARAMS = (
    P("Mode", "int", -1, help="-1 = legacy mode (no CLI params = old behavior)"),

    P("InputDir", "str", "."),

    # Output path control
    P("OutputDir", "str", "", help='"" = in-place (modes 0/8), or flat root (mode 2)'),
    P("ZipSuffix", "str", "_ZIP", help="Mode 4: folder rename suffix"),
    P("ZipSubfolderName", "str", "ZIP", help="Mode 1/3: subfolder name"),
    P("ExportMarker", "str", "_EXPORT", help="Modes 6/7"),
    P("ExportZipSubfolder", "str", "ZIP", help="Mode 6: subfolder inside _EXPORT"),
    P("ExportTiffSubfolder", "str", "TIFF", help="Mode 7: TIFF subfolder inside _EXPORT"),

    # Behavior
    P("Workers", "int", 8, lo=1, hi=64),
    P("CapWorkers", "int", 0, lo=0, hi=64, help="0 = no cap; >0 = clamp parallel workers to this value"),
    P("DryRun", "switch"),
    P("SafeMode", "bool", True, help="true = skip multi-page TIFFs"),
    P("SkipLzwAsCompressed", "bool", False, help="true = treat LZW as already compressed"),
    P("Overwrite", "switch"),
    P("DeleteSource", "switch"),
    P("ForceParallel", "switch", help="accepted for old command lines; parallel is the default"),
    P("ForceSequential", "switch", help="one file at a time"),
    P("StagingDir", "str", ""),
    P("DuplicateAction", "str", "Numbered", choices=("Skip", "Numbered", "Overwrite")),
    P("MagickTimeout", "int", 30),
    P("ExcludeFolders", "str", "", help="';'-separated folder NAMES to skip during discovery (any mode)"),

    # Thumbnail generation
    P("GenerateThumbnail", "switch", help="Generate embedded thumbnail in TIFF"),
    P("ThumbSize", "int", 256, help="Thumbnail size in pixels"),
    P("ThumbQuality", "str", "85", help="JPEG quality for thumbnail"),
    P("ThumbFormat", "str", "jpg", help="Thumbnail format (jpg, png, etc.)"),
    P("ThumbPage", "int", 1, help="Output page position for the embedded thumbnail; must be >= 1 (after the main image)"),
    P("SkipCompressedWithThumb", "switch", help="Skip TIFFs that are already compressed AND have thumbnail"),
    P("SrgbProfile", "str", "", help='ICC profile used to convert wide-gamut sources; "" = auto-detect'),
    P("WorkerMemoryFraction", "float", 0.8, help="Cap parallel workers so their ImageMagick processes fit in this share of free memory; 0 = no cap"),
    P("InputDirFile", "str", "", help="UTF-8 text file, one input folder per line (the wizard uses it when -InputDir would exceed the command-line limit)"),
    P("RestoreStranded", "switch", help="Move originals stranded in OLD_TIFFs/ (nothing left in their place) back where they came from"),
)

# Settings overlay (.tiff_workflow_settings.json): the ONLY names it can change in this script,
# each with the type/range it must have. Never a safety or per-run setting (Mode, InputDir,
# OutputDir, StagingDir, Workers, DryRun, SafeMode, SkipLzwAsCompressed, Overwrite,
# DeleteSource, ...): tests/test_settings_overlay.py pins that.
# OVERLAY-SPECS-BEGIN
OVERLAY_SPECS = {
    "ZipSuffix": "name",
    "ZipSubfolderName": "name",
    "ExportMarker": "name",
    "ExportZipSubfolder": "name",
    "ExportTiffSubfolder": "name",
    "CapWorkers": "int:0:64",
    "MagickTimeout": "int:1:",
    "ThumbSize": "int:1:",
    "ThumbQuality": "intstr:1:100",
    "ThumbFormat": "name",
    "SrgbProfile": "path?",
    "WorkerMemoryFraction": "float:0:2",
}
# OVERLAY-SPECS-END

SCRIPT_NAME = "compress_tiff_zip"

# OLD_TIFFs holds the PRISTINE originals that modes 0/9 archived. Excluded from discovery in
# EVERY mode, matched RELATIVE to the scan root: pointing the tool AT an OLD_TIFFs folder on
# purpose still works.
TOOL_OUTPUT_NAMES = ("OLD_TIFFs", "OLD_TIFF")

# Mode 8's own staging folders: `_cs8_<8 hex>` is the short form; the long
# `compress_staging*_<32 hex>` form is still matched so a folder left behind by an old
# version is not picked up as input.
_STAGING_DIR_RE = re.compile(
    r"[\\/](compress_staging(_mode8)?_[0-9a-f]{32}|_cs8_[0-9a-f]{8})([\\/]|$)", re.I)

# The overlay names that decide where an output lands (see the "would write over its own
# source" refusal).
_OVERLAY_PATH_NAMES = ("ZipSuffix", "ZipSubfolderName", "ExportMarker", "ExportZipSubfolder",
                       "ExportTiffSubfolder")

MAX_PATH_CHARS = 259     # Windows refuses longer paths and magick.exe is not long-path aware
TEMP_SIBLING_CHARS = 26  # ".<8 hex>_<12 hex>.tif"


class Run:
    """The state of one run: options, log, counters, and what an interrupt has to undo."""

    def __init__(self, opt, log, overlay_applied):
        self.opt = opt
        self.log = log
        self.overlay_applied = overlay_applied
        self.run_id = tw.new_run_id()
        self.srgb_profile = None
        self.exclude_names = []
        self.parallel = not opt["ForceSequential"]
        self.counter = self.total = 0
        self.ok = self.skip = self.multi = self.warn = self.err = self.dry = 0
        self.stranded = self.restored = 0
        self.multi_paths = []
        # Folders an interrupt removes THIS run's staged files from (names with its id).
        self.cleanup_dirs = []
        # Temp siblings this run writes next to the photos (<name>.<run id>_<12 hex>.tif).
        self.temp_files = set()
        # Originals moved to OLD_TIFFs/ whose compressed copy has not landed yet
        # ({"backup", "original", "final_dst"}): an interrupt puts them back. Checked
        # against the DISK at that time, so a stale entry is a no-op, never a wrong restore.
        self.moved_originals = []
        self.created_default_staging = False

    def count(self, line):
        """Log one result line with its [n/total] prefix and count it."""
        self.counter += 1
        level = "INFO"
        if line.startswith("OK"):
            self.ok += 1
        elif line.startswith("SKIP"):
            self.skip += 1
        elif line.startswith("MULTI"):
            self.multi += 1
            level = "WARN"
        elif line.startswith("ERROR"):
            self.err += 1
            level = "ERROR"
        elif line.startswith("WARN"):
            self.warn += 1
            level = "WARN"
        elif line.startswith("DRY"):
            # counted on its own -- nothing was written
            self.dry += 1
        self.log(f"[{self.counter}/{self.total}] {line}", level)

    def summary(self):
        """The end-of-run tally plus the multi-page list. Also printed by the early
        "nothing to do" exits: a folder where every file was correctly skipped must not end
        on a bare WARN that reads like a failure."""
        log = self.log
        log("")
        log("-" * 50)
        dry = f" | {self.dry} dry-run (nothing written)" if self.dry > 0 else ""
        log(f"Done: {self.ok} OK | {self.skip} skipped | {self.multi} multi-page (not touched) | "
            f"{self.warn} warnings | {self.err} errors | {self.counter}/{self.total} processed{dry}")
        if self.stranded > self.restored:
            log(f"STRANDED: {self.stranded - self.restored} original(s) still in OLD_TIFFs with "
                f"nothing in their place -- see the STRANDED lines above", "WARN")
        self.multi_list()

    def multi_list(self):
        if self.multi > 0:
            self.log("")
            self.log("-- Multi-page TIFFs found (not compressed - review manually):")
            for p in sorted(self.multi_paths, key=str.lower):
                self.log(f"   {p}", "WARN")


# -- Stranded originals ---------------------------------------------

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
    past 260 characters "no original here" was once said about a file that was there."""
    found = []
    full = tw.full_path(root)
    backup_lower = {n.lower() for n in backup_names}
    backup_dirs = []
    stack = [full]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(tw.long_path(cur)) as it:
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
            if tw._entry_flags(e)[1]:
                continue
            stack.append(plain)
    backup_dirs.sort(key=str.lower)

    def is_file(directory, name):
        return os.path.isfile(tw.long_path(os.path.join(directory, name)))

    for d in backup_dirs:
        parent = os.path.dirname(d)
        if exclude_names and tw.path_excluded(parent, root, exclude_names):
            continue
        try:
            with os.scandir(tw.long_path(d)) as it:
                names = sorted((e.name for e in it if e.is_file()), key=str.lower)
        except OSError:
            continue
        for b_name in names:
            stem, ext = os.path.splitext(b_name)
            if not tw.TIFF_EXT_RE.match(ext):
                continue
            stems = [stem]
            m = re.fullmatch(r"(.+)_\d{8}_\d{6}(_\d+)?", stem)
            if m and any(is_file(d, m.group(1) + e) for e in (".tif", ".tiff")):
                stems.append(m.group(1))
            present = any(is_file(parent, s + e) for s in stems for e in (".tif", ".tiff"))
            if not present:
                found.append({"backup": os.path.join(d, b_name),
                              "target": os.path.join(parent, stems[-1] + ext)})
    return found


def report_stranded(run, input_roots):
    """Report (and with -RestoreStranded move back) the stranded originals of every root.
    On EVERY run, whatever the mode: discovery skips OLD_TIFFs/ and nothing else would ever
    notice them. Runs BEFORE discovery, so a restored original is compressed by this run."""
    opt = run.opt
    log = run.log
    seen = set()
    for root in input_roots:
        stranded = find_stranded_originals(root, opt["Mode"] != 0, TOOL_OUTPUT_NAMES,
                                           run.exclude_names)
        # One target claimed by two backups (an original and its timestamped namesake):
        # which one is the original cannot be told from the names, so neither is moved.
        by_target = {}
        for s in stranded:
            by_target.setdefault(s["target"].lower(), []).append(s)
        for s in stranded:
            if s["backup"].lower() in seen:
                continue
            seen.add(s["backup"].lower())
            run.stranded += 1
            run.warn += 1
            log(f"STRANDED (original in OLD_TIFFs, nothing in its place) | {s['backup']}", "WARN")
            if not opt["RestoreStranded"]:
                continue
            if len(by_target[s["target"].lower()]) > 1:
                log(f"  not restored: more than one backup claims {s['target']} -- move the "
                    f"right one by hand", "WARN")
                continue
            if opt["DryRun"]:
                log(f"  DRY: would restore to {s['target']}")
                continue
            if os.path.isfile(tw.long_path(s["target"])):
                log(f"  not restored: {s['target']} appeared meanwhile", "WARN")
                continue
            try:
                # os.rename never overwrites on Windows: a file that appeared after the
                # check above stays.
                if os.path.exists(tw.long_path(s["target"])):
                    raise FileExistsError(s["target"])
                os.rename(tw.long_path(s["backup"]), tw.long_path(s["target"]))
                run.restored += 1
                log(f"  RESTORED (stranded original) | {s['target']}", "WARN")
            except OSError as e:
                run.err += 1
                log(f"  RESTORE FAILED | {s['backup']}: {e}", "ERROR")
    if run.stranded > 0:
        if opt["RestoreStranded"] and not opt["DryRun"]:
            log(f"STRANDED: {run.stranded} original(s) found in OLD_TIFFs with nothing in their "
                f"place, {run.restored} restored", "WARN")
        else:
            log(f"STRANDED: {run.stranded} original(s) in OLD_TIFFs with nothing in their place "
                f"(an interrupted run?). Run again with -RestoreStranded to move them back; "
                f"never delete or move OLD_TIFFs away until then.", "WARN")


def complete_keep_original(run, written, src, final_dst, keep_dir, src_length, src_mtime_ns):
    """Modes 0/9: land a verified compressed copy and only THEN archive the original in
    OLD_TIFFs/. Returns None on success, else the error text (the original is left, or put
    back, in its place).

      1. the output is brought next to its destination as a temp sibling (a copy from
         another volume is size-checked first);
      2. the original must be unchanged since planning (size + write time);
      3. the original moves to OLD_TIFFs/ (a unique name);
      4. the temp sibling is renamed onto final_dst -- for a .tif source the place the
         original just left, so only the two renames on one volume ever leave it empty.
    A failure in 4 puts the original back. An interrupt between 3 and 4 is undone from
    run.moved_originals; a kill in that instant is what find_stranded_originals reports."""
    if not os.path.exists(written):
        return f"compressed file missing: {written}"
    tmp = tw.new_temp_sibling_path(final_dst, run.run_id)
    run.temp_files.add(tmp)
    err = tw.move_file_into_place(written, tmp, run.run_id)
    if err:
        return err
    try:
        st = os.stat(src)
    except OSError:
        st = None
    if st is None or st.st_size != src_length or st.st_mtime_ns != src_mtime_ns:
        tw.remove_quietly(tmp)
        return "the original changed or vanished while it was being compressed - left as it is"
    os.makedirs(keep_dir, exist_ok=True)
    src_name = os.path.basename(src)
    backup = os.path.join(keep_dir, src_name)
    if os.path.exists(backup):
        stem, ext = os.path.splitext(src_name)
        base = os.path.join(keep_dir, f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        backup = base + ext
        n = 1
        while os.path.exists(backup):
            backup = f"{base}_{n}{ext}"
            n += 1
    track = {"backup": backup, "original": src, "final_dst": final_dst}
    run.moved_originals.append(track)
    try:
        os.rename(src, backup)      # never overwrites
    except OSError as e:
        run.moved_originals.remove(track)
        tw.remove_quietly(tmp)
        return f"could not move the original to OLD_TIFFs: {e}"
    try:
        tw.move_file_replacing(tmp, final_dst)
    except OSError as e:
        try:
            os.rename(backup, src)
            run.moved_originals.remove(track)
            tw.remove_quietly(tmp)
            return f"could not put the compressed file in place ({e}) - original restored"
        except OSError:
            return (f"could not put the compressed file in place ({e}) - "
                    f"ORIGINAL LEFT IN OLD_TIFFs: {backup}")
    run.moved_originals.remove(track)
    return None


# -- Output path per mode -------------------------------------------

def _rel_parts(directory, root_p):
    """The folder segments of `directory` below `root_p`; None when it is not under it."""
    dir_n = directory.replace("/", "\\").rstrip("\\")
    if dir_n.lower() == root_p.lower():
        return []
    if dir_n.lower().startswith(root_p.lower() + "\\"):
        return dir_n[len(root_p) + 1:].split("\\")
    return None


def resolve_output(f, mode, input_root, opt):
    """Where the compressed copy of `f` goes in `mode`; None when the mode has no place
    for it (reported, never dropped silently). The output is always <stem>.tif."""
    parent = f.dir
    out_name = f.stem + ".tif"
    # One separator: '/' is legal on Windows, and -InputDir given that way used to defeat
    # the mode 5 prefix compare and collapse the whole tree into <root>\ZIP.
    root_p = input_root.replace("/", "\\").rstrip("\\")

    if mode in (0, 8, 9):
        return tw.join_path(parent, out_name)
    if mode in (1, 3):
        return tw.join_path(tw.join_path(parent, opt["ZipSubfolderName"]), out_name)
    if mode == 2:
        return tw.join_path(opt["OutputDir"] or root_p, out_name)
    if mode == 4:
        folder = os.path.basename(parent.rstrip("\\/"))
        grandparent = tw.parent_dir(parent)
        if not grandparent or not folder:
            return None
        suffix = opt["ZipSuffix"]
        if re.search(r"_(TIFF)$", folder, re.I):
            new = re.sub(r"_(TIFF)$", lambda m: suffix, folder, flags=re.I)
        elif folder.lower() == "tiff":
            # A folder named exactly "TIFF" has no stem: the suffix rule made it "_ZIP".
            new = opt["ZipSubfolderName"]
        elif re.search(r"^(.+)(TIFF)$", folder, re.I):
            new = re.sub(r"(TIFF)$", lambda m: suffix, folder, flags=re.I)
        else:
            new = folder + suffix
        return tw.join_path(tw.join_path(grandparent, new), out_name)
    if mode == 5:
        grandparent = tw.parent_dir(parent)
        # Never climb above the input root: a TIFF sitting directly in the root would land
        # in the drive root, outside the chosen tree. The prefix compare needs a separator
        # boundary: "C:\data" also prefixes "C:\database".
        if not grandparent:
            grandparent = root_p
        else:
            gp_l = grandparent.replace("/", "\\").rstrip("\\").lower()
            root_l = root_p.lower()
            if not (gp_l == root_l or gp_l.startswith(root_l + "\\")):
                grandparent = root_p
        return tw.join_path(tw.join_path(grandparent, opt["ZipSubfolderName"]), out_name)
    if mode in (6, 7):
        # The marker only counts AT/BELOW the input root: an _EXPORT folder ABOVE the
        # chosen -InputDir once rebuilt the output outside the selected tree.
        parts = _rel_parts(parent, root_p)
        if parts is None:
            return None
        marker = opt["ExportMarker"].lower()
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
            if mode == 7 and export_idx >= 0 and seg.lower() == opt["ExportTiffSubfolder"].lower():
                tail_from = i + 1
                break
        if export_idx < 0 or tail_from < 0:
            return None
        # Inside the _EXPORT folder the file actually came from (<root>\_EXPORT would
        # merge every job into one folder).
        export_root = tw.join_path(root_p, "\\".join(parts[:export_idx + 1]))
        new_parent = tw.join_path(export_root, opt["ExportZipSubfolder"])
        rel = [p for p in parts[tail_from:] if p]
        if rel:
            new_parent = tw.join_path(new_parent, "\\".join(rel))
        return tw.join_path(new_parent, out_name)
    return None


def discover(run, mode, root, unreadable):
    """The TIFFs of `root` that `mode` works on."""
    opt = run.opt
    files = [f for f in tw.list_files(root, mode not in (0, 1), unreadable)
             if tw.is_tiff_name(f.name)]
    if mode == 6:
        # the marker RELATIVE to the scan root, same contract as the folder exclusion
        files = [f for f in files if tw.path_excluded(f.dir, root, [opt["ExportMarker"]])]
    elif mode == 7:
        root_p = root.replace("/", "\\").rstrip("\\")
        marker = opt["ExportMarker"].lower()
        sub = opt["ExportTiffSubfolder"].lower()

        def below_marker(f):
            parts = _rel_parts(f.dir, root_p)
            if not parts:
                return False
            return any(parts[i].lower() == marker and parts[i + 1].lower() == sub
                       for i in range(len(parts) - 1))
        files = [f for f in files if below_marker(f)]
    elif mode == 8:
        files = [f for f in files if not _STAGING_DIR_RE.search(f.dir)]
    return files


# -- One TIFF -> ZIP job --------------------------------------------

def process_job(run, t, load):
    """Compress one TIFF. Runs on a worker thread: it never logs or counts. Returns
    {"result": line, "staging_name", "name", "src", "final_dst" (None on the results that
    carry no rename note), "multi", "can_delete", "integrity_failed", "integrity_reason"}.

    `load` = workers running at once (they share the disk; it scales the probe timeouts)."""
    opt = run.opt
    mode = opt["Mode"]
    src = t["src"]
    write_dst = t["write_dst"]
    final_dst = t["final_dst"]
    name = os.path.basename(src)
    timeout = opt["MagickTimeout"]
    generate_thumb = opt["GenerateThumbnail"]

    def res(line, fd=False, **extra):
        d = {"result": line, "staging_name": None, "name": name, "src": src,
             "final_dst": final_dst if fd else None, "multi": None, "can_delete": False,
             "integrity_failed": False, "integrity_reason": ""}
        d.update(extra)
        return d

    comp = t.get("comp") or ""
    if not comp:
        ok, comp = tw.ifd0_compression(src)
        if not ok:
            return res(f"ERROR (exiftool check) | {name} | cannot detect compression")
    # The SKIP decision runs for pre-probed files too.
    if tw.is_compressed(comp, opt["SkipLzwAsCompressed"]):
        if opt["SkipCompressedWithThumb"]:
            # Only skip when a thumbnail is already embedded; otherwise reprocess
            if tw.tiff_has_thumbnail_page(src, timeout):
                return res(f"SKIP (compressed+thumb) | {name}")
        else:
            # Say WHY nothing was produced when a thumbnail was asked for: this SKIP runs
            # before the thumbnail step.
            note = " [no thumbnail added: pass -SkipCompressedWithThumb]" if generate_thumb else ""
            return res(f"SKIP ({comp}) | {name}{note}")

    if os.path.exists(final_dst) and not opt["Overwrite"] and not tw.same_path(final_dst, src):
        return res(f"SKIP (exists) | {name}")

    if opt["SafeMode"] and not t.get("safe_checked"):
        pc = tw.tiff_page_count(src, timeout)
        if not pc.ok:
            return res(tw.page_count_error_line(pc, name))
        if pc.count > 1 and not tw.tiff_has_only_subfile_pages(
                src, pc.count, ("REDUCEDIMAGE", "REDUCED"), timeout):
            return res(f"MULTI ({pc.count} pages - skipped) | {name}", multi=src)
        # only thumbnails: go on

    # -GenerateThumbnail rebuilds the file from page 0 only, so every other source page is
    # dropped. Refuse when that would lose a real page. Before the dry-run return on
    # purpose: a dry run reports the refusal instead of promising an OK.
    thumb_note = ""
    if generate_thumb:
        ts = tw.thumbnail_safe_source(src, timeout)
        if not ts.ok:
            return res(f"ERROR (-GenerateThumbnail refused) | {name} | {ts.reason}", fd=True)
        if ts.reason == "thumb-replaced":
            thumb_note = " [thumb replaced]"

    if opt["DryRun"]:
        return res(f"DRY ({comp} -> ZIP){thumb_note} | {name}", fd=True)

    # magick turned a neutral RGB file into a 1-channel gray one and every pixel gate said
    # OK (audit round 28). The source layout picks the -type that keeps it and is the
    # reference for the gate after the write.
    layout_ok, src_pages = tw.tiff_sample_layout(src)
    if not layout_ok:
        return res(f"ERROR (sample layout unreadable) | {name}", fd=True)
    layout_pages = [src_pages[0]] if generate_thumb else list(src_pages)
    type_args = tw.magick_keep_type_args(layout_pages)
    if not tw.associated_alpha_rewritable(src, layout_pages, timeout):
        return res(f"ERROR (associated alpha that is not opaque: ImageMagick would change it - refused) | {name}", fd=True)

    # When the output overwrites the input the original is gone by the time the EXIF
    # restore runs: park the metadata in a temp MIE container first (also for a '%' name).
    needed, meta_backup = tw.backup_tiff_metadata(src, write_dst)
    if needed and not meta_backup:
        # Fail-closed: a write without the container would strip every tag
        return res(f"ERROR (metadata container failed - write refused) | {name}", fd=True)

    # An in-place write would destroy the only copy of the image if magick died mid-write:
    # compress to a temp sibling, then replace the original with one same-volume rename,
    # after the gates below. In every mode: mode 2 with -OutputDir on the input root and
    # mode 5 for a file already inside ZIP/ resolve final_dst == src too.
    in_place_final = None
    if tw.same_path(write_dst, src):
        in_place_final = write_dst
        write_dst = tw.new_temp_sibling_path(write_dst, run.run_id)
        run.temp_files.add(write_dst)

    no_thumb_note = ""
    thumb_marker_failed = False
    subfile_restore_failed = False
    history_failed = False

    if generate_thumb:
        # The main image is always page 0; the thumbnail is appended after it. '[0]' makes
        # the input a filename SPEC, so '%' is escaped there and in every output path.
        main_page = f"{tw.magick_path_spec(src)}[0]"
        thumb_ext = (opt["ThumbFormat"] or "jpg").lower()
        temp_tiff = tw.temp_file_path(".tif")
        thumb_temp = tw.temp_file_path("." + thumb_ext)
        try:
            rc, out = tw.magick_run(["-quiet", main_page, "-compress", "zip"] + type_args
                                    + [tw.magick_path_spec(temp_tiff)])
            if rc != 0:
                tw.remove_quietly(meta_backup)
                return res(f"ERROR (magick compress) | {name} | {tw.one_line(out)}")
            thumb_cmd = ["-quiet", main_page] + tw.thumbnail_magick_args(run.srgb_profile, opt["ThumbSize"])
            if thumb_ext == "jpg":
                thumb_cmd += ["-quality", str(opt["ThumbQuality"])]
            thumb_cmd.append(f"{thumb_ext}:{tw.magick_path_spec(thumb_temp)}")
            rc, _out = tw.magick_run(thumb_cmd)
            if rc != 0 or not os.path.exists(thumb_temp):
                # the thumbnail failed: the compressed main image alone
                shutil.copyfile(temp_tiff, write_dst)
                no_thumb_note = " [no thumb]"
            if not no_thumb_note:
                # The two INPUTS stay literal: a plain input is opened verbatim.
                rc, _out = tw.magick_run(["-quiet", temp_tiff, f"{thumb_ext}:{thumb_temp}",
                                          "-compress", "zip"] + type_args
                                         + [tw.magick_path_spec(write_dst)])
                if rc != 0:
                    shutil.copyfile(temp_tiff, write_dst)
                    no_thumb_note = " [no thumb]"
            # magick can exit 0 and still leave nothing at write_dst (a full disk).
            if not os.path.exists(write_dst):
                tw.remove_quietly(meta_backup)
                return res(f"ERROR (magick reported success but wrote no file) | {name} | expected: {write_dst}", fd=True)
            if not no_thumb_note:
                # Both markers: magick stamps PAGE on page 0 of any multi-page write, so
                # the main image has to get the source's marker back.
                if not tw.set_tiff_thumb_markers(src, write_dst):
                    thumb_marker_failed = True
        except OSError as e:
            tw.remove_quietly(meta_backup)
            tw.remove_quietly(write_dst)
            return res(f"ERROR (magick compress) | {name} | {e}")
        finally:
            tw.remove_quietly(temp_tiff)
            tw.remove_quietly(thumb_temp)
    else:
        # The input stays literal (magick opens a plain path verbatim); the OUTPUT is
        # escaped, or a '%' in the name silently redirects it.
        rc, out = tw.magick_run(["-quiet", src, "-compress", "zip"] + type_args
                                + [tw.magick_path_spec(write_dst)])
        if rc != 0:
            tw.remove_quietly(meta_backup)
            # The partial output is this run's own file (never the source: an in-place
            # write goes to a temp sibling): drop it. A leftover also vetoes a later
            # "exists" check.
            tw.remove_quietly(write_dst)
            return res(f"ERROR (magick) | {name} | {tw.one_line(out)}", fd=True)
        if not os.path.exists(write_dst):
            tw.remove_quietly(meta_backup)
            return res(f"ERROR (magick reported success but wrote no file) | {name} | expected: {write_dst}", fd=True)

    # The written file must have the source's sample layout, page for page (page 0 on the
    # thumbnail path). The output is always this run's own file here.
    if not tw.tiff_sample_layout_kept(layout_pages, write_dst, main_page_only=generate_thumb):
        tw.remove_quietly(meta_backup)
        tw.remove_quietly(write_dst)
        return res(f"ERROR (sample layout changed: {' '.join(layout_pages)} - output dropped, original untouched) | {name}", fd=True)

    staging_name = os.path.basename(write_dst)
    # Always restore the metadata from the source (or the container). ONE -q, not two: the
    # second suppresses WARNINGS, and a warning is the only signal exiftool gives when the
    # tagsfromfile source could not be opened -- it still exits 0. IPTC:CodedCharacterSet is
    # not part of -all:all: without it UTF-8 IPTC text was written back as Latin.
    exif_warn = True
    try:
        rc, out = tw.exiftool(["-q", "-overwrite_original", "-tagsfromfile", meta_backup or src,
                               "-all:all", "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet",
                               "-unsafe", write_dst], merge_stderr=True)
        # Only this one message is a failure besides the exit code: the SOURCE was never
        # opened. A tag-less source legitimately reports "No writable tags set".
        exif_warn = rc != 0 or bool(re.search("Error opening file", out, re.I))
    except Exception:
        exif_warn = True
    tw.remove_quietly(meta_backup)

    if not generate_thumb:
        if not tw.restore_tiff_subfile_types(src, write_dst, timeout):
            subfile_restore_failed = True

    # xmpMM:History: this rewrite, in its own exiftool call. Before the gates, so they check
    # the final bytes; the irreversible step stays last. A failed entry is a WARN.
    history = ("compress_tiff_zip legacy: ZIP (Deflate)" if mode < 0
               else f"compress_tiff_zip mode {mode}: ZIP (Deflate)")
    if generate_thumb and not no_thumb_note:
        history += " + thumbnail page"
    if not tw.add_tiff_history_entry(write_dst, tw.history_agent(), history):
        history_failed = True

    # Modes 1-7 have no pixel gate: a rewrite that dropped pages is data loss (audit
    # round 16). Fail closed and drop an output this run created.
    if not generate_thumb and 1 <= mode <= 7:
        pc_src = tw.tiff_page_count(src, timeout)
        pc_dst = tw.tiff_page_count(write_dst, timeout)
        if not pc_dst.ok or (pc_src.ok and pc_dst.count < pc_src.count):
            if in_place_final or tw.same_path(write_dst, final_dst):
                tw.remove_quietly(write_dst)
            detail = (f"output page count unreadable ({pc_dst.error})" if not pc_dst.ok
                      else f"{pc_src.count} -> {pc_dst.count} pages")
            return res(f"ERROR (page count shrink) | {name} | {detail}", fd=True)

    # This output REPLACES the original when it is written in place (temp sibling) -- and
    # also when it is staged (-StagingDir) for a destination that is the source itself
    # (legacy, mode 2 with -OutputDir on the input root, mode 5 inside ZIP/). Modes 0/9
    # keep the original in OLD_TIFFs/ and mode 8 has its own gate below.
    replaces_source = bool(in_place_final) or (
        (mode < 0 or 1 <= mode <= 7) and tw.same_path(final_dst, src))
    if replaces_source:
        # Only a fully decoded, pixel-verified ZIP whose metadata was restored may replace
        # the original; on any failure the new file is dropped and the source stays.
        in_place_ok = not exif_warn
        if in_place_ok:
            in_place_ok = tw.zip_integrity(write_dst, timeout, load)
        if in_place_ok:
            if generate_thumb:
                in_place_ok = tw.thumb_output_matches_source(
                    src, write_dst, 1 if no_thumb_note else 2, timeout)
            else:
                in_place_ok = tw.pixel_identical(src, write_dst, timeout, load)
        if not in_place_ok:
            tw.remove_quietly(write_dst)
            why = "metadata restore failed" if exif_warn else "in-place verification failed"
            return res(f"ERROR ({why} - original untouched) | {name}")
        if in_place_final:
            try:
                tw.move_file_replacing(write_dst, in_place_final)
            except OSError as e:
                tw.remove_quietly(write_dst)
                return res(f"ERROR (in-place replace failed) | {name} | {e}")

    can_delete = False
    integrity_failed = False
    integrity_reason = ""
    if mode == 8:
        # Always verified, the [no thumb] path included: the staged file overwrites the
        # original. Decode alone is not enough (a truncated page still decodes): it must be
        # pixel-identical to the source it replaces, and its metadata restored.
        verify = write_dst if not tw.same_path(write_dst, src) else src
        if exif_warn:
            integrity_reason = "metadata restore failed"
        ok8 = (not exif_warn) and tw.zip_integrity(verify, timeout, load) and os.path.exists(src)
        if ok8:
            if generate_thumb:
                ok8 = tw.thumb_output_matches_source(src, verify, 1 if no_thumb_note else 2, timeout)
            else:
                ok8 = tw.pixel_identical(src, verify, timeout, load)
        if ok8:
            can_delete = bool(opt["DeleteSource"])
        else:
            integrity_failed = True
    elif mode in (0, 9):
        # The original is archived in OLD_TIFFs/ only after this copy lands, so it must
        # decode first. Checked here, in the worker, to keep the decode parallel.
        if not tw.zip_integrity(write_dst, timeout, load):
            integrity_failed = True

    if exif_warn or thumb_marker_failed or subfile_restore_failed or history_failed:
        text = ("WARN (exiftool failed, ZIP ok)" + no_thumb_note + thumb_note
                + (" [thumb marker failed]" if thumb_marker_failed else "")
                + (" [subfiletype restore failed]" if subfile_restore_failed else "")
                + (" [history not written]" if history_failed else "")
                + f" | {name}")
    else:
        text = (f"OK ({comp} -> ZIP){no_thumb_note}{thumb_note}"
                + (" [SOURCE DELETED]" if can_delete else "") + f" | {name}")
    return res(text, fd=True, staging_name=staging_name, can_delete=can_delete,
               integrity_failed=integrity_failed, integrity_reason=integrity_reason)


def _job_result(t, result):
    """A worker that raised is that file's error, never a lost file."""
    if isinstance(result, Exception):
        name = os.path.basename(t["src"])
        return {"result": f"ERROR (unexpected) | {name} | {type(result).__name__}: {result}",
                "staging_name": None, "name": name, "src": t["src"], "final_dst": None,
                "multi": None, "can_delete": False, "integrity_failed": False,
                "integrity_reason": ""}
    return result


# -- Legacy mode (Mode = -1): the current folder, in place -----------

def run_legacy(run):
    opt = run.opt
    log = run.log
    if opt["InputDirFile"]:
        tw.say("ERROR: -InputDirFile needs -Mode 0-9")
        return 1
    root = os.getcwd()
    unreadable = []
    # Not the backups in OLD_TIFFs (relative to the root, so pointing legacy AT an
    # OLD_TIFFs folder on purpose still works), not a staged/temp file an interrupted run
    # left behind.
    files = [f for f in tw.list_files(root, True, unreadable)
             if tw.is_tiff_name(f.name)
             and not tw.path_excluded(f.dir, root, TOOL_OUTPUT_NAMES)
             and not tw.LEFTOVER_NAME_RE.search(f.name)]
    run.total = len(files)
    log(f"Log: {log.path}")
    for d in unreadable:
        log(f"cannot read folder (skipped): {d}", "WARN")

    if run.total == 0:
        log(f"No TIFF files found in: {root}", "WARN")
        log("Make sure you are in the right folder and that .tif or .tiff files exist")
        log(f"Log: {log.path}")
        return 1 if run.err > 0 else 0

    out_dir = opt["OutputDir"]
    staging = opt["StagingDir"]
    dry = opt["DryRun"]
    mode_label = ("SAFE (multi-page TIFFs will be skipped)" if opt["SafeMode"]
                  else "STANDARD (all TIFFs will be compressed)")
    log(f"TIFFs: {run.total} | Workers: {opt['Workers']} | "
        f"OutputDir: {out_dir or '(overwrite in place)'} | Staging: {staging or 'disabled'} | "
        f"DryRun: {dry}")
    log(f"Mode: {mode_label}")

    workers = effective_workers(run, [f.path for f in files], tw.COMPARE_BYTES_PER_PIXEL)

    groups = {}
    for f in files:
        groups.setdefault(f.dir.lower(), []).append(f)

    for key in sorted(groups):
        group_files = groups[key]
        group_dir = group_files[0].dir
        if len(groups) > 1 or len(group_files) > 1:
            log("")
            log(f"-- Group: {group_dir} ({len(group_files)} file(s))")

        if out_dir:
            final_dir = out_dir if os.path.isabs(out_dir) else tw.join_path(group_dir, out_dir)
        else:
            final_dir = group_dir
        write_dir = staging if (staging and not dry) else final_dir

        if staging and not dry:
            if os.path.isfile(staging):
                run.err += 1
                log(f"ERROR: StagingDir exists as a file: {staging}", "ERROR")
                continue
            os.makedirs(staging, exist_ok=True)
        if out_dir and not dry:
            if os.path.isfile(final_dir):
                run.err += 1
                log(f"ERROR: Output path exists as a file: {final_dir}", "ERROR")
                continue
            os.makedirs(final_dir, exist_ok=True)

        tasks = []
        for f in group_files:
            final_dst = tw.join_path(final_dir, f.name)
            # staged names carry the run id so an interrupt can clean them up
            if not tw.same_path(write_dir, final_dir):
                write_dst = tw.join_path(write_dir, tw.staged_name(run.run_id, f.name))
            else:
                write_dst = final_dst
            tasks.append({"src": f.path, "write_dst": write_dst, "final_dst": final_dst,
                          "name": f.name})

        staging_map = {}
        load = float(max(1, min(workers, len(tasks))))
        for t, result in tw.run_tasks(tasks, lambda q: process_job(run, q, load), workers):
            r = _job_result(t, result)
            if r["staging_name"]:
                staging_map[t["final_dst"].lower()] = (r["staging_name"], t["final_dst"])
            if r["multi"]:
                run.multi_paths.append(r["multi"])
            run.count(r["result"])

        # Move from staging to the final destination
        if not tw.same_path(write_dir, final_dir) and not dry:
            moved = 0
            for key2 in sorted(staging_map):
                staged, dest_path = staging_map[key2]
                stage_path = tw.join_path(write_dir, staged)
                if not os.path.exists(stage_path) or tw.same_path(stage_path, dest_path):
                    continue
                try:
                    stage_size = os.stat(stage_path).st_size
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    # never a copy straight onto the destination (a cross-volume staging)
                    mv_err = tw.move_file_into_place(stage_path, dest_path, run.run_id)
                    if mv_err:
                        raise OSError(mv_err)
                    if os.path.exists(dest_path) and os.stat(dest_path).st_size == stage_size:
                        moved += 1
                    else:
                        run.err += 1
                        log(f"ERROR (legacy move failed) | {os.path.basename(dest_path)}", "ERROR")
                except OSError as e:
                    run.err += 1
                    log(f"ERROR (legacy move failed) | {os.path.basename(dest_path)}: {e}", "ERROR")
            if moved > 0:
                log(f"  -> Moved {moved} file(s) -> {final_dir}")

    log("")
    log("-" * 50)
    dry_note = f" | {run.dry} dry-run (nothing written)" if run.dry > 0 else ""
    multi_note = f" | {run.multi} multi-page (not touched)" if opt["SafeMode"] else ""
    log(f"Done: {run.ok} OK | {run.skip} skipped{multi_note} | {run.warn} warnings | "
        f"{run.err} errors | {run.counter}/{run.total} processed{dry_note}")
    run.multi_list()
    log(f"Log: {log.path}")
    return 1 if run.err > 0 else 0


def effective_workers(run, paths, bytes_per_pixel):
    """The workers this run uses: -Workers, clamped by -CapWorkers and by the memory the
    ImageMagick processes need (not in a dry run, which starts none). 1 with
    -ForceSequential."""
    opt = run.opt
    if not run.parallel:
        return 1
    workers = opt["Workers"]
    if opt["CapWorkers"] > 0:
        if workers > opt["CapWorkers"]:
            run.log(f"Workers capped to {opt['CapWorkers']} (CapWorkers limit)")
        workers = min(workers, opt["CapWorkers"])
    if not opt["DryRun"]:
        workers = min(workers, tw.memory_capped_workers(
            opt["Workers"], paths, bytes_per_pixel, opt["WorkerMemoryFraction"], run.log))
    return max(1, workers)


# -- Modes 0-9 ------------------------------------------------------

def run_modes(run, bound):
    opt = run.opt
    log = run.log
    mode = opt["Mode"]
    dry = opt["DryRun"]
    overwrite = opt["Overwrite"]

    # Several input folders separated by ';' (the wizard's AutoFind), or -InputDirFile.
    entries = opt["InputDir"].split(";")
    if opt["InputDirFile"]:
        if "InputDir" in bound:
            log("ERROR: pass -InputDir or -InputDirFile, not both", "ERROR")
            return 1
        try:
            entries = tw.read_input_dir_file(opt["InputDirFile"])
        except tw.UsageError as e:
            log(f"ERROR: {e}", "ERROR")
            return 1

    input_roots = []
    missing_roots = []
    seen_roots = set()
    for d in entries:
        d = d.strip()
        if not d:
            continue
        # Normalised: "." or "a\..\b" once broke the root compare of the folder exclusion.
        d = tw.full_path(d)
        if d.lower() in seen_roots:
            continue
        seen_roots.add(d.lower())
        # A typo used to fall through to "No TIFF files found" and exit 0.
        if not os.path.isdir(d):
            missing_roots.append(d)
            continue
        input_roots.append(d)

    for m in missing_roots:
        log(f"ERROR: input directory not found: {m}", "ERROR")
    if not input_roots:
        log("No valid input directories specified.", "ERROR")
        log(f"Log: {log.path}")
        return 1

    # A RELATIVE -OutputDir is resolved ONCE, against the current folder: re-based on each
    # input root it scattered one flat output across A\out and B\out.
    if opt["OutputDir"] and not os.path.isabs(opt["OutputDir"]):
        opt["OutputDir"] = tw.full_path(opt["OutputDir"])
    staging = opt["StagingDir"]

    # a bad path in a ';' list still yields exit code 1
    run.err += len(missing_roots)

    log(f"Log: {log.path}")
    # Mode 8 resolves final_dst == src for a .tif source, so the compressed file is moved ON
    # TOP of the original whether or not -DeleteSource was passed.
    if mode == 8 and not opt["DeleteSource"]:
        log("NOTE: mode 8 replaces each original in place (verified pixel-identical first). "
            "-DeleteSource additionally removes a .tiff source whose output is written as .tif.", "WARN")
    log(f"Mode: {mode} | Workers: {opt['Workers']} | "
        f"OutputDir: {opt['OutputDir'] or '(in-place/flat)'} | Staging: {staging or 'disabled'} | "
        f"DryRun: {dry} | SafeMode: {opt['SafeMode']} | SkipLzw: {opt['SkipLzwAsCompressed']} | "
        f"Overwrite: {overwrite} | DeleteSource: {'ON' if opt['DeleteSource'] else 'OFF'}")

    report_stranded(run, input_roots)

    # Collect the files of every input folder
    all_files = []
    seen_files = set()
    excluded = backups = leftovers = 0
    unreadable = []
    for root in input_roots:
        dir_files = discover(run, mode, root, unreadable)
        for f in dir_files:
            # overlapping roots ("dir;dir\sub") list the same file twice
            if f.path.lower() in seen_files:
                continue
            seen_files.add(f.path.lower())
            # this toolkit's own backups, in EVERY mode, relative to THIS root
            if tw.path_excluded(f.dir, root, TOOL_OUTPUT_NAMES):
                backups += 1
                continue
            # staged/temp files an interrupted run left behind
            if tw.LEFTOVER_NAME_RE.search(f.name):
                leftovers += 1
                continue
            if tw.path_excluded(f.dir, root, run.exclude_names):
                excluded += 1
                continue
            f.input_root = root
            all_files.append(f)
        if len(input_roots) > 1 and dir_files:
            log(f"  Found {len(dir_files)} TIFF(s) in: {root}")
    for d in unreadable:
        log(f"cannot read folder (skipped): {d}", "WARN")
    if run.exclude_names:
        log(f"Excluded {excluded} file(s) under: {'; '.join(run.exclude_names)}")
    if backups > 0:
        log(f"Skipped {backups} file(s) in {'/'.join(TOOL_OUTPUT_NAMES)} (this tool's own backups)")
    if leftovers > 0:
        log(f"Skipped {leftovers} staged/temp file(s) left by an interrupted run", "WARN")

    # Sorted by full path: the _v2 renames below must not depend on the listing order.
    files = sorted(all_files, key=lambda f: f.path.lower())
    run.total = len(files)

    if run.total == 0:
        log(f"No TIFF files found for mode {mode} in: {'; '.join(input_roots)}", "WARN")
        run.summary()
        log(f"Log: {log.path}")
        return 1 if run.err > 0 else 0

    log(f"Found: {run.total} TIFF(s)")

    # One exiftool run per ~400 files instead of one per file. A file missing from the map
    # is probed on its own at its decision point.
    probe_start = time.time()
    comp_map = tw.get_compression_map([f.path for f in files])
    log(f"Compression probe: {len(comp_map)}/{run.total} file(s) in "
        f"{int((time.time() - probe_start) * 1000)}ms")

    # Mode 8 always stages: the original stays until its replacement is verified.
    if mode == 8 and not dry and not staging.strip():
        # Same-volume staging makes the final move one rename. With several input roots
        # there is no single same-volume choice: the temp folder (the move then copies next
        # to the destination first).
        if len(input_roots) == 1:
            # a short name: it sits INSIDE the user's tree and every staged path is built
            # under it
            default_staging = tw.join_path(input_roots[0], "_cs8_" + tw.new_run_id())
        else:
            default_staging = os.path.join(tempfile.gettempdir(), "_cs8_" + tw.new_run_id())
        msg = ("Mode 8 deletes source files after compression. A staging directory is required "
               "to avoid overwriting originals before verification.")
        log(msg, "WARN")
        log("Recommended: provide -StagingDir on a fast local drive.", "WARN")
        log(f"Default temporary staging will be used: {default_staging}", "WARN")
        if _is_interactive():
            tw.say("")
            tw.say(msg)
            tw.say(f"Staging directory: {default_staging}")
            try:
                answer = input("Use default temporary staging? [Y/n]: ")
            except EOFError:
                answer = ""
            if answer.strip().lower().startswith("n"):
                log("Mode 8 aborted: no staging directory selected.", "ERROR")
                return 1
        staging = default_staging
        opt["StagingDir"] = staging
        os.makedirs(staging, exist_ok=True)
        run.cleanup_dirs.append(staging)
        run.created_default_staging = True

    # Build the (src, write_dst, final_dst) tasks
    tasks = []
    used_names = {}        # mode 2: names given out per output folder
    claimed = {}           # other modes: destination -> the source that claimed it
    collision_notes = {}   # rename notes appended to the result lines
    for f in files:
        final_dst = resolve_output(f, mode, f.input_root or f.dir, opt)
        if not final_dst:
            run.count(f"SKIP (mode {mode} cannot resolve an output path) | {f.name}")
            continue

        # Modes 1-7 write a COPY elsewhere. A folder name can still make the output resolve
        # onto the source (ExportZipSubfolder "TIFF" in mode 7, ZipSuffix "_TIFF" in mode
        # 4), and the original was replaced with no OLD_TIFFs and no question. Modes
        # 1/3/4/6/7 never mean that; modes 2 (-OutputDir on the input root) and 5 (a file
        # already inside ZIP\) do when the user asked for it on the command line, but not
        # when the settings overlay set the name.
        if 1 <= mode <= 7 and tw.same_path(final_dst, f.path):
            overlay_names = [n for n in run.overlay_applied if n in _OVERLAY_PATH_NAMES]
            if mode not in (2, 5) or overlay_names:
                why = (f"settings overlay: {', '.join(overlay_names)}" if overlay_names else
                       "check -ZipSuffix / -ZipSubfolderName / -ExportZipSubfolder / -ExportTiffSubfolder")
                run.count(f"ERROR (mode {mode} would write over its own source - refused; {why}) | {f.name}")
                continue

        # An over-long output path used to fail deep inside the worker with a bare "No such
        # file or directory". Refused here, before any folder is created. An in-place write
        # goes through a temp sibling: +26 characters.
        final_len = len(final_dst) + (TEMP_SIBLING_CHARS if tw.same_path(final_dst, f.path) else 0)
        if final_len > MAX_PATH_CHARS:
            run.count(f"ERROR (path too long: {final_len} chars, Windows allows {MAX_PATH_CHARS}) | {f.name} | {final_dst}")
            continue

        final_dir = os.path.dirname(final_dst)
        if not dry and final_dir.strip() and not os.path.isdir(final_dir):
            os.makedirs(final_dir, exist_ok=True)

        if mode == 2:
            # Name collisions across source folders, by -DuplicateAction
            dest_dir = os.path.dirname(final_dst)
            dest_name = os.path.basename(final_dst)
            names = used_names.setdefault(dest_dir.lower(), set())
            stem, ext = os.path.splitext(dest_name)

            def taken(n, p):
                return n.lower() in names or os.path.exists(p)

            if dest_name.lower() in names:
                action = opt["DuplicateAction"]
                if action == "Skip":
                    log(f"SKIP (duplicate filename in flat output) | {f.name}", "INFO")
                    run.skip += 1
                    run.counter += 1
                    continue
                if action == "Overwrite":
                    # Last claimant wins; the dropped file is still REPORTED.
                    for sup in [t for t in tasks if tw.same_path(t["final_dst"], final_dst)]:
                        run.count(f"SKIP (superseded by a later duplicate in flat output) | {sup['name']}")
                    tasks = [t for t in tasks if not tw.same_path(t["final_dst"], final_dst)]
                    log(f"OVERWRITE (duplicate filename in flat output) | {f.name}", "INFO")
                else:
                    dest_name, final_dst = tw.numbered_name(dest_dir, stem, ext, taken)
            elif opt["DuplicateAction"] == "Numbered" and not overwrite \
                    and not tw.same_path(final_dst, f.path) and os.path.exists(final_dst):
                # not claimed in this run but already on disk
                dest_name, final_dst = tw.numbered_name(dest_dir, stem, ext, taken)
            names.add(dest_name.lower())
        else:
            # Two sources can claim one destination: modes 4-7 flatten different trees
            # into a shared folder, and photo.tif and photo.tiff both map onto photo.tif.
            key = final_dst.lower()
            first = claimed.get(key)
            # When the earlier claimant IS the destination file (photo.tif, the output of an
            # earlier run, next to the photo.tiff it came from) this output already exists.
            if not overwrite and first and not tw.same_path(first, f.path) \
                    and tw.same_path(first, final_dst):
                run.count(f"SKIP (exists) | {f.name}")
                continue
            if first and not tw.same_path(first, f.path):
                stem, ext = os.path.splitext(os.path.basename(final_dst))
                cand_name, cand = tw.numbered_name(
                    os.path.dirname(final_dst), stem, ext,
                    lambda n, p: p.lower() in claimed or os.path.exists(p))
                collision_notes[cand.lower()] = f" [renamed to {cand_name} to avoid collision]"
                final_dst = cand
            claimed[final_dst.lower()] = f.path

        if staging and not dry:
            if os.path.isfile(staging):
                run.count(f"ERROR (StagingDir exists as a file) | {f.name} | {staging}")
                continue
            os.makedirs(staging, exist_ok=True)
            write_dst = tw.join_path(staging, tw.staged_name(run.run_id, f.name))
        else:
            write_dst = final_dst

        # The same length guard for the staged path (and for a final_dst a rename
        # lengthened). Modes 0/9 always land through a temp sibling of final_dst.
        write_len = len(write_dst) + (TEMP_SIBLING_CHARS if tw.same_path(write_dst, f.path) else 0)
        final_len2 = len(final_dst) + (TEMP_SIBLING_CHARS if (tw.same_path(final_dst, f.path)
                                                               or mode in (0, 9)) else 0)
        if write_len > MAX_PATH_CHARS:
            run.count(f"ERROR (path too long: {write_len} chars, Windows allows {MAX_PATH_CHARS}) | {f.name} | {write_dst}")
            continue
        if final_len2 > MAX_PATH_CHARS:
            run.count(f"ERROR (path too long: {final_len2} chars, Windows allows {MAX_PATH_CHARS}) | {f.name} | {final_dst}")
            continue

        pre_comp = comp_map.get(f.path.lower(), "")
        if pre_comp == "-":
            pre_comp = ""

        if mode in (0, 9):
            # Everything that can refuse the file is decided HERE, while planning, so a
            # skipped file is never queued.
            comp = pre_comp
            if f.path.lower() not in comp_map:
                ok, comp = tw.ifd0_compression(f.path)
                if not ok:
                    comp = ""
            if not comp:
                run.count(f"ERROR (exiftool check) | {f.name} | cannot detect compression")
                continue
            if tw.is_compressed(comp, opt["SkipLzwAsCompressed"]):
                skip_compressed = True
                if opt["SkipCompressedWithThumb"]:
                    # only skip when a thumbnail is already embedded; otherwise reprocess
                    skip_compressed = tw.tiff_has_thumbnail_page(f.path, opt["MagickTimeout"])
                if skip_compressed:
                    note = (" [no thumbnail added: pass -SkipCompressedWithThumb]"
                            if opt["GenerateThumbnail"] else "")
                    run.count(f"SKIP ({comp}) | {f.name}{note}")
                    continue

            safe_checked = False
            if opt["SafeMode"]:
                pc = tw.tiff_page_count(f.path, opt["MagickTimeout"])
                if not pc.ok:
                    run.count(tw.page_count_error_line(pc, f.name))
                    continue
                if pc.count > 1 and not tw.tiff_has_only_subfile_pages(
                        f.path, pc.count, ("REDUCEDIMAGE", "REDUCED"), opt["MagickTimeout"]):
                    run.multi_paths.append(f.path)
                    run.count(f"MULTI ({pc.count} pages - skipped, original untouched) | {f.name}")
                    continue
                safe_checked = True

            if os.path.exists(final_dst) and not overwrite and not tw.same_path(final_dst, f.path):
                run.count(f"SKIP (exists) | {f.name}")
                continue

            task = {"src": f.path, "write_dst": write_dst, "final_dst": final_dst,
                    "name": f.name, "comp": comp, "safe_checked": safe_checked}
            if not dry:
                # Nothing moves here: the original stays in place until its compressed copy
                # has been written, decoded and put next to it (complete_keep_original).
                # Without -StagingDir the worker writes a temp sibling of final_dst, never
                # final_dst itself: for a .tif source that IS the original.
                if not staging:
                    task["write_dst"] = tw.new_temp_sibling_path(final_dst, run.run_id)
                    run.temp_files.add(task["write_dst"])
                try:
                    st = os.stat(f.path)
                except OSError as e:
                    run.count(f"ERROR (cannot read the original) | {f.name} | {e}")
                    continue
                task["keep_dir"] = tw.join_path(f.dir, "OLD_TIFFs")
                task["src_length"] = st.st_size
                task["src_mtime_ns"] = st.st_mtime_ns
            tasks.append(task)
        else:
            tasks.append({"src": f.path, "write_dst": write_dst, "final_dst": final_dst,
                          "name": f.name, "comp": pre_comp, "safe_checked": False})

    if not tasks:
        # INFO when nothing went wrong: every file skipped as already compressed is the
        # expected outcome of a re-run.
        log(f"No tasks to process (mode {mode} filtered all {run.total} file(s))",
            "WARN" if run.err > 0 else "INFO")
        run.summary()
        log(f"Log: {log.path}")
        _remove_default_staging(run)
        return 1 if run.err > 0 else 0

    # Only an in-place write or mode 8's delete gate runs a pixel compare in the workers;
    # modes 0/9 only decode-check the output before it lands.
    needs_compare = mode == 8 or any(tw.same_path(t["write_dst"], t["src"]) for t in tasks)
    workers = effective_workers(run, [t["src"] for t in tasks],
                                tw.COMPARE_BYTES_PER_PIXEL if needs_compare
                                else tw.CONVERT_BYTES_PER_PIXEL)

    # Grouped by the folder the outputs land in
    groups = {}
    for t in tasks:
        groups.setdefault(os.path.dirname(t["final_dst"]).lower(), []).append(t)

    for key in sorted(groups):
        group_tasks = groups[key]
        group_dir = os.path.dirname(group_tasks[0]["final_dst"])
        # the header names the folder the outputs land in
        if len(groups) > 1 or len(group_tasks) > 1:
            log("")
            log(f"-- Group: {group_dir} ({len(group_tasks)} file(s))")
        if not dry and group_dir and not os.path.isdir(group_dir):
            os.makedirs(group_dir, exist_ok=True)

        staging_map = {}
        error_srcs = set()
        integrity_failed_dst = set()
        results = []
        load = float(max(1, min(workers, len(group_tasks))))
        for t, result in tw.run_tasks(group_tasks, lambda q: process_job(run, q, load), workers):
            r = _job_result(t, result)
            results.append((t, r))
            key_dst = t["final_dst"].lower()
            if r["staging_name"]:
                staging_map[key_dst] = r["staging_name"]
            if r["multi"]:
                run.multi_paths.append(r["multi"])
            text = r["result"]
            if text.startswith("ERROR"):
                error_srcs.add(t["src"].lower())
            if r["integrity_failed"]:
                integrity_failed_dst.add(key_dst)
                text = (f"ERROR ({r['integrity_reason'] or 'ZIP integrity check failed'} - "
                        f"source preserved) | {r['name']}")
            if r["final_dst"] and key_dst in collision_notes:
                text += collision_notes[key_dst]
            run.count(text)

        # Move from staging to the final destination (modes 0/9 land below).
        if staging and not dry:
            moved = 0
            for t in group_tasks:
                if t.get("keep_dir"):
                    continue
                key_dst = t["final_dst"].lower()
                if key_dst not in staging_map:
                    continue
                stage_path = tw.join_path(staging, staging_map[key_dst])
                if key_dst in integrity_failed_dst:
                    # a bad ZIP must not replace the original: drop the staged file
                    tw.remove_quietly(stage_path)
                    continue
                dest_path = t["final_dst"]
                if not os.path.exists(stage_path) or tw.same_path(stage_path, dest_path):
                    continue
                dest_name = os.path.basename(dest_path)
                try:
                    stage_size = os.stat(stage_path).st_size
                    # never a copy straight onto the destination (a cross-volume staging)
                    mv_err = tw.move_file_into_place(stage_path, dest_path, run.run_id)
                    if mv_err:
                        raise OSError(mv_err)
                    if not os.path.exists(dest_path):
                        run.err += 1
                        error_srcs.add(t["src"].lower())
                        log(f"ERROR (move failed) | {dest_name}", "ERROR")
                    elif os.stat(dest_path).st_size != stage_size:
                        run.err += 1
                        error_srcs.add(t["src"].lower())
                        log(f"ERROR (size mismatch after move) | {dest_name}", "ERROR")
                    else:
                        moved += 1
                except OSError as e:
                    run.err += 1
                    error_srcs.add(t["src"].lower())
                    log(f"ERROR (move exception) | {dest_name}: {e}", "ERROR")
            if moved > 0:
                log(f"  -> Moved {moved} file(s) -> {group_dir}")

        # Modes 0/9: the copy lands first, THEN the original goes to OLD_TIFFs/. A task
        # whose worker failed, skipped or wrote a copy that does not decode never touches
        # its original; its leftover is dropped.
        if not dry:
            kept = 0
            for t in group_tasks:
                if not t.get("keep_dir"):
                    continue
                key_dst = t["final_dst"].lower()
                if staging:
                    written = tw.join_path(staging, staging_map[key_dst]) if key_dst in staging_map else None
                else:
                    written = t["write_dst"]
                if not written or not os.path.exists(written):
                    continue
                if t["src"].lower() in error_srcs or key_dst in integrity_failed_dst:
                    tw.remove_quietly(written)
                    continue
                k_err = complete_keep_original(run, written, t["src"], t["final_dst"], t["keep_dir"],
                                               t["src_length"], t["src_mtime_ns"])
                if k_err:
                    run.err += 1
                    tw.remove_quietly(written)
                    log(f"ERROR (keep original) | {t['name']} | {k_err}", "ERROR")
                else:
                    kept += 1
            if kept > 0:
                log(f"  -> {kept} original(s) archived in OLD_TIFFs after their compressed copy landed")

        # Mode 8: a .tiff source is deleted only after its .tif landed and decodes there.
        if mode == 8 and not dry:
            deleted = 0
            for t, r in results:
                if not r["can_delete"] or not os.path.exists(t["src"]):
                    continue
                if tw.same_path(t["src"], t["final_dst"]):
                    # in place: the original was already replaced by the compressed file
                    continue
                src_name = os.path.basename(t["src"])
                if not os.path.exists(t["final_dst"]):
                    run.err += 1
                    log(f"ERROR (final destination missing, source preserved) | {src_name}", "ERROR")
                    continue
                # The pixel gate ran on the STAGED file: the file that authorises the delete
                # is checked again where it actually sits.
                if not tw.zip_integrity(t["final_dst"], opt["MagickTimeout"]):
                    run.err += 1
                    log(f"ERROR (final ZIP failed integrity - source preserved) | {src_name}", "ERROR")
                    continue
                try:
                    tw.remove_file(t["src"])     # a read-only source is deleted too
                    deleted += 1
                except OSError:
                    run.err += 1
                    log(f"ERROR (failed to delete source after move) | {src_name}", "ERROR")
            if deleted > 0:
                log(f"  -> Deleted {deleted} source file(s) after successful move")

    run.summary()
    log(f"Log: {log.path}")
    _remove_default_staging(run)
    return 1 if run.err > 0 else 0


def _is_interactive():
    try:
        return sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _remove_default_staging(run):
    """Remove the default staging folder only when this run created it (mode 8) and it is
    empty; one that still holds staged files is kept, with a pointer to it."""
    staging = run.opt["StagingDir"]
    if not run.created_default_staging or not os.path.isdir(staging):
        return
    try:
        left = os.listdir(staging)
    except OSError:
        return
    if not left:
        try:
            os.rmdir(staging)
        except OSError:
            pass
    else:
        run.log(f"NOTE: {len(left)} file(s) left in temporary staging: {staging}", "WARN")


# -- Entry point ----------------------------------------------------

def run_main(argv, state):
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

    if opt["Mode"] > 9:
        tw.say(f"ERROR: -Mode {opt['Mode']} does not exist: the modes are 0 to 9")
        return 1

    try:
        exclude_names = tw.parse_exclude_folders(opt["ExcludeFolders"])
    except tw.UsageError as e:
        tw.say(f"ERROR: {e}")
        return 1

    # Only "thumbnail AFTER the main image" is supported: every page classifier of this
    # project treats page 0 as the main image, so a thumbnail-first file would read as
    # multi-page and be refused by every later run.
    if opt["GenerateThumbnail"] and opt["ThumbPage"] < 1:
        tw.say(f"ERROR: -ThumbPage {opt['ThumbPage']} is not supported: the thumbnail must come AFTER the main image.")
        tw.say("       Page 0 has to stay the full-resolution image -- SafeMode classifies a")
        tw.say("       thumbnail-first file as multi-page, so every later run would skip it.")
        tw.say("       Use -ThumbPage 1 (the default).")
        return 1

    srgb = tw.resolve_srgb_profile(opt["SrgbProfile"])
    # A profile path from the settings overlay that does not exist must never stop a run:
    # warn and auto-detect. A -SrgbProfile typed on the command line still fails.
    if opt["SrgbProfile"] and not srgb and "SrgbProfile" in overlay["applied"]:
        tw.say(f"WARNING: settings overlay: SrgbProfile not found: {opt['SrgbProfile']}; "
               f"using the auto-detected profile")
        opt["SrgbProfile"] = ""
        srgb = tw.resolve_srgb_profile("")
    if opt["SrgbProfile"] and not srgb:
        tw.say(f"ERROR: -SrgbProfile not found: {opt['SrgbProfile']}")
        return 1

    missing = tw.missing_tools(["exiftool", "magick"])
    if missing:
        tw.say(f"ERROR: Required tools not found in PATH: {', '.join(missing)}")
        tw.say("Please install the missing tools and try again.")
        return 1

    log = tw.RunLog(SCRIPT_NAME)
    run = Run(opt, log, overlay["applied"])
    run.srgb_profile = srgb
    run.exclude_names = exclude_names
    state["run"] = run
    if opt["StagingDir"].strip():
        run.cleanup_dirs.append(opt["StagingDir"])

    log(f"tiff-workflow {tw.TOOLKIT_VERSION} | compress_tiff_zip.py | {tw.python_label()}")
    if overlay["info"]:
        log(overlay["info"])
    for w in overlay["warnings"]:
        log(f"settings overlay: {w}", "WARN")
    if opt["GenerateThumbnail"] and not srgb:
        log("WARN: no sRGB ICC profile found -- embedded thumbnails from wide-gamut sources "
            "(ProPhoto, AdobeRGB) will have shifted colours. Pass -SrgbProfile <path.icc> to fix.", "WARN")
    # -ThumbPage is accepted for compatibility but only page 1 is implemented.
    if opt["GenerateThumbnail"] and opt["ThumbPage"] > 1:
        log(f"WARN: -ThumbPage {opt['ThumbPage']} is not implemented -- the thumbnail is always "
            f"written at page 1 (right after the main image). Ignoring the value.", "WARN")

    if opt["Mode"] < 0:
        return run_legacy(run)

    note = " [FORCED via -ForceSequential]" if opt["ForceSequential"] else ""
    tw.say(f"{tw.python_label()} ({'PARALLEL' if run.parallel else 'SEQUENTIAL'}){note}")
    return run_modes(run, bound)


def cleanup_after_stop(run):
    """After Ctrl+C or an unhandled error: remove this run's staged and temp files, and put
    back any original whose compressed copy never landed."""
    tw.cleanup_run_files(run.cleanup_dirs, run.run_id)
    for path in list(run.temp_files):
        tw.remove_quietly(path)
    # An original goes back unless its output exists AND decodes cleanly (modes 0/9 keep
    # archived originals for completed files); a partial output is deleted first.
    for mv in list(run.moved_originals):
        try:
            if not os.path.exists(mv["backup"]):
                continue
            if mv["final_dst"] and os.path.exists(mv["final_dst"]) and \
                    tw.zip_integrity(mv["final_dst"], run.opt["MagickTimeout"]):
                continue
            if mv["final_dst"]:
                tw.remove_quietly(mv["final_dst"])
            os.makedirs(os.path.dirname(mv["original"]), exist_ok=True)
            os.rename(mv["backup"], mv["original"])
            run.moved_originals.remove(mv)
            run.log(f"RESTORED (interrupt cleanup) | {mv['original']}", "WARN")
        except Exception as e:
            run.log(f"RESTORE FAILED | {mv['original']}: {e}", "ERROR")
    if run.created_default_staging:
        try:
            os.rmdir(run.opt["StagingDir"])     # only when it is empty
        except OSError:
            pass


def main(argv=None):
    tw.setup_console()
    state = {}
    try:
        return run_main(sys.argv[1:] if argv is None else argv, state)
    except BaseException as e:
        if isinstance(e, SystemExit):
            raise
        # Only Ctrl+C is an interrupt; any other error is a failed run and exits 1 (a real
        # failure was once logged as "Interrupted!" and read as success).
        run = state.get("run")
        interrupted = isinstance(e, KeyboardInterrupt)
        text = ("Interrupted! Cleaning up staging files..." if interrupted else
                f"FATAL (unhandled error) | {type(e).__name__}: {e} | cleaning up staging files...")
        if run:
            run.log(text, "WARN" if interrupted else "ERROR")
            try:
                cleanup_after_stop(run)
            except BaseException:
                pass
        else:
            tw.say(text)
        return 130 if interrupted else 1


if __name__ == "__main__":
    sys.exit(main())
