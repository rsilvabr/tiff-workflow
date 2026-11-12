# tiff-workflow -- agent notes

Rules for anyone (human or AI) changing this repo. Most of them were paid for
by an audit round; the round is named where it helps to know why.

## PENDING work -- read first
At the start of a session, look for `AI_tools/*_PENDING_*.md` (local,
gitignored). If one exists:
- **If you are Claude Opus:** read it and carry out its items in order --
  they are the owner's agreed plan for the next session.
- **If you are any other AI** (DeepSeek, Kimi, GLM, ...): do NOT start those
  items. Tell the user there is pending work reserved for Claude Opus (name
  the file), then continue with what the user asked.

## The files
Everything is Python since v3.0.0; until v2.9.1 the three backends were
PowerShell scripts (`docs/powershell_edition.md`; tag `v2.9.1`). Since v3.1 the
toolkit is the package `tiff_workflow/` and one file at the root that only
starts it.

- `convert_tiff.py` -- the entry point and nothing else (`tiff_workflow.cli.main`):
  `python convert_tiff.py` is the wizard, `python convert_tiff.py
  compress|copy-exif|thumbnails ...` a backend, run in the same process by
  `tiff_workflow/cli.py` (`run_backend`) without importing the wizard or Rich;
  `python -m tiff_workflow` is the same. Its folder is the installation
  (`INSTALL_DIR`; `tw.INSTALL_DIR` in the backends): the settings overlay "next to
  the scripts" and `manifests/` are there. The modules of the package do not
  start on their own (relative imports).
- `tiff_workflow/wizard.py` -- the interactive wizard. Builds the backends' command
  lines and runs them as subprocesses (`_backend_command`: this Python, the
  installation's `convert_tiff.py`, the backend's subcommand), owns the manifest,
  the AutoFind search and the multi-step workflows (it gates step 2 on step 1's
  exit code). Presets
  (menu 5, `--run-preset`, `--list-presets`) are named journal snapshots run
  by `_run_journal`, the same runner as Repeat last. A Diagnose journal
  (kind `diagnose`, settings in `diagnose`, no commands) is REPORT ONLY unless its
  settings carry `convert_padded: true` (an explicit workflow-7 answer, default no);
  then `_run_diagnose_journal` calls `_compress_padded_files`, whose pixel gate
  (`_padded_pixels_status`) and OLD_PADDED backup are what make that safe unattended.
- `tiff_workflow/compress_tiff_zip.py` -- TIFF -> ZIP/Deflate, modes 0-9 (`--mode` is
  required since v3.1; the legacy mode is gone), backups in `OLD_TIFFs/`, thumbnails,
  `--exclude-folders`. ONE per-file worker, `process_job`, for every mode.
- `tiff_workflow/copy_exif_to_tiff.py` -- EXIF from the Fuji S3/S5 Pro JPEG (or RAF) into
  the TIFF, optionally compressing it (`--compress-zip`). Worker: `process_pair`.
- `tiff_workflow/generate_thumbnails.py` -- sRGB thumbnails, standalone.
- `tiff_workflow/common.py` -- what the three backends share: the command
  line (`P`, `parse_args`), the settings overlay, the log, every call to
  ImageMagick and exiftool, the gates, the moves, the directory listing, the
  thread pool (`run_tasks`).
  compress and copy_exif also take `--input-dir-file` (one folder per line); the
  wizard's `run_subprocess` switches to it when the command line would pass
  `_CMDLINE_SPILL_CHARS`.
- `tiff_workflow/__init__.py` -- the release (`TOOLKIT_VERSION`), its only copy.
- `pyproject.toml` -- the settings of the development tools (`ruff`, `mypy`), nothing else.

