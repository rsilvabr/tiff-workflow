# convert_tiff.py -- TIFF Workflow Manager (Wizard)

Unified Python/Rich wizard for TIFF processing. Use this for guided workflows with AutoFind, persistent config, and streaming output from the backends.

**Requires:** Python 3.9+, ImageMagick 7, exiftool. Rich (`pip install rich`) is optional: without it the wizard falls back to a plain-text UI. No PowerShell is needed: the backends (`compress_tiff_zip.py`, `copy_exif_to_tiff.py`, `generate_thumbnails.py` and the module they share, `common.py`) are Python modules of the folder `tiff_workflow/`, beside the wizard (`wizard.py`), and the wizard starts each with the same Python that runs it, as `convert_tiff.py compress`, `copy-exif` or `thumbnails`. The PowerShell backends of v2.9.1 and earlier are described in [powershell_edition.md](powershell_edition.md).

---

## Quick Start

```powershell
python convert_tiff.py
```

Eight workflows available:
- **[1] Compress TIFFs** -- To Zip/Deflate, modes 0-9 (any folder)
- **[2] Fuji: Copy EXIF** -- From JPEG to TIFF (AutoFind, S3/S5 Pro)
- **[3] Fuji: Compress** -- To Zip/Deflate (AutoFind, S3/S5 Pro)
- **[4] Fuji: Copy + Compress** -- Copy EXIF, then compress (AutoFind, S3/S5 Pro)
- **[5] Restore OLD_TIFFs** -- Move TIFFs back to parent folder
- **[6] Delete OLD_TIFFs** -- Verify copy matches, then delete
- **[7] Diagnose TIFFs** -- Check if 16-bit is real or padded 8-bit
- **[8] Generate Thumbnails** -- Create sRGB thumbnails from TIFFs (standalone or embedded in compression)

---

## Workflows

### Compress TIFFs (option 1)

Full TIFF ZIP compression with modes 0-9:

```
Step 1: Select mode (0-9)
Step 2: Choose input folder
Step 3: Output folder + duplicate policy (mode 2 only)
Step 4: Workers, staging, dry-run
Step 5: Thumbnail generation options (optional)
Step 6: Summary + confirm
```

**Mode 2** merges every TIFF into one folder, so the wizard asks where that folder is
(default `<input>\ZIP_flat`) and how to handle duplicate filenames (`Numbered` (default),
`Skip`, `Overwrite`). Choosing the input folder itself means recompressing in place without
a backup, so it requires an explicit confirmation.

**Workers** are clamped to 1-64, the range the backends accept.

