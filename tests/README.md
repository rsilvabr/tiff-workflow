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
- `LANES = ["parallel", "sequential"]`. `parallel` is the backend's default (`--workers` as the script has it). `sequential` adds `--force-sequential` for compress and `--workers 1` for copy_exif and generate_thumbnails, unless the test already chose
- **Every end-to-end test runs once per lane.** This replaces "both shells" (Windows PowerShell 5.1 = sequential, PowerShell 7 = parallel) of the PowerShell edition. The per-file work is one implementation now; what is left of that axis is whether it runs on the caller's thread or on a thread pool

### `test_backend_invariants.py` covers
- 300 tests that run the Python backends with ImageMagick and exiftool replaced by fakes (`FakeTools` answers every tool call and records the order of the steps). No tools needed. Each test names the audit round its rule comes from
- What a failed step leaves behind, on the paths a real run cannot reach: copy_exif with `-OutputDir` leaves neither the copy nor a staged file (12 failure points), and on success only the staged ZIP; the tag list of the JPEG copy (`_jpeg_copy_args`)
- The staging loops (modes 1-8, copy_exif `-CompressZip`): the six error lines of a staged file that did not land (destination missing, wrong size, the move's own error), each counted once; `tw.land_staged_file` on its own (same volume, another volume, the folder made only when asked)
- The steps of a group in `run_modes`, each called on its own (`_run_group`, `_move_staged`, `_archive_originals`, `_delete_sources`): mode 8 checks the file where it landed before it deletes the source; a staged file that failed its gate is dropped, never moved; modes 0/9 never archive an original whose copy failed or whose worker raised
- v3.1, the thumbnail page inside `process_job` (`_write_with_thumbnail`, `TestThumbnailWrite`): the three magick calls of `-GenerateThumbnail`; a thumbnail that fails, is not written or cannot be joined falls back to the main image alone -- `[no thumb]`, the gate then expects ONE page and the history does not claim a thumbnail; a failed page marker is a `WARN ... [thumb marker failed]`; a failed main image, a full disk in the middle and "success" without a file leave nothing behind (no partial output, no metadata container, no temp file), in place and in a copy mode
- v3.1, copy_exif `-OutputDir` names (`_dest_name`, `TestCopyExifOutputNames`): the second session that holds `DSCF0001.tif` gets `_v2` with a `RENAME` line, also with `-Overwrite` and with `-CompressZip`; a third gets the next free number and a name already on disk is never taken; a dry run gives each session its own name; a folder listed twice is not its own rival; without `-OutputDir` nothing is renamed
- v3.1, what `run_modes` decides before a worker starts (`_plan_jobs`, `TestPlan`): mode 2 `Numbered` / `Overwrite` / `Skip` for two sources with one name (the dropped one is reported and never compressed); modes 0/9 refuse a multi-page file while planning and write a temp sibling, never the file itself; a dry run plans without creating the staging folder; a failed archive is a counted error; a copy mode whose output resolves onto its own source is refused; mode 5 never climbs above the input root
- v3.1, mode 8 without `-StagingDir` (`TestMode8DefaultStaging`): an unattended run is never asked and stages in `_cs8_<id>` inside the input folder (in the temp folder for several roots), which is gone afterwards; on an interactive console Enter / `y` go on, `n` ends the run with exit 1 before anything is made, end of input counts as Enter; with `-StagingDir` or `-DryRun` there is no question
- v3.1 phase 3, small defects fixed (each test fails on the v3.0.0 behaviour): the SKIP line of a compressed file that has its thumbnail is the same in every mode (`TestPlan`); the skipped duplicate of mode 2 has its `[n/total]`; the memory limit of the workers starts from the `-CapWorkers` value; one resolver of the input folders for the three backends (`tw.resolve_input_dirs`: each entry once, a missing folder listed twice is one error, a file is a root only for generate_thumbnails); copy_exif `-AutoFind` leaves out the sessions below a `-ExcludeFolders` name and says how many (`TestCopyExifAutoFind`); the page count is `identify -ping` (what is sent to magick); an `-OutputDir` that names a file is refused by the three backends before the tools are looked for and before a log exists; mode 2 with its output in the input folder (`TestMode2InTheInputFolder`): with `-Overwrite` a source is never replaced by another source of the same name, whichever is listed first, the in-place write of a root file stays allowed, and the copies into the root are announced once; generate_thumbnails says when `-Workers` is above its limit (`MAX_PARALLEL`) and runs with the limit. What a job made for itself is gone when it ends in an error, whatever the error (`TestNothingOfAJobIsLeftBehind`, and the copy_exif twin in `TestCopyExif`): an exception raised by each step after the write, in the four ways a job writes (in place, staged, straight to its destination, modes 0/9), a write that dies half way, a staged file the page gate refused; an output of an EARLIER run at the destination is not deleted by a failure that comes before the write, and a good staged file stays for the caller
- v3.1 phase 3, the inside of the backends: the five tool helpers raise `tw.ToolError` (`TestGatesFailClosed`: `exiftool` and `magick_run` raise on a non-zero exit with the tool's text, and when the tool cannot be started; `exiftool(check=False)` keeps a batch's output; `max_image_pixels` uses a batch exiftool exits 1 on and `get_compression_map` drops a failed chunk; the subfile marker writes answer False when exiftool exits 1); `FakeTools` and every double follow that contract (`fake_exiftool`, `tool_fails`), and the error lines still carry magick's message (compress, copy_exif ZIP, thumbnails). One parser of the result lines (`tw.result_kind`, `TestResultKinds`): a misspelt kind counts as an error. compress reads its staging folder from `run.staging` only (`TestSourceRules`). `tw.run_captured` works with thread creation forbidden (it replaced a text match on the source)
- v3.1 phase 4, fewer exiftool processes per file: `tw.exiftool_commands` runs several commands in one exiftool and reads each one's status and output between its own `${status}` markers (a marker without a number is a failure; real run in `test_exiftool_stdin.py`, hostile path); `tw.tiff_facts` reads the compression, the layout and the page markers of the source in one call (an unreadable layout refuses the rewrite, a missing compression tag is refused by copy_exif); `tw.finish_written_tiff` writes the metadata, the page markers and the history entry as three commands of one exiftool, each judged on its own (the history entry stays a command of its own: `test_compress_history.py` fails when it shares the copy's). `FakeTools` answers `tiff_facts` and `finish_written_tiff` and sends the metadata restore through the exiftool double. `test_v31_backends.py`: copy_exif `-CompressZip` keeps the page markers of a 2-page TIFF
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
- The `xmpMM:History` entry `compress_tiff_zip.py` appends (both lanes, hostile folder, a `%` in the file name), in the four ways the per-file work runs (a copy mode and mode 2 in place, each sequential and parallel with three workers): exactly one entry in the padded conversion's format, earlier entries kept and not doubled (jxl-photo #531), a second mode 0 run adds nothing, mode 8 `-DeleteSource` and `-GenerateThumbnail` (page markers intact), and the Delete OLD_TIFFs gate still accepts the pair

