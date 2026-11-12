# Changelog

Release-level history, moved here from `README.md`. Per-bug technical detail -- what broke, why,
which code path, and which test pins it -- is in [`bugs_fixed.md`](bugs_fixed.md). Downloads are on
the [GitHub Releases page](https://github.com/rsilvabr/tiff-workflow/releases).

> **Numbering.** Sections carry the version the work shipped in. Audit rounds are the stable
> identifier and match the `fix(audit-round-N)` commits: rounds 8-14 shipped as **v2.3**, rounds
> 15-16 as **v2.4**, rounds 17-22 as **v2.5**.

---

## Unreleased (3.1.0)

Work in progress on the branch `python-v3.1`; nothing here is released.

- **Presets and *Repeat last* saved by v3.0.0 or earlier in the PowerShell form are no longer run.** Until v3.0.0 the wizard still read a command line the PowerShell edition had saved (`pwsh -File <script>.ps1 args`, or the `-Command` form of Windows PowerShell 5.1). Such a preset stays in the list; choosing it says `Saved workflow cannot be replayed` and runs nothing. Run the workflow once more from the menu and save it again. Presets saved by v3.0.0 itself (they name a `.py` script) keep working
- **`compress_tiff_zip.py` needs `-Mode`; the legacy mode is gone.** Until v3.0.0, `python compress_tiff_zip.py` with no `-Mode` compressed every TIFF under the *current* folder in place, with no `OLD_TIFFs` backup. A forgotten `-Mode` now does nothing: the script says `-Mode is required` and exits with code 1 (so does `-Mode -1`). What it did -- the folder and its subfolders, each file replaced in place after a pixel check -- is mode 8 (`-Mode 8 -InputDir .`; a `.tiff` gets its `.tif` next to it and is only removed with `-DeleteSource`); mode 9 does the same and keeps the originals in `OLD_TIFFs/` (mode 0: one folder, without its subfolders). Mode 2 is *not* a replacement: its output is flat, so the files of subfolders would be copied into the root. The wizard always passed `-Mode`: nothing changes there. New files no longer get the `xmpMM:History` note `compress_tiff_zip legacy: ...`
- **copy_exif: `-ExcludeFolders` now applies to `-AutoFind`.** The flag was accepted and did nothing in this backend. A session folder that sits below a folder with an excluded name (or is named so) is no longer processed, and the log says how many were left out (`Excluded 1 folder(s) under: temp`). Names are matched below the scan root, as in the other backends
- **A missing folder listed twice in `-InputDir` is one error in every backend.** copy_exif and generate_thumbnails logged and counted it once per mention, compress once. The three now read their input folders with the same function; the exit code is 1 as before
- **compress, three result lines corrected.** Modes 0/9 with `-GenerateThumbnail -SkipCompressedWithThumb` on a file that is already compressed and has its thumbnail said `SKIP (Adobe Deflate) ... [no thumbnail added: pass -SkipCompressedWithThumb]` -- the option that was passed; they now say `SKIP (compressed+thumb)` like the other modes. Mode 2 with `-DuplicateAction Skip`: the line of the skipped duplicate has its `[n/total]` prefix. `-Workers 8 -CapWorkers 2` with memory for 4 logged `Workers reduced from 8 to 4` while 2 ran: the memory limit now starts from the capped value, and the line appears only when memory lowers it further
- **Counting the pages of a TIFF no longer decodes it.** The page count behind SafeMode and the page gates ran `magick identify` without `-ping`, which decodes every page: seconds on a large scan, about half the time of a Copy EXIF run, and on a busy machine a healthy file could run past the timeout and be reported as `ERROR (magick timeout) ... possibly corrupted` (nothing was touched, but the run ended with exit code 1). It now reads the file's directories only. Checked on 104 real TIFFs (camera exports, film scans with preview and infrared pages, camera thumbnails): the same count on every one, 91 s against 4 s. What the count answers is unchanged; whether the pixels decode is still decided by the decode and pixel gates
- **`-OutputDir` naming an existing file is refused before the run starts**, in the three backends: `ERROR: -OutputDir is a file, not a folder: <path>`, exit code 1, no log created. compress used to end in `FATAL (unhandled error) | FileExistsError` in the middle of the run (also exit code 1, nothing touched)
- **compress mode 2 with its output in the input folder: a photo is never replaced by another one.** Mode 2 writes every output into one folder; without `-OutputDir` that folder is the input folder itself, and the files of its subfolders become copies in the root. With `-Overwrite`, a file of the root was replaced by a file of the same name from a subfolder -- another photo, the only copy of the first one gone, `OK` and exit code 0. That file is now refused (`ERROR (mode 2 would replace another source file of this run - refused; pass -OutputDir)`, exit code 1) and the photo of the root is compressed in its own place. The copies themselves are still made, and announced before the work: `Mode 2 writes into the input folder itself: N file(s) from its subfolders will be COPIED into <folder> ...`. The wizard already asked for confirmation here; this is for the script run directly
- **Restore OLD_TIFFs: a restored `.tiff` takes its place alone.** Modes 0/9 write `x.tif` from an `x.tiff` original; restoring all put `x.tiff` back next to `x.tif`, and the folder kept both without a word. The original now goes back first, then the compressed `x.tif` moves into the `ZIP` subfolder of that folder (where mode 1 puts compressed copies; a name already there gets `_v2`), with a `MOVED ASIDE` line. Nothing is deleted, and if the original cannot go back the compressed copy stays where it is
- **Delete OLD_TIFFs checks again right before it deletes.** Each backup is compared with its copy before the questions, and deleted after them -- minutes later, or at the end of a big folder. If the copy changed in between (an edit, another run) or is gone, the backup is now kept and listed (`KEPT (changed since it was checked)`), and the summary says to run Delete OLD_TIFFs again for those. What it compares at that moment is the size and the write time of the two files
- **Wizard: Workers and Staging read the same on both screens.** Enter keeps the staging folder shown (the last one used) and `-` turns staging off -- the Rich screen had no way to turn a remembered folder off, the plain screen forgot it on Enter. A Workers answer that is no number uses the value shown, and one outside 1-64 is clamped and said
- **Wizard, Diagnose: the temp folder is one question.** The Rich screen asked "Use a specific temp directory?" and then the path; the plain screen asked for the path under a question that read like yes/no, so `y` made a folder named `y` in the current folder. Both screens now ask `Temp folder for the checks (Enter = the system temp folder)`; a yes/no answer is read as one (yes asks for the path), and a relative path is made absolute and shown before it is used
- **Wizard: every yes/no question reads the same words.** `y`, `yes`, `s`, `sim` are yes and `n`, `no`, `nao`, `não` are no, on both screens. On the plain screen `yes` typed out counted as NO in 22 questions, `s`/`sim` was NO nearly everywhere, and where the default is yes only a bare `n` said no: `no` at "Proceed?" ran the compression and `no` at Restore's "Move all files back?" moved the files. The Rich screen took only `y`/`n`. An answer that is none of these keeps the question's default on the plain screen (always "no" where something is deleted or replaced) and is asked again on the Rich one
- **Wizard, small things.** The manifest's combined log is written to `Logs\convert_tiff\` under the current folder, beside the backends' logs and Diagnose's report (it went next to the scripts), and names the backends' log folder by its full path. A folder answer is kept as typed (a mapped or SUBST drive letter stays; it was replaced by the network path). AutoFind lists the sessions in one order, the first run and a preset alike
- **Wizard, Restore: the same "nothing in its place" rule as the backends.** The wizard decided with its own copy which originals in `OLD_TIFFs` had nothing left in their place, and it disagreed with compress's in three cases: it walked into a backup folder nested in another (`OLD_TIFFs\sub\OLD_TIFFs\deep.tif` would have been restored into `OLD_TIFFs\sub\`), it missed the singular `OLD_TIFF` the backends also write, and it took a folder of the photo's name for the photo. It now asks the backends' own rule; "restore all" never touches a nested backup folder either
- **Wizard, manifest: no false collision from a backup.** The check that two files of a manifest run would land on one name counted the files inside `OLD_TIFFs` (and the run's `-ExcludeFolders`), which the backend never reads: `a.tif` beside `OLD_TIFFs\a.tif` stopped a mode 2 run as a collision
- **Wizard: a temp folder with `[ ]` in its name works.** Diagnose and the padded conversion reported every file as `ERROR: 8-bit conversion failed` when the temp folder had square brackets (`D:\tmp [x]`); nothing was touched, nothing was done. Fixed
- **Wizard, AutoFind no longer offers a backup as a session.** A folder matching the pattern INSIDE `OLD_TIFFs` (or another tool folder: `ZIP`, `OLD_PADDED`, ...) -- `S5pro 01\OLD_TIFFs\S5pro bak\` -- was listed as a session to process, because only the folder's own name was checked. Every level below the chosen folder counts now, as in copy_exif's own `-AutoFind`
- **Wizard: Restore, the config file and the Script settings menu.** Restore moves files the way the backends land them (one replacing rename; never a copy-then-delete on a locked file). The config file that holds the presets is written whole through a temp file (a run killed mid-write lost every preset). The Script settings menu refuses an overlay file the scripts refuse (two keys that differ only in case, an empty key), instead of opening it. A folder name that looks like Rich markup (`[bold]`, `[/b]`) is shown as it is; `[/b]` used to stop the wizard right before a run
- **exiftool 12.15 or later.** compress, and copy_exif with `-CompressZip`, read the result of each exiftool command of one run (see "Fewer exiftool runs per file" below); an older exiftool cannot report it, and every file would have ended as an ERROR or a WARN although the writes were done. The run now checks it once at the start and stops before touching anything: `ERROR: exiftool 12.10 does not report the result of each command it runs; update it to 12.15 or later`. The Windows program ships 13.59
- **Two small changes of lines.** `-OutputDir` naming a file is refused in every mode, also in the modes that do not use `-OutputDir` (until v3.0.0, `-Mode 9 -OutputDir file.txt` ran). And a failure nobody foresaw while the metadata are written back now ends that file as `ERROR (unexpected)`, with nothing replaced; it used to be a `WARN` and the file went on
- **The same protection in every copy mode (1-7), not only mode 2.** Mode 5 sends `sub\x.tif` to `ZIP\x.tif`; when `ZIP\x.tif` was itself a photo of the run (an uncompressed TIFF living in a `ZIP` folder), `-Overwrite` replaced it with the other photo, `OK` and exit code 0 -- already so in v3.0.0, found by the review of v3.1. Mode 4 could do the same with a source in a folder ending in the suffix. That file is now refused (`ERROR (mode 5 would replace another source file of this run - refused)`, exit code 1) and the photo in the way is compressed as usual. Modes 0, 8 and 9 write in place and are unchanged
- **generate_thumbnails says when it uses fewer workers than asked.** `-Workers` accepts up to 64 and at most 16 thumbnails were made at once, without a word. The limit is the same (the setting `MAX_PARALLEL` at the top of the script); a run that asks for more now logs `Workers: 64 asked, 16 used`
- **Fewer exiftool runs per file.** compress read the source's tags in up to two exiftool runs and wrote the metadata, the page markers and the history entry in three more; copy_exif `-CompressZip` did the same. Each file now takes one read before the conversion and one write after it (the three writes are commands of one exiftool, each still checked on its own). Measured with one worker on the same machine: 20 small TIFFs in mode 9, 31.6 s → 26.4 s; on 120 MB photos and film scans, where the conversion and the pixel check are most of the time, 3-4%. With the page count of the previous item, Copy EXIF on 8 Fuji TIFFs went from 25.2 s (v3.0.0) to 13.1 s. What is written and checked is unchanged
- **A file that ends in an error leaves none of its temporary files behind.** Two leftovers of compress and copy_exif are gone. With `-StagingDir` in the modes 1-7, a staged file refused by the page gate (`ERROR (page count shrink)`) stayed in the staging folder until someone deleted it. And when a step failed in a way nobody had foreseen (`ERROR (unexpected)`), the temp sibling next to the photo, the metadata container and -- in copy_exif with `-OutputDir` -- the copy and the staged ZIP stayed on disk; the copy was then skipped as `exists in OutputDir` by the next run, without its EXIF. Each worker now removes what it made for that file, whatever the error. Nothing changes for a file that succeeds, and an original is never among these files

## v3.0.0

**The backends are Python now.** Until v2.9.1 the wizard was Python and the backends were
PowerShell scripts (four files: Copy EXIF had one per PowerShell version). They were rewritten
in Python, one file each. What a run does to your files is the same: before the PowerShell
scripts were removed, each was run next to its replacement on copies of the same files with the
same arguments -- 63 scenarios, synthetic and real photos, every mode -- and the resulting
files, pixels, metadata, exit codes and result lines were the same.
[The PowerShell edition](powershell_edition.md) says what to change if you call the scripts
yourself, and what was found wrong in the old scripts on the way.

- **No PowerShell needed.** Python 3.9+, ImageMagick and exiftool run everything. The backends
  use only what Python ships with (`rich` is still only the wizard's)
- **The same command line.** `python compress_tiff_zip.py -Mode 9 -InputDir F:\Photos` takes
  the parameters `compress_tiff_zip.ps1` took, written the same way (`-Name value`, `-Switch`,
  `-SafeMode:$false` or `-SafeMode:false`, names without case)
- **Presets, *Repeat last* and manifests saved by v2.x keep working.** A saved PowerShell
  command line is read and run with the Python script and the same arguments
- **Several files at once on every machine.** That used to need PowerShell 7; Windows
  PowerShell 5.1 worked one file at a time. `-ForceSequential` (compress) or `-Workers 1` turn
  it off; `-ForceParallel` is still accepted and does nothing
- **Copy EXIF is one script**, `copy_exif_to_tiff.py`, in place of `copy_exif_to_TIFF_ps5.ps1`
  and `copy_exif_to_TIFF_ps7.ps1`
- **`tiff_workflow_common.py`** holds what the three backends share (it must stay next to
  them). The PowerShell scripts carried up to four copies of each helper, and a fix made in
  one copy and not in the others was this project's most frequent bug
- **Settings:** the defaults a user may edit are the `PARAMS` table at the top of each script.
  The settings file (`.tiff_workflow_settings.json`, main menu **[6]**) is unchanged
- **Fixed on the way** (present in v2.9.1, none damages a file): with `-InputDir .`, an
  `-ExcludeFolders` entry equal to the folder's own name excluded the whole run in
  `generate_thumbnails` and Copy EXIF; a Copy EXIF dry run created the `-OutputDir` folder;
  legacy mode with a file where a folder was expected, and `-Mode 10` or higher, ended with
  exit code 0; a check that timed out left its `magick.exe` running; Ctrl+C in modes 0/9 left
  the temporary copies it was writing next to the photos
- **Output:** the first log line names the Python script; a multi-page file skipped by Safe
  mode always reads `MULTI (N pages - skipped)`; `-- Group:` shows the folder as spelled on
  disk; `Workers capped to N (CapWorkers limit)` is printed once, not once per folder
- **Windows program:** [tiff-workflow-exe](https://github.com/rsilvabr/tiff-workflow-exe) no
  longer carries PowerShell 7
- **Tests:** the suite drives the Python backends end to end in two lanes, parallel and
  sequential (what the two shells were). The rules that used to be checked on the shape of the
  PowerShell source are now checked by running the code with the tools replaced
  (`tests/test_backend_invariants.py`). Pester is gone. 944 tests and the real-photo battery
  (394 checks, in both lanes) pass

## v2.9.1

Fixes only (audit round 28: a bug sweep of v2.9.0, 22 findings, and its review).

- **A black-and-white photo stored as RGB stays RGB.** When every pixel of an RGB TIFF had
  R=G=B (B&W film scanned in colour mode, a B&W conversion exported as RGB), ImageMagick wrote
  the compressed file with ONE gray channel and left the RGB colour profile in it. The tones
  were all there, but the file was no longer RGB, and a gray file with an RGB profile is a pair
  some programs reject. A VueScan RGB + infrared scan in the same case (4 samples, R=G=B) came
  out scrambled in modes 0-7 and 9 (mode 8 refused it). Every check said OK, because they all
  decode through ImageMagick, where gray and neutral RGB look the same. The compressed file
  now keeps the channel layout of the source, and a new check compares it page by page before
  anything is replaced. **If you compressed such files with v2.9.0 or earlier, see the release
  notes of v2.9.1**
- **IPTC text outside Latin (Japanese, Greek, ...) survives.** With a `%` anywhere in the path,
  or in an in-place run, keywords and captions stored in IPTC came back as `???` (XMP was not
  affected). Copy EXIF did the same to a JPEG's UTF-8 IPTC
- **No swap leaves a photo's place empty.** Replacing a file is now a single rename everywhere
  (PowerShell's own move deletes the destination first). Copy EXIF with `-CompressZip` and a
  staging folder on another drive copied straight into the photo's name; the padded 16 -> 8-bit
  conversion moved the original out before its replacement had arrived. Both now bring the new
  file next to the photo first and swap last
- **A file whose metadata could not be restored never replaces the original** (mode 8 and
  in-place runs; it used to be a warning). On PowerShell 7 an error inside a worker could even
  report `OK [SOURCE DELETED]` for a file left without EXIF
- **With a staging folder, an in-place run is checked like any other** (the pixel check was
  skipped when the destination was the file itself)
- **Settings file:** a folder name in it can no longer turn a copy mode into an in-place
  replace, and the PowerShell scripts now refuse exactly the files the wizard refuses (comments,
  trailing commas, UTF-16, ...), so both always run with the same values
- **Delete OLD_TIFFs** also compares XMP, IPTC and the channel layout before it deletes a backup
- Associated alpha that is not fully opaque is refused (ImageMagick cannot rewrite it without
  changing samples); such a file is left as it is and reported
- Smaller: a backup named like `Scan_20190815_143000.tif` is restored under its own name;
  `move_old_backups.py` keeps a `name_v2.tif` whose place is empty; AutoFind skips a folder with
  `;` in its name; presets store absolute folders; `--run-preset` refuses every spelling of
  `-DeleteSource` and mode 8 when unattended; mode 8 without `-DeleteSource` no longer makes
  `_v2`, `_v3` copies of a `.tiff`; `generate_thumbnails.ps1 -Remove` keeps a full-size TIFF
  named `*_thumb.tif`; a read-only TIFF is still replaced

## v2.9.0

- **A settings file instead of editing the scripts.** The settings at the top of the scripts
  -- folder names (`ZIP`, `_EXPORT`, ...), timeouts, thumbnail defaults, the memory share of the
  workers -- can live in `.tiff_workflow_settings.json`, next to the scripts or in your user
  profile (or wherever `TIFFWORKFLOW_SETTINGS` points). The wizard and the four PowerShell
  scripts read it, in PowerShell 5.1 and 7, so an update no longer means re-applying your
  edits. Main menu **[6] Script settings** edits it. It can never change anything that deletes,
  overwrites or skips a safety check; a bad file or value changes nothing and is reported, and
  a `-Param` typed on a command line still wins
- **Advanced options per run:** after the basic parameters the wizard offers the options it
  never asked (ImageMagick timeout, sRGB profile, memory share, fail on warnings). Only what
  you change is passed; presets and Repeat last keep it
- Every option of every script is now known to the wizard in one place, and a test fails when
  a script gains one nobody decided about. The commands the wizard builds are byte for byte
  the same as before

## v2.8.0

- **exiftool works when the Windows temp folder has characters outside the system's code page**
  (a user name in Japanese, Greek, Polish...). Every exiftool call used to fail there --
  compress refused every file with "cannot detect compression", Copy EXIF with "exiftool
  EXIF", and the wizard's checks could not read any tag. Nothing was ever written wrong: the
  runs stopped with an error. exiftool now gets its arguments on stdin (round 27)
- **Every TIFF compress rewrites records it in the photo's `xmpMM:History`** (tool, version,
  date, mode), like the padded 16 -> 8-bit conversion since v2.7. Earlier entries (Capture One,
  scanner software) are kept, a second run adds nothing, and a file whose entry cannot be
  written is still a good ZIP (a `WARN`). About half a second to a second per file
- **The release is in every script and log:** `python convert_tiff.py --version`, the main
  menu's title, and the first line of each backend's log and of the wizard's own reports
- **New-release notice:** the main menu looks once a day, in the background, for a newer
  release and offers `U` to open its page. One anonymous request to GitHub, nothing else sent;
  never for `--run-preset`. Off with `"check_for_updates": false` in the config file or the
  environment variable `TIFFWORKFLOW_NO_UPDATE_CHECK`

## v2.7.4

- **Modes 0 and 9 keep each original in place until its compressed copy is ready.** The copy is
  written beside it, checked, and only then is the original moved to `OLD_TIFFs` and the copy
  put in its place. These modes used to move every original of the run to `OLD_TIFFs` first,
  so a run stopped by closing the window or a power cut could leave folders without their
  photos (the case v2.7.3 learned to detect). An edit saved to a photo during the run is kept
  (round 26)
- **A staging folder on another drive is safe in every mode.** A staged file now reaches its
  place as a copy beside it plus a rename; before, mode 8 copied it straight over the original,
  so a run stopped mid-copy could leave a damaged file (round 26)
- **The wizard helps with stranded originals:** after a run that reports them it offers to
  put them back, and *Restore OLD_TIFFs* can restore only those. The check is also faster
  (round 26)

## v2.7.3

- **Compress notices originals left behind in `OLD_TIFFs`.** Modes 0 and 9 move a folder's
  originals to `OLD_TIFFs` before writing the compressed copies; a run stopped by closing the
  window or a power cut could leave some there with nothing in their place, and later runs did
  not notice. Every run, in any mode, now lists them (`STRANDED`, also counted under `Done:`),
  and `-RestoreStranded` puts them back -- the same run then compresses them (round 25)
- **`tools/move_old_backups.py`**, the script that moves `OLD_PADDED` / `OLD_TIFFs` folders to
  another drive, joins the repo and never moves a backup whose original is missing -- that
  backup is the only copy (round 25)

## v2.7.2

- **A preset runs the backends next to the wizard it is started from.** It used to run
  the backend at the path recorded when it was saved: after moving or updating the
  toolkit into another folder, a preset failed with "Backend script no longer exists", or
  quietly ran the older copy if that folder was still there (round 24)

## v2.7.1

- **Copy EXIF: `-SkipIfTiffHasExif` works.** It checked for any EXIF tag, and in exiftool
  that includes the basic tags every TIFF has (size, resolution), so it skipped every TIFF --
  including the Hyper Utility exports it is meant for (451 of 451 on a real S5 Pro folder).
  It now looks for camera EXIF only (the Exif IFD: exposure, aperture, dates). The wizard
  never passes it, so wizard runs and presets were not affected (round 23)
- The Copy EXIF reference says where its log goes (`Logs\Copy-S5Pro-Exif\` under the folder
  the run starts from)

## v2.7

- **The padded 16 -> 8-bit conversion records itself in the photo.** It appends one entry
  to `xmpMM:History`, in the format jxl-photo uses: `Action=converted`, `When`,
  `SoftwareAgent=tiff-workflow <version> (<link>)` and
  `Parameters=convert_tiff: 16-bit padded -> 8-bit ZIP (pixel-identical)`. Earlier entries
  (Capture One, ...) are kept, and the entry travels on into a jxl-photo master made from the
  file. If the entry cannot be written, the original is kept, as for a failed EXIF copy.
  Read it with `exiftool -struct -XMP-xmpMM:History file.tif`
- **No silent phases in Diagnose.** The folder search reports every few seconds (folders
  scanned, TIFFs found, elapsed), the image-size read for the memory estimate reports each
  batch, and the checks print `N/total checked in X -- about Y left` every 25 files
- **Diagnose no longer cuts the workers because of one huge scan.** The memory cap sized
  every worker for the largest file of the batch: one 220 MP scan among 32 MP photos made
  each worker count as 15 GB. Diagnose now keeps the requested workers and each check
  reserves the memory its own file needs; a large file waits for room (alone if it must)
- **Large scans no longer time out the header probes.** `magick identify` (bit depth,
  dimensions, page count, SubfileType) ran without `-ping`, so it decoded the whole image
  under a fixed 30 s limit: 782 MB film scans on a busy hard disk failed with
  "timed out after 30.0 seconds". The probes now read the header only and their limit grows
  with the file size and the number of workers
- **A Diagnose preset can convert what it finds.** Workflow 7 asks whether a repeat or
  preset should also convert the padded files (default no). Answered yes, it runs the
  menu's conversion right after the check, unattended too -- pixel-verified, originals in
  `OLD_PADDED/` -- and the report says how many were converted
- **Folder names in Japanese (or any non-ANSI script) work in the wizard's own steps.**
  exiftool reads its command line in the ANSI code page, so under `伊豆沼_夏` every metadata
  step failed: the padded conversion refused every file (`subfile_restore_failed`) and
  Delete OLD_TIFFs refused every purge. All its exiftool calls now pass their arguments in a
  UTF-8 argfile, as the PowerShell backends always did
- **The padded conversion finishes each file as soon as it is ready** -- printed, backed up,
  moved into place, with `N/total done -- about X left` every 25 files -- instead of
  converting the whole batch first and replacing everything at the end; it also budgets
  memory per file like Diagnose
- **Japanese folder names show correctly on screen.** The backends' output reached the
  wizard in the console's OEM code page, so `谷津バラ園` printed as `?????` (the log file was
  always right). Each backend writes UTF-8 when its output is redirected
- **PowerShell 5.1 started from a folder with `[ ]` in its name** silently began in
  `System32` and died before its first log line; every backend now re-enters the real
  start folder, and builds its log path with `[System.IO.Path]` instead of `Join-Path`

## v2.6.2

- **Temporary copies no longer pile up in `%TEMP%`.** ImageMagick cannot open a path with
  `[` `]` on Windows (a `[FINAL]` folder), so the wizard reads such files through a
  bracket-free alias in `%TEMP%`. When the photos sit on another drive than `%TEMP%` the alias
  is a full copy, and every one was kept until the run ended -- about three copies per file. A
  Diagnose over 5000 TIFFs filled 434 GB of the system drive. Each file now gets one alias,
  deleted as soon as that file is done (Diagnose, the padded conversion, Delete OLD_TIFFs)
- The one-time creation of the alias folder is properly locked (parallel workers made several)
- Diagnose's progress line shows the file's path below the scan root
  (`2025\Shoot [FINAL]\_EXPORT\TIFF16\DSC_0001.tif`), not only its name, so same-named files
  in different sessions can be told apart

## v2.6.1

- The Diagnose cache is written every 25 files and when a run is stopped, not only at the
  end, so an interrupted run keeps what it already checked
- Workflow 7 from the menu uses the cache too: converting what a preset found no longer
  re-checks the whole library
- The padded 16 -> 8-bit conversion compares the rewrite with the original pixel by pixel
  and keeps only identical results. It used to trust the Diagnose verdict, so a file
  wrongly passed to it would have been rounded down to 8-bit

## v2.6

One new feature; nothing changes for the other workflows.

- **Diagnose as a preset** -- workflow 7 can now be repeated and saved as a preset. A preset
  run re-scans the saved folders and only reports (full paths in
  `Logs\convert_tiff\diagnose_<timestamp>.log`); it never converts. Two new questions narrow
  the search: a folder path the files must sit under (`_EXPORT/TIFF16`) and file-name
  patterns to skip (`*NXSTUDIO*`). Files already checked and unchanged are not checked again
  (`DIAGNOSE_CACHE`)

## v2.5

Audit rounds 17-22. New features plus hardening; nothing changes for a run that already works.

- **Presets** -- save the last workflow under a name (main menu **[5]**) and run it unattended with
  `python convert_tiff.py --run-preset NAME` (`--list-presets`, `--dry-run`). A Fuji preset redoes
  the AutoFind each time, so new sessions are picked up. An unattended preset that would delete
  sources is refused unless `--dry-run`
- **Parallel workers fit in memory** -- the number of parallel ImageMagick jobs is capped by the
  free memory (`-WorkerMemoryFraction`, default 0.8; 0 turns the cap off), so big film scans no
  longer push the machine into swapping
- **Copy EXIF finds Capture One variants** (`_DSF4817_1.TIF` -> `_DSF4817.JPG`) and falls back
  to the RAF when a session has no JPEG
- **Long AutoFind lists** -- a folder list past the Windows command-line limit is passed to the
  backend through a temporary file (`-InputDirFile`)
- The wizard reads the backends' own settings instead of keeping copies, and passes them
  explicitly, so an edited default in a `.ps1` is respected
- Copy EXIF with `-CompressZip` counts a file that got its EXIF as OK in the summary
- Hardening: subprocess calls without reader threads, timeouts that scale with the number of
  workers, and helper functions pinned identical across the PowerShell backends
- New `tools/real_photo_battery.py`: the backends end to end on copies of real photos, under both
  shells

**Upgrading:** "Repeat last" of a manifest workflow saved by v2.4 is refused with a message --
run the manifest once more from menu **[3]** and it is recorded with the new data.

Per-bug detail: [`bugs_fixed.md`](bugs_fixed.md), rounds 17-22.

---

## v2.4

Audit rounds 15 and 16. Round 15 is the section below -- it was written while unreleased and landed
after the v2.3 tag. Round 16 (interrupt rollback, metadata-aware purge gate, fail-closed metadata
restores in Copy EXIF, size-scaled timeouts) is covered in `RELEASE_v2.4.md` and, per bug, in
[`bugs_fixed.md`](bugs_fixed.md).

### Audit round 15

Two of these lose data silently; if you have file names containing `%`, this round matters.

- **A `%` in a file name stripped every metadata tag** and the run still said `OK`. exiftool
  expands printf-style filename codes in the `-tagsfromfile` argument (`50%off.tif` is read as
  `50ff.tif`), warns, and **exits 0** — so the check every backend relied on passed. In mode 8
  with `-DeleteSource` it was permanent. Doubling the `%` does not escape it; the metadata now
  travels in a container whose name has none. Every `-tagsfromfile` call also fails on
  `Error opening file` now, whatever the exit code
- **Modes 1-7 walked into `OLD_TIFFs/` and compressed the backups.** The real file is already
  Deflate so it was skipped, which left the *backup* to claim the output name — a mode 2 run
  over a mode 9 tree produced a flat folder holding nothing but `_v2` copies of backups. One
  exclusion list now covers every mode, relative to the folder you point at
- **The thumbnail generator no longer refuses `ZIP/` and `_EXPORT/` trees** — that is where the
  compressed library and the export trees live, and the exclusion was silent, undocumented and
  impossible to turn off. It takes `-ExcludeFolders` now and logs what it left out
- **`-Page all` no longer makes `-Page 0` unrunnable**: the frame set only counts as "already
  there" when `-Page all` was actually asked for
- Files left behind by an interrupted run are no longer discovered as photos
- Workflows [6] and [7] work on `%` names (the Python side never got the escaping fix)
- Mode 8 says at startup that it replaces each original in place, with or without
  `-DeleteSource` — it always did, and the docs implied otherwise

---

## v2.3

Six audit rounds (8-13), driven by real scanner and Capture One files:

- **Folder exclusion** — `-ExcludeFolders "_EXPORT;temp"` skips whole folder trees by name, in every mode and in all three backends; the wizard asks once per workflow. Matching is relative to the folder you point at, so a name appearing higher up the path never excludes anything
- **Worker cap decoupled from Safe Mode** — `-CapWorkers` (default: no cap), so Safe Mode no longer quietly limits parallelism
- **Discovery is much faster** — the "already compressed?" probe is batched, one `exiftool` per ~400 files instead of one process per file
- **Colour-managed thumbnails** — wide-gamut sources are converted through their embedded ICC profile before it is stripped, so a ProPhoto export finally produces a thumbnail that looks like the photo
- `-GenerateThumbnail` no longer silently drops pages, and no longer leaves the main image marked as a page of a multi-page document; `-ThumbPage 0` is refused, because those files were classified multi-page by the tool's own Safe Mode on every later run
- **Mode 8 works on deeply nested archives** — staged names were long enough to blow past Windows' 260-character path limit, which failed *every* file with an unexplained error; an over-long path is now refused up front, with the number
- In-place replacement and the mode 8 delete now require a pixel-identical check, not just "the file decodes"
- Safe Mode classifies a thumbnail by size as well as by tag, so a full-size IR `MASK` page is always treated as a real page
- "Repeat last workflow" rebuilds the command for the PowerShell it is about to run; it could previously turn Safe Mode back on

⚠️ **Breaking:** `-ThumbPage 0` is now refused (exit 1). It only ever produced files this toolkit would not touch again.

See [`bugs_fixed.md`](bugs_fixed.md) for the complete list.

---

## Earlier releases

Per-release notes for v2.0, v2.2, v2.3 and v2.4 are kept at the repository root
(`RELEASE_v2.0.md`, `RELEASE_v2.2.md`, `RELEASE_v2.3.md`, `RELEASE_v2.4.md`).
