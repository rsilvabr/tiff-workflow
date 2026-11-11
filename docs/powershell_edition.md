# The PowerShell edition (up to v2.9.1)

Until **v2.9.1** the wizard (`convert_tiff.py`) was Python and the backends were PowerShell
scripts: `compress_tiff_zip.ps1`, `generate_thumbnails.ps1`, and two copies of Copy EXIF,
`copy_exif_to_TIFF_ps5.ps1` (Windows PowerShell 5.1, one file at a time) and
`copy_exif_to_TIFF_ps7.ps1` (PowerShell 7, in parallel). **v3.0.0** rewrote them in Python.
v2.9.1 is the last release of the PowerShell scripts.

This page was written once, with v3.0.0. It is not kept up to date: see
[After v3.0.0](#after-v300).

## Where the PowerShell scripts are

- The tag [`v2.9.1`](https://github.com/rsilvabr/tiff-workflow/tree/v2.9.1) of this
  repository, and its [release](https://github.com/rsilvabr/tiff-workflow/releases/tag/v2.9.1).
- As a Windows program:
  [tiff-workflow-exe v2.9.1-exe1](https://github.com/rsilvabr/tiff-workflow-exe/releases/tag/v2.9.1-exe1),
  which carries PowerShell 7 in its folder.

They get no more fixes. Nothing in them is known to damage a file, and they went through 28
audit rounds ([`bugs_fixed.md`](bugs_fixed.md)); what is known to be wrong in them is listed
[below](#known-problems-of-the-powershell-scripts-fixed-in-v300).

## Moving from v2.x to v3.0.0

What a run does to your files did not change. Before the PowerShell scripts were removed, each
one was run next to its Python replacement on copies of the same files, with the same
arguments (65 scenarios: every mode, staging, thumbnails, dry runs, second runs, Copy EXIF,
real scans and camera files, in folders named with Japanese, `[ ]`, spaces and `%`): the
resulting files, their pixels and metadata, the exit codes and the result lines were the same.

| | v2.9.1 | v3.0.0 |
|---|---|---|
| Needs | PowerShell 5.1 or 7; Python only for the wizard | Python 3.9+ for everything; no PowerShell |
| Compress | `compress_tiff_zip.ps1` | `compress_tiff_zip.py` |
| Copy EXIF | `copy_exif_to_TIFF_ps5.ps1` or `copy_exif_to_TIFF_ps7.ps1`, by shell | `copy_exif_to_tiff.py` |
| Thumbnails | `generate_thumbnails.ps1` | `generate_thumbnails.py` |
| Shared code | copied into every script | `tiff_workflow_common.py`, next to the three scripts |
| Several files at once | only under PowerShell 7 | always (`-ForceSequential`, or `-Workers 1`, turns it off) |

**If you only use the wizard**, nothing changes. Presets, *Repeat last* and manifests saved by
v2.x keep working: a saved PowerShell command line is read and run with the Python script and
the same arguments. The settings file (`.tiff_workflow_settings.json`) is the same file.

**If you call the scripts yourself** (a `.bat`, a scheduled task), change how each line starts
and keep its arguments:

```
before:  pwsh -NoProfile -File compress_tiff_zip.ps1 -Mode 9 -InputDir "F:\Photos" -Workers 8
after:   python compress_tiff_zip.py -Mode 9 -InputDir "F:\Photos" -Workers 8
```

- The parameter names are the same, upper or lower case, and an unambiguous start of a name
  still works (`-Rec` for `-Recursive`).
- To turn a yes/no setting off, `-SafeMode:false` works in any terminal. `-SafeMode:$false`
  works too, but in PowerShell the `$` has to be quoted: `'-SafeMode:$false'`.
- A value always needs its parameter name: `compress_tiff_zip.py 9` is refused, where
  PowerShell would have taken `9` as the first parameter.
- Exit codes are the same: 1 when a file failed or an input folder does not exist, else 0.
- The logs are in the same folders (`Logs\compress_tiff_zip`, `Logs\Copy-S5Pro-Exif`,
  `Logs\generate_thumbnails`) and their lines read the same.

**If you edited a default at the top of a `.ps1`**, make the same edit in the `PARAMS` table
at the top of the `.py` — or better, put it in the settings file (wizard, main menu **[6]**),
which survives an update.

A few lines of the output changed:

- The first log line names the Python script and version (`tiff-workflow 3.0.0 |
  compress_tiff_zip.py | Python 3.12.7`).
- A multi-page file skipped by Safe mode always reads `MULTI (3 pages - skipped)`. PowerShell 7
  said `3 IFDs - skipped` and PowerShell 5.1 said `3 pages - skipped`.
- `-- Group: <folder>` shows the folder as it is spelled on disk (it was in lower case).
- `Workers capped to N (CapWorkers limit)` is printed once per run, not once per folder.
- `-ForceParallel` is still accepted and does nothing.

## Known problems of the PowerShell scripts, fixed in v3.0.0

Found while the scripts were being ported, and confirmed by running v2.9.1. None of them
damages a file.

| Problem in v2.9.1 | When it happens | How often |
|---|---|---|
| `generate_thumbnails` and Copy EXIF: with `-InputDir .`, an `-ExcludeFolders` entry equal to the name of that very folder excluded every file. The run did nothing and ended with exit code 0. | Only with the relative `.` (or a path with `..`) **and** an exclusion that happens to be the folder's own name. `compress_tiff_zip` had been fixed in v2.4. | Rare |
| Copy EXIF: a `-DryRun` with `-CompressZip -OutputDir X` created the folder `X`. | Every such dry run. An empty folder; nothing else is written. | Always, harmless |
| Legacy mode (no `-Mode`): when `-StagingDir` or the output path named an existing *file*, every folder logged an `ERROR`, nothing was compressed and the exit code was 0. | A typo in the path. | Rare |
| `-Mode 10` or higher: "No TIFF files found", exit code 0. | A typo in the mode. | Rare |
| A check that ran out of time (`-MagickTimeout`) left its `magick.exe` running until the script ended. | Corrupted files, or very large scans on a slow disk. The file is reported as an error either way; the leftover process only costs memory for a while. | Rare |
| After Ctrl+C in modes 0 and 9, the temporary copies the run was writing (`<name>.<8 hex>_<12 hex>.tif`) stayed next to the photos. Every later run skips them and says so, but none removes them. | Every interrupted run without `-StagingDir`. They are safe to delete by hand. | Whenever a run is interrupted |

Not a bug, but the reason for the rewrite: the same logic existed in up to four copies (the
sequential worker and two parallel workers of `compress_tiff_zip.ps1`, and the two Copy EXIF
scripts), and under two PowerShell versions that behave differently. A fix made in one copy
and not in the others was the most frequent kind of bug in this project's history. The Python
backends have one copy of everything.

## After v3.0.0

Fixes and new features from v3.0.0 on are in [`CHANGELOG.md`](CHANGELOG.md), and only there.

Nobody checks whether a bug fixed later was also in the PowerShell scripts. The Python code
was ported from them line by line, so **assume that it was**: a problem fixed after v3.0.0 in
code that came from the port very likely exists in v2.9.1 too. This page will not list them.
