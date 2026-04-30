# generate_thumbnails.ps1 -- Thumbnail Generator

Standalone thumbnail generator for TIFF files. Converts TIFF pages to smaller JPEG/PNG/TIFF thumbnails.

**Requires:** PowerShell 5.1 or 7, ImageMagick 7. No Python or exiftool required.

---

## CLI Reference

```powershell
# Basic usage: generate 256px JPEG thumbnails in current directory
powershell -NoProfile -File generate_thumbnails.ps1

# Custom size and output directory
powershell -NoProfile -File generate_thumbnails.ps1 -InputDir . -Size 512 -OutputDir "C:\Thumbs"

# Recursive processing with parallel workers
powershell -NoProfile -File generate_thumbnails.ps1 -InputDir . -Recursive -Workers 8

# TIFF output with ZIP compression
powershell -NoProfile -File generate_thumbnails.ps1 -Format tif -Quality 95

# Dry-run to preview what would be generated
powershell -NoProfile -File generate_thumbnails.ps1 -InputDir . -DryRun
```

---

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-InputDir` | string | `.` | Source directory containing TIFF files |
| `-Size` | int | `256` | Thumbnail size in pixels (32-4096) |
| `-OutputDir` | string | `""` | Output directory. If empty, uses `InputDir/Thumbnails/` |
| `-Remove` | switch | off | Remove generated thumbnails instead of creating them |
| `-DryRun` | switch | off | Show what would be generated, don't create files |
| `-Recursive` | switch | off | Process subdirectories recursively |
| `-Workers` | int | `4` | Number of parallel worker threads (PS7) |
| `-Page` | string | `"0"` | Page number to extract (`0`=first, `all`=all pages) |
| `-Quality` | string | `"85"` | JPEG quality (1-100) or TIFF compression level |
| `-Format` | string | `"jpg"` | Output format: `jpg`, `jpeg`, `png`, `tif`, `tiff` |

---

## Output

Thumbnails are saved with the original filename plus `_thumb` suffix:
- `photo.tif` → `Thumbnails/photo_thumb.jpg`

Logs are written to `Logs/generate_thumbnails/`.

---

## Features

- **sRGB colorspace**: All outputs converted to sRGB for consistency
- **ICC stripping**: Removes ICC profiles to reduce file size
- **Configurable quality**: JPEG quality adjustable (default 85)
- **Multi-format**: Supports JPEG, PNG, and TIFF output
- **Parallel processing**: Uses PS7 `ForEach-Object -Parallel` for speed
- **Page selection**: Can extract specific page or all pages from multi-page TIFFs