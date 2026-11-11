# Test Suite for tiff-workflow

## Python Tests (pytest)

### Setup
```bash
cd tests
pip install pytest
```

### Run Python tests
```bash
# Run everything (from the repo root)
pytest tests -v

# Unit tests only
pytest tests/test_convert_tiff.py -v

# Regression suite only
pytest tests/test_regression_classes.py -v

# Skip the slow end-to-end PowerShell tests
pytest tests -v -k "not InPlaceExif and not ExitCodeContract"

# Run with coverage
pytest tests --cov=..
```

### `test_convert_tiff.py` covers
- `_format_size()` - file size formatting
- `truncate_path()` - path truncation
- `detect_powershell_version()` - PS version detection
- `build_compress_command()` - command building
- `build_copy_exif_command()` - command building
- `_compare_tiff_metadata()` - TIFF comparison using magick compare RMSE (all pages)
- `_is_real_16bit()` - diagnose: real 16-bit vs padded 8-bit, incl. Capture One 2-page layout
- `_compress_padded_files()`, `run_purge_old_tiffs()` gates, `_wrap_ps5_command()`, page-count gates

### `test_manifest.py` covers
- Manifest encoding (BOM in, ANSI refused, `;`-delimited refused, `;` in paths refused)
- Mode parsing (Excel floats, fractions/text/out-of-range refused, comment rows ignored whole)
- Guards: `..` traversal, source overlaps, output collisions (modes 2/4/5), mode-8 delete gate
- Generation (per-folder vs root entries, backup/output folder exclusions), repeat-last

### `test_inputdir_file.py` covers
- `-InputDirFile` end to end in the three backends (non-ASCII folder list), the refuse-both/missing/empty gates, and the wizard's spill rewrite plus its temp-file cleanup

### `test_stranded_originals.py` covers
- Round 25: originals left in `OLD_TIFFs/` with nothing in their place are reported in every mode (and mode 0 only at its own depth), `-RestoreStranded` puts them back and the same run compresses them, a dry run moves nothing, ambiguous and excluded backups are left alone (both shells, hostile folder)

### `test_mode9_keep_original.py` covers
- Round 26: modes 0/9 archive an original only after its copy landed -- a killed run (both shells, sequential and parallel, polled every 50 ms) never leaves a place empty; an edit saved during the run is never archived; staged and in-place runs, `.tiff` sources, a second run; every staging loop lands through `Move-FileIntoPlace`

### `test_wizard_stranded.py` covers
- Round 26: the wizard offers `-RestoreStranded` after a run that ends with stranded originals (never on presets or dry runs; PS 5.1 `-Command` form), and option 5 can restore only the stranded ones (same rule as the backend, never over a file)

### `test_move_old_backups.py` covers
- Round 25: `tools/move_old_backups.py` (formerly `move_old_padded.py`) never moves a backup whose original place is empty (same name, `.tif`/`.tiff`, `_vN` and timestamped backups, sidecars), re-checks before each move, exits 1 when it kept any; `--folder` is required and picks its own destination

### `test_exiftool_stdin.py` covers
- Round 27: exiftool never gets an argfile PATH on its command line (it reads it in the ANSI code page). Static: no `exiftool -@ $` left in a `.ps1`, `$OutputEncoding` set at script scope and first in every `-Parallel` block, Python only passes `"-@", "-"`. Real (needs a non-UTF-8 ANSI code page): `%TEMP%` set to `一時 Ωμ ąę [x] 100%`, compress modes 1/8/0 and copy_exif `-CompressZip` under both shells (and the parallel blocks), and `_tiff_metadata_signature` / `_image_pixels` with `tempfile.tempdir` there. Defines `exif_stdin`, the helper the other tests use to read tags with a Japanese path

