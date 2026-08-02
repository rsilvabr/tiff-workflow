# TIFF Workflow Automation

Unified TIFF processing toolkit with two complementary workflows:

1. **TIFF ZIP Compression** — Re-compress TIFFs with better Deflate parameters (lossless, smaller files)
2. **Copy EXIF to TIFF** — Copy EXIF metadata from JPEG to TIFF (Fuji S3/S5 Pro workflow)
3. **Diagnose TIFFs** — Detect if 16-bit TIFFs are real 16-bit or padded 8-bit (stretched from 8-bit)
4. **Generate Thumbnails** — Create sRGB thumbnails from TIFFs (standalone or embedded in compression)

**Key feature:** Lossless recompression. ZIP/Deflate is a lossless format — pixel data stays identical, only the compression is re-optimized.

---
 
## Why compress TIFFs to Deflate?

TIFFs support many codecs, but only Deflate actually reduces size on 16-bit files. LZW makes 16-bit files *larger*. Deflate is the heaviest compression for TIFF encoding — but encoding happens only once. After encoding, there is no penalty: smaller files actually open faster.

**Benchmark** (45MP ProPhotoRGB, 5 photos average):

| Format | 16-bit real | 8-bit real | Padded 16-bit* |
|--------|-------------|------------|----------------|
| Uncompressed | 241 MB | 127 MB | 254 MB |
| LZW | 277 MB (+15%) | 64 MB (-49%) | 84 MB (-67%) |
| Deflate | 214 MB (-11%) | 60 MB (-53%) | 71 MB (-72%) |

*Padded 16-bit = 8-bit image converted to 16-bit TIFFs

**For bloated 8-bit TIFFs** (marked as 16-bit but actually 8-bit padded):
- Keep as 16-bit: 70 MB (Deflate)
- Convert to 8-bit: 59 MB (Deflate, ~17% smaller)

Option 7 (Diagnose TIFFs) helps you find these bloated files.

---

## Quick Start

### Wizard (recommended — full workflow with AutoFind)

```powershell
python convert_tiff.py
```

Eight workflows available:
- **[1] Compress TIFFs** — To Zip/Deflate, modes 0-9 (any folder)
- **[2] Fuji: Copy EXIF** — From JPEG to TIFF (Fuji S3/S5 Pro)
- **[3] Fuji: Compress** — To Zip/Deflate (Fuji S3/S5 Pro)
- **[4] Fuji: Copy + Compress** — Combined in one pass (Fuji S3/S5 Pro)
- **[5] Restore OLD_TIFFs** — Move TIFFs from OLD_TIFFs/ back to parent folder
- **[6] Delete OLD_TIFFs** — Delete OLD_TIFFs/ after verifying parent copy matches
- **[7] Diagnose TIFFs** — Check if 16-bit TIFFs are real 16-bit or padded 8-bit
- **[8] Generate Thumbnails** — Create sRGB thumbnails from TIFFs (standalone or embedded in compression)

The wizard supports **AutoFind** — automatically locates folders matching patterns like `S5pro` or `S3pro` for Fuji workflows.

### Direct PowerShell (no Python required)

```powershell
# Compress TIFFs, mode 0 (in-place)
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 0 -InputDir .

# Compress TIFFs, mode 8 (recursive, delete source after)
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 8 -InputDir F:\Photos -DeleteSource

# Compress with embedded thumbnail (creates multi-page TIFF: main image + thumbnail)
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 9 -GenerateThumbnail -ThumbSize 512 -InputDir F:\Photos

# Generate standalone thumbnails (sRGB, stripped ICC)
powershell -NoProfile -File generate_thumbnails.ps1 -InputDir F:\Photos -Size 256
```

---

## File Structure

