# -- CLI PARAMETERS -------------------------------------------------
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
    [string]$ExcludeFolders = "",    # ';'-separated folder NAMES to skip during discovery
    [double]$WorkerMemoryFraction = 0.8  # Accepted for interface parity; this script is sequential and ignores it
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

# -- Logging -----------------------------------------------------------
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
$script:zipSkipTotal = 0
$script:missTotal    = 0
$script:errTotal     = 0
$script:warnTotal    = 0
$script:multiTotal   = 0
$script:multiPagePaths = [System.Collections.Concurrent.ConcurrentBag[string]]::new()
# Destinations already claimed in this run (only used with -OutputDir, where every
# session writes into the same folder). Shared across folders on purpose.
$script:claimedDest  = @{}

function Process-Results {
    param($lines)
    foreach ($line in $lines) {
        $script:counterTotal++
        $lvl = "INFO"
        # OK+SKIP-ZIP: the EXIF WAS written, only the ZIP step was skipped (already
        # compressed / output exists) -- an OK, not a skip (audit round 21).
        if     ($line -match '^OK\+SKIP-ZIP') { $script:okTotal++; $script:zipSkipTotal++ }
        elseif ($line -match '^OK')           { $script:okTotal++ }
        elseif ($line -match '^SKIP')         { $script:skipTotal++ }
        elseif ($line -match '^MISS')  { $script:missTotal++; $lvl = "WARN" }
        elseif ($line -match '^ERROR')  { $script:errTotal++;  $lvl = "ERROR" }
        elseif ($line -match '^WARN')  { $script:warnTotal++; $lvl = "WARN" }
        elseif ($line -match '^MULTI')  { $script:multiTotal++; $lvl = "WARN" }
        Write-Log "[$($script:counterTotal)/$($script:total)] $line" $lvl
    }
}

