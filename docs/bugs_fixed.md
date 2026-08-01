# Bug Fixes History

This document tracks critical and significant bug fixes applied to the TIFF Workflow project.

## v2.6 - Audit Round 7

A full read of all five files. Ten findings; the five that mattered were reproduced on disk
before being touched, and every fix is pinned by a test that fails when reverted. Four of the
five are the same story as round 6: **a fix that landed in one file and was never generalised**.

### 🔴 HIGH - `copy_exif` Reported Success for a Folder That Does Not Exist
**Issue:** The v2.5 exit-code fix ("a bad path in a `;` list must exit 1") was applied to
`compress_tiff_zip.ps1` and `generate_thumbnails.ps1` and never to the two `copy_exif` scripts,
which had **no input validation at all**. `Get-ChildItem` on a missing path raises a
*non-terminating* error, so it never became an `ERROR` line and never reached `$errTotal`:

```
copy_exif_to_TIFF_ps5.ps1 -InputDir "<does not exist>"   ->  0 errors, EXIT 0
copy_exif_to_TIFF_ps7.ps1 -InputDir "<valid>;<missing>"  ->  0 errors, EXIT 0
```

`convert_tiff.py` gates Step 2 of workflow 4 on that exit code, so a stale or mistyped AutoFind
folder made Copy EXIF "succeed" without touching a file and Compress ran anyway.
- **Fix:** Both scripts now resolve, validate and report each root exactly like the other two
  backends: missing roots are logged as `ERROR`, counted in `$errTotal`, and a run with no valid
  root exits 1. Verified on both hosts: bad path -> 1, `valid;bad` -> 1, valid empty folder -> 0.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟡 MEDIUM - `-Remove` Could Not Find the Thumbnails `-Remove`'s Own Flags Had Created
**Issue:** Generation resolves a relative `-OutputDir` **per file directory**
(`Join-Path $f.DirectoryName $OutputDir`); removal resolved it **per input root**. With
`-Recursive` the thumbnails land in `<any subfolder>\<OutputDir>`, so the identical command with
`-Remove` looked in `<root>\<OutputDir>`, a folder that was never created, reported
"No thumbnails found" and exited 0 with every thumbnail still on disk.
- **Fix:** A relative `-OutputDir` now expands to every matching subfolder under each input root
  when `-Recursive` is set. Search roots can nest, so the collected list is de-duplicated --
  deleting one file twice would have counted a phantom error.
- **Files:** `generate_thumbnails.ps1`

### 🟡 MEDIUM - The Collision Rename Escaped Both the Self-Exclusion and `-Remove`
**Issue:** The `_v2` numbering added in v2.3 produces `photo_thumb_v2.tif`, but both filters
matched only `_thumb(-\d+)?$`. So a renamed **.tif** thumbnail was neither excluded from the
input scan nor removable: the next run treated it as a source and produced
`photo_thumb_v2_thumb.tif` -- the `_thumb_thumb` bug v2.3 fixed, coming back through a feature
added in the same round.
- **Fix:** One `$script:ThumbNamePattern` (`_thumb(_v\d+)?(-\d+)?$`) shared by the input scan and
  the `-Remove` filter, covering all four shapes this script can emit (`_thumb`, `_thumb-0`,
  `_thumb_v2`, `_thumb_v2-0`).
- **Files:** `generate_thumbnails.ps1`

### 🟡 MEDIUM - One Unmovable File Aborted the Whole OLD_TIFFs Restore
**Issue:** `run_undo_old_tiffs` called `_safe_move` bare. A locked, read-only or otherwise
unmovable destination raised straight out of the workflow: files queued after it were never
attempted, no summary was printed, and since `main()` only catches `KeyboardInterrupt` the
traceback killed the wizard. `run_purge_old_tiffs` already worked per-file.
- **Fix:** Each move is guarded, failures are reported and counted, the run continues, and the
  workflow returns `False` when anything failed. `rmdir` on the empty OLD_TIFFs folders is
  guarded the same way.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - Legacy Mode Recompressed the Pristine Originals in `OLD_TIFFs/`
**Issue:** Every mode >= 0 excludes `OLD_TIFFs` (the `Get-Files-Mode8` filter, the guard in the
modes 0/9 task loop). Legacy mode scanned `$PWD` recursively with **no exclusion at all**, so
running it in a folder already processed by mode 0/9 rewrote the backups in place -- the one
copy that exists specifically to stay untouched (`None` -> `Zip`, verified).
- **Fix:** The legacy file scan carries the same `OLD_TIFFs` exclusion as the other modes.
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - Probe and Hygiene Fixes
- The modes 0/9 pre-check ran the only bare `magick identify` left in the script, so a corrupted
  file could hang the whole run there while the three workers used the timeout wrapper for the
  same probe. It now uses `Invoke-MagickWithTimeout` and the workers' output handling.
  (`compress_tiff_zip.ps1`)
- `exiftool -Compression` prints one line per IFD, and `-match` on an array is truthy when **any**
  element matches: an uncompressed main image with a compressed thumbnail page was skipped as
  "already compressed", and the log printed `SKIP (None Deflate/Adobe)`. All four probe sites now
  decide on IFD0, and the task carries the collapsed value. (`compress_tiff_zip.ps1`)
- Workflow 8 accepted a `;` in the input and output paths that `step_folder` rejects for every
  other workflow, even though the backend splits `-InputDir` on it. (`convert_tiff.py`)
- The staging move fell back to `<writeDir>\<tif.Name>` when this run had staged nothing for a
  TIFF. Every name this run stages carries the run-scoped prefix, so that file belongs to another
  session -- it would have been moved on top of the user's TIFF. Fallback removed.
  (`copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`)
- Dead `if ($tiffCopied) { Remove-Item ... }` inside a branch guarded by `-not $tiffCopied`.
  (`copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`)

