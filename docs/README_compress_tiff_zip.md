# compress (`tiff_workflow/compress_tiff_zip.py`) -- ZIP Compression Backend

Python compression backend with modes 0-9 CLI flags. Called by `convert_tiff.py` wizard or used directly.

**Requires:** Python 3.9+ (standard library only), ImageMagick 7, exiftool. It runs as `python convert_tiff.py compress ...`, from a copy of the whole toolkit (`convert_tiff.py` and the folder `tiff_workflow/`).

Until v2.9.1 this backend was `compress_tiff_zip.ps1`; that edition is described in [powershell_edition.md](powershell_edition.md). v3.0.0 kept the `.ps1` command line; v3.1 writes it the usual Python way (`--mode 9 --no-safe-mode`), and `--mode` is required (the legacy mode of a run without it is gone). [command_line.md](command_line.md) translates every option of v3.0.0.

---

## CLI Reference

```powershell
# In-place, non-recursive (mode 0)
python convert_tiff.py compress --mode 0 --input-dir .

# Recursive, in-place with delete (mode 8)
python convert_tiff.py compress --mode 8 --input-dir F:\Photos --delete-source

# Subfolder per folder (mode 1)
python convert_tiff.py compress --mode 1 --input-dir F:\Photos --zip-subfolder-name "ZIP"

# Flat to output folder (mode 2)
python convert_tiff.py compress --mode 2 --input-dir F:\Photos --output-dir E:\all_zips

# Folder rename (mode 4)
python convert_tiff.py compress --mode 4 --input-dir F:\Photos --zip-suffix "_ZIP"

# With staging and workers
python convert_tiff.py compress --mode 3 --input-dir F:\Photos --staging-dir E:\staging --workers 12

# Dry-run
python convert_tiff.py compress --mode 3 --input-dir F:\Photos --dry-run
```

An option is written in full, in lower case, its value after it: `--workers 8`. A yes/no is
`--safe-mode` / `--no-safe-mode`; a value that starts with `-` is written `--name=value`. The
same line runs as `python convert_tiff.py compress ...`, and `--help` lists every option. A
command line that cannot run as written (an unknown or abbreviated name, a value out of range,
a missing `--mode`, a line of v3.0.0) ends the run with **exit code 2** before anything is
touched; 1 is a run in which something failed.

---

## Parameters

The defaults below are the `PARAMS` table at the top of `tiff_workflow/compress_tiff_zip.py`.

### Core

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--mode` | int | (required) | **0-9**, see the mode table. There is no default: without `--mode` the script refuses to run (exit code 2), and so does a value outside 0-9. Until v3.0.0 a run without `--mode` was the "legacy mode" (the current folder and its subfolders, compressed in place, no backup). Mode 8 does that (`--mode 8 --input-dir .`; a `.tiff` gets its `.tif` written next to it and is only removed with `--delete-source`); mode 9 does it and keeps the originals in `OLD_TIFFs/` (mode 0: the same for one folder, without its subfolders). Not mode 2: its output is flat, so the files of subfolders would be copied into the root |
| `--input-dir` | string | `.` | Input folder. Can be relative or absolute |
| `--input-dir-file` | string | `""` | UTF-8 text file with one input folder per line, used instead of `--input-dir` (not together). The wizard switches to it automatically when the folder list would exceed the Windows command-line limit. |

### Output path control

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--output-dir` | string | `""` | Mode 2: flat output root. `""` = same as input dir |
| `--zip-suffix` | string | `_ZIP` | Mode 4: folder rename suffix. `TIFF` in folder name replaced with this |
| `--zip-subfolder-name` | string | `ZIP` | Mode 1/3: subfolder name inside each folder |
| `--export-marker` | string | `_EXPORT` | Modes 6/7: folder name that marks export root |
| `--export-zip-subfolder` | string | `ZIP` | Mode 7: subfolder inside `_EXPORT` tree |

