#!/usr/bin/env python3
"""copy_exif_to_tiff.py -- copy the EXIF of a Fuji S3/S5 Pro JPEG (or RAF) into the TIFF
exported from the same shot, optionally compressing the TIFF to ZIP/Deflate in the same
pass.

    python copy_exif_to_tiff.py --input-dir <session folder> [--compress-zip] [--dry-run]
    python copy_exif_to_tiff.py --input-dir <root> --auto-find --folder-pattern S5pro

The command line (v3.1): `--name value`, every yes/no as `--x` / `--no-x`, also as
`convert_tiff.py copy-exif ...`; `--help` lists the options. tools/legacy_args.py translates
a line of v3.0.0 or of the .ps1 (`-Name value`, `-SafeMode:$false`). Exit code 0 when
every file went right, 1 when any file failed or an input folder does not exist (with
--fail-on-warn also on a warning or a TIFF without a JPEG), 2 when the command line
cannot run as written. docs/README_copy_exif.md has the reference.
"""

import os
import re
import shutil
import sys
from dataclasses import dataclass
from typing import NamedTuple, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tiff_workflow_common as tw  # noqa: E402
from tiff_workflow_common import P  # noqa: E402

# -- CLI PARAMETERS -------------------------------------------------
# The defaults are the settings of this script: edit them here, or leave the file alone
# and put them in .tiff_workflow_settings.json (the names in OVERLAY_SPECS).
PARAMS = (
    P("input_dir", "str", ""),
    P("workers", "int", 16, lo=1, hi=64),
    P("dry_run", "switch"),
    P("skip_if_tiff_has_exif", "switch"),
    P("skip_lzw_as_compressed", "switch"),
    P("safe_mode", "bool", True),
    P("icc_policy", "str", "never"),
    P("compress_zip", "switch"),
    P("output_dir", "str", ""),
    P("staging_dir", "str", ""),
    P("overwrite", "switch"),
    P("auto_find", "switch"),
    P("folder_pattern", "str", "S5pro"),
    P("magick_timeout", "int", 30),
    P("fail_on_warn", "switch"),
    P("exclude_folders", "str", "", help="';'-separated folder NAMES to skip during discovery"),
    P("worker_memory_fraction", "float", 0.8, help="Cap parallel workers so their ImageMagick processes fit in this share of free memory; 0 = no cap"),
    P("input_dir_file", "str", "", help="UTF-8 text file, one input folder per line (the wizard uses it when --input-dir would exceed the command-line limit)"),
)

# Settings overlay (.tiff_workflow_settings.json): the ONLY names it can change in this script,
# each with the type/range it must have. Never a safety or per-run setting (input_dir,
# output_dir, staging_dir, workers, dry_run, safe_mode, skip_lzw_as_compressed, overwrite, ...):
# tests/test_settings_overlay.py pins that.
# OVERLAY-SPECS-BEGIN
OVERLAY_SPECS = {
    "magick_timeout": "int:1:",
    "worker_memory_fraction": "float:0:2",
}
# OVERLAY-SPECS-END

SCRIPT_NAME = "copy_exif"          # section of the settings overlay
LOG_NAME = "Copy-S5Pro-Exif"       # Logs/<this>/, the name the logs always had

# Tool-output folders AutoFind never descends into, matched as segments BELOW the root.
AUTOFIND_SKIP = ("Logs", "OLD_TIFFs", "ZIP", "_EXPORT", "converted_zip")


class Run:
    """The state of one run: options, log, counters."""

    def __init__(self, opt, log):
        self.opt = opt
        self.log = log
        self.run_id = tw.new_run_id()
        self.cleanup_dirs = []
        self.exclude_names = []
        self.counter = self.total = 0
        self.ok = self.skip = self.zip_skip = self.miss = 0
        self.err = self.warn = self.multi = 0
        self.multi_paths = []
        # Destinations already claimed in this run (only used with --output-dir, where every
        # session writes into the same folder). Shared across folders on purpose.
        self.claimed_dest = {}
        self._dir_listing = {}

    def count(self, line):
        self.counter += 1
        kind = tw.result_kind(line)
        if kind is tw.Kind.OK:
            self.ok += 1
            # OK+SKIP-ZIP: the EXIF WAS written, only the ZIP step was skipped (already
            # compressed / output exists) -- an OK, not a skip (audit round 21).
            if line.startswith("OK+SKIP-ZIP"):
                self.zip_skip += 1
        elif kind is tw.Kind.SKIP:
            self.skip += 1
        elif kind is tw.Kind.MISS:
            self.miss += 1
        elif kind is tw.Kind.ERROR:
            self.err += 1
        elif kind is tw.Kind.WARN:
            self.warn += 1
        elif kind is tw.Kind.MULTI:
            self.multi += 1
        self.log(f"[{self.counter}/{self.total}] {line}", kind.level)

    def error(self, text):
        """Count an error and log it, in one step: an error that is logged but not
        counted ends in exit code 0, and the wizard reads that as success."""
        self.err += 1
        self.log(text, "ERROR")

    def names_in(self, directory):
        """{lower-case name: real name} of the files in a folder (cached)."""
        key = directory.lower()
        if key not in self._dir_listing:
            self._dir_listing[key] = {f.name.lower(): f.name
                                      for f in tw.list_files(directory, False)}
        return self._dir_listing[key]