function Invoke-MagickWithTimeout {
    <#
    .SYNOPSIS
        Runs `magick` with the given arguments under a timeout and returns
        @{ TimedOut = [bool]; ExitCode = [int]; Output = [string[]] }.

    .NOTES
        Uses an in-process runspace instead of Start-Job: Start-Job spawns a whole
        PowerShell process per call (~700 ms), which on this sequential PS5 path was
        paid once per file, back to back.

        ExitCode -1 means "could not determine" -- every caller must treat it as failure.

        Duplicated across compress_tiff_zip.ps1 and copy_exif_to_TIFF_ps*.ps1 so each
        script stays self-contained. Keep the implementations identical.
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

function Invoke-ExifTool {
    <#
    .SYNOPSIS
        Runs exiftool with its arguments routed through a UTF-8 `-@` argfile and returns
        @{ ExitCode = [int]; Output = $null/string/array }.

    .DESCRIPTION
        The main copy/probe/restore calls used to pass file names as raw argv, so a non-ASCII
        name reached exiftool in the console code page and could not be found. Every invocation
        now goes through a per-call argfile: `-charset filename=utf8` first, then one argument
        per line (options and operands on their own lines, paths as-is), UTF-8 without BOM
        (WriteAllText's default encoding) -- the same pattern compress_tiff_zip.ps1 uses for
        every exiftool call. -MergeStderr folds stderr into Output, for the callers whose
        fail-closed 'Error opening file' check reads the combined stream.

    .NOTES
        Re-inject into -Parallel runspaces with:
            ${function:Invoke-ExifTool} = $using:ExifToolFnDef
    #>
    param(
        [string[]]$Arguments = @(),
        [switch]$MergeStderr
    )

    $argFile = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($argFile, ((@('-charset', 'filename=utf8') + $Arguments) -join "`n") + "`n")
        if ($MergeStderr) {
            $out = exiftool -@ $argFile 2>&1
        } else {
            $out = exiftool -@ $argFile 2>$null
        }
        return @{ ExitCode = $LASTEXITCODE; Output = $out }
    } finally {
        Remove-Item $argFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-TiffPageCount {
    <#
    .SYNOPSIS
        Returns @{ Ok = [bool]; PageCount = [int]; Error = [string] } for a TIFF.
        Error is "timeout", "failed" or "parse:<raw>" -- never silently 0.
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
        -LoadFactor = workers running at once (they share the disk); the -Parallel blocks
        pass it, sequential callers leave it at 1.
    #>
    param([string]$SrcPath, [string]$DstPath, [int]$TimeoutSec = 30, [double]$LoadFactor = 1)

    # Audit round 16: per-page compare of multi-hundred-MB scans overran the flat 30 s
    # probe timeout. Scale by the larger input (~0.25 s/MB) -- see Test-ZipIntegrity.
    try {
        $maxMB = 0.0
        foreach ($p in @($SrcPath, $DstPath)) {
            $mb = (Get-Item -LiteralPath $p -ErrorAction Stop).Length / 1MB
            if ($mb -gt $maxMB) { $maxMB = $mb }
        }
        $TimeoutSec = [Math]::Max($TimeoutSec, [int][Math]::Ceiling($maxMB * 0.25 * [Math]::Max(1.0, $LoadFactor)))
    } catch { }

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
        # A failed dump can still leave a PARTIAL container behind; remove it so nothing
        # downstream mistakes it for a complete backup.
        if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $metaBackup -Force -ErrorAction SilentlyContinue; return @{ Needed = $true; Path = $null } }
    } catch {
        Remove-Item -LiteralPath $metaBackup -Force -ErrorAction SilentlyContinue
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

    # Deterministic order: destination claiming hands out _v2 renames in enumeration order,
    # which otherwise depends on Get-ChildItem's (filesystem-dependent) order.
    $tiffFiles = @($tiffFiles | Sort-Object FullName)

    if ($tiffFiles.Count -eq 0) {
        Write-Log "No TIFFs found in: $RootPath" "WARN"
        return
    }

    $script:total += $tiffFiles.Count
    Write-Log "TIFFs: $($tiffFiles.Count) | JPEGs: $($jpgFiles.Count)"

    $jpgIndex = @{}
    foreach ($j in $jpgFiles) {
        $key = ($j.DirectoryName.ToLowerInvariant() + "|" + $j.BaseName.ToLowerInvariant())
        if (-not $jpgIndex.ContainsKey($key)) {
            $jpgIndex[$key] = $j.FullName
        } elseif ($j.Extension.ToLowerInvariant() -eq ".jpg") {
            $jpgIndex[$key] = $j.FullName
        }
    }

    function Find-JpegPair {
        # Search order (first hit wins), each step over every $searchDirs entry:
        #   JPEG exact -> JPEG without the Capture One variant suffix (_1, _2 ...) ->
        #   RAF exact -> RAF without the variant suffix -> JPEG heuristic (_\d{3,4}).
        # The RAF steps come before the heuristic: for DSC_0001.tif, DSC_0001.RAF is the
        # right source and DSC.jpg is not. Audit round 20.
        param([System.IO.FileInfo]$tif)
        $dir    = $tif.DirectoryName
        $base   = $tif.BaseName
        $parent = Split-Path $dir -Parent
        $variant = ($base -replace '_\d{1,2}$', '')
        $heur    = ($base -replace '(_\d{3,4})$', '')
        $steps = @(@{ Base = $base; Kind = "exact"; Src = "JPEG" })
        if ($variant -ne $base -and $variant.Length -gt 0) { $steps += @{ Base = $variant; Kind = "variant"; Src = "JPEG" } }
        $steps += @{ Base = $base; Kind = "exact"; Src = "RAF" }
        if ($variant -ne $base -and $variant.Length -gt 0) { $steps += @{ Base = $variant; Kind = "variant"; Src = "RAF" } }
        if ($heur -ne $base -and $heur.Length -gt 0) { $steps += @{ Base = $heur; Kind = "heuristic"; Src = "JPEG" } }
        $searchDirs = @($dir, (Join-Path $dir "JPEG"), (Join-Path $dir "JPG"),
                        $parent, (Join-Path $parent "JPEG"), (Join-Path $parent "JPG")) |
                      Where-Object { $_ -and (Test-Path -LiteralPath $_) }
        foreach ($s in $steps) {
            $b = $s.Base
            foreach ($d in $searchDirs) {
                if ($s.Src -eq "JPEG") {
                    $key = ($d.ToLowerInvariant() + "|" + $b.ToLowerInvariant())
                    if ($jpgIndex.ContainsKey($key)) { return @{ Path = $jpgIndex[$key]; UsedBase = $b; MatchKind = $s.Kind; SourceKind = "JPEG" } }
                    # The index only covers $RootPath, so the parent folder (and its JPEG/JPG
                    # subfolders) could never produce a hit. Probe the filesystem directly.
                    foreach ($ext in @(".jpg", ".jpeg")) {
                        $probe = Join-Path $d "$b$ext"
                        if (Test-Path -LiteralPath $probe -PathType Leaf) {
                            return @{ Path = (Get-Item -LiteralPath $probe).FullName; UsedBase = $b; MatchKind = $s.Kind; SourceKind = "JPEG" }
                        }
                    }
                } else {
                    # A directory listing, not Get-Item on a built path: Get-Item echoes the
                    # case it was given (.raf), the listing returns the real name (.RAF).
                    $raf = @(Get-ChildItem -LiteralPath $d -File -Filter "$b.raf" -ErrorAction SilentlyContinue |
                             Where-Object { $_.BaseName -ieq $b -and $_.Extension -ieq ".raf" })[0]
                    if ($raf) {
                        return @{ Path = $raf.FullName; UsedBase = $b; MatchKind = $s.Kind; SourceKind = "RAF" }
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
            Write-Log ""
            Write-Log "-- Group: $groupDir ($($groupFiles.Count) file(s))"
        }

        $finalDir = if ($OutputDir)                            { $OutputDir }  else { $groupDir }
        $writeDir = if ($StagingDir -and -not $DryRun)         { $StagingDir } else { $finalDir }

        # With no -StagingDir the staged ZIP is written INTO the source folder, but only
        # -StagingDir was ever registered for cleanup -- so an interrupted run left
        # `<runId>_<12 hex>_photo.tif` sitting next to the photos, and the next run discovered
        # it as an ordinary source. Register whatever folder this run actually stages into; the
        # trap only removes names carrying THIS run's prefix.
        if (-not $DryRun -and $script:cleanupDirs -notcontains $writeDir) { $script:cleanupDirs += $writeDir }

        if ($CompressZip -and $StagingDir -and -not $DryRun) { [System.IO.Directory]::CreateDirectory($StagingDir) | Out-Null }
        if ($CompressZip -and $OutputDir)                    { [System.IO.Directory]::CreateDirectory($OutputDir)  | Out-Null }

        # DestName: with -OutputDir every session writes into the SAME folder, so two
        # sessions holding DSC_0001.tif used to silently overwrite/skip each other.
        # Later claimants get _v2, _v3, ... (same policy as compress_tiff_zip.ps1).
        $destNameMap = @{}
        $pairs = @(foreach ($tif in $groupFiles) {
            $pair = Find-JpegPair $tif
            $destName = $tif.Name
            if ($OutputDir -and ($finalDir -ine $tif.DirectoryName)) {
                $dKey = (Join-Path $finalDir $destName).ToLowerInvariant()
                if ($script:claimedDest.ContainsKey($dKey) -and $script:claimedDest[$dKey] -ine $tif.FullName) {
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
                MatchKind  = if ($pair) { $pair.MatchKind }  else { $null }
                SourceKind = if ($pair) { $pair.SourceKind } else { $null }
            }
        })

        $script:stagingMap = @{}

            # Sequential -- compatible with PowerShell 5.1
        $results = foreach ($p in $pairs) {
            $skipExifL = $SkipIfTiffHasExif
            $dryL      = $DryRun
            $compressL = $CompressZip
            $writeDirL = $writeDir
            $finalDirL = $finalDir
            $overL     = $Overwrite
            $skipLzwL  = $SkipLzwAsCompressed
            $safeModeL = $SafeMode
            $bagL      = $script:multiPagePaths
            $iccPolicyL = $IccPolicy

            if (-not $p.Jpeg) {
                "MISS | $($p.TifName) | no matching JPEG or RAF (base: $($p.TifBase))"
                continue
            }

            # Only the _\d{3,4}$ strip is heuristic: it can match an unrelated JPEG
            # (foto_2024.tif -> foto.jpg). The Capture One variant suffix _\d{1,2}$
            # (_DSF4817_1 -> _DSF4817) is the normal name of the photo and reports OK.
            # Case-insensitive: Find-JpegPair matched case-insensitively, so
            # DSC_0001.tif + dsc_0001.jpg is an EXACT stem match, not a heuristic one.
            $heurMatch = ($p.UsedBase -and $p.UsedBase -ine $p.TifBase -and $p.MatchKind -eq "heuristic")
            $okPrefix = if ($heurMatch) { "WARN (heuristic JPEG match)" } else { "OK" }
            $heurSuffix = if ($heurMatch) { " | TIFF base '$($p.TifBase)' matched JPEG base '$($p.UsedBase)'" } else { "" }
            $rawSuffix = if ($p.SourceKind -eq "RAF") { " [EXIF from RAW]" } else { "" }

            if ($skipExifL) {
                # No `| Select-Object -First 1` on the native call: -First stops the upstream
                # pipeline, so $LASTEXITCODE could still hold an earlier command's value --
                # and the check right below is the fail-closed gate that decides whether this
                # TIFF may be overwritten. Read it all, check the exit code, then take [0].
                $exifProbeRes = Invoke-ExifTool -Arguments @('-q','-q','-G1','-s','-EXIF:all',$p.Tiff)
                $exifProbeExit = $exifProbeRes.ExitCode
                $firstExif = @($exifProbeRes.Output | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") })[0]
                # Fail-closed: an unreadable TIFF must not be overwritten by the copy below
                if ($exifProbeExit -ne 0) { "ERROR (exiftool EXIF check) | $($p.TifName) | cannot inspect TIFF, not overwriting"; continue }
                if ($firstExif) { "SKIP (already has EXIF) | $($p.TifName)"; continue }
            }

            if ($dryL) {
                $zipInfo = if ($compressL) { " + ZIP" } else { "" }
                "DRY (EXIF$zipInfo) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$rawSuffix"
                continue
            }

            # Hoisted out of the SafeMode block: the ZIP pixel gate below needs it even when
            # -SafeMode:$false skipped the page-count check.
            $magickTimeoutSec = if ($MagickTimeout -gt 0) { $MagickTimeout } else { 30 }

            if ($safeModeL) {
                $pc = Get-TiffPageCount -Path $p.Tiff -TimeoutSec $magickTimeoutSec
                if (-not $pc.Ok) {
                    switch -Wildcard ($pc.Error) {
                        "timeout" { "ERROR (magick timeout) | $($p.TifName) | possibly corrupted" }
                        "parse:*" { "ERROR (magick page count parse) | $($p.TifName) | unexpected output: $($pc.Error.Substring(6))" }
                        default   { "ERROR (magick page count failed) | $($p.TifName) | possibly corrupted" }
                    }
                    continue
                }
                if ($pc.PageCount -gt 1) {
                    # -AllowedSubfileTypes is passed explicitly, exactly like the four call
                    # sites in compress_tiff_zip.ps1. Falling back to the parameter default
                    # (which also allows MASK and PAGE) made SafeMode accept scanner RGB+IR
                    # files that the compression backend skips -- the opposite of what the
                    # README promises for "scanner IR files".
                    if (-not (Test-TiffHasOnlySubfilePages -Path $p.Tiff -PageCount $pc.PageCount -AllowedSubfileTypes @("REDUCEDIMAGE", "REDUCED") -TimeoutSec $magickTimeoutSec)) {
                        $bagL.Add($p.Tiff) | Out-Null
                        "MULTI ($($pc.PageCount) IFDs -- skipped) | $($p.TifName)"
                        continue
                    }
                }
            }

            # Check if TIFF already has ICC (fixed logic - don't reset $LASTEXITCODE)
            $tiffHasIcc = $false
            if ($iccPolicyL -eq "preserve_tiff" -or $iccPolicyL -eq "always") {
                $iccRes = Invoke-ExifTool -Arguments @('-s','-s','-s','-ICC_Profile:all',$p.Tiff)
                if (-not [string]::IsNullOrWhiteSpace("$($iccRes.Output)")) { $tiffHasIcc = $true }
            }
            $copyIcc = ($iccPolicyL -eq "always") -or ($iccPolicyL -eq "preserve_tiff" -and -not $tiffHasIcc)
            $iccTag = if ($copyIcc) { "-ICC_Profile" } else { "" }
            # A RAF does not describe the colour space of the exported TIFF.
            if ($p.SourceKind -eq "RAF") { $iccTag = "" }

            # Determine target TIFF path: if OutputDir is specified (different from source dir), copy first to preserve original
            $tiffTarget = $p.Tiff
            $tiffCopied = $false
            $srcDir = [System.IO.Path]::GetFullPath((Split-Path $p.Tiff -Parent)).TrimEnd('\', '/')
            if ($finalDirL -and ($finalDirL -ine $srcDir) -and -not $dryL) {
                $destTiff = Join-Path $finalDirL $p.DestName
                if (-not (Test-Path -LiteralPath $destTiff) -or $overL) {
                    if (-not (Test-Path -LiteralPath $finalDirL)) {
                        [System.IO.Directory]::CreateDirectory($finalDirL) | Out-Null
                    }
                    try {
                        Copy-Item -LiteralPath $p.Tiff -Destination $destTiff -Force
                        $tiffTarget = $destTiff
                        $tiffCopied = $true
                    } catch {
                        # A partial copy must not stay behind: a later run without -Overwrite
                        # would skip it as "exists in OutputDir" and the truncated TIFF persists
                        if (Test-Path -LiteralPath $destTiff) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                        "ERROR (copy to OutputDir failed) | $($p.TifName): $($_.Exception.Message)"
                        continue
                    }
                } else {
                    "SKIP (exists in OutputDir) | $($p.TifName)"
                    continue
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
                "ERROR (metadata container failed - EXIF copy refused) | $($p.TifName)"; continue
            }
            $jpegTagSource = if ($jpegMeta.Path) { $jpegMeta.Path } else { $p.Jpeg }

            # No -unsafe: it stamps the JPEG's GEOMETRY (ImageWidth/Height, ExifImageWidth/
            # Height, Orientation) onto a TIFF whose dimensions legitimately differ (Capture
            # One crops/resizes). Those tags are excluded explicitly instead; the remaining
            # -unsafe-only tags (embedded thumbnail/preview images) do not belong on the TIFF
            # either. Verified live on a synthetic pair: EXIF DateTime/MakerNotes copy,
            # geometry does not.
            $tagsArgs = @('-tagsfromfile', $jpegTagSource, '-EXIF:All', '-XMP:All', '-IPTC:All',
                          '--ImageWidth', '--ImageHeight', '--ImageLength',
                          '--ExifImageWidth', '--ExifImageHeight', '--PixelXDimension', '--PixelYDimension',
                          '--Orientation', '--ThumbnailImage', '--PreviewImage', '--JpgFromRaw')
            # A RAF's EXIF describes its embedded 1440x960 preview: the focal-plane
            # resolution (630 vs 1861 px/cm on an S5 Pro) is wrong for the TIFF.
            if ($p.SourceKind -eq "RAF") {
                $tagsArgs += @('--FocalPlaneXResolution', '--FocalPlaneYResolution', '--FocalPlaneResolutionUnit')
            }
            if ($iccTag) { $tagsArgs += $iccTag }
            $tagsArgs += $tiffTarget

            # ONE -q, not two: the second suppresses WARNINGS, and a warning is the only signal
            # exiftool gives when the tagsfromfile source could not be opened -- it still exits 0.
            $exifRes = Invoke-ExifTool -Arguments (@('-q','-overwrite_original','-P') + $tagsArgs) -MergeStderr
            $exifOut = "$($exifRes.Output)"
            $exifExit = $exifRes.ExitCode
            if ($jpegMeta.Path) { Remove-Item -LiteralPath $jpegMeta.Path -Force -ErrorAction SilentlyContinue }
            # Only "Error opening file" is fatal: it means the SOURCE was never read. A JPEG that
            # legitimately carries nothing writable reports differently and stays non-fatal.
            if ($exifExit -ne 0 -or $exifOut -match 'Error opening file') {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "ERROR (exiftool EXIF) | $($p.TifName)"; continue
            }

            if (-not $compressL) {
                $copyNote = if ($tiffCopied) { " -> $finalDirL" } else { "" }
                "$okPrefix | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$copyNote$heurSuffix$rawSuffix"
                continue
            }

            # -IFD0:Compression, and collapsed to the first line below: exiftool can print one
            # line per IFD and `-match` on an ARRAY is truthy when ANY element matches, so an
            # uncompressed main image whose thumbnail page happened to be compressed was
            # reported "already ZIP" and never compressed. compress_tiff_zip.ps1 fixed this in
            # four places; these two backends were the copies left behind.
            $compRes = Invoke-ExifTool -Arguments @('-s','-s','-s','-IFD0:Compression',$tiffTarget)
            if ($compRes.ExitCode -ne 0 -or -not $compRes.Output) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "ERROR (exiftool check) | $($p.TifName) | cannot detect compression"
                continue
            }
            $comp = "$(@($compRes.Output)[0])".Trim()
            if ($comp -match $(if ($skipLzwL) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
                "$(if ($heurMatch) { "WARN (heuristic JPEG match, already $comp -- ZIP skipped)" } else { "OK+SKIP-ZIP ($comp)" }) | $($p.TifName)$heurSuffix$rawSuffix"; continue
            }

            # Run-scoped prefix so the interrupt trap only cleans up this run's staged files
            $stagingName = "$($script:runStagingId)_$([guid]::NewGuid().ToString('N').Substring(0, 12))_$($p.DestName)"
            $writeDst = Join-Path $writeDirL $stagingName
            $finalDst = Join-Path $finalDirL $p.DestName

            if ((Test-Path -LiteralPath $finalDst) -and -not $overL -and ($finalDst -ine $p.Tiff) -and -not $tiffCopied) {
                # No cleanup here: the guard above already requires -not $tiffCopied
                "OK+SKIP-ZIP (exists) | $($p.TifName)$rawSuffix"; continue
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
                "ERROR (magick ZIP) | $($p.TifName) | $magickErr"; continue
            }

            # The staged ZIP is moved ON TOP of the TIFF it came from (see the move loop
            # below) and there is no OLD_TIFFs backup on this path, so it must be proven
            # pixel-identical first. A zero exit from magick is not proof: a page-short or
            # truncated file still exits 0 and still decodes. Same fail-closed gate that
            # compress_tiff_zip.ps1 requires before a mode 8 delete.
            if (-not (Test-PixelIdentical -SrcPath $tiffTarget -DstPath $writeDst -TimeoutSec $magickTimeoutSec)) {
                Remove-Item -LiteralPath $writeDst -Force -ErrorAction SilentlyContinue
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "ERROR (ZIP pixel verification failed - original untouched) | $($p.TifName)"; continue
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
                $restoreRes = Invoke-ExifTool -Arguments @('-q','-overwrite_original','-tagsfromfile',$tiffTagSource,'-all:all','-unsafe',$writeDst) -MergeStderr
                $restoreOut = "$($restoreRes.Output)"
                $restoreExit = $restoreRes.ExitCode
                if ($restoreOut -match 'Error opening file') { $restoreExit = 1 }
            }
            if ($tiffMeta.Path) { Remove-Item -LiteralPath $tiffMeta.Path -Force -ErrorAction SilentlyContinue }
            # The staging mapping is registered ONLY after every restore gate below has passed:
            # the move loop moves whatever is registered, so a staged ZIP whose metadata restore
            # failed was moved ON TOP of the EXIF-bearing original and reported as a mere WARN
            # (exit 0). On failure the staged file is deleted, the original stays untouched and
            # the file counts as an error.
            if ($restoreExit -ne 0) {
                Remove-Item -LiteralPath $writeDst -Force -ErrorAction SilentlyContinue
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "ERROR (exiftool metadata restore failed - original untouched) | $($p.TifName)"; continue
            }

            if (-not (Restore-TiffSubfileTypes -SrcPath $tiffTarget -DstPath $writeDst -TimeoutSec $magickTimeoutSec)) {
                Remove-Item -LiteralPath $writeDst -Force -ErrorAction SilentlyContinue
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "ERROR (subfiletype restore failed - original untouched) | $($p.TifName)"; continue
            }

            if ($stagingName) { $script:stagingMap[$p.Tiff] = $stagingName }

            if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
            "$(if ($heurMatch) { 'WARN (heuristic JPEG match)' } else { 'OK+ZIP' }) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$heurSuffix$rawSuffix"
        }

        foreach ($line in $results) { Process-Results @($line) }

        # Move from staging to final destination (with integrity check and UUID mapping)
        if ($CompressZip -and -not $DryRun) {
            $moved = 0
            foreach ($tif in $groupFiles) {
                # Use full path as key (filename-only collides across folders)
                $tifFullPath = $tif.FullName
                # No fallback to <writeDir>\<tif.Name>: this run stages under a run-scoped
                # prefix, so an unprefixed file of that name belongs to somebody else. Moving
                # it would drop a stranger's file on top of the user's TIFF.
                if (-not $script:stagingMap.ContainsKey($tifFullPath)) { continue }
                $stagingName = $script:stagingMap[$tifFullPath]
                $stagePath = Join-Path $writeDir $stagingName
                $destName = if ($destNameMap.ContainsKey($tif.FullName)) { $destNameMap[$tif.FullName] } else { $tif.Name }
                $destPath  = Join-Path $finalDir   $destName
                if ((Test-Path -LiteralPath $stagePath) -and $stagePath -ine $destPath) {
                    try {
                        # In-place, $destPath IS $tif.FullName: the original mtime must be
                        # captured BEFORE the move -- reading it afterwards read the staged
                        # file's own fresh timestamp and preserved nothing.
                        $origMtime = $null
                        try { $origMtime = (Get-Item -LiteralPath $tif.FullName).LastWriteTime } catch { }
                        $stageSize = (Get-Item -LiteralPath $stagePath).Length
                        Move-Item -Force -LiteralPath $stagePath -Destination $destPath -ErrorAction Stop
                        if ((Test-Path -LiteralPath $destPath) -and ((Get-Item -LiteralPath $destPath).Length -eq $stageSize)) {
                            # The staged ZIP is a new file with "now" as mtime; the plain
                            # EXIF-only path preserves the source date via exiftool -P, so
                            # the ZIP path does the same for consistency.
                            if ($origMtime) { try { (Get-Item -LiteralPath $destPath).LastWriteTime = $origMtime } catch { } }
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

# -- Entry point ----------------------------------------------------
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

# A doubled entry ("A;A") used to process the same folder twice.
$seenInputDirs = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$inputDirs = @($inputDirs | Where-Object { $seenInputDirs.Add($_) })

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

        # Escape the pattern: -like is a WILDCARD match, so a '[' in a folder name would be
        # parsed as a character class and never match the literal folder.
        $folderPatternEsc = [System.Management.Automation.WildcardPattern]::Escape($FolderPattern)
        $matchingFolders = Get-ChildItem -LiteralPath $root -Directory -Recurse |
                           Where-Object { $_.Name -like "*$folderPatternEsc*" -and $_.FullName -notlike "*\Logs\*" -and $_.FullName -notlike "*\OLD_TIFFs\*" -and $_.FullName -notlike "*\ZIP\*" -and $_.FullName -notlike "*\_EXPORT\*" -and $_.FullName -notlike "*\converted_zip\*" }

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
Write-Log "Done: $($script:okTotal) OK$(if ($script:zipSkipTotal -gt 0) { " (ZIP step skipped on $($script:zipSkipTotal))" }) | $($script:skipTotal) skipped | $($script:missTotal) no JPEG pair | $($script:multiTotal) multi-page | $($script:warnTotal) warnings | $($script:errTotal) errors | $($script:counterTotal)/$($script:total) processed"

if ($script:multiTotal -gt 0) {
    Write-Log ""
    Write-Log "-- Multi-page TIFFs found (not touched):"
    foreach ($p in ($script:multiPagePaths | Sort-Object)) {
        Write-Log "   $p" "WARN"
    }
}
Write-Log "Log: $logFile"

if ($script:errTotal -gt 0 -or ($FailOnWarn -and ($script:warnTotal + $script:missTotal) -gt 0)) { exit 1 } else { exit 0 }