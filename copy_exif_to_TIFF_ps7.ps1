# -- CLI PARAMETERS ------------------------------------------------------
param(
    [string]$InputDir = "",
    [ValidateRange(1, 64)][int]$Workers = 16,
    [switch]$DryRun,
    [switch]$SkipIfTiffHasExif,
    [switch]$SkipLzwAsCompressed,
    [bool]$SafeMode = $true,
    [string]$IccPolicy = "never",
    [switch]$CompressZip,
    [string]$OutputDir = "",
    [string]$StagingDir = "",
    [switch]$Overwrite,
    [switch]$AutoFind,
    [string]$FolderPattern = "S5pro",
    [int]$MagickTimeout = 30,
    [switch]$FailOnWarn,
    [string]$ExcludeFolders = ""     # ';'-separated folder NAMES to skip during discovery
)
# ------------------------------------------------------------------


# -- Excluded folder names ------------------------------------------
# ';'-separated folder NAMES (not paths): any TIFF with a folder of that name BELOW the scan
# root (case-insensitive) is skipped during discovery. Segment match only, so "_EXPORT" never
# touches "My_EXPORT_photos", and matching is relative to the root, so folders ABOVE it --
# including the root's own name -- never exclude anything (see Test-PathExcluded).
#
# NOTE: this backend discovers TIFFs NON-recursively (Invoke-S5ProFolder -IsRecurse $false),
# so in practice every discovered TIFF sits directly in a root and there is nothing below it
# to exclude. The flag is accepted and applied for consistency -- the wizard asks the question
# once and passes it to every step of a workflow -- but it only really bites in
# compress_tiff_zip.ps1's recursive modes.
$script:excludeNames = @()
foreach ($entry in ($ExcludeFolders -split ';')) {
    $entry = $entry.Trim()
    if (-not $entry) { continue }
    if ($entry -match '[\\/]') {
        Write-Host "ERROR: -ExcludeFolders takes folder NAMES, not paths: '$entry'" -ForegroundColor Red
        Write-Host "       Use a ';'-separated list of bare names, e.g. -ExcludeFolders '_EXPORT;temp'" -ForegroundColor Yellow
        exit 1
    }
    $script:excludeNames += $entry
}

# -- Logging -------------------------------------------------------
$scriptName = "Copy-S5Pro-Exif"
$logDir     = Join-Path $PWD.Path "Logs\$scriptName"
[System.IO.Directory]::CreateDirectory($logDir) | Out-Null
$logFile    = Join-Path $logDir "$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

function Write-Log {
    param([string]$msg, [string]$level = "INFO")
    $line = "$(Get-Date -Format 'HH:mm:ss') | $level | $msg"
    Write-Host $line
    [System.IO.File]::AppendAllText($logFile, $line + [System.Environment]::NewLine)
}

# -- Cleanup on interrupt -----------------------------------------
$script:cleanupDirs  = @()
# Run-scoped prefix: a bare GUID pattern matched EVERY run's staged files, so Ctrl-C in one
# session destroyed the in-flight output of any other session sharing -StagingDir.
# Same fix as compress_tiff_zip.ps1.
# 8 hex chars, not 32: the prefix plus a per-file id is prepended to every staged file name
# and Windows still refuses paths over 260 characters. Same change as compress_tiff_zip.ps1.
$script:runStagingId = [guid]::NewGuid().ToString('N').Substring(0, 8)
if (-not [string]::IsNullOrWhiteSpace($StagingDir)) { $script:cleanupDirs += $StagingDir }

trap {
    # Every terminating error landed here, not just Ctrl+C: a real failure was logged as
    # "Interrupted!" and `break` swallowed it, so callers read the run as success. Only a
    # pipeline stop counts as an interrupt; anything else exits 1.
    $isInterrupt = $_.Exception -is [System.Management.Automation.PipelineStoppedException]
    if ($logFile) {
        if ($isInterrupt) { Write-Log "Interrupted! Cleaning up staging files..." "WARN" }
        else { Write-Log "FATAL (unhandled error) | $($_.Exception.Message) | cleaning up staging files..." "ERROR" }
    } else {
        Write-Host "Interrupted! Cleaning up..." -ForegroundColor Yellow
    }
    foreach ($dir in $script:cleanupDirs) {
        if (Test-Path -LiteralPath $dir) {
            # Only remove staging files created by this run
            Get-ChildItem -LiteralPath $dir -Force | Where-Object { $_.Name -like "$($script:runStagingId)_*" } | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
        }
    }
    if ($isInterrupt) { break } else { exit 1 }
}

$script:counterTotal = 0
$script:total        = 0
$script:okTotal      = 0
$script:skipTotal    = 0
$script:missTotal    = 0
$script:errTotal     = 0
$script:warnTotal    = 0
$script:multiTotal    = 0
$script:multiPagePaths = [System.Collections.Concurrent.ConcurrentBag[string]]::new()
# Destinations already claimed in this run (only used with -OutputDir, where every
# session writes into the same folder). Shared across folders on purpose.
$script:claimedDest   = @{}

function Process-Line {
    param([string]$line)
    $script:counterTotal++
    $lvl = "INFO"
    if     ($line -match '^OK\+SKIP-ZIP') { $script:skipTotal++ }
    elseif ($line -match '^OK')           { $script:okTotal++ }
    elseif ($line -match '^SKIP')         { $script:skipTotal++ }
    elseif ($line -match '^MISS')  { $script:missTotal++; $lvl = "WARN" }
    elseif ($line -match '^ERROR')  { $script:errTotal++;  $lvl = "ERROR" }
    elseif ($line -match '^WARN')  { $script:warnTotal++; $lvl = "WARN" }
    elseif ($line -match '^MULTI')  { $script:multiTotal++; $lvl = "WARN" }
    Write-Log "[$($script:counterTotal)/$($script:total)] $line" $lvl
}

