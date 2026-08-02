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
    [switch]$FailOnWarn
)
# ------------------------------------------------------------------

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
    if ($logFile) {
        Write-Log "Interrupted! Cleaning up staging files..." "WARN"
    } else {
        Write-Host "Interrupted! Cleaning up..." -ForegroundColor Yellow
    }
    foreach ($dir in $script:cleanupDirs) {
        if (Test-Path -LiteralPath $dir) {
            # Only remove staging files created by this run
            Get-ChildItem -LiteralPath $dir | Where-Object { $_.Name -like "$($script:runStagingId)_*" } | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
        }
    }
    break
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

function Test-TiffHasOnlySubfilePages {
    <#
    .SYNOPSIS
        Checks whether all pages beyond IFD[0] are non-independent subfile pages.
        Empty/missing subfiletype is treated as non-thumbnail (fail-closed).

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

    $r = Invoke-MagickWithTimeout -Arguments @("identify", "-format", "%[tiff:subfiletype]\n", $Path) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return $false }

    $subfileTypes = @($r.Output)
    # Fewer lines than pages means we could not classify every extra page -> fail closed
    if ($subfileTypes.Count -lt $PageCount) { return $false }

    for ($i = 1; $i -lt $PageCount; $i++) {
        $st = if ($subfileTypes[$i]) { "$($subfileTypes[$i])".Trim() } else { "" }
        if ($st -notin $AllowedSubfileTypes) {
            return $false
        }
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

    $r = Invoke-MagickWithTimeout -Arguments @("identify", "-format", "%[tiff:subfiletype]\n", $SrcPath) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return $false }

    $subfileTypes = @($r.Output)
    if ($subfileTypes.Count -le 1) { return $true }

    $tags = @()
    for ($i = 0; $i -lt $subfileTypes.Count; $i++) {
        $st = if ($subfileTypes[$i]) { "$($subfileTypes[$i])".Trim() } else { "" }
        $n = switch -Regex ($st) {
            '^(REDUCEDIMAGE|REDUCED)$' { 1; break }
            '^PAGE$'                   { 2; break }
            '^MASK$'                   { 4; break }
            default                    { 0 }
        }
        if ($i -eq 0) {
            # magick stamps PAGE on IFD0 of multi-page output; clear it when the source had none
            if ($n -eq 0) { $tags += "-IFD0:SubfileType=" } elseif ($n -ne 0) { $tags += "-IFD0:SubfileType#=$n" }
        } elseif ($n -gt 0) {
            $tags += "-IFD${i}:SubfileType#=$n"
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
    $tiffFiles = $allFiles | Where-Object { $_.Extension -match '^\.(tif|tiff)$' }

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

            if ($safeModeL) {
                $magickTimeoutSec = if ($MagickTimeout -gt 0) { $MagickTimeout } else { 30 }
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
                    if (-not (Test-TiffHasOnlySubfilePages -Path $p.Tiff -PageCount $pc.PageCount -TimeoutSec $magickTimeoutSec)) {
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