**Checked and cleared** (suspected, then disproved): the legacy parallel worker's `$stagingName`
is assigned early, so staged files are not orphaned; ImageMagick handles the `[0]` frame selector
on filenames that contain brackets; and the `errorSrcPaths` / `IntegrityFailed` interaction in
mode 8 cannot affect rollback, which only applies to modes 0/9.

**Verification:** four `.ps1` parse clean under 5.1 and 7 and stay pure ASCII without BOM;
`pytest` 128/128 (17 new) and `Invoke-Pester` 82/82 (6 new). Each of the five reproduced bugs was
demonstrated before the fix, re-run after it, and mutation-checked: the fix was reverted in the
working tree, the new test was confirmed to fail, and the file restored and md5-compared.

## v2.5 - Audit Round 6

Round 6 was a **class-directed** audit rather than a free read: the five recurring failure
classes from rounds 1-5 (destination collision, data loss, PS5/PS7 divergence, path
resolution, exit code) were swept across all ten modes and all five scripts. Every finding
below is an instance of a class that had already been fixed *somewhere else* — the fixes had
never been generalised, and no regression test pinned them.

### 🔴 CRITICAL - Legacy In-Place Compression Silently Stripped All EXIF
**Issue:** With no `-StagingDir` and no output folder, `$writeDst` equals `$srcPath`. `magick`
overwrote the original, and the EXIF restore that follows then ran
`-tagsfromfile <source> -all:all` against the **already-compressed file** — so it copied
nothing. Every tag (Make, Model, Artist, XMP, IPTC) was lost, reported as `OK` with `0 warnings`.
Reproduced on both PS5 and PS7, with and without `-GenerateThumbnail`; the same applied to
Mode 2 without `-OutputDir`, where files at the input root also resolve to an in-place write.
- **Fix:** New `Backup-TiffMetadata` parks the source metadata in a temp MIE container before
  compression whenever the write target is the source, and the restore reads from that
  container. Cleaned up on every exit path, including the `magick` error returns.
- **Files:** `compress_tiff_zip.ps1` (all three workers: `Process-TiffJob`, legacy `-Parallel`, mode `-Parallel`)

### 🟠 HIGH - `return` at Script Scope Reported Exit 0 With Errors Recorded
**Issue:** Two early exits used `return`, which at script scope always yields exit code **0**,
discarding the `$errTotal -gt 0 -> exit 1` contract. Reachable two ways: a bad path in a `;`
list when the valid folders hold no TIFFs, and — worse — modes 0/9 failing *every* file in the
pre-check loop, which empties `$tasks` and lands on the second `return` with N errors logged.
`convert_tiff.py` gates Step 2 on this exit code, so a fully failed Step 1 read as success.
Direct regression of the v2.2 fix "Scripts Exited 0 Despite Per-File Errors".
- **Fix:** Both sites now end with `if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }`.
  The same treatment was applied to the two `exit 0` sites in `generate_thumbnails.ps1`, which
  additionally gained `;`-list splitting and missing-directory validation to match the other
  backends. Verified end to end: bad path -> exit 1, valid empty folder -> exit 0.
- **Files:** `compress_tiff_zip.ps1`, `generate_thumbnails.ps1`

### 🟠 HIGH - Purge and Padded-Compress Gates Failed Open
**Issue:** Two gates that authorise destroying data skipped their own check when they could not
read it, instead of refusing:
- `_compare_tiff_metadata` only compared page counts when *both* were readable. An unreadable
  count left page-0 RMSE as the sole evidence, and `run_purge_old_tiffs` **permanently deletes**
  on that verdict — despite the comment stating a lost page must block the purge.
- `_process_single_padded` only compared dimensions when both `identify` calls returned 0 with
  parseable output; any other outcome fell through to `status = "ok"` and the original was
  replaced by a **lossy 16 -> 8-bit** conversion. It also never checked page count at all.
- **Fix:** Both fail closed. Unreadable dimensions yield `dimension_unreadable`, unreadable or
  differing page counts yield `page_count_mismatch`, and a page-count parity check was added to
  the padded path. Matches the fail-closed contract of `Test-ZipIntegrity`/`Get-TiffPageCount`.
- **Files:** `convert_tiff.py`

### 🟠 HIGH - Legacy PS7 Lost EXIF Where PS5 Kept It (`[no thumb]` Fallback)
**Issue:** In legacy mode with `-GenerateThumbnail`, a failed thumbnail made the PS7 `-Parallel`
block `return` immediately, skipping the EXIF restore that the PS5 path (`Process-TiffJob`)
reaches via `$noThumbNote`. Same input, same file: PS7 produced a TIFF with no metadata, PS5
kept it. The mode `>= 0` worker had already been fixed this way; legacy never got the backport.
- **Fix:** The legacy `-Parallel` block now accumulates `$noThumbNote`/`$exifWarn` and falls
  through to the shared EXIF restore, matching the sequential path.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Staging Cleanup Destroyed Other Runs' In-Flight Files
**Issue:** `compress_tiff_zip.ps1` scopes staging cleanup to `$script:runStagingId`; the other
three did not.
- Both `copy_exif` scripts' interrupt trap deleted anything matching `^[0-9a-f]{32}_`, and their
  staging names were bare GUIDs. Ctrl-C in one session wiped a concurrent session's staged output.
- `_compress_padded_files` deleted *every* file in a shared `compress_staging/`. A run caught
  between its `OLD_PADDED` backup and its final move lost the file from the source folder.
