# Bug Fixes History

This document tracks critical and significant bug fixes applied to the TIFF Workflow project.

## v2.1 - Data Loss Prevention & Stability Fixes

### 🔴 CRITICAL - Thumbnail Page Used as Source Page
**Issue:** `-GenerateThumbnail` read the main image from page `$ThumbPage` instead of page 0.
- **Root Cause:** Commit `6b08bbb` replaced `[0]` with `[$thumbPage]` in the wrong place; `ThumbPage` was intended as the output position for the thumbnail, not the source page.
- **Impact:** Single-page TIFFs failed with "no images defined"; multi-page TIFFs with existing thumbnails compressed the thumbnail instead of the main image, causing silent data loss.
- **Fix:** Always read the main image from `[0]`; `ThumbPage` now controls where the thumbnail is inserted in the output.
- **Files:** `compress_tiff_zip.ps1`

### 🔴 CRITICAL - Rollback + Staging Destroyed Originals
**Issue:** In modes 0/9 with `-StagingDir`, successful files lost their originals when any file in the group errored.
- **Root Cause:** Rollback checked `Test-Path $t.FinalDst`, which is false while files are still in staging, so it "restored" the original from `OLD_TIFFs/` and the subsequent staging move overwrote it.
- **Fix:** Rollback now uses the task result (`Result -like "ERROR*"`) and staging is moved to the final destination *before* rollback runs.
- **Files:** `compress_tiff_zip.ps1`

### 🔴 CRITICAL - Mode 8 Staging Trap Deleted Only Copy
**Issue:** Mode 8 with `-StagingDir` deleted the source before the staging file was moved to the final destination.
- **Root Cause:** The worker deleted the source after verification; if a terminating error occurred before the group-level move, the trap removed the staging files.
- **Fix:** Mode 8 now requires staging, deletes sources only after a successful final move, and prompts for confirmation when using a default temp staging dir.
- **Files:** `compress_tiff_zip.ps1`

### 🔴 CRITICAL - `-DeleteSource` Switch Ignored in New Modes
**Issue:** Passing `-DeleteSource` on the command line had no effect; mode 8 never deleted sources.
- **Root Cause:** Legacy settings block set `$script:DeleteSource = $false`, shadowing the bound parameter value in the new-mode path.
- **Fix:** Changed to `$script:DeleteSource = $DeleteSource.IsPresent` so the CLI flag is honored.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Multi-Page Detection Missed Non-Thumbnail Extra Pages
**Issue:** SafeMode did not skip TIFFs whose extra pages included masks/transparency pages alongside thumbnails.
- **Root Cause:** Subfiletype check only looked at `IFD1` or compared a single numeric value, missing pages with symbolic types like `MASK`.
- **Fix:** Check all extra pages using `magick identify -format "%[tiff:subfiletype]\n"` and accept only `REDUCEDIMAGE`/`REDUCED` as thumbnails.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Output Directory Not Created (Modes 1/3/5/6/7)
**Issue:** Modes that create subfolders failed on clean trees.
- **Root Cause:** `Group-Object` keyed on the parent directory and only that parent was created, never the actual output folder.
- **Fix:** Ensure the full `FinalDst` directory exists when building tasks.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Wrong inputRoot Broke Modes 6/7 and Relative Mode 2
**Issue:** Path resolution used each file's own directory as the input root.
- **Root Cause:** `$fileInputRoot = $f.DirectoryName` discarded the user-provided root.
- **Fix:** Propagate the original `$inputRoot` from file discovery via a new `InputRoot` note property.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - copy_exif Left UUID Files Without StagingDir
**Issue:** `copy_exif` with `-CompressZip` and no `-StagingDir` wrote files with UUID prefixes that were never renamed.
- **Root Cause:** Rename/move block was gated by `$StagingDir`.
- **Fix:** Always rename staging files to final names when compression is enabled.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟠 HIGH - _is_real_16bit False Positives Reintroduced
**Issue:** Worker exceptions in `run_diagnose_tiffs` were reported as "real 16-bit".
- **Root Cause:** Exception handler set `is_real = True`.
- **Fix:** Set `is_real = False` and `detail = f"ERROR: {e}"`.
- **Files:** `convert_tiff.py`