class JpegMatch(NamedTuple):
    """The file find_jpeg_pair chose for a TIFF."""
    path: str
    used_base: str      # the base name that matched (the TIFF's own, or with a suffix stripped)
    kind: str           # "exact", "variant" or "heuristic"
    source: str         # "JPEG" or "RAF"


@dataclass(frozen=True)
class Pair:
    """One TIFF and the file its EXIF comes from: what process_pair works on. The last
    four fields are None when no JPEG or RAF was found."""
    tiff: str
    tif_name: str
    tif_base: str
    dest_name: str
    jpeg: Optional[str]
    used_base: Optional[str]
    kind: Optional[str]
    source: Optional[str]


class PairResult(NamedTuple):
    """What process_pair returns: the result line, the path of a multi-page TIFF it
    skipped, the name of the staged ZIP it registered for the move."""
    line: str
    multi: Optional[str] = None
    staged: Optional[str] = None


def find_jpeg_pair(run, tif, jpg_index):
    """The JPEG (or RAF) a TIFF takes its EXIF from, or None.

    Search order (first hit wins), each step over every search folder: JPEG exact -> JPEG
    without the Capture One variant suffix (_1, _2 ...) -> RAF exact -> RAF without the
    variant suffix -> JPEG heuristic (_\\d{3,4} stripped). The RAF steps come before the
    heuristic: for DSC_0001.tif, DSC_0001.RAF is the right source and DSC.jpg is not
    (audit round 20). Folders: the TIFF's, its JPEG/ and JPG/ subfolders, then the same
    three one level up."""
    d = tif.dir
    base = tif.stem
    parent = tw.parent_dir(d)
    variant = re.sub(r"_\d{1,2}$", "", base)
    heur = re.sub(r"(_\d{3,4})$", "", base)
    steps = [(base, "exact", "JPEG")]
    if variant != base and variant:
        steps.append((variant, "variant", "JPEG"))
    steps.append((base, "exact", "RAF"))
    if variant != base and variant:
        steps.append((variant, "variant", "RAF"))
    if heur != base and heur:
        steps.append((heur, "heuristic", "JPEG"))
    candidates = [d, tw.join_path(d, "JPEG"), tw.join_path(d, "JPG")]
    if parent:
        candidates += [parent, tw.join_path(parent, "JPEG"), tw.join_path(parent, "JPG")]
    search_dirs = [c for c in candidates if os.path.isdir(c)]
    for b, kind, src in steps:
        for sd in search_dirs:
            if src == "JPEG":
                key = sd.lower() + "|" + b.lower()
                if key in jpg_index:
                    return JpegMatch(jpg_index[key], b, kind, "JPEG")
                # The index only covers the scanned root, so the parent folder (and its
                # JPEG/JPG subfolders) is probed on disk.
                for ext in (".jpg", ".jpeg"):
                    probe = tw.join_path(sd, b + ext)
                    if os.path.isfile(probe):
                        return JpegMatch(probe, b, kind, "JPEG")
            else:
                # From the folder listing, not a built path: the listing has the real
                # name (.RAF), a built path the case it was given.
                real = run.names_in(sd).get((b + ".raf").lower())
                if real:
                    return JpegMatch(tw.join_path(sd, real), b, kind, "RAF")
    return None