- **Fix:** Both `copy_exif` scripts gained a run-scoped `$script:runStagingId` prefix (passed
  into the PS7 runspaces via `$using:`); the Python staging directory is now per-run
  (`compress_staging_<uuid>`) and removed wholesale.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`, `convert_tiff.py`

### 🟡 MEDIUM - `_safe_move` Destroyed the Destination Before It Could Fail
**Issue:** `dst.unlink()` ran before `shutil.move`. A move that then failed (disk full,
permission, cross-device) left **both** files gone. Worst in `run_undo_old_tiffs` with
overwrite enabled. Separately, `shutil.rmtree(dst)` would recursively erase an entire directory
that merely shared a filename.
- **Fix:** An existing destination is parked under a sibling `.bak-<uuid>` name via `os.replace`
  and only unlinked after the move succeeds; on failure it is restored. A directory destination
  now raises `IsADirectoryError` instead of being deleted.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - Rollback Blind to Staging-Move and Mode 8 Delete Failures
**Issue:** `$groupErrs` was measured *before* the staging-move block that increments
`$script:errTotal`, so a modes 0/9 file whose staged output failed to move never triggered the
rollback: the original stayed in `OLD_TIFFs/` with no output and no `ROLLBACK` line. Move
failures were also never added to `$errorSrcPaths`. Mode 8's delete block logged `ERROR` for
"final destination missing" and "failed to delete source" without counting them, so those runs
still exited 0.
- **Fix:** `$groupErrs` is computed immediately before the rollback in both branches, move
  failures register their `$t.Src` in `$errorSrcPaths`, and the mode 8 delete errors increment
  `$script:errTotal`.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Dry-Run -> Real Path Dropped the Step 1 Gate
**Issue:** The first pass refuses to run Step 2 when Copy EXIF fails, but the "run for real now?"
follow-up called both steps unconditionally — a failed Copy EXIF still proceeded to Compress.
Present in both the Rich and plain-input branches.
- **Fix:** Both branches apply the same gate as the first pass.
- **Files:** `convert_tiff.py`

### 🟢 LOW - Reporting and Verification Hygiene
- Purge verified non-TIFF sidecars by **size only**; a same-size sidecar with different content
  passed and was deleted. Now compared by streamed SHA-256 (`_file_digest`). (`convert_tiff.py`)
- `run_undo_old_tiffs` counted every file in the header but only moved `.tif`/`.tiff`, promising
  more than the run delivered. Counts now match what is moved. (`convert_tiff.py`)
- `Process-Results` carried a dead `^OK\+SKIP-ZIP` branch; that result string belongs to
  `copy_exif` and is never emitted here. (`compress_tiff_zip.ps1`)
- The non-SafeMode legacy summary omitted `warnTotal`. (`compress_tiff_zip.ps1`)
- The PS7 `MULTI` return omitted `SrcPath`/`CopiedTiffPath` that every sibling return carries.
  Harmless today (`$copiedTiffPath` is still `$null` there and the consumer is guarded by
  `StagingName`), but the uneven shape is exactly how the rollback bugs above started.
  (`copy_exif_to_TIFF_ps7.ps1`)

### Regression coverage

Every finding above is an instance of a class that had already been fixed elsewhere and was never
pinned by a test, so round 6 closes with `tests/test_regression_classes.py` — one group per class,
each written to fail when the fix is reverted:

| Class | Coverage |
|---|---|
| Exit code | `compress_tiff_zip.ps1` and `generate_thumbnails.ps1` driven end to end on both shells: bad path in a `;` list → 1, valid empty folder → 0, every file failing the mode 0 pre-check → 1. Plus a static AST guard against `return` at script scope in all four backends. |
| Data loss | In-place compression of a real TIFF across all three workers (`Process-TiffJob`, legacy `-Parallel`, mode `-Parallel`) × both shells, asserting the EXIF survives and the file really was compressed. |
| PS5/PS7 divergence | The `[no thumb]` fallback forced via an unencodable `-ThumbFormat`, sequential and parallel, both shells. Plus an AST check that every `-Parallel` block re-injects — transitively — each script-level helper it calls. |
| Fail-closed gates | `_compare_tiff_metadata` and `_process_single_padded` with `identify` returning non-zero, empty and unparseable output on either side, asserting no deletion or replacement is authorised, and that the happy path still is. |
| Staging / collision | Run-scoped staging is not shared between runs and does not delete a concurrent run's files; `_safe_move` restores the destination when the move fails and refuses a directory destination. |

The PowerShell behaviour is exercised from pytest through the real shells rather than from Pester,
because it needs real TIFFs and both hosts; the Pester suites stay what they are, static pattern
checks over the sources. Tests requiring a shell or ImageMagick/exiftool skip when those are
absent. The static AST checks live in `tests/helpers/Find-PsInvariantViolations.ps1`.

### Pester suites unblocked and refreshed

Pester 6.0.1 was installed (`Install-Module Pester -Force -SkipPublisherCheck -Scope CurrentUser`),
so the five `.ps1` suites ran for the first time since v2.1 — and 17 tests failed. All 17 were
**stale assertions**, not regressions: they pinned code shapes that rounds 4-6 deliberately
replaced (`[int]` cast → `[int]::TryParse`; `magick "$p" null:` → `Invoke-MagickWithTimeout`;
`Start-Job`'s `$jobOutput` → the runspace helper's `TimedOut`/`ExitCode`; `$stagingUsed` →
`$verifyTarget`; collision numbering restricted to modes 4-7 → every mode; `_thumb$` →
`_thumb(-\d+)?$`; `$destPath`/`$tif.Name` → the `$destNameMap` fallback). Each was rewritten to
pin the *current* invariant rather than the old syntax.

One was the opposite of stale: `No-Thumb StagingName` asserted that every `[no thumb]` path
returns early with a `StagingName` — the exact shape the CRITICAL/HIGH fixes above removed. It is
now inverted: no worker may return there, and all three must accumulate `$noThumbNote` and fall
through to the EXIF restore.

**Verification:** all four `.ps1` files parse clean under both PowerShell 5.1 and 7 and remain
pure ASCII without BOM (the v2.4 constraint); `pytest` 111/111 (52 existing + 59 new) and
`Invoke-Pester` 76/76; modes 0-9 swept on both shells with real TIFFs, exit 0 throughout, EXIF
preserved in-place on every worker, and no leaked temp files. Each new regression test was
mutation-checked by reverting the corresponding v2.5 fix and confirming the test fails.

## v2.4 - Audit Round 5

### 🔴 CRITICAL - `copy_exif_to_TIFF_ps5.ps1` Did Not Run on PowerShell 5.1 At All
**Issue:** The script is saved as UTF-8 **without BOM** but contains non-ASCII characters (`─`, `═`, `—`, `→`). Windows PowerShell 5.1 decodes BOM-less files as ANSI (cp1252), so `─` and `—` become `â”`/`â€”` — and `”` (U+201D) is a **string delimiter** for the PowerShell parser. Every affected string terminated early:
```
Token 'skipped' inesperado na expressão ou instrução.
+ "MULTI ($pageCount IFDs â€” skipped) | $($p.T ...
```
The PS5-only script — whose entire reason to exist is PowerShell 5.1 — failed to parse before executing a single line. It was invisible in review because PS7 (used by the tokenizer checks in `Notes`) reads BOM-less files as UTF-8.
- **Fix:** All four `.ps1` files are now pure ASCII (`─`→`-`, `═`→`=`, `—`→`--`, `→`→`->`). Verified by running the script end-to-end under `powershell.exe` 5.1.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`, `compress_tiff_zip.ps1`