### `test_dry_run_creates_nothing.py` covers
- A `-DryRun` creates no staging/output folder and changes nothing in the fixture folder: compress modes 1/2/8, copy_exif `-CompressZip -StagingDir`, generate_thumbnails (both shells, hostile folder). A pin, not a regression: it passes before round 27 too (there is no `TEMP_DIR` setting here, unlike jxl-photo #613/#614)

### `test_version_parity.py` covers
- `$script:ToolkitVersion` in all four `.ps1` equals `convert_tiff.TOOLKIT_VERSION` (the release bumps five places at once); `convert_tiff.py --version` prints `tiff-workflow <version>` and exits 0; every backend's `-DryRun` log carries `tiff-workflow <version> | <script>.ps1 | PowerShell <n>` (both shells)

### `test_compress_history.py` covers
- The `xmpMM:History` entry `compress_tiff_zip.ps1` appends (both shells, hostile folder, a `%` in the file name), in all three worker paths (PS 5.1 sequential, PS 7 sequential, the mode `-Parallel` block, the legacy sequential and `-Parallel` blocks): exactly one entry in the padded conversion's format, earlier entries kept and not doubled (jxl-photo #531), a second mode 0 run adds nothing, mode 8 `-DeleteSource` and `-GenerateThumbnail` (page markers intact), the Delete OLD_TIFFs gate still accepts the pair, and a static check that the helper is wired into every path

### `test_release_notice.py` covers
- The menu's new-release notice with **no network** (`_latest_release_tag` monkeypatched): newer/same/older/beta/exe tags, a day without an answer stays unmarked, one answer per day, the background thread keeps a late answer and writes only its two config keys, `--run-preset`/`--list-presets`/`--version` never ask, the environment variable and `check_for_updates` switch it off. `conftest.py` sets `TIFFWORKFLOW_NO_UPDATE_CHECK=1` for every other test

### `test_wizard_registry.py` covers
- v2.9.0: the wizard's option REGISTRY (`convert_tiff.py`, "Option Registry"). T1 **golden**: the argv `build_compress_command` / `build_copy_exif_command` produced before the registry existed, 13 workflow dicts x both shells, stored in `tests/data/wizard_registry_golden.json` (generated once from the pre-registry builders; `python tests/test_wizard_registry.py --generate-golden` refuses to overwrite it -- never regenerate it). T2 coverage: every parameter of every backend (`_ps_param_names`; both copy_exif scripts must agree) is in `REGISTRY` or `NOT_OFFERED` with a reason, and nothing registered is missing from the script. T3 the per-run Advanced options screen, plain and rich UI: "No" stores nothing, an edited value equal to the default is still emitted, a value not edited is not, invalid values are not stored, the two backends of workflow 4 stay apart. T4 a saved workflow (JSON, the config file's `last_manifest_workflow`, the manifest entry command) rebuilds the same argv. `tests/conftest.py` answers "No" to the Advanced screen for every other test (they script the prompts); this file keeps the real one

### `test_settings_overlay.py` covers
- v2.9.0: the settings overlay `.tiff_workflow_settings.json`. T5 the Python and PowerShell readers give the same verdict and value on ~70 (spec, JSON value) cases (`true` for an int, `1.5`, `"85"` for an intstr, 101, `..`, `a/b`, `null` for `path?`, ...), the function taken from each of the four scripts under both shells. T6 no safety or per-run name is in any `OVERLAY-SPECS` table (a mutation that lets `SafeMode` in fails T6 and T7), both copy_exif tables equal, the function identical, the version line still the first `Write-Log`. T7 real runs from `写真 [FINAL] x`: the file's `ZipSubfolderName` is the folder mode 1 really writes, `-ZipSubfolderName` beats it, `SafeMode`/`Overwrite`/`DeleteSource` entries are refused and multi-page handling is unchanged, every script applies its own section. T8 corrupt JSON, root array/scalar, empty file, wrong-case name, unknown section, invalid value, a UTF-8 BOM: same results and exit codes as without a file, a warning where one is due. T9 discovery order in Python and in both shells (variable, blank = off, next to the scripts, user profile). T10 `_backend_default` and `_tool_output_names` follow the overlay, a wrong type falls back, the `convert_tiff` section applies to the module. T11 the Script settings menu: atomic write, unknown sections/keys kept, a corrupt file or section refused and left byte for byte, `-` removes a key. `tests/conftest.py` sets `TIFFWORKFLOW_SETTINGS` to empty for every test so the owner's real file never reaches one

