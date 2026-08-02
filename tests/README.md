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
- The `[no thumb]` fallback must NOT return early (v2.5)
- Error handling

> These assert code *shapes*, so a refactor invalidates them even when the invariant survives.
> When one fails, check whether the invariant moved before "fixing" the script: in v2.5 all 17
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