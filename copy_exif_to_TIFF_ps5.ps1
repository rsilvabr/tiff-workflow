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
    [string]$ExcludeFolders = ""     # ';'-separated folder NAMES to skip during discovery
)
# ------------------------------------------------------------------


# -- Excluded folder names ------------------------------------------
# ';'-separated folder NAMES (not paths): any TIFF whose directory contains a segment
# matching one of these (case-insensitive) is skipped during discovery. Segment match only,
# so "_EXPORT" never touches "My_EXPORT_photos". Kept identical to compress_tiff_zip.ps1 --
# the wizard asks this question once and applies it to every step of a workflow.
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
$script:runStagingId = [guid]::NewGuid().ToString('N')
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
        if     ($line -match '^OK\+SKIP-ZIP') { $script:skipTotal++ }
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
        (Invoke-MagickWithTimeout and Get-TiffPageCount must be injected too.)
    #>
    param([string]$SrcPath, [string]$DstPath, [int]$TimeoutSec = 30)

    $ps = Get-TiffPageCount -Path $SrcPath -TimeoutSec $TimeoutSec
    $pd = Get-TiffPageCount -Path $DstPath -TimeoutSec $TimeoutSec
    if (-not $ps.Ok -or -not $pd.Ok) { return $false }
    if ($ps.PageCount -ne $pd.PageCount) { return $false }

    for ($i = 0; $i -lt $ps.PageCount; $i++) {
        $r = Invoke-MagickWithTimeout -Arguments @("compare", "-metric", "RMSE", "$SrcPath[$i]", "$DstPath[$i]", "-format", "%[distortion]`n", "info:") -TimeoutSec $TimeoutSec
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

function Invoke-S5ProFolder {
    param([string]$RootPath, [bool]$IsRecurse)

    $allFiles  = Get-ChildItem -LiteralPath $RootPath -File -Recurse:$IsRecurse
    # JPEG index is always built recursively so JPEG/JPG subfolders can be found by Find-JpegPair
    $jpgFiles  = Get-ChildItem -LiteralPath $RootPath -File -Recurse | Where-Object { $_.Extension -match '^\.(jpg|jpeg)$' }
    $tiffFiles = @($allFiles | Where-Object { $_.Extension -match '^\.(tif|tiff)$' })

    if ($script:excludeNames.Count -gt 0) {
        $beforeExclude = $tiffFiles.Count
        $tiffFiles = @($tiffFiles | Where-Object {
            $segHit = $false
            foreach ($seg in ($_.DirectoryName -split '[\\/]')) {
                if ($seg -in $script:excludeNames) { $segHit = $true; break }
            }
            -not $segHit
        })
        $excluded = $beforeExclude - $tiffFiles.Count
        if ($excluded -gt 0) {
            Write-Log "Excluded $excluded file(s) under: $($script:excludeNames -join '; ')"
        }
    }

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
        param([System.IO.FileInfo]$tif)
        $dir    = $tif.DirectoryName
        $base   = $tif.BaseName
        $parent = Split-Path $dir -Parent

        $candidates = @($base)
        $stripped = ($base -replace '(_\d{3,4})$', '')
        if ($stripped -ne $base -and $stripped.Length -gt 0) { $candidates += $stripped }

        $searchDirs = @(
            $dir,
            (Join-Path $dir    "JPEG"),
            (Join-Path $dir    "JPG"),
            $parent,
            (Join-Path $parent "JPEG"),
            (Join-Path $parent "JPG")
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

        foreach ($b in $candidates) {
            foreach ($d in $searchDirs) {
                $key = ($d.ToLowerInvariant() + "|" + $b.ToLowerInvariant())
                if ($jpgIndex.ContainsKey($key)) {
                    return @{ Path = $jpgIndex[$key]; UsedBase = $b }
                }
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
            Write-Log ""
            Write-Log "-- Group: $groupDir ($($groupFiles.Count) file(s))"
        }

        $finalDir = if ($OutputDir)                            { $OutputDir }  else { $groupDir }
        $writeDir = if ($StagingDir -and -not $DryRun)         { $StagingDir } else { $finalDir }

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
                "MISS | $($p.TifName) | no matching JPEG (base: $($p.TifBase))"
                continue
            }

            # The _\d{3,4}$ strip in Find-JpegPair can match an unrelated JPEG
            # (foto_2024.tif -> foto.jpg): EXIF copied from the wrong file must be loud.
            $heurMatch = ($p.UsedBase -and $p.UsedBase -cne $p.TifBase)
            $okPrefix = if ($heurMatch) { "WARN (heuristic JPEG match)" } else { "OK" }
            $heurSuffix = if ($heurMatch) { " | TIFF base '$($p.TifBase)' matched JPEG base '$($p.UsedBase)'" } else { "" }

            if ($skipExifL) {
                $firstExif = exiftool -q -q -G1 -s -EXIF:all $p.Tiff 2>$null | Select-Object -First 1
                # Fail-closed: an unreadable TIFF must not be overwritten by the copy below
                if ($LASTEXITCODE -ne 0) { "ERROR (exiftool EXIF check) | $($p.TifName) | cannot inspect TIFF, not overwriting"; continue }
                if ($firstExif) { "SKIP (already has EXIF) | $($p.TifName)"; continue }
            }

            if ($dryL) {
                $zipInfo = if ($compressL) { " + ZIP" } else { "" }
                "DRY (EXIF$zipInfo) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))"
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
                $iccCheck = exiftool -s -s -s -ICC_Profile:all $p.Tiff 2>$null
                if (-not [string]::IsNullOrWhiteSpace($iccCheck)) { $tiffHasIcc = $true }
            }
            $copyIcc = ($iccPolicyL -eq "always") -or ($iccPolicyL -eq "preserve_tiff" -and -not $tiffHasIcc)
            $iccTag = if ($copyIcc) { "-ICC_Profile" } else { "" }

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

            $tagsArgs = @("-tagsfromfile", $p.Jpeg, "-EXIF:All", "-XMP:All", "-IPTC:All")
            if ($iccTag) { $tagsArgs += $iccTag }
            $tagsArgs += "-unsafe", $tiffTarget

            exiftool -q -q -overwrite_original -P @tagsArgs | Out-Null
            if ($LASTEXITCODE -ne 0) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "ERROR (exiftool EXIF) | $($p.TifName)"; continue
            }

            if (-not $compressL) {
                $copyNote = if ($tiffCopied) { " -> $finalDirL" } else { "" }
                "$okPrefix | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$copyNote$heurSuffix"
                continue
            }

            $comp = exiftool -s -s -s -Compression $tiffTarget 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $comp) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "ERROR (exiftool check) | $($p.TifName) | cannot detect compression"
                continue
            }
            if ($comp -match $(if ($skipLzwL) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
                "$(if ($heurMatch) { 'WARN (heuristic JPEG match)' } else { "OK+SKIP-ZIP ($comp)" }) | $($p.TifName)$heurSuffix"; continue
            }

            # Run-scoped prefix so the interrupt trap only cleans up this run's staged files
            $stagingName = "$($script:runStagingId)_$([guid]::NewGuid().ToString('N'))_$($p.DestName)"
            $writeDst = Join-Path $writeDirL $stagingName
            $finalDst = Join-Path $finalDirL $p.DestName

            if ((Test-Path -LiteralPath $finalDst) -and -not $overL -and ($finalDst -ne $p.Tiff) -and -not $tiffCopied) {
                # No cleanup here: the guard above already requires -not $tiffCopied
                "OK+SKIP-ZIP (exists) | $($p.TifName)"; continue
            }

            $magickErr = magick -quiet $tiffTarget -compress zip $writeDst 2>&1
            if ($LASTEXITCODE -ne 0) {
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

            exiftool -q -q -overwrite_original -tagsfromfile $tiffTarget -all:all -unsafe $writeDst | Out-Null
            # Store staging mapping BEFORE checking LASTEXITCODE so WARN files get moved too
            if ($stagingName) { $script:stagingMap[$p.Tiff] = $stagingName }
            if ($LASTEXITCODE -ne 0) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "WARN (exiftool metadata copy failed, ZIP ok) | $($p.TifName)"; continue
            }

            if (-not (Restore-TiffSubfileTypes -SrcPath $tiffTarget -DstPath $writeDst)) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                "WARN (subfiletype restore failed, ZIP ok) | $($p.TifName)"; continue
            }

            if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
            "$(if ($heurMatch) { 'WARN (heuristic JPEG match)' } else { 'OK+ZIP' }) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$heurSuffix"
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