```
tiff-workflow/
|
|-- README.md                      <- You are here (project hub)
|
|-- convert_tiff.py               <- Wizard (Python + Rich UI)
|                                   [Use this for guided workflows]
|
|-- compress_tiff_zip.ps1         <- Compression backend
|                                   [PowerShell 5.1 or 7, direct usage]
|                                   Supports embedded thumbnail generation
|
|-- generate_thumbnails.ps1        <- Thumbnail generator (standalone)
|                                   [sRGB, stripped ICC, configurable size/quality]
|
|-- copy_exif_to_TIFF_ps7.ps1      <- EXIF copy (PowerShell 7, parallel)
|-- copy_exif_to_TIFF_ps5.ps1      <- EXIF copy (PowerShell 5.1, sequential)
|
|-- docs/
|   |-- README_convert_tiff_py.md      <- Wizard detailed docs
|   |-- README_compress_tiff_zip.md <- compress_tiff_zip.ps1 detailed docs
|   |-- README_copy_exif.md           <- Copy EXIF scripts detailed docs
|   |-- README_generate_thumbnails.md  <- Thumbnail generator docs
|   |-- bugs_fixed.md                  <- Bug fixes history
|
|-- tests/                         <- Pytest + Pester tests
```

---

## File Organization Modes (modes 0-9)

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

**Safe Mode** (default: ON) automatically skips multi-page TIFFs (scanner IR files, Photoshop layers) to protect proprietary IFD structures.

---

## Manifest System

Process many folders in one run, each with its own mode, driven by a CSV you edit in Excel:

```csv
Source,Destination,Mode
F:\2025\Tokyo\TIFF,F:\2025\Tokyo\ZIP_flat,2
F:\2025\Kyoto\TIFF,F:\2025\Kyoto\TIFF,0
# F:\2025\Osaka\TIFF,F:\2025\Osaka\TIFF,3
```

**Workflow:**
1. Start workflow **[1] Compress TIFFs**, pick mode and folder
2. When asked, generate a manifest CSV instead of running (saved to `manifests/`)
3. Edit paths/modes in Excel, delete rows, comment with `#` to skip
4. Main menu **[3] Run from manifest** → pick the CSV → confirm → run

- **One entry per folder.** Modes 0/1 (non-recursive) generate one row per folder containing TIFFs; recursive modes (2-9) generate a single row for the root.
- **Destination column:** only mode **2** honors it (`-OutputDir`). All other modes compute their own output locations — the wrapper prints a warning when a Destination is ignored.
- **Rerun anytime:** main menu **[2] Repeat last workflow** re-reads the CSV (with all validation) and runs it again.
- **Encoding:** manifests are written as UTF-8 with BOM so Excel opens them correctly. A manifest re-saved by Excel in ANSI is *refused* (not guessed) — save as "CSV UTF-8".
- **Safety guards:** paths with `..` refuse the whole manifest; `Mode` accepts Excel floats (`7.0` → 7) but refuses fractions/text/out-of-range; nested or duplicate Source folders warn and require confirmation; mode 2/4/5 entries are scanned for cross-entry output collisions (abort on conflict); mode-8 entries (delete sources) require an explicit confirmation before anything runs.
- **Manifest compatibility:** manifests are guaranteed to work with the version that generated them. Regenerate after upgrading.

---

## Requirements

```
ImageMagick 7    https://imagemagick.org/script/download.php
exiftool         https://exiftool.org
Python 3.9+      (for wizard only)
Rich             pip install rich
```

Both `magick.exe` and `exiftool.exe` must be on your PATH.

### Verify
```powershell
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx
```

---

## Workflows

### Compress TIFFs (wizard option 1 or direct PowerShell)

Suitable for any TIFF collection. Choose a mode (0-9), pick a folder, and compress.

```powershell
# Direct PowerShell example
powershell -NoProfile -File compress_tiff_zip.ps1 -Mode 3 -InputDir F:\Photos -StagingDir E:\staging -Workers 12
```

See [docs/README_compress_tiff_zip.md](docs/README_compress_tiff_zip.md) for full CLI reference.

### Copy EXIF (wizard options 2, 3, 4)

For Fuji S5 Pro / S3 Pro users who export TIFFs via Hyper Utility — these TIFFs come without EXIF metadata. The wizard's AutoFind automatically locates all `S5pro` or `S3pro` folders and processes them.

**Workflow:**
1. Shoot RAW + JPEG (JPEG has EXIF)
2. Export TIFF from Hyper Utility
3. Run Copy EXIF → TIFF now has camera model, lens, date, GPS
4. Optionally compress to ZIP

See [docs/README_copy_exif.md](docs/README_copy_exif.md) for detailed documentation.

### Diagnose TIFFs (wizard option 7)