### Behavior

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--workers` | int | `8` | Files processed at once (1-64) |
| `--cap-workers` | int | `0` | Clamp parallel workers to this value (0 = no cap). Independent of safe mode |
| `--worker-memory-fraction` | double | `0.8` | Cap parallel workers so their ImageMagick processes fit in this share of the memory still free (0 = no cap) |
| `--dry-run` | switch | off | Show what would be compressed, don't write anything |
| `--safe-mode` / `--no-safe-mode` | yes/no | on | Skip multi-page TIFFs (scanner IR, Photoshop layers). Does NOT cap workers |
| `--skip-lzw-as-compressed` / `--no-skip-lzw-as-compressed` | yes/no | off | Treat LZW as already compressed (skip re-compression) |
| `--recompress-zip` | switch | off | Also rewrite TIFFs that are already ZIP **without a predictor** (Capture One's exports), keeping a file only when it comes out smaller. See [Recompressing ZIP TIFFs](#recompressing-zip-tiffs-without-a-predictor---recompress-zip) |
| `--recompress-min-saving` | number | `1` | `--recompress-zip` keeps a rewritten file only when it is at least this many percent smaller (0-50). Settings file: `recompress_min_saving` |
| `--zip-predictor` | `on` / `auto` | `on` | `on` = always the ZIP predictor (as every version before); `auto` = a sample of each file decides with or without it, whichever is smaller -- the file is still written once. See [Choosing the predictor](#choosing-the-predictor---zip-predictor-auto). Settings file: `zip_predictor` |
| `--overwrite` | switch | off | Overwrite existing ZIP files |
| `--delete-source` | switch | off | Delete source TIFF after successful compression (mode 8 only) |
| `--force-parallel` | switch | off | Does nothing: parallel is the default. Still accepted, for old command lines and presets |
| `--force-sequential` | switch | off | One file at a time |
| `--staging-dir` | string | `""` | SSD staging folder for faster I/O. Files moved to final destination after each group |
| `--duplicate-action` | string | `"Numbered"` | Mode 2 collision handling: `Skip`, `Numbered` (v2, v3...), or `Overwrite` |
| `--restore-stranded` | switch | off | Move originals stranded in `OLD_TIFFs/` (nothing left in their place, see [Stranded originals](#stranded-originals)) back where they came from, before this run's discovery -- so this run compresses them. Never over an existing file; a dry run only says what it would restore |
| `--exclude-folders` | string | `""` | `';'`-separated folder **names** skipped during discovery, in every mode. Segment match (case-insensitive), evaluated **relative to `--input-dir`**: `_EXPORT` skips any `<root>\...\_EXPORT\...` tree but never `My_EXPORT_photos`, and never a folder *above* the root -- including the root's own name, so pointing the tool at `...\_EXPORT` to process it still works. Entries containing `\` or `/` are rejected -- names only, no paths. Every run logs `Excluded N file(s) under: ...` so an exclusion is never invisible |
| `--include-folders` | string | `""` | The opposite: only the TIFFs inside a folder of one of these `';'`-separated **names** (`TIFF16`), in every mode. Whole names, case-insensitive; the root's own name counts (pointing the tool at a `TIFF16` folder takes its files) and the folders below it, never a folder *above* the root. `--exclude-folders` wins over it, and `OLD_TIFFs` stays out. Every run logs `Left out N file(s) not inside a folder named: ...` |

> **Performance note:** the compression probe (Deflate or not?) reads only the IFD0 header tag, but it used to spawn one exiftool process per file. Probing is now batched (~400 files per exiftool invocation, logged as `Compression probe: N/M file(s) in Xms`); files a batch could not answer fall back to the per-file probe, so decisions are identical, just faster.

### Thumbnail Generation (v1.2+)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--generate-thumbnail` | switch | off | Embed thumbnail as additional page in TIFF |
| `--thumb-size` | int | `256` | Thumbnail size in pixels (max width/height) |
| `--thumb-quality` | string | `"85"` | JPEG quality for thumbnail (JPEG only) |
| `--thumb-page` | int | `1` | Output page position for the embedded thumbnail. Must be **>= 1** -- `0` (thumbnail first) is refused at startup, see below |
| `--thumb-format` | string | `"jpg"` | Thumbnail format: `jpg`, `png`, or `tif` |
| `--skip-compressed-with-thumb` | switch | off | Skip TIFFs that are already compressed AND have thumbnail |
| `--srgb-profile` | string | `""` | ICC profile used to convert wide-gamut sources before the profile is stripped. `""` auto-detects the Windows sRGB ICC; pass a path to `.icc` to override |