def _jpeg_copy_args(source, tag_source, icc_tag, target):
    """The exiftool tag arguments that copy the camera's EXIF onto the TIFF `target`.
    `source` is "JPEG" or "RAF"; `tag_source` the file the tags are read from (the JPEG/RAF
    itself, or the container made for a '%' name); `icc_tag` "-ICC_Profile" or "".

    No -unsafe: it stamps the JPEG's GEOMETRY (ImageWidth/Height, ExifImageWidth/Height,
    Orientation) onto a TIFF whose dimensions legitimately differ (Capture One crops and
    resizes). Those tags are excluded by name; the embedded thumbnail/preview images do not
    belong on the TIFF either.

    -IPTC:All does not copy IPTC:CodedCharacterSet, so a JPEG whose IPTC is UTF-8 landed
    on the TIFF as Latin: every Japanese keyword a '?' (audit round 28). The charset is
    copied by name (only when the JPEG has one), and the TIFF's OWN IPTC is copied onto
    itself first ('-tagsfromfile @'): exiftool then writes the whole block in the new
    charset, so a Latin caption the TIFF already had is not left mislabelled as UTF-8."""
    tags = ["-tagsfromfile", "@", "-IPTC:All",
            "-tagsfromfile", tag_source, "-EXIF:All", "-XMP:All", "-IPTC:All",
            "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet",
            "--ImageWidth", "--ImageHeight", "--ImageLength",
            "--ExifImageWidth", "--ExifImageHeight", "--PixelXDimension", "--PixelYDimension",
            "--Orientation", "--ThumbnailImage", "--PreviewImage", "--JpgFromRaw"]
    # A RAF's EXIF describes its embedded 1440x960 preview: the focal-plane resolution
    # (630 vs 1861 px/cm on an S5 Pro) is wrong for the TIFF.
    if source == "RAF":
        tags += ["--FocalPlaneXResolution", "--FocalPlaneYResolution", "--FocalPlaneResolutionUnit"]
    if icc_tag:
        tags.append(icc_tag)
    tags.append(target)
    return tags


def _zip_staged(run, name, tiff_target, facts, write_dst, timeout, load_factor, drop_copy, own):
    """The ZIP half of process_pair: write `tiff_target` as ZIP to the staged file
    `write_dst` and prove it. Returns None when the staged file may be registered for the
    move, else the ERROR result line -- by then the staged file is gone, and so is the copy
    in --output-dir (`drop_copy`). `facts`: tiff_target's tw.tiff_facts. `own`: see
    process_pair."""
    own.append(write_dst)       # a staged name carries this run's id: this pair's, half written too
    # magick turned a neutral RGB TIFF into a 1-channel gray one with the RGB profile still
    # in it, and the pixel gate below cannot see that (audit round 28). The source layout
    # picks the -type that keeps it and is the reference for the layout gate after the write.
    src_pages = facts.pages
    if src_pages is None:
        drop_copy()
        return f"ERROR (sample layout unreadable) | {name}"
    type_args = tw.magick_keep_type_args(src_pages)
    if not tw.associated_alpha_rewritable(tiff_target, src_pages, timeout):
        drop_copy()
        return f"ERROR (associated alpha that is not opaque: ImageMagick would change it - refused) | {name}"

    def drop_staged():
        tw.remove_quietly(write_dst)
        drop_copy()

    # Input literal, OUTPUT escaped: a '%' in the name sends the output somewhere else while
    # magick still exits 0.
    try:
        tw.magick_run(["-quiet", tiff_target, "-compress", "zip"] + type_args
                      + [tw.magick_path_spec(write_dst)])
    except tw.ToolError as e:
        failure = tw.one_line(e.output)
    else:
        failure = (None if os.path.exists(write_dst)
                   else f"magick reported success but wrote no file (expected: {write_dst})")
    if failure is not None:
        drop_staged()
        return f"ERROR (magick ZIP) | {name} | {failure}"

    # The staged ZIP is moved ON TOP of the TIFF it came from and there is no OLD_TIFFs
    # backup on this path, so it must be proven pixel-identical first. A zero exit from
    # magick is not proof: a page-short or truncated file still exits 0 and still decodes.
    if not tw.tiff_sample_layout_kept(src_pages, write_dst):
        drop_staged()
        return f"ERROR (sample layout changed: {' '.join(src_pages)} - original untouched) | {name}"
    if not tw.pixel_identical(tiff_target, write_dst, timeout, load_factor):
        drop_staged()
        return f"ERROR (ZIP pixel verification failed - original untouched) | {name}"

    # Same '%' trap as the JPEG copy, for a TIFF whose NAME carries one.
    # The metadata and the page markers go back in one exiftool (v3.1 phase 4).
    try:
        tiff_meta = tw.backup_tiff_metadata(tiff_target, write_dst)
    except tw.ToolError:
        done = tw.Finished(False, False, True)
    else:
        if tiff_meta:
            own.append(tiff_meta)
        markers = tw.subfile_marker_args(tiff_target, facts.subfile_types, timeout)
        done = tw.finish_written_tiff(write_dst, tiff_meta or tiff_target, markers)
        tw.remove_quietly(tiff_meta)
    if not done.metadata:
        drop_staged()
        return f"ERROR (exiftool metadata restore failed - original untouched) | {name}"
    if not done.markers:
        drop_staged()
        return f"ERROR (subfiletype restore failed - original untouched) | {name}"
    return None