### 🟠 HIGH - Timeout/Process Group Handling in Python
**Issue:** `run_subprocess` timeout only fired between output lines; `CREATE_NEW_PROCESS_GROUP` broke Ctrl+C and did not kill child processes.
- **Root Cause:** Blocking `readline()` loop and incorrect use of process group flag.
- **Fix:** Reader thread + `process.wait(timeout)`; use `taskkill /F /T /PID` on Windows; handle `KeyboardInterrupt`.
- **Files:** `convert_tiff.py`

### 🟠 HIGH - Thumbnail SubfileType Marker Broken
**Issue:** Generated thumbnails were never marked as `ReducedResolution`, so `SkipCompressedWithThumb` and multi-page detection never matched.
- **Root Cause:** Used `SubfileType=ReducedResolution` string (PrintConv error) and compared `%[tiff:subfiletype]` to `"1"`.
- **Fix:** Write `-IFD1:SubfileType#=1` and read symbolic subfiletype strings (`REDUCEDIMAGE`/`REDUCED`) with ImageMagick.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Diagnose/Purge Broke on Multi-Page TIFFs
**Issue:** `%z`, `%w`, `%h` concatenate per page, producing bogus depths/dimensions; `magick compare` compared all pages.
- **Fix:** Use `file[0]` for single-page metadata and comparison.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - `$script:cleanupFiles` in PS7 Parallel Runspace
**Issue:** `copy_exif_ps7` referenced `$script:cleanupFiles` inside `-Parallel`, which is null in the local runspace.
- **Fix:** Removed runspace-scoped cleanup; intermediate copied TIFFs are returned and cleaned up by the parent loop.
- **Files:** `copy_exif_to_TIFF_ps7.ps1`

### 🟡 MEDIUM - Mode 2 Duplicated Files Every Run
**Issue:** Mode 2 generated random UUID suffixes on collisions, creating new files each run.
- **Fix:** Added `-DuplicateAction` parameter (`Skip | Numbered | Overwrite`, default `Numbered`) producing predictable `v2`, `v3`, etc. suffixes.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Padded Compression Without Integrity Check
**Issue:** `_compress_padded_files` replaced originals without verifying the compressed output.
- **Fix:** Added decode check and dimension comparison before overwriting.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - JPEG Search in Subfolders Never Worked
**Issue:** `Find-JpegPair` searched `JPEG/`/`JPG/` subfolders, but the JPEG index was built non-recursively.
- **Fix:** JPEG index is now always built recursively.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟢 LOW - `-ThumbFormat` Ignored
**Issue:** Thumbnail format parameter was declared but unused; thumbnails were always JPEG.
- **Fix:** Use `$thumbFormat` for the temporary thumbnail file and magick format prefix, pass it explicitly into `Process-TiffJob`, and fix the missing `$using:ThumbFormat` in the PS7 parallel block.
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - SubfileType Marker Used Wrong IFD for `ThumbPage=0`
**Issue:** When `-ThumbPage 0` was used, the thumbnail page was still written to `IFD1`, so the marker (`IFD1:SubfileType#=1`) could mark the main image.
- **Fix:** Choose `IFD0` when `ThumbPage -le 0`, otherwise `IFD1`.
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - Mode 8 Default Temp Staging Not Cleaned Up on Interrupt
**Issue:** If mode 8 used the default temp staging directory and the script was interrupted, the staging directory could be left behind.
- **Fix:** Add the default temp staging directory to `$script:cleanupDirs`.
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - `MagickTimeout` Hard-Coded
**Issue:** The `$script:MagickTimeout` value was hard-coded to 30 seconds with no CLI override.
- **Fix:** Added `-MagickTimeout` parameter (default 30) to `compress_tiff_zip.ps1`.
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - Multi-Page False Positives in `copy_exif`
**Issue:** TIFFs with a single logical image plus thumbnail/MASK pages were skipped as multi-page.
- **Fix:** Apply the same `tiff:subfiletype` heuristic used by `compress_tiff_zip.ps1`; treat `REDUCEDIMAGE`/`REDUCED`/`MASK`/`PAGE` pages as non-independent.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟢 LOW - Duplicate `get_dimensions` Call in `_compare_tiff_metadata`
**Issue:** Dimensions were fetched twice, duplicating work and leaving redundant code.
- **Fix:** Removed the second identical call pair.
- **Files:** `convert_tiff.py`