### `test_release_notice.py` covers
- The menu's new-release notice with **no network** (`_latest_release_tag` monkeypatched): newer/same/older/beta/exe tags, a day without an answer stays unmarked, one answer per day, the background thread keeps a late answer and writes only its two config keys, `--run-preset`/`--list-presets`/`--version` never ask, the environment variable and `check_for_updates` switch it off. `conftest.py` sets `TIFFWORKFLOW_NO_UPDATE_CHECK=1` for every other test

### `test_wizard_registry.py` covers
- v2.9.0: the wizard's option REGISTRY (`convert_tiff.py`, "Option Registry"). T1 **golden**: `tests/data/wizard_registry_golden.json` holds the command lines `build_compress_command` / `build_copy_exif_command` produced before the registry existed, for 13 workflow dicts, in the two forms the PowerShell edition wrote (`pwsh -NoProfile -File <script>.ps1 args` and the Windows PowerShell 5.1 `-Command` line). The file is unchanged and **never regenerated**. The test compares today's ARGUMENTS with the golden ones translated into v3.1's form by `tools/legacy_args.py` (option by option; only what runs them changed: this Python and `<script>.py`), and checks that neither golden form is read by `_journal_step` (v3.1: a preset saved by v2.x is refused), while today's own command round-trips through the journal. T2 coverage: every parameter of every backend's `PARAMS` table (`_backend_param_names`) is in `REGISTRY` or `NOT_OFFERED` with a reason, and nothing registered is missing from the script. T3 the per-run Advanced options screen, plain and rich UI: "No" stores nothing, an edited value equal to the default is still emitted, a value not edited is not, invalid values are not stored, the two backends of workflow 4 stay apart, and `step_basic_params` ends with the screen (v3.1: it calls it itself, so no workflow can skip it; checked by running it). T4 a saved workflow that carries `advanced` rebuilds the same argv. `tests/conftest.py` answers "No" to the Advanced screen for every other test (they script the prompts); this file keeps the real one