def process_pair(run, p, write_dir, final_dir, load_factor):
    """One TIFF: copy the EXIF from its JPEG/RAF and, with --compress-zip, stage a verified
    ZIP copy. `p` is a Pair; returns a PairResult. Runs on a worker thread: it never logs
    or counts.

    An exception nobody expected must not leave this pair's own files behind: the copy in
    --output-dir (the next run would skip it as "exists", without EXIF), the metadata
    containers and the staged ZIP (v3.1; until v3.0.0 they stayed on disk)."""
    own = []
    try:
        return _copy_pair(run, p, write_dir, final_dir, load_factor, own)
    except BaseException:
        for path in own:
            tw.remove_quietly(path)
        raise


def _copy_pair(run, p, write_dir, final_dir, load_factor, own):
    """process_pair's work. `own`: the files of this pair that exist only while it works
    (process_pair removes them when this raises)."""
    opt = run.opt
    name = p.tif_name
    tiff = p.tiff

    if not p.jpeg:
        return PairResult(f"MISS | {name} | no matching JPEG or RAF (base: {p.tif_base})")

    # Only the _\d{3,4}$ strip is heuristic: it can match an unrelated JPEG (foto_2024.tif
    # -> foto.jpg). The Capture One variant suffix (_DSF4817_1 -> _DSF4817) is the normal
    # name of the photo and reports OK. Without case: DSC_0001.tif + dsc_0001.jpg is an
    # EXACT stem match.
    heur = bool(p.used_base and p.used_base.lower() != p.tif_base.lower()
                and p.kind == "heuristic")
    ok_prefix = "WARN (heuristic JPEG match)" if heur else "OK"
    heur_suffix = f" | TIFF base '{p.tif_base}' matched JPEG base '{p.used_base}'" if heur else ""
    raw_suffix = " [EXIF from RAW]" if p.source == "RAF" else ""
    jpeg_name = os.path.basename(p.jpeg)

    if opt["skip_if_tiff_has_exif"]:
        # Camera EXIF only (the Exif IFD): -EXIF:all includes IFD0 -- the size and
        # resolution every TIFF has -- and skipped every TIFF (round 23).
        try:
            out = tw.exiftool(["-q", "-q", "-G1", "-s", "-ExifIFD:all", tiff])
        except tw.ToolError:
            # Fail-closed: an unreadable TIFF must not be overwritten by the copy below
            return PairResult(f"ERROR (exiftool EXIF check) | {name} | cannot inspect TIFF, not overwriting")
        if any(ln.strip() for ln in out.split("\n")):
            return PairResult(f"SKIP (already has EXIF) | {name}")

    if opt["dry_run"]:
        zip_info = " + ZIP" if opt["compress_zip"] else ""
        return PairResult(f"DRY (EXIF{zip_info}) | {name} <= {jpeg_name}{raw_suffix}")

    timeout = opt["magick_timeout"] if opt["magick_timeout"] > 0 else 30

    if opt["safe_mode"]:
        pc = tw.tiff_page_count(tiff, timeout)
        if not pc.ok:
            return PairResult(tw.page_count_error_line(pc, name, "output"))
        if pc.count > 1:
            # REDUCEDIMAGE/REDUCED only, like compress_tiff_zip.py: MASK and PAGE are real
            # pages here (scanner RGB+IR files).
            if not tw.tiff_has_only_subfile_pages(tiff, pc.count, tw.THUMBNAIL_PAGE_TYPES, timeout):
                return PairResult(f"MULTI ({pc.count} IFDs -- skipped) | {name}", multi=tiff)

    icc_policy = opt["icc_policy"].lower()
    tiff_has_icc = False
    if icc_policy in ("preserve_tiff", "always"):
        try:
            out = tw.exiftool(["-s", "-s", "-s", "-ICC_Profile:all", tiff], check=False)
            tiff_has_icc = bool(out.strip())
        except tw.ToolError:
            tiff_has_icc = True     # not started: never replace a profile nobody could see
    copy_icc = icc_policy == "always" or (icc_policy == "preserve_tiff" and not tiff_has_icc)
    # A RAF does not describe the colour space of the exported TIFF.
    icc_tag = "-ICC_Profile" if (copy_icc and p.source != "RAF") else ""

    # With --output-dir (another folder than the source's) the TIFF is copied there first:
    # the original is never written to.
    tiff_target = tiff
    tiff_copied = False
    dest_tiff = None
    src_dir = tw.full_path(os.path.dirname(tiff)).rstrip("\\/")
    if final_dir and final_dir.lower() != src_dir.lower():
        dest_tiff = tw.join_path(final_dir, p.dest_name)
        if not os.path.exists(dest_tiff) or opt["overwrite"]:
            try:
                os.makedirs(final_dir, exist_ok=True)
                if os.path.isfile(dest_tiff) and tw.is_read_only(dest_tiff):
                    os.chmod(dest_tiff, 0o666)
                shutil.copy2(tiff, dest_tiff)
                tiff_target = dest_tiff
                tiff_copied = True
                own.append(dest_tiff)
            except OSError as e:
                # A partial copy must not stay behind: a later run without --overwrite would
                # skip it as "exists in OutputDir" and the truncated TIFF persists
                tw.remove_quietly(dest_tiff)
                return PairResult(f"ERROR (copy to OutputDir failed) | {name}: {e}")
        else:
            return PairResult(f"SKIP (exists in OutputDir) | {name}")

    def drop_copy():
        if tiff_copied:
            tw.remove_quietly(dest_tiff)

    # exiftool expands printf codes in the -tagsfromfile ARGUMENT: a JPEG named `50%off.jpg`
    # is read as `50ff.jpg`, exiftool warns and STILL EXITS 0. Park the tags in a container
    # with a safe name first; a name without '%' is passed through untouched.
    try:
        jpeg_meta = tw.backup_tiff_metadata(p.jpeg, tiff_target)
    except tw.ToolError:
        drop_copy()
        return PairResult(f"ERROR (metadata container failed - EXIF copy refused) | {name}")
    if jpeg_meta:
        own.append(jpeg_meta)
    jpeg_tag_source = jpeg_meta or p.jpeg

    tags = _jpeg_copy_args(p.source, jpeg_tag_source, icc_tag, tiff_target)

    # ONE -q, not two: the second suppresses WARNINGS, and a warning is the only signal
    # exiftool gives when the tagsfromfile source could not be opened -- it still exits 0.
    try:
        out = tw.exiftool(["-q", "-overwrite_original", "-P"] + tags, merge_stderr=True)
        copied = True
    except tw.ToolError:
        out, copied = "", False
    tw.remove_quietly(jpeg_meta)
    # Only "Error opening file" is fatal: the SOURCE was never read. A JPEG that carries
    # nothing writable reports differently and stays non-fatal.
    if not copied or re.search("Error opening file", out, re.I):
        drop_copy()
        return PairResult(f"ERROR (exiftool EXIF) | {name}")

    if not opt["compress_zip"]:
        copy_note = f" -> {final_dir}" if tiff_copied else ""
        return PairResult(f"{ok_prefix} | {name} <= {jpeg_name}{copy_note}{heur_suffix}{raw_suffix}")

    # One exiftool read: the compression here, the layout and the page markers for the ZIP
    # step (v3.1 phase 4).
    try:
        facts = tw.tiff_facts(tiff_target)
    except tw.ToolError:
        facts = None
    comp = facts.compression if facts else ""
    if not comp:
        drop_copy()
        return PairResult(f"ERROR (exiftool check) | {name} | cannot detect compression")
    if tw.is_compressed(comp, opt["skip_lzw_as_compressed"]):
        head = (f"WARN (heuristic JPEG match, already {comp} -- ZIP skipped)" if heur
                else f"OK+SKIP-ZIP ({comp})")
        return PairResult(f"{head} | {name}{heur_suffix}{raw_suffix}")

    # Run-scoped prefix: the interrupt cleanup only removes this run's staged files
    staging_name = tw.staged_name(run.run_id, p.dest_name)
    write_dst = tw.join_path(write_dir, staging_name)
    final_dst = tw.join_path(final_dir, p.dest_name)

    if os.path.exists(final_dst) and not opt["overwrite"] and not tw.same_path(final_dst, tiff) \
            and not tiff_copied:
        return PairResult(f"OK+SKIP-ZIP (exists) | {name}{raw_suffix}")

    # The staged file is registered for the move ONLY after every gate has passed: a staged
    # ZIP whose metadata restore failed was once moved ON TOP of the EXIF-bearing original.
    error = _zip_staged(run, name, tiff_target, facts, write_dst, timeout, load_factor, drop_copy, own)
    if error:
        return PairResult(error)

    drop_copy()
    head = "WARN (heuristic JPEG match)" if heur else "OK+ZIP"
    return PairResult(f"{head} | {name} <= {jpeg_name}{heur_suffix}{raw_suffix}", staged=staging_name)