function Invoke-MagickWithTimeout {
    <#
    .SYNOPSIS
        Runs `magick` with the given arguments under a timeout and returns
        @{ TimedOut = [bool]; ExitCode = [int]; Output = [string[]] }.

    .NOTES
        Uses an in-process runspace instead of Start-Job: Start-Job spawns a whole
        PowerShell process per call (~700 ms), which dominated runtime because
        SafeMode calls this once per file.

        ExitCode -1 means "could not determine" -- every caller must treat it as failure.

        Duplicated across compress_tiff_zip.ps1 and copy_exif_to_TIFF_ps*.ps1 so each
        script stays self-contained. Keep the implementations identical.
        Re-inject into ForEach-Object -Parallel runspaces with:
            ${function:Invoke-MagickWithTimeout} = $using:MagickTimeoutFnDef
    #>
    param(
        [string[]]$Arguments = @(),
        [int]$TimeoutSec = 30
    )

    if ($TimeoutSec -le 0) { $TimeoutSec = 30 }

    $ps = [System.Management.Automation.PowerShell]::Create()
    [void]$ps.AddScript({
        param([string[]]$a)
        $out = & magick @a 2>$null
        [pscustomobject]@{ Output = @($out); ExitCode = $LASTEXITCODE }
    }).AddArgument($Arguments)

    try {
        $handle = $ps.BeginInvoke()
    } catch {
        try { $ps.Dispose() } catch { }
        return @{ TimedOut = $false; ExitCode = -1; Output = @() }
    }

    if (-not $handle.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($TimeoutSec))) {
        # BeginStop (async): Stop() blocks until the native child exits, which defeats the timeout.
        try { [void]$ps.BeginStop($null, $null) } catch { }
        return @{ TimedOut = $true; ExitCode = -1; Output = @() }
    }

    try {
        $res = $ps.EndInvoke($handle)
        $payload = @($res | Where-Object { $null -ne $_ })
        if ($payload.Count -eq 0) { return @{ TimedOut = $false; ExitCode = -1; Output = @() } }
        $last = $payload[-1]
        return @{ TimedOut = $false; ExitCode = [int]$last.ExitCode; Output = @($last.Output) }
    } catch {
        return @{ TimedOut = $false; ExitCode = -1; Output = @() }
    } finally {
        try { $ps.Dispose() } catch { }
    }
}

function ConvertTo-MagickPathSpec {
    <#
    .SYNOPSIS
        Escapes '%' for the filename SPECS ImageMagick expands, so a path containing one
        addresses the file the caller meant.

    .DESCRIPTION
        ImageMagick reads printf-style scene specifiers in a filename spec: `50%off.tif`
        writes `500ff.tif` ('%o'), `100%dpi.tif` writes `1000pi.tif`, and on a multi-page
        image it writes ONE FILE PER PAGE. magick still exits 0, so the caller sees a success
        while the output sits under a name nobody asked for. '%%' is the escape and
        round-trips to a literal '%'.

        WHERE THIS APPLIES, exactly:
          - EVERY output path.
          - An INPUT that carries a '[n]' page suffix -- appending the suffix turns the
            argument into a filename spec.
        And where it must NOT be used:
          - A PLAIN input path. There ImageMagick opens the name literally, so an escaped
            name is simply not found.

    .NOTES
        Re-inject into -Parallel runspaces with:
            ${function:ConvertTo-MagickPathSpec} = $using:MagickPathFnDef
        Duplicated across compress_tiff_zip.ps1 and copy_exif_to_TIFF_ps*.ps1 so each
        script stays self-contained. Keep the implementations identical.
    #>
    param([string]$Path)

    if ([string]::IsNullOrEmpty($Path)) { return $Path }
    return $Path.Replace('%', '%%')
}

