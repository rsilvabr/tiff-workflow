# The command line (v3.1)

v3.1 gives the three scripts the usual Python command line. A `.bat`, a scheduled task or a
shortcut written for v3.0.0 or earlier stops working until it is rewritten: the scripts refuse
the old form (exit code 2, nothing touched) and say where this page is. The table below
translates every option; `tools/legacy_args.py` translates a whole line.

## What changed

| | v3.0.0 and before | v3.1 |
|---|---|---|
| an option with a value | `-Mode 9 -InputDir "D:\photos"` | `--mode 9 --input-dir "D:\photos"` |
| a yes/no | `-SafeMode:$true`, `-SafeMode:$false` | `--safe-mode`, `--no-safe-mode` |
| a switch | `-DryRun` (`-DryRun:$false` off) | `--dry-run` (`--no-dry-run` off) |
| names | any case, any unambiguous prefix (`-mo 9`) | written in full, in lower case |
| a value that starts with `-` | `-ExcludeFolders "-old"` | `--exclude-folders=-old` |
| the scripts | `python compress_tiff_zip.py ...` | the same, or `python convert_tiff.py compress ...` |
| help | -- | `--help` lists the options of a script |
| a command line that cannot run | exit code 1 | exit code 2 (1: a file failed, a folder does not exist) |
| the settings file | `"ZipSubfolderName": "ZIP"` | `"zip_subfolder_name": "ZIP"` |
| presets, Repeat last | the saved command line | saved by v3.1 again (see below) |

- **One entry point.** `python convert_tiff.py` is the wizard, as before.
  `python convert_tiff.py compress ...`, `copy-exif ...` and `thumbnails ...` run a backend
  exactly as its own script does; the Windows program takes the same three names.
- **Exit codes.** 0: every file went right. 1: a file failed, an input folder does not exist,
  a tool is missing, a folder or file named on the line is not usable. 2: the line cannot
  run as written -- an unknown option, a missing `--mode`, `--mode 10`, `--input-dir` together
  with `--input-dir-file`, a path in `--exclude-folders`, a thumbnail size or format out of
  range. A 2 always means nothing was touched and no log was made.
- **The settings file** (`.tiff_workflow_settings.json`, menu 6 of the wizard) takes the
  names of the last column below. A name of v3.0.0 in it is ignored with a warning, the
  script's own value stays: rewrite it in the new form. The wizard's own section
  (`convert_tiff`: `WORKER_MEMORY_FRACTION`, ...) did not change.
- **Presets and Repeat last** saved by v3.0.0 hold the old command line. v3.1 does not read
  it: running one says "cannot be replayed" and runs nothing. Run the workflow again from the
  menu and save the preset again; the old one stays in the config until you delete it.

## Translating a line

From a clone of the repository:

```
python tools/legacy_args.py compress -Mode 9 -InputDir "D:\my photos" -SafeMode:$false
--mode 9 --input-dir "D:\my photos" --no-safe-mode
```

The first word names the script (`compress`, `copy-exif`, `thumbnails`, or the script's file
name). Each argument is translated in place, its value as written; a line v3.0.0 would have
refused is refused (exit 2). With the Windows program, use the table.

## Every option

The value of an option is written after it, as before: `--workers 8`,
`--duplicate-action Skip`. The last column is the name in the settings file, for the options
the file can change.

### compress_tiff_zip.py (`convert_tiff.py compress`)

