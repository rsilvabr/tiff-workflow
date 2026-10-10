# Copy EXIF Scripts -- TIFF Metadata Restoration

Copy EXIF metadata from JPEG to TIFF. Used in Fuji S3/S5 Pro workflows where Hyper Utility exports TIFFs without camera metadata.

**Status:** Scripts implemented. The `convert_tiff.py` wizard integrates with `copy_exif_to_TIFF_ps7.ps1` (PowerShell 7, parallel) and `copy_exif_to_TIFF_ps5.ps1` (PowerShell 5.1, sequential).

---

## Scripts

Two versions are provided:
- **`copy_exif_to_TIFF_ps7.ps1`** -- PowerShell 7, parallel processing with `ForEach-Object -Parallel`
- **`copy_exif_to_TIFF_ps5.ps1`** -- PowerShell 5.1, sequential processing (no `-Parallel` support)

Both will be callable directly or via the `convert_tiff.py` wizard.

---

## Use Case

Fuji S3 Pro / S5 Pro workflow with Hyper Utility:

1. Shoot RAW + JPEG simultaneously (JPEG contains full EXIF: camera model, lens, date, GPS, etc.)
2. Export TIFF from Hyper Utility (TIFFs come **without** EXIF metadata)
3. Run Copy EXIF script -- JPEG EXIF is extracted and written to matching TIFF
4. TIFFs now have proper metadata for Lightroom, Capture One, exiftool, etc.

---

## Input Requirements

- Folder with `.tif`/`.tiff` files and matching `.jpg`/`.jpeg` files; the `.RAF` beside a TIFF
  is used as the fallback when there is no JPEG (see [Pairing](#pairing))
- Filenames should match between JPEG and TIFF (e.g. `DSC_0001.jpg` and `DSC_0001.tif`)
- EXIF data must be present in the JPEG (or RAF) files

---

## CLI Parameters

```powershell
# Basic usage (PowerShell 7, parallel)
powershell -NoProfile -File copy_exif_to_TIFF_ps7.ps1 -InputDir .

# With worker count
powershell -NoProfile -File copy_exif_to_TIFF_ps7.ps1 -InputDir . -Workers 12

# Dry-run
powershell -NoProfile -File copy_exif_to_TIFF_ps7.ps1 -InputDir . -DryRun

# Skip if TIFF already has EXIF (default: overwrite existing EXIF)
powershell -NoProfile -File copy_exif_to_TIFF_ps7.ps1 -InputDir . -SkipIfTiffHasExif

# Compress TIFF to ZIP after copying EXIF
powershell -NoProfile -File copy_exif_to_TIFF_ps7.ps1 -InputDir . -CompressZip

# Overwrite existing output
powershell -NoProfile -File copy_exif_to_TIFF_ps7.ps1 -InputDir . -Overwrite
```

---

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-InputDir` | string | `.` | Folder containing JPEG and TIFF pairs |
| `-OutputDir` | string | `""` | Output folder for processed TIFFs. **If set and different from source folder, the original TIFF is preserved** — a copy is created in OutputDir and EXIF is applied to the copy |
| `-Workers` | int | `8` | Parallel jobs (PS7 only) |
| `-WorkerMemoryFraction` | double | `0.8` | Cap parallel workers so their ImageMagick compares fit in this share of the memory still free (0 = no cap; PS5 is sequential and ignores it) |
| `-DryRun` | switch | off | Show what would be copied, don't modify anything |
| `-SkipIfTiffHasExif` | switch | off | Skip TIFFs that already have EXIF data |
| `-CompressZip` | switch | off | Compress TIFF to ZIP after copying EXIF |
| `-Overwrite` | switch | off | Overwrite existing TIFFs |

**Memory and `-Workers` (PS7).** The `-CompressZip` flow proves the staged ZIP is
pixel-identical to the TIFF it replaces with a `magick compare`, which holds ~68 bytes per
pixel -- measured at **5.35 GiB on an 84 MP scan** -- and up to `-Workers` of those ran at
once with no memory limit. Before the parallel block starts, the run reads the largest TIFF
of the group and logs `Memory: ~N GB per worker | N GB available | workers N`; if fewer
workers than requested fit in `-WorkerMemoryFraction` of the free memory, it logs
`Workers reduced from N to M` and runs at M. A size that cannot be read counts as 150 MP
(never shrinks the estimate); if the available memory cannot be read, the count is left
alone. `-DryRun` skips the cap entirely (nothing runs).

---

## How It Works

1. Scan input folder for TIFF files
2. For each TIFF, find the matching JPEG or RAF (see Pairing below)
3. Extract EXIF from the source using exiftool
4. **If `-OutputDir` is specified and different from source folder:** copy TIFF to OutputDir first, then write EXIF to the copy (original is preserved)
5. **Otherwise:** write EXIF directly to the original TIFF
6. If `-CompressZip` is set, compress the TIFF (with EXIF) to ZIP format
7. Verify EXIF was written

### Pairing

Each TIFF takes the first source found, trying every search folder in order (the file's own
folder and its `JPEG`/`JPG` subfolders, then the parent folder and its `JPEG`/`JPG`
subfolders):

1. JPEG with the exact stem (`_DSF4796.TIF` <- `_DSF4796.JPG`)
2. JPEG with the Capture One variant suffix removed (`_DSF4817_1.TIF` <- `_DSF4817.JPG`; the
   `_1`/`_2` exported variants are normal names and report plain `OK`)
3. RAF with the exact stem (`_DSF4796.RAF` for a RAW-only S5 Pro session)
4. RAF with the variant suffix removed (`DSCF0092_1.TIF` <- `DSCF0092.RAF`)
5. JPEG with the `_\d{3,4}$` cut removed (`foto_2024.tif` <- `foto.jpg`) -- a heuristic that
   can match a different photo and reports `WARN (heuristic JPEG match)`

The RAF steps come before the heuristic: for `DSC_0001.tif`, `DSC_0001.RAF` is the right
source and `DSC.jpg` is not. A result whose source was the RAF ends with `[EXIF from RAW]`.

**RAF source specifics.** A RAF carries the same EXIF/MakerNotes as the camera JPEG except
the tags that describe its embedded 1440x960 preview, so when the source is a RAF:

- `FocalPlaneXResolution`, `FocalPlaneYResolution` and `FocalPlaneResolutionUnit` are NOT
  copied (630 vs 1861 px/cm on an S5 Pro -- the preview value would be wrong for the TIFF).
- The ICC profile is never copied, whatever `-IccPolicy` (a RAF does not describe the colour
  space of the exported TIFF).
- Everything else (DateTimeOriginal, ExposureTime, FNumber, ISO, Model, ...) is copied
  exactly as it would be from the JPEG.

With neither a JPEG nor a RAF the TIFF is reported `MISS | ... | no matching JPEG or RAF`.

---

## Multi-Session Processing

The wizard's AutoFind feature scans recursively for folders matching `S5pro` or `S3pro` (or custom patterns) and passes multiple folders as a semicolon-separated list:

```powershell
-InputDir "E:\photos\S5pro\session1;E:\photos\S5pro\session2;E:\photos\S3pro\session3"
```

**Note on `-ExcludeFolders`:** both Copy EXIF backends accept the flag for consistency with the
other backends, but they read each `-InputDir` folder **non-recursively** -- there is nothing below
their roots to exclude, so the flag only really bites in the recursive compression modes.

Each session folder is processed independently.

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
```
