# Bug Fixes History

This document tracks critical and significant bug fixes applied to the TIFF Workflow project.

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