**Memory and `--workers`.** A `magick compare` (the in-place pixel gate) holds ~68 bytes per
pixel -- measured at **5.35 GiB on an 84 MP scan**, while a plain recompress/decode peaks at
~12.7 B/px. ImageMagick's own limit is 32 GiB per process, so it does not protect the machine.
Before the first worker starts, the run reads the largest image of the batch and logs
`Memory: ~N GB per worker | N GB available | workers N`; if that needs fewer workers than you
asked for, it logs `Workers reduced from N to M` and runs at M. `--cap-workers` still applies
(logged once per run as `Workers capped to N (--cap-workers)`) --
the effective count is the **smaller** of `--workers`, `--cap-workers` and the memory cap. A file
whose size cannot be read is counted as 150 MP, so an unreadable file can only make the cap
stricter; if the available memory cannot be read at all, the count is left alone.

**Example:**
```powershell
# Compress with embedded 512px thumbnail
python convert_tiff.py compress --mode 9 --generate-thumbnail --thumb-size 512 --input-dir F:\Photos
```

**Thumbnail format:**
- Page 0: Original image (compressed with ZIP/Deflate), keeping the `SubfileType` marker the source carried
- Page 1: Thumbnail (sRGB, ICC stripped, aspect ratio preserved), marked `subfiletype=1` (reduced resolution flag)
- The main image is always read from page 0, and the thumbnail is always appended after it.
- The thumbnail is made first, then page 0 and the thumbnail are written together: the main image
  is compressed once (since v3.1.1; until then twice, through a temp TIFF -- 26 s instead of 13
  for a 45 MP scan).

**Thumbnails are colour-managed.** Wide-gamut sources (ProPhoto, AdobeRGB) are converted *through*
their embedded ICC profile before the profile is stripped, so a Capture One ProPhoto export produces
a thumbnail that actually looks like the photo. The sRGB profile is auto-detected on Windows; if
none is found the run logs a single `WARN` and the thumbnails come out with shifted colours --
pass `--srgb-profile <path.icc>` to fix it.

**`--generate-thumbnail` never drops pages.** It rebuilds the file from page 0 plus a new thumbnail,
so it replaces an existing embedded preview but **refuses** any file whose extra pages are real
(scanner IR, Photoshop layers) instead of discarding them.

**`--thumb-page 0` is refused.** Every page classifier in this toolkit -- safe mode, the
thumbnail-safety gate, the Copy EXIF backend and the wizard's own classifier -- treats page 0 as the
main image and measures the other pages against it. A thumbnail-first file therefore has a
*full-size* page 1, which reads as a real extra page: the file came back `MULTI` and was
skipped by every later run, `--generate-thumbnail` included. The flag now exits 2 (a usage error) with an
explanation instead of writing such a file.

