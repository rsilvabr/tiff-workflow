# thumbnails (`tiff_workflow/generate_thumbnails.py`) -- Thumbnail Generator

Standalone thumbnail generator for TIFF files. Converts TIFF pages to smaller JPEG/PNG/TIFF thumbnails.

**Requires:** Python 3.9+ (standard library only), ImageMagick 7. No exiftool required. It runs as `python convert_tiff.py thumbnails ...`, from a copy of the whole toolkit (`convert_tiff.py` and the folder `tiff_workflow/`).

Until v2.9.1 this backend was `generate_thumbnails.ps1`; that edition is described in [powershell_edition.md](powershell_edition.md). v3.0.0 kept the `.ps1` command line; v3.1 writes it the usual Python way (`--input-dir X --size 256`).

---

## CLI Reference

```powershell
# Basic usage: generate 256px JPEG thumbnails in current directory
python convert_tiff.py thumbnails

# Custom size and output directory
python convert_tiff.py thumbnails --input-dir . --size 512 --output-dir "C:\Thumbs"

# Recursive processing with parallel workers
python convert_tiff.py thumbnails --input-dir . --recursive --workers 8

# TIFF output with ZIP compression
python convert_tiff.py thumbnails --format tif --quality 95

# Dry-run to preview what would be generated
python convert_tiff.py thumbnails --input-dir . --dry-run
```

An option is written in full, in lower case, its value after it; a yes/no is `--x` /
`--no-x`. The same line runs as `python convert_tiff.py thumbnails ...`, and `--help` lists
every option. A command line that cannot run as written (an unknown or abbreviated name,
a value out of range, a line of v3.0.0) ends the run with **exit code 2** before anything
is touched; [command_line.md](command_line.md) translates a line of v3.0.0.

---

## Parameters

The defaults below are the `PARAMS` table at the top of `tiff_workflow/generate_thumbnails.py`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--input-dir` | string | `.` | Source directory containing TIFF files |
| `--size` | int | `256` | Thumbnail size in pixels (32-4096) |
| `--output-dir` | string | `""` | Output directory. If empty, thumbnails are written next to each source TIFF |
| `--remove` | switch | off | Remove generated thumbnails instead of creating them |
| `--dry-run` | switch | off | Show what would be generated, don't create files |
| `--recursive` | switch | off | Process subdirectories recursively |
| `--workers` | int | `4` | Thumbnails made at once. Range 1-64, capped at 16. `1` = one at a time |
| `--page` | string | `"0"` | Page number to extract (`0`=first, `all`=all pages) |
| `--quality` | string | `"85"` | JPEG quality (1-100). Ignored for `png` and `tif` -- ImageMagick reinterprets `-quality` for PNG as a zlib level + filter, so it is deliberately not passed there (TIFF output uses `-compress zip`) |
| `--format` | string | `"jpg"` | Output format: `jpg`, `jpeg`, `png`, `tif`, `tiff` |
| `--exclude-folders` | string | `""` | `';'`-separated folder NAMES to skip during discovery, on top of the built-in list below |

---

## Output

Thumbnails are saved with the original filename plus `_thumb` suffix. By default (empty `--output-dir`) they are written next to each source TIFF:
- `photo.tif` → `photo_thumb.jpg` (same folder as `photo.tif`)
- `--page all` on a multi-page TIFF → `photo_thumb-0.jpg`, `photo_thumb-1.jpg`, ...

With `--output-dir` + `--recursive`, files with the same name in different folders would map to
the same thumbnail. The later one is renamed instead of being skipped:

```
RENAME (thumbnail name already taken) | photo.tif -> photo_thumb_v2.jpg
```

Re-running with the SAME options is a no-op: existing thumbnails are detected (with `--page all`,
its `name_thumb-N` frames) and reported as `SKIP (exists)`.

The check is by NAME, not by content: changing `--size` or `--quality` alone does not regenerate
anything, because the thumbnail of that name already exists. Use `--remove` first when you want a
different size. (`--page all` and `--page <n>` write different names, so switching between them does
produce the missing one.)

### Folders skipped during discovery

`OLD_TIFFs`, `OLD_TIFF` and `converted_zip` are skipped -- they hold backups and tool output
nobody wants a thumbnail of -- matched RELATIVE to `--input-dir`, so pointing the tool straight at
one of them still works. Every run logs how many files were left out.

`ZIP` and `_EXPORT` used to be on that list and no longer are: `ZIP/` is where compress modes
1/3/5/6/7 put the compressed library and `_EXPORT` is what modes 6/7 exist to process, so
excluding them meant "make thumbnails of my compressed library" silently did nothing. Add them
back per-run with `--exclude-folders "ZIP;_EXPORT"` if that is what you want.

Logs are written to `Logs/generate_thumbnails/`.

---

## Removing thumbnails

`--remove` scans for thumbnail files directly (`*_thumb.*` and `*_thumb-N.*` in `jpg`, `jpeg`,
`png`, `tif`, `tiff`) in `--input-dir` — or in `--output-dir` when given — honouring `--recursive`.
Because it does not derive names from source TIFFs, thumbnails whose original was moved or
deleted are also cleaned up. Combine with `--dry-run` to preview.

A `.tif`/`.tiff` with such a name is only removed when it is no larger than `--size` on its long
side (round 28): a photo that happens to be called `something_thumb.tif` is kept and reported as
`SKIP (not a thumbnail this script wrote ...)`, and so is a TIFF that cannot be read. If your
TIFF thumbnails were made with a larger `--size`, pass the same `--size` to `--remove`.

```powershell
python convert_tiff.py thumbnails --input-dir . --recursive --remove --dry-run
```

---

## Features

- **sRGB colorspace**: All outputs converted to sRGB for consistency
- **Colour-managed**: wide-gamut sources (ProPhoto, AdobeRGB) are converted *through* their embedded
  ICC profile before it is stripped, so a Capture One ProPhoto export produces a thumbnail that
  actually looks like the photo. The sRGB profile is auto-detected on Windows; override it with
  `--srgb-profile <path.icc>` (a missing profile logs one `WARN` and shifts the colours)
- **ICC stripping**: Removes ICC profiles to reduce file size
- **Configurable quality**: JPEG quality adjustable (default 85)
- **Multi-format**: Supports JPEG, PNG, and TIFF output
- **Parallel processing**: several thumbnails at once (`--workers`), on any machine; `--workers 1` runs one at a time
- **Page selection**: Can extract specific page or all pages from multi-page TIFFs