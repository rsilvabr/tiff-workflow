# compress_tiff_zip.ps1 -- ZIP Compression Backend

PowerShell compression backend with modes 0-8 CLI flags. Called by `convert_tiff.py` wizard or used directly.

**Requires:** PowerShell 5.1 or 7, ImageMagick 7, exiftool. No Python required for direct usage.

---

## CLI Reference

```powershell
# In-place, non-recursive (mode 0)
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 0 -InputDir .

# Recursive, in-place with delete (mode 8)
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 8 -InputDir F:\Photos -DeleteSource

# Subfolder per folder (mode 1)
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 1 -InputDir F:\Photos -ZipSubfolderName "ZIP"

# Flat to output folder (mode 2)
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 2 -InputDir F:\Photos -OutputDir E:\all_zips

# Folder rename (mode 4)
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 4 -InputDir F:\Photos -ZipSuffix "_ZIP"

# With staging and workers
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 3 -InputDir F:\Photos -StagingDir E:\staging -Workers 12

# Dry-run
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 3 -InputDir F:\Photos -DryRun
```

---

## Parameters

### Core

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-Mode` | int | `-1` | **-1 = legacy mode** (no CLI params = original behavior). **0-9** = new mode behavior |
| `-InputDir` | string | `.` | Input folder. Can be relative or absolute |

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
| `-Workers` | int | `8` | Parallel jobs (PS7) / throttle limit |
| `-DryRun` | switch | off | Show what would be compressed, don't write anything |
| `-SafeMode` | bool | `$true` | Skip multi-page TIFFs (scanner IR, Photoshop layers) |
| `-SkipLzwAsCompressed` | bool | `$false` | Treat LZW as already compressed (skip re-compression) |
| `-Overwrite` | switch | off | Overwrite existing ZIP files |
| `-DeleteSource` | switch | off | Delete source TIFF after successful compression (mode 8 only) |
| `-ForceParallel` | switch | off | Force parallelism ON (use if PS5 detected but pwsh is available) |
| `-ForceSequential` | switch | off | Force parallelism OFF (use if PS7 detected but want sequential) |
| `-StagingDir` | string | `""` | SSD staging folder for faster I/O. Files moved to final destination after each group |

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

---

## SafeMode (default: ON)

Multi-page TIFFs (scanner Infrared files, Photoshop layer stacks) have proprietary IFD structures that can be corrupted by re-compression. SafeMode detects these via `magick identify` with a 30-second timeout and skips them.

```
SKIP Multi-page TIFFs: SafeMode=$true
COMPRESS all TIFFs: SafeMode=$false
```

To force compression of all TIFFs (including multi-page):
```powershell
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 0 -InputDir . -SafeMode:$false
```

---

## OLD_TIFFs Protection (Modes 0 and 9)

Modes 0 and 9 move originals to `OLD_TIFFs/` subfolder and **skip any TIFF already inside an `OLD_TIFFs` folder**. This protects originals from previous conversions and prevents re-processing.

Other modes (1-8) do not have this filter — running them on a folder that already has `OLD_TIFFs` content will process those files normally.

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

Mode 8 deletes the original TIFF after successful ZIP compression. Deletion only happens if:
1. ZIP file was created successfully
2. `magick identify` verifies ZIP integrity
3. Source file still exists

If any check fails, the source is preserved.

---

## PowerShell Version Handling

- **PS7+**: Uses `ForEach-Object -Parallel` with `-ThrottleLimit $Workers -AsJob`
- **PS5.1**: Falls back to sequential `foreach` loop (no parallel execution)

Detected automatically via `$PSVersionTable.PSVersion.Major -ge 7`.

---

## Logging

Logs written to `Logs/compress_tiff_zip/<timestamp>.log` in the current working directory. Each run appends to a new timestamped file.

Sample output:
```
10:23:45 | INFO | Log: Logs/compress_tiff_zip/20240415_102345.log
10:23:45 | INFO | Mode: 3 | Workers: 8 | OutputDir: (in-place/flat) | Staging: disabled | DryRun: False | SafeMode: True | SkipLzw: False | Overwrite: False | DeleteSource: OFF
10:23:45 | INFO | Found: 150 TIFF(s)
10:23:45 | INFO | -- Group: E:\photos\session1 (25 file(s))
10:23:46 | INFO | [1/150] OK (Uncompressed → ZIP) | DSC_0001.tif
...
10:24:12 | INFO | Done: 142 OK | 5 skipped | 3 multi-page (not touched) | 0 errors | 150/150 processed
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
PowerShell 5.1+  (PS7 recommended for parallel processing)
```

Both `magick.exe` and `exiftool.exe` must be on your PATH.

### Verify
```powershell
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx
$PSVersionTable.PSVersion.Major  # PowerShell version
```