**Already-compressed files are skipped before the thumbnail step.** `--generate-thumbnail`
alone on a library that is already Deflate produces nothing, because the "already
compressed" check runs first; the skip line says so (`[no thumbnail added: pass
--skip-compressed-with-thumb]`). Add `--skip-compressed-with-thumb` to reprocess compressed files
that have no thumbnail yet.

---

## Modes 0-9

| Mode | Name | Input | Output location | Example |
|------|------|-------|-----------------|---------|
| `0` | In-place | Directory (non-recursive) | In-place. Originals → `OLD_TIFFs/` | `photo.tif` → compressed in place |
| `1` | Subfolder | Directory (non-recursive) | `ZIP/` subfolder next to each TIFF | `photo.tif` → `ZIP/photo.tif` |
| `2` | Flat | Directory (recursive) | Single output folder (flat) | `in/photo.tif` → `out/photo.tif` |
| `3` | Recursive subfolders | Directory (recursive) | `ZIP/` inside each folder | `in/folder/photo.tif` → `in/folder/ZIP/photo.tif` |
| `4` | Sibling `_ZIP` folder | Directory (recursive) | Sibling folder named `<parent>_ZIP` (the source folder is NOT renamed) | `photo_TIFF/a.tif` → `photo_ZIP/a.tif` |
| `5` | Sibling folder | Directory (recursive) | `TIFF/ZIP/` → grandparent `ZIP/` | `in/2024/photo.tif` → `in/ZIP/photo.tif` |
| `6` | Export marker | Directory (recursive) | ONLY inside `_EXPORT` trees | `_EXPORT/photo.tif` → `_EXPORT/ZIP/photo.tif` |
| `7` | Export marker subfolder | Directory (recursive) | Only inside `_EXPORT/TIFF` trees | `_EXPORT/TIFF/photo.tif` → `_EXPORT/ZIP/photo.tif` |
| `8` | In-place + delete | Directory (recursive) | In-place, always: the verified output replaces each original. `--delete-source` additionally removes a `.tiff` source | `photo.tif` → replaced; `photo.tiff` → `photo.tif`, source deleted |
| `9` | In-place recursive + OLD_TIFFs | Directory (recursive) | In-place. Originals → `OLD_TIFFs/` | `photo.tif` → compressed in place |

**Modes 6/7** write inside the `_EXPORT` folder each file actually came from — `in\2024\JobA\_EXPORT\photo.tif` produces `in\2024\JobA\_EXPORT\ZIP\photo.tif`. Different jobs never share an output folder.

**Mode 5** never writes above `--input-dir`: a TIFF sitting directly in the input root produces `<input folder>\ZIP\photo.tif` instead of climbing to the drive root.

---

## Destination Collisions

Every mode maps its output to `<stem>.tif`, so two different sources can claim one destination:

- modes 4-7 flatten separate source trees into a shared folder;
- modes 0/1/3/8/9 map both `photo.tif` and `photo.tiff` onto `photo.tif`.

The later claimant is renamed `_v2`, `_v3`, ... and the rename is noted in the result line:

```
OK (Uncompressed -> ZIP) | photo.tiff [renamed to photo_v2.tif to avoid collision]
```

Mode 2 uses `--duplicate-action` instead (`Skip`, `Numbered` (default), `Overwrite`), which also covers names that already exist on disk.

---

## Safe mode (`--safe-mode`, default: on)

Multi-page TIFFs (scanner Infrared files, Photoshop layer stacks) have proprietary IFD structures that can be corrupted by re-compression. Safe mode detects these via `magick identify` with a `--magick-timeout` second timeout (default 30) and skips them.

In modes 0 and 9 this check runs **before** the original is moved to `OLD_TIFFs/`, so a skipped multi-page TIFF stays exactly where it was:

```
MULTI (2 pages - skipped, original untouched) | scan_ir.tif
```

A TIFF with extra pages is treated as "single-page with thumbnail(s)" when every extra page is either:

- tagged `REDUCEDIMAGE` / `REDUCED`, **or**
- **strictly smaller than the main image** -- a thumbnail is a thumbnail even when the tag lies. ImageMagick rewrites multi-page files and stamps `PAGE` on every page, so a previously recompressed Capture One file (small thumbnail marked `PAGE`) would otherwise be skipped as genuinely multi-page.

If any extra page is full-size without a whitelisted tag (scanner IR, Photoshop layer, real second photo), safe mode marks the file as multi-page and skips it. This is **fail-closed** behavior: when in doubt, the file is not touched.

> **Note:** `copy_exif_to_tiff.py` applies the same rule under its own `--safe-mode`: only `REDUCEDIMAGE`/`REDUCED` (plus the size rule) make an extra page a thumbnail. A `MASK` or `PAGE` page is a real page there too (scanner RGB+IR files).

```
SKIP Multi-page TIFFs: --safe-mode (the default)
COMPRESS all TIFFs: --no-safe-mode
```

To force compression of all TIFFs (including multi-page):
```powershell
python convert_tiff.py compress --mode 0 --input-dir . --no-safe-mode
```

---

## OLD_TIFFs Protection (all modes)

Modes 0 and 9 move originals to an `OLD_TIFFs/` subfolder. **Every mode then skips any TIFF inside an
`OLD_TIFFs` folder** -- the same relative rule `--exclude-folders` uses, so pointing `--input-dir`
straight at an `OLD_TIFFs` folder still processes it. This protects originals from previous
conversions and prevents re-processing.

`OLD_PADDED` folders are skipped the same way since v3.1.1: there the wizard's Diagnose
workflow keeps the 16-bit originals of the padded files it converted to 8-bit. Before, a
recursive mode compressed those originals in place (mode 8) or wrote its output into the
folder (`OLD_PADDED/OLD_TIFFs/`, `OLD_PADDED/ZIP/`; audit round 31).

Before round 15 only modes 0 and 9 applied the filter, so a mode 2/3/5 run over a tree already
compressed with mode 0/9 walked into the backups: the real file was skipped as already-Deflate,
which left the *backup* to claim the output name and produced a folder of `_v2` copies of backups.
One exclusion list now covers every mode.

Staged and temporary files left behind by an interrupted run are skipped the same way, and both
counts are logged.

### When the original moves (modes 0/9, since round 26)

The original stays in its place until its compressed copy exists: the copy is written to a
temporary file next to it (or to `--staging-dir`), checked to decode, brought next to its
destination, and only then is the original moved into `OLD_TIFFs/` and the copy renamed into
its place. The original is also checked to be unchanged since the run read it (size and
date); an edit saved meanwhile wins -- the copy is dropped and nothing is archived. A copy that
fails to decode, or any error, never moves the original.

On Ctrl+C the run also removes the temporary copies (`<name>.<8 hex>_<12 hex>.tif`) it had
written next to the photos.

Before round 26 these modes moved **every** original of the run to `OLD_TIFFs/` while planning,
before compressing a single file, so for the whole run their places were empty.

A file staged on **another drive** (`--staging-dir`, every mode) reaches its destination as a copy
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
WARN | STRANDED: 1 original(s) in OLD_TIFFs with nothing in their place (an interrupted run?). Run again with --restore-stranded ...
```