### 🟢 LOW - Wizard Prompts Misleading
**Issue:** "Safe mode" and "Skip LZW" prompts described the opposite behavior.
- **Fix:** Rewrote prompts to match actual effects.
- **Files:** `convert_tiff.py`

---

## v1.2.2 - Critical Fixes & Regression Repair (2024-04-30)

### 🔴 CRITICAL - Data Loss Regression (Fixed in fba6801)
**Issue:** `copy_exif_to_TIFF_ps7.ps1` OutputDir files deleted after successful EXIF copy
- **Root Cause:** `try/finally` block (introduced in fdfa074) unconditionally deleted `destTiff` on ALL returns, including success paths
- **Impact:** Files copied to OutputDir with EXIF applied were silently deleted when:
  - Using `-OutputDir` without `-CompressZip` (output file deleted)
  - Using `-OutputDir` with already-compressed source (OK+SKIP-ZIP path)
- **Fix:** Removed blanket `try/finally`, added explicit cleanup only for intermediate `destTiff`:
  - **Preserve:** OK (no compression), OK+SKIP-ZIP (already Deflate)
  - **Delete:** Error paths, OK+ZIP (intermediate), SKIP exists (copied unnecessarily)
- **Files:** `copy_exif_to_TIFF_ps7.ps1`, `copy_exif_to_TIFF_ps5.ps1`

### 🔴 CRITICAL - Orphaned PowerShell Jobs (Fixed in 6b08bbb)
**Issue:** `Start-Job` instances not removed on timeout in parallel processing
- **Root Cause:** If `Receive-Job` failed after timeout, `Remove-Job` never executed
- **Impact:** Memory leak with orphaned `magick identify` processes accumulating
- **Fix:** Wrapped all `Start-Job` calls in `try/finally` to guarantee `Remove-Job -Force`
- **Files:** `compress_tiff_zip.ps1` (3 locations: Process-TiffJob, legacy parallel, PS7 parallel)

### 🔴 CRITICAL - Thumbnail Page Ignored (Fixed in 6b08bbb)
**Issue:** `-ThumbPage` parameter completely ignored, always used page 0
- **Root Cause:** Hardcoded `"$srcPath[0]"` instead of using `$thumbPage` variable
- **Impact:** Users could not select which TIFF page to use for thumbnail generation
- **Fix:** Replaced `[0]` with `[$thumbPage]` in both sequential and parallel paths
- **Files:** `compress_tiff_zip.ps1`

### 🔴 CRITICAL - Orphaned Child Processes on Timeout (Fixed in 6b08bbb)
**Issue:** `magick`/`exiftool` child processes survived parent kill
- **Root Cause:** `process.kill()` only killed Python process, not Windows child processes
- **Impact:** Zombie processes consuming CPU/memory after timeout
- **Fix:** Added `subprocess.CREATE_NEW_PROCESS_GROUP` flag for Windows
- **Files:** `convert_tiff.py`