def _dest_name(run, tif, final_dir):
    """The name `tif` gets in its destination folder.

    With --output-dir every session writes into the SAME folder, so two sessions holding
    DSC_0001.tif used to overwrite/skip each other. Later claimants get _v2, _v3, ..."""
    dest_name = tif.name
    if run.opt["output_dir"] and final_dir.lower() != tif.dir.lower():
        d_key = tw.join_path(final_dir, dest_name).lower()
        if d_key in run.claimed_dest and run.claimed_dest[d_key].lower() != tif.path.lower():
            dest_name, cand_path = tw.numbered_name(
                final_dir, tif.stem, tif.ext,
                lambda name, path: path.lower() in run.claimed_dest or os.path.exists(path))
            d_key = cand_path.lower()
            run.log(f"  RENAME (name already taken in OutputDir) | {tif.name} -> {dest_name}", "WARN")
        run.claimed_dest[d_key] = tif.path
    return dest_name


def _move_staged(run, group_files, staging_map, dest_names, write_dir, final_dir):
    """Move the staged ZIPs of a group to their final destination. Only what this run
    registered: an unprefixed file of the same name in the staging folder belongs to
    somebody else."""
    opt = run.opt
    if not (opt["compress_zip"] and not opt["dry_run"]):
        return
    moved = 0
    for tif in group_files:
        staged = staging_map.get(tif.path.lower())
        if not staged:
            continue
        stage_path = tw.join_path(write_dir, staged)
        dest_path = tw.join_path(final_dir, dest_names.get(tif.path.lower(), tif.name))
        if not os.path.exists(stage_path) or tw.same_path(stage_path, dest_path):
            continue
        # In place, dest_path IS the TIFF: its write time is read BEFORE the move --
        # afterwards it is the staged file's own fresh timestamp.
        orig_mtime = None
        try:
            orig_mtime = os.stat(tif.path).st_mtime_ns
        except OSError:
            pass
        # Never a copy straight onto the photo's name (a --staging-dir on another drive):
        # copied next to it, then swapped with one rename.
        failure = tw.land_staged_file(stage_path, dest_path, run.run_id)
        if failure is None:
            # The EXIF-only path keeps the source date (exiftool -P); the ZIP path does
            # the same.
            if orig_mtime is not None:
                try:
                    os.utime(dest_path, ns=(os.stat(dest_path).st_atime_ns, orig_mtime))
                except OSError:
                    pass
            moved += 1
        elif failure[0] == "error":
            run.error(f"ERROR (move failed) | {tif.name}: {failure[1]}")
        else:
            run.error(f"ERROR (move failed) | {tif.name}")
    if moved > 0:
        run.log(f"  -> Moved {moved} file(s) -> {final_dir}")