Detects whether 16-bit TIFFs are real 16-bit data or padded 8-bit (stretched to 16-bit by software that only converted the depth without adding real data).

**How it works:** Round-trip test — converts 16-bit → 8-bit → 16-bit via ImageMagick, then compares RMSE. If RMSE=0, the original was 8-bit padded. If RMSE>0, it's real 16-bit.

**Temporary files compression:** During comparison, you can choose how to compress temp TIFFs (option 1-3):
- `[1] Uncompressed` — fastest processing, higher I/O (no compression overhead)
- `[2] LZW` — balanced, 8-bit LZW compression reduces I/O with low CPU cost
- `[3] ZIP` — compresses both 8-bit and 16-bit temp files, reduces I/O but higher CPU cost

**Benchmark results** (Ryzen 5950x, SSD SATA M.2, 155 photos):

| Mode | Total Time |
|------|------------|
| Uncompressed | 78.9s |
| LZW | 80.8s |
| ZIP | 135.8s |

On fast storage (SSD), processing cost >> I/O cost, so uncompressed is fastest. On slow storage (HDD, network), LZW may win.

**After diagnosis,** you can optionally compress detected padded files to 8-bit ZIP — reducing size significantly for files that were 8-bit stretched to 16-bit.

---

## Troubleshooting

### "ERROR (exiftool check)" — cannot detect compression
```powershell
exiftool -s -s -s -Compression photo.tif
# Should output: Deflate, LZW, or nothing (uncompressed)
```

### "ERROR (magick)" — compression failed
```powershell
magick identify -verbose photo.tif
# Should display image info without errors
```

---

## Disclaimer

These tools were made for my personal workflow. 
Use at your own risk — I am not responsible for any issues you may encounter.

If you find any bugs, feel free to report — I will gladly try my best to improve this project.

Always test with a small batch before processing important archives.

---

## More about this project

I am sharing these scripts because getting all of this to work correctly was unexpectedly difficult. The challenges were:

- Lossless TIFF recompression with better Deflate parameters
- Copying EXIF/XMP/IPTC metadata from JPEG to TIFF (Fuji S3/S5 Pro workflow)
- Pixel-perfect verification using RMSE (comparing pixel data, not just metadata)
- Multi-page TIFF detection to protect scanner IR and Photoshop layer files
- Staging safety — preventing file loss during interrupted operations
- Safe undo of OLD_TIFFs folders

Getting there required finding and fixing several bugs. The full history and technical details are documented in [`docs/bugs_fixed.md`](docs/bugs_fixed.md).

### Latest release

**v2.2** - Manifest system and five audit rounds of data-loss prevention:
- **Manifest system:** process many folders in one run from a CSV edited in Excel - one entry per folder, each with its own mode, with fail-closed guards (BOM/ANSI encoding, `..` traversal, cross-entry output collisions, mode-8 delete gate). Generate from workflow [1], run from main menu [3], repeat from [2]
- `copy_exif_to_TIFF_ps5.ps1` did not even parse on PowerShell 5.1 (BOM-less UTF-8 read as ANSI); all scripts are now pure ASCII
- Legacy in-place compression silently stripped all EXIF; modes 0/9 no longer strand multi-page TIFFs in `OLD_TIFFs/`
- `photo.tif` + `photo.tiff` no longer overwrite each other; collision numbering (`_v2`, `_v3`) now covers every mode
- Mode 8 always runs its ZIP integrity check, and its abort no longer exits 0
- Wizard mode 2 finally asks for an output folder (it used to flatten everything into the input root)
- Modes 6/7 write inside the `_EXPORT` tree the file came from; cross-`_EXPORT` collisions are numbered
- `Start-Job` (one PowerShell process per file) replaced by in-process runspaces: **5.66 s → 3.60 s** on 24 files / 8 workers

See [`docs/bugs_fixed.md`](docs/bugs_fixed.md) for the complete list.

---

## License

MIT License — feel free to use, modify, and distribute.

---

## Acknowledgments

- [ExifTool](https://exiftool.org) by Phil Harvey for metadata handling
- [ImageMagick](https://imagemagick.org) for TIFF processing and compression
- [Kimi](https://www.kimi.com) (Moonshot AI) and [Claude](https://www.anthropic.com/claude) (Anthropic) for code assistance and technical discussion
