# compress_tiff_zip.py -- ZIP Compression Backend

Python compression backend with modes 0-9 CLI flags. Called by `convert_tiff.py` wizard or used directly.

**Requires:** Python 3.9+ (standard library only), ImageMagick 7, exiftool. `tiff_workflow_common.py`, the module the three backends share, must sit next to the script.

Until v2.9.1 this backend was `compress_tiff_zip.ps1`; that edition is described in [powershell_edition.md](powershell_edition.md). The command line is the same -- only the way to start it changed.

---

## CLI Reference

```powershell
# In-place, non-recursive (mode 0)
python compress_tiff_zip.py -Mode 0 -InputDir .

# Recursive, in-place with delete (mode 8)
python compress_tiff_zip.py -Mode 8 -InputDir F:\Photos -DeleteSource

# Subfolder per folder (mode 1)
python compress_tiff_zip.py -Mode 1 -InputDir F:\Photos -ZipSubfolderName "ZIP"

# Flat to output folder (mode 2)
python compress_tiff_zip.py -Mode 2 -InputDir F:\Photos -OutputDir E:\all_zips

# Folder rename (mode 4)
python compress_tiff_zip.py -Mode 4 -InputDir F:\Photos -ZipSuffix "_ZIP"

# With staging and workers
python compress_tiff_zip.py -Mode 3 -InputDir F:\Photos -StagingDir E:\staging -Workers 12

# Dry-run
python compress_tiff_zip.py -Mode 3 -InputDir F:\Photos -DryRun
```

The parameters keep the form they had in the `.ps1`: `-Name value`, `-Switch`,
`-SafeMode:$false`. Names are matched without case, and an unambiguous prefix of a name works
(`-Dry` for `-DryRun`). A `bool` takes `$true`/`$false`, `true`/`false` or `1`/`0`. Typed in a
PowerShell terminal, a `$false` inside an argument must be quoted (`'-SafeMode:$false'`) -- or
simply write `-SafeMode:false`. A command line that cannot be read (an unknown name, a value out
of range, a value without its `-Parameter`) ends the run with exit code 1 before anything is
touched.

---

## Parameters

The defaults below are the `PARAMS` table at the top of `compress_tiff_zip.py`.

### Core

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-Mode` | int | `-1` | **-1 = legacy mode** (no CLI params = original behavior). **0-9** = new mode behavior. A value above 9 is refused (exit code 1) |
| `-InputDir` | string | `.` | Input folder. Can be relative or absolute |
| `-InputDirFile` | string | `""` | UTF-8 text file with one input folder per line, used instead of `-InputDir` (not together). The wizard switches to it automatically when the folder list would exceed the Windows command-line limit. compress: modes 0-9 only. |

### Output path control

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-OutputDir` | string | `""` | Mode 2: flat output root. `""` = same as input dir |
| `-ZipSuffix` | string | `_ZIP` | Mode 4: folder rename suffix. `TIFF` in folder name replaced with this |
| `-ZipSubfolderName` | string | `ZIP` | Mode 1/3: subfolder name inside each folder |
| `-ExportMarker` | string | `_EXPORT` | Modes 6/7: folder name that marks export root |
| `-ExportZipSubfolder` | string | `ZIP` | Mode 7: subfolder inside `_EXPORT` tree |