"Its place" is the folder above `OLD_TIFFs`: the same name, the other TIFF extension (a `.tiff`
source is written as `.tif`), and the base name of a timestamped backup
(`DSC_0001_20260803_081300.tif`, made when `DSC_0001.tif` was already in `OLD_TIFFs`) -- only
when `DSC_0001.tif` is still in `OLD_TIFFs`; otherwise the stamp is part of the photo's own name
(`Scan_20190815_143000.tif`, a phone's `IMG_20190815_143000.tif`) and it is restored under that
name (round 28). Mode 0
looks only at `<input folder>\OLD_TIFFs`; the other modes at every `OLD_TIFFs` below the root.
`--exclude-folders` applies, and an `OLD_TIFFs` inside another one is never reported.

`--restore-stranded` moves each one back, never over an existing file. Two backups claiming the
same place (an original and its timestamped namesake) are both left alone: which one is the
original cannot be told from the names. **Until they are restored, do not delete `OLD_TIFFs` or
move it to another drive** -- the wizard's *Delete OLD_TIFFs* already keeps a backup whose parent
file is missing, and `tools/move_old_backups.py` refuses to move one (round 25).

---

exiftool checks current compression. Files already using Deflate/ZIP/Adobe are skipped unless `--skip-lzw-as-compressed` is set (LZW still gets re-compressed to ZIP by default).

```
SKIP (Deflate)   -- already compressed
SKIP (ZIP)       -- already compressed
SKIP (LZW)       -- already compressed (with --skip-lzw-as-compressed)
OK (Uncompressed) -- compressed
OK (LZW)         -- re-compressed to ZIP
```

---

## Staging Mode

Use a fast SSD as staging to speed up processing:
```powershell
--staging-dir E:\staging
```

Files are written to staging with UUID names, then moved to final destination after each group. The staging folder is cleaned up on interrupt.

---

## Delete Source (Mode 8)

Mode 8 deletes the original TIFF after successful ZIP compression. **Mode 8 always requires a staging directory** to avoid overwriting the original before verification. If `--staging-dir` is not provided, a temporary `_cs8_<8 hex>` folder is used -- inside the input folder when there is one input root (same volume, so the final move is a rename), else under `%TEMP%` -- and you are prompted to confirm.

The staged ZIP is **always verified** with `magick <file> null:` (full pixel decode) and a pixel compare before the move — including the `[no thumb]` fallback. A staged ZIP whose metadata restore failed never replaces the original: `ERROR (metadata restore failed - source preserved)` (round 28; it used to be a `WARN` with the file without EXIF moved over the original). If the integrity check fails, times out, or cannot be determined, the staged file is discarded, an error is logged (exit code 1), and the original is preserved.

Deletion only happens if:
1. ZIP file was created successfully in staging
2. `magick file null:` verifies ZIP integrity
3. File was moved successfully to the final destination
4. Source file still exists

If any check fails, the source is preserved.

---

## Recompressing ZIP TIFFs without a predictor (`--recompress-zip`)

Capture One writes its TIFF exports with ZIP (Deflate) at zlib level 6 and **no predictor**
(measured on real exports, 15/11/2026: the compressed blocks are byte for byte what zlib level 6
writes). This script writes ZIP with ImageMagick, which adds the horizontal predictor for 8- and
16-bit images: it stores the difference between neighbouring pixels instead of their values, and
Deflate packs those much better. The pixels are the same, bit for bit.

Measured on copies of 30 real 16-bit Capture One exports (12-261 MB, 2024-2026, 15/11/2026): 24
came out between 2.7 % and 28.1 % smaller (median 10.7 %), 6 would not shrink and stayed as they
were. A few of them:

| before | after |
|---|---|
| 234.8 MB (Nikon Z8) | 209.6 MB (-10.7 %) |
| 239.3 MB | 172.1 MB (-28.1 %) |
| 190.0 MB | 140.7 MB (-25.9 %) |
| 28.9 MB | 23.9 MB (-17.3 %) |
| 154.7 MB | 160.9 MB (**+4.0 %**: kept as it was) |
| 11.9 MB (Nikon D700) | not even written: its sample was 24.4 % smaller without the predictor |

So the gain is real but not the same for every image, and on some the predictor makes the file
**larger**. `--recompress-zip` therefore:

- takes a file that is already compressed only when page 0 is Deflate **without** a predictor, 8
  or 16 bits per sample, gray or RGB. A file with a predictor (everything this script wrote), LZW,
  32-bit or palette stays the `SKIP` it always was. So does a file it cannot rewrite safely --
  an RGBA scan whose associated alpha is not opaque, a layout it cannot read: `SKIP (Adobe
  Deflate: not rewritten - associated alpha that is not opaque)`, the file exactly as it was;
- sends it through the normal path of the mode: the same safe-mode check, the same metadata
  copy, the same gates (in mode 8 every pixel is compared before the swap), the same landing;
- first writes a small SAMPLE of the image both ways (see [Choosing the
  predictor](#choosing-the-predictor---zip-predictor-auto)): when it is clearly smaller without
  the predictor, nothing is written at all -- `SKIP (no gain expected: sample -23.2% without it -
  original kept)` -- and the file is remembered;
- keeps the new file only when it is at least `--recompress-min-saving` percent smaller (default
  1). Otherwise the new file is dropped before any pixel check and the line says
  `SKIP (no gain: 18.0 MB -> 25.1 MB, +39.4% - original kept)`: the original is not touched;
- **remembers** the files that did not shrink (path, size and modification time) in
  `.tiff_workflow_no_gain.json` in your user profile, so the next run skips them without reading
  them: `SKIP (no gain on 2026-11-15 - not tried again)`. A file that changed is tried again;
  deleting the memory file forgets everything (the environment variable `TIFFWORKFLOW_NO_GAIN`
  names another file, set but empty = no memory). A dry run reads it and never writes it.

A kept file says `OK (Deflate -> ZIP, recompressed: 234.8 MB -> 209.6 MB, -10.7%)`, its
`xmpMM:History` entry ends in `- recompressed with predictor - zipcheck 1 predictor 2`, and the
run ends with `Recompressed: N file(s), X -> Y (Z saved, -P%)`. Running it again over its own
output changes nothing: the files now have a predictor. A file `--zip-predictor auto` wrote
WITHOUT the predictor (its History ends in `zipcheck 1 predictor 1`) is skipped as
`SKIP (Adobe Deflate: already the smaller - zipcheck)`, on any machine and after any rename.

Example -- a preset over every `TIFF16` folder of three years of exports, replaced in place, the
staging on a fast drive:

```
python convert_tiff.py compress --mode 8 --input-dir "G:\2024;G:\2025;G:\2026" --include-folders TIFF16 --recompress-zip --staging-dir E:\temp_tiff --workers 8 --safe-mode --no-skip-lzw-as-compressed --magick-timeout 180
```

A recursive mode finds the sessions added since the last run on its own. Things to know before
the first run over a whole library:

- the first run reads and writes every export once (about 15 s of work per 220 MB file); later
  runs only read the headers;
- the new files keep their dates (see [The file's date](#the-files-date)), but their **size**
  changes: a backup tool that compares size copies each of them again once (photo-backup also
  moves the previous copy to its versions folder);
- jxl-photo's own checksum of the TIFF a master came from (`jxlphoto-srcsum`) no longer matches
  the recompressed TIFF: its gates that delete a TIFF after proving the archive by content will
  keep it.

---

## Choosing the predictor (`--zip-predictor auto`)

The predictor is not always the better choice. On 31 varied real TIFFs (film scans from
SilverFast, Epson, VueScan and Noritsu, camera exports from Lightroom, Photoshop, Capture One),
each written whole as compress writes it, with and without the predictor (15/11/2026):

| | files | |
|---|---|---|
| the predictor wins | 27 | 3-83 % smaller with it (most scans: 10-27 %) |
| without it wins | 4 | a Luminar RGBA scan **42 %**, a Photoshop file 5.7 %, a VueScan RGB+IR scan 5.6 %, a Windows Photo file 0.8 % smaller without it |

| the 31 files | total |
|---|---|
| always with the predictor (`on`) | 2.585 GB |
| always without | 2.808 GB |
| `auto` | **2.498 GB (-3.38 %)** |
| the smaller of the two for every file (both written) | 2.497 GB (-3.39 %) |

Which way an image goes cannot be told from its type (none of those four is a padded 8-bit
file); it has to be measured.

There is a choice only where ImageMagick writes the horizontal predictor on its own: 8- and
16-bit gray, RGB and CMYK pages. A float page is written with the floating-point predictor, a
palette or a 1-bit page with none, and `auto` leaves those as `on` does (no sample, no record).

Writing the TIFF both ways and keeping the smaller would cost a full temporary TIFF per file.
`--zip-predictor auto` measures a **sample** instead: one ImageMagick reads page 0 and writes four
full-width bands of 16 rows (at 20/40/60/80 % of the height) both ways, a few MB in the temp
folder, deleted at once -- with the write's own options, so the sample has the channels the
file will have. The sample's smaller decides; a difference under 2 % keeps the predictor; the
TIFF is then written **once**, as always. On the 31 scans and the 22 Capture One exports the
sample never chose against the whole file where it mattered (the one miss: 0.8 % on a
Windows Photo file, inside the margin); `auto` lands 0.01 % from the best possible. It costs
about 0.1-1.7 s per file.

The choice is recorded in the `xmpMM:History` entry the rewrite writes anyway -- no extra write:
its Parameters end in ` - zipcheck 1 predictor 2` (with) or ` - zipcheck 1 predictor 1`
(without). `--recompress-zip` reads it back: a ZIP file without a predictor whose LAST History
entry is that one is already the smaller and is skipped. A later entry by another program (an
edit, jxl-photo's decode back to TIFF), or a predictor that does not match the file, makes the
record count for nothing. jxl-photo copies the History into the JXL with the rest of the XMP --
there it is history, and jxl-photo's own reader skips other programs' entries.

The default stays `on` (the predictor, as every version before): set `"zip_predictor": "auto"` in
the settings file, or pick it on the wizard's Advanced screen, to make it yours.

---

## The file's date

A compressed file that **takes its original's place** -- modes 0, 8 and 9, and a copy mode whose
output is its own source -- keeps the original's modification (and access) time, since v3.2.
The image did not change, and tools that decide by date must not take a lossless rewrite for a
new photo: jxl-photo's smart sync re-encodes every master whose TIFF looks newer than it, and then
every copy made from those masters. A copy written somewhere else (`ZIP/`, a flat folder) is a new
file with a date of its own. Windows' "date created" of the replaced file is the date of the run.
If the date cannot be set the file is still landed and the line ends in `[date not kept]`.

---

## Parallel and Sequential Runs

Every run works on several files at once (`--workers`), on any machine. The per-file work is the
same code either way; only how many files are in flight changes.

- `--force-sequential`: one file at a time.
- `--force-parallel`: does nothing (parallel is the default). It is still accepted, so old command
  lines and presets keep working.

A mode 0-9 run prints which one it is at start-up: `Python 3.13.1 (PARALLEL)`, or
`Python 3.13.1 (SEQUENTIAL) [FORCED via --force-sequential]`.

`magick` calls that need a timeout (page count, ZIP integrity, pixel compare) are killed when the
timeout passes, and a timeout is a failed check, never a pass.

`--workers` accepts 1-64. A value outside that range is refused before the run starts (exit code 2).

---

## Edit history in the file (`xmpMM:History`)

Every TIFF this script **rewrites** gets one entry appended to its `xmpMM:History`, in the
format of the wizard's padded 16 -> 8-bit conversion and of jxl-photo:

```
Action=converted
When=2026-11-10T22:04:16+09:00
SoftwareAgent=tiff-workflow 3.0.0 (https://github.com/rsilvabr/tiff-workflow)
Parameters=compress_tiff_zip mode 1: ZIP (Deflate)          (with a thumbnail:
                                                             "... + thumbnail page";
                                                             --recompress-zip: "... - recompressed
                                                             with predictor"; a predictor that was
                                                             CHOSEN ends in " - zipcheck 1
                                                             predictor 2" or "... predictor 1")
```

- Earlier entries (Capture One, scanner software) are kept, never doubled. The tag copy, the
  page markers and the entry are ONE exiftool command, so the new file is rewritten once (until
  v3.1.1-rc1 three times: exiftool writes the whole file for each command); the entry is
  assigned after the copy, with `=` (`+=` in that command would double the copied entries).
- It is written to the new file before the page-count, integrity and pixel checks, so those
  check the final bytes; the original still leaves its place only after every check passed.
- Only a file this run wrote: skipped files, multi-page files left alone and dry runs get none,
  and a second run over the output adds nothing (it skips the already-ZIP file).
- Always on (no switch). No cost of its own since v3.1.1: it goes in the rewrite that copies
  the metadata back (until then a rewrite of its own, 0.5-0.9 s per file).
- If the entry cannot be written the file is still a good ZIP: the line is a `WARN` ending
  in `[history not written]`, never an `ERROR`.

The rest of the metadata is copied from the original. The IPTC times (TimeCreated and the
others) are kept exactly as they were since v3.2: exiftool gives a time without a zone the zone
of the computer it runs on, so until then a Capture One export's `15:02:12` came out
`15:02:12+09:00` in Japan and `-03:00` in Brazil, wherever the photo was taken. Now a time
without a zone stays without one, and one with the camera's zone keeps it.

---

## Logging

Logs written to `Logs/compress_tiff_zip/<timestamp>.log` in the current working directory. Each run appends to a new timestamped file. The first line of every backend log names the release, the script and the Python version: `tiff-workflow 3.0.0 | compress_tiff_zip.py | Python 3.13.1`.

Sample output:
```
10:23:45 | INFO | tiff-workflow 3.1.0 | compress_tiff_zip.py | Python 3.13.1
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

When safe mode skips multi-page TIFFs, they are listed at the end:
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

Both `magick.exe` and `exiftool.exe` must be on your PATH. The backends use only Python's
standard library; keep the folder `tiff_workflow/` next to `convert_tiff.py`.

### Verify
```powershell
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx
python --version    # Python 3.9+
```