function Get-TiffPageCount {
    <#
    .SYNOPSIS
        Returns @{ Ok = [bool]; PageCount = [int]; Error = [string] } for a TIFF.
        Error is "timeout", "failed" or "parse:<raw>" -- never silently 0.

    .NOTES
        Re-inject into -Parallel runspaces with:
            ${function:Get-TiffPageCount} = $using:PageCountFnDef
        (Invoke-MagickWithTimeout must be injected too.)
    #>
    param([string]$Path, [int]$TimeoutSec = 30)

    $r = Invoke-MagickWithTimeout -Arguments @("identify", "-format", "%n\n", $Path) -TimeoutSec $TimeoutSec
    if ($r.TimedOut) { return @{ Ok = $false; PageCount = 0; Error = "timeout" } }

    $lines = @($r.Output | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") })
    if ($lines.Count -eq 0) { return @{ Ok = $false; PageCount = 0; Error = "failed" } }

    $val = "$($lines[0])".Trim()
    $n = 0
    if (-not [int]::TryParse($val, [ref]$n)) { return @{ Ok = $false; PageCount = 0; Error = "parse:$val" } }
    return @{ Ok = $true; PageCount = $n; Error = "" }
}

function Test-PixelIdentical {
    <#
    .SYNOPSIS
        Returns $true only when two TIFFs hold pixel-identical data: same page count and
        per-page RMSE == 0. A decode check (Test-ZipIntegrity) cannot see a truncated page,
        a wrong bit depth or a colorspace shift -- all of those still decode -- so this is
        the gate that authorises destroying the source (mode 8, in-place replace).

        `%[distortion]` goes to stdout, which the timeout wrapper captures (the RMSE metric
        itself prints to stderr, which the wrapper drops). `magick compare A B` on
        multi-page files pairs pages across the two lists and returns nonsense, so pages
        are compared one by one. Anything unreadable fails CLOSED.

    .NOTES
        This function is duplicated across compress_tiff_zip.ps1 and the copy_exif_to_TIFF_ps*.ps1
        scripts. Keep implementations identical. If you change one, change all three.

        Re-inject into -Parallel runspaces with:
            ${function:Test-PixelIdentical} = $using:PixelCompareFnDef
        (Invoke-MagickWithTimeout, Get-TiffPageCount and ConvertTo-MagickPathSpec must be
        injected too.)
    #>
    param([string]$SrcPath, [string]$DstPath, [int]$TimeoutSec = 30)

    $ps = Get-TiffPageCount -Path $SrcPath -TimeoutSec $TimeoutSec
    $pd = Get-TiffPageCount -Path $DstPath -TimeoutSec $TimeoutSec
    if (-not $ps.Ok -or -not $pd.Ok) { return $false }
    if ($ps.PageCount -ne $pd.PageCount) { return $false }

    # Appending '[i]' turns these into filename SPECS, so a '%' in the path has to be
    # escaped or magick opens a different file -- and this gate authorises overwriting the
    # original. See ConvertTo-MagickPathSpec.
    $srcSpec = ConvertTo-MagickPathSpec $SrcPath
    $dstSpec = ConvertTo-MagickPathSpec $DstPath

    for ($i = 0; $i -lt $ps.PageCount; $i++) {
        $r = Invoke-MagickWithTimeout -Arguments @("compare", "-metric", "RMSE", "$srcSpec[$i]", "$dstSpec[$i]", "-format", "%[distortion]`n", "info:") -TimeoutSec $TimeoutSec
        # compare exits 1 when images differ -- that is data, not an error. Only a
        # timeout or an undetermined/error exit code fails here directly; the parsed
        # distortion value below has the final word.
        if ($r.TimedOut -or $r.ExitCode -lt 0 -or $r.ExitCode -gt 1) { return $false }
        $lines = @($r.Output | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") })
        if ($lines.Count -eq 0) { return $false }
        $val = 0.0
        if (-not [double]::TryParse("$($lines[0])".Trim(), [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$val)) { return $false }
        if ($val -ne 0) { return $false }
    }
    return $true
}

function Backup-TiffMetadata {
    <#
    .SYNOPSIS
        Parks the source metadata in a temp MIE container when $SrcPath cannot serve as the
        `-tagsfromfile` source directly. Returns @{ Needed = [bool]; Path = $null-or-mie-path }.

    .DESCRIPTION
        TWO conditions need the container, for different reasons:

        1. $WriteDst overwrites $SrcPath. In-place compression destroys the original before
           the EXIF restore step runs, so `-tagsfromfile <source>` silently read the
           already-compressed file and every tag was lost.

        2. $SrcPath contains a '%'. exiftool expands printf-style FILENAME FORMAT CODES in the
           `-tagsfromfile` argument -- `50%off.tif` is read as `50ff.tif` ('%o' consumed),
           `100%dpi.tif` as `100pi.tif`. exiftool then prints "Warning: Error opening file"
           and STILL EXITS 0, so every caller's `$LASTEXITCODE -ne 0` check passed and the
           run reported OK while the output carried no metadata at all. In mode 8 that is
           permanent: the source is deleted right after.

           Doubling the '%' does NOT escape it (verified on exiftool 13.59):
             50%%off.jpg  -> 50%ff.jpg      50%%%off.jpg -> 50%%ff.jpg
           Each extra '%' is emitted literally and the trailing '%o' is consumed anyway.
           The only fix is to hand exiftool a name that has no '%' in it -- which is exactly
           what this container is. The SOURCE FILE OPERAND is not format-expanded, only the
           `-tagsfromfile` argument is, so dumping FROM the '%' name works.

        Callers must pass the returned path as the tagsfromfile source and delete it when done.

        FAIL-CLOSED: when a container was needed but could not be written, Path stays $null --
        the caller must REFUSE the write, because going ahead would strip every metadata tag.

    .NOTES
        Re-inject into -Parallel runspaces with:
            ${function:Backup-TiffMetadata} = $using:MetaBackupFnDef
        Duplicated across compress_tiff_zip.ps1 and copy_exif_to_TIFF_ps*.ps1 so each script
        stays self-contained. Keep the implementations identical.
    #>
    param([string]$SrcPath, [string]$WriteDst)

    if ($WriteDst -ne $SrcPath -and -not "$SrcPath".Contains('%')) {
        return @{ Needed = $false; Path = $null }
    }

    $metaBase = [System.IO.Path]::GetTempFileName()
    Remove-Item -LiteralPath $metaBase -Force -ErrorAction SilentlyContinue
    $metaBackup = $metaBase -replace '\.tmp$', '.mie'

    # `-o` is format-expanded exactly like `-tagsfromfile`, so a '%' anywhere in %TEMP% would
    # send the container somewhere else and leave nothing to read back. Fail closed instead.
    if ("$metaBackup".Contains('%')) { return @{ Needed = $true; Path = $null } }

    $argMeta = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($argMeta, "-charset`nfilename=utf8`n-q`n-q`n-all:all`n-o`n$metaBackup`n$SrcPath`n")
        exiftool -@ $argMeta | Out-Null
        if ($LASTEXITCODE -ne 0) { return @{ Needed = $true; Path = $null } }
    } catch {
        return @{ Needed = $true; Path = $null }
    } finally {
        Remove-Item $argMeta -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path -LiteralPath $metaBackup) { return @{ Needed = $true; Path = $metaBackup } }
    return @{ Needed = $true; Path = $null }
}

function Test-TiffHasOnlySubfilePages {
    <#
    .SYNOPSIS
        Checks whether all pages beyond IFD[0] are non-independent subfile pages
        (thumbnails/previews). An extra page counts as a thumbnail when its subfiletype
        is in $AllowedSubfileTypes OR when it is strictly smaller than the main image.
        A full-size extra page with an empty/missing/unlisted tag is treated as a real
        page (fail-closed).

    .PARAMETER Path
        Path to the TIFF file.

    .PARAMETER PageCount
        Total number of pages/IFDs in the TIFF.

    .PARAMETER AllowedSubfileTypes
        List of symbolic subfiletype values considered safe for extra pages.
        Default: @("REDUCEDIMAGE", "REDUCED", "MASK", "PAGE")

    .NOTES
        This function is duplicated across compress_tiff_zip.ps1 and the copy_exif_to_TIFF_ps*.ps1 scripts.
        Keep implementations identical. If you change one, change all three.

        Inside ForEach-Object -Parallel runspaces, functions defined in the parent script are not
        visible. Re-inject the function at the top of each parallel block with:
            ${function:Test-TiffHasOnlySubfilePages} = $using:TestSubfileFnDef
        (Invoke-MagickWithTimeout must be injected too.)
    #>
    param(
        [string]$Path,
        [int]$PageCount,
        [string[]]$AllowedSubfileTypes = @("REDUCEDIMAGE", "REDUCED", "MASK", "PAGE"),
        [int]$TimeoutSec = 30
    )

    if ($PageCount -le 1) { return $true }

    $r = Invoke-MagickWithTimeout -Arguments @("identify", "-format", "%[tiff:subfiletype]|%w|%h`n", $Path) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return $false }

    $lines = @($r.Output | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") })
    # Fewer lines than pages means we could not classify every extra page -> fail closed
    if ($lines.Count -lt $PageCount) { return $false }

    # A page is a thumbnail when its tag says so -- or when it is strictly smaller than
    # the main image. The tag alone is unreliable: ImageMagick rewrites multi-page files
    # and stamps PAGE on every page, so a Capture One file whose thumbnail lost its
    # REDUCEDIMAGE marker (e.g. after an earlier magick rewrite) was skipped as
    # genuinely multi-page. A full-size extra page (scanner IR, Photoshop layer, real
    # second photo) is never a thumbnail regardless of this rule.
    $mainW = 0; $mainH = 0
    $mainParts = "$($lines[0])".Trim() -split '\|'
    if ($mainParts.Count -ge 3) {
        [void][int]::TryParse($mainParts[1], [ref]$mainW)
        [void][int]::TryParse($mainParts[2], [ref]$mainH)
    }

    for ($i = 1; $i -lt $PageCount; $i++) {
        $parts = "$($lines[$i])".Trim() -split '\|'
        $st = "$($parts[0])".Trim()
        if ($st -in $AllowedSubfileTypes) { continue }
        $w = 0; $h = 0
        if ($parts.Count -ge 3) {
            [void][int]::TryParse($parts[1], [ref]$w)
            [void][int]::TryParse($parts[2], [ref]$h)
        }
        $isReducedSize = ($mainW -gt 0 -and $mainH -gt 0 -and $w -gt 0 -and $h -gt 0 -and $w -lt $mainW -and $h -lt $mainH)
        if (-not $isReducedSize) { return $false }
    }
    return $true
}

function Restore-TiffSubfileTypes {
    <#
    .SYNOPSIS
        Re-applies the source per-page NewSubfileType markers to a freshly magick-written TIFF.
        ImageMagick rewrites every page on `-compress zip` but does not preserve the tag: a
        REDUCEDIMAGE thumbnail or MASK (scanner IR) page comes back as PAGE/untagged, so
        viewers and SafeMode heuristics no longer recognise those pages.
        Returns $true when nothing needed restoring or every marker was written.

    .NOTES
        This function is duplicated across compress_tiff_zip.ps1 and the copy_exif_to_TIFF_ps*.ps1 scripts.
        Keep implementations identical. If you change one, change all three.

        Inside ForEach-Object -Parallel runspaces, re-inject with:
            ${function:Restore-TiffSubfileTypes} = $using:RestoreSubfileFnDef
        (Invoke-MagickWithTimeout must be injected too.)
    #>
    param(
        [string]$SrcPath,
        [string]$DstPath,
        [int]$TimeoutSec = 30
    )

    # Single-page sources need no restore: magick only stamps PAGE when it writes a
    # multi-page file, and an absent NewSubfileType means 0 per the TIFF spec anyway.
    $pcSrc = Get-TiffPageCount -Path $SrcPath -TimeoutSec $TimeoutSec
    if (-not $pcSrc.Ok) { return $false }
    if ($pcSrc.PageCount -le 1) { return $true }

    # Markers are read with exiftool, not magick. `%[tiff:subfiletype]` prints an EMPTY
    # string both when the tag is ABSENT and when it is 0 ("full-resolution image"), so a
    # source carrying an explicit 0 -- every scanner TIFF does -- had the tag DELETED from
    # the output instead of preserved. exiftool prints one line per IFD that really has the
    # tag, with its numeric value, which tells the two cases apart.
    $argRead = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($argRead, "-charset`nfilename=utf8`n-a`n-G1`n-s`n-s`n-n`n-SubfileType`n$SrcPath`n")
        $readOut = exiftool -@ $argRead 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
    } finally {
        Remove-Item $argRead -Force -ErrorAction SilentlyContinue
    }

    $srcTypes = @{}
    foreach ($line in @($readOut)) {
        if ("$line" -match '^\[IFD(\d+)\]\s+SubfileType:\s*(\d+)\s*$') {
            $srcTypes[[int]$Matches[1]] = [int]$Matches[2]
        }
    }

    $tags = @()
    for ($i = 0; $i -lt $pcSrc.PageCount; $i++) {
        if ($srcTypes.ContainsKey($i)) {
            $tags += "-IFD${i}:SubfileType#=$($srcTypes[$i])"
        } else {
            # Source had no marker on this page: clear whatever magick stamped on it.
            $tags += "-IFD${i}:SubfileType="
        }
    }
    if ($tags.Count -eq 0) { return $true }

    $argFile = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($argFile, "-charset`nfilename=utf8`n-q`n-q`n-overwrite_original`n$($tags -join "`n")`n$DstPath`n")
        $out = exiftool -@ $argFile 2>&1
        return ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item $argFile -Force -ErrorAction SilentlyContinue
    }
}

# Capture function definitions once so they can be re-injected into -Parallel runspaces
$script:MagickTimeoutFnDef = ${function:Invoke-MagickWithTimeout}.ToString()
$script:MagickPathFnDef    = ${function:ConvertTo-MagickPathSpec}.ToString()
$script:PixelCompareFnDef  = ${function:Test-PixelIdentical}.ToString()
$script:PageCountFnDef     = ${function:Get-TiffPageCount}.ToString()
$script:TestSubfileFnDef   = ${function:Test-TiffHasOnlySubfilePages}.ToString()
$script:RestoreSubfileFnDef = ${function:Restore-TiffSubfileTypes}.ToString()
$script:MetaBackupFnDef    = ${function:Backup-TiffMetadata}.ToString()

function Test-PathExcluded {
    <#
    .SYNOPSIS
        True when any folder segment BELOW $Root matches one of $Names (case-insensitive).

    .DESCRIPTION
        Matching is relative to the scan root, never absolute. `FileInfo.DirectoryName` is
        always absolute -- even when Get-ChildItem was handed a relative path -- so splitting
        it yielded every ancestor as well: the drive letter, every folder above the one the
        user pointed at, and the root's own name. An entry that happened to match one of
        those (`Temp`, `Users`, a year, a client name, the root itself) excluded the ENTIRE
        run, logged it as a normal exclusion and exited 0.

        Relative matching is what the docs promise ("skip whole folder TREES by name during
        discovery"), what the parameter validation implies (paths are refused, names only),
        and what the Python mirror in convert_tiff.py already did. Pointing the tool AT a
        folder named `_EXPORT` therefore still works -- only folders nested below the root
        are considered.

    .NOTES
        Used from script scope only (discovery), so no runspace re-injection is needed.
        Duplicated across compress_tiff_zip.ps1 and copy_exif_to_TIFF_ps*.ps1 so each script
        stays self-contained. Keep the implementations identical.
    #>
    param([string]$Directory, [string]$Root, [string[]]$Names)

    if (-not $Names -or $Names.Count -eq 0) { return $false }

    $dirN  = "$Directory".Replace('/', '\').TrimEnd('\')
    $rootN = "$Root".Replace('/', '\').TrimEnd('\')

    if ($dirN -ieq $rootN) {
        $rel = ""
    } elseif ($dirN.StartsWith("$rootN\", [System.StringComparison]::OrdinalIgnoreCase)) {
        $rel = $dirN.Substring($rootN.Length + 1)
    } else {
        # Not under the root we were given -- should not happen, discovery always enumerates
        # from the root. Fall back to the whole path, matching jxl-photo's
        # _is_tool_output_path (`except ValueError: rel = p`): in doubt, skip the file rather
        # than process something from an unexpected place.
        $rel = $dirN
    }

    if (-not $rel) { return $false }
    foreach ($seg in ($rel -split '\\')) {
        if ($seg -and ($seg -in $Names)) { return $true }
    }
    return $false
}

function Invoke-S5ProFolder {
    param([string]$RootPath, [bool]$IsRecurse)

    $allFiles  = Get-ChildItem -LiteralPath $RootPath -File -Recurse:$IsRecurse
    # JPEG index is always built recursively so JPEG/JPG subfolders can be found by Find-JpegPair
    $jpgFiles  = Get-ChildItem -LiteralPath $RootPath -File -Recurse | Where-Object { $_.Extension -match '^\.(jpg|jpeg)$' }
    $tiffFiles = @($allFiles | Where-Object { $_.Extension -match '^\.(tif|tiff)$' })

    if ($script:excludeNames.Count -gt 0) {
        $beforeExclude = $tiffFiles.Count
        # Relative to $RootPath: see Test-PathExcluded for why an absolute match excluded
        # entire runs.
        $tiffFiles = @($tiffFiles | Where-Object {
            -not (Test-PathExcluded -Directory $_.DirectoryName -Root $RootPath -Names $script:excludeNames)
        })
        $excluded = $beforeExclude - $tiffFiles.Count
        if ($excluded -gt 0) {
            Write-Log "Excluded $excluded file(s) under: $($script:excludeNames -join '; ')"
        }
    }

    if ($tiffFiles.Count -eq 0) { Write-Log "No TIFFs found in: $RootPath" "WARN"; return }

    $script:total += $tiffFiles.Count
    Write-Log "TIFFs: $($tiffFiles.Count) | JPEGs: $($jpgFiles.Count)"

    # Build JPEG index
    $jpgIndex = @{}
    foreach ($j in $jpgFiles) {
        $key = ($j.DirectoryName.ToLowerInvariant() + "|" + $j.BaseName.ToLowerInvariant())
        if (-not $jpgIndex.ContainsKey($key)) { $jpgIndex[$key] = $j.FullName }
        elseif ($j.Extension.ToLowerInvariant() -eq ".jpg") { $jpgIndex[$key] = $j.FullName }
    }

    function Find-JpegPair {
        param([System.IO.FileInfo]$tif)
        $dir    = $tif.DirectoryName
        $base   = $tif.BaseName
        $parent = Split-Path $dir -Parent
        $candidates = @($base)
        $stripped = ($base -replace '(_\d{3,4})$', '')
        if ($stripped -ne $base -and $stripped.Length -gt 0) { $candidates += $stripped }
        $searchDirs = @($dir, (Join-Path $dir "JPEG"), (Join-Path $dir "JPG"),
                        $parent, (Join-Path $parent "JPEG"), (Join-Path $parent "JPG")) |
                      Where-Object { $_ -and (Test-Path -LiteralPath $_) }
        foreach ($b in $candidates) {
            foreach ($d in $searchDirs) {
                $key = ($d.ToLowerInvariant() + "|" + $b.ToLowerInvariant())
                if ($jpgIndex.ContainsKey($key)) { return @{ Path = $jpgIndex[$key]; UsedBase = $b } }
                # The index only covers $RootPath, so the parent folder (and its JPEG/JPG
                # subfolders) could never produce a hit -- half of $searchDirs was dead code.
                # Probe the filesystem directly for those.
                foreach ($ext in @(".jpg", ".jpeg")) {
                    $probe = Join-Path $d "$b$ext"
                    if (Test-Path -LiteralPath $probe -PathType Leaf) {
                        return @{ Path = (Get-Item -LiteralPath $probe).FullName; UsedBase = $b }
                    }
                }
            }
        }
        return $null
    }

    $groups = $tiffFiles | Group-Object { $_.DirectoryName }

    foreach ($group in $groups) {
        $groupDir   = $group.Name
        $groupFiles = $group.Group

        if ($groups.Count -gt 1 -or $AutoFind) {
            Write-Log ""; Write-Log "-- Group: $groupDir ($($groupFiles.Count) file(s))"
        }

        $finalDir = if ($OutputDir) { $OutputDir } else { $groupDir }
        $writeDir = if ($StagingDir -and -not $DryRun) { $StagingDir } else { $finalDir }

        # With no -StagingDir the staged ZIP is written INTO the source folder, but only
        # -StagingDir was ever registered for cleanup -- so an interrupted run left
        # `<runId>_<12 hex>_photo.tif` sitting next to the photos, and the next run discovered
        # it as an ordinary source. Register whatever folder this run actually stages into; the
        # trap only removes names carrying THIS run's prefix.
        if (-not $DryRun -and $script:cleanupDirs -notcontains $writeDir) { $script:cleanupDirs += $writeDir }

        if ($CompressZip -and $StagingDir -and -not $DryRun) { [System.IO.Directory]::CreateDirectory($StagingDir) | Out-Null }
        if ($CompressZip -and $OutputDir)                    { [System.IO.Directory]::CreateDirectory($OutputDir)  | Out-Null }

        # Resolve TIFF->JPEG pairs
        # DestName: with -OutputDir every session writes into the SAME folder, so two
        # sessions holding DSC_0001.tif used to silently overwrite/skip each other.
        # Later claimants get _v2, _v3, ... (same policy as compress_tiff_zip.ps1).
        $destNameMap = @{}
        $pairs = @(foreach ($tif in $groupFiles) {
            $pair = Find-JpegPair $tif
            $destName = $tif.Name
            if ($OutputDir -and ($finalDir -ine $tif.DirectoryName)) {
                $dKey = (Join-Path $finalDir $destName).ToLowerInvariant()
                if ($script:claimedDest.ContainsKey($dKey) -and $script:claimedDest[$dKey] -ne $tif.FullName) {
                    $dStem = $tif.BaseName
                    $dExt  = $tif.Extension
                    $n = 2
                    do {
                        $candidate = "${dStem}_v${n}${dExt}"
                        $candKey = (Join-Path $finalDir $candidate).ToLowerInvariant()
                        $n++
                    } while ($script:claimedDest.ContainsKey($candKey) -or (Test-Path -LiteralPath (Join-Path $finalDir $candidate)))
                    $destName = $candidate
                    $dKey = $candKey
                    Write-Log "  RENAME (name already taken in OutputDir) | $($tif.Name) -> $destName" "WARN"
                }
                $script:claimedDest[$dKey] = $tif.FullName
            }
            $destNameMap[$tif.FullName] = $destName
            [PSCustomObject]@{
                Tiff     = $tif.FullName
                TifName  = $tif.Name
                TifBase  = $tif.BaseName
                DestName = $destName
                Jpeg     = if ($pair) { $pair.Path }    else { $null }
                UsedBase = if ($pair) { $pair.UsedBase } else { $null }
            }
        })

        if ($pairs.Count -eq 0) {
            $script:skipTotal += $groupFiles.Count
            $script:counterTotal += $groupFiles.Count
            continue
        }

        # Synchronous parallel -- more reliable than -AsJob when run from terminal
        $writeDirCapture  = $writeDir
        $finalDirCapture  = $finalDir
        $skipExifCapture  = $SkipIfTiffHasExif
        $dryCapture       = $DryRun
        $compressCapture  = $CompressZip
        $overCapture      = $Overwrite
        $skipLzwCapture   = $SkipLzwAsCompressed
        $safeModeCapture  = $SafeMode
        $iccPolicyCapture = $IccPolicy
        $magickTimeoutCapture = $MagickTimeout
        $runStagingIdCapture  = $script:runStagingId

        $results = $pairs | ForEach-Object -ThrottleLimit $Workers -Parallel {
            $p         = $_
            # Functions from the parent scope are not visible inside -Parallel runspaces
            ${function:Invoke-MagickWithTimeout}      = $using:MagickTimeoutFnDef
            ${function:ConvertTo-MagickPathSpec}     = $using:MagickPathFnDef
            ${function:Get-TiffPageCount}            = $using:PageCountFnDef
            ${function:Test-PixelIdentical}          = $using:PixelCompareFnDef
            ${function:Test-TiffHasOnlySubfilePages} = $using:TestSubfileFnDef
            ${function:Restore-TiffSubfileTypes}     = $using:RestoreSubfileFnDef
            ${function:Backup-TiffMetadata}          = $using:MetaBackupFnDef
            $skipExifL = $using:skipExifCapture
            $dryL      = $using:dryCapture
            $compressL = $using:compressCapture
            $writeDirL = $using:writeDirCapture
            $finalDirL = $using:finalDirCapture
            $overL     = $using:overCapture
            $skipLzwL  = $using:skipLzwCapture
            $safeModeL = $using:safeModeCapture
            $iccPolicyL = $using:iccPolicyCapture
            $magickTimeoutL = $using:magickTimeoutCapture
            $copiedTiffPath = $null

            if (-not $p.Jpeg) {
                return @{ Result = "MISS | $($p.TifName) | no matching JPEG (base: $($p.TifBase))"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
            }

            # The _\d{3,4}$ strip in Find-JpegPair can match an unrelated JPEG
            # (foto_2024.tif -> foto.jpg): EXIF copied from the wrong file must be loud.
            $heurMatch = ($p.UsedBase -and $p.UsedBase -cne $p.TifBase)
            $okPrefix = if ($heurMatch) { "WARN (heuristic JPEG match)" } else { "OK" }
            $heurSuffix = if ($heurMatch) { " | TIFF base '$($p.TifBase)' matched JPEG base '$($p.UsedBase)'" } else { "" }

            if ($skipExifL) {
                # No `| Select-Object -First 1` on the native call: -First stops the upstream
                # pipeline, so $LASTEXITCODE could still hold an earlier command's value --
                # and the check right below is the fail-closed gate that decides whether this
                # TIFF may be overwritten. Read it all, check the exit code, then take [0].
                $exifProbe = exiftool -q -q -G1 -s -EXIF:all $p.Tiff 2>$null
                $exifProbeExit = $LASTEXITCODE
                $firstExif = @($exifProbe | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") })[0]
                # Fail-closed: an unreadable TIFF must not be overwritten by the copy below
                if ($exifProbeExit -ne 0) { return @{ Result = "ERROR (exiftool EXIF check) | $($p.TifName) | cannot inspect TIFF, not overwriting"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false } }
                if ($firstExif) { return @{ Result = "SKIP (already has EXIF) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false } }
            }

            if ($dryL) {
                $zipInfo = if ($compressL) { " + ZIP" } else { "" }
                return @{ Result = "DRY (EXIF$zipInfo) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
            }

            # Hoisted out of the SafeMode block: the ZIP pixel gate below needs it even when
            # -SafeMode:$false skipped the page-count check.
            $magickTimeoutSec = if ($magickTimeoutL -gt 0) { $magickTimeoutL } else { 30 }

            if ($safeModeL) {
                $pc = Get-TiffPageCount -Path $p.Tiff -TimeoutSec $magickTimeoutSec
                if (-not $pc.Ok) {
                    $reason = switch -Wildcard ($pc.Error) {
                        "timeout" { "ERROR (magick timeout) | $($p.TifName) | possibly corrupted" }
                        "parse:*" { "ERROR (magick page count parse) | $($p.TifName) | unexpected output: $($pc.Error.Substring(6))" }
                        default   { "ERROR (magick page count failed) | $($p.TifName) | possibly corrupted" }
                    }
                    return @{ Result = $reason; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
                }
                if ($pc.PageCount -gt 1) {
                    # -AllowedSubfileTypes is passed explicitly, exactly like the four call
                    # sites in compress_tiff_zip.ps1. Falling back to the parameter default
                    # (which also allows MASK and PAGE) made SafeMode accept scanner RGB+IR
                    # files that the compression backend skips -- the opposite of what the
                    # README promises for "scanner IR files".
                    if (-not (Test-TiffHasOnlySubfilePages -Path $p.Tiff -PageCount $pc.PageCount -AllowedSubfileTypes @("REDUCEDIMAGE", "REDUCED") -TimeoutSec $magickTimeoutSec)) {
                        return @{ Result = "MULTI ($($pc.PageCount) IFDs -- skipped) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; MultiPagePath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
                    }
                }
            }

            # Check if TIFF already has ICC (fixed logic - don't reset $LASTEXITCODE)
            $tiffHasIcc = $false
            if ($iccPolicyL -eq "preserve_tiff" -or $iccPolicyL -eq "always") {
                $iccCheck = exiftool -s -s -s -ICC_Profile:all $p.Tiff 2>$null
                if (-not [string]::IsNullOrWhiteSpace($iccCheck)) { $tiffHasIcc = $true }
            }
            $copyIcc = ($iccPolicyL -eq "always") -or ($iccPolicyL -eq "preserve_tiff" -and -not $tiffHasIcc)
            $iccTag = if ($copyIcc) { "-ICC_Profile" } else { "" }

            # Determine target TIFF path: if OutputDir is specified (different from source dir), copy first to preserve original
            $tiffTarget = $p.Tiff
            $tiffCopied = $false
            $copiedTiffPath = $null
            $srcDir = [System.IO.Path]::GetFullPath((Split-Path $p.Tiff -Parent)).TrimEnd('\', '/')
            if ($finalDirL -and ($finalDirL -ine $srcDir) -and -not $dryL) {
                $destTiff = Join-Path $finalDirL $p.DestName
                if (-not (Test-Path -LiteralPath $destTiff) -or $overL) {
                    if (-not (Test-Path -LiteralPath $finalDirL)) {
                        [System.IO.Directory]::CreateDirectory($finalDirL) | Out-Null
                    }
                    # $copiedTiffPath is set BEFORE the copy, not after: assigning it only on
                    # success meant a Copy-Item that threw mid-write returned CopiedTiffPath
                    # = $null, the post-loop cleanup found nothing to remove, and the partial
                    # file stayed behind -- a later run without -Overwrite then skipped it as
                    # "exists in OutputDir" and the truncated TIFF persisted. The PS5 script
                    # already guarded this inline; the fix was never ported here.
                    $copiedTiffPath = $destTiff
                    try {
                        Copy-Item -LiteralPath $p.Tiff -Destination $destTiff -Force
                        $tiffTarget = $destTiff
                        $tiffCopied = $true
                    } catch {
                        return @{ Result = "ERROR (copy to OutputDir failed) | $($p.TifName): $($_.Exception.Message)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $true }
                    }
                } else {
                    return @{ Result = "SKIP (exists in OutputDir) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
                }
            }

            # exiftool expands printf-style filename format codes in the -tagsfromfile
            # ARGUMENT, so a JPEG named `50%off.jpg` is read as `50ff.jpg` -- a file that does
            # not exist. exiftool warns and STILL EXITS 0, so the TIFF came out with no EXIF at
            # all and the run reported OK. Park the tags in a container with a safe name first
            # (see Backup-TiffMetadata); a name without '%' is passed through untouched.
            $jpegMeta = Backup-TiffMetadata -SrcPath $p.Jpeg -WriteDst $tiffTarget
            if ($jpegMeta.Needed -and -not $jpegMeta.Path) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (metadata container failed - EXIF copy refused) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }
            $jpegTagSource = if ($jpegMeta.Path) { $jpegMeta.Path } else { $p.Jpeg }

            $tagsArgs = @("-tagsfromfile", $jpegTagSource, "-EXIF:All", "-XMP:All", "-IPTC:All")
            if ($iccTag) { $tagsArgs += $iccTag }
            $tagsArgs += "-unsafe", $tiffTarget

            # ONE -q, not two: the second suppresses WARNINGS, and a warning is the only signal
            # exiftool gives when the tagsfromfile source could not be opened -- it still exits 0.
            $exifOut = "$(exiftool -q -overwrite_original -P @tagsArgs 2>&1)"
            $exifExit = $LASTEXITCODE
            if ($jpegMeta.Path) { Remove-Item -LiteralPath $jpegMeta.Path -Force -ErrorAction SilentlyContinue }
            # Only "Error opening file" is fatal: it means the SOURCE was never read. A JPEG that
            # legitimately carries nothing writable reports differently and stays non-fatal.
            if ($exifExit -ne 0 -or $exifOut -match 'Error opening file') {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (exiftool EXIF) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            if (-not $compressL) {
                $copyNote = if ($tiffCopied) { " -> $finalDirL" } else { "" }
                return @{ Result = "$okPrefix | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$copyNote$heurSuffix"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
            }

            # -IFD0:Compression, and collapsed to the first line below: exiftool can print one
            # line per IFD and `-match` on an ARRAY is truthy when ANY element matches, so an
            # uncompressed main image whose thumbnail page happened to be compressed was
            # reported "already ZIP" and never compressed. compress_tiff_zip.ps1 fixed this in
            # four places; these two backends were the copies left behind.
            $comp = exiftool -s -s -s -IFD0:Compression $tiffTarget 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $comp) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (exiftool check) | $($p.TifName) | cannot detect compression"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }
            $comp = "$(@($comp)[0])".Trim()
            if ($comp -match $(if ($skipLzwL) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
                return @{ Result = "$(if ($heurMatch) { "WARN (heuristic JPEG match, already $comp -- ZIP skipped)" } else { "OK+SKIP-ZIP ($comp)" }) | $($p.TifName)$heurSuffix"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
            }

            $runIdL = $using:runStagingIdCapture
            # Run-scoped prefix so the interrupt trap only cleans up this run's staged files
            $stagingName = "${runIdL}_$([guid]::NewGuid().ToString('N').Substring(0, 12))_$($p.DestName)"
            $writeDst = Join-Path $writeDirL $stagingName
            $finalDst = Join-Path $finalDirL $p.DestName

            if ((Test-Path -LiteralPath $finalDst) -and -not $overL -and ($finalDst -ne $p.Tiff) -and -not $tiffCopied) {
                # No cleanup here: the guard above already requires -not $tiffCopied
                return @{ Result = "OK+SKIP-ZIP (exists) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            # Input literal, OUTPUT escaped: a '%' in the name sends the output somewhere
            # else while magick still exits 0 (see ConvertTo-MagickPathSpec).
            $magickErr = magick -quiet $tiffTarget -compress zip (ConvertTo-MagickPathSpec $writeDst) 2>&1
            $zipFailed = ($LASTEXITCODE -ne 0)
            if (-not $zipFailed -and -not (Test-Path -LiteralPath $writeDst)) {
                # Exit 0 with no file: the staged ZIP never landed, so there is nothing to move
                # and nothing to verify. Report it here instead of failing later on a file that
                # is not there.
                $zipFailed = $true
                $magickErr = "magick reported success but wrote no file (expected: $writeDst)"
            }
            if ($zipFailed) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (magick ZIP) | $($p.TifName) | $magickErr"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            # The staged ZIP is moved ON TOP of the TIFF it came from (see the move loop
            # below) and there is no OLD_TIFFs backup on this path, so it must be proven
            # pixel-identical first. A zero exit from magick is not proof: a page-short or
            # truncated file still exits 0 and still decodes. Same fail-closed gate that
            # compress_tiff_zip.ps1 requires before a mode 8 delete.
            if (-not (Test-PixelIdentical -SrcPath $tiffTarget -DstPath $writeDst -TimeoutSec $magickTimeoutSec)) {
                Remove-Item -LiteralPath $writeDst -Force -ErrorAction SilentlyContinue
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (ZIP pixel verification failed - original untouched) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            # Same '%' trap as the JPEG copy above: a TIFF whose NAME carries one is read as a
            # different file, exiftool exits 0, and the staged ZIP that replaces it carries no
            # metadata at all. See Backup-TiffMetadata.
            $tiffMeta = Backup-TiffMetadata -SrcPath $tiffTarget -WriteDst $writeDst
            $tiffTagSource = if ($tiffMeta.Path) { $tiffMeta.Path } else { $tiffTarget }
            $restoreOut = ""
            $restoreExit = 1
            if (-not ($tiffMeta.Needed -and -not $tiffMeta.Path)) {
                # ONE -q: the second hides the unreadable-source warning (exiftool exits 0 anyway).
                $restoreOut = "$(exiftool -q -overwrite_original -tagsfromfile $tiffTagSource -all:all -unsafe $writeDst 2>&1)"
                $restoreExit = $LASTEXITCODE
                if ($restoreOut -match 'Error opening file') { $restoreExit = 1 }
            }
            if ($tiffMeta.Path) { Remove-Item -LiteralPath $tiffMeta.Path -Force -ErrorAction SilentlyContinue }
            if ($restoreExit -ne 0) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "WARN (exiftool metadata copy failed, ZIP ok) | $($p.TifName)"; StagingName = $stagingName; OriginalName = $p.TifName; FinalDst = $finalDst; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            if (-not (Restore-TiffSubfileTypes -SrcPath $tiffTarget -DstPath $writeDst -TimeoutSec $magickTimeoutSec)) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "WARN (subfiletype restore failed, ZIP ok) | $($p.TifName)"; StagingName = $stagingName; OriginalName = $p.TifName; FinalDst = $finalDst; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
            return @{ Result = "$(if ($heurMatch) { 'WARN (heuristic JPEG match)' } else { 'OK+ZIP' }) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$heurSuffix"; StagingName = $stagingName; OriginalName = $p.TifName; FinalDst = $finalDst; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
        }

        # Process results and build staging map
        $script:groupStagingMap = @{}
        foreach ($r in $results) {
            if ($r.StagingName) { $script:groupStagingMap[$r.SrcPath] = $r.StagingName }
            if ($r.MultiPagePath) { $script:multiPagePaths.Add($r.MultiPagePath) | Out-Null }
            if ($r.CopiedTiffPath -and $r.IsIntermediate -and (Test-Path -LiteralPath $r.CopiedTiffPath)) {
                Remove-Item -LiteralPath $r.CopiedTiffPath -Force -ErrorAction SilentlyContinue
            }
            Process-Line $r.Result
        }

        # Move from staging to final destination (with integrity check and UUID mapping)
        if ($CompressZip -and -not $DryRun) {
            $moved = 0
            foreach ($tif in $groupFiles) {
                # No fallback to <writeDir>\<tif.Name>: this run stages under a run-scoped
                # prefix, so an unprefixed file of that name belongs to somebody else. Moving
                # it would drop a stranger's file on top of the user's TIFF.
                if (-not $script:groupStagingMap.ContainsKey($tif.FullName)) { continue }
                $stagingName = $script:groupStagingMap[$tif.FullName]
                $stagePath = Join-Path $writeDir $stagingName
                $destName = if ($destNameMap.ContainsKey($tif.FullName)) { $destNameMap[$tif.FullName] } else { $tif.Name }
                $destPath  = Join-Path $finalDir   $destName
                if ((Test-Path -LiteralPath $stagePath) -and $stagePath -ne $destPath) {
                    try {
                        $stageSize = (Get-Item -LiteralPath $stagePath).Length
                        Move-Item -Force -LiteralPath $stagePath -Destination $destPath -ErrorAction Stop
                        if ((Test-Path -LiteralPath $destPath) -and ((Get-Item -LiteralPath $destPath).Length -eq $stageSize)) {
                            # The staged ZIP is a new file with "now" as mtime; the plain
                            # EXIF-only path preserves the source date via exiftool -P, so
                            # the ZIP path does the same for consistency.
                            try { (Get-Item -LiteralPath $destPath).LastWriteTime = (Get-Item -LiteralPath $tif.FullName).LastWriteTime } catch { }
                            $moved++
                        } else {
                            $script:errTotal++
                            Write-Log "ERROR (move failed) | $($tif.Name)" "ERROR"
                        }
                    } catch {
                        $script:errTotal++
                        Write-Log "ERROR (move failed) | $($tif.Name): $($_.Exception.Message)" "ERROR"
                    }
                }
            }
            if ($moved -gt 0) { Write-Log "  -> Moved $moved file(s) -> $finalDir" }
        }
    }
}

# -- Entry point -----------------------------------------------------
if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir.TrimEnd('\', '/'))
}

# A bad path used to fall through to "No TIFFs found" and exit 0, so the wizard could not tell
# a typo from an empty folder -- and convert_tiff.py gates Step 2 of workflow 4 on this exit
# code, so a Copy EXIF that touched nothing read as success. Same contract as the other backends.
$inputDirs    = @()
$missingRoots = @()
if ($InputDir) {
    foreach ($dir in ($InputDir -split ';')) {
        $dir = $dir.Trim()
        if ([string]::IsNullOrWhiteSpace($dir)) { continue }
        if (-not [System.IO.Path]::IsPathRooted($dir)) { $dir = Join-Path $PWD.Path $dir }
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) { $missingRoots += $dir; continue }
        $inputDirs += $dir
    }
} else {
    $inputDirs = @($PWD.Path)
}

Write-Log "Log: $logFile"

foreach ($m in $missingRoots) { Write-Log "ERROR: input directory not found: $m" "ERROR" }
$script:errTotal += $missingRoots.Count

if ($inputDirs.Count -eq 0) {
    Write-Log "No valid input directories specified." "ERROR"
    Write-Log "Log: $logFile"
    exit 1
}

Write-Log "Workers: $Workers | CompressZip: $CompressZip | SkipIfTiffHasExif: $SkipIfTiffHasExif | OutputDir: $(if ($OutputDir) { $OutputDir } else { '(overwrite in place)' }) | Staging: $(if ($StagingDir) { $StagingDir } else { 'disabled' }) | DryRun: $DryRun"

foreach ($root in $inputDirs) {
    if ($AutoFind) {
        Write-Log "AutoFind mode | Pattern: '$FolderPattern' | Root: $root"
        $matchingFolders = Get-ChildItem -LiteralPath $root -Directory -Recurse |
                           Where-Object { $_.Name -like "*$FolderPattern*" -and $_.FullName -notlike "*\Logs\*" -and $_.FullName -notlike "*\OLD_TIFFs\*" -and $_.FullName -notlike "*\ZIP\*" -and $_.FullName -notlike "*\_EXPORT\*" -and $_.FullName -notlike "*\converted_zip\*" }

        if ($matchingFolders.Count -eq 0) {
            Write-Log "No folders matching '$FolderPattern' found in: $root" "WARN"
        } else {
            Write-Log "Folders found: $($matchingFolders.Count)"
            foreach ($f in $matchingFolders) { Write-Log "  $($f.FullName)" }
            Write-Log ""
            foreach ($folder in $matchingFolders) {
                Write-Log "==== Processing: $($folder.FullName)"
                Invoke-S5ProFolder -RootPath $folder.FullName -IsRecurse $false
                Write-Log ""
            }
        }
    } else {
        Write-Log "Root: $root"
        Invoke-S5ProFolder -RootPath $root -IsRecurse $false
    }
}

Write-Log ""
Write-Log ("-" * 50)
Write-Log "Done: $($script:okTotal) OK | $($script:skipTotal) skipped | $($script:missTotal) no JPEG pair | $($script:multiTotal) multi-page | $($script:warnTotal) warnings | $($script:errTotal) errors | $($script:counterTotal)/$($script:total) processed"

if ($script:multiTotal -gt 0) {
    Write-Log ""
    Write-Log "-- Multi-page TIFFs found (not touched):"
    foreach ($p in ($script:multiPagePaths | Sort-Object)) {
        Write-Log "   $p" "WARN"
    }
}
Write-Log "Log: $logFile"

if ($script:errTotal -gt 0 -or ($FailOnWarn -and ($script:warnTotal + $script:missTotal) -gt 0)) { exit 1 } else { exit 0 }