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

## Active scripts
- `convert_tiff.py` -- the interactive wizard (Python). Builds and runs the
  PowerShell backends, owns the manifest, the AutoFind search and the
  multi-step workflows (it gates step 2 on step 1's exit code). Presets
  (menu 5, `--run-preset`, `--list-presets`) are named journal snapshots run
  by `_run_journal`, the same runner as Repeat last. A Diagnose journal
  (kind `diagnose`, settings in `diagnose`, no commands) is REPORT ONLY:
  `_run_diagnose_journal` must never reach `_compress_padded_files`.
- `compress_tiff_zip.ps1` -- TIFF -> ZIP/Deflate, modes 0-9, in-place backups
  in `OLD_TIFFs/`, thumbnails, `-ExcludeFolders`.
  compress and both copy_exif backends also take `-InputDirFile` (one folder per
  line); the wizard's `run_subprocess` switches to it when the command line would
  pass `_CMDLINE_SPILL_CHARS`.
- `copy_exif_to_TIFF_ps5.ps1` / `copy_exif_to_TIFF_ps7.ps1` -- EXIF from the
  Fuji S3/S5 Pro JPEG into the TIFF. Two copies of one behaviour: PS5
  (sequential) and PS7 (`-Parallel`).
- `generate_thumbnails.ps1` -- sRGB thumbnails, standalone or embedded.

## Hard constraints of the .ps1 files
- **Pure ASCII, no BOM.** PowerShell 5.1 reads a BOM-less file as cp1252, and
  `"` (U+201D) is a string delimiter there: one em-dash broke parsing of a
  whole script (v2.4). Write `--`, `->`, plain `-`. Check after every edit:
  `grep -P '[^\x00-\x7F]' *.ps1`. Python's `encoding="utf-8-sig"` silently
  adds a BOM.
- **Every script parses under BOTH PowerShell 5.1 and 7**, and the PS5
  (sequential) and PS7 (`-Parallel`) paths stay behaviourally identical --
  divergence between them is this project's most recurring bug class.
  `compress_tiff_zip.ps1` carries near-duplicate worker paths (the
  sequential `Process-TiffJob` and the `ForEach-Object -Parallel` blocks of
  the legacy and the mode paths): **a fix in one is a fix in ALL of them**,
  and in both copy_exif copies.
- **Functions are invisible inside `ForEach-Object -Parallel`.** They are
  re-injected with `${function:Name} = $using:NameFnDef`, captured once near
  the top. A new helper used in a parallel block needs a `...FnDef` capture
  AND an injection line in EVERY runspace.
- **Functions duplicated across the .ps1 files are pinned by
  `tests/test_ps_helper_parity.py` -- fix every copy.** The test extracts each
  duplicated function body and requires the copies to be identical (doc and
  line comments stripped); `_KNOWN_DIVERGENT` lists the annotated exceptions.
- **Never `return` at script scope.** It exits 0 and discards the
  `$errTotal -gt 0 -> exit 1` contract that `convert_tiff.py` relies on.
- **Gates that authorise destroying data fail CLOSED**: a timeout, a non-zero
  exit or unparseable output is a failure, never a pass (`Test-ZipIntegrity`,
  `Get-TiffPageCount`, `_compare_tiff_metadata`, `_process_single_padded`).
  Ask what a gate can actually OBSERVE: a decode check cannot see a missing
  page (round 12).

## Settings are never hardcoded
- The user-editable settings live at the top of each script. Code READS them
  -- never repeat a default as a literal. A literal equal to the default
  passes every test and silently discards the user's edit.
- When `convert_tiff.py` passes a setting to a backend, pass it explicitly,
  even when it equals the default: omitting it "because it is the default"
  lets a backend whose own setting was edited run with a different value
  than the wizard planned with.
- The wizard reads a backend default with `_backend_default(script, param)`;
  its `_BACKEND_PARAM_FALLBACKS` is used only when the `.ps1` cannot be read,
  and `tests/test_settings_literals.py` pins it to the scripts.

## Tool traps (verified, all silent)
- **exiftool `-tagsfromfile SRCFILE` expands printf codes** in SRCFILE: `%o`
  in `50%off.tif` is eaten, it warns and STILL exits 0. Doubling does not
  escape it; use a `%`-free container name (`Backup-TiffMetadata`). One `-q`
  only (`-q -q` hides the warning); fail on `Error opening file` regardless of
  the exit code -- but NOT on `No writable tags set`.
- **`magick identify -format "%[compression]"` prints once per page**
  (`zipzip`); read `exiftool -IFD0:Compression` instead. `%[tiff:subfiletype]`
  is empty both when absent and when 0 -- use `exiftool -a -G1 -s -s -n
  -SubfileType` when the difference matters.
- **PS5 cannot bind `-Switch:$false` through `-File`**: the switch arrives as a
  string and stays ON. Route PS5 through `_wrap_ps5_command` (tests:
  `run_ps_switched`).
- Windows' 260-character path limit: staged names in mode 8 blew past it
  (round 13). Keep generated names short.

## Shared semantics with jxl-photo
`jxl-photo` (sibling repo) is the **reference implementation** of the features
both toolkits have -- `-ExcludeFolders` (folder NAMES, matched as segments,
case-insensitive, only BELOW the scan root; round 14 fixed a matcher that
split the absolute path), export-marker matching, manifests. When a shared
feature's semantics are in doubt, read jxl-photo's version and ask before
inventing a new one.

## Verification
On the owner's machine `py` is a Python 3.13 without pytest -- use `python` for
the suite and the battery.
- **Two suites, different jobs.** `pytest tests` is the real one: it drives
  the PowerShell backends end to end through both `powershell` and `pwsh`
  with real TIFFs. Pester is static pattern-matching over the sources, and
  only runs under `pwsh` (Pester lives in the PS7 module path):
  `Invoke-Pester -Path (Get-ChildItem tests/test_*.ps1).FullName`
  (Pester 5+ auto-discovers only `*.Tests.ps1`).
- **Real-photo battery** -- `python tools/real_photo_battery.py
  --fixtures <folder>` (or `TW_FIXTURES`) runs the backends unattended on COPIES
  of real photos under both shells and checks the files on disk. Run it before
  every release and after any change to what magick/exiftool read or write, to a
  delete or in-place path, or to discovery. Paste its summary into the report.
- **Prove every regression test FAILS against the pre-fix code** (mutation
  check): extract the old file and run the test against it, then compare the
  restored file's hash with the original bytes.
  ```
  git show HEAD:compress_tiff_zip.ps1 > <tmpdir>/compress_tiff_zip.ps1
  ```
  Never `git stash` for this.
- **Pester assertions pin code SHAPES**: a refactor can break them with no
  bug present, and a test can also be the wrong side (round 15 found three
  failing against correct code). Decide which side is wrong before touching
  either; rewrite a moved invariant, never just delete it.
- **Changing a function's signature OR its return shape** (a new parameter, a
  tuple/array gaining a member, a hashtable losing a key): first list every
  caller, every splatted call and every test double -- in every worker path
  and both copy_exif copies. jxl-photo's round 43 shipped a crash because one
  loop unpacked a result tuple at a fixed arity.