### Behavior

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-Workers` | int | `8` | Files processed at once (1-64) |
| `-CapWorkers` | int | `0` | Clamp parallel workers to this value (0 = no cap). Independent of SafeMode |
| `-WorkerMemoryFraction` | double | `0.8` | Cap parallel workers so their ImageMagick processes fit in this share of the memory still free (0 = no cap) |
| `-DryRun` | switch | off | Show what would be compressed, don't write anything |
| `-SafeMode` | bool | `$true` | Skip multi-page TIFFs (scanner IR, Photoshop layers). Does NOT cap workers |
| `-SkipLzwAsCompressed` | bool | `$false` | Treat LZW as already compressed (skip re-compression) |
| `-Overwrite` | switch | off | Overwrite existing ZIP files |
| `-DeleteSource` | switch | off | Delete source TIFF after successful compression (mode 8 only) |
| `-ForceParallel` | switch | off | Does nothing: parallel is the default. Still accepted, for old command lines and presets |
| `-ForceSequential` | switch | off | One file at a time |
| `-StagingDir` | string | `""` | SSD staging folder for faster I/O. Files moved to final destination after each group |
| `-DuplicateAction` | string | `"Numbered"` | Mode 2 collision handling: `Skip`, `Numbered` (v2, v3...), or `Overwrite` |
| `-RestoreStranded` | switch | off | Move originals stranded in `OLD_TIFFs/` (nothing left in their place, see [Stranded originals](#stranded-originals)) back where they came from, before this run's discovery -- so this run compresses them. Never over an existing file; a dry run only says what it would restore |
| `-ExcludeFolders` | string | `""` | `';'`-separated folder **names** skipped during discovery, in every mode. Segment match (case-insensitive), evaluated **relative to `-InputDir`**: `_EXPORT` skips any `<root>\...\_EXPORT\...` tree but never `My_EXPORT_photos`, and never a folder *above* the root -- including the root's own name, so pointing the tool at `...\_EXPORT` to process it still works. Entries containing `\` or `/` are rejected -- names only, no paths. Every run logs `Excluded N file(s) under: ...` so an exclusion is never invisible |

> **Performance note:** the compression probe (Deflate or not?) reads only the IFD0 header tag, but it used to spawn one exiftool process per file. Probing is now batched (~400 files per exiftool invocation, logged as `Compression probe: N/M file(s) in Xms`); files a batch could not answer fall back to the per-file probe, so decisions are identical, just faster.

### Thumbnail Generation (v1.2+)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-GenerateThumbnail` | switch | off | Embed thumbnail as additional page in TIFF |
| `-ThumbSize` | int | `256` | Thumbnail size in pixels (max width/height) |
| `-ThumbQuality` | string | `"85"` | JPEG quality for thumbnail (JPEG only) |
| `-ThumbPage` | int | `1` | Output page position for the embedded thumbnail. Must be **>= 1** -- `0` (thumbnail first) is refused at startup, see below |
| `-ThumbFormat` | string | `"jpg"` | Thumbnail format: `jpg`, `png`, or `tif` |
| `-SkipCompressedWithThumb` | switch | off | Skip TIFFs that are already compressed AND have thumbnail |
| `-SrgbProfile` | string | `""` | ICC profile used to convert wide-gamut sources before the profile is stripped. `""` auto-detects the Windows sRGB ICC; pass a path to `.icc` to override |

**Memory and `-Workers`.** A `magick compare` (the in-place pixel gate) holds ~68 bytes per
pixel -- measured at **5.35 GiB on an 84 MP scan**, while a plain recompress/decode peaks at
~12.7 B/px. ImageMagick's own limit is 32 GiB per process, so it does not protect the machine.
Before the first worker starts, the run reads the largest image of the batch and logs
`Memory: ~N GB per worker | N GB available | workers N`; if that needs fewer workers than you
asked for, it logs `Workers reduced from N to M` and runs at M. `-CapWorkers` still applies
(logged once per run as `Workers capped to N (CapWorkers limit)`) --
the effective count is the **smaller** of `-Workers`, `-CapWorkers` and the memory cap. A file
whose size cannot be read is counted as 150 MP, so an unreadable file can only make the cap
stricter; if the available memory cannot be read at all, the count is left alone.

**Example:**
```powershell
# Compress with embedded 512px thumbnail
python compress_tiff_zip.py -Mode 9 -GenerateThumbnail -ThumbSize 512 -InputDir F:\Photos
```