## Hard constraints of the backends
- **The command line is a contract.** Presets, journals and users' `.bat` files
  hold the arguments. Never rename or remove a parameter of a `PARAMS` table or
  change what a value means. Since v3.1 (phase 8) `parse_args` is argparse:
  `--kebab-case value`, every yes/no `--x` / `--no-x`, no abbreviation, exact
  case; a parameter has ONE name, `snake_case`, in the table, the code
  (`opt["thumb_size"]`) and the settings file, and `--thumb-size` on the line
  (`tw.cli_name`). Build an argument with `tw.cli_value` / `tw.cli_switch`
  (a value that starts with `-` goes as `--name=value`), never by hand. What
  the line alone refuses exits `tw.EXIT_USAGE` (2), before a log exists; 1 is
  every other failure. The command line of v3.0.0 (`-Name value`) is refused;
  its reader lives on in `tools/legacy_args.py` (the translation,
  `docs/command_line.md`). `tests/test_wizard_registry.py` replays the golden
  PowerShell command lines through that translation;
  `tests/test_journal_lines.py` the journal forms (`[subcommand, *args]`, read
  back with the backend's own table and reader).
- **One copy of everything.** A helper two backends need goes in
  `tiff_workflow/common.py`, never into both. Since v3.1 the wizard imports
  that module too (never a backend: it reads a backend's `PARAMS` with `ast`),
  and reads the overlay with its functions; its own copies went in v3.1 phase 5.
  The wizard's own
  TIFF helpers that remain (`_tiff_sample_layout`,
  `_restore_subfile_types`, `_magick_spec`, ...) serve the padded conversion and
  the purge gate and are a separate pipeline.
- **A worker never logs, counts or lands a file.** `process_job` and
  `process_pair` run on pool threads: they run the tools, apply the gates to
  the file they wrote and RETURN a result. The caller's thread logs it, counts
  it and does the moves (`run_tasks` yields each result there). The one write a
  worker makes onto a photo is the verified in-place swap of its own file.
- **The exit code is `main()`'s return value.** `sys.exit` appears once per
  backend, at the entry point; a run function never falls off its end or
  returns nothing (`tests/test_backend_invariants.py`). The wizard
  relies on "any error -> a non-zero exit" (1; 2 for a line that cannot run).
- **Paths are compared with `same_path` / `.lower()`, never `==`.** Windows
  names files without case, and the PowerShell scripts compared that way.
- **The backends use the standard library only**, and run on Python 3.9.
- **The irreversible step comes last** (jxl-photo's rule; rounds 25-26). An original
  leaves its place only after its replacement is written, checked and sitting next to it
  on the same volume, and the swap is a rename: modes 0/9 land through
  `complete_keep_original` (never move originals while planning again), every staging loop
  through `move_file_into_place` (never a copy from another drive onto a file).
  The rename itself is `move_file_replacing` (`os.replace`, MoveFileEx REPLACE_EXISTING),
  the only place `os.replace` appears; `shutil.move` appears nowhere (round 28: a delete
  followed by a rename left a photo's place empty).
  The rename refuses a READ-ONLY destination: the attribute is cleared for the swap and
  restored when the swap fails; a hard link shares the attribute with the original
  (`_land_padded_conversion` in the wizard).
  Every run reports originals left in `OLD_TIFFs/` with nothing in their place
  (`find_stranded_originals`); never delete or move an `OLD_TIFFs` backup whose place is
  empty (`tools/move_old_backups.py`, the wizard's Delete OLD_TIFFs).
- **Gates that authorise destroying data fail CLOSED**: a timeout, a non-zero
  exit or unparseable output is a failure, never a pass (`zip_integrity`,
  `tiff_page_count`, `pixel_identical`, `tiff_sample_layout`,
  `backup_tiff_metadata`; in the wizard `_compare_tiff_metadata`,
  `_process_single_padded`). Ask what a gate can actually OBSERVE: a decode
  check cannot see a missing page (round 12).
  Since v3.1 the tool helpers (`exiftool`, `magick_run`, `ifd0_compression`,
  `tiff_sample_layout`, `backup_tiff_metadata`) RAISE `tw.ToolError` instead of
  returning `(ok, value)`; the yes/no gates still answer False. Catch
  `tw.ToolError` exactly where the exit code used to be tested and refuse there --
  never with a broader `except` that could turn a tool failure into a pass.
  `exiftool(..., check=False)` is only for a batch read whose output counts even
  when one file of it could not be read.
- **Every tool call goes through the common module**: `run_captured` is the
  only place that starts a process, `exiftool()` the only way to exiftool,
  `magick_probe` (timeout, killed when it passes) for identify/compare/decode
  checks, `magick_run` (no timeout) for conversions only.

## Settings are never hardcoded
- The user-editable settings are the defaults in the `PARAMS` table at the top
  of each backend (and the constants at the top of `tiff_workflow/wizard.py`). Code
  READS them -- never repeat a default as a literal. A literal equal to the
  default passes every test and silently discards the user's edit.
- When the wizard passes a setting to a backend, pass it explicitly,
  even when it equals the default: omitting it "because it is the default"
  lets a backend whose own setting was edited run with a different value
  than the wizard planned with.
- The wizard reads a backend default with `_backend_default(script, param)`,
  from the script's `PARAMS` table parsed with `ast` (`_backend_tables`: the
  table is written with literals only, and a backend is never imported by the
  wizard); its `_BACKEND_PARAM_FALLBACKS` is used only when the script cannot
  be read, and `tests/test_settings_literals.py` pins it to the scripts.
- **The settings overlay** `.tiff_workflow_settings.json` (env
  `TIFFWORKFLOW_SETTINGS`, blank = off; else next to `convert_tiff.py`; else the
  user profile) changes those settings without editing a file. It is read by
  the wizard (`_OVERLAY_SETTINGS`) and by every backend
  (`apply_settings_overlay`). Only the names in a script's `OVERLAY_SPECS`
  table are taken: **never add a safety or per-run parameter to one**
  (tests/test_settings_overlay.py T6 pins the forbidden list). A `--param` on
  the command line wins; a bad file or value changes nothing and warns. Tests
  and the battery switch it off.
- **Every backend parameter is in `REGISTRY` or in `NOT_OFFERED`** (with the
  reason) in the wizard; the argv comes from `_emit_all`, in REGISTRY
  order. `tests/test_wizard_registry.py` fails on a new parameter nobody
  decided about, and its golden file `tests/data/wizard_registry_golden.json`
  is never regenerated: it holds the PowerShell edition's command lines, and
  since v3.1 the wizard's arguments must be their translation by
  `tools/legacy_args.py`, option by option.

## Tool traps (verified, all silent)
- **exiftool `-tagsfromfile SRCFILE` expands printf codes** in SRCFILE: `%o`
  in `50%off.tif` is eaten, it warns and STILL exits 0. Doubling does not
  escape it; use a `%`-free container name (`backup_tiff_metadata`). One `-q`
  only (`-q -q` hides the warning); fail on `Error opening file` regardless of
  the exit code -- but NOT on `No writable tags set`.
- **`magick identify -format "%[compression]"` prints once per page**
  (`zipzip`); read `exiftool -IFD0:Compression` instead (`ifd0_compression`).
  `%[tiff:subfiletype]` is empty both when absent and when 0 -- use
  `exiftool -a -G1 -s -s -n -SubfileType` when the difference matters.
- **magick picks the output channel layout from the PIXELS** (round 28): an RGB file whose
  every pixel is R=G=B comes out as 1-channel gray (RGB+IR as 2 samples), with the RGB ICC
  still in it, and every pixel compare says identical. Every rewrite passes
  `magick_keep_type_args` (`-type TrueColor`/`TrueColorAlpha`) and is checked by
  `tiff_sample_layout_kept`; `-define colorspace:auto-grayscale=false` does NOT help.
- **`-tagsfromfile ... -all:all` does not copy `IPTC:CodedCharacterSet`** (round 28): into a
  file (or the MIE container) without it, UTF-8 IPTC text is written as Latin, Japanese as
  `?`. Add `-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet` after `-all:all`. `-IPTC:All` has
  the same hole (copy_exif's JPEG -> TIFF copy); there the destination can hold IPTC of its
  own, so its IPTC is copied onto itself first (`-tagsfromfile @ -IPTC:All`) and exiftool
  rewrites the whole block in the new charset -- without that a Latin caption is left
  mislabelled as UTF-8.
- **exiftool reads its command line in the ANSI code page -- an argfile PATH
  too** (round 27). Every call sends the argfile's CONTENT on stdin,
  `exiftool -@ -` (`tw.exiftool`, `_run_exiftool` in the wizard), with
  `-charset filename=utf8`. A test that reads tags must not put a Japanese path
  on exiftool's command line either (`exif_stdin` in
  `tests/test_exiftool_stdin.py`), and a fixture built by exiftool or magick is
  made under a plain name and renamed.
- **magick expands `%` in every output name and in an input with a `[n]`
  suffix** (`magick_path_spec`: `%%`); a plain input must stay unescaped.
- **magick globs `[ ]` in a spec that carries a `[n]` selector**, and a glob
  match wins over the literal file (`foo[1].tif[0]` reads `foo1.tif` when both
  exist; no escape works on Windows). The wizard routes such paths through a
  bracket-free hard-link alias (`_magick_spec`, released per file: across
  drives the alias is a full COPY). The backends do not: their compares would
  copy every scan of a library that lives in a `[FINAL]` folder on another
  drive. There the literal file wins unless a file matching the glob really
  exists, and then the gate compares the wrong file and refuses unless its
  pixels happen to be equal.
- **A wait on a lock cannot be interrupted on Windows**: `run_tasks` polls
  with a timeout so Ctrl+C is seen while files are in flight.
- **`shutil.which` returns `magick.EXE`**, and ImageMagick prints the name it
  was started under in every message (`find_tool` lower-cases the extension).
- Windows' 260-character path limit: staged names in mode 8 blew past it
  (round 13). Keep generated names short; `magick.exe` is not long-path aware.

## Shared semantics with jxl-photo
`jxl-photo` (sibling repo) is the **reference implementation** of the features
both toolkits have -- `--exclude-folders` (folder NAMES, matched as segments,
case-insensitive, only BELOW the scan root; round 14 fixed a matcher that
split the absolute path), export-marker matching, manifests. When a shared
feature's semantics are in doubt, read jxl-photo's version and ask before
inventing a new one.

## Verification
- **ALWAYS test with hostile paths** (owner's rule, 2026-10-17: "SEMPRE da erro").
  The owner's real library has every one of these, often in one path:
  `G:\2019\190815_Filmbbb_伊豆沼_夏 [FINAL]\NORITSU\0014_14 (2).tif` -- Japanese,
  `[ ]` brackets, spaces, `%`, parentheses, long paths, files on another drive
  than `%TEMP%`. Every change that touches a path (discovery, magick, exiftool,
  staging, moves, reports) gets a REAL test whose fixture path has Japanese
  characters AND `[ ]` AND a space -- not only an ASCII `tmp_path`. Round
  2026-10-17 shipped three such bugs (alias copies filling C:, every conversion
  refused under `伊豆沼`, Delete OLD_TIFFs refused).
On the owner's machine `py` is a Python 3.13 without pytest -- use `python` for
the suite and the battery.
- **A mini run before every long run** (owner's rule, 2026-11-12: "quando for rodar
  algo demorado, 10min ou mais, faz um mini teste rápido"). Anything of ~10 min or
  more (the literal suite, the battery, a side-by-side, a timing measurement, a chain
  in the background) is first run in miniature -- the SAME command, folders and
  redirections, one scenario or a few files, 1-2 min -- and the miniature must show
  three things: it runs (the right code, hostile paths), its result lands where it
  will be read (the log is written, the summary line the grep/monitor waits for is
  there) and that result answers the question. Say what the long run is for and how
  long it takes (from a real log, never a guess) before starting it. Long runs never
  in parallel: they make magick time out.
- **One suite: `python -m pytest tests`.** It drives the backends end to end
  as subprocesses with real TIFFs, once per LANE (`tests/backends.py`:
  `parallel` and `sequential` -- what the two PowerShell shells were).
  `tests/test_backend_invariants.py` runs the backends' code with the tools
  replaced (`FakeTools`): which gate runs before which irreversible step, what
  a failed gate leaves on disk, what is sent to the tools. Add a rule there by
  RUNNING the code; match the text of the source only for what cannot be run
  (the AST rules at its end).
- **`ruff` and `mypy` find nothing** (settings in `pyproject.toml`; mypy 1.x, the
  last that checks Python 3.9): `tests/test_lint.py` runs both, and is skipped
  in a Python that does not have them (`python -m pip install ruff "mypy<2"`).
  They are development tools: nothing imports them at run time.
- **Real-photo battery** -- `python tools/real_photo_battery.py
  --fixtures <folder>` (or `TW_FIXTURES`) runs the backends unattended on COPIES
  of real photos in both lanes and checks the files on disk. Run it before
  every release and after any change to what magick/exiftool read or write, to a
  delete or in-place path, or to discovery. Paste its summary into the report.
- **Prove every regression test FAILS against the pre-fix code** (mutation
  check): extract the old file and run the test against it, then compare the
  restored file's hash with the original bytes.
  ```
  git show HEAD:tiff_workflow/compress_tiff_zip.py > <tmpdir>/compress_tiff_zip.py
  ```
  Never `git stash` for this.
- **Changing a function's signature OR its return shape** (a new parameter, a
  result dict gaining or losing a key): first list every caller and every test
  double (`FakeTools`, the monkeypatches in the wizard's tests). jxl-photo's
  round 43 shipped a crash because one loop unpacked a result tuple at a fixed
  arity.
- **Run the tool twice and feed it its own output** when auditing or testing a
  destructive path: two of round 13's three serious findings only appear on a
  second run over files the toolkit wrote itself.
- **Real files beat reasoning.** Codec/metadata/multi-page bugs only show up on
  actual files (scanner RGB + preview + IR `MASK` pages, Capture One 2-page
  layouts, names with `%`). The owner keeps local real-photo fixtures: ask for
  the folder, never modify them, work on copies.
- Fixtures with `%` in the name are built under a safe name and renamed
  (`make_tiff_named`), or `magick` writes `500ff.tif`.
- **Every subprocess call in the wizard (`tiff_workflow/wizard.py`) goes through `_run_captured`**
  except the version probe (`_probe_version`) and the streaming
  `run_subprocess`; `tests/test_memory_subprocess.py` enforces it.
  `capture_output=True` starts reader threads that can hang `Thread.start()`
  under memory exhaustion, before the timeout is armed. The backends have the
  same rule (`tw.run_captured`).

## Editing source safely
- **Bash heredocs eat backslashes** even with a quoted delimiter
  (`python - <<'PY'`): `'[\\\\/]'` landed as `'[\/]'`, a path guard that
  silently stopped matching `\`. Use the Edit tool for exact edits. When a bulk
  edit needs Python, write the script to a file first (not a heredoc),
  `assert s.count(old) == N` before replacing, then verify the bytes on disk
  (`grep ... | cat -A`) against a sibling construct.
- **Stage files by name, never `git add -A`**: it once swept the owner's
  untracked bug report into a commit.
- **Code inserted right above a `def` can land between it and its decorator**
  (v3.1 phase 7, twice): `@_scoped_magick_aliases` then wraps the new code, the
  gate keeps every alias copy, and nothing fails until a test reaches it. Anchor
  such an insertion on the decorator line;
  `test_every_function_that_makes_aliases_releases_them` checks the gates.

## AI workflow: planner -> coder -> reviewer
Heavy changes run in three steps, each leaving a document in `AI_tools/`
(gitignored, local only): `YYMMDD_<Tool>_<plan|report|review>_<topic>.md`,
same `<topic>` across the three.

**Ask first who codes** (owner's rule, 2026-11-12). Before planning a heavy
change, ask the owner: will the coder be a cheap model (DeepSeek, Kimi, GLM,
...) or a strong one (Claude Opus or Fable) that plans and codes itself?
- **Cheap coder:** the three steps below, as written. The literal plan and the
  full review are what make its work safe.
- **Strong model doing both:** a one-page plan (what changes for the user, the
  decisions taken and the ones that are the owner's, the callers and test
  doubles when a signature or return shape changes, the tests to write, what
  can go wrong), the code, and a short closing report (the proofs with their
  REAL output, what is left for later, what was NOT verified). No line-by-line
  "current -> required" tables, and the independent review (step 3) runs when
  the owner asks for it, not by default.
- **Either way, unchanged:** one step per commit, every new test proved to fail
  against the pre-fix code, hostile paths, the suite and the battery as
  "Verification" says.

1. **Plan (Claude/Opus)** -- literal and complete, no design decision left to
   the coder: file + function, current -> required behaviour and why, every
   decision already taken, the caller list when a signature or return shape
   changes, the tests to write (real files where it matters) and the command
   that proves each one fails against the pre-fix code, and the rules above
   most often broken (the command-line contract, one copy of everything,
   fail-closed, no hardcoded settings, hostile paths).
2. **Code (DeepSeek or another coder)** -- follow the plan literally; when it
   is wrong or ambiguous take the most conservative option (fail closed,
   change less) and record it. Never commit. Report: each plan item done /
   partly / not done; every decision and deviation, with why; the commands run
   with their REAL output (the pytest summary, the pre-fix failure proof); what
   was NOT verified. Tell the user the report's path.
3. **Review (Claude/Opus)** -- read plan, report and the full diff; re-run the
   suite and the pre-fix proofs instead of trusting the report; must-fix /
   should-fix list and a commit verdict.

## Docs map
- `README.md` -- short by design, in jxl-photo's format (owner's rule, 2026-11-11:
  the README must not grow): the opening lines and the size table, the scripts, a
  short install, the wizard's quick start, one command per script, the mode table,
  a one-line-per-item "Good to know", the documentation table and a SHORT "What's
  new" for the current release ONLY (replaced at each release, never appended to;
  earlier releases are `docs/CHANGELOG.md`). Anything longer goes to a page below
  with a link.
- `docs/install.md` -- the Python install step by step (downloads, PATH, verify,
  troubleshooting).
- `docs/README_compress_tiff_zip.md`, `docs/README_convert_tiff_py.md`,
  `docs/README_copy_exif.md`, `docs/README_generate_thumbnails.md` -- per-script
  reference.
- `docs/command_line.md` -- the command line of v3.1 and every option of v3.0.0
  in its new form (`tests/test_legacy_args.py` checks the table against the
  `PARAMS` tables: a new option needs its row).
- `docs/powershell_edition.md` -- the PowerShell scripts of v2.9.1: where they
  are, what changes for someone moving to v3, and the problems found in them
  while porting. **Written once, with v3.0.0, and frozen** (owner's decision,
  2026-11-11): never spend time finding out whether a bug fixed later also
  existed in the `.ps1`, and never add to its list -- the page itself says to
  assume it did.
- `docs/bugs_fixed.md` -- one section per audit round, newest first (severity
  emoji, `**Issue:** / - **Fix:** / - **Files:**`). Its old version headings
  are not the real numbering: find the next round with
  `git log --grep audit-round`. Rounds 1-28 describe the PowerShell scripts;
  the function names in them map to Python ones (`Test-PixelIdentical` ->
  `pixel_identical`, ...).
- `docs/CHANGELOG.md` -- per release. `tests/README.md` -- what each suite pins.
- `RELEASE_vX.Y.md` (root, gitignored) -- notes pasted into the GitHub release
  with `gh release create --notes-file`. Their FIRST line points to the Windows
  program (owner's rule, 2026-11-08):
  `> **Windows program, no Python needed:** [tiff-workflow-exe](https://github.com/rsilvabr/tiff-workflow-exe/releases/latest) — the same scripts with ExifTool and ImageMagick included.`
  Every release has a TITLE, as jxl-photo's do (owner's rule, 2026-11-11):
  `--title "vX.Y.Z — What Changed, In Title Case"`, never the bare number. The
  exe's is `vX.Y.Z-exe1 — tiff-workflow vX.Y.Z`.

## Commits and releases
- Commits carry the repo owner's authorship only -- **no `Co-Authored-By`,
  session or generated-with trailers**; AI assistance is credited in the
  README's Acknowledgments.
- An audit round is one `fix(audit-round-N): ...` commit plus its
  `docs/bugs_fixed.md` section.
- The release number has one copy, `TOOLKIT_VERSION` in
  `tiff_workflow/__init__.py` (the Windows program's build reads it there;
  `tests/test_version_parity.py` fails on a second copy), and it must match
  the newest `docs/CHANGELOG.md` section (`tests/test_diagnose_preset.py`).
- Commit or push only when the owner asks.
- **Every tiff-workflow release updates tiff-workflow-exe too** (owner's rule,
  2026-10-25), in the sibling repo `../tiff-workflow-exe`: `TIFFWORKFLOW_TAG`
  in its `build.py` set to the new tag and `PACKAGE_REVISION` back to 1, its
  README "Current version" line and `docs/version_history.md`, then a tag
  `vX.Y.Z-exe1`: GitHub Actions builds the zip with a build attestation and
  creates a DRAFT release; `gh attestation verify` on the zip, then
  `python tests/battery_on_exe.py --app <that zip, extracted>` (runs the
  program from a hostile, internet-marked folder; must pass) before it is
  published as a regular release. A bundled tool update (ExifTool,
  ImageMagick: URL + SHA-256 in its `TOOLS`) goes the same way with the next
  `PACKAGE_REVISION`. Its `build.py` copies `convert_tiff.py` and every `.py` of
  `tiff_workflow/` (nothing to list) and reads the release from
  `tiff_workflow/__init__.py`; its `tiff-workflow.spec` analyses the package and
  leaves it OUT of the bundle, so the plain files in the program's `scripts/` are
  what runs (a setting edited there counts).

## Pending list
The owner keeps one list of everything still pending, across all repos, in
`../PENDENCIAS.md` (the folder above this repo, outside git, in Portuguese).
Read it when asked what is left to do. When work here finishes an item, leaves
something for later, or waits on a decision from the owner, update that file in
the same session: remove what is done, add what is new. It is private: never
copy its contents into tracked files, commits or release notes.