### 🔴 CRITICAL - Modes 0/9: Multi-Page TIFFs Left Stranded in `OLD_TIFFs/`
**Issue:** The pre-check moved the original to `OLD_TIFFs/` and only then queued the task; SafeMode's multi-page test ran later, inside the worker. A multi-page TIFF was therefore rejected *after* being moved, and since `MULTI` is not an `ERROR`, the rollback never restored it. The log said "skipped" while the file had disappeared from its folder, leaving nothing in the parent — the opposite of the documented "multi-page TIFFs are not touched".
- **Fix:** The page-count/subfiletype check now runs in the task-building loop, before `Move-Item`. Rejected files are reported as `MULTI (n pages - skipped, original untouched)` and never moved.
- **Files:** `compress_tiff_zip.ps1`

### 🔴 CRITICAL - `photo.tif` + `photo.tiff` Overwrote Each Other (Modes 0/1/3/8/9)
**Issue:** `Resolve-Output` always returns `<stem>.tif`, but destination de-duplication existed only for mode 2 and modes 4-7. With both extensions present, two parallel workers wrote the same output file simultaneously, `$stagingMap` (keyed by destination) kept only the last entry — orphaning one staged file — and in mode 8 the `.tiff` source was deleted after the move. One of the two images was lost.
- **Fix:** The claimed-destination map now covers every mode except 2 (which keeps its own `-DuplicateAction` policy). The later claimant becomes `_v2`, `_v3`, ...
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Mode 8 Skipped Its Own Integrity Gate on Three Paths
**Issue:** The docs promise the staged ZIP is *always* verified with `magick ... null:` before replacing the original. But the `[no thumb]` fallbacks (thumbnail generation or merge failure) and the `WARN (exiftool failed, ZIP ok)` path returned early, before the mode 8 block — while still setting `StagingName`, so the move loop happily overwrote the original with a file that was never decoded.
- **Fix:** Those paths no longer return early; they set `$noThumbNote` / `$exifWarn` and fall through to a single tail that always runs the integrity gate and builds the result string.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Wizard Had No Output Folder for Mode 2
**Issue:** `build_compress_command()` never emitted `-OutputDir`. Mode 2 ("merge all TIFFs into a single output folder") therefore always ran with an empty `-OutputDir`, which the backend resolves to the *input root*: every TIFF from every subfolder was dumped next to the sources, and root-level TIFFs were recompressed in place (`magick photo.tif ... photo.tif`) with no backup.
- **Fix:** The wizard asks for the output folder when mode 2 is selected (default `<input>\ZIP_flat`), warns and requires confirmation if it equals the input folder, and also exposes `-DuplicateAction`. `build_compress_command()` now passes `-OutputDir` and `-DuplicateAction`.
- **Files:** `convert_tiff.py`