def process_folder(run, root):
    """Every TIFF directly in `root` (the session folder)."""
    opt = run.opt
    log = run.log
    unreadable = []
    all_files = tw.list_files(root, False, unreadable)
    # The JPEG index is always built recursively so JPEG/JPG subfolders are found
    jpg_files = [f for f in tw.list_files(root, True, unreadable)
                 if f.ext.lower() in (".jpg", ".jpeg")]
    # A temp sibling or a staged file a killed run left behind is not a photo.
    tiffs = [f for f in all_files
             if tw.is_tiff_name(f.name) and not tw.LEFTOVER_NAME_RE.search(f.name)]
    for d in sorted(set(unreadable), key=str.lower):
        log(f"cannot read folder (skipped): {d}", "WARN")

    if not tiffs:
        log(f"No TIFFs found in: {root}", "WARN")
        return

    run.total += len(tiffs)
    log(f"TIFFs: {len(tiffs)} | JPEGs: {len(jpg_files)}")

    jpg_index = {}
    for j in jpg_files:
        key = j.dir.lower() + "|" + j.stem.lower()
        if key not in jpg_index or j.ext.lower() == ".jpg":
            jpg_index[key] = j.path

    groups = {}
    for f in tiffs:
        groups.setdefault(f.dir.lower(), []).append(f)

    for key in sorted(groups):
        group_files = groups[key]
        group_dir = group_files[0].dir
        if len(groups) > 1 or len(group_files) > 1 or opt["auto_find"]:
            log("")
            log(f"-- Group: {group_dir} ({len(group_files)} file(s))")

        final_dir = opt["output_dir"] or group_dir
        write_dir = opt["staging_dir"] if (opt["staging_dir"] and not opt["dry_run"]) else final_dir

        # With no --staging-dir the staged ZIP is written INTO the source folder: register
        # whatever folder this run stages into, so an interrupt can clean it.
        if not opt["dry_run"]:
            if write_dir not in run.cleanup_dirs:
                run.cleanup_dirs.append(write_dir)
            if opt["compress_zip"] and opt["staging_dir"]:
                os.makedirs(opt["staging_dir"], exist_ok=True)
            if opt["compress_zip"] and opt["output_dir"]:
                os.makedirs(opt["output_dir"], exist_ok=True)

        dest_names = {}
        pairs = []
        for tif in group_files:
            match = find_jpeg_pair(run, tif, jpg_index)
            dest_name = _dest_name(run, tif, final_dir)
            dest_names[tif.path.lower()] = dest_name
            pairs.append(Pair(
                tiff=tif.path, tif_name=tif.name, tif_base=tif.stem, dest_name=dest_name,
                jpeg=match.path if match else None,
                used_base=match.used_base if match else None,
                kind=match.kind if match else None,
                source=match.source if match else None))

        # The cap is about how many ImageMagick processes run at once; --dry-run starts none.
        workers = opt["workers"]
        if not opt["dry_run"]:
            workers = tw.memory_capped_workers(workers, [p.tiff for p in pairs],
                                               tw.COMPARE_BYTES_PER_PIXEL,
                                               opt["worker_memory_fraction"], log, quiet=True)
        load = float(max(1, min(workers, len(pairs))))   # workers running at once share the disk

        staging_map = {}
        for p, result in tw.run_tasks(pairs,
                                      lambda q: process_pair(run, q, write_dir, final_dir, load),
                                      workers):
            if isinstance(result, Exception):
                result = PairResult(f"ERROR | {p.tif_name} | {type(result).__name__}: {result}")
            if result.multi:
                run.multi_paths.append(result.multi)
            if result.staged:
                staging_map[p.tiff.lower()] = result.staged
            run.count(result.line)

        _move_staged(run, group_files, staging_map, dest_names, write_dir, final_dir)