### `test_journal_lines.py` covers
- What a journal (a preset, the last run) holds since v3.1 (T8): each step as `[subcommand, *args]` (`compress`, `copy-exif`, `thumbnails`; `_journal_line`), no Python and no folder -- a repeat runs the backend next to the wizard (round 24). `_journal_step` reads a step with the backend's own table and reader (`tw.parse_args`): today's journal round-trips for every backend, and what a step deletes (`--delete-source`, compress mode 8, the last one given wins) is what the backend binds (round 28). Refused, whole, nothing run: what v3.0.0 saved (`[<python>, <folder>/compress_tiff_zip.py, -Mode 9 ...]`), what the PowerShell edition saved (`pwsh -File <script>.ps1`, `powershell -Command`), a line its backend would refuse (an abbreviation, another case, a value out of range, an option of another backend), a backend whose table cannot be read; also when one step of two is bad. The old preset stays in the config and the message says to save it again; the same preset in today's form runs; a dry run of a preset adds `--dry-run` once. The real `--run-preset` on a PowerShell-form preset exits 1, with and without `--dry-run`, runs nothing and leaves the config byte for byte, and a v3.0.0 preset that points at an older copy runs nothing of it (`test_presets.py`)

### `test_legacy_args.py` covers
- v3.1 phase 8: `tools/legacy_args.py` translates a command line of v3.0.0 (`-Name value`, `-Name:value`, `-Switch:$false`, names without case, unambiguous prefixes) into v3.1's (`--kebab-case value`, `--x` / `--no-x`; a value that starts with `-` as `--x=value`). Every parameter of the three backends has a clean long name; the 13 golden lines of the wizard and every form the old reader took, for every parameter (exact name, other case, shortest prefix; every value shape, hostile ones included), bind to the same values and given names after translation; the last of a repeated parameter wins in both; what v3.0.0 refused is refused; the tool prints the new line, its own documented example included, and exits 2 on a refused one