### 🟠 HIGH - `copy_exif` `-OutputDir` Merged Every Session Into One Folder
**Issue:** With AutoFind (or a `;` list) all sessions share a single `-OutputDir`, so a second session holding `DSC_0001.tif` hit `SKIP (exists in OutputDir)` and silently produced nothing (or overwrote the first with `-Overwrite`). None of the numbering added to `compress_tiff_zip.ps1` in v2.2/v2.3 existed here.
- **Fix:** Destinations are claimed per run across folders; later claimants get `_v2`, `_v3`, ... with a `RENAME` log line. The staging→final move uses the same mapping.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟠 HIGH - Modes 6/7 Wrote Outside the Source `_EXPORT` Tree
**Issue:** `Resolve-Output` built `<inputRoot>\_EXPORT\<ZIP>\...`, discarding the path between the root and the actual `_EXPORT`. `root\2024\JobA\_EXPORT\shot.tif` and `root\2024\JobB\_EXPORT\shot.tif` both landed in `root\_EXPORT\ZIP\` (one renamed `_v2`), inside an `_EXPORT` folder created at the root that never existed. The documented mapping is `_EXPORT/photo.tif → _EXPORT/ZIP/photo.tif`.
- **Fix:** New `Get-PathPrefix` helper rebuilds the real `_EXPORT` location from the path segments; each tree now gets its own `ZIP/`. Mode 7 additionally requires the TIFF subfolder to sit *below* the marker.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - `Start-Job` Per File (Performance)
**Issue:** SafeMode's page count and mode 8's integrity check used `Start-Job`, which spawns a full PowerShell **process** per file (~700 ms measured), inside `ForEach-Object -Parallel`. On the sequential PS5 path that cost was paid back-to-back for every file.
- **Fix:** New `Invoke-MagickWithTimeout` runs `magick` in an in-process runspace with the same timeout semantics (`BeginStop` async, since `Stop()` blocks until the native child exits). `Get-TiffPageCount` and `Test-ZipIntegrity` are built on it and replace all six `Start-Job` call sites; `_Verify-ZipIntegrity` was removed. Arguments are passed as a real array, so no manual quoting. Measured: **5.66 s → 3.60 s (-36%)** on 24 files / 8 workers, identical output.
- **Files:** `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟡 MEDIUM - Modes 0/9 Probed Every File Twice
**Issue:** The exiftool compression probe and (after the fix above) the page count ran once in the task loop and again inside the worker.
- **Fix:** Tasks carry `Comp` and `SafeChecked`; `Process-TiffJob` and the parallel block accept them and skip the redundant probes.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - `_compare_tiff_metadata` Mis-Parsed Scientific-Notation RMSE
**Issue:** The regex fixed in `_is_real_16bit` was never applied here. For `1.23457e+06 (0.0188238)` it matched `06` as the RMSE, so the purge verification (workflow 6) reported a wrong value.
- **Fix:** Same pattern as `_is_real_16bit`, accepting an exponent in the integer part.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - `copy_exif`: JPEG Search in the Parent Folder Was Dead Code
**Issue:** `Find-JpegPair` searched `$parent`, `$parent\JPEG` and `$parent\JPG`, but the JPEG index is built only from `$RootPath` — those three directories could never produce a hit. Half the documented search path silently did nothing (the same class of bug as "JPEG Search in Subfolders Never Worked" in v2.1).
- **Fix:** When the index misses, the filesystem is probed directly for `<base>.jpg` / `<base>.jpeg` in each search directory.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟡 MEDIUM - `generate_thumbnails.ps1`: Collisions, Idempotence and `-Remove`
- **Collisions:** with `-OutputDir` + `-Recursive`, `a\photo.tif` and `b\photo.tif` both mapped to `photo_thumb.jpg`; the second reported `SKIP (exists)` and was never generated. Later claimants now get `_thumb_v2`, `_thumb_v3`, ...
- **Idempotence:** with `-Page all` the "already exists" test checked the unsuffixed path while ImageMagick writes `name_thumb-0.jpg`, `name_thumb-1.jpg`, so every run regenerated everything. New `Test-ThumbExists` also matches suffixed frames.
- **`-Remove`:** now enumerates the thumbnails themselves instead of deriving them from source TIFFs, so orphans (whose TIFF was moved or deleted) are cleaned up, and the counters no longer mix "source files" with "thumbnails removed".
- **`-Workers`:** gained `[ValidateRange(1, 64)]`.
- **Files:** `generate_thumbnails.ps1`

### 🟡 MEDIUM - Wizard Sent `-Workers` Values the Backends Reject
**Issue:** The backends declare `[ValidateRange(1, 64)]`; the wizard accepted any integer, so `-Workers 100` aborted at parameter binding with a raw PowerShell error and the backend never started.
- **Fix:** New `clamp_workers()` used by every command builder and prompt (workflow 7 included), with a message when the value is clamped.
- **Files:** `convert_tiff.py`

