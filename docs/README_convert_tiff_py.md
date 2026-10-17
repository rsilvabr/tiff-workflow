# convert_tiff.py -- TIFF Workflow Manager (Wizard)

Unified Python/Rich wizard for TIFF processing. Use this for guided workflows with AutoFind, persistent config, and streaming PowerShell output.

**Requires:** Python 3.9+, Rich (`pip install rich`), PowerShell 5.1+ or 7, ImageMagick 7, exiftool.

---

## Quick Start

```powershell
python convert_tiff.py
```

Eight workflows available:
- **[1] Compress TIFFs** -- To Zip/Deflate, modes 0-9 (any folder)
- **[2] Fuji: Copy EXIF** -- From JPEG to TIFF (AutoFind, S3/S5 Pro)
- **[3] Fuji: Compress** -- To Zip/Deflate (AutoFind, S3/S5 Pro)
- **[4] Fuji: Copy + Compress** -- Combined in one pass (AutoFind, S3/S5 Pro)
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

**Workers** are clamped to 1-64, the range the PowerShell backends accept.

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

Runs Fuji: Copy EXIF first, then Fuji: Compress in sequence -- two steps in one pass.

### Restore OLD_TIFFs (option 5)

Move TIFFs from OLD_TIFFs/ subfolders back to their parent folders.

### Delete OLD_TIFFs (option 6)

Verify each TIFF in OLD_TIFFs matches the parent copy (via RMSE), then delete if identical.

### Diagnose TIFFs (option 7)

Check if 16-bit TIFFs are real 16-bit data or padded 8-bit (stretched from 8-bit by software that only converted depth). Uses round-trip RMSE method.

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

**As a preset (report only).** Workflow 7 is journalled like the others, so it can be
repeated (menu 2) or saved as a preset (menu 5) and run with `--run-preset`. A repeat or
preset re-scans the saved folder(s) with the saved answers and **only reports**: it never
offers or runs the 8-bit rewrite, attended or not. The report -- counts plus the full path
of every padded file and of every file that could not be checked -- is printed and written
to `Logs\convert_tiff\diagnose_<timestamp>.log` under the current folder. Exit code `0`
ran, `1` a folder is gone, the run was cancelled or a file could not be checked.
`--dry-run` only counts the files.

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
value falls back to the default instead of aborting at PowerShell parameter binding.

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
  entry starts; `-DeleteSource` is passed only to mode-8 entries, and never in a
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

### Running a preset unattended (Task Scheduler)

```powershell
python convert_tiff.py --list-presets                    # names + what each one does
python convert_tiff.py --run-preset nightly-zip          # runs it, no menu, no prompts
python convert_tiff.py --run-preset nightly-zip --dry-run
```

`--run-preset` runs once and exits. It never waits on stdin (`-NonInteractive` and a
closed stdin are passed to the backends), so a prompt left on the path is a refusal, not
a hang.

| | |
|---|---|
| **Dry-run** | off unless you pass `--dry-run`. It is **never** inherited from the stored run |
| **Exit code** | `0` ran (or nothing to do), `1` failed or refused, `2` no such preset (available names are printed) |

- A preset that deletes sources (`-DeleteSource`, or a manifest with a mode-8 row) is
  **refused** unattended (exit 1) unless you pass `--dry-run`: the confirmation cannot
  be given without a console. Run those from the menu.
- There is no `--overwrite`: the stored command already carries `-Overwrite` if the
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
logs to `Logs\` of the **current directory** (`$PWD`), so the run must start in the
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

A long AutoFind folder list is passed to the backend through a temporary `-InputDirFile` when it would exceed the command-line limit; journals and presets still record the folders themselves.

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
- `last_exclude_folders` -- last `-ExcludeFolders` list. The wizard asks once per workflow and
  offers this as the default; a fresh install defaults to nothing excluded

The wizard's defaults for Safe mode, LZW, thumbnails and duplicate names are read from the
backends' own `param()` defaults — edit them there.

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

## CLI Flags (all optional)

```powershell
python convert_tiff.py                    # Interactive wizard
python convert_tiff.py --help             # Show help
python convert_tiff.py --list-presets     # List saved presets and exit
python convert_tiff.py --run-preset NAME  # Run a preset unattended and exit
python convert_tiff.py --run-preset NAME --dry-run
```

No CLI flags are required -- the wizard is fully interactive. `--run-preset` and
`--list-presets` are for Task Scheduler/cron: see
[Presets (menu 5)](#presets-menu-5).

---

## Output

The wizard calls `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps7.ps1`, or `generate_thumbnails.ps1` as a subprocess and streams their output with color coding:
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
Rich             pip install rich
```

Both `magick.exe` and `exiftool.exe` must be on your PATH.

### Verify
```powershell
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx
python --version    # Python 3.9+
```