**Advanced options.** Right after the basic questions (Workers, staging, dry-run, safe mode,
...) every compress and Copy EXIF workflow asks `Advanced options? [y/N]`. The default is
**No** and then nothing changes. On yes it lists the backend parameters no other step asks,
with what each one is right now (this run, the settings file, or the script's own default):

| Option | Backend | What it is |
|---|---|---|
| `--magick-timeout` | compress, Copy EXIF | seconds one `magick` call may run before it is killed (whole number >= 1) |
| `--worker-memory-fraction` | compress, Copy EXIF | share of the free memory the workers may use, `0`-`2` (`0` = no cap; above `1` trusts the memory estimate) |
| `--srgb-profile` | compress | ICC file used to convert wide-gamut sources; empty = auto-detect |
| `--fail-on-warn` | Copy EXIF | exit 1 when a file only produced a warning or had no JPEG |

Type a number, then a value; `-` takes your change back; `0` is done. A value is checked
before it is kept (the same rules as the settings file below) and a wrong one says what was
expected and is not stored. **Only an option you change is passed to the script, and it is
passed explicitly even when it equals the default** -- an option you did not touch is not on
the command line, so the script's own setting (or the settings file) applies. The choice is
part of the workflow, so Repeat last, presets and the manifest runs carry it. In the combined
workflow (4) the Copy EXIF options go to step 1 and the compress ones to step 2.

**Thumbnail Options (v1.2+):**
- Generate embedded thumbnails? [y/N]
- Configure thumbnail settings? [y/N]
  - Thumbnail size (px): default 256
  - JPEG quality: default 85
  - Format: jpg/png/tif (default: jpg)
- Skip already-compressed TIFFs that have thumbnails? [y/N]

When enabled, creates a multi-page TIFF:
- Page 0: Original image (ZIP/Deflate compressed)
- Page 1: Thumbnail (sRGB, ICC stripped, aspect ratio preserved)

Mode 8 shows an extra confirmation prompt since it deletes source TIFFs after compression.

### Fuji: Copy EXIF (option 2)

For Fuji S3/S5 Pro users who export TIFFs via Hyper Utility (which strips EXIF metadata). The wizard uses **AutoFind** to automatically locate all `S5pro` or `S3pro` folders recursively.

**AutoFind pattern options:**
- [1] S5 Pro folders -- matches `S5pro` in folder name
- [2] S3 Pro folders -- matches `S3pro` in folder name
- [3] Both -- matches `S5pro` and `S3pro`
- [4] Custom -- type any pattern (e.g. `S2pro`, `mycamera`)

**Workflow:**
1. Shoot RAW + JPEG (JPEG has EXIF)
2. Export TIFF from Hyper Utility
3. Run Fuji: Copy EXIF wizard
4. TIFFs now have camera model, lens, date, GPS

### Fuji: Compress (option 3)

Same as Compress TIFFs but filtered to S3/S5 Pro sessions found via AutoFind.

### Fuji: Copy + Compress (option 4)

Runs Fuji: Copy EXIF first, then Fuji: Compress -- two steps, one after the other (the second
runs only when the first ended cleanly).

### Restore OLD_TIFFs (option 5)

Move TIFFs from OLD_TIFFs/ subfolders back to their parent folders.

When some of them have **nothing in their place** (originals left behind by an interrupted
mode 0/9 run -- see *Stranded originals* in the compress reference), the first question is
whether to restore only those (default yes). That restores them without touching the
originals of files that were compressed fine, never over an existing file, and leaves alone two
backups that claim the same name (an original and its timestamped namesake: move the right one
by hand). Answer no to restore everything, as before.

### Stranded originals after a compress run

When a compress run ends with `STRANDED: N original(s) still in OLD_TIFFs ...`, the wizard asks
whether to put them back and compress them now (it runs the same command again with
`--restore-stranded`). It never asks on a preset, a scheduled run or a dry run -- there it prints
what to do instead.

### Delete OLD_TIFFs (option 6)

Verify each TIFF in OLD_TIFFs matches the parent copy (via RMSE), then delete if identical.

### Diagnose TIFFs (option 7)

Check if 16-bit TIFFs are real 16-bit data or padded 8-bit (stretched from 8-bit by software that only converted depth). Uses round-trip RMSE method.

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

**What a padded file costs** (45 MP, Deflate): 70 MB kept as 16-bit, 59 MB as 8-bit (~17% smaller).

Two questions narrow the search before anything is checked:

- **Only inside this folder path** -- e.g. `_EXPORT/TIFF16` (either slash, any case):
  only TIFFs whose folder path contains those folder names one after the other, and
  everything below them. Pointing the root straight at such a folder works too. `-` =
  everywhere (the old behaviour: the export trees and the toolkit's own folders are
  skipped below the root).
- **Skip file names matching** -- `;`-separated patterns, case-insensitive, e.g.
  `*NXSTUDIO*` (an NX Studio export is always real 16-bit). `-` = none.

`OLD_TIFFs/`, `OLD_PADDED/`, `ZIP/`, `Logs/` and `converted_zip/` are skipped below the
root in every case.

When padded files are found, the workflow offers to rewrite them as 8-bit ZIP (originals
backed up to `OLD_PADDED/`). Before replacing a file, the rewrite is compared with the
original pixel by pixel (`magick compare -metric AE`, page 0) and kept only when they are
identical; a file that holds real 16-bit data is skipped with a message, never rounded down.
Each converted file gets one new `xmpMM:History` entry (`Action=converted`,
`SoftwareAgent=tiff-workflow <version> (<link>)`,
`Parameters=convert_tiff: 16-bit padded -> 8-bit ZIP (pixel-identical)`), after the entries it
already had; if it cannot be written, the original is kept. See it with
`exiftool -struct -XMP-xmpMM:History file.tif`.
Think twice inside an export tree that another tool reads (jxl-photo encodes
`_EXPORT\TIFF16`): the backup folder sits next to the files -- exclude `OLD_PADDED` there.

Workflow 7 uses the same verdict cache as the preset (below): a file already checked and
unchanged since is not checked again. Running the preset first and converting from the menu
afterwards therefore only re-reads the padded files, for the conversion itself.

**As a preset.** Workflow 7 is journalled like the others, so it can be repeated (menu 2)
or saved as a preset (menu 5) and run with `--run-preset`. A repeat or preset re-scans the
saved folder(s) with the saved answers. The report -- counts plus the full path of every
padded file and of every file that could not be checked -- is printed and written to
`Logs\convert_tiff\diagnose_<timestamp>.log` under the current folder. Exit code `0` ran,
`1` a folder is gone, the run was cancelled or a file could not be checked. `--dry-run`
only counts the files and never converts.

Workflow 7 asks one more question for this: **"Repeat/preset: also convert the padded files
it finds?"** (default no, label `[report only]`). Answered yes (label `[converts padded]`), the
repeat or preset runs the same conversion as the menu right after the check, unattended
too: every file pixel-verified, scanner files with real extra pages refused, the original
moved to `OLD_PADDED/`. The report adds `Converted to 8-bit now: N of M`. A preset saved by
an earlier version has no answer and only reports.

Diagnose remembers each verdict (real 16-bit, padded, not 16-bit) with the file's size
and modification time in `.convert_tiff_diagnose_cache.json` next to the config, so the
next run checks only new or changed files and still reports the cached padded ones. The
cache is written every 25 files while the run goes and once more when it ends or is
stopped with Ctrl+C, so an interrupted run keeps what it already checked. Errors are never
remembered. `DIAGNOSE_CACHE = False` at the top of `convert_tiff.py` turns it
off. A hand-edited preset may list several folders in `"roots"`.

### Generate Thumbnails (option 8)

Create sRGB thumbnails from TIFFs. Can be used standalone or as part of the compression workflow.

**Settings:**
- Input directory
- Remove mode (delete existing thumbnails instead of creating them)
- Thumbnail size (32-4096 px, default: 256)
- JPEG quality (1-100, default: 85)
- Format: jpg, png, or tif (default: jpg)
- Page: `0` (first) or `all`
- Output folder (empty = next to each TIFF)
- Recursive processing
- Dry-run mode

Size, quality and format are validated before the backend is launched — an out-of-range
value falls back to the default instead of the backend refusing the run.

**Output:** `filename_thumb.jpg` next to each TIFF (or in specified output folder)

**Thumbnail format:**
- Converted to sRGB
- ICC profile stripped
- Aspect ratio preserved (only downscales)
- First page only for multi-page TIFFs

---

## Manifest System (main menu option 3)

Run the Compress workflow (modes 0-9) over many folders in one pass, driven by a
CSV edited in Excel.

**Generate:** start workflow [1] (Compress TIFFs), pick mode and folder, and answer
"yes" when asked to generate a manifest instead of running. The CSV is saved to
`manifests/manifest_<timestamp>.csv`:

- Modes 0/1 (non-recursive): one row per folder containing TIFFs.
- Modes 2-9 (recursive): a single row for the root folder.
- Mode 2 pre-fills `Destination` with `<root>/ZIP_flat`.

**Format:**

```csv
Source,Destination,Mode
F:\2025\Tokyo\TIFF,F:\2025\Tokyo\ZIP_flat,2
# F:\2025\Osaka\TIFF,F:\2025\Osaka\TIFF,3
```

- Written as UTF-8 **with BOM** so Excel detects the encoding. A manifest re-saved
  by Excel in ANSI is refused, not guessed (paths drive a tool that can delete
  sources in mode 8).
- Rows starting with `#` are skipped. An empty `Destination` falls back to `Source`.
- An empty `Mode` cell inherits a default mode asked once per run.
- `Mode` accepts Excel floats (`7.0` → 7) but refuses fractions, text, and values
  outside 0-9 — any invalid row refuses the whole manifest.
- Only mode **2** honors the `Destination` column; other modes compute their own
  outputs (a warning lists ignored Destinations).
- A manifest is guaranteed to work with the version that generated it. Regenerate
  after upgrading.

**Run:** main menu [3] lists manifests (newest first), shows a preview table, asks
for workers/staging/dry-run (and the duplicate policy when mode 2 is present), then
executes **one child process per entry**. A failed entry does not stop the rest;
the end-of-run summary lists every entry's state and is written to
`Logs/convert_tiff/manifest_<timestamp>.log`.

**Safety guards (fail closed):**

- Paths containing `..` refuse the whole manifest.
- Duplicate or nested Source folders warn and require confirmation (default: no).
- Mode 2/4/5 entries are scanned for cross-entry output collisions before anything
  runs; a conflict aborts the run.
- Missing Source folders refuse the run.
- Mode-8 entries (delete sources) require an explicit confirmation once, before any
  entry starts; `--delete-source` is passed only to mode-8 entries, and never in a
  dry run.

**Repeat:** main menu [2] ("Repeat last workflow") re-reads the manifest CSV through
the same loader guards and re-executes it — it never replays stored command lines,
so edits to the CSV between runs are honored. The run's answers (Safe mode, exclusions,
worker cap, duplicate policy, the default mode for rows without a Mode cell) travel
with the journal, so a repeat runs with exactly the settings of the original run. A
journal saved by an older version without those settings is refused: run the manifest
once more from menu [3], then repeat it.