### 🟢 LOW - Stability & Hygiene Fixes
- `Test-TiffHasOnlySubfilePages` now fails closed when `identify` returns fewer lines than pages, and honours `-MagickTimeout` (it previously called `magick` with no timeout at all). **Files:** all three PS scripts
- Mode 5 no longer climbs above the input root: a TIFF sitting directly in the root used to produce `<drive>:\ZIP\`, outside the selected tree. **Files:** `compress_tiff_zip.ps1`
- A non-existent `-InputDir` is now an error with exit code 1 instead of falling through to "No TIFF files found" and exit 0 (a typo was indistinguishable from an empty folder). **Files:** `compress_tiff_zip.ps1`
- Files that `Resolve-Output` cannot map are reported as `SKIP` instead of being dropped silently, so the `N/N processed` total closes. **Files:** `compress_tiff_zip.ps1`
- Mode 8's temporary staging directory is removed whenever it is empty (previously kept on any error) and any leftovers are reported with their path instead of being discarded silently. **Files:** `compress_tiff_zip.ps1`
- Workflow 7 (Diagnose) now persists `last_workers` like every other workflow; workflow 8 gained `-OutputDir`, `-Page` and `-Remove`, and validates size/quality/format before starting the backend (invalid `-Quality` used to abort it at binding). **Files:** `convert_tiff.py`
- Dead code removed: unused `$bagL` / `$multiPageBagCapture` in the PS7 parallel block, unused `$markerLower` in `Get-Files-Mode6`. **Files:** `copy_exif_to_TIFF_ps7.ps1`, `compress_tiff_zip.ps1`

---

## v2.3 - Audit Round 4

### 🔴 CRITICAL - copy_exif `-OutputDir` + `-CompressZip` Deleted Its Own Output
**Issue:** With `-OutputDir` (different from the source dir) and `-CompressZip` (without `-Overwrite`), the script copied the TIFF to the output dir, applied EXIF, then tested `Test-Path $finalDst` — but `$finalDst` and `$destTiff` are the same path the script itself just created. It entered the "exists" branch, deleted its own output, and returned `OK+SKIP-ZIP (exists)` producing no file at all.
- **Fix:** The exists-check now skips files copied by the current run (`-and -not $tiffCopied`).
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟠 HIGH - Mode 8: Integrity Check Did Not Protect the Original
**Issue:** For `.tif` sources `finalDst == srcPath`, and the staging→final move loop overwrote the original unconditionally. The verification only ran with `-DeleteSource`, and even when it failed (`CanDeleteSource = $false`) the move still replaced the original — the delete loop's `src == final` guard made it dead code for `.tif` files.
- **Fix:** Mode 8 now always verifies the staged ZIP with `magick file null:` before the move. On failure the worker flags `IntegrityFailed`, the result is reported as `ERROR (ZIP integrity check failed - source preserved)`, the staged file is discarded, and the move loop skips it (exit code 1).
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Modes 4/5: Untreated Destination Collisions
**Issue:** Dedup existed only for mode 2 and modes 6/7. Mode 5 flattens one level (`in\A\x.tif` and `in\B\x.tif` → same output) and mode 4 merges `X`/`X_TIFF` into `X_ZIP`. With staging, one output silently vanished; without staging, two magick processes raced on the same file.
- **Fix:** The collision detection (number later claimants `_v2`, `_v3`, ...) now covers all flattening modes 4–7.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Page Count via `identify -format "%n"` Concatenated Digits
**Issue:** `%n` prints once per frame with no separator: a 3-page TIFF returned `"333"` (log lied), and 10+ pages produced 20+ digit strings that overflowed int32 parsing → spurious `ERROR (page count parse)` instead of a clean MULTI skip.
- **Fix:** Use `"%n\n"` and parse the first line (existing array handling).
- **Files:** `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟡 MEDIUM - Rollback Broken for `ERROR (magick)` (PS7 Parallel Path)
**Issue:** The plain-compression `ERROR (magick)` return was the only one without `SrcPath`, so `errorSrcPaths` never contained it and the OLD_TIFFs rollback silently did not run.
- **Fix:** Added `SrcPath` to that return.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Mode 8 Abort Exited 0
**Issue:** Declining the default staging dir logged `Mode 8 aborted` as ERROR but used `return`, so the wizard/CI saw success.
- **Fix:** `exit 1`.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - PS 5.1: Wizard Generated Invalid Commands
**Issue:** `build_compress_command`/`build_copy_exif_command` pass `-SafeMode:$false`/`-SkipLzwAsCompressed:$true`. `powershell.exe -File` (5.1) cannot bind `:$bool` values from the command line (verified empirically) — the backend failed to start when those defaults were changed.
- **Fix:** For PS5 the wizard wraps the invocation in `-Command "& 'script' ..."` so `$true`/`$false` literals are evaluated by the PowerShell parser; parameter names stay unquoted, values are single-quoted with `''` escaping. PS7 path unchanged.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - Integrity Verification Failed Open on Job Failure
**Issue:** In `_Verify-ZipIntegrity` and the inline parallel check, a failed job returning no output made `[int]$null = 0` → treated as "intact", the exact path that authorizes source destruction in mode 8.
- **Fix:** Empty job output now fails closed (`return $false`).
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Exiftool Argfiles Broke Non-ASCII Paths
**Issue:** Argfiles are written UTF-8 but exiftool interpreted file names in the system ANSI codepage unless told otherwise → paths like `Fotos_João` failed with mass `ERROR (exiftool check)`.
- **Fix:** All 13 argfiles now start with `-charset filename=utf8`.
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - Stability & Hygiene Fixes
- Mode 2: `-Overwrite` no longer renamed to `_v2` under the default `Numbered` action; intra-run duplicates with `DuplicateAction=Overwrite` now drop the earlier task (last claimant wins) instead of racing/orphaning staging files. **Files:** `compress_tiff_zip.ps1`
- Legacy PS7 honors `-SkipCompressedWithThumb`; legacy PS5 staging names use the run-scoped prefix so the interrupt trap cleans them. **Files:** `compress_tiff_zip.ps1`
- Wizard: `Workers = 0` clamped to 1 (was a `ValueError` traceback); `_compress_padded_files` catches worker exceptions per file instead of aborting the batch; `run_subprocess` does a final queue drain (last output lines could be lost); `run_generate_thumbnails` strips pasted quotes, validates `is_dir`, and passes `-Workers`; PS5 output decoding falls back to the console OEM codepage (cp850/cp866, not just ANSI). **Files:** `convert_tiff.py`
- Purge: non-TIFF sidecars in OLD_TIFFs must have an identical-size copy in the parent or the purge is blocked; `_compare_tiff_metadata` now also compares page counts (RMSE only covered page `[0]`, so lost extra pages went unnoticed). **Files:** `convert_tiff.py`
- Last `magick convert` (deprecated IM7) removed from `_process_single_padded`; AutoFind `rglob("*/")` guards `is_dir()` for Python < 3.11. **Files:** `convert_tiff.py`
- `generate_thumbnails.ps1` self-exclusion now also covers multi-frame thumbs (`_thumb-0`, `_thumb-1`, ...).

---

## v2.2 - Audit Round 3

### 🔴 CRITICAL - `[no thumb]` Fallback Orphaned Output in Staging
**Issue:** When thumbnail generation failed, the compressed main image was copied to the write destination but the result carried `StagingName = $null`, so the staging→final move loop skipped it. With `-StagingDir` the final output was missing while the result said "OK" and the original was already in `OLD_TIFFs/` (modes 0/9), with no rollback.
- **Fix:** The `[no thumb]` returns now set `StagingName` to the written file name, mirroring the success path, in `Process-TiffJob` and the PS7 parallel block.
- **Files:** `compress_tiff_zip.ps1`

### 🔴 CRITICAL - `-SkipCompressedWithThumb` Was a No-Op
**Issue:** A compressed TIFF without a thumbnail was always skipped; the flag only changed the log wording, and the check existed only in the PS7 new-mode parallel path.
- **Fix:** All three paths (`Process-TiffJob`, mode 0/9 pre-check, PS7 parallel) now implement consistent semantics: flag off → skip all compressed; flag on → skip only compressed+thumbnail, reprocess compressed-without-thumbnail to add one.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - `-Page all` + jpg/png Reported False Errors
**Issue:** ImageMagick writes `name-0.jpg`, `name-1.jpg`, ... for multi-page input with single-frame formats, but `generate_thumbnails.ps1` tested the exact unsuffixed path and reported `ERROR (output not created)` despite success.
- **Fix:** When the exact path is missing, the script now looks for suffixed frames (`base-*.ext`) and reports OK with the frame count.
- **Files:** `generate_thumbnails.ps1`