### `test_round28_ps_backends.py` covers
- Round 28, the PowerShell backends (both shells, `190815_Film_伊豆沼_夏 [FINAL]`, `50%` where it matters). Real runs: a neutral RGB16 file keeps 3 samples in legacy and modes 1/8/9, and both real neutral scans of `TW_FIXTURES` (Photoshop RGB, VueScan RGB+IR) keep every sample in mode 9; a neutral RGB page beside a gray page is refused (the layout gate), opaque associated alpha stays associated and translucent associated alpha is refused; Japanese IPTC survives a `%` path and a legacy in-place write; legacy with `-StagingDir` still replaces through the gate; an overlay `ExportZipSubfolder` cannot make mode 7 overwrite; the overlay reader of all four scripts refuses the files the wizard refuses (comments, trailing comma, single quotes, bare key, hex, `010`, UTF-16, cp932, text after the end) and reads the good ones; `Scan_20190815_143000.tif` is restored under its own name and a real collision copy still points at the base name; mode 8 without `-DeleteSource` on a `.tiff` makes no `_v2`; `generate_thumbnails -Remove` keeps a 1200x800 `*_thumb.tif`; `Move-FileReplacing` never leaves the destination missing (400 swaps under a poller). Static: `$copyExit` starts at 1, every magick write carries `@typeArgs`, every worker gates the layout, the replace gate keys on the destination, no `Move-Item -Force` onto a photo. Review of the round: `Move-FileReplacing` (all three copies) replaces a READ-ONLY destination and leaves it read-only when the swap still fails; a read-only original is compressed in legacy and mode 8 as in v2.9.0; copy_exif keeps the JPEG's UTF-8 IPTC (`伊豆沼`) and the TIFF's own Latin IPTC (`São Paulo`), with and without `%`, and creates no IPTC when neither file has one; with the metadata restore failing (an `exiftool` in front of PATH that fails only that call), legacy and mode 8 end in `ERROR (metadata restore failed ...)`, exit 1, the original byte for byte, and mode 9 stays a `WARN` with the original in OLD_TIFFs

### `test_round28_py_fixes.py` covers
- Round 28, the Python side: the padded conversion keeps RGB on both real neutral scans and refuses a collapsed layout; the Delete OLD_TIFFs gate sees a gray collapse and lost XMP/IPTC; Japanese IPTC through the MIE container; the padded swap (hard link + `os.replace`) killed at each step always leaves a photo in place and leaves no copy in OLD_PADDED on a locked original; the stranded-stem rule in the wizard and `move_old_backups.py` (a `_v2` in OLD_TIFFs is kept); overlay case-duplicate / empty keys refused; AutoFind skips a `;` folder; `step_folder` returns an absolute path; the Advanced `-SrgbProfile` is passed; the manifest collision reads the backend's folder names; the unattended refusal reads every `-DeleteSource` spelling and mode 8; Diagnose ignores "padded" in a path, a `"false"` preset never converts, a failed conversion exits 1 naming the file, an unusable `temp_dir` does not crash. Review of the round: the padded swap converts a read-only original (the backup in OLD_PADDED keeps the attribute, the new file does not), and when the swap still fails no link stays in OLD_PADDED and the original is read-only again

### `test_exclude_folders_parity.py` covers
- `Test-PathExcluded` in all four backends against jxl-photo's exclusion semantics (12-row table, both shells)

## Regression Suite (`test_regression_classes.py`)

Audit rounds 1-6 kept re-finding the same five failure classes in modes and files the previous
round had not visited, because every fix was pointwise and nothing pinned it. This suite has one
group per class, and each test was mutation-checked: revert the fix, the test fails.

| Class | What it pins |
|---|---|
| Exit code | `return` at script scope exits 0 and discards `$errTotal -gt 0 -> exit 1`, which `convert_tiff.py` uses to gate step 2. Covers all four backends, including a missing input directory |
| Data loss | In-place compression must not strip EXIF (`Backup-TiffMetadata`), on every worker; `OLD_TIFFs` backups stay untouched in every mode |
| PS5/PS7 divergence | The sequential and `-Parallel` paths must behave identically; helpers must be re-injected into runspaces; probes go through the timeout wrapper |
| Fail-closed gates | A gate that authorises deleting or replacing data must treat "cannot read" as failure |
| Staging / collision | A run must not touch another run's in-flight files, and a failed move must not lose both files |
| Round-trip | What a workflow creates, the same workflow must be able to find and undo -- thumbnail generate/remove, and OLD_TIFFs restore surviving a per-file failure |
| Multi-page | SafeMode skips MASK (scanner IR) files without moving them; REDUCEDIMAGE markers survive compression; mode 8 deletes the `.tiff` source only with pixel-identical output; copy_exif copies Make/Model end to end |