---

## Presets (menu 5)

"Repeat last workflow" only ever remembers **one** run. Presets are named snapshots of
that journal, so several recurring jobs (a nightly manifest sync, a per-shoot
compression) can coexist instead of overwriting each other:

```
--- Presets ---
  1. nightly-zip
     Compress TIFFs -- mode 9 -- D:\Fotos  (saved 2026-10-10 03:00)
  2. fuji-copy
     Fuji: Copy EXIF -- pattern 'S5pro' under E:\Fuji (12 folders)  (saved 2026-10-10 03:00)
[number] run | [S] save last workflow as preset | [D] delete | [B] back
```

- **`[S]`** snapshots whatever ran last (including a manifest run) under a name you
  choose, so it stops being affected by later runs. A manifest preset re-reads the CSV
  on every run, exactly like "Repeat last workflow".
- **`[number]`** runs a preset. It goes through the same questions as
  "Repeat last workflow" (`Run it now?`, the mode-8 warning), because it is the same
  runner — the two cannot drift apart.
- A workflow that deletes sources keeps `[DELETES SOURCES]` in its label.
- Presets live in the same `~/.convert_tiff_config.json` as the rest of the settings.
- A preset runs the backends next to the wizard that starts it, not the ones of the
  folder it was saved from: it keeps working after the toolkit is moved or updated
  into another folder.
