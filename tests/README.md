# Test Suite for tiff-workflow

One suite: `pytest tests`. Since v3.0.0 the three backends are Python scripts
(`compress_tiff_zip.py`, `copy_exif_to_tiff.py`, `generate_thumbnails.py`, sharing
`tiff_workflow_common.py`), so the same runner covers the wizard and the backends. The Pester
suites and every `tests/*.ps1` of the PowerShell edition (up to v2.9.1) are gone with the
scripts they read.

## Python Tests (pytest)

### Setup
```bash
pip install pytest numpy tifffile
```

The end-to-end tests also need ImageMagick 7 (`magick`) and exiftool on PATH. They skip
automatically when a tool is missing, so the suite is safe to run anywhere -- but the coverage
is only real where the tools exist.

### Run Python tests
```bash
# Run everything (from the repo root)
pytest tests -v

# Unit tests only
pytest tests/test_convert_tiff.py -v

# The backend rules, without ImageMagick or exiftool (about a second)
pytest tests/test_backend_invariants.py -v

# Regression suite only
pytest tests/test_regression_classes.py -v

# Skip two of the slow end-to-end groups
pytest tests -v -k "not InPlaceExif and not ExitCodeContract"

# Run with coverage
pytest tests --cov=..
```

### `backends.py` -- how a test starts a backend
- `backend_cmd(lane, script, args)` builds the command line of one backend run: the Python that runs the tests (`sys.executable`), the script, the arguments. `run_backend(...)` runs it as an unattended subprocess (stdin closed) and decodes the output as UTF-8
- `LANES = ["parallel", "sequential"]`. `parallel` is the backend's default (`-Workers` as the script has it). `sequential` adds `-ForceSequential` for compress and `-Workers 1` for copy_exif and generate_thumbnails, unless the test already chose
- **Every end-to-end test runs once per lane.** This replaces "both shells" (Windows PowerShell 5.1 = sequential, PowerShell 7 = parallel) of the PowerShell edition. The per-file work is one implementation now; what is left of that axis is whether it runs on the caller's thread or on a thread pool