### `test_v3_fixes.py` covers
- v3.0.0: four problems of the PowerShell edition found while porting, each confirmed on the v2.9.1 scripts. `-InputDir .` is normalised before the folder exclusion (an `-ExcludeFolders` entry equal to the folder's own name no longer excludes the whole run); copy_exif `-DryRun -CompressZip -OutputDir X` no longer creates `X`; `-StagingDir` or `-OutputDir` naming an existing FILE exits 1 and touches nothing (found in the legacy mode, pinned on mode 2 since v3.1); `-Mode 10/19/99` and `-Mode -1/-5` are refused with exit 1; v3.1: a run without `-Mode` (until v3.0.0 the legacy mode, the current folder in place) is refused, exit 1, nothing touched or created. Also the two round-16 rules about how compress resolves `-InputDir` and a relative `-OutputDir`, end to end (both lanes)

### `test_settings_overlay.py` covers
- v2.9.0: the settings overlay `.tiff_workflow_settings.json`. T5 the wizard's reader and the backends' reader (`tiff_workflow_common.read_settings_overlay`) give the same verdict, value and type on ~70 (spec, JSON value) cases (`true` for an int, `1.5`, `"85"` for an intstr, 101, `..`, `a/b`, `null` for `path?`, ...). T6 no safety or per-run name is in any `OVERLAY_SPECS` table (a mutation that lets `SafeMode` in fails T6 and T7), the tables are exactly the planned ones, the table the wizard reads from the file is the one the script runs with, and each backend applies the overlay once, after `parse_args` and before anything reads a parameter (v3.1: watched during a run of each backend, every read of the options recorded) and logs it right after its first line. T7 real runs from `写真 [FINAL] x` (both lanes): the file's `ZipSubfolderName` is the folder mode 1 really writes, `-ZipSubfolderName` beats it, a `SafeMode` entry is refused and multi-page handling is unchanged, every script applies its own section. T8 corrupt JSON, root array/scalar, empty file, wrong-case name, unknown section, invalid value, a UTF-8 BOM: same results and exit codes as without a file, a warning where one is due. T9 discovery order in the wizard and in a backend (variable, blank = off, next to the scripts, user profile). T10 `_backend_default` and `_tool_output_names` follow the overlay, a wrong type falls back, the `convert_tiff` section applies to the module. T11 the Script settings menu: atomic write, unknown sections/keys kept, a corrupt file or section refused and left byte for byte, `-` removes a key. `tests/conftest.py` sets `TIFFWORKFLOW_SETTINGS` to empty for every test so the owner's real file never reaches one

### `test_round28_backends.py` covers
- Round 28, found and fixed in the PowerShell backends of v2.9.1; the same runs now drive the Python backends (both lanes, `190815_Film_伊豆沼_夏 [FINAL]`, `50%` where it matters). A neutral RGB16 file keeps 3 samples in modes 1/2 (in place)/8/9, and both real neutral scans of `TW_FIXTURES` (Photoshop RGB, VueScan RGB+IR) keep every sample in mode 9; a neutral RGB page beside a gray page is refused (the layout gate), opaque associated alpha stays associated and translucent associated alpha is refused; Japanese IPTC survives a `%` path and a mode 2 in-place write; mode 2 in place with `-StagingDir` still replaces through the gate; an overlay `ExportZipSubfolder` cannot make mode 7 overwrite; the backends' overlay reader refuses the files the wizard refuses (comments, trailing comma, single quotes, bare key, hex, `010`, UTF-16, cp932, text after the end, an empty key, keys that differ only in case) and reads the good ones; `Scan_20190815_143000.tif` is restored under its own name and a real collision copy still points at the base name; mode 8 without `-DeleteSource` on a `.tiff` makes no `_v2`; `generate_thumbnails -Remove` keeps a 1200x800 `*_thumb.tif`; `move_file_replacing` never leaves the destination missing (400 swaps under a poller). Review of the round: `move_file_replacing` replaces a READ-ONLY destination and leaves it read-only when the swap still fails; a read-only original is compressed in place (mode 2) and in mode 8 as in v2.9.0; copy_exif keeps the JPEG's UTF-8 IPTC (`伊豆沼`) and the TIFF's own Latin IPTC (`São Paulo`), with and without `%`, and creates no IPTC when neither file has one; with the metadata restore failing (an `exiftool` in front of PATH that fails only that call), mode 2 in place and mode 8 end in `ERROR (metadata restore failed ...)`, exit 1, the original byte for byte, and mode 9 stays a `WARN` with the original in OLD_TIFFs. The paths a real run cannot reach without sabotaging a tool are in `test_backend_invariants.py`

### `test_round28_py_fixes.py` covers
- Round 28, the wizard side: the padded conversion keeps RGB on both real neutral scans and refuses a collapsed layout; the Delete OLD_TIFFs gate sees a gray collapse and lost XMP/IPTC; Japanese IPTC through the MIE container; the padded swap (hard link + `os.replace`) killed at each step always leaves a photo in place and leaves no copy in OLD_PADDED on a locked original; the stranded-stem rule in the wizard and `move_old_backups.py` (a `_v2` in OLD_TIFFs is kept); overlay case-duplicate / empty keys refused; AutoFind skips a `;` folder; `step_folder` returns an absolute path; the Advanced `-SrgbProfile` is passed; the manifest collision reads the backend's folder names; the unattended refusal reads every `-DeleteSource` spelling and mode 8; Diagnose ignores "padded" in a path, a `"false"` preset never converts, a failed conversion exits 1 naming the file, an unusable `temp_dir` does not crash. Review of the round: the padded swap converts a read-only original (the backup in OLD_PADDED keeps the attribute, the new file does not), and when the swap still fails no link stays in OLD_PADDED and the original is read-only again

### `test_exclude_folders_parity.py` covers
- `tiff_workflow_common.path_excluded` -- one copy for the three backends -- against jxl-photo's exclusion semantics (12-row table)

### `test_v31_backends.py` covers
- v3.1 phase 3, what needs real files: `tw.tiff_page_count` with `-ping` counts every page of the layouts the gates meet (one page, image + preview, scanner image + preview + IR mask, three unmarked pages) in a hostile folder with a `%` name; a file cut short is not counted (fails closed); damaged pixel data with the directory intact still counts 1, as the full identify did. Mode 2 without `-OutputDir` and with `-Overwrite`, both lanes: the red photo of the root is not replaced by the blue one of a subfolder (refused, exit 1), it is compressed in its own place, and the blue one is byte for byte

### `test_wizard_thumbnails.py` covers
- v3.1: workflow 8 of the wizard, Generate Thumbnails (`run_generate_thumbnails`), which had no test. On both screens (plain text and Rich; the answers go in through `builtins.input`, so Rich's own handling of an answer runs): the questions in order and how many, the exact command line of `generate_thumbnails.py` for Enter everywhere (the backend's own defaults, read from its `PARAMS`) and for every answer, what is journalled for Repeat last and the presets (a dry run never is), the `-Remove` flow and its three questions, `-ExcludeFolders` asked only for a recursive run, the backend's exit code as the answer, a missing folder, a `;` in the input or output path, quotes around a pasted path, a relative folder made absolute, sizes and qualities out of range, workers clamped. A format or a page that is not offered, and the long format names, are asked again on both screens (phase 7, T4; v3.0.0's plain screen took them or the default)