- A preset keeps each step as the backend's name and its arguments
  (`["compress", "--mode", "9", ...]`), and the wizard reads them back with the backend's
  own reader before anything runs: what it deletes is what the backend will do.
- A preset or "Repeat last workflow" saved by v3.0.0 or earlier holds the command line of
  then (`python ...\compress_tiff_zip.py -Mode 9 ...`, or for v2.x a PowerShell one). It is
  no longer run: it stays in the list, and choosing it says `Saved workflow cannot be
  replayed` and runs nothing. Run the workflow once more from the menu and save it again
  ([command_line.md](command_line.md)).

### Running a preset unattended (Task Scheduler)

```powershell
python convert_tiff.py --list-presets                    # names + what each one does
python convert_tiff.py --run-preset nightly-zip          # runs it, no menu, no prompts
python convert_tiff.py --run-preset nightly-zip --dry-run
```

`--run-preset` runs once and exits. It never waits on stdin (the backends are started with
a closed stdin), so a prompt left on the path is a refusal, not a hang.

| | |
|---|---|
| **Dry-run** | off unless you pass `--dry-run`. It is **never** inherited from the stored run |
| **Exit code** | `0` ran (or nothing to do), `1` failed or refused, `2` no such preset (available names are printed) |

- A preset that deletes or replaces sources (`--delete-source` in any spelling the backends
  bind -- any case, an abbreviation such as `-DeleteS`, `--delete-source` -- compress
  mode 8, or a manifest with a mode-8 row) is **refused** unattended (exit 1) unless you
  pass `--dry-run`: the confirmation cannot be given without a console. Run those from the
  menu.
