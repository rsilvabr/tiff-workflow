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

- Folder with both `.tif`/`.tiff` files AND `.jpg`/`.jpeg` files
- Filenames should match between JPEG and TIFF (e.g. `DSC_0001.jpg` and `DSC_0001.tif`)
- EXIF data must be present in the JPEG files

---

## Planned CLI Parameters

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

## Planned Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-InputDir` | string | `.` | Folder containing JPEG and TIFF pairs |
| `-OutputDir` | string | `""` | Output folder for processed TIFFs. **If set and different from source folder, the original TIFF is preserved** — a copy is created in OutputDir and EXIF is applied to the copy |
| `-Workers` | int | `8` | Parallel jobs (PS7 only) |
| `-DryRun` | switch | off | Show what would be copied, don't modify anything |
| `-SkipIfTiffHasExif` | switch | off | Skip TIFFs that already have EXIF data |
| `-CompressZip` | switch | off | Compress TIFF to ZIP after copying EXIF |
| `-Overwrite` | switch | off | Overwrite existing TIFFs |

---

## How It Works

1. Scan input folder for TIFF files
2. For each TIFF, find matching JPEG (same filename stem)
3. Extract EXIF from JPEG using exiftool
4. **If `-OutputDir` is specified and different from source folder:** copy TIFF to OutputDir first, then write EXIF to the copy (original is preserved)
5. **Otherwise:** write EXIF directly to the original TIFF
6. If `-CompressZip` is set, compress the TIFF (with EXIF) to ZIP format
7. Verify EXIF was written

---

## Multi-Session Processing

The wizard's AutoFind feature scans recursively for folders matching `S5pro` or `S3pro` (or custom patterns) and passes multiple folders as a semicolon-separated list:

```powershell
-InputDir "E:\photos\S5pro\session1;E:\photos\S5pro\session2;E:\photos\S3pro\session3"
```

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
