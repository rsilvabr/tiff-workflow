# copy_exif_to_tiff.py -- TIFF Metadata Restoration

Copy EXIF metadata from JPEG to TIFF. Used in Fuji S3/S5 Pro workflows where Hyper Utility exports TIFFs without camera metadata.

**Status:** Implemented. The `convert_tiff.py` wizard runs `copy_exif_to_tiff.py`.

---

## Script

One script, **`copy_exif_to_tiff.py`**, callable directly or via the `convert_tiff.py` wizard.
It works on several TIFFs at once by default (`--workers`), on any machine; `--workers 1` runs
one at a time.

Until v2.9.1 there were two PowerShell scripts, `copy_exif_to_TIFF_ps5.ps1` (sequential) and
`copy_exif_to_TIFF_ps7.ps1` (parallel); that edition is described in
[powershell_edition.md](powershell_edition.md). v3.0.0 kept the `.ps1` command line; v3.1
writes it the usual Python way (`--input-dir X --compress-zip`).

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
# Basic usage
python copy_exif_to_tiff.py --input-dir .

# With worker count
python copy_exif_to_tiff.py --input-dir . --workers 12

# One TIFF at a time
python copy_exif_to_tiff.py --input-dir . --workers 1

# Dry-run
python copy_exif_to_tiff.py --input-dir . --dry-run

# Skip TIFFs that already carry camera EXIF (default: overwrite existing EXIF)
python copy_exif_to_tiff.py --input-dir . --skip-if-tiff-has-exif

# Compress TIFF to ZIP after copying EXIF
python copy_exif_to_tiff.py --input-dir . --compress-zip

# Overwrite existing output
python copy_exif_to_tiff.py --input-dir . --overwrite
```

An option is written in full, in lower case, its value after it; a yes/no is `--x` /
`--no-x`. The same line runs as `python convert_tiff.py copy-exif ...`, and `--help` lists
every option. A command line that cannot run as written (an unknown or abbreviated name,
a value out of range, a line of v3.0.0) ends the run with **exit code 2** before anything
is touched; [command_line.md](command_line.md) translates a line of v3.0.0.

---

## Parameters

The defaults below are the `PARAMS` table at the top of `copy_exif_to_tiff.py`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--input-dir` | string | `""` | Folder containing JPEG and TIFF pairs. Empty = the current folder |
| `--input-dir-file` | string | `""` | UTF-8 text file with one input folder per line, used instead of `--input-dir` (not together). The wizard switches to it automatically when the folder list would exceed the Windows command-line limit. |
| `--output-dir` | string | `""` | Output folder for processed TIFFs. **If set and different from source folder, the original TIFF is preserved** — a copy is created in the output folder and EXIF is applied to the copy |
| `--workers` | int | `16` | TIFFs processed at once (1-64). `1` = one at a time |
| `--worker-memory-fraction` | double | `0.8` | Cap parallel workers so their ImageMagick compares fit in this share of the memory still free (0 = no cap; with `--workers 1` there is nothing to cap) |
| `--dry-run` | switch | off | Show what would be copied, don't modify anything. No folder is created either, `--output-dir` included |
| `--skip-if-tiff-has-exif` | switch | off | Skip TIFFs that already carry camera EXIF (an Exif IFD: exposure, aperture, dates). The basic tags every TIFF has -- size, resolution, orientation, and the Make/Model some converters write -- do not count. Before v2.7.1 they did, and the option skipped every TIFF |
| `--compress-zip` | switch | off | Compress TIFF to ZIP after copying EXIF |
| `--overwrite` | switch | off | Overwrite existing TIFFs |

**Memory and `--workers`.** The `--compress-zip` flow proves the staged ZIP is
pixel-identical to the TIFF it replaces with a `magick compare`, which holds ~68 bytes per
pixel -- measured at **5.35 GiB on an 84 MP scan** -- and up to `--workers` of those ran at
once with no memory limit. Before the workers of a group start, the run reads the largest TIFF
of the group; if fewer workers than requested fit in `--worker-memory-fraction` of the free
memory, it logs `Memory: ~N GB per worker | N GB available | workers N` and
`Workers reduced from N to M`, and runs at M. A size that cannot be read counts as 150 MP
(never shrinks the estimate); if the available memory cannot be read, the count is left
alone. `--dry-run` skips the cap entirely (nothing runs).

---

## How It Works

1. Scan input folder for TIFF files
2. For each TIFF, find the matching JPEG or RAF (see Pairing below)
3. Extract EXIF from the source using exiftool
4. **If `--output-dir` is specified and different from source folder:** copy TIFF to the output folder first, then write EXIF to the copy (original is preserved)
5. **Otherwise:** write EXIF directly to the original TIFF
6. If `--compress-zip` is set, compress the TIFF (with EXIF) to ZIP format; the ZIP replaces the
   TIFF only after it is proven pixel-identical to it
7. A file whose EXIF copy failed is reported as `ERROR`, and the run ends with exit code 1

Each run writes its log to `Logs\Copy-S5Pro-Exif\<timestamp>.log` under the **current
folder** (the one you start it from), like the other backends -- through the wizard, that is
the folder the wizard was started from. Calling the script from somewhere else puts its
`Logs\` there. The first log line names the release, the script and the Python version:
`tiff-workflow 3.0.0 | copy_exif_to_tiff.py | Python 3.13.1`.

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
- The ICC profile is never copied, whatever `--icc-policy` (a RAF does not describe the colour
  space of the exported TIFF).
- Everything else (DateTimeOriginal, ExposureTime, FNumber, ISO, Model, ...) is copied
  exactly as it would be from the JPEG.

With neither a JPEG nor a RAF the TIFF is reported `MISS | ... | no matching JPEG or RAF`.

---

## Multi-Session Processing

The wizard's AutoFind feature scans recursively for folders matching `S5pro` or `S3pro` (or custom patterns) and passes multiple folders as a semicolon-separated list:

```powershell
--input-dir "E:\photos\S5pro\session1;E:\photos\S5pro\session2;E:\photos\S3pro\session3"
```

**Note on `--exclude-folders`:** the Copy EXIF backend reads each `--input-dir` folder
**non-recursively**, so inside a folder there is nothing to exclude. Since v3.1 the names apply to
the folders `--auto-find` finds: a session that sits below a folder of that name (or is named so) is
left out, and the log says how many (`Excluded 1 folder(s) under: temp`). Until v3.0.0 the flag did
nothing in this backend. The tool's own folders (`Logs`, `OLD_TIFFs`, `ZIP`, `_EXPORT`,
`converted_zip`) are never searched, with or without the flag.

Each session folder is processed independently.

---

## Requirements

```
ImageMagick 7    https://imagemagick.org/script/download.php
exiftool         https://exiftool.org
Python 3.9+      https://www.python.org
```

Both `magick.exe` and `exiftool.exe` must be on your PATH. The script uses only Python's
standard library; `tiff_workflow_common.py` must be in the same folder.

### Verify
```powershell
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx
python --version    # Python 3.9+
```