def _input_dirs(opt, log):
    """(the folders to process, the entries that do not exist), or None after logging why
    the list file cannot be used (--input-dir together with --input-dir-file is refused
    before, by run_main). Without --input-dir and --input-dir-file: the current folder.

    A bad path used to fall through to "No TIFFs found" and exit 0, and convert_tiff.py
    gates Step 2 of the combined workflow on this exit code."""
    entries = []
    if opt["input_dir_file"]:
        try:
            entries = tw.read_input_dir_file(opt["input_dir_file"])
        except tw.UsageError as e:
            log(f"ERROR: {e}", "ERROR")
            return None
    elif opt["input_dir"]:
        entries = opt["input_dir"].split(";")

    if not entries:
        return [os.getcwd()], []
    return tw.resolve_input_dirs(entries)


def _autofind_folders(root, pattern, exclude_names=()):
    """(the folders below `root` whose name contains `pattern`, how many more of them
    --exclude-folders left out). The pattern is text, not a wildcard: a '[' in a folder name
    is itself. The tool-output folders and the --exclude-folders names are matched as
    segments BELOW the root (a session inside an excluded folder, or named like one)."""
    hits = [d for d in tw.list_dirs(root, True)
            if pattern.lower() in os.path.basename(d).lower()
            and not tw.path_excluded(d, root, AUTOFIND_SKIP)]
    kept = [d for d in hits if not tw.path_excluded(d, root, exclude_names)]
    return kept, len(hits) - len(kept)


def _log_summary(run):
    log = run.log
    log("")
    log("-" * 50)
    zip_note = f" (ZIP step skipped on {run.zip_skip})" if run.zip_skip > 0 else ""
    log(f"Done: {run.ok} OK{zip_note} | {run.skip} skipped | {run.miss} no JPEG pair | "
        f"{run.multi} multi-page | {run.warn} warnings | {run.err} errors | "
        f"{run.counter}/{run.total} processed")
    if run.multi > 0:
        log("")
        log("-- Multi-page TIFFs found (not touched):")
        for mp in sorted(run.multi_paths, key=str.lower):
            log(f"   {mp}", "WARN")
    log(f"Log: {log.path}")