### 🟠 HIGH - Parallel Processing Disabled for Thumbnails (Fixed in 05df501)
**Issue:** `-GenerateThumbnail` forced sequential processing in PS7
- **Root Cause:** `$useParallel = $IS_PS7 -and -not $GenerateThumbnail` blocked all parallelism
- **Impact:** 1300+ files processed sequentially on 5950X = massive performance loss
- **Fix:** Removed restriction, added thumbnail generation logic inside PS7 `-Parallel` block
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Fake ZIP Verification (Fixed in fdfa074)
**Issue:** ZIP integrity check only read header, not actual pixels
- **Root Cause:** Used `magick identify` (metadata only) instead of forcing full decode
- **Impact:** Corrupted ZIPs could pass verification in Mode 8 (delete source), causing data loss
- **Fix:** Replaced with `magick convert "$p" null:` which forces pixel decode
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - PS5/PS7 Divergence in Multi-Page Handling (Fixed in 72825cd)
**Issue:** PS7 parallel path ignored subfiletype=1 (thumbnail) detection
- **Root Cause:** PS7 parallel block always skipped multi-page TIFFs without checking if pages were thumbnails
- **Impact:** Same file produced different results in PS5 vs PS7
- **Fix:** Added subfiletype check in PS7 parallel path to match PS5 behavior
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Race Condition in Cleanup (Fixed in fdfa074)
**Issue:** `$script:cleanupFiles += $destTiff` in PS7 `-Parallel` used local runscope
- **Root Cause:** `$script:` inside `-Parallel` block references local runspace, not parent
- **Impact:** Ctrl+C during parallel processing left orphaned files in OutputDir
- **Fix:** Migrated to `try/finally` local cleanup, replaced array with `ConcurrentBag`
- **Files:** `copy_exif_to_TIFF_ps7.ps1`, `compress_tiff_zip.ps1`

### 🟡 MEDIUM - IndentationError in Python (Fixed in 72825cd)
**Issue:** `convert_tiff.py` had `IndentationError` at line 707
- **Root Cause:** `if not f.exists():` at same indentation as `for f in old_path.glob("*"):`
- **Impact:** Entire module failed to import - wizard completely broken
- **Fix:** Indented loop body correctly (4 spaces)
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - Unbalanced Braces in PowerShell (Fixed in 72825cd)
**Issue:** `compress_tiff_zip.ps1` had 2 extra closing braces
- **Root Cause:** Merge conflict resolution left orphaned braces and duplicate logic block
- **Impact:** PowerShell parser rejected entire script
- **Fix:** Removed duplicate `if (-not $hasOnlyThumbnails)` block and orphaned braces
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Double Underscore in Mode 4 (Fixed in 72825cd)
**Issue:** Mode 4 produced `photo__ZIP` instead of `photo_ZIP`
- **Root Cause:** Default `$ZipSuffix = "_ZIP"` + code concatenation `"_$zipSuffix"` = double underscore
- **Fix:** Removed underscore from concatenation template
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Broken Mode 7 Loop (Fixed in 72825cd)
**Issue:** Mode 7 never produced output
- **Root Cause:** `break` after first `if` prevented finding second index
- **Fix:** Removed `break` statements, added condition to break only when both indices found
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - _is_real_16bit False Positives (Fixed in 05df501)
**Issue:** Failures reported as "real 16-bit" instead of errors
- **Root Cause:** `True, 1.0` returned for compare failures, timeouts, exceptions
- **Impact:** Corrupted files classified as "genuine 16-bit"
- **Fix:** Return `False, 0.0, "ERROR: ..."` for all failure paths
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - Missing ThumbFormat Parameter (Fixed in fdfa074)
**Issue:** `compress_tiff_zip.ps1` didn't declare `-ThumbFormat` parameter
- **Root Cause:** Wizard passed `-ThumbFormat` but script didn't accept it
- **Impact:** PowerShell aborted with "parameter not found" when thumbnails enabled
- **Fix:** Added `[string]$ThumbFormat = "jpg"` to parameter block
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - Various Documentation Fixes (Fixed in e4edb96)
- Removed non-existent `LEGACY/` folder from README
- Fixed workflow count: "Eight" (was "Nine")
- Fixed mode range: "0-9" (was "0-8")
- Replaced "Planned" with actual parameters in copy_exif docs
- Created `README_generate_thumbnails.md`

## v1.2.1 - Claude AI Analysis Fixes (d798b69)

Original fixes from Claude AI analysis (some introduced regressions fixed in v1.2.2 above):
- Fixed subfiletype check for multi-page TIFFs
- Added ZIP integrity verification (initial implementation)
- Fixed array handling for pageCountStr
- Added StagingDir cleanup filters
- Various syntax fixes

---

## Notes

- All fixes tested with `python -m py_compile` for Python files
- All fixes tested with `[System.Management.Automation.PSParser]::Tokenize()` for PowerShell files
- Tested against real TIFF files from `E:\TIFF16` (21 files, ~250MB each)
- Commit `fba6801` validates the regression fix with explicit per-return cleanup