**Thumbnail format:**
- Page 0: Original image (compressed with ZIP/Deflate), keeping the `SubfileType` marker the source carried
- Page 1: Thumbnail (sRGB, ICC stripped, aspect ratio preserved), marked `subfiletype=1` (reduced resolution flag)
- The main image is always read from page 0, and the thumbnail is always appended after it.

**Thumbnails are colour-managed.** Wide-gamut sources (ProPhoto, AdobeRGB) are converted *through*
their embedded ICC profile before the profile is stripped, so a Capture One ProPhoto export produces
a thumbnail that actually looks like the photo. The sRGB profile is auto-detected on Windows; if
none is found the run logs a single `WARN` and the thumbnails come out with shifted colours --
pass `-SrgbProfile <path.icc>` to fix it.

**`-GenerateThumbnail` never drops pages.** It rebuilds the file from page 0 plus a new thumbnail,
so it replaces an existing embedded preview but **refuses** any file whose extra pages are real
(scanner IR, Photoshop layers) instead of discarding them.

**`-ThumbPage 0` is refused.** Every page classifier in this toolkit -- SafeMode, the
thumbnail-safety gate, the Copy EXIF backend and the wizard's own classifier -- treats page 0 as the
main image and measures the other pages against it. A thumbnail-first file therefore has a
*full-size* page 1, which reads as a real extra page: the file came back `MULTI` and was
skipped by every later run, `-GenerateThumbnail` included. The flag now exits 1 with an
explanation instead of writing such a file.