- There is no `--overwrite`: the stored command already carries `--overwrite` if the
  wizard asked for it, and an override has no clear meaning for the backends. A preset
  runs exactly as it was saved.
- A Fuji preset (workflows 2/3/4) **redoes the AutoFind** on every run, so sessions
  added since it was saved are included. A journal saved by an older version replays
  the folders it saved and says so.
- `--dry-run` without `--run-preset` is an error (exit 2) rather than silently ignored:
  a stray flag must never turn a requested simulation into a real run.

### Scheduling on Windows (Task Scheduler)

One line — it works the same in `cmd` and in PowerShell (a `^` line continuation does
**not** work in PowerShell; keep it on one line):

```text
schtasks /Create /TN "tiff-workflow nightly" /SC DAILY /ST 03:00 /RL LIMITED /TR "cmd /k cd /d C:\Users\You\Documents\GitHub\tiff-workflow && python convert_tiff.py --run-preset nightly-zip"
```

`cmd /k` keeps the window open when the run ends, so a failure is in front of you the
next morning instead of buried in a log. The `cd /d` matters: the backends write their
logs to `Logs\` of the **current directory**, so the run must start in the
folder where the scripts live. Or use the task editor (*Create Task -> Actions -> New ->
Start a program*):

- **Program/script:** `cmd`
- **Add arguments:** `/k python convert_tiff.py --run-preset "nightly-zip"` — quote a
  name that contains spaces
- **Start in:** the folder where `convert_tiff.py` lives (same reason as `cd /d`).

### Several presets in one scheduled run (a `.cmd` file)

One scheduled task can run any number of presets, in order. Save this as
`run_scheduled_presets.cmd` in the folder where `convert_tiff.py` lives:

```bat
@echo off
rem One block per preset, in the order they must run.
cd /d "%~dp0"

echo === %date% %time%  nightly-zip
python convert_tiff.py --run-preset "nightly-zip"
echo === exit code %errorlevel%

echo === %date% %time%  fuji-copy
python convert_tiff.py --run-preset "fuji-copy"
echo === exit code %errorlevel%

