# copy_exif_to_tiff.py -- TIFF Metadata Restoration

Copy EXIF metadata from JPEG to TIFF. Used in Fuji S3/S5 Pro workflows where Hyper Utility exports TIFFs without camera metadata.

**Status:** Implemented. The `convert_tiff.py` wizard runs `copy_exif_to_tiff.py`.

---

## Script

One script, **`copy_exif_to_tiff.py`**, callable directly or via the `convert_tiff.py` wizard.
It works on several TIFFs at once by default (`-Workers`), on any machine; `-Workers 1` runs
one at a time.

Until v2.9.1 there were two PowerShell scripts, `copy_exif_to_TIFF_ps5.ps1` (sequential) and
`copy_exif_to_TIFF_ps7.ps1` (parallel); that edition is described in
[powershell_edition.md](powershell_edition.md). The command line is the same -- only the way to
start it changed.

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
python copy_exif_to_tiff.py -InputDir .

# With worker count
python copy_exif_to_tiff.py -InputDir . -Workers 12

# One TIFF at a time
python copy_exif_to_tiff.py -InputDir . -Workers 1

# Dry-run
python copy_exif_to_tiff.py -InputDir . -DryRun

# Skip TIFFs that already carry camera EXIF (default: overwrite existing EXIF)
python copy_exif_to_tiff.py -InputDir . -SkipIfTiffHasExif

# Compress TIFF to ZIP after copying EXIF
python copy_exif_to_tiff.py -InputDir . -CompressZip

# Overwrite existing output
python copy_exif_to_tiff.py -InputDir . -Overwrite
```

The parameters keep the form they had in the `.ps1` scripts: `-Name value`, `-Switch`. Names
are matched without case, and an unambiguous prefix of a name works. A command line that cannot
be read ends the run with exit code 1 before anything is touched.

---

## Parameters

The defaults below are the `PARAMS` table at the top of `copy_exif_to_tiff.py`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-InputDir` | string | `""` | Folder containing JPEG and TIFF pairs. Empty = the current folder |
| `-InputDirFile` | string | `""` | UTF-8 text file with one input folder per line, used instead of `-InputDir` (not together). The wizard switches to it automatically when the folder list would exceed the Windows command-line limit. |
| `-OutputDir` | string | `""` | Output folder for processed TIFFs. **If set and different from source folder, the original TIFF is preserved** — a copy is created in OutputDir and EXIF is applied to the copy |
| `-Workers` | int | `16` | TIFFs processed at once (1-64). `1` = one at a time |
| `-WorkerMemoryFraction` | double | `0.8` | Cap parallel workers so their ImageMagick compares fit in this share of the memory still free (0 = no cap; with `-Workers 1` there is nothing to cap) |
| `-DryRun` | switch | off | Show what would be copied, don't modify anything. No folder is created either, `-OutputDir` included |
| `-SkipIfTiffHasExif` | switch | off | Skip TIFFs that already carry camera EXIF (an Exif IFD: exposure, aperture, dates). The basic tags every TIFF has -- size, resolution, orientation, and the Make/Model some converters write -- do not count. Before v2.7.1 they did, and the option skipped every TIFF |
| `-CompressZip` | switch | off | Compress TIFF to ZIP after copying EXIF |
| `-Overwrite` | switch | off | Overwrite existing TIFFs |

**Memory and `-Workers`.** The `-CompressZip` flow proves the staged ZIP is
pixel-identical to the TIFF it replaces with a `magick compare`, which holds ~68 bytes per
pixel -- measured at **5.35 GiB on an 84 MP scan** -- and up to `-Workers` of those ran at
once with no memory limit. Before the workers of a group start, the run reads the largest TIFF
of the group; if fewer workers than requested fit in `-WorkerMemoryFraction` of the free
memory, it logs `Memory: ~N GB per worker | N GB available | workers N` and
`Workers reduced from N to M`, and runs at M. A size that cannot be read counts as 150 MP
(never shrinks the estimate); if the available memory cannot be read, the count is left
alone. `-DryRun` skips the cap entirely (nothing runs).

---

## How It Works

1. Scan input folder for TIFF files
2. For each TIFF, find the matching JPEG or RAF (see Pairing below)
3. Extract EXIF from the source using exiftool
4. **If `-OutputDir` is specified and different from source folder:** copy TIFF to OutputDir first, then write EXIF to the copy (original is preserved)
5. **Otherwise:** write EXIF directly to the original TIFF
6. If `-CompressZip` is set, compress the TIFF (with EXIF) to ZIP format; the ZIP replaces the
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

**Note on `-ExcludeFolders`:** the Copy EXIF backend accepts the flag for consistency with the
other backends, but it reads each `-InputDir` folder **non-recursively** -- there is nothing below
its roots to exclude, so the flag only really bites in the recursive compression modes.

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
