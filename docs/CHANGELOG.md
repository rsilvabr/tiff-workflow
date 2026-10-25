# Changelog

Release-level history, moved here from `README.md`. Per-bug technical detail -- what broke, why,
which code path, and which test pins it -- is in [`bugs_fixed.md`](bugs_fixed.md). Downloads are on
the [GitHub Releases page](https://github.com/rsilvabr/tiff-workflow/releases).

> **Numbering.** Sections carry the version the work shipped in. Audit rounds are the stable
> identifier and match the `fix(audit-round-N)` commits: rounds 8-14 shipped as **v2.3**, rounds
> 15-16 as **v2.4**, rounds 17-22 as **v2.5**.

---

## v2.7.2

- **A preset runs the backends next to the wizard it is started from.** It used to run
  the backend at the path recorded when it was saved: after moving or updating the
  toolkit into another folder, a preset failed with "Backend script no longer exists", or
  quietly ran the older copy if that folder was still there (round 24)

## v2.7.1

- **Copy EXIF: `-SkipIfTiffHasExif` works.** It checked for any EXIF tag, and in exiftool
  that includes the basic tags every TIFF has (size, resolution), so it skipped every TIFF --
  including the Hyper Utility exports it is meant for (451 of 451 on a real S5 Pro folder).
  It now looks for camera EXIF only (the Exif IFD: exposure, aperture, dates). The wizard
  never passes it, so wizard runs and presets were not affected (round 23)
- The Copy EXIF reference says where its log goes (`Logs\Copy-S5Pro-Exif\` under the folder
  the run starts from)

## v2.7

- **The padded 16 -> 8-bit conversion records itself in the photo.** It appends one entry
  to `xmpMM:History`, in the format jxl-photo uses: `Action=converted`, `When`,
  `SoftwareAgent=tiff-workflow <version> (<link>)` and
  `Parameters=convert_tiff: 16-bit padded -> 8-bit ZIP (pixel-identical)`. Earlier entries
  (Capture One, ...) are kept, and the entry travels on into a jxl-photo master made from the
  file. If the entry cannot be written, the original is kept, as for a failed EXIF copy.
  Read it with `exiftool -struct -XMP-xmpMM:History file.tif`
- **No silent phases in Diagnose.** The folder search reports every few seconds (folders
  scanned, TIFFs found, elapsed), the image-size read for the memory estimate reports each
  batch, and the checks print `N/total checked in X -- about Y left` every 25 files
- **Diagnose no longer cuts the workers because of one huge scan.** The memory cap sized
  every worker for the largest file of the batch: one 220 MP scan among 32 MP photos made
  each worker count as 15 GB. Diagnose now keeps the requested workers and each check
  reserves the memory its own file needs; a large file waits for room (alone if it must)
- **Large scans no longer time out the header probes.** `magick identify` (bit depth,
  dimensions, page count, SubfileType) ran without `-ping`, so it decoded the whole image
  under a fixed 30 s limit: 782 MB film scans on a busy hard disk failed with
  "timed out after 30.0 seconds". The probes now read the header only and their limit grows
  with the file size and the number of workers
- **A Diagnose preset can convert what it finds.** Workflow 7 asks whether a repeat or
  preset should also convert the padded files (default no). Answered yes, it runs the
  menu's conversion right after the check, unattended too -- pixel-verified, originals in
  `OLD_PADDED/` -- and the report says how many were converted
- **Folder names in Japanese (or any non-ANSI script) work in the wizard's own steps.**
  exiftool reads its command line in the ANSI code page, so under `伊豆沼_夏` every metadata
  step failed: the padded conversion refused every file (`subfile_restore_failed`) and
  Delete OLD_TIFFs refused every purge. All its exiftool calls now pass their arguments in a
  UTF-8 argfile, as the PowerShell backends always did
- **The padded conversion finishes each file as soon as it is ready** -- printed, backed up,
  moved into place, with `N/total done -- about X left` every 25 files -- instead of
  converting the whole batch first and replacing everything at the end; it also budgets
  memory per file like Diagnose
- **Japanese folder names show correctly on screen.** The backends' output reached the
  wizard in the console's OEM code page, so `谷津バラ園` printed as `?????` (the log file was
  always right). Each backend writes UTF-8 when its output is redirected
- **PowerShell 5.1 started from a folder with `[ ]` in its name** silently began in
  `System32` and died before its first log line; every backend now re-enters the real
  start folder, and builds its log path with `[System.IO.Path]` instead of `Join-Path`

## v2.6.2

- **Temporary copies no longer pile up in `%TEMP%`.** ImageMagick cannot open a path with
  `[` `]` on Windows (a `[FINAL]` folder), so the wizard reads such files through a
  bracket-free alias in `%TEMP%`. When the photos sit on another drive than `%TEMP%` the alias
  is a full copy, and every one was kept until the run ended -- about three copies per file. A
  Diagnose over 5000 TIFFs filled 434 GB of the system drive. Each file now gets one alias,
  deleted as soon as that file is done (Diagnose, the padded conversion, Delete OLD_TIFFs)
- The one-time creation of the alias folder is properly locked (parallel workers made several)
- Diagnose's progress line shows the file's path below the scan root
  (`2025\Shoot [FINAL]\_EXPORT\TIFF16\DSC_0001.tif`), not only its name, so same-named files
  in different sessions can be told apart

## v2.6.1

- The Diagnose cache is written every 25 files and when a run is stopped, not only at the
  end, so an interrupted run keeps what it already checked
- Workflow 7 from the menu uses the cache too: converting what a preset found no longer
  re-checks the whole library
- The padded 16 -> 8-bit conversion compares the rewrite with the original pixel by pixel
  and keeps only identical results. It used to trust the Diagnose verdict, so a file
  wrongly passed to it would have been rounded down to 8-bit

## v2.6

One new feature; nothing changes for the other workflows.

- **Diagnose as a preset** -- workflow 7 can now be repeated and saved as a preset. A preset
  run re-scans the saved folders and only reports (full paths in
  `Logs\convert_tiff\diagnose_<timestamp>.log`); it never converts. Two new questions narrow
  the search: a folder path the files must sit under (`_EXPORT/TIFF16`) and file-name
  patterns to skip (`*NXSTUDIO*`). Files already checked and unchanged are not checked again
  (`DIAGNOSE_CACHE`)

## v2.5

Audit rounds 17-22. New features plus hardening; nothing changes for a run that already works.

- **Presets** -- save the last workflow under a name (main menu **[5]**) and run it unattended with
  `python convert_tiff.py --run-preset NAME` (`--list-presets`, `--dry-run`). A Fuji preset redoes
  the AutoFind each time, so new sessions are picked up. An unattended preset that would delete
  sources is refused unless `--dry-run`
- **Parallel workers fit in memory** -- the number of parallel ImageMagick jobs is capped by the
  free memory (`-WorkerMemoryFraction`, default 0.8; 0 turns the cap off), so big film scans no
  longer push the machine into swapping
- **Copy EXIF finds Capture One variants** (`_DSF4817_1.TIF` -> `_DSF4817.JPG`) and falls back
  to the RAF when a session has no JPEG
- **Long AutoFind lists** -- a folder list past the Windows command-line limit is passed to the
  backend through a temporary file (`-InputDirFile`)
- The wizard reads the backends' own settings instead of keeping copies, and passes them
  explicitly, so an edited default in a `.ps1` is respected
- Copy EXIF with `-CompressZip` counts a file that got its EXIF as OK in the summary
- Hardening: subprocess calls without reader threads, timeouts that scale with the number of
  workers, and helper functions pinned identical across the PowerShell backends
- New `tools/real_photo_battery.py`: the backends end to end on copies of real photos, under both
  shells

**Upgrading:** "Repeat last" of a manifest workflow saved by v2.4 is refused with a message --
run the manifest once more from menu **[3]** and it is recorded with the new data.

Per-bug detail: [`bugs_fixed.md`](bugs_fixed.md), rounds 17-22.

---

## v2.4

Audit rounds 15 and 16. Round 15 is the section below -- it was written while unreleased and landed
after the v2.3 tag. Round 16 (interrupt rollback, metadata-aware purge gate, fail-closed metadata
restores in Copy EXIF, size-scaled timeouts) is covered in `RELEASE_v2.4.md` and, per bug, in
[`bugs_fixed.md`](bugs_fixed.md).

### Audit round 15

Two of these lose data silently; if you have file names containing `%`, this round matters.

- **A `%` in a file name stripped every metadata tag** and the run still said `OK`. exiftool
  expands printf-style filename codes in the `-tagsfromfile` argument (`50%off.tif` is read as
  `50ff.tif`), warns, and **exits 0** — so the check every backend relied on passed. In mode 8
  with `-DeleteSource` it was permanent. Doubling the `%` does not escape it; the metadata now
  travels in a container whose name has none. Every `-tagsfromfile` call also fails on
  `Error opening file` now, whatever the exit code
- **Modes 1-7 walked into `OLD_TIFFs/` and compressed the backups.** The real file is already
  Deflate so it was skipped, which left the *backup* to claim the output name — a mode 2 run
  over a mode 9 tree produced a flat folder holding nothing but `_v2` copies of backups. One
  exclusion list now covers every mode, relative to the folder you point at
- **The thumbnail generator no longer refuses `ZIP/` and `_EXPORT/` trees** — that is where the
  compressed library and the export trees live, and the exclusion was silent, undocumented and
  impossible to turn off. It takes `-ExcludeFolders` now and logs what it left out
- **`-Page all` no longer makes `-Page 0` unrunnable**: the frame set only counts as "already
  there" when `-Page all` was actually asked for
- Files left behind by an interrupted run are no longer discovered as photos
- Workflows [6] and [7] work on `%` names (the Python side never got the escaping fix)
- Mode 8 says at startup that it replaces each original in place, with or without
  `-DeleteSource` — it always did, and the docs implied otherwise

---

## v2.3

Six audit rounds (8-13), driven by real scanner and Capture One files:

- **Folder exclusion** — `-ExcludeFolders "_EXPORT;temp"` skips whole folder trees by name, in every mode and in all three backends; the wizard asks once per workflow. Matching is relative to the folder you point at, so a name appearing higher up the path never excludes anything
- **Worker cap decoupled from Safe Mode** — `-CapWorkers` (default: no cap), so Safe Mode no longer quietly limits parallelism
- **Discovery is much faster** — the "already compressed?" probe is batched, one `exiftool` per ~400 files instead of one process per file
- **Colour-managed thumbnails** — wide-gamut sources are converted through their embedded ICC profile before it is stripped, so a ProPhoto export finally produces a thumbnail that looks like the photo
- `-GenerateThumbnail` no longer silently drops pages, and no longer leaves the main image marked as a page of a multi-page document; `-ThumbPage 0` is refused, because those files were classified multi-page by the tool's own Safe Mode on every later run
- **Mode 8 works on deeply nested archives** — staged names were long enough to blow past Windows' 260-character path limit, which failed *every* file with an unexplained error; an over-long path is now refused up front, with the number
- In-place replacement and the mode 8 delete now require a pixel-identical check, not just "the file decodes"
- Safe Mode classifies a thumbnail by size as well as by tag, so a full-size IR `MASK` page is always treated as a real page
- "Repeat last workflow" rebuilds the command for the PowerShell it is about to run; it could previously turn Safe Mode back on

⚠️ **Breaking:** `-ThumbPage 0` is now refused (exit 1). It only ever produced files this toolkit would not touch again.

See [`bugs_fixed.md`](bugs_fixed.md) for the complete list.

---

## Earlier releases

Per-release notes for v2.0, v2.2, v2.3 and v2.4 are kept at the repository root
(`RELEASE_v2.0.md`, `RELEASE_v2.2.md`, `RELEASE_v2.3.md`, `RELEASE_v2.4.md`).