### `test_backend_invariants.py` covers
- 171 tests that run the Python backends with ImageMagick and exiftool replaced by fakes (`FakeTools` answers every tool call and records the order of the steps). No tools needed. Each test names the audit round its rule comes from
- What a failed step leaves behind, on the paths a real run cannot reach: copy_exif with `-OutputDir` leaves neither the copy nor a staged file (12 failure points), and on success only the staged ZIP; the tag list of the JPEG copy (`_jpeg_copy_args`)
- The staging loops (legacy, modes 1-8, copy_exif `-CompressZip`): the nine error lines of a staged file that did not land (destination missing, wrong size, the move's own error), each counted once; `tw.land_staged_file` on its own (same volume, another volume, the folder made only when asked)
- The steps of a group in `run_modes`, each called on its own (`_run_group`, `_move_staged`, `_archive_originals`, `_delete_sources`): mode 8 checks the file where it landed before it deletes the source; a staged file that failed its gate is dropped, never moved; modes 0/9 never archive an original whose copy failed or whose worker raised
- generate_thumbnails: `-Remove` keeps a `.tif` larger than `-Size` or one it cannot measure (`_own_thumbnail`), a taken thumbnail name is numbered (`_plan`), what one thumbnail sends to magick
- Gate order: an in-place write replaces the original only after every gate; the metadata is parked first and restored from the container; a staged file for its own place is verified like an in-place write; modes 0/9 land the copy, then archive (round 26), and an interrupt puts back an original whose copy never landed
- Fail-closed gates: page count, ZIP integrity, pixel compare (same page count, every page), extra pages, sample layout, associated alpha, the metadata container, the subfiletype restore -- a timeout, a non-zero exit or unparseable output is a failure
- What is sent to the tools: magick input literal and output `%`-escaped, `-type` from the source layout, exit 0 without a file is an error; exiftool arguments on stdin; the copy_exif tag list (geometry excluded, never `-unsafe`, a RAF never gives its focal-plane resolution or ICC); SafeMode passes the restricted `REDUCEDIMAGE`/`REDUCED` list
- Paths and moves: exclusion relative to the root, short generated names, cleanup of this run's files only, a read-only destination, a move from another volume copies next to the destination first, stranded originals
- The command line: `-Name value`, `-Name:value`, switches, prefixes, names without case; what cannot be bound is refused with exit 1 before anything is touched
- AST rules on the source (`TestSourceRules`): only `tiff_workflow_common.py` starts a process and only in `run_captured`; identify/compare go through `magick_probe` (a timeout); `os.replace` only inside `move_file_replacing`, never `shutil.move`; compression is read from `-IFD0:Compression`; the exit code is `main()`'s return value, with no bare `return`; the overlay reader is the wizard's, function for function
- It replaces the checks that matched the SHAPE of the PowerShell source, and `test_ps_helper_parity.py` (deleted: every helper has one copy now)

### `test_convert_tiff.py` covers
- `_format_size()` - file size formatting
- `truncate_path()` - path truncation
- `build_compress_command()` - command building
- `build_copy_exif_command()` - command building
- `_compare_tiff_metadata()` - TIFF comparison using magick compare RMSE (all pages)
- `_is_real_16bit()` - diagnose: real 16-bit vs padded 8-bit, incl. Capture One 2-page layout
- `_compress_padded_files()`, `run_purge_old_tiffs()` gates, `_magick_spec()`, the size-scaled timeouts, page-count gates

### `test_manifest.py` covers
- Manifest encoding (BOM in, ANSI refused, `;`-delimited refused, `;` in paths refused)
- Mode parsing (Excel floats, fractions/text/out-of-range refused, comment rows ignored whole)
- Guards: `..` traversal, source overlaps, output collisions (modes 2/4/5), mode-8 delete gate
- Generation (per-folder vs root entries, backup/output folder exclusions), repeat-last

### `test_inputdir_file.py` covers
- `-InputDirFile` end to end in the two backends that take it, compress and copy_exif (non-ASCII folder list, both lanes), the refuse-both/missing/empty gates, and the wizard's spill rewrite plus its temp-file cleanup

### `test_stranded_originals.py` covers
- Round 25: originals left in `OLD_TIFFs/` with nothing in their place are reported in every mode (and mode 0 only at its own depth), `-RestoreStranded` puts them back and the same run compresses them, a dry run moves nothing, ambiguous and excluded backups are left alone, paths past 260 characters are seen and restored (both lanes, hostile folder)

### `test_mode9_keep_original.py` covers
- Round 26: modes 0/9 archive an original only after its copy landed -- a killed run (`taskkill /T /F`, polled every 50 ms) never leaves a place empty; an edit saved during the run is never archived; staged and in-place runs, `.tiff` sources, a second run over its own output (hostile folder). The order of the landing steps and `move_file_into_place` are pinned without the tools in `test_backend_invariants.py`

### `test_wizard_stranded.py` covers
- Round 26: the wizard offers `-RestoreStranded` after a run that ends with stranded originals (never on presets or dry runs; the flag is one more argument of the same command), and option 5 can restore only the stranded ones (same rule as the backend, never over a file)

### `test_move_old_backups.py` covers
- Round 25: `tools/move_old_backups.py` (formerly `move_old_padded.py`) never moves a backup whose original place is empty (same name, `.tif`/`.tiff`, `_vN` and timestamped backups, sidecars), re-checks before each move, exits 1 when it kept any; `--folder` is required and picks its own destination

### `test_exiftool_stdin.py` covers
- Round 27: exiftool never gets an argfile PATH on its command line (it reads it in the ANSI code page). Static: `generate_thumbnails.py` never calls exiftool, and Python (`convert_tiff.py`, `tiff_workflow_common.py`, `tools/real_photo_battery.py`) only passes `"-@", "-"`. Real (needs a non-UTF-8 ANSI code page): `%TEMP%` set to `一時 Ωμ ąę [x] 100%`, compress modes 1/8/0 and copy_exif `-CompressZip` in both lanes (plus a three-worker run in the parallel lane), and `_tiff_metadata_signature` / `_image_pixels` with `tempfile.tempdir` there. Defines `exif_stdin`, the helper the other tests use to read tags with a Japanese path

### `test_dry_run_creates_nothing.py` covers
- A `-DryRun` creates no staging/output folder and changes nothing in the fixture folder: compress modes 1/2/8, copy_exif `-CompressZip -StagingDir`, generate_thumbnails `-OutputDir` (both lanes, hostile folder). A pin, not a regression: it passes before round 27 too (there is no `TEMP_DIR` setting here, unlike jxl-photo #613/#614)

### `test_version_parity.py` covers
- The release number lives in two places: `tiff_workflow_common.TOOLKIT_VERSION` (the three backends read it; none carries a version of its own) equals `convert_tiff.TOOLKIT_VERSION`. `convert_tiff.py --version` prints `tiff-workflow <version>` and exits 0; every backend's `-DryRun` output carries `tiff-workflow <version> | <script>.py | Python <n>` (both lanes)

### `test_compress_history.py` covers
- The `xmpMM:History` entry `compress_tiff_zip.py` appends (both lanes, hostile folder, a `%` in the file name), in the four ways the per-file work runs (mode and legacy, each sequential and parallel with three workers): exactly one entry in the padded conversion's format, earlier entries kept and not doubled (jxl-photo #531), a second mode 0 run adds nothing, mode 8 `-DeleteSource` and `-GenerateThumbnail` (page markers intact), and the Delete OLD_TIFFs gate still accepts the pair

### `test_release_notice.py` covers
- The menu's new-release notice with **no network** (`_latest_release_tag` monkeypatched): newer/same/older/beta/exe tags, a day without an answer stays unmarked, one answer per day, the background thread keeps a late answer and writes only its two config keys, `--run-preset`/`--list-presets`/`--version` never ask, the environment variable and `check_for_updates` switch it off. `conftest.py` sets `TIFFWORKFLOW_NO_UPDATE_CHECK=1` for every other test

### `test_wizard_registry.py` covers
- v2.9.0: the wizard's option REGISTRY (`convert_tiff.py`, "Option Registry"). T1 **golden**: `tests/data/wizard_registry_golden.json` holds the command lines `build_compress_command` / `build_copy_exif_command` produced before the registry existed, for 13 workflow dicts, in the two forms the PowerShell edition wrote (`pwsh -NoProfile -File <script>.ps1 args` and the Windows PowerShell 5.1 `-Command` line). The file is unchanged and **never regenerated**. The test compares today's ARGUMENTS with the golden ones byte for byte (only what runs them changed: this Python and `<script>.py`), and replays both golden forms through `_journal_parts` / `_rebuild_journaled_command` -- what a preset saved by v2.x holds must rebuild into today's command. T2 coverage: every parameter of every backend's `PARAMS` table (`_backend_param_names`) is in `REGISTRY` or `NOT_OFFERED` with a reason, and nothing registered is missing from the script. T3 the per-run Advanced options screen, plain and rich UI: "No" stores nothing, an edited value equal to the default is still emitted, a value not edited is not, invalid values are not stored, the two backends of workflow 4 stay apart. T4 a saved workflow that carries `advanced` rebuilds the same argv. `tests/conftest.py` answers "No" to the Advanced screen for every other test (they script the prompts); this file keeps the real one

### `test_powershell_journals.py` covers
- A preset or last run saved by the PowerShell edition (up to v2.9.1) still replays under Python: the `pwsh -NoProfile -File <script>.ps1 args` form and the Windows PowerShell 5.1 `-Command "& '<script>.ps1' args"` form both reduce to `[script, *args]` and run today's script, the copy next to the wizard (both old Copy EXIF scripts map to `copy_exif_to_tiff.py`); today's own journal round-trips; a journal saved from another folder runs this copy (round 24); a PowerShell journal is still read for what it deletes; anything that is not one of the backends is refused, never guessed; and a v2.x compress preset replayed from the menu runs the Python backend

### `test_v3_fixes.py` covers
- v3.0.0: four problems of the PowerShell edition found while porting, each confirmed on the v2.9.1 scripts. `-InputDir .` is normalised before the folder exclusion (an `-ExcludeFolders` entry equal to the folder's own name no longer excludes the whole run); copy_exif `-DryRun -CompressZip -OutputDir X` no longer creates `X`; legacy mode with `-StagingDir` or the output path naming an existing FILE exits 1; `-Mode 10/19/99` is refused with exit 1. Also the two round-16 rules about how compress resolves `-InputDir` and a relative `-OutputDir`, end to end (both lanes)

### `test_settings_overlay.py` covers
- v2.9.0: the settings overlay `.tiff_workflow_settings.json`. T5 the wizard's reader and the backends' reader (`tiff_workflow_common.read_settings_overlay`) give the same verdict, value and type on ~70 (spec, JSON value) cases (`true` for an int, `1.5`, `"85"` for an intstr, 101, `..`, `a/b`, `null` for `path?`, ...). T6 no safety or per-run name is in any `OVERLAY_SPECS` table (a mutation that lets `SafeMode` in fails T6 and T7), the tables are exactly the planned ones, the table the wizard reads from the file is the one the script runs with, and each backend applies the overlay once, after `parse_args` and before anything reads a parameter. T7 real runs from `写真 [FINAL] x` (both lanes): the file's `ZipSubfolderName` is the folder mode 1 really writes, `-ZipSubfolderName` beats it, a `SafeMode` entry is refused and multi-page handling is unchanged, every script applies its own section. T8 corrupt JSON, root array/scalar, empty file, wrong-case name, unknown section, invalid value, a UTF-8 BOM: same results and exit codes as without a file, a warning where one is due. T9 discovery order in the wizard and in a backend (variable, blank = off, next to the scripts, user profile). T10 `_backend_default` and `_tool_output_names` follow the overlay, a wrong type falls back, the `convert_tiff` section applies to the module. T11 the Script settings menu: atomic write, unknown sections/keys kept, a corrupt file or section refused and left byte for byte, `-` removes a key. `tests/conftest.py` sets `TIFFWORKFLOW_SETTINGS` to empty for every test so the owner's real file never reaches one

### `test_round28_backends.py` covers
- Round 28, found and fixed in the PowerShell backends of v2.9.1; the same runs now drive the Python backends (both lanes, `190815_Film_伊豆沼_夏 [FINAL]`, `50%` where it matters). A neutral RGB16 file keeps 3 samples in legacy and modes 1/8/9, and both real neutral scans of `TW_FIXTURES` (Photoshop RGB, VueScan RGB+IR) keep every sample in mode 9; a neutral RGB page beside a gray page is refused (the layout gate), opaque associated alpha stays associated and translucent associated alpha is refused; Japanese IPTC survives a `%` path and a legacy in-place write; legacy with `-StagingDir` still replaces through the gate; an overlay `ExportZipSubfolder` cannot make mode 7 overwrite; the backends' overlay reader refuses the files the wizard refuses (comments, trailing comma, single quotes, bare key, hex, `010`, UTF-16, cp932, text after the end, an empty key, keys that differ only in case) and reads the good ones; `Scan_20190815_143000.tif` is restored under its own name and a real collision copy still points at the base name; mode 8 without `-DeleteSource` on a `.tiff` makes no `_v2`; `generate_thumbnails -Remove` keeps a 1200x800 `*_thumb.tif`; `move_file_replacing` never leaves the destination missing (400 swaps under a poller). Review of the round: `move_file_replacing` replaces a READ-ONLY destination and leaves it read-only when the swap still fails; a read-only original is compressed in legacy and mode 8 as in v2.9.0; copy_exif keeps the JPEG's UTF-8 IPTC (`伊豆沼`) and the TIFF's own Latin IPTC (`São Paulo`), with and without `%`, and creates no IPTC when neither file has one; with the metadata restore failing (an `exiftool` in front of PATH that fails only that call), legacy and mode 8 end in `ERROR (metadata restore failed ...)`, exit 1, the original byte for byte, and mode 9 stays a `WARN` with the original in OLD_TIFFs. The paths a real run cannot reach without sabotaging a tool are in `test_backend_invariants.py`

### `test_round28_py_fixes.py` covers
- Round 28, the wizard side: the padded conversion keeps RGB on both real neutral scans and refuses a collapsed layout; the Delete OLD_TIFFs gate sees a gray collapse and lost XMP/IPTC; Japanese IPTC through the MIE container; the padded swap (hard link + `os.replace`) killed at each step always leaves a photo in place and leaves no copy in OLD_PADDED on a locked original; the stranded-stem rule in the wizard and `move_old_backups.py` (a `_v2` in OLD_TIFFs is kept); overlay case-duplicate / empty keys refused; AutoFind skips a `;` folder; `step_folder` returns an absolute path; the Advanced `-SrgbProfile` is passed; the manifest collision reads the backend's folder names; the unattended refusal reads every `-DeleteSource` spelling and mode 8; Diagnose ignores "padded" in a path, a `"false"` preset never converts, a failed conversion exits 1 naming the file, an unusable `temp_dir` does not crash. Review of the round: the padded swap converts a read-only original (the backup in OLD_PADDED keeps the attribute, the new file does not), and when the swap still fails no link stays in OLD_PADDED and the original is read-only again

### `test_exclude_folders_parity.py` covers
- `tiff_workflow_common.path_excluded` -- one copy for the three backends -- against jxl-photo's exclusion semantics (12-row table)

## Regression Suite (`test_regression_classes.py`)

Audit rounds 1-6 kept re-finding the same five failure classes in modes and files the previous
round had not visited, because every fix was pointwise and nothing pinned it. This suite has one
group per class, and each test was mutation-checked: revert the fix, the test fails.

| Class | What it pins |
|---|---|
| Exit code | An early exit that discards the error count reads as success, and `convert_tiff.py` gates step 2 on the exit code. Covers the three backends, including a missing input directory |
| Data loss | In-place compression must not strip EXIF (the metadata container), whatever the lane; `OLD_TIFFs` backups stay untouched in every mode |
| Two paths | The sequential and the parallel lane must behave identically: every end-to-end test runs in both |
| Fail-closed gates | A gate that authorises deleting or replacing data must treat "cannot read" as failure |
| Staging / collision | A run must not touch another run's in-flight files, and a failed move must not lose both files |
| Round-trip | What a workflow creates, the same workflow must be able to find and undo -- thumbnail generate/remove, and OLD_TIFFs restore surviving a per-file failure |
| Multi-page | SafeMode skips MASK (scanner IR) files without moving them; REDUCEDIMAGE markers survive compression; mode 8 deletes the `.tiff` source only with pixel-identical output; copy_exif copies Make/Model end to end |

Later audit rounds pinned their own fixes the same way; the round-specific classes
(`TestRound15Reporting`, the `TestAuditRound16*` group, ...) sit below the classes above.

The backends run **end to end as subprocesses**, with the real ImageMagick and exiftool, once
per lane (`tests/backends.py`: parallel and sequential -- what the two shells of v2.x were).
Those tests skip automatically when ImageMagick or exiftool is missing, so they are safe to run
anywhere -- but the coverage is only real where the tools exist. They create their own TIFFs in
`tmp_path`; nothing outside the temp folder is written or deleted.

The rules that used to be pinned on the shape of the PowerShell source (script-scope `return`,
a helper missing from a `-Parallel` block, parse errors: `helpers/Find-PsInvariantViolations.ps1`,
deleted) are in `test_backend_invariants.py`, which runs the Python code with the tools replaced
and checks the source with `ast`.

## Real-Photo Battery (`tools/real_photo_battery.py`)

`python tools/real_photo_battery.py --fixtures <folder>` (or set `TW_FIXTURES`) runs the
backends unattended on COPIES of real photos, once per lane (`--lanes parallel,sequential`, the
default), and checks the files on disk --
metadata, pixels, page counts and scanner page layouts, OLD_TIFFs, `%` and near-260-char
paths, embedded and standalone thumbnails, Purge/Restore, and real Fuji Copy EXIF (incl.
`-InputDirFile` with a non-ASCII session). The fixtures are never modified; the scratch folder
is deleted on success and kept on failure. Needs `magick` and `exiftool`; the report lands in
`AI_tools/<YYMMDD>_battery_report.md`.

## Test Philosophy

Three layers, on purpose:

1. **Unit / contract tests** (`test_convert_tiff.py`, `test_manifest.py`, ...) - fast, no
   external tools, verify functions return the expected structure.
2. **Backend invariants** (`test_backend_invariants.py`) - the backends' own code, run with
   ImageMagick and exiftool replaced by fakes: which gate runs before which irreversible step,
   what a failed gate leaves on disk, what is sent to the tools. They fail in a second when a
   gate is removed or reordered.
3. **Regression tests** (`test_regression_classes.py` and the per-round files) - one group per
   recurring failure class. These *do* use real TIFFs, ImageMagick, exiftool and both lanes,
   because the classes they pin (EXIF lost on in-place writes, the two lanes diverging, exit
   codes) are only fully observable on real files. Each one is mutation-checked against the
   fix it protects.

When fixing a bug, ask which of the classes it belongs to and add the case to the matching
group -- a pointwise fix with no test is how the same bug came back six rounds running.