| v3.0.0 and before | v3.1 | settings file |
|---|---|---|
| `-Mode <value>` | `--mode <value>` |  |
| `-InputDir <value>` | `--input-dir <value>` |  |
| `-OutputDir <value>` | `--output-dir <value>` |  |
| `-ZipSuffix <value>` | `--zip-suffix <value>` | `zip_suffix` |
| `-ZipSubfolderName <value>` | `--zip-subfolder-name <value>` | `zip_subfolder_name` |
| `-ExportMarker <value>` | `--export-marker <value>` | `export_marker` |
| `-ExportZipSubfolder <value>` | `--export-zip-subfolder <value>` | `export_zip_subfolder` |
| `-ExportTiffSubfolder <value>` | `--export-tiff-subfolder <value>` | `export_tiff_subfolder` |
| `-Workers <value>` | `--workers <value>` |  |
| `-CapWorkers <value>` | `--cap-workers <value>` | `cap_workers` |
| `-DryRun` / `-DryRun:$false` | `--dry-run` / `--no-dry-run` |  |
| `-SafeMode:$true` / `-SafeMode:$false` | `--safe-mode` / `--no-safe-mode` |  |
| `-SkipLzwAsCompressed:$true` / `-SkipLzwAsCompressed:$false` | `--skip-lzw-as-compressed` / `--no-skip-lzw-as-compressed` |  |
| `-Overwrite` / `-Overwrite:$false` | `--overwrite` / `--no-overwrite` |  |
| `-DeleteSource` / `-DeleteSource:$false` | `--delete-source` / `--no-delete-source` |  |
| `-ForceParallel` / `-ForceParallel:$false` | `--force-parallel` / `--no-force-parallel` |  |
| `-ForceSequential` / `-ForceSequential:$false` | `--force-sequential` / `--no-force-sequential` |  |
| `-StagingDir <value>` | `--staging-dir <value>` |  |
| `-DuplicateAction <value>` | `--duplicate-action <value>` |  |
| `-MagickTimeout <value>` | `--magick-timeout <value>` | `magick_timeout` |
| `-ExcludeFolders <value>` | `--exclude-folders <value>` |  |
| `-GenerateThumbnail` / `-GenerateThumbnail:$false` | `--generate-thumbnail` / `--no-generate-thumbnail` |  |
| `-ThumbSize <value>` | `--thumb-size <value>` | `thumb_size` |
| `-ThumbQuality <value>` | `--thumb-quality <value>` | `thumb_quality` |
| `-ThumbFormat <value>` | `--thumb-format <value>` | `thumb_format` |
| `-ThumbPage <value>` | `--thumb-page <value>` |  |
| `-SkipCompressedWithThumb` / `-SkipCompressedWithThumb:$false` | `--skip-compressed-with-thumb` / `--no-skip-compressed-with-thumb` |  |
| `-SrgbProfile <value>` | `--srgb-profile <value>` | `srgb_profile` |
| `-WorkerMemoryFraction <value>` | `--worker-memory-fraction <value>` | `worker_memory_fraction` |
| `-InputDirFile <value>` | `--input-dir-file <value>` |  |
| `-RestoreStranded` / `-RestoreStranded:$false` | `--restore-stranded` / `--no-restore-stranded` |  |

### copy_exif_to_tiff.py (`convert_tiff.py copy-exif`)

| v3.0.0 and before | v3.1 | settings file |
|---|---|---|
| `-InputDir <value>` | `--input-dir <value>` |  |
| `-Workers <value>` | `--workers <value>` |  |
| `-DryRun` / `-DryRun:$false` | `--dry-run` / `--no-dry-run` |  |
| `-SkipIfTiffHasExif` / `-SkipIfTiffHasExif:$false` | `--skip-if-tiff-has-exif` / `--no-skip-if-tiff-has-exif` |  |
| `-SkipLzwAsCompressed` / `-SkipLzwAsCompressed:$false` | `--skip-lzw-as-compressed` / `--no-skip-lzw-as-compressed` |  |
| `-SafeMode:$true` / `-SafeMode:$false` | `--safe-mode` / `--no-safe-mode` |  |
| `-IccPolicy <value>` | `--icc-policy <value>` |  |
| `-CompressZip` / `-CompressZip:$false` | `--compress-zip` / `--no-compress-zip` |  |
| `-OutputDir <value>` | `--output-dir <value>` |  |
| `-StagingDir <value>` | `--staging-dir <value>` |  |
| `-Overwrite` / `-Overwrite:$false` | `--overwrite` / `--no-overwrite` |  |
| `-AutoFind` / `-AutoFind:$false` | `--auto-find` / `--no-auto-find` |  |
| `-FolderPattern <value>` | `--folder-pattern <value>` |  |
| `-MagickTimeout <value>` | `--magick-timeout <value>` | `magick_timeout` |
| `-FailOnWarn` / `-FailOnWarn:$false` | `--fail-on-warn` / `--no-fail-on-warn` |  |
| `-ExcludeFolders <value>` | `--exclude-folders <value>` |  |
| `-WorkerMemoryFraction <value>` | `--worker-memory-fraction <value>` | `worker_memory_fraction` |
| `-InputDirFile <value>` | `--input-dir-file <value>` |  |

### generate_thumbnails.py (`convert_tiff.py thumbnails`)

| v3.0.0 and before | v3.1 | settings file |
|---|---|---|
| `-InputDir <value>` | `--input-dir <value>` |  |
| `-Size <value>` | `--size <value>` | `size` |
| `-OutputDir <value>` | `--output-dir <value>` |  |
| `-Remove` / `-Remove:$false` | `--remove` / `--no-remove` |  |
| `-DryRun` / `-DryRun:$false` | `--dry-run` / `--no-dry-run` |  |
| `-Recursive` / `-Recursive:$false` | `--recursive` / `--no-recursive` |  |
| `-Workers <value>` | `--workers <value>` |  |
| `-Page <value>` | `--page <value>` |  |
| `-Quality <value>` | `--quality <value>` | `quality` |
| `-Format <value>` | `--format <value>` | `format` |
| `-SrgbProfile <value>` | `--srgb-profile <value>` | `srgb_profile` |
| `-ExcludeFolders <value>` | `--exclude-folders <value>` |  |
