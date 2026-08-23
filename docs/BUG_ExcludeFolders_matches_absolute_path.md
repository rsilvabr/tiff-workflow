# BUG (open): `-ExcludeFolders` matches the ABSOLUTE path, not the tree below the scan root

**Status:** FIXED in audit round 14 (see `docs/bugs_fixed.md`, section "v2.3 - Audit Round 14").
Matching now goes through `Test-PathExcluded` in all three backends and is evaluated relative
to the scan root, following `jxl-photo`'s `_is_tool_output_path` exactly. Pinned by
`TestExcludeFoldersIsRelativeToTheScanRoot` and `TestCopyExifExcludeFolders` in
`tests/test_regression_classes.py`; 6/6 targeted mutations caught.
**Found:** 2026-08-06, while porting this feature to the sibling project `jxl-photo`
**Severity:** medium. Not data loss, but it can silently turn a whole run into a no-op.
**Affects:** `compress_tiff_zip.ps1`, `copy_exif_to_TIFF_ps7.ps1`, `copy_exif_to_TIFF_ps5.ps1`

---

## One paragraph

`-ExcludeFolders` splits each file's **absolute** directory path and tests every
segment — including the drive letter and every ancestor **above** the folder the
user pointed at. So an entry that happens to match any ancestor (`Temp`,
`Users`, a project folder name, the scan root itself) excludes the entire run,
and the log reports it as a normal exclusion. The intent, and what the README
documents, is "skip whole folder **trees** by name during discovery" — i.e.
folders nested **below** the input root. The Python mirror in this same repo
already does it that way.

---

## Expected vs actual

Input root `E:\TESTE`, files at `E:\TESTE\raw_scan\a.tif` and
`E:\TESTE\_EXPORT\TIFF16\b.tif`:

| `-ExcludeFolders` | expected | actual |
|---|---|---|
| `_EXPORT` | skip `b.tif` | skip `b.tif` ✅ |
| `raw_scan` | skip `a.tif` | skip `a.tif` ✅ |
| `TESTE` | nothing to skip (it IS the root you pointed at) | **skips everything** ❌ |
| `Temp` | nothing to skip (not part of the project at all) | **skips everything** ❌ |

The two correct rows are the normal case, which is why this has not been
noticed: you would not normally name an ancestor. It bites when a folder name
you legitimately want to exclude *also* appears somewhere above the root —
`zip`, `logs`, `old`, `temp`, `export`, a year, a client name.

---

## Where

The same block is duplicated in three files:

| file | lines |
|---|---|
| `compress_tiff_zip.ps1` | ~1737-1743 |
| `copy_exif_to_TIFF_ps7.ps1` | ~394-400 |
| `copy_exif_to_TIFF_ps5.ps1` | ~373-379 |

```powershell
$segHit = $false
foreach ($seg in ($df.DirectoryName -split '[\\/]')) {
    if ($seg -in $script:excludeNames) { $segHit = $true; break }
}
if ($segHit) { $excludedCount++; continue }
```

## Why it happens

`FileInfo.DirectoryName` is **always absolute**, even when `Get-ChildItem` was
given a relative path. Splitting it therefore yields every ancestor segment.

Verified:

```powershell
Set-Location "$env:TEMP\exclchk\TESTE"
Get-ChildItem -LiteralPath "raw_scan" -File -Recurse |
    ForEach-Object { ($_.DirectoryName -split '[\\/]') -join ' | ' }
```

```
C: | Users | yhang | AppData | Local | Temp | exclchk | TESTE | raw_scan
```

Nine segments, of which only the last one is below the folder that was passed in.

## Reproduction

```powershell
$t = Join-Path $env:TEMP "exclchk"
Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$t\TESTE\raw_scan"      | Out-Null
New-Item -ItemType Directory -Force "$t\TESTE\_EXPORT\TIFF16" | Out-Null
"x" | Set-Content "$t\TESTE\raw_scan\a.tif"
"x" | Set-Content "$t\TESTE\_EXPORT\TIFF16\b.tif"

$files = Get-ChildItem -LiteralPath "$t\TESTE" -File -Recurse
foreach ($name in @("_EXPORT", "raw_scan", "TESTE", "Temp")) {
    $excludeNames = @($name)
    $kept = @()
    foreach ($df in $files) {
        $segHit = $false
        foreach ($seg in ($df.DirectoryName -split '[\\/]')) {
            if ($seg -in $excludeNames) { $segHit = $true; break }
        }
        if (-not $segHit) { $kept += $df.Name }
    }
    "{0,-10} -> kept: {1}" -f $name, $(if ($kept) { $kept -join ', ' } else { '(NOTHING)' })
}
```

Output:

```
_EXPORT    -> kept: a.tif
raw_scan   -> kept: b.tif
TESTE      -> kept: (NOTHING)
Temp       -> kept: (NOTHING)
```

## Evidence that "relative" is the intended semantic

1. **The Python mirror in this repo already does it relative.**
   `convert_tiff.py:2551`, in the manifest generator:

   ```python
   if any(part.lower() in _excluded_parts for part in p.relative_to(root).parts):
       continue
   ```

   Same idea, different rule. So the two halves of this toolkit disagree today.