**Already-compressed files are skipped before the thumbnail step.** `-GenerateThumbnail`
alone on a library that is already Deflate produces nothing, because the "already
compressed" check runs first; the skip line says so (`[no thumbnail added: pass
-SkipCompressedWithThumb]`). Add `-SkipCompressedWithThumb` to reprocess compressed files
that have no thumbnail yet.

---

## Modes 0-9

| Mode | Name | Input | Output location | Example |
|------|------|-------|-----------------|---------|
| `0` | In-place | Directory (non-recursive) | In-place. Originals → `OLD_TIFFs/` | `photo.tif` → compressed in place |
| `1` | Subfolder | Directory (non-recursive) | `ZIP/` subfolder next to each TIFF | `photo.tif` → `ZIP/photo.tif` |
| `2` | Flat | Directory (recursive) | Single output folder (flat) | `in/photo.tif` → `out/photo.tif` |
| `3` | Recursive subfolders | Directory (recursive) | `ZIP/` inside each folder | `in/folder/photo.tif` → `in/folder/ZIP/photo.tif` |
| `4` | Folder rename | Directory (recursive) | Parent folder renamed `_TIFF` → `_ZIP` | `photo_TIFF/` → `photo_ZIP/` |
| `5` | Sibling folder | Directory (recursive) | `TIFF/ZIP/` → grandparent `ZIP/` | `in/2024/photo.tif` → `in/ZIP/photo.tif` |
| `6` | Export marker | Directory (recursive) | ONLY inside `_EXPORT` trees | `_EXPORT/photo.tif` → `_EXPORT/ZIP/photo.tif` |
| `7` | Export marker subfolder | Directory (recursive) | Only inside `_EXPORT/TIFF` trees | `_EXPORT/TIFF/photo.tif` → `_EXPORT/ZIP/photo.tif` |
| `8` | In-place + delete | Directory (recursive) | In-place. Originals deleted after confirm | `photo.tif` → deleted after compression |
| `9` | In-place recursive + OLD_TIFFs | Directory (recursive) | In-place. Originals → `OLD_TIFFs/` | `photo.tif` → compressed in place |

**Modes 6/7** write inside the `_EXPORT` folder each file actually came from — `in\2024\JobA\_EXPORT\photo.tif` produces `in\2024\JobA\_EXPORT\ZIP\photo.tif`. Different jobs never share an output folder.

**Mode 5** never writes above `-InputDir`: a TIFF sitting directly in the input root produces `<InputDir>\ZIP\photo.tif` instead of climbing to the drive root.

---

## Destination Collisions

Every mode maps its output to `<stem>.tif`, so two different sources can claim one destination:

- modes 4-7 flatten separate source trees into a shared folder;
- modes 0/1/3/8/9 map both `photo.tif` and `photo.tiff` onto `photo.tif`.

The later claimant is renamed `_v2`, `_v3`, ... and the rename is noted in the result line:

```
OK (Uncompressed -> ZIP) | photo.tiff [renamed to photo_v2.tif to avoid collision]
```

Mode 2 uses `-DuplicateAction` instead (`Skip`, `Numbered` (default), `Overwrite`), which also covers names that already exist on disk.

---

## SafeMode (default: ON)

Multi-page TIFFs (scanner Infrared files, Photoshop layer stacks) have proprietary IFD structures that can be corrupted by re-compression. SafeMode detects these via `magick identify` with a `-MagickTimeout` second timeout (default 30) and skips them.

In modes 0 and 9 this check runs **before** the original is moved to `OLD_TIFFs/`, so a skipped multi-page TIFF stays exactly where it was:

```
MULTI (2 pages - skipped, original untouched) | scan_ir.tif
```

A TIFF with extra pages is treated as "single-page with thumbnail(s)" when every extra page is either:

- tagged `REDUCEDIMAGE` / `REDUCED`, **or**
- **strictly smaller than the main image** -- a thumbnail is a thumbnail even when the tag lies. ImageMagick rewrites multi-page files and stamps `PAGE` on every page, so a previously recompressed Capture One file (small thumbnail marked `PAGE`) would otherwise be skipped as genuinely multi-page.

If any extra page is full-size without a whitelisted tag (scanner IR, Photoshop layer, real second photo), SafeMode marks the file as multi-page and skips it. This is **fail-closed** behavior: when in doubt, the file is not touched.

> **Note:** `copy_exif_to_tiff.py` applies the same rule under its own `-SafeMode`: only `REDUCEDIMAGE`/`REDUCED` (plus the size rule) make an extra page a thumbnail. A `MASK` or `PAGE` page is a real page there too (scanner RGB+IR files).

```
SKIP Multi-page TIFFs: SafeMode=$true
COMPRESS all TIFFs: SafeMode=$false
```

To force compression of all TIFFs (including multi-page):
```powershell
python compress_tiff_zip.py -Mode 0 -InputDir . -SafeMode:false
```

---

## OLD_TIFFs Protection (all modes)

Modes 0 and 9 move originals to an `OLD_TIFFs/` subfolder. **Every mode then skips any TIFF inside an
`OLD_TIFFs` folder** -- the same relative rule `-ExcludeFolders` uses, so pointing `-InputDir`
straight at an `OLD_TIFFs` folder still processes it. This protects originals from previous
conversions and prevents re-processing.

Before round 15 only modes 0 and 9 applied the filter, so a mode 2/3/5 run over a tree already
compressed with mode 0/9 walked into the backups: the real file was skipped as already-Deflate,
which left the *backup* to claim the output name and produced a folder of `_v2` copies of backups.
One exclusion list now covers every mode.

Staged and temporary files left behind by an interrupted run are skipped the same way, and both
counts are logged.

### When the original moves (modes 0/9, since round 26)

The original stays in its place until its compressed copy exists: the copy is written to a
temporary file next to it (or to `-StagingDir`), checked to decode, brought next to its
destination, and only then is the original moved into `OLD_TIFFs/` and the copy renamed into
its place. The original is also checked to be unchanged since the run read it (size and
date); an edit saved meanwhile wins -- the copy is dropped and nothing is archived. A copy that
fails to decode, or any error, never moves the original.

On Ctrl+C the run also removes the temporary copies (`<name>.<8 hex>_<12 hex>.tif`) it had
written next to the photos.

Before round 26 these modes moved **every** original of the run to `OLD_TIFFs/` while planning,
before compressing a single file, so for the whole run their places were empty.

A file staged on **another drive** (`-StagingDir`, every mode) reaches its destination as a copy
to a temporary file beside it, then a rename -- never a copy written straight over the
destination. In mode 8 that destination is the original: a run killed mid-copy could leave a
truncated file where the only copy of the image had been.

### Stranded originals

Since round 26 the only moment an original is out of its place is between two renames on the
same drive. Older versions (and a kill in that instant) could leave originals in
`OLD_TIFFs/` with **nothing in their place**: the backup is then the only copy of the image.

Every run, in every mode, looks at the `OLD_TIFFs` folders it would walk past and logs each such
file, plus a count at the start and again under `Done:`:

```
WARN | STRANDED (original in OLD_TIFFs, nothing in its place) | ...\OLD_TIFFs\DSC_0001.tif
WARN | STRANDED: 1 original(s) in OLD_TIFFs with nothing in their place (an interrupted run?). Run again with -RestoreStranded ...
```

"Its place" is the folder above `OLD_TIFFs`: the same name, the other TIFF extension (a `.tiff`
source is written as `.tif`), and the base name of a timestamped backup
(`DSC_0001_20260803_081300.tif`, made when `DSC_0001.tif` was already in `OLD_TIFFs`) -- only
when `DSC_0001.tif` is still in `OLD_TIFFs`; otherwise the stamp is part of the photo's own name
(`Scan_20190815_143000.tif`, a phone's `IMG_20190815_143000.tif`) and it is restored under that
name (round 28). Mode 0
looks only at `<InputDir>\OLD_TIFFs`; the other modes at every `OLD_TIFFs` below the root.
`-ExcludeFolders` applies, and an `OLD_TIFFs` inside another one is never reported.

`-RestoreStranded` moves each one back, never over an existing file. Two backups claiming the
same place (an original and its timestamped namesake) are both left alone: which one is the
original cannot be told from the names. **Until they are restored, do not delete `OLD_TIFFs` or
move it to another drive** -- the wizard's *Delete OLD_TIFFs* already keeps a backup whose parent
file is missing, and `tools/move_old_backups.py` refuses to move one (round 25).

---

exiftool checks current compression. Files already using Deflate/ZIP/Adobe are skipped unless `-SkipLzwAsCompressed:$true` is set (LZW still gets re-compressed to ZIP by default).

```
SKIP (Deflate)   -- already compressed
SKIP (ZIP)       -- already compressed
SKIP (LZW)       -- already compressed (with SkipLzwAsCompressed)
OK (Uncompressed) -- compressed
OK (LZW)         -- re-compressed to ZIP
```

---

## Staging Mode

Use a fast SSD as staging to speed up processing:
```powershell
-StagingDir E:\staging
```

Files are written to staging with UUID names, then moved to final destination after each group. The staging folder is cleaned up on interrupt.

---

## Delete Source (Mode 8)

Mode 8 deletes the original TIFF after successful ZIP compression. **Mode 8 always requires a staging directory** to avoid overwriting the original before verification. If `-StagingDir` is not provided, a temporary `_cs8_<8 hex>` folder is used -- inside the input folder when there is one input root (same volume, so the final move is a rename), else under `%TEMP%` -- and you are prompted to confirm.

The staged ZIP is **always verified** with `magick <file> null:` (full pixel decode) and a pixel compare before the move — including the `[no thumb]` fallback. A staged ZIP whose metadata restore failed never replaces the original: `ERROR (metadata restore failed - source preserved)` (round 28; it used to be a `WARN` with the file without EXIF moved over the original). If the integrity check fails, times out, or cannot be determined, the staged file is discarded, an error is logged (exit code 1), and the original is preserved.

Deletion only happens if:
1. ZIP file was created successfully in staging
2. `magick file null:` verifies ZIP integrity
3. File was moved successfully to the final destination
4. Source file still exists

If any check fails, the source is preserved.

---

## Parallel and Sequential Runs

Every run works on several files at once (`-Workers`), on any machine. The per-file work is the
same code either way; only how many files are in flight changes.

- `-ForceSequential`: one file at a time.
- `-ForceParallel`: does nothing (parallel is the default). It is still accepted, so old command
  lines and presets keep working.

A mode 0-9 run prints which one it is at start-up: `Python 3.13.1 (PARALLEL)`, or
`Python 3.13.1 (SEQUENTIAL) [FORCED via -ForceSequential]`.

`magick` calls that need a timeout (page count, ZIP integrity, pixel compare) are killed when the
timeout passes, and a timeout is a failed check, never a pass.

`-Workers` accepts 1-64. A value outside that range is refused before the run starts (exit code 1).

---

## Edit history in the file (`xmpMM:History`)

Every TIFF this script **rewrites** gets one entry appended to its `xmpMM:History`, in the
format of the wizard's padded 16 -> 8-bit conversion and of jxl-photo:

```
Action=converted
When=2026-11-10T22:04:16+09:00
SoftwareAgent=tiff-workflow 3.0.0 (https://github.com/rsilvabr/tiff-workflow)
Parameters=compress_tiff_zip mode 1: ZIP (Deflate)          (legacy mode: "legacy"; with a
                                                             thumbnail: "... + thumbnail page")
```

- Earlier entries (Capture One, scanner software) are kept, never doubled: the entry is its
  own exiftool call, after the tag copy.
- It is written to the new file before the page-count, integrity and pixel checks, so those
  check the final bytes; the original still leaves its place only after every check passed.
- Only a file this run wrote: skipped files, multi-page files left alone and dry runs get none,
  and a second run over the output adds nothing (it skips the already-ZIP file).
- Always on (no switch). Cost on real files: 0.5-0.9 s per file, 2-15 % of the compression.
- If the entry cannot be written the file is still a good ZIP: the line is a `WARN` ending
  in `[history not written]`, never an `ERROR`.

---

## Logging

Logs written to `Logs/compress_tiff_zip/<timestamp>.log` in the current working directory. Each run appends to a new timestamped file. The first line of every backend log names the release, the script and the Python version: `tiff-workflow 3.0.0 | compress_tiff_zip.py | Python 3.13.1`.

Sample output:
```
10:23:45 | INFO | tiff-workflow 3.0.0 | compress_tiff_zip.py | Python 3.13.1
10:23:45 | INFO | Log: Logs/compress_tiff_zip/20240415_102345.log
10:23:45 | INFO | Mode: 3 | Workers: 8 | OutputDir: (in-place/flat) | Staging: disabled | DryRun: False | SafeMode: True | SkipLzw: False | Overwrite: False | DeleteSource: OFF
10:23:45 | INFO | Found: 150 TIFF(s)
10:23:45 | INFO | -- Group: E:\photos\session1 (25 file(s))
10:23:46 | INFO | [1/150] OK (Uncompressed -> ZIP) | DSC_0001.tif
...
10:24:12 | INFO | Done: 142 OK | 5 skipped | 3 multi-page (not touched) | 0 warnings | 0 errors | 150/150 processed
```

---

## Multi-Page TIFF Report

When SafeMode skips multi-page TIFFs, they are listed at the end:
```
-- Multi-page TIFFs found (not compressed - review manually):
   E:\photos\session2\scan_ir.tif
   E:\photos\session2\layered.tif
```

---

## Requirements

```
ImageMagick 7    https://imagemagick.org/script/download.php
exiftool         https://exiftool.org
Python 3.9+      https://www.python.org
```

Both `magick.exe` and `exiftool.exe` must be on your PATH. The script uses only Python's
standard library; `tiff_workflow_common.py` must be in the same folder.

### Verify
```powershell
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx
python --version    # Python 3.9+
```