def run_main(argv, state):
    if tw.asks_for_help(argv):
        tw.say(tw.help_text(PARAMS, os.path.basename(__file__), __doc__))
        return 0
    try:
        opt, bound = tw.parse_args(argv, PARAMS)
    except tw.UsageError as e:
        tw.say(f"ERROR: {e}")
        return tw.EXIT_USAGE

    overlay = tw.apply_settings_overlay(SCRIPT_NAME, OVERLAY_SPECS, opt, bound,
                                        os.path.dirname(os.path.abspath(__file__)))
    if overlay["info"]:
        tw.say(overlay["info"])
    for w in overlay["warnings"]:
        tw.say(f"WARNING: settings overlay: {w}")

    if opt["input_dir_file"] and opt["input_dir"]:
        tw.say("ERROR: pass --input-dir or --input-dir-file, not both")
        return tw.EXIT_USAGE

    # ';'-separated folder NAMES (not paths). This backend lists the TIFFs of a folder
    # without descending, so inside a folder there is nothing to exclude: the names apply
    # to the folders --auto-find finds (v3.1; until v3.0.0 the flag did nothing here).
    try:
        exclude_names = tw.parse_exclude_folders(opt["exclude_folders"])
    except tw.UsageError as e:
        tw.say(f"ERROR: {e}")
        return tw.EXIT_USAGE

    if tw.output_dir_error(opt["output_dir"]):
        tw.say(f"ERROR: {tw.output_dir_error(opt['output_dir'])}")
        return 1

    need = ["exiftool"] + (["magick"] if (opt["safe_mode"] or opt["compress_zip"]) else [])
    missing = tw.missing_tools(need)
    if missing:
        tw.say(f"ERROR: Required tools not found in PATH: {', '.join(missing)}")
        tw.say("Please install the missing tools and try again.")
        return 1
    # Only the ZIP step writes through exiftool_commands; the EXIF copy alone works with an
    # older exiftool as before.
    problem = tw.exiftool_status_problem() if opt["compress_zip"] else None
    if problem:
        tw.say(f"ERROR: {problem}")
        return 1

    log = tw.RunLog(LOG_NAME)
    run = Run(opt, log)
    run.exclude_names = exclude_names
    state["run"] = run
    if opt["staging_dir"].strip():
        run.cleanup_dirs.append(opt["staging_dir"])

    if opt["output_dir"].strip():
        opt["output_dir"] = tw.full_path(opt["output_dir"].rstrip("\\/"))
    else:
        opt["output_dir"] = ""

    # Before the banner: an unusable --input-dir-file is the first line of the log.
    resolved = _input_dirs(opt, log)
    if resolved is None:
        return 1
    input_dirs, missing_roots = resolved

    log(f"tiff-workflow {tw.TOOLKIT_VERSION} | copy_exif_to_tiff.py | {tw.python_label()}")
    if overlay["info"]:
        log(overlay["info"])
    for w in overlay["warnings"]:
        log(f"settings overlay: {w}", "WARN")
    log(f"Log: {log.path}")

    for m in missing_roots:
        log(f"ERROR: input directory not found: {m}", "ERROR")
    run.err += len(missing_roots)

    if not input_dirs:
        log("No valid input directories specified.", "ERROR")
        log(f"Log: {log.path}")
        return 1

    log(f"Workers: {opt['workers']} | CompressZip: {opt['compress_zip']} | "
        f"SkipIfTiffHasExif: {opt['skip_if_tiff_has_exif']} | "
        f"OutputDir: {opt['output_dir'] or '(overwrite in place)'} | "
        f"Staging: {opt['staging_dir'] or 'disabled'} | DryRun: {opt['dry_run']}")

    for root in input_dirs:
        if opt["auto_find"]:
            pattern = opt["folder_pattern"]
            log(f"AutoFind mode | Pattern: '{pattern}' | Root: {root}")
            matching, left_out = _autofind_folders(root, pattern, run.exclude_names)
            if left_out:
                log(f"Excluded {left_out} folder(s) under: {'; '.join(run.exclude_names)}")
            if not matching:
                log(f"No folders matching '{pattern}' found in: {root}", "WARN")
            else:
                log(f"Folders found: {len(matching)}")
                for d in matching:
                    log(f"  {d}")
                log("")
                for d in matching:
                    log(f"==== Processing: {d}")
                    process_folder(run, d)
                    log("")
        else:
            log(f"Root: {root}")
            process_folder(run, root)

    _log_summary(run)

    if run.err > 0 or (opt["fail_on_warn"] and (run.warn + run.miss) > 0):
        return 1
    return 0


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
            tw.cleanup_run_files(run.cleanup_dirs, run.run_id)
        else:
            tw.say(text)
        return 130 if interrupted else 1


if __name__ == "__main__":
    sys.exit(main())
