# TIFF Workflow Automation

Unified TIFF processing toolkit with four complementary workflows:

1. **TIFF ZIP Compression** — Re-compress TIFFs with better Deflate parameters (lossless, smaller files)
2. **Copy EXIF to TIFF** — Copy EXIF metadata from JPEG to TIFF (Fuji S3/S5 Pro workflow)
3. **Diagnose TIFFs** — Detect if 16-bit TIFFs are real 16-bit or padded 8-bit (stretched from 8-bit)
4. **Generate Thumbnails** — Create sRGB thumbnails from TIFFs (standalone or embedded in compression)

**Key feature:** Lossless recompression. ZIP/Deflate is a lossless format — pixel data stays identical, only the compression is re-optimized.

**Current version: v3.1.0** (2026-11-13) · [What's new](#whats-new-in-v310) · [Coming from v3.0.0](docs/command_line.md) · [All releases](docs/CHANGELOG.md)

**Windows program, no Python needed:** [tiff-workflow-exe](https://github.com/rsilvabr/tiff-workflow-exe/releases/latest) — unzip and double-click; ExifTool and ImageMagick included.

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

Some software offers LZW as the only TIFF compression option on export; an existing archive of 16-bit LZW files can therefore be larger than the same files stored uncompressed.

**For bloated 8-bit TIFFs** (marked as 16-bit but actually 8-bit padded):
- Keep as 16-bit: 70 MB (Deflate)
- Convert to 8-bit: 59 MB (Deflate, ~17% smaller)

Option 7 (Diagnose TIFFs) helps you find these bloated files.

---

## The scripts

| File | What it does |
|------|--------------|
| [`convert_tiff.py`](convert_tiff.py) | What you run: the interactive wizard (guided workflows, AutoFind, presets, manifests), and each backend below by its name |
| [`tiff_workflow/compress_tiff_zip.py`](tiff_workflow/compress_tiff_zip.py) | `compress`: TIFF → ZIP/Deflate, modes 0-9, optionally with an embedded thumbnail |
| [`tiff_workflow/copy_exif_to_tiff.py`](tiff_workflow/copy_exif_to_tiff.py) | `copy-exif`: EXIF from the Fuji S3/S5 Pro JPEG (or RAF) into the TIFF |
| [`tiff_workflow/generate_thumbnails.py`](tiff_workflow/generate_thumbnails.py) | `thumbnails`: sRGB thumbnails, standalone |
| [`tiff_workflow/`](tiff_workflow) | Also the wizard (`wizard.py`) and what the backends share (`common.py`): keep the folder next to `convert_tiff.py` |

---

## Install

**On Windows, without Python:** [tiff-workflow-exe](https://github.com/rsilvabr/tiff-workflow-exe/releases/latest) — unzip and double-click.

**With Python 3.9+:**

```powershell
pip install rich
```

plus two tools on PATH: [ImageMagick 7](https://imagemagick.org/script/download.php) and [ExifTool](https://exiftool.org), the latter named exactly `exiftool.exe` (the download comes as `exiftool(-k).exe`: rename it). `rich` is only for the wizard's menus; the backends use only what Python ships with. Step by step, with the PATH setup and troubleshooting: [docs/install.md](docs/install.md).

---

## Quick Start — the wizard

```powershell
python convert_tiff.py
```

Eight workflows available:
- **[1] Compress TIFFs** — To Zip/Deflate, modes 0-9 (any folder)
- **[2] Fuji: Copy EXIF** — From JPEG to TIFF (Fuji S3/S5 Pro); falls back to the RAF when there is no JPEG
- **[3] Fuji: Compress** — To Zip/Deflate (Fuji S3/S5 Pro)
- **[4] Fuji: Copy + Compress** — Copy EXIF, then compress (Fuji S3/S5 Pro)
- **[5] Restore OLD_TIFFs** — Move TIFFs from OLD_TIFFs/ back to parent folder
- **[6] Delete OLD_TIFFs** — Delete OLD_TIFFs/ after verifying parent copy matches
- **[7] Diagnose TIFFs** — Check if 16-bit TIFFs are real 16-bit or padded 8-bit
- **[8] Generate Thumbnails** — Create sRGB thumbnails from TIFFs (standalone or embedded in compression)

The wizard supports **AutoFind** — automatically locates folders matching patterns like `S5pro` or `S3pro` for Fuji workflows. The main menu also repeats the last workflow, runs a manifest, saves and runs presets, and edits the script settings. Full guide: [docs/README_convert_tiff_py.md](docs/README_convert_tiff_py.md).

---

## The backends on their own

```powershell
python convert_tiff.py compress --mode 0 --input-dir .                          # compress in place; originals go to OLD_TIFFs/
python convert_tiff.py compress --mode 8 --input-dir F:\Photos --delete-source   # recursive, in place, replaces each original
python convert_tiff.py compress --mode 3 --input-dir F:\Photos --staging-dir E:\staging --workers 12
python convert_tiff.py compress --mode 9 --generate-thumbnail --thumb-size 512 --input-dir F:\Photos   # with an embedded thumbnail page
python convert_tiff.py copy-exif --input-dir "F:\Photos\S5pro session"         # EXIF from each JPEG into its TIFF
python convert_tiff.py thumbnails --input-dir F:\Photos --size 256             # standalone sRGB thumbnails
```

`--help` after the name lists its options. A yes/no is `--safe-mode` / `--no-safe-mode`. **A line written for v3.0.0 or earlier** (`python compress_tiff_zip.py -Mode 9 -SafeMode:$false`) is refused: [the table](docs/command_line.md) rewrites it. Every parameter: [compress](docs/README_compress_tiff_zip.md), [Copy EXIF](docs/README_copy_exif.md), [thumbnails](docs/README_generate_thumbnails.md).

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

What each mode does with name collisions, staging and deletes: [docs/README_compress_tiff_zip.md](docs/README_compress_tiff_zip.md#modes-0-9).

---

## Manifests

Process many folders in one run, each with its own mode, driven by a CSV you edit in Excel:

```csv
Source,Destination,Mode
F:\2025\Recife\TIFF,F:\2025\Recife\ZIP_flat,2
F:\2025\São Paulo\TIFF,F:\2025\São Paulo\TIFF,0
F:\2025\横浜\TIFF,F:\2025\横浜\TIFF,3
F:\2026\京都・嵐山 [FINAL]\TIFF,F:\2026\京都・嵐山 [FINAL]\TIFF,0
# F:\2026\Outtakes (later)\TIFF,F:\2026\Outtakes (later)\TIFF,0
```

Start workflow **[1] Compress TIFFs**, pick a mode and a folder, and answer yes to generate a manifest instead of running; edit it, comment a row out with `#`, and run it with main menu **[3] Run from manifest**. The whole file is checked before anything runs. The columns, the guards and what a repeat does: [docs/README_convert_tiff_py.md](docs/README_convert_tiff_py.md#manifest-system-main-menu-option-3).

---

## Good to know

One line each; the detail is behind each link.

- **Safe Mode** (default: ON) skips multi-page TIFFs (scanner IR files, Photoshop layers). A page counts as a thumbnail only when it is marked as one or is strictly smaller than the main image. [More](docs/README_compress_tiff_zip.md#safemode-default-on)
- **Where the originals go:** modes 0 and 9 keep each original in `OLD_TIFFs/`, moved there only after its compressed copy is ready; wizard options 5 and 6 restore or delete those folders, and no mode ever takes `OLD_TIFFs/` as input. [More](docs/README_compress_tiff_zip.md#old_tiffs-protection-all-modes)
- **After an interrupted run**, every later run lists originals left in `OLD_TIFFs/` with nothing in their place, and `--restore-stranded` puts them back. [More](docs/README_compress_tiff_zip.md#stranded-originals)
- **Embedded thumbnails** (`--generate-thumbnail`) are colour-managed and never drop pages: a file whose extra pages are real is refused, not rebuilt. A library that is already Deflate needs `--skip-compressed-with-thumb`. [More](docs/README_compress_tiff_zip.md#thumbnail-generation-v12)
- **Folder exclusion** (`--exclude-folders "_EXPORT;temp"`) skips whole folder trees by name, in every mode, and only below the folder you point at. [More](docs/README_compress_tiff_zip.md#behavior)
- **Copy EXIF** is for Fuji S3/S5 Pro TIFFs exported by Hyper Utility, which come without EXIF: it takes the camera data from the JPEG (or RAF) of the same shot, and can compress in the same pass. [More](docs/README_copy_exif.md)
- **Diagnose** tells real 16-bit TIFFs from padded 8-bit ones by a 16 → 8 → 16-bit round trip, and only reports unless you ask it to convert them. [More](docs/README_convert_tiff_py.md#diagnose-tiffs-option-7)
- **Presets:** save the last workflow under a name (main menu **[5]**) and run it unattended with `python convert_tiff.py --run-preset NAME`. [More](docs/README_convert_tiff_py.md#presets-menu-5)
- **Script settings without editing a script:** folder names, timeouts and thumbnail defaults live in `.tiff_workflow_settings.json` (main menu **[6]**); it can never change anything that deletes, overwrites or skips a safety check. [More](docs/README_convert_tiff_py.md#script-settings-main-menu-6)
- **Version and new releases:** `python convert_tiff.py --version` prints the release, which is also on the first line of every log; the main menu looks once a day for a newer one (one anonymous request to GitHub; never for `--run-preset`). [More](docs/README_convert_tiff_py.md#new-release-notice)
- **What goes into your files:** your metadata is kept; each compress adds one `xmpMM:History` entry. [More](docs/README_compress_tiff_zip.md#edit-history-in-the-file-xmpmmhistory)

---

## Documentation

| Document | Contents |
|----------|----------|
| [docs/install.md](docs/install.md) | Installing the Python version step by step: downloads, PATH, troubleshooting |
| [docs/README_convert_tiff_py.md](docs/README_convert_tiff_py.md) | The wizard: every workflow, manifests, presets and scheduling, script settings |
| [docs/README_compress_tiff_zip.md](docs/README_compress_tiff_zip.md) | `convert_tiff.py compress`: parameters, modes, Safe Mode, `OLD_TIFFs`, staging, thumbnails |
| [docs/README_copy_exif.md](docs/README_copy_exif.md) | `convert_tiff.py copy-exif`: pairing a TIFF with its JPEG or RAF, parameters |
| [docs/README_generate_thumbnails.md](docs/README_generate_thumbnails.md) | `convert_tiff.py thumbnails`: parameters, output, removing thumbnails |
| [docs/command_line.md](docs/command_line.md) | The command line of v3.1, and every option of v3.0.0 in its new form |
| [docs/powershell_edition.md](docs/powershell_edition.md) | The PowerShell scripts of v2.9.1 and what changes when you move to v3 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | What changed in each release |
| [docs/bugs_fixed.md](docs/bugs_fixed.md) | Every bug fix, one section per audit round |

**Testing.** `python -m pytest tests` runs the suite, which drives the scripts end to end on real TIFFs ([tests/README.md](tests/README.md)). [tools/real_photo_battery.py](tools/real_photo_battery.py) runs them on **copies of your own photos** (`python tools/real_photo_battery.py --fixtures <folder>`).

---

## What's new in v3.1.0

2026-11-13. If you only use the wizard's menus, nothing to do. If you type commands, keep `.bat` files or run presets, read the first two lines.

- **The backends take the usual Python command line**, `--mode 9 --input-dir F:\Photos --no-safe-mode`, through one entry point: `python convert_tiff.py compress ...` (also `copy-exif`, `thumbnails`). A line written for v3.0.0 is refused, nothing touched: [the table](docs/command_line.md) rewrites it.
- **Presets and *Repeat last* saved by v3.0.0 or earlier no longer run:** run the workflow once and save it again. The settings file takes the new names (`zip_subfolder_name`).
- **The files moved:** everything but `convert_tiff.py` is in the folder `tiff_workflow/`; copy both.
- **The wizard reads every yes/no the same way** on both screens (`y/yes/s/sim`, `n/no/nao/não`; "no" is the default of every question that deletes or replaces), and Restore, Delete OLD_TIFFs, Diagnose and mode 2 got fixes.

[Every change](docs/CHANGELOG.md#v310) · [earlier releases](docs/CHANGELOG.md) · [GitHub Releases](https://github.com/rsilvabr/tiff-workflow/releases)

---

## About this project

I am sharing these scripts because getting all of this to work correctly was unexpectedly difficult. The challenges were:

- Lossless TIFF recompression with better Deflate parameters
- Copying EXIF/XMP/IPTC metadata from JPEG to TIFF (Fuji S3/S5 Pro workflow)
- Pixel-perfect verification using RMSE (comparing pixel data, not just metadata)
- Multi-page TIFF detection to protect scanner IR and Photoshop layer files
- Staging safety — preventing file loss during interrupted operations
- Safe undo of OLD_TIFFs folders

Getting there required finding and fixing several bugs. The full history and technical details are documented in [`docs/bugs_fixed.md`](docs/bugs_fixed.md).

[tiff-workflow-exe](https://github.com/rsilvabr/tiff-workflow-exe) packages this toolkit as a Windows program, no Python needed. The wizard and the scripts inside are byte for byte those of a release here, it passes the same real-photo battery, and presets are shared between the two. The Python version here is the one I run on my own archive and remains the more tested.

---

## Disclaimer

These tools were made for my personal workflow. 
Use at your own risk — I am not responsible for any issues you may encounter.

If you find any bugs, feel free to report — I will gladly try my best to improve this project.

Always test with a small batch before processing important archives.

---

## License

MIT License — feel free to use, modify, and distribute.

---

## Acknowledgments

- [ExifTool](https://exiftool.org) by Phil Harvey for metadata handling
- [ImageMagick](https://imagemagick.org) for TIFF processing and compression
- [Claude](https://www.anthropic.com/claude) (Anthropic) and [DeepSeek](https://www.deepseek.com), among other AI tools, for code assistance, reviews and technical discussion