The PowerShell parts run **end to end through the real shells** (`powershell` and `pwsh`) instead
of through Pester, because the Pester suites below need Pester 5 and the dev boxes here still ship
3.4.0. Those tests skip automatically when a shell, ImageMagick or exiftool is missing, so they
are safe to run anywhere -- but the coverage is only real where the tools exist. They create their
own TIFFs in `tmp_path`; nothing outside the temp folder is written or deleted.

Static AST invariants (script-scope `return`, missing `${function:...}` re-injection in a
`-Parallel` block, parse errors) live in `helpers/Find-PsInvariantViolations.ps1`, which runs
under both PowerShell 5.1 and 7 and is invoked from the suite.

## Real-Photo Battery (`tools/real_photo_battery.py`)

`python tools/real_photo_battery.py --fixtures <folder>` (or set `TW_FIXTURES`) runs the
backends unattended on COPIES of real photos under both shells and checks the files on disk --
metadata, pixels, page counts and scanner page layouts, OLD_TIFFs, `%` and near-260-char
paths, embedded and standalone thumbnails, Purge/Restore, and real Fuji Copy EXIF (incl.
`-InputDirFile` with a non-ASCII session). The fixtures are never modified; the scratch folder
is deleted on success and kept on failure. Needs `magick` and `exiftool`; the report lands in
`AI_tools/<YYMMDD>_battery_report.md`.

## PowerShell Tests (Pester)

These are **static pattern checks over the sources**, not behavioural tests -- the behaviour is
covered from pytest (see the regression suite above). They need Pester 5+; Windows PowerShell
ships 3.4.0, which cannot run them, so install into the current user's PS7 module path:

### Setup
```powershell
Install-Module -Name Pester -Force -SkipPublisherCheck -Scope CurrentUser
```

### Run PowerShell tests
```powershell
# From project root, under pwsh (not Windows PowerShell 5.1)
Invoke-Pester -Path tests/test_compress_tiff_zip.ps1

# Run all PS suites -- note the explicit list; Pester 5+ only auto-discovers *.Tests.ps1
Invoke-Pester -Path (Get-ChildItem tests/test_*.ps1).FullName

# Run with detailed output
Invoke-Pester -Path tests/test_compress_tiff_zip.ps1 -Output Detailed
```

### PowerShell tests cover
- Parameter validation
- StagingDir cleanup check (IsNullOrWhiteSpace), run-scoped staging prefix
- stagingMap initialization
- Page count parsing ([int]::TryParse, `%n\n`, no Measure-Object -Line)
- Parallel processing structure
- Mode 8 verifies what it wrote before deleting the source
- Integrity check fails closed (timeout / no exit code)
- The `[no thumb]` fallback must NOT return early (round 6)
- Error handling

> These assert code *shapes*, so a refactor invalidates them even when the invariant survives.
> When one fails, check whether the invariant moved before "fixing" the script: in round 6 all 17
> failures were stale assertions and one was pinning the bug itself. Rewrite the assertion
> against the current form rather than deleting it.

## Test Philosophy

Two layers, on purpose:

1. **Unit / contract / static tests** (`test_convert_tiff.py`, the Pester suites) - fast, no
   external tools, verify functions return the expected structure and that code patterns exist
   or do not exist.
2. **Regression tests** (`test_regression_classes.py`) - one group per recurring failure class.
   These *do* use real TIFFs, ImageMagick, exiftool and both shells where available, because
   the classes they pin (EXIF lost on in-place writes, PS5/PS7 divergence, exit codes) are not
   observable from static analysis. Each one is mutation-checked against the fix it protects.

When fixing a bug, ask which of the five classes it belongs to and add the case to the matching
group -- a pointwise fix with no test is how the same bug came back six rounds running.