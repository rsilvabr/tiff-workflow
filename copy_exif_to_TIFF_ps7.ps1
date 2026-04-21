# ── CLI PARAMETERS ──────────────────────────────────────────────────────
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
    [int]$MagickTimeout = 30
)
# ──────────────────────────────────────────────────────────────────

# ── Logging ───────────────────────────────────────────────────────
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

# ── Cleanup on interrupt ─────────────────────────────────────────
$script:cleanupDirs  = @()
$script:cleanupFiles = @()
if (-not [string]::IsNullOrWhiteSpace($StagingDir)) { $script:cleanupDirs += $StagingDir }

trap {
    if ($logFile) {
        Write-Log "Interrupted! Cleaning up staging files..." "WARN"
    } else {
        Write-Host "Interrupted! Cleaning up..." -ForegroundColor Yellow
    }
    foreach ($dir in $script:cleanupDirs) {
        if (Test-Path -LiteralPath $dir) {
            Remove-Item -Path "$dir\*" -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($file in $script:cleanupFiles) {
        if (Test-Path -LiteralPath $file) {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
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
$script:multiTotal    = 0
$script:multiPagePaths = [System.Collections.Concurrent.ConcurrentBag[string]]::new()

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

function Invoke-S5ProFolder {
    param([string]$RootPath, [bool]$IsRecurse)

    $allFiles  = Get-ChildItem -LiteralPath $RootPath -File -Recurse:$IsRecurse
    $jpgFiles  = $allFiles | Where-Object { $_.Extension -match '^\.(jpg|jpeg)$' }
    $tiffFiles = $allFiles | Where-Object { $_.Extension -match '^\.(tif|tiff)$' }

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
            }
        }
        return $null
    }

    $groups = $tiffFiles | Group-Object { $_.DirectoryName }

    foreach ($group in $groups) {
        $groupDir   = $group.Name
        $groupFiles = $group.Group

        if ($groups.Count -gt 1 -or $AutoFind) {
            Write-Log ""; Write-Log "── Group: $groupDir ($($groupFiles.Count) file(s))"
        }

        $finalDir = if ($OutputDir) { $OutputDir } else { $groupDir }
        $writeDir = if ($StagingDir -and -not $DryRun) { $StagingDir } else { $finalDir }

        if ($CompressZip -and $StagingDir -and -not $DryRun) { [System.IO.Directory]::CreateDirectory($StagingDir) | Out-Null }
        if ($CompressZip -and $OutputDir)                    { [System.IO.Directory]::CreateDirectory($OutputDir)  | Out-Null }

        # Resolve TIFF→JPEG pairs
        $pairs = @(foreach ($tif in $groupFiles) {
            $pair = Find-JpegPair $tif
            [PSCustomObject]@{
                Tiff     = $tif.FullName
                TifName  = $tif.Name
                TifBase  = $tif.BaseName
                Jpeg     = if ($pair) { $pair.Path }    else { $null }
                UsedBase = if ($pair) { $pair.UsedBase } else { $null }
            }
        })

        if ($pairs.Count -eq 0) {
            $script:skipTotal += $groupFiles.Count
            $script:counterTotal += $groupFiles.Count
            continue
        }

        # Synchronous parallel — more reliable than -AsJob when run from terminal
        $writeDirCapture  = $writeDir
        $finalDirCapture  = $finalDir
        $skipExifCapture  = $SkipIfTiffHasExif
        $dryCapture       = $DryRun
        $compressCapture  = $CompressZip
        $overCapture      = $Overwrite
        $skipLzwCapture   = $SkipLzwAsCompressed
        $safeModeCapture  = $SafeMode
        $multiPageBagCapture = $script:multiPagePaths
        $iccPolicyCapture = $IccPolicy
        $magickTimeoutCapture = $MagickTimeout

        $results = $pairs | ForEach-Object -ThrottleLimit $Workers -Parallel {
            $p         = $_
            $skipExifL = $using:skipExifCapture
            $dryL      = $using:dryCapture
            $compressL = $using:compressCapture
            $writeDirL = $using:writeDirCapture
            $finalDirL = $using:finalDirCapture
            $overL     = $using:overCapture
            $skipLzwL  = $using:skipLzwCapture
            $safeModeL = $using:safeModeCapture
            $bagL      = $using:multiPageBagCapture
            $iccPolicyL = $using:iccPolicyCapture
            $magickTimeoutL = $using:magickTimeoutCapture

            if (-not $p.Jpeg) {
                return @{ Result = "MISS | $($p.TifName) | no matching JPEG (base: $($p.TifBase))"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
            }

            if ($skipExifL) {
                $firstExif = exiftool -q -q -G1 -s -EXIF:all $p.Tiff 2>$null | Select-Object -First 1
                if ($firstExif) { return @{ Result = "SKIP (already has EXIF) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff } }
            }

            if ($dryL) {
                $zipInfo = if ($compressL) { " + ZIP" } else { "" }
                return @{ Result = "DRY (EXIF$zipInfo) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
            }

            if ($safeModeL) {
                $magickTimeoutSec = if ($magickTimeoutL -gt 0) { $magickTimeoutL } else { 30 }
                $tiffPathCapture = $p.Tiff
                $pageCountJob = $null
                try {
                    $pageCountJob = Start-Job { param($path) magick identify -format "%n" $path 2>$null } -ArgumentList $tiffPathCapture
                    $pageCountJob | Wait-Job -Timeout $magickTimeoutSec | Out-Null
                    if ($pageCountJob.State -eq 'Running') {
                        Stop-Job $pageCountJob
                        return @{ Result = "ERROR (magick timeout) | $($p.TifName) | possibly corrupted"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
                    }
                    $pageCountStr = $pageCountJob | Receive-Job
                    if ([string]::IsNullOrWhiteSpace($pageCountStr)) {
                        return @{ Result = "ERROR (magick page count failed) | $($p.TifName) | possibly corrupted"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
                    }
                    $pageCount = [int]$pageCountStr
                    if ($pageCount -gt 1) {
                        return @{ Result = "MULTI ($pageCount IFDs — skipped) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; MultiPagePath = $p.Tiff }
                    }
                } finally {
                    if ($pageCountJob) {
                        Remove-Job $pageCountJob -Force -ErrorAction SilentlyContinue
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
            if ($finalDirL -and ($finalDirL -ne (Split-Path $p.Tiff -Parent)) -and -not $dryL) {
                $destTiff = Join-Path $finalDirL $p.TifName
                if (-not (Test-Path -LiteralPath $destTiff) -or $overL) {
                    if (-not (Test-Path -LiteralPath $finalDirL)) {
                        [System.IO.Directory]::CreateDirectory($finalDirL) | Out-Null
                    }
                    try {
                        Copy-Item -LiteralPath $p.Tiff -Destination $destTiff -Force
                        $tiffTarget = $destTiff
                        $tiffCopied = $true
                    } catch {
                        return @{ Result = "ERROR (copy to OutputDir failed) | $($p.TifName): $($_.Exception.Message)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
                    }
                } else {
                    return @{ Result = "SKIP (exists in OutputDir) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
                }
            }

            $tagsArgs = @("-tagsfromfile", $p.Jpeg, "-EXIF:All", "-XMP:All", "-IPTC:All")
            if ($iccTag) { $tagsArgs += $iccTag }
            $tagsArgs += "-unsafe", $tiffTarget

            exiftool -q -q -overwrite_original -P @tagsArgs | Out-Null
            if ($LASTEXITCODE -ne 0) { return @{ Result = "ERROR (exiftool EXIF) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff } }

            if (-not $compressL) {
                $copyNote = if ($tiffCopied) { " -> $finalDirL" } else { "" }
                return @{ Result = "OK | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$copyNote"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
            }

            $comp = exiftool -s -s -s -Compression $tiffTarget 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $comp) {
                return @{ Result = "ERROR (exiftool check) | $($p.TifName) | cannot detect compression"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
            }
            if ($comp -match $(if ($skipLzwL) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
                return @{ Result = "OK+SKIP-ZIP ($comp) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
            }

            $stagingName = "$([guid]::NewGuid().ToString('N'))_$($p.TifName)"
            $writeDst = Join-Path $writeDirL $stagingName
            $finalDst = Join-Path $finalDirL $p.TifName

            if ((Test-Path -LiteralPath $finalDst) -and -not $overL -and ($finalDst -ne $p.Tiff)) {
                return @{ Result = "OK+SKIP-ZIP (exists) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff }
            }

            magick -quiet $tiffTarget -compress zip $writeDst 2>$null
            if ($LASTEXITCODE -ne 0) { return @{ Result = "ERROR (magick ZIP) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff } }

            exiftool -q -q -overwrite_original -tagsfromfile $tiffTarget -all:all -unsafe $writeDst | Out-Null
            if ($LASTEXITCODE -ne 0) { return @{ Result = "WARN (exiftool metadata copy failed, ZIP ok) | $($p.TifName)"; StagingName = $stagingName; OriginalName = $p.TifName; FinalDst = $finalDst; SrcPath = $p.Tiff } }
            return @{ Result = "OK+ZIP | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))"; StagingName = $stagingName; OriginalName = $p.TifName; FinalDst = $finalDst; SrcPath = $p.Tiff }
        }

        # Process results and build staging map
        $script:groupStagingMap = @{}
        foreach ($r in $results) {
            if ($r.StagingName) { $script:groupStagingMap[$r.SrcPath] = $r.StagingName }
            if ($r.MultiPagePath) { $script:multiPagePaths.Add($r.MultiPagePath) | Out-Null }
            Process-Line $r.Result
        }

        # Move from staging to final destination (with integrity check and UUID mapping)
        if ($CompressZip -and $StagingDir -and -not $DryRun) {
            $moved = 0
            foreach ($tif in $groupFiles) {
                if ($script:groupStagingMap.ContainsKey($tif.FullName)) {
                    $stagingName = $script:groupStagingMap[$tif.FullName]
                    $stagePath = Join-Path $StagingDir $stagingName
                } else {
                    $stagePath = Join-Path $StagingDir $tif.Name
                }
                $destPath  = Join-Path $finalDir   $tif.Name
                if ((Test-Path -LiteralPath $stagePath) -and $stagePath -ne $destPath) {
                    try {
                        $stageSize = (Get-Item -LiteralPath $stagePath).Length
                        Move-Item -Force -LiteralPath $stagePath -Destination $destPath -ErrorAction Stop
                        if ((Test-Path -LiteralPath $destPath) -and ((Get-Item -LiteralPath $destPath).Length -eq $stageSize)) {
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
            if ($moved -gt 0) { Write-Log "  → Moved $moved file(s) → $finalDir" }
        }
    }
}

# ── Entry point ─────────────────────────────────────────────────────
$inputDirs = if ($InputDir) { $InputDir -split ';' | ForEach-Object { $_.Trim() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } } else { @($PWD.Path) }
if ($inputDirs.Count -eq 0) { $inputDirs = @($PWD.Path) }

Write-Log "Log: $logFile"
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
                Write-Log "════ Processing: $($folder.FullName)"
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
Write-Log ("─" * 50)
Write-Log "Done: $($script:okTotal) OK | $($script:skipTotal) skipped | $($script:missTotal) no JPEG pair | $($script:multiTotal) multi-page | $($script:warnTotal) warnings | $($script:errTotal) errors | $($script:counterTotal)/$($script:total) processed"

if ($script:multiTotal -gt 0) {
    Write-Log ""
    Write-Log "── Multi-page TIFFs found (not touched):"
    foreach ($p in ($script:multiPagePaths | Sort-Object)) {
        Write-Log "   $p" "WARN"
    }
}
Write-Log "Log: $logFile"