### 🟠 HIGH - Modes 6/7 Destination Collisions Across `_EXPORT` Trees
**Issue:** `Resolve-Output` flattened outputs to `<inputRoot>\_EXPORT\ZIP\<rel-after-_EXPORT>\name.tif`, discarding the path between the root and `_EXPORT`. Same-named files under different `_EXPORT` trees collided and were skipped or silently overwritten.
- **Fix:** Claimed destinations are tracked per run; a different source claiming the same destination gets `_v2`, `_v3`, ... with a note in the result.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - `DuplicateAction: Numbered` Ignored On-Disk Duplicates
**Issue:** Mode 2 only numbered intra-run collisions; a name existing only on disk fell through to `SKIP (exists)`, contradicting the documented behavior.
- **Fix:** When `DuplicateAction` is `Numbered` and the destination exists on disk, `_v2`, `_v3`, ... are generated until a free name is found. Skip/Overwrite unchanged.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - `_compress_padded_files` Replaced Originals Without Backup
**Issue:** The lossy 16→8-bit padded-file conversion replaced originals in place with no backup.
- **Fix:** The original is moved to an `OLD_PADDED/` subfolder (with `_v2` collision suffixes) before the converted file is written; backup failure preserves the original.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - Undefined/Before-Assignment Variables
**Issue:** The legacy PS7 parallel block referenced undefined `$srcPath` (only `$src` existed), and `copy_exif_to_TIFF_ps7.ps1` used `$copiedTiffPath` before assignment in MISS/SKIP/DRY returns.
- **Fix:** Corrected the variable reference; `$copiedTiffPath` is now initialized at the top of the parallel block.
- **Files:** `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟡 MEDIUM - Wizard Crashed When PowerShell Executable Missing
**Issue:** `subprocess.Popen` ran outside any try block, so a missing PowerShell executable crashed the wizard with a raw traceback.
- **Fix:** Wrapped in try/except with a handled error message.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - Rich Markup Silently Eaten from Echoed Output
**Issue:** Subprocess lines echoed via `console.print` were parsed as Rich markup; a file named `IMG_[test].tif` printed as `IMG_.tif`.
- **Fix:** All echoed subprocess/user content is escaped with `rich.markup.escape()`.
- **Files:** `convert_tiff.py`

### 🟡 MEDIUM - Legacy Path Divergences (PS5 vs PS7)
**Issue:** The legacy PS7 parallel path ignored `-GenerateThumbnail`, and legacy PS5 forced `.tif` output names while PS7 kept the original extension.
- **Fix:** Legacy PS7 now honors `-GenerateThumbnail`; legacy PS5 keeps the original extension.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - Scripts Exited 0 Despite Per-File Errors
**Issue:** None of the scripts exited non-zero on processing errors, so the wizard/CI could not detect failures.
- **Fix:** All scripts now `exit 1` when the error count > 0 (dry-run errors included), `exit 0` otherwise.
- **Files:** `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`, `generate_thumbnails.ps1`

### 🟢 LOW - Stability & Hygiene Fixes
- Deprecated `magick convert` replaced with IM7 `magick ... null:` in `_Verify-ZipIntegrity`; 0-byte `.tmp` files from `GetTempFileName()` are deleted immediately; mode-8 default temp staging dir is removed after a successful run; mode 8 no longer recurses into `OLD_TIFFs/`; staging files carry a per-run prefix so the trap only deletes files created by this run; `-Workers` validates `[ValidateRange(1, 64)]`; `[int]` casts on `identify` output use `TryParse` with explicit error results; dead `$script:cleanupFiles` trap code removed. **Files:** `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`
- `generate_thumbnails.ps1`: `*_thumb` files excluded from input scans (no more `_thumb_thumb` on re-runs); `-Page`/`-Quality` validated; `-Remove` cleans thumbnails of all formats; magick stderr included in error results. **Files:** `generate_thumbnails.ps1`
- `copy_exif`: `-OutputDir` normalized (trailing slashes) and self-copy guarded. **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`
- `convert_tiff.py`: `--help` now works via argparse; PS5 (OEM codepage) output decoded with UTF-8 → locale fallback; `ps_major`/`ps_name` no longer persisted to config; purge verification only feeds TIFFs to `magick identify`; timeouts lifted to `CONVERT_TIMEOUT_S`/`COMPARE_TIMEOUT_S` constants; pasted paths strip surrounding quotes; `truncate_path` guarantees `max_len`; folder names containing `;` rejected; dead imports removed.
- Docs aligned with code: default thumbnail output location (next to source TIFF), no `-AsJob`, AutoFind exclusions include `OLD_TIFFs`, Python 3.9+ requirement.

---

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

---

## v2.1 - Audit Round 2