### `test_wizard_ui.py` covers
- v3.1 phase 7 (T4): the wizard's one screen -- `_say`, `_error`, `_ask`, `_ask_yes_no`, the only code that knows whether Rich is there. The Rich screen shows exactly the words the plain screen prints for hostile names (`[FINAL]`, `[bold]`, `[/b]`, `\[`, a backslash at the end -- escaped markup misread the last one next to a tag); a part's own style wins over the line's; the screen keeps console.print's highlighting (numbers, paths) and works with a console that has none; the plain prompts (`[y/N]`, `[default]`, a choice not offered asked again) and what the Rich questions receive (the text, the default, the choices)

### `test_ctrl_c.py` covers
- v3.1: a REAL Ctrl+C in the middle of the work, which nothing in the suite did. The program under test gets a hidden console of its own; a helper attaches to that console and raises `CTRL_C_EVENT` there (the suite's own console is never touched). A `magick` in front of PATH converts for real and then does not return, so the interrupt lands with a finished staged file on disk. compress mode 9, with and without `-StagingDir`: exit code 130, the line `Interrupted! Cleaning up staging files...`, the photo in flight byte for byte, no staged or temp file left, and the next run finds nothing stranded and compresses both; mode 8 with `-DeleteSource`: nothing replaced or deleted and the run's own staging folder gone. The wizard's `run_subprocess` (both screens): returns -1, says `Interrupted by user.`, and the process it started is gone with ITS children, also when they ignore Ctrl+C. Windows only; hostile folder without `%` (the stand-in magick is a `.cmd`)

## Regression Suite (`test_regression_classes.py`)

Audit rounds 1-6 kept re-finding the same five failure classes in modes and files the previous
round had not visited, because every fix was pointwise and nothing pinned it. This suite has one
group per class, and each test was mutation-checked: revert the fix, the test fails.

| Class | What it pins |
|---|---|
| Exit code | An early exit that discards the error count reads as success, and `convert_tiff.py` gates step 2 on the exit code. Covers the three backends, including a missing input directory |
| Data loss | In-place compression must not strip EXIF (the metadata container), whatever the lane; `OLD_TIFFs` backups stay untouched in every mode, and an uncompressed photo beside them is still compressed (modes 2, 3, 8) |
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