2. **The README describes trees, not paths.** `README.md:127` — *"skips whole
   folder trees by name during discovery"*; `docs/README_compress_tiff_zip.md:70`
   — *"`_EXPORT` skips any `...\_EXPORT\...` tree"*. Both describe something
   nested below the root.

3. **The parameter validation already refuses paths.** Entries containing `\` or
   `/` are rejected with "takes folder NAMES, not paths". The feature is
   deliberately about names inside the tree — but the matcher then reads names
   from outside it.

4. **The sibling project made this decision explicitly.** `jxl-photo`'s
   `_is_tool_output_path` (`jxl_jpeg_transcoder.py`) carries the reasoning in a
   docstring: *"Evaluated RELATIVE to the scan root, so pointing the tool
   directly AT a folder named 'converted' still works — only files NESTED inside
   such folders below the root are skipped."*

## Proposed fix

Match only the segments **below** the input root. Both scripts already know that
root at the point of the test (`$inputRoot` in `compress_tiff_zip.ps1`'s
discovery loop — it is even attached to each file as the `InputRoot`
note-property; `$RootPath` in `copy_exif`'s `Invoke-S5ProFolder`).

Extract ONE helper instead of patching three copies — the three have to stay
identical, and nothing enforces that today:

```powershell
function Test-ExcludedByFolderName {
    <#
      True when any directory segment BELOW $Root matches one of $Names.
      Absolute segments above $Root are deliberately NOT tested: pointing the
      tool at a folder is an explicit request and must win over the exclusion
      list, and an ancestor name (Temp, Users, a project folder) would
      otherwise silently exclude the entire run.
      Case-insensitive, whole-segment: '_EXPORT' never matches 'My_EXPORT_photos'.
    #>
    param(
        [Parameter(Mandatory)][string]$DirectoryName,
        [Parameter(Mandatory)][string]$Root,
        [string[]]$Names
    )
    if (-not $Names -or $Names.Count -eq 0) { return $false }

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $dirFull  = [System.IO.Path]::GetFullPath($DirectoryName)

    # Not under the root at all (should not happen): fall back to no exclusion
    # rather than testing absolute segments.
    if (-not $dirFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $rel = $dirFull.Substring($rootFull.Length).Trim('\', '/')
    if (-not $rel) { return $false }   # file sits directly in the root

    foreach ($seg in ($rel -split '[\\/]')) {
        if ($seg -and ($seg -in $Names)) { return $true }
    }
    return $false
}
```

Call sites become:

```powershell
if (Test-ExcludedByFolderName -DirectoryName $df.DirectoryName `
                              -Root $inputRoot -Names $script:excludeNames) {
    $excludedCount++
    continue
}
```

Keep everything else as it is: the `;` separator, the path-vs-name validation
with `exit 1`, the case-insensitive whole-segment match, and the
`Excluded N file(s) under: ...` log line.

### One decision to make

With this fix you can no longer exclude **the folder you pointed at**
(`-InputDir E:\TESTE\_EXPORT -ExcludeFolders _EXPORT` becomes a no-op instead of
excluding everything). That is the recommended behaviour — an explicit
`-InputDir` should beat the exclusion list, which is the rule the sibling
project documents. If you disagree, the alternative is to keep testing the root
segment itself but never its ancestors; say so explicitly in the docs either
way, because right now neither is written down.

Note that `-InputDir` accepts a `;`-separated list of roots, and
`compress_tiff_zip.ps1` already attaches the matching `InputRoot` to every
discovered file — so each file must be tested against **its own** root, not
against the first one.

## Tests to add

`tests/test_regression_classes.py` already has
`test_exclusion_is_segment_match_not_substring`, which is the right shape to
copy:

1. **Ancestor name does not exclude.** Build `<tmp>/Outer/PhotoSet/img.tif`, run
   with `-InputDir <tmp>/Outer/PhotoSet -ExcludeFolders "Outer"`, assert the file
   WAS processed. Fails today.
2. **Root name does not exclude.** Same tree, `-ExcludeFolders "PhotoSet"`,
   assert processed. Fails today.
3. **Nested name still excludes.** The existing `_EXPORT` test — must keep
   passing (no over-fix).
4. **Substring still does not match.** The existing `My_EXPORT_photos` test.
5. **Per-root with a `;` list.** Two roots, a name that is nested under one and
   an ancestor of the other; assert only the nested one is excluded.
6. **`copy_exif` backends behave the same** — the `ps5`/`ps7` copies were added
   later (see `docs/bugs_fixed.md:284`) and are the ones most likely to drift.

`tests/test_compress_tiff_zip.ps1:40` currently asserts the source contains
`'$seg -in $script:excludeNames'` — that string assertion will need updating to
match the extracted helper.

---

## Context: why this came up

While auditing `jxl-photo` (the JXL toolkit) the same folder-exclusion feature
was requested there. Porting it meant reading this implementation closely, which
surfaced the absolute-vs-relative difference. `jxl-photo` will implement the
relative rule, matching its existing `_is_tool_output_path` / `_skip_decoder_output`
helpers. Once this is fixed, the two toolkits will accept the same
`;`-separated list and behave identically — which matters if the same list gets
reused between them.
