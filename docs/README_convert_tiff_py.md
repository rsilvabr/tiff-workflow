# convert_tiff.py -- TIFF Workflow Manager (Wizard)

Unified Python/Rich wizard for TIFF processing. Use this for guided workflows with AutoFind, persistent config, and streaming PowerShell output.

**Requires:** Python 3.9+, Rich (`pip install rich`), PowerShell 5.1+ or 7, ImageMagick 7, exiftool.

---

## Quick Start

```powershell
python convert_tiff.py
```

Eight workflows available:
- **[1] Compress TIFFs** -- To Zip/Deflate, modes 0-9 (any folder)
- **[2] Fuji: Copy EXIF** -- From JPEG to TIFF (AutoFind, S3/S5 Pro)
- **[3] Fuji: Compress** -- To Zip/Deflate (AutoFind, S3/S5 Pro)
- **[4] Fuji: Copy + Compress** -- Combined in one pass (AutoFind, S3/S5 Pro)
- **[5] Restore OLD_TIFFs** -- Move TIFFs back to parent folder
- **[6] Delete OLD_TIFFs** -- Verify copy matches, then delete
- **[7] Diagnose TIFFs** -- Check if 16-bit is real or padded 8-bit
- **[8] Generate Thumbnails** -- Create sRGB thumbnails from TIFFs (standalone or embedded in compression)

---

## Workflows

### Compress TIFFs (option 1)

Full TIFF ZIP compression with modes 0-9:

```
Step 1: Select mode (0-9)
Step 2: Choose input folder
Step 3: Workers, staging, dry-run
Step 4: Thumbnail generation options (optional)
Step 5: Summary + confirm
```

**Thumbnail Options (v1.2+):**
- Generate embedded thumbnails? [y/N]
- Configure thumbnail settings? [y/N]
  - Thumbnail size (px): default 256
  - JPEG quality: default 85
  - Format: jpg/png/tif (default: jpg)
- Skip already-compressed TIFFs that have thumbnails? [y/N]

When enabled, creates a multi-page TIFF:
- Page 0: Original image (ZIP/Deflate compressed)
- Page 1: Thumbnail (sRGB, ICC stripped, aspect ratio preserved)

Mode 8 shows an extra confirmation prompt since it deletes source TIFFs after compression.

### Fuji: Copy EXIF (option 2)

For Fuji S3/S5 Pro users who export TIFFs via Hyper Utility (which strips EXIF metadata). The wizard uses **AutoFind** to automatically locate all `S5pro` or `S3pro` folders recursively.

**AutoFind pattern options:**
- [1] S5 Pro folders -- matches `S5pro` in folder name
- [2] S3 Pro folders -- matches `S3pro` in folder name
- [3] Both -- matches `S5pro` and `S3pro`
- [4] Custom -- type any pattern (e.g. `S2pro`, `mycamera`)

**Workflow:**
1. Shoot RAW + JPEG (JPEG has EXIF)
2. Export TIFF from Hyper Utility
3. Run Fuji: Copy EXIF wizard
4. TIFFs now have camera model, lens, date, GPS

### Fuji: Compress (option 3)

Same as Compress TIFFs but filtered to S3/S5 Pro sessions found via AutoFind.

### Fuji: Copy + Compress (option 4)

Runs Fuji: Copy EXIF first, then Fuji: Compress in sequence -- two steps in one pass.

### Restore OLD_TIFFs (option 5)

Move TIFFs from OLD_TIFFs/ subfolders back to their parent folders.

### Delete OLD_TIFFs (option 6)

Verify each TIFF in OLD_TIFFs matches the parent copy (via RMSE), then delete if identical.

### Diagnose TIFFs (option 7)

Check if 16-bit TIFFs are real 16-bit data or padded 8-bit (stretched from 8-bit by software that only converted depth). Uses round-trip RMSE method.

### Generate Thumbnails (option 8)

Create sRGB thumbnails from TIFFs. Can be used standalone or as part of the compression workflow.

**Settings:**
- Input directory
- Thumbnail size (32-4096 px, default: 256)
- JPEG quality (default: 85)
- Format: jpg, png, or tif (default: jpg)
- Recursive processing
- Dry-run mode

**Output:** `filename_thumb.jpg` next to each TIFF (or in specified output folder)

**Thumbnail format:**
- Converted to sRGB
- ICC profile stripped
- Aspect ratio preserved (only downscales)
- First page only for multi-page TIFFs

---

## AutoFind

Recursively scans a root folder for sessions matching the chosen pattern. Shows a table with session name, TIFF count, and truncated path. Prompts for confirmation before processing.

Excluded from scan: `Logs`, `logs`, `converted_zip`, `ZIP`, `_EXPORT` folders.

---

## Persistent Config

Config saved to `~/.convert_tiff_config.json`:
- `last_input_dir` -- last used folder
- `last_workers` -- last worker count
- `last_staging` -- last staging folder
- `last_pattern` -- last AutoFind pattern
- `last_mode` -- last mode selected
- `last_origin` -- last workflow used

---

## CLI Flags (all optional)

```powershell
python convert_tiff.py                    # Interactive wizard
python convert_tiff.py --help             # Show help
```

No CLI flags are required -- the wizard is fully interactive.

---

## Output

The wizard calls `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps7.ps1`, or `generate_thumbnails.ps1` as a subprocess and streams their output with color coding:
- `[OK]` lines -- green
- `[ERROR]` lines -- red
- `[WARNING]` / `[WARN]` lines -- yellow
- `[DRY` / `DRY` lines -- blue

---

## Error Handling

- Folder not found: stops with error message
- No matching folders (AutoFind): stops with "No matching folders found"
- Mode 8 delete confirmation: requires explicit `y` to proceed
- Keyboard interrupt: exits cleanly

---

## Requirements

```
ImageMagick 7    https://imagemagick.org/script/download.php
exiftool         https://exiftool.org
Python 3.9+      https://www.python.org
Rich             pip install rich
```

Both `magick.exe` and `exiftool.exe` must be on your PATH.

### Verify
```powershell
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx
python --version    # Python 3.9+
```
