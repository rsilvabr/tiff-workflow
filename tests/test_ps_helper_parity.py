"""audit round 18: duplicated PowerShell helpers are pinned to stay identical.

The four backends are self-contained copies of each other by design (no dot-
sourcing), and round 16 proved the cost: a timeout fix reached the
Test-PixelIdentical copy of compress_tiff_zip.ps1 only, while the two copy_exif
copies kept a flat 30 s -- documented as "Keep implementations identical" in
each. This module extracts every `function Name { ... }` body from the four
scripts, strips doc comments and line comments, and requires the copies to be
byte-identical -- `_KNOWN_DIVERGENT` lists the four exceptions that exist TODAY
(each annotated, none reviewed by round 18).

It also scans the -LoadFactor wiring statically: every Test-PixelIdentical /
Test-ZipIntegrity CALL inside a `ForEach-Object -Parallel` block must pass
-LoadFactor (the workers share the disk, so each gets ~1/N of the throughput the
timeout formula assumes), and no call outside such a block may.
"""

import difflib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SCRIPTS = [
    "compress_tiff_zip.ps1",
    "copy_exif_to_TIFF_ps5.ps1",
    "copy_exif_to_TIFF_ps7.ps1",
    "generate_thumbnails.ps1",
]

# Duplicated functions that are NOT identical today. The parity test requires
# every OTHER duplicated function to be identical, so this list is the complete
# inventory of known divergence -- a fix that reaches one copy must reach the
# others or move its entry out of here.
_KNOWN_DIVERGENT = {
    # copy_exif deletes the failed MIE container in both failure paths; the
    # compress copies leave it behind in %TEMP%.
    "Backup-TiffMetadata": "divergent at round 18, not reviewed",
    # ps7's copy is re-wrapped/re-indented against ps5's; never reviewed.
    "Invoke-S5ProFolder": "divergent at round 18, not reviewed",
    # copy_exif counts OK+SKIP-ZIP and MISS, result classes compress never emits.
    "Process-Results": "differs on purpose: copy_exif's result vocabulary",
    # compress routes non-console hosts to Write-Information (pytest/ISE), the
    # copy_exif copies always Write-Host.
    "Write-Log": "differs on purpose: compress writes Write-Information outside a console host",
}

_FUNC_RE = re.compile(r"^function\s+([A-Za-z0-9_-]+)\s*\{", re.MULTILINE)


def _extract_functions(text):
    """name -> body (brace-matched from the `function Name {` line)."""
    out = {}
    for m in _FUNC_RE.finditer(text):
        i = m.end() - 1
        depth = 0
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out[m.group(1)] = text[m.start():i + 1]
    return out


def _normalise(body):
    """Drop `<# ... #>` blocks and whole-line comments, rstrip the rest.

    The docstrings comments legitimately differ (each copy names its own call
    sites), so they must not count as divergence; the CODE must not."""
    body = re.sub(r"<#.*?#>", "", body, flags=re.DOTALL)
    lines = [ln.rstrip() for ln in body.splitlines()]
    lines = [ln for ln in lines if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def _duplicates():
    """name -> {script: normalised body} for every function defined in 2+ scripts."""
    dupes = {}
    for script in SCRIPTS:
        text = (REPO / script).read_text(encoding="utf-8", errors="replace")
        for name, body in _extract_functions(text).items():
            dupes.setdefault(name, {})[script] = _normalise(body)
    return {name: copies for name, copies in dupes.items() if len(copies) >= 2}


def test_duplicated_functions_are_identical():
    """Every function with copies in 2+ scripts must be byte-identical after
    comment stripping, except the annotated _KNOWN_DIVERGENT entries."""
    failures = []
    for name, copies in sorted(_duplicates().items()):
        if name in _KNOWN_DIVERGENT:
            continue
        ref_script = next(iter(copies))
        ref = copies[ref_script]
        for script, body in copies.items():
            if body == ref:
                continue
            diff = "\n".join(difflib.unified_diff(
                ref.splitlines(), body.splitlines(),
                fromfile=f"{name} ({ref_script})", tofile=f"{name} ({script})",
                lineterm=""))
            failures.append(diff)
    assert not failures, (
        "duplicated PowerShell functions diverged (fix every copy, or move the "
        "entry into _KNOWN_DIVERGENT with a reason):\n\n" + "\n\n".join(failures))


# ---------------------------------------------------------------------------
# The memory estimate's bytes-per-pixel classification (static)
# ---------------------------------------------------------------------------


def test_compress_compare_bpp_follows_in_place():
    """R18-2: the memory estimate must charge the compare cost (75 B/px) only when a
    worker actually compares pixels -- mode 8 or an in-place write. Modes 0/9 moved the
    source to OLD_TIFFs/ before enqueueing and only decode-check the output
    (Test-ZipIntegrity), so the old mode list over-estimated them 5x; an in-place
    mode-5 write (mode 5 without -OutputDir) did compare and was under-estimated.

    Static on purpose: with fixtures this small the Memory line formats as ~0.0 GB
    either way and cannot distinguish the classification."""
    text = (REPO / "compress_tiff_zip.ps1").read_text(encoding="utf-8", errors="replace")
    assert "$needsCompare = ($Mode -eq 8) -or" in text, (
        "the estimate must ask whether mode 8 or any in-place write runs a compare")
    assert "$Mode -in 0, 2, 8, 9" not in text, (
        "the old mode list charged modes 0/9 the compare cost and missed in-place "
        "mode-5 writes")


# ---------------------------------------------------------------------------
# The -LoadFactor wiring (static)
# ---------------------------------------------------------------------------

_PARALLEL_RE = re.compile(r"ForEach-Object\s+[^{}]*?-Parallel\s*\{")
_CALL_RE = re.compile(r"(?<![\w-])(Test-PixelIdentical|Test-ZipIntegrity)\s+-\w")


def _parallel_blocks(text):
    """(start, end) character offsets of every `ForEach-Object ... -Parallel {` body."""
    blocks = []
    for m in _PARALLEL_RE.finditer(text):
        start = m.end() - 1
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks.append((start, i + 1))
    return blocks


def _call_statements(text):
    """(line index, logical statement) for every call to the two gates.

    Backtick-continued lines are joined; whole-line comments are skipped (the
    injection lines `${function:Test-PixelIdentical} = ...` and comments must
    not count as calls)."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("#"):
            i += 1
            continue
        stmt = lines[i]
        j = i
        while stmt.rstrip().endswith("`") and j + 1 < len(lines):
            j += 1
            stmt += "\n" + lines[j]
        if _CALL_RE.search(stmt):
            out.append((i, stmt))
        i = j + 1
    return out


def test_load_factor_passed_only_inside_parallel_blocks():
    """T11: every Test-PixelIdentical / Test-ZipIntegrity call inside a
    -Parallel block passes -LoadFactor; no call outside one does."""
    problems = []
    for script in SCRIPTS:
        text = (REPO / script).read_text(encoding="utf-8", errors="replace")
        spans = [(text.count("\n", 0, s), text.count("\n", 0, e))
                 for s, e in _parallel_blocks(text)]

        def inside(lineno):
            return any(a <= lineno <= b for a, b in spans)

        for lineno, stmt in _call_statements(text):
            has = "-LoadFactor" in stmt
            if inside(lineno) and not has:
                problems.append(
                    f"{script}:{lineno + 1}: call inside -Parallel without "
                    f"-LoadFactor: {stmt.splitlines()[0].strip()}")
            if not inside(lineno) and has:
                problems.append(
                    f"{script}:{lineno + 1}: call outside -Parallel with "
                    f"-LoadFactor: {stmt.splitlines()[0].strip()}")
    assert not problems, "\n".join(problems)