### 🔴 CRITICAL - SubfileType Check Still Fail-Open in PS7 New-Mode Parallel
**Issue:** The Audit Round 1 follow-up fixed the fail-closed subfiletype behavior in two of the three checks inside `compress_tiff_zip.ps1`, but the PS7 parallel path for new modes (the default wizard path on PS7) still used `$st -and $st -notin`. Genuine multi-page TIFFs without a subfiletype tag on extra pages were allowed through SafeMode, and `-GenerateThumbnail` silently discarded the extra pages.
- **Fix (Audit Round 2):** Extracted the subfiletype iteration logic into `Test-TiffHasOnlySubfilePages`. The function is placed in `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps5.ps1`, and `copy_exif_to_TIFF_ps7.ps1` (kept inline so each script remains self-contained). All call sites now treat missing/empty subfiletype as non-thumbnail.
- **Fix (Audit Round 2 patch):** Functions defined in the parent scope are not visible inside `ForEach-Object -Parallel` runspaces. The function definition is captured once as `$script:TestSubfileFnDef` and re-injected at the top of every parallel block with `${function:Test-TiffHasOnlySubfilePages} = $using:TestSubfileFnDef`, so the check now works in the PS7 parallel paths as well as the sequential ones.
- **Files:** `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟡 MEDIUM - Different SubfileType Whitelists Between Scripts
**Issue:** `compress_tiff_zip.ps1` rejected `MASK`/`PAGE` extra pages while `copy_exif_to_TIFF_ps*.ps1` accepted them, which could be confusing and was not documented.
- **Rationale:** The difference is intentional. `compress_tiff_zip.ps1` with `-GenerateThumbnail` discards existing extra pages and replaces them with a generated thumbnail, so it only trusts `REDUCEDIMAGE`/`REDUCED`. `copy_exif_to_TIFF_ps*.ps1` preserve all existing pages while copying metadata, so `MASK`/`PAGE` pages are also safe to keep.
- **Fix:** Documented the whitelist difference in `bugs_fixed.md` and the README.
- **Files:** `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`, `docs/bugs_fixed.md`, `docs/README_compress_tiff_zip.md`

---

## v2.1 - Audit Round 1

### 🔴 CRITICAL - PS7 `copy_exif` Deleted Final Outputs with `-OutputDir`
**Issue:** When `copy_exif_to_TIFF_ps7.ps1` was run with `-OutputDir` (and no `-CompressZip`), the final output file was copied, EXIF was applied, the script reported OK, and then the parent loop deleted the output file because every return carried `CopiedTiffPath`.
- **Fix:** Added an explicit `IsIntermediate` flag to parallel returns. The cleanup loop now only removes files marked as intermediate (error paths and OK+ZIP staging paths). OK and OK+SKIP-ZIP final outputs are preserved.
- **Files:** `copy_exif_to_TIFF_ps7.ps1`

### 🔴 CRITICAL - SubfileType Check Failed Open (Multi-Page False Negative)
**Issue:** `compress_tiff_zip.ps1` treated an unset `tiff:subfiletype` as a thumbnail, so genuine multi-page TIFFs without the tag passed SafeMode and had extra pages silently discarded.
- **Fix:** Removed the `$st -and` guard; an empty/missing subfiletype is now treated as non-thumbnail, keeping the fail-closed behavior.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Mode 2 Re-Executions Created GUID-Suffixed Duplicates
**Issue:** `Resolve-Output` mode 2 generated a random GUID suffix whenever the destination file already existed on disk, before `DuplicateAction` was evaluated. Running the script twice produced unpredictable `_a1b2c3d4` files instead of numbered versions.
- **Fix:** Removed the GUID fallback from `Resolve-Output`. `DuplicateAction` (`Skip | Numbered | Overwrite`, default `Numbered`) now handles both intra-run collisions and on-disk duplicates consistently, producing `_v2`, `_v3`, etc.
- **Files:** `compress_tiff_zip.ps1`

### 🟠 HIGH - Mode 6 Path Resolution Broke for Files Directly in `_EXPORT/`
**Issue:** Mode 6 built a relative path without validating that the relative part was non-empty. When a TIFF sat directly inside the `_EXPORT` folder, the PowerShell range `$parts[($exportIdx + 1)..($parts.Count - 1)]` returned the last element instead of an empty array, causing the output path to repeat the `_EXPORT` segment (`_EXPORT/ZIP/_EXPORT/photo.tif`).
- **Fix:** Guard the range so `$relParts` is `@()` when there are no components after `_EXPORT` (or `_EXPORT/TIFF` for Mode 7), and only append the relative path when it actually exists.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - `copy_exif` Multi-Page Detection Only Checked Page `[0]`
**Issue:** The thumbnail/MASK heuristic only looked at the first IFD, so the canonical case (page 0 untagged, page 1 `REDUCEDIMAGE`) was still skipped, while a `PAGE` tag on IFD0 could let a genuine multi-page file through.
- **Fix:** Iterates all extra pages (`1..n`) and only treats the TIFF as single-page when every extra page is `REDUCEDIMAGE`/`REDUCED`/`MASK`/`PAGE`, matching `compress_tiff_zip.ps1`.
- **Files:** `copy_exif_to_TIFF_ps5.ps1`, `copy_exif_to_TIFF_ps7.ps1`

### 🟡 MEDIUM - Legacy Mode (`-1`) Left Files in Staging
**Issue:** Legacy mode populated the staging map but never moved files from `-StagingDir` to the final output directory.
- **Fix:** Added a staging-to-final move block after group processing, mirroring the new-mode logic.
- **Files:** `compress_tiff_zip.ps1`

### 🟡 MEDIUM - PS7 Parallel Thumbnail Marker Failure Skipped EXIF Copy
**Issue:** In the PS7 parallel path, if the thumbnail `SubfileType` marker failed, the script returned immediately with an OK status before copying EXIF metadata, producing a ZIP file without metadata. The sequential path correctly logged a warning and continued.
- **Fix:** The PS7 parallel path now sets a `$thumbMarkerFailed` flag, continues through EXIF copy, and emits a `WARN` result at the end.
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - DryRun Created Output Directories
**Issue:** Several `CreateDirectory` calls in legacy and new-mode paths ran even when `-DryRun` was specified.
- **Fix:** Guarded all output-directory creations with `-not $DryRun`.
- **Files:** `compress_tiff_zip.ps1`

### 🟢 LOW - Mode 8 Default Staging Prompt Could Hang in Non-Interactive Sessions
**Issue:** The `Read-Host` prompt used a case-sensitive `-NonInteractive` check, and ignored `[Environment]::UserInteractive`, so CI/scheduled tasks could hang.
- **Fix:** Detect `-NonInteractive` case-insensitively and skip the prompt when `[Environment]::UserInteractive` is `$false`.
- **Files:** `compress_tiff_zip.ps1`

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