rem python convert_tiff.py --run-preset "backup-zip"
echo === %date% %time%  done
```

- The `echo === exit code %errorlevel%` lines are the morning report: `0` ran, `1`
  failed or refused, `2` no such preset.
- Each line runs even if the one before failed. Join lines with `&&` only if a failure
  must stop everything after it.
- `rem` disables a line without deleting it.
- Do not list presets that delete sources: they are refused unattended (exit 1) and only
  clutter the log.

Then point one task at the batch file:

```text
schtasks /Create /TN "tiff-workflow" /SC WEEKLY /D SAT /ST 23:30 /RL LIMITED /TR "cmd /k C:\Users\You\Documents\GitHub\tiff-workflow\run_scheduled_presets.cmd"
```

---

## AutoFind

Recursively scans a root folder for sessions matching the chosen pattern. Shows a table with session name, TIFF count, and truncated path. Prompts for confirmation before processing.

Excluded from scan: `Logs`, `logs`, `converted_zip`, `ZIP`, `_EXPORT`, `OLD_TIFFs` folders.

A long AutoFind folder list is passed to the backend through a temporary `--input-dir-file` when it would exceed the command-line limit; journals and presets still record the folders themselves.

---

## Persistent Config

Config saved to `~/.convert_tiff_config.json`:
- `last_input_dir` -- last used folder
- `last_workers` -- last worker count
- `last_staging` -- last staging folder
- `last_pattern` -- last AutoFind pattern
- `last_mode` -- last mode selected
- `last_origin` -- last workflow used
- `last_manifest_path` -- last manifest CSV run (repeat re-reads it via the loader)
- `last_manifest_workflow` / `last_manifest_default_mode` -- the answers of that manifest
  run (Safe mode, exclusions, cap, duplicate policy, default mode), so a repeat or preset
  re-runs it with the same rules
- `last_autofind` -- root and patterns of the last Fuji run (2/3/4); a repeat/preset
  searches again, so sessions added since are included
- `last_diagnose` -- folders and answers of the last Diagnose run (7); a repeat/preset
  re-scans and reports only
- `presets` -- named snapshots of the journal above (menu 5 and `--run-preset`)
- `last_exclude_folders` -- last `--exclude-folders` list. The wizard asks once per workflow and
  offers this as the default; a fresh install defaults to nothing excluded
- `check_for_updates` -- `true` (default) lets the main menu look for a new release (see
  [New-release notice](#new-release-notice)); set it to `false` to turn that off
- `update_checked_on` / `update_latest_seen` -- the day GitHub last answered and the tag it
  answered; written by the check itself, nothing to edit

The wizard's defaults for Safe mode, LZW, thumbnails and duplicate names are read from the
backends' own defaults (the `PARAMS` table at the top of each backend) — edit them there, or
change them without editing a file in the [settings file](#script-settings-main-menu-6) below.

The wizard's own workflows that run ImageMagick in parallel (Diagnose, the padded-8-bit
compress, Delete OLD_TIFFs) are capped by memory instead of by the worker count alone. Two
settings at the top of `convert_tiff.py` control it:

- `WORKER_MEMORY_FRACTION` (default `0.8`) — the run may use at most this share of the
  available memory; `0` disables the cap.
- `WORKER_MEMORY_LIMIT` (default `"both"`) — which reading to budget from: `"both"` (the
  smaller of what the system can still commit and the physical RAM still free), `"commit"`
  or `"physical"`.

The peak of one worker is estimated from the largest image of the batch: a `magick compare`
holds ~68 bytes per pixel — 5.4 GB on an 84 MP scan. The estimate is printed before the pool
starts (`Memory: ~N GB per worker | N GB available | workers N`), with a warning whenever
the count is reduced. An image whose size cannot be read counts as 150 MP, so an unknown
size never shrinks the estimate.

**Diagnose budgets per file instead.** A film-scan library mixes 30 MP photos with the odd
200 MP scan, and sizing every worker for the largest one cut the workers on a machine with
plenty of free RAM. Diagnose keeps the workers you asked for and gives the run a memory
budget (available memory x `WORKER_MEMORY_FRACTION`); each check reserves its own estimate
before it starts, so typical files run side by side and a huge scan waits until there is
room -- alone, if it needs more than the whole budget. The line printed before the checks
says the budget and what the typical and the largest file need.

---

## Script settings (main menu 6)

The settings at the top of the scripts -- folder names, timeouts, thumbnail defaults -- can be
changed **without editing a `.py`**: they live in one JSON file, read by this wizard
and by the three backends, and menu **[6]** edits it. In a backend, the settings are the
defaults of the `PARAMS = (P("name", "kind", default, ...), ...)` table at the top of the file
(`tiff_workflow/compress_tiff_zip.py` and the other two; the wizard's own at the top of
`tiff_workflow/wizard.py`).

```json
{
  "compress_tiff_zip":   { "zip_subfolder_name": "ZIPPED", "thumb_quality": 90, "magick_timeout": 120 },
  "copy_exif":           { "magick_timeout": 120 },
  "generate_thumbnails": { "size": 128, "format": "png" },
  "convert_tiff":        { "CONVERT_TIMEOUT_S": 120, "DIAGNOSE_CACHE": false }
}
```

**Where the file is** (first that applies): the path in the environment variable
`TIFFWORKFLOW_SETTINGS` (**set but empty = no overlay at all**), else
`.tiff_workflow_settings.json` next to `convert_tiff.py` if it is there, else the same name in the
user profile (`%USERPROFILE%`). Menu 6 shows which one it is using. `copy_exif` is the section
of `copy_exif_to_tiff.py`. Since v3.1 a name is the one of the code and of the option
(`zip_subfolder_name`, `--zip-subfolder-name`); a name written for v3.0.0 or earlier
(`ZipSubfolderName`) is ignored with a warning -- [command_line.md](command_line.md) has every one.

**What it can change** -- only the names in each script's `OVERLAY_SPECS` table (the
`OVERLAY_SPECS = {...}` table right below `PARAMS` at the top of each backend), each with a
type and range:

| Section | Names |
|---|---|
| `compress_tiff_zip` | `zip_suffix`, `zip_subfolder_name`, `export_marker`, `export_zip_subfolder`, `export_tiff_subfolder` (folder/file names), `cap_workers` (0-64), `magick_timeout`, `thumb_size` (whole numbers >= 1), `thumb_quality` (1-100), `thumb_format` (name), `srgb_profile` (path or `null`), `worker_memory_fraction` (0-2) |
| `copy_exif` | `magick_timeout`, `worker_memory_fraction` |
| `generate_thumbnails` | `size`, `quality`, `format`, `srgb_profile` |
| `convert_tiff` | `WORKER_MEMORY_FRACTION`, `WORKER_MEMORY_LIMIT` (`both`, `commit`, `physical`), `DIAGNOSE_CACHE`, `CONVERT_TIMEOUT_S`, `COMPARE_TIMEOUT_S`, `TIMEOUT_S_PER_MB` |

**What it can never change:** anything that deletes, overwrites or is chosen per run -- `mode`,
the input/output/staging folders, `workers`, `dry_run`, `safe_mode`, `skip_lzw_as_compressed`,
`overwrite`, `delete_source`, `duplicate_action`, `force_parallel`/`force_sequential`,
`restore_stranded`, the thumbnail on/off switches, `fail_on_warn`, `exclude_folders`. A name that is
not in a script's table is ignored with a warning.

A folder name in the file can never turn a copy mode into an in-place replace either: when an
overlay `zip_suffix`, `zip_subfolder_name` or `Export*` name makes an output resolve onto its own
source, compress refuses that file (round 28).

**It never stops a run.** A file that cannot be read (bad JSON -- strict JSON in UTF-8 for the
wizard and every backend alike: no comments, trailing commas, single quotes, `0x10` or `010` --
the root is not an object), an
unknown name, a value of the wrong type or range, or an `srgb_profile` that does not exist
changes nothing -- the script keeps its own
value and says so: `WARNING: settings overlay: ...` on the console and in the script's log,
which also names the file and the settings it did change (`Script settings overlay: <file>: N
value(s): ...`). **A `-Param` on the command line always beats the file**, and so does anything
the wizard passes for the run.

**Menu 6** lists a script's settings with the value written in the script, the value from the
file (`-` when it has none) and a one-line help; pick a number, type a value (`-` = back to the
script's value, `0` = back). The value is checked as the scripts will check it; the file is
written whole through a temp file and `os.replace` in its own folder, keeping every section and
key it does not know. A file that exists and cannot be parsed is **not touched**: the menu
says why and stops. A change to a script applies to its next run; a change to the `convert_tiff`
section applies the next time the wizard starts. The folder names are also what discovery skips
as this toolkit's own output (`ZIP`, `_EXPORT`, ...), so a changed name counts at once.

---

## CLI Flags (all optional)

```powershell
python convert_tiff.py                    # Interactive wizard
python convert_tiff.py --help             # Show help
python convert_tiff.py --list-presets     # List saved presets and exit
python convert_tiff.py --run-preset NAME  # Run a preset unattended and exit
python convert_tiff.py --run-preset NAME --dry-run
python convert_tiff.py --version          # Print the release (tiff-workflow 3.0.0) and exit
python convert_tiff.py compress ...       # A backend, as its script: also copy-exif, thumbnails
python convert_tiff.py compress --help    # Its options
```

No CLI flags are required -- the wizard is fully interactive. `--run-preset` and
`--list-presets` are for Task Scheduler/cron: see
[Presets (menu 5)](#presets-menu-5). `--version` prints before any config, menu, dependency
check or network access. `convert_tiff.py compress|copy-exif|thumbnails ...` runs that
backend, without loading the wizard, with the command line of v3.1
([command_line.md](command_line.md)).

The release is also in the main menu's title, on the first line of the logs the wizard
writes (`Logs/convert_tiff/`), and on the first line of every backend's log.

---

## New-release notice

When the main menu opens, the wizard looks once a day for a newer release of the project on
GitHub and, if there is one, adds a line and a **[U]** entry that opens the release page:

```
A new release is out: v3.1.0 (this copy is 3.0.0) -- [U] in the menu opens its page.
```

- It is **one anonymous request** to `api.github.com` (`releases/latest`; a generic
  `User-Agent: tiff-workflow-update-check`, no token, nothing else sent), made in a
  background thread with a 3-second timeout; the menu waits at most half a second. No
  network, a timeout or a rate limit are silent, and the day is only counted once GitHub
  answered, so the next run asks again.
- The Windows program (`tiff-workflow-exe`) follows its own releases; the scripts follow
  `tiff-workflow`. A re-packaged build of the same release (`-exe2`) is not news.
- Menu **[4] Check dependencies again** asks GitHub too, right then.
- `--run-preset`, `--list-presets` and `--version` never ask, so a scheduled run does not
  touch the network.
- **To turn it off:** `"check_for_updates": false` in `~/.convert_tiff_config.json`, or set
  the environment variable `TIFFWORKFLOW_NO_UPDATE_CHECK` to any non-empty value.

---

## Output

The wizard runs each backend as a subprocess -- the same Python, `convert_tiff.py compress`, `copy-exif` or `thumbnails` -- and streams its output with color coding:
- `[OK]` lines -- green
- `[ERROR]` lines -- red
- `[WARNING]` / `[WARN]` lines -- yellow
- `[DRY` / `DRY` lines -- blue

---

## Error Handling

- Folder not found: stops with error message
- No matching folders (AutoFind): stops with "No matching folders found"
- Mode 8 delete confirmation: requires explicit `y` to proceed
- Keyboard interrupt: exits cleanly

---

## Requirements

```
ImageMagick 7    https://imagemagick.org/script/download.php
exiftool         https://exiftool.org
Python 3.9+      https://www.python.org
Rich (optional)  pip install rich
```

Both `magick.exe` and `exiftool.exe` must be on your PATH. Without Rich the wizard uses a
plain-text UI; the backends themselves use only Python's standard library.

### Verify
```powershell
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx
python --version    # Python 3.9+
```