- **Run the tool twice and feed it its own output** when auditing or testing a
  destructive path: two of round 13's three serious findings only appear on a
  second run over files the toolkit wrote itself.
- **Real files beat reasoning.** Codec/metadata/multi-page bugs only show up on
  actual files (scanner RGB + preview + IR `MASK` pages, Capture One 2-page
  layouts, names with `%`). The owner keeps local real-photo fixtures: ask for
  the folder, never modify them, work on copies.
- Fixtures with `%` in the name are built under a safe name and renamed
  (`make_tiff_named`), or `magick` writes `500ff.tif`.
- **Every subprocess call in `convert_tiff.py` goes through `_run_captured`**
  except the version probes (`detect_powershell_version`, `_probe_version`) and
  the streaming `run_subprocess`; `tests/test_memory_subprocess.py` enforces
  it. `capture_output=True` starts reader threads that can hang `Thread.start()`
  under memory exhaustion, before the timeout is armed.

## Editing source safely
- **Bash heredocs eat backslashes** even with a quoted delimiter
  (`python - <<'PY'`): `'[\\\\/]'` landed as `'[\/]'`, a path guard that
  silently stopped matching `\`. Use the Edit tool for exact edits. When a bulk
  edit needs Python, `assert s.count(old) == N` before replacing, then verify
  the bytes on disk (`grep ... | cat -A`) against a sibling construct.
- **Stage files by name, never `git add -A`**: it once swept the owner's
  untracked bug report into a commit.

## AI workflow: planner -> coder -> reviewer
Heavy changes run in three steps, each leaving a document in `AI_tools/`
(gitignored, local only): `YYMMDD_<Tool>_<plan|report|review>_<topic>.md`,
same `<topic>` across the three.

1. **Plan (Claude/Opus)** -- literal and complete, no design decision left to
   the coder: file + function, current -> required behaviour and why, every
   decision already taken, the caller list when a signature or return shape
   changes, the tests to write (real files where it matters) and the command
   that proves each one fails against the pre-fix code, and the rules above
   most often broken (ASCII, PS5/PS7 parity, every worker path, fail-closed,
   no hardcoded settings).
2. **Code (DeepSeek or another coder)** -- follow the plan literally; when it
   is wrong or ambiguous take the most conservative option (fail closed,
   change less) and record it. Never commit. Report: each plan item done /
   partly / not done; every decision and deviation, with why; the commands run
   with their REAL output (pytest and Pester summaries, the pre-fix failure
   proof); what was NOT verified. Tell the user the report's path.
3. **Review (Claude/Opus)** -- read plan, report and the full diff; re-run the
   suites and the pre-fix proofs instead of trusting the report; must-fix /
   should-fix list and a commit verdict.

## Docs map
- `README.md` -- overview, install, quick start, current release.
- `docs/README_compress_tiff_zip.md`, `docs/README_convert_tiff_py.md`,
  `docs/README_copy_exif.md`, `docs/README_generate_thumbnails.md` -- per-script
  reference.
- `docs/bugs_fixed.md` -- one section per audit round, newest first (severity
  emoji, `**Issue:** / - **Fix:** / - **Files:**`). Its old version headings
  are not the real numbering: find the next round with
  `git log --grep audit-round`.
- `docs/CHANGELOG.md` -- per release. `tests/README.md` -- what each suite pins.
- `RELEASE_vX.Y.md` (root, gitignored) -- notes pasted into the GitHub release
  with `gh release create --notes-file`.

## Commits and releases
- Commits carry the repo owner's authorship only -- **no `Co-Authored-By`,
  session or generated-with trailers**; AI assistance is credited in the
  README's Acknowledgments.
- An audit round is one `fix(audit-round-N): ...` commit plus its
  `docs/bugs_fixed.md` section.
- Commit or push only when the owner asks.
