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
| `-OutputDir` | string | `""` | Output directory. If empty, thumbnails are written next to each source TIFF |
| `-Remove` | switch | off | Remove generated thumbnails instead of creating them |
| `-DryRun` | switch | off | Show what would be generated, don't create files |
| `-Recursive` | switch | off | Process subdirectories recursively |
| `-Workers` | int | `4` | Number of parallel worker threads (PS7). Range 1-64, capped at 16 |
| `-Page` | string | `"0"` | Page number to extract (`0`=first, `all`=all pages) |
| `-Quality` | string | `"85"` | JPEG quality (1-100). Ignored for `png` and `tif` -- ImageMagick reinterprets `-quality` for PNG as a zlib level + filter, so it is deliberately not passed there (TIFF output uses `-compress zip`) |
| `-Format` | string | `"jpg"` | Output format: `jpg`, `jpeg`, `png`, `tif`, `tiff` |
| `-ExcludeFolders` | string | `""` | `';'`-separated folder NAMES to skip during discovery, on top of the built-in list below |

---

## Output

Thumbnails are saved with the original filename plus `_thumb` suffix. By default (empty `-OutputDir`) they are written next to each source TIFF:
- `photo.tif` → `photo_thumb.jpg` (same folder as `photo.tif`)
- `-Page all` on a multi-page TIFF → `photo_thumb-0.jpg`, `photo_thumb-1.jpg`, ...

With `-OutputDir` + `-Recursive`, files with the same name in different folders would map to
the same thumbnail. The later one is renamed instead of being skipped:

```
RENAME (thumbnail name already taken) | photo.tif -> photo_thumb_v2.jpg
```

Re-running with the SAME options is a no-op: existing thumbnails are detected (with `-Page all`,
its `name_thumb-N` frames) and reported as `SKIP (exists)`.

The check is by NAME, not by content: changing `-Size` or `-Quality` alone does not regenerate
anything, because the thumbnail of that name already exists. Use `-Remove` first when you want a
different size. (`-Page all` and `-Page <n>` write different names, so switching between them does
produce the missing one.)

### Folders skipped during discovery

`OLD_TIFFs`, `OLD_TIFF` and `converted_zip` are skipped -- they hold backups and tool output
nobody wants a thumbnail of -- matched RELATIVE to `-InputDir`, so pointing the tool straight at
one of them still works. Every run logs how many files were left out.

`ZIP` and `_EXPORT` used to be on that list and no longer are: `ZIP/` is where compress modes
1/3/5/6/7 put the compressed library and `_EXPORT` is what modes 6/7 exist to process, so
excluding them meant "make thumbnails of my compressed library" silently did nothing. Add them
back per-run with `-ExcludeFolders "ZIP;_EXPORT"` if that is what you want.

Logs are written to `Logs/generate_thumbnails/`.

---

## Removing thumbnails

`-Remove` scans for thumbnail files directly (`*_thumb.*` and `*_thumb-N.*` in `jpg`, `jpeg`,
`png`, `tif`, `tiff`) in `-InputDir` — or in `-OutputDir` when given — honouring `-Recursive`.
Because it does not derive names from source TIFFs, thumbnails whose original was moved or
deleted are also cleaned up. Combine with `-DryRun` to preview.

```powershell
powershell -NoProfile -File generate_thumbnails.ps1 -InputDir . -Recursive -Remove -DryRun
```

---

## Features

- **sRGB colorspace**: All outputs converted to sRGB for consistency
- **ICC stripping**: Removes ICC profiles to reduce file size
- **Configurable quality**: JPEG quality adjustable (default 85)
- **Multi-format**: Supports JPEG, PNG, and TIFF output
- **Parallel processing**: Uses PS7 `ForEach-Object -Parallel` for speed
- **Page selection**: Can extract specific page or all pages from multi-page TIFFs