# -- CLI PARAMETERS -----------------------------------------------
param(
    [int]$Mode = -1,                    # -1 = legacy mode (no CLI params = old behavior)

    [string]$InputDir = ".",

    # Output path control
    [string]$OutputDir = "",            # "" = in-place (modes 0/8), or flat root (mode 2)
    [string]$ZipSuffix = "_ZIP",        # Mode 4: folder rename suffix
    [string]$ZipSubfolderName = "ZIP",  # Mode 1/3: subfolder name
    [string]$ExportMarker = "_EXPORT",  # Modes 6/7
    [string]$ExportZipSubfolder = "ZIP",# Mode 6: subfolder inside _EXPORT
    [string]$ExportTiffSubfolder = "TIFF",# Mode 7: TIFF subfolder inside _EXPORT

    # Behavior
    [int]$Workers = 8,
    [switch]$DryRun,
    [bool]$SafeMode = $true,            # true = skip multi-page TIFFs
    [bool]$SkipLzwAsCompressed = $false, # true = treat LZW as already compressed
    [switch]$Overwrite,
    [switch]$DeleteSource,
    [switch]$ForceParallel,         # force parallelism ON (use if PS5 detected but pwsh available)
    [switch]$ForceSequential,       # force parallelism OFF (use if PS7 detected but want sequential)
    [string]$StagingDir = "",
    
    # Thumbnail generation
    [switch]$GenerateThumbnail,     # Generate embedded thumbnail in TIFF
    [int]$ThumbSize = 256,          # Thumbnail size in pixels
    [string]$ThumbQuality = "85",   # JPEG quality for thumbnail
    [string]$ThumbFormat = "jpg",   # Thumbnail format (jpg, png, etc.)
    [int]$ThumbPage = 1,            # Page number for thumbnail (0=first, 1=after main, etc.)
    [switch]$SkipCompressedWithThumb  # Skip TIFFs that are already compressed AND have thumbnail
)
# -----------------------------------------------------------------

# -- LEGACY SETTINGS (used when Mode = -1 / no CLI params) --------
# These are overridden by CLI params when Mode >= 0
$script:Workers    = $Workers
$script:DryRun     = $DryRun.IsPresent
$script:Recurse    = $true
$script:SafeMode   = $SafeMode
$script:SkipLzwAsCompressed = $SkipLzwAsCompressed
$script:OutputDir  = $OutputDir
$script:StagingDir = $StagingDir
$script:Overwrite  = $Overwrite.IsPresent
$script:Mode       = $Mode
$script:DeleteSource = $false
$script:MagickTimeout = 30
$script:GenerateThumbnail = $GenerateThumbnail.IsPresent
$script:ThumbSize = $ThumbSize
$script:ThumbQuality = $ThumbQuality
$script:ThumbPage = $ThumbPage
$script:SkipCompressedWithThumb = $SkipCompressedWithThumb.IsPresent

# -----------------------------------------------------------------

# -- Prerequisite checks -------------------------------------------
$missingTools = @()
if (-not (Get-Command exiftool -ErrorAction SilentlyContinue)) { $missingTools += "exiftool" }
if (-not (Get-Command magick -ErrorAction SilentlyContinue)) { $missingTools += "ImageMagick (magick)" }
if ($missingTools.Count -gt 0) {
    Write-Host "ERROR: Required tools not found in PATH: $($missingTools -join ', ')" -ForegroundColor Red
    Write-Host "Please install the missing tools and try again." -ForegroundColor Yellow
    exit 1
}

# -- Logging -------------------------------------------------------
$scriptName = "compress_tiff_zip"
$logDir     = Join-Path $PWD.Path "Logs\$scriptName"
[System.IO.Directory]::CreateDirectory($logDir) | Out-Null
$logFile    = Join-Path $logDir "$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

function Write-Log {
    param([string]$msg, [string]$level = "INFO")
    $line = "$(Get-Date -Format 'HH:mm:ss') | $level | $msg"
    if ($Host.Name -eq 'ConsoleHost' -or $Host.Name -eq 'Windows PowerShell ISE Host') {
        Write-Host $line
    } else {
        Write-Information $line
    }
    [System.IO.File]::AppendAllText($logFile, $line + [System.Environment]::NewLine)
}

$script:counterTotal   = 0
$script:okTotal        = 0
$script:skipTotal      = 0
$script:multiTotal     = 0
$script:warnTotal      = 0
$script:errTotal       = 0
$script:total          = 0
$script:multiPagePaths = [System.Collections.Concurrent.ConcurrentBag[string]]::new()

function Process-Results {
    param($lines)
    foreach ($line in $lines) {
    $script:counterTotal++
    $lvl = "INFO"
    if     ($line -match '^OK\+SKIP-ZIP') { $script:skipTotal++ }
    elseif ($line -match '^OK')           { $script:okTotal++ }
    elseif ($line -match '^SKIP')         { $script:skipTotal++ }
    elseif ($line -match '^MULTI')        { $script:multiTotal++; $lvl = "WARN" }
    elseif ($line -match '^ERROR')        { $script:errTotal++; $lvl = "ERROR" }
    elseif ($line -match '^WARN')         { $script:warnTotal++; $lvl = "WARN" }
    Write-Log "[$($script:counterTotal)/$($script:total)] $line" $lvl
}
}

# -- Cleanup on interrupt -----------------------------------------
$script:cleanupDirs   = @()
$script:cleanupFiles  = @()
if (-not [string]::IsNullOrWhiteSpace($StagingDir)) { $script:cleanupDirs += $StagingDir }

trap {
    Write-Log "Interrupted! Cleaning up staging files..." "WARN"
    foreach ($dir in $script:cleanupDirs) {
        if (Test-Path -LiteralPath $dir) {
            # Only remove files with UUID prefix (created by this script)
            Get-ChildItem -LiteralPath $dir | Where-Object { $_.Name -match '^[0-9a-f]{32}_' } | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
        }
    }
    foreach ($file in $script:cleanupFiles) {
        if (Test-Path -LiteralPath $file) {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        }
    }
    break
}

# Detect PowerShell version for parallel execution
# ONLY enable parallelism if actually running on PowerShell 7+
$script:PSMajor = $PSVersionTable.PSVersion.Major
$script:IS_PS7 = $script:PSMajor -ge 7

# On PS5, check if PS7 is installed for informational purposes only
if (-not $script:IS_PS7) {
    $pwshPath = $null
    $testPaths = @(
        "HKLM:\SOFTWARE\Microsoft\PowerShell\3\Paths",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\PowerShell\3\Paths",
        "HKCU:\SOFTWARE\Microsoft\PowerShell\3\Paths"
    )
    foreach ($regPath in $testPaths) {
        if (Test-Path $regPath) {
            $val = (Get-ItemProperty -LiteralPath $regPath -Name "pwsh" -ErrorAction SilentlyContinue).pwsh
            if ($val -and (Test-Path $val)) { $pwshPath = $val; break }
        }
    }
    if ($pwshPath) {
        Write-Host "INFO: PowerShell 7 detected at: $pwshPath" -ForegroundColor Yellow
        Write-Host "       Run with 'pwsh' instead of 'powershell' for parallel processing." -ForegroundColor Yellow
    }
}

# Override: force parallelism only if running on PS7+
if ($ForceParallel) {
    if ($script:PSMajor -ge 7) {
        $script:IS_PS7 = $true
    } else {
        Write-Log "WARNING: -ForceParallel ignored on PowerShell $($script:PSMajor). Parallel processing requires PowerShell 7+." "WARN"
    }
}
# Override: force sequential (disable parallelism even if PS7 detected)
if ($ForceSequential) {
    $script:IS_PS7 = $false
}

# Show detection on startup (only for new CLI modes, not legacy)
if ($Mode -ge 0) {
    $detectedLabel = if ($script:IS_PS7) { "PS$script:PSMajor (PARALLEL)" } else { "PS$script:PSMajor (SEQUENTIAL)" }
    $overrideNote  = if ($ForceSequential) { " [FORCED via -ForceSequential]" } elseif ($ForceParallel) { " [FORCED via -ForceParallel]" } else { "" }
    Write-Host "PowerShell: $detectedLabel$overrideNote"
}

# -- File Discovery per Mode --------------------------------------

function Get-Files-Mode0 {
    param([string]$root)
    Get-ChildItem -LiteralPath $root -File -Recurse:$false |
        Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
}

function Get-Files-Mode1 {
    param([string]$root)
    Get-ChildItem -LiteralPath $root -File -Recurse:$false |
        Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
}

function Get-Files-Mode2 {
    param([string]$root)
    Get-ChildItem -LiteralPath $root -File -Recurse:$true |
        Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
}

function Get-Files-Mode3 {
    param([string]$root)
    Get-ChildItem -LiteralPath $root -File -Recurse:$true |
        Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
}

function Get-Files-Mode4 {
    param([string]$root)
    Get-ChildItem -LiteralPath $root -File -Recurse:$true |
        Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
}

function Get-Files-Mode5 {
    param([string]$root)
    Get-ChildItem -LiteralPath $root -File -Recurse:$true |
        Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
}

function Get-Files-Mode6 {
    param([string]$root, [string]$marker)
    $markerLower = $marker.ToLowerInvariant()
    Get-ChildItem -LiteralPath $root -File -Recurse:$true |
        Where-Object {
            $_.Extension -match '^\.(tif|tiff)$' -and
            $_.DirectoryName -match "(?i)[\\/]$([regex]::Escape($marker))(?:[\\/]|$)"
        }
}

function Get-Files-Mode7 {
    param([string]$root, [string]$marker, [string]$tiffSubfolder)
    Get-ChildItem -LiteralPath $root -File -Recurse:$true |
        Where-Object {
            $_.Extension -match '^\.(tif|tiff)$' -and
            $_.DirectoryName -match "(?i)[\\/]$([regex]::Escape($marker))[\\/]$([regex]::Escape($tiffSubfolder))(?:[\\/]|$)"
        }
}

function Get-Files-Mode8 {
    param([string]$root)
    Get-ChildItem -LiteralPath $root -File -Recurse:$true |
        Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
}

function Get-Files-Mode9 {
    param([string]$root)
    Get-ChildItem -LiteralPath $root -File -Recurse:$true |
        Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
}

# -- Output Path Resolution ----------------------------------------

function Resolve-Output {
    param(
        [System.IO.FileInfo]$tiff,
        [int]$mode,
        [string]$inputRoot,
        [string]$outputDir,
        [string]$zipSuffix,
        [string]$zipSubfolderName,
        [string]$exportMarker,
        [string]$exportZipSubfolder,
        [bool]$overWrite = $false
    )
    $parent     = $tiff.DirectoryName
    $stem       = $tiff.BaseName
    $inputRootP  = $inputRoot.TrimEnd('/', '\')

    switch ($mode) {
        0 {
            return Join-Path $parent "$stem.tif"
        }
        1 {
            $subfolder = Join-Path $parent $zipSubfolderName
            return Join-Path $subfolder "$stem.tif"
        }
        2 {
            $root = if ($outputDir -and [System.IO.Path]::IsPathRooted($outputDir)) {
                $outputDir
            } elseif ($outputDir) {
                Join-Path $inputRootP $outputDir
            } else {
                $inputRootP
            }
            $candidate = Join-Path $root "$stem.tif"
            if ((Test-Path -LiteralPath $candidate) -and -not $overWrite) {
                $uniq = [guid]::NewGuid().ToString('N')[0..7] -join ''
                $candidate = Join-Path $root "${stem}_${uniq}.tif"
            }
            return $candidate
        }
        3 {
            $subfolder = Join-Path $parent $zipSubfolderName
            return Join-Path $subfolder "$stem.tif"
        }
        4 {
            $parentFolder = Split-Path $parent -Leaf
            $grandparent  = Split-Path $parent -Parent
            # Replace TIFF suffix with ZIP, handling _TIFF → _ZIP and TIFF → ZIP
            if ($parentFolder -match '(?i)_(TIFF)$') {
                $newFolderName = $parentFolder -replace '(?i)_(TIFF)$', "$zipSuffix"
            } elseif ($parentFolder -match '(?i)^(.*)(TIFF)$') {
                $newFolderName = $parentFolder -replace '(?i)(TIFF)$', $zipSuffix
            } else {
                $newFolderName = $parentFolder + "$zipSuffix"
            }
            $newParent    = Join-Path $grandparent $newFolderName
            return Join-Path $newParent "$stem.tif"
        }
        5 {
            $grandparent = Split-Path $parent -Parent
            if (-not $grandparent) { return $null }
            $zipFolder = Join-Path $grandparent $zipSubfolderName
            return Join-Path $zipFolder "$stem.tif"
        }
        6 {
            $parts = $parent -split '[\\/]'
            $exportIdx = -1
            for ($i = 0; $i -lt $parts.Count; $i++) {
                if ($parts[$i] -ieq $exportMarker) { $exportIdx = $i; break }
            }
            if ($exportIdx -lt 0) { return $null }
            $relParts = $parts[($exportIdx + 1)..($parts.Count - 1)]
            $relPath  = $relParts -join '/'
            $newParent = Join-Path (Join-Path $inputRootP $exportMarker) $exportZipSubfolder
            if ($relPath) { $newParent = Join-Path $newParent $relPath }
            return Join-Path $newParent "$stem.tif"
        }
        7 {
            $parts = $parent -split '[\\/]'
            $exportIdx = -1
            $tifIdx    = -1
            for ($i = 0; $i -lt $parts.Count; $i++) {
                if ($parts[$i] -ieq $exportMarker) { $exportIdx = $i }
                if ($parts[$i] -ieq $ExportTiffSubfolder) { $tifIdx = $i }
                if ($exportIdx -ge 0 -and $tifIdx -ge 0) { break }
            }
            if ($exportIdx -lt 0 -or $tifIdx -lt 0) { return $null }
            $relParts = $parts[($tifIdx + 1)..($parts.Count - 1)]
            $newParent = Join-Path (Join-Path $inputRootP $exportMarker) $exportZipSubfolder
            if ($relParts.Count -gt 0 -and $relParts[0]) {
                $relPath = $relParts -join '/'
                $newParent = Join-Path $newParent $relPath
            }
            return Join-Path $newParent "$stem.tif"
        }
        8 {
            return Join-Path $parent "$stem.tif"
        }
        9 {
            return Join-Path $parent "$stem.tif"
        }
        default { return $null }
    }
}

# -- Find Files for a given mode ------------------------------------

function Get-Files-ForMode {
    param([int]$mode, [string]$root)
    switch ($mode) {
        0 { return Get-Files-Mode0 $root }
        1 { return Get-Files-Mode1 $root }
        2 { return Get-Files-Mode2 $root }
        3 { return Get-Files-Mode3 $root }
        4 { return Get-Files-Mode4 $root }
        5 { return Get-Files-Mode5 $root }
        6 { return Get-Files-Mode6 $root $ExportMarker }
        7 { return Get-Files-Mode7 $root $ExportMarker $ExportTiffSubfolder }
        8 { return Get-Files-Mode8 $root }
        9 { return Get-Files-Mode9 $root }
        default { return @() }
    }
}

# -- Process one TIFF -> ZIP job ------------------------------------

function Process-TiffJob {
    param(
        [string]$srcPath,
        [string]$writeDst,
        [string]$finalDst,
        [bool]$dryRun,
        [bool]$overWrite,
        [bool]$safeMode,
        [bool]$skipLzw,
        [bool]$deleteSource,
        [int]$mode,
        [bool]$generateThumb = $false,
        [int]$thumbSize = 256,
        [string]$thumbQuality = "85",
        [int]$thumbPage = 1
    )

    $name = [System.IO.Path]::GetFileName($srcPath)

    $argComp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($argComp, "-s`n-s`n-s`n-Compression`n$srcPath`n")
        $comp = exiftool -@ $argComp 2>$null
        $exifExit = $LASTEXITCODE
    } finally {
        Remove-Item $argComp -Force -ErrorAction SilentlyContinue
    }
    if ($exifExit -ne 0 -or -not $comp) {
        return @{ Result = "ERROR (exiftool check) | $name | cannot detect compression"; StagingName = $null; OriginalName = $name }
    }
    if ($comp -match $(if ($skipLzw) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
        return @{ Result = "SKIP ($comp) | $name"; StagingName = $null; OriginalName = $name }
    }

    if ((Test-Path -LiteralPath $finalDst) -and -not $overWrite -and ($finalDst -ne $srcPath)) {
        return @{ Result = "SKIP (exists) | $name"; StagingName = $null; OriginalName = $name }
    }

    if ($safeMode) {
        $magickTimeoutSec = $script:MagickTimeout
        $srcCapture = $srcPath
        $pageCountJob = $null
        try {
            $pageCountJob = Start-Job { param($path) magick identify -format "%n" $path 2>$null } -ArgumentList $srcCapture
            $pageCountJob | Wait-Job -Timeout $magickTimeoutSec | Out-Null
            if ($pageCountJob.State -eq 'Running') {
                Stop-Job $pageCountJob
                return @{ Result = "ERROR (magick timeout) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name }
            }
            $pageCountStr = $pageCountJob | Receive-Job
            if ([string]::IsNullOrWhiteSpace($pageCountStr)) {
                return @{ Result = "ERROR (magick page count failed) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name }
            }
        } finally {
            if ($pageCountJob) {
                Remove-Job $pageCountJob -Force -ErrorAction SilentlyContinue
            }
        }
        $pageCountVal = if ($pageCountStr -is [array]) { $pageCountStr[0] } else { $pageCountStr }
        $pageCount = [int]$pageCountVal
        if ($pageCount -gt 1) {
            # Check if all extra pages are thumbnails (subfiletype=1)
            $hasOnlyThumbnails = $true
            $subfileTypes = magick identify -format "%[tiff:subfiletype]\n" "$srcPath" 2>$null
            if ($subfileTypes -is [array]) {
                for ($i = 1; $i -lt $subfileTypes.Count -and $i -lt $pageCount; $i++) {
                    if ($subfileTypes[$i] -ne "1") {
                        $hasOnlyThumbnails = $false
                        break
                    }
                }
            } else {
                $hasOnlyThumbnails = $false
            }
            if (-not $hasOnlyThumbnails) {
                $script:multiPagePaths.Add($srcPath) | Out-Null
                return @{ Result = "MULTI ($pageCount pages - skipped) | $name"; StagingName = $null; OriginalName = $name; MultiPagePath = $srcPath }
            }
            # If only thumbnails, continue processing page 0
        }
    }

    if ($dryRun) {
        return @{ Result = "DRY ($comp -> ZIP) | $name"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
    }

    # Build compression command
    if ($generateThumb) {
        # Compress page 0 only, then add thumbnail as page 1
        $mainPage = "$srcPath[$thumbPage]"
        $tempTiff = [System.IO.Path]::GetTempFileName() + ".tif"
        $thumbTemp = [System.IO.Path]::GetTempFileName() + ".jpg"
        
        try {
            # First: compress main image
            $out = magick -quiet $mainPage -compress zip $tempTiff 2>&1
            if ($LASTEXITCODE -ne 0) {
                return @{ Result = "ERROR (magick compress) | $name | $out"; StagingName = $null; OriginalName = $name }
            }
            
            # Generate thumbnail: convert to sRGB, strip ICC, resize
            $thumbResult = magick -quiet $mainPage -colorspace sRGB -strip -thumbnail "${thumbSize}x${thumbSize}>" -quality $thumbQuality $thumbTemp 2>&1
            if ($LASTEXITCODE -ne 0) {
                # If thumbnail fails, just copy the compressed TIFF
                Copy-Item -LiteralPath $tempTiff -Destination $writeDst -Force
                return @{ Result = "OK ($comp -> ZIP) [no thumb] | $name"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
            }
            
            # Combine: main TIFF + thumbnail as page 1
            $out = magick -quiet $tempTiff $thumbTemp -compress zip $writeDst 2>&1
            if ($LASTEXITCODE -ne 0) {
                # Fallback: just use compressed main
                Copy-Item -LiteralPath $tempTiff -Destination $writeDst -Force
                return @{ Result = "OK ($comp -> ZIP) [no thumb] | $name"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
            }
            
            # Mark second page as thumbnail (subfiletype=1)
            $argThumb = [System.IO.Path]::GetTempFileName()
            try {
                [System.IO.File]::WriteAllText($argThumb, "-q`n-q`n-overwrite_original`n-IFD1:SubfileType=ReducedResolution`n$writeDst`n")
                exiftool -@ $argThumb | Out-Null
            } finally {
                Remove-Item $argThumb -Force -ErrorAction SilentlyContinue
            }
        } finally {
            Remove-Item -LiteralPath $tempTiff -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $thumbTemp -Force -ErrorAction SilentlyContinue
        }
    } else {
        # Normal compression (all pages or page 0)
        $out = magick -quiet $srcPath -compress zip $writeDst 2>&1
        if ($LASTEXITCODE -ne 0) {
            return @{ Result = "ERROR (magick) | $name | $out"; StagingName = $null; OriginalName = $name }
        }
    }

    $stagingName = [System.IO.Path]::GetFileName($writeDst)
    $argExif = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($argExif, "-s`n-s`n-s`n-EXIF:Make`n$writeDst`n")
        $hasExif = exiftool -@ $argExif 2>$null
    } finally {
        Remove-Item $argExif -Force -ErrorAction SilentlyContinue
    }

    if (-not $hasExif) {
        $argCopy = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($argCopy, "-q`n-q`n-overwrite_original`n-tagsfromfile`n$srcPath`n-all:all`n-unsafe`n$writeDst`n")
            exiftool -@ $argCopy | Out-Null
        } finally {
            Remove-Item $argCopy -Force -ErrorAction SilentlyContinue
        }
        if ($LASTEXITCODE -ne 0) {
            return @{ Result = "WARN (exiftool failed, ZIP ok) | $name"; StagingName = $stagingName; OriginalName = $name; FinalDst = $finalDst }
        }
    }

    $deleted = $false
    if ($deleteSource -and $mode -eq 8) {
        $stagingUsed = ($writeDst -ne $srcPath)
        if ($stagingUsed) {
            if ((_Verify-ZipIntegrity $writeDst) -and (Test-Path -LiteralPath $srcPath)) {
                Remove-Item -LiteralPath $srcPath -Force
                $deleted = $true
            }
        } else {
            if ((_Verify-ZipIntegrity $srcPath) -and (Test-Path -LiteralPath $srcPath)) {
                $deleted = $true
            }
        }
    }

    return @{
        Result = "OK ($comp -> ZIP)$(if ($deleted) { ' [SOURCE DELETED]' } else { '' }) | $name"
        StagingName = $stagingName
        OriginalName = $name
        FinalDst = $finalDst
    }
}

function _Verify-ZipIntegrity {
    param([string]$path, [int]$timeoutSec = 30)
    try {
        # Return exit code explicitly from the job — Receive-Job does NOT propagate $LASTEXITCODE
        $job = Start-Job { param($p) magick convert "$p" null: 2>$null; $LASTEXITCODE } -ArgumentList $path
        $completed = $job | Wait-Job -Timeout $timeoutSec
        if (-not $completed) {
            Stop-Job $job -ErrorAction SilentlyContinue
            Remove-Job $job -Force
            return $false
        }
        $jobOutput = Receive-Job $job
        Remove-Job $job -Force
        # The last element is the explicit exit code we returned
        $zipExitCode = if ($jobOutput -is [array]) { $jobOutput[-1] } else { $jobOutput }
        return [int]$zipExitCode -eq 0
    } catch {
        return $false
    }
}

# -- LEGACY MODE (Mode = -1) --------------------------------------

if ($Mode -lt 0) {
    $root = $PWD.Path
    $files = Get-ChildItem -LiteralPath $root -File -Recurse:$script:Recurse |
             Where-Object { $_.Extension -match '^\.(tif|tiff)$' }

    $script:total = $files.Count
    Write-Log "Log: $logFile"

    if ($script:total -eq 0) {
        Write-Log "No TIFF files found in: $root" "WARN"
        Write-Log "Make sure you are in the right folder and that .tif or .tiff files exist"
    } else {
        $modeLabel = if ($script:SafeMode) { "SAFE (multi-page TIFFs will be skipped)" } else { "STANDARD (all TIFFs will be compressed)" }
        Write-Log "TIFFs: $($script:total) | Workers: $script:Workers | OutputDir: $(if ($script:OutputDir) { $script:OutputDir } else { '(overwrite in place)' }) | Staging: $(if ($script:StagingDir) { $script:StagingDir } else { 'disabled' }) | DryRun: $script:DryRun"
        Write-Log "Mode: $modeLabel"

        $groups = $files | Group-Object { $_.DirectoryName.ToLowerInvariant() }

        foreach ($group in $groups) {
            $groupDir   = $group.Name
            $groupFiles = $group.Group

            if ($groups.Count -gt 1) {
                Write-Log ""
                Write-Log "-- Group: $groupDir ($($groupFiles.Count) file(s))"
            }

            $finalDir = if ($script:OutputDir) {
                if ([System.IO.Path]::IsPathRooted($script:OutputDir)) { $script:OutputDir }
                else { Join-Path $groupDir $script:OutputDir }
            } else { $groupDir }

            $writeDir = if ($script:StagingDir -and -not $script:DryRun) { $script:StagingDir } else { $finalDir }

            if ($script:StagingDir -and -not $script:DryRun) { 
                if (Test-Path -LiteralPath $script:StagingDir -PathType Leaf) {
                    Write-Log "ERROR: StagingDir exists as a file: $($script:StagingDir)" "ERROR"
                    continue
                }
                [System.IO.Directory]::CreateDirectory($script:StagingDir) | Out-Null 
            }
            if ($script:OutputDir) { 
                if (Test-Path -LiteralPath $finalDir -PathType Leaf) {
                    Write-Log "ERROR: Output path exists as a file: $finalDir" "ERROR"
                    continue
                }
                [System.IO.Directory]::CreateDirectory($finalDir) | Out-Null 
            }

            $safeL        = $script:SafeMode
            $multiPageBag = $script:multiPagePaths
            $skipLzwL     = $script:SkipLzwAsCompressed
            $magickTimeout = $script:MagickTimeout

            $script:stagingMap = @{}
            $effectiveWorkersLegacy = if ($script:SafeMode) { [Math]::Min($script:Workers, 8) } else { $script:Workers }

            if ($script:IS_PS7) {
                $results = $groupFiles | ForEach-Object -Parallel {
                    $src       = $_.FullName
                    $name      = $_.Name
                    $writeDirL = $using:writeDir
                    $finalDirL = $using:finalDir
                    $dryL      = $using:DryRun
                    $overL     = $using:Overwrite
                    $safeMode  = $using:safeL
                    $bagL      = $using:multiPageBag
                    $skipLzw   = $using:skipLzwL
                    $magickTimeoutSec = $using:magickTimeout

                    $stem = [System.IO.Path]::GetFileNameWithoutExtension($name)
                    $ext = [System.IO.Path]::GetExtension($name)
                    if (-not $ext) { $ext = ".tif" }
                    $finalName = "${stem}${ext}"
                    $finalDst = Join-Path $finalDirL $finalName
                    # Only use staging name if staging dir is different from final dir
                    if ($writeDirL -ne $finalDirL) {
                        $stagingName = "$([guid]::NewGuid().ToString('N'))_${stem}${ext}"
                        $writeDst = Join-Path $writeDirL $stagingName
                    } else {
                        $stagingName = $finalName
                        $writeDst = $finalDst
                    }

                    $argComp = [System.IO.Path]::GetTempFileName()
                    try {
                        [System.IO.File]::WriteAllText($argComp, "-s`n-s`n-s`n-Compression`n$src`n")
                        $comp = exiftool -@ $argComp 2>$null
                        $exifExit = $LASTEXITCODE
                    } finally {
                        Remove-Item $argComp -Force -ErrorAction SilentlyContinue
                    }
                    if ($exifExit -ne 0 -or -not $comp) {
        return @{ Result = "ERROR (exiftool check) | $name | cannot detect compression"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
                    }
                    if ($comp -match $(if ($skipLzw) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
        return @{ Result = "SKIP ($comp) | $name"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
                    }

                    if ((Test-Path -LiteralPath $finalDst) -and -not $overL -and ($finalDst -ne $src)) {
        return @{ Result = "SKIP (exists) | $name"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
                    }

                    if ($safeMode) {
                        $srcCapture = $src
                        $pageCountJob = $null
                        try {
                            $pageCountJob = Start-Job { param($path) magick identify -format "%n" $path 2>$null } -ArgumentList $srcCapture
                            $pageCountJob | Wait-Job -Timeout $magickTimeoutSec | Out-Null
                            if ($pageCountJob.State -eq 'Running') {
                                Stop-Job $pageCountJob
                return @{ Result = "ERROR (magick timeout) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
                            }
                            $pageCountStr = $pageCountJob | Receive-Job
                        } finally {
                            if ($pageCountJob) {
                                Remove-Job $pageCountJob -Force -ErrorAction SilentlyContinue
                            }
                        }
                        if ([string]::IsNullOrWhiteSpace($pageCountStr)) {
            return @{ Result = "ERROR (magick page count failed) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
                        }
                        $pageCountVal = if ($pageCountStr -is [array]) { $pageCountStr[0] } else { $pageCountStr }
                        $pageCount = [int]$pageCountVal
                        if ($pageCount -gt 1) {
                            $hasOnlyThumbnails = $true
                            $subfileTypes = magick identify -format "%[tiff:subfiletype]\n" "$src" 2>$null
                            if ($subfileTypes -is [array]) {
                                for ($i = 1; $i -lt $subfileTypes.Count -and $i -lt $pageCount; $i++) {
                                    if ($subfileTypes[$i] -ne "1") {
                                        $hasOnlyThumbnails = $false
                                        break
                                    }
                                }
                            } else {
                                $hasOnlyThumbnails = $false
                            }
                            if (-not $hasOnlyThumbnails) {
                                return @{ Result = "MULTI ($pageCount IFDs - skipped) | $name"; StagingName = $null; OriginalName = $name; MultiPagePath = $src }
                            }
                        }
                    }

                    if ($dryL) { return @{ Result = "DRY ($comp -> ZIP) | $name"; StagingName = $null; OriginalName = $name } }

                    $out = magick -quiet $src -compress zip $writeDst 2>&1
                    if ($LASTEXITCODE -ne 0) { return @{ Result = "ERROR (magick) | $name | $out"; StagingName = $null; OriginalName = $name } }

                    $argExif = [System.IO.Path]::GetTempFileName()
                    try {
                        [System.IO.File]::WriteAllText($argExif, "-s`n-s`n-s`n-EXIF:Make`n$writeDst`n")
                        $hasExif = exiftool -@ $argExif 2>$null
                    } finally {
                        Remove-Item $argExif -Force -ErrorAction SilentlyContinue
                    }

                    if (-not $hasExif) {
                        $argCopy = [System.IO.Path]::GetTempFileName()
                        try {
                            [System.IO.File]::WriteAllText($argCopy, "-q`n-q`n-overwrite_original`n-tagsfromfile`n$src`n-all:all`n-unsafe`n$writeDst`n")
                            exiftool -@ $argCopy | Out-Null
                        } finally {
                            Remove-Item $argCopy -Force -ErrorAction SilentlyContinue
                        }
                        $stagingName = [System.IO.Path]::GetFileName($writeDst)
                        if ($LASTEXITCODE -ne 0) { return @{ Result = "WARN (exiftool failed, ZIP ok) | $name"; StagingName = $stagingName; OriginalName = $name; FinalDst = $finalDst } }
                    }

                    return @{ Result = "OK ($comp -> ZIP) | $name"; StagingName = $stagingName; OriginalName = $name; FinalDst = $finalDst }

                } -ThrottleLimit $effectiveWorkersLegacy
                foreach ($r in $results) {
                    if ($r.StagingName) { $script:stagingMap[$r.FinalDst.ToLowerInvariant()] = @{ StagingName = $r.StagingName; FinalDst = $r.FinalDst } }
                    if ($r.MultiPagePath) { $script:multiPagePaths.Add($r.MultiPagePath) | Out-Null }
                    Process-Results @($r.Result)
                }
            } else {
                # PS5.1: sequential processing for legacy mode
                foreach ($f in $groupFiles) {
                    $tifName = "$([System.IO.Path]::GetFileNameWithoutExtension($f.Name)).tif"
                    $result = Process-TiffJob $f.FullName $(Join-Path $writeDir $tifName) $(Join-Path $finalDir $tifName) `
                                $script:DryRun $script:Overwrite $script:SafeMode `
                                $script:SkipLzwAsCompressed $script:DeleteSource $script:Mode
                    if ($result.StagingName) { $script:stagingMap[$result.FinalDst.ToLowerInvariant()] = @{ StagingName = $result.StagingName; FinalDst = $result.FinalDst } }
                    Process-Results @($result.Result)
                }
            }

            if ($script:StagingDir -and -not $script:DryRun) {
                $moved = 0
                foreach ($f in $groupFiles) {
                    # Resolve-Output always returns .tif extension, but $f.Name may be .tiff
                    $expectedName = "$([System.IO.Path]::GetFileNameWithoutExtension($f.Name)).tif"
                    $destPath = Join-Path $finalDir $expectedName
                    $destKey = $destPath.ToLowerInvariant()
                    if (-not $script:stagingMap.ContainsKey($destKey)) { continue }
                    $stagingName = $script:stagingMap[$destKey].StagingName
                    $stagePath = Join-Path $script:StagingDir $stagingName
                    if ((Test-Path -LiteralPath $stagePath) -and $stagePath -ne $destPath) {
                        $stageSize = (Get-Item -LiteralPath $stagePath).Length
                        try {
                            Move-Item -Force -LiteralPath $stagePath -Destination $destPath -ErrorAction Stop
                            if (Test-Path -LiteralPath $destPath) {
                                $destSize = (Get-Item -LiteralPath $destPath).Length
                                if ($destSize -eq $stageSize) {
                                    $moved++
                                } else {
                                    $script:errTotal++
                                    Write-Log "ERROR (size mismatch after move) | $($f.Name)" "ERROR"
                                }
                            } else {
                                $script:errTotal++
                                Write-Log "ERROR (move failed) | $($f.Name)" "ERROR"
                            }
                        } catch {
                            $script:errTotal++
                            $errMsg = if ($_.Exception -and $_.Exception.Message) { $_.Exception.Message } else { $_.ToString() }
                            Write-Log "ERROR (move exception) | $($f.Name): $errMsg" "ERROR"
                        }
                    }
                }
                if ($moved -gt 0) { Write-Log "  -> Moved $moved file(s) -> $finalDir" }
            }
        }

        Write-Log ""
        Write-Log ("-" * 50)
        if ($script:SafeMode) {
            Write-Log "Done: $($script:okTotal) OK | $($script:skipTotal) skipped | $($script:multiTotal) multi-page (not touched) | $($script:warnTotal) warnings | $($script:errTotal) errors | $($script:counterTotal)/$($script:total) processed"
        } else {
            Write-Log "Done: $($script:okTotal) OK | $($script:skipTotal) skipped | $($script:errTotal) errors | $($script:counterTotal)/$($script:total) processed"
        }

        if ($script:multiTotal -gt 0) {
            Write-Log ""
            Write-Log "-- Multi-page TIFFs found (not compressed - review manually):"
            foreach ($p in ($script:multiPagePaths | Sort-Object)) {
                Write-Log "   $p" "WARN"
            }
        }
    }

    Write-Log "Log: $logFile"
    return
}

# -- NEW MODE (Mode >= 0) -----------------------------------------

# Support multiple input directories separated by semicolon (from wizard AutoFind)
$inputRoots = @()
foreach ($dir in ($InputDir -split ';')) {
    $dir = $dir.Trim()
    if ([string]::IsNullOrWhiteSpace($dir)) { continue }
    if (-not [System.IO.Path]::IsPathRooted($dir)) {
        $dir = Join-Path $PWD.Path $dir
    }
    $inputRoots += $dir
}

if ($inputRoots.Count -eq 0) {
    Write-Log "No valid input directories specified." "WARN"
    Write-Log "Log: $logFile"
    return
}

$script:total = 0
$script:counterTotal = 0
$script:okTotal = 0
$script:skipTotal = 0
$script:multiTotal = 0
$script:errTotal = 0

Write-Log "Log: $logFile"

$deleteLabel = if ($DeleteSource) { "ON" } else { "OFF" }
Write-Log "Mode: $Mode | Workers: $Workers | OutputDir: $(if ($OutputDir) { $OutputDir } else { '(in-place/flat)' }) | Staging: $(if ($StagingDir) { $StagingDir } else { 'disabled' }) | DryRun: $DryRun | SafeMode: $SafeMode | SkipLzw: $SkipLzwAsCompressed | Overwrite: $($Overwrite.IsPresent) | DeleteSource: $deleteLabel"

# Collect files from all input directories
$allFiles = @()
foreach ($inputRoot in $inputRoots) {
    $dirFiles = @(Get-Files-ForMode $Mode $inputRoot)
    $allFiles += $dirFiles
    if ($inputRoots.Count -gt 1 -and $dirFiles.Count -gt 0) {
        Write-Log "  Found $($dirFiles.Count) TIFF(s) in: $inputRoot"
    }
}

$files = $allFiles
$script:total = $files.Count

if ($script:total -eq 0) {
    Write-Log "No TIFF files found for mode $Mode in: $($inputRoots -join '; ')" "WARN"
    Write-Log "Log: $logFile"
    return
}

# Count TIFFs inside OLD_TIFFs folders (already processed, will be skipped)
$oldTiffsCount = ($files | Where-Object { $_.DirectoryName -match '(?i)[\\/]OLD_TIFFS?[\\/]|[\\/]OLD_TIFFS?$' }).Count
$oldTiffsNote = if ($oldTiffsCount -gt 0) { " ($oldTiffsCount in OLD_TIFFs)" } else { "" }
Write-Log "Found: $($script:total) TIFF(s)$oldTiffsNote"

# Build (src, writeDst, finalDst) tasks grouped by output directory
$tasks = @()
$usedNames = @{}  # Track allocated names per output directory to avoid collisions
foreach ($f in $files) {
    # Use the file's directory as the input root for path resolution
    $fileInputRoot = $f.DirectoryName
    $finalDst = Resolve-Output $f $Mode $fileInputRoot $OutputDir $ZipSuffix $ZipSubfolderName $ExportMarker $ExportZipSubfolder $Overwrite.IsPresent
    if (-not $finalDst) { continue }

    # Mode 2: Detect name collisions across different source folders
    if ($Mode -eq 2) {
        $destDir = [System.IO.Path]::GetDirectoryName($finalDst)
        $destName = [System.IO.Path]::GetFileName($finalDst)
        if (-not $usedNames.ContainsKey($destDir)) {
            $usedNames[$destDir] = @()
        }
        if ($destName -in $usedNames[$destDir]) {
            # Collision: append unique suffix
            $uniq = [guid]::NewGuid().ToString('N').Substring(0, 8)
            $stem = [System.IO.Path]::GetFileNameWithoutExtension($destName)
            $ext = [System.IO.Path]::GetExtension($destName)
            $destName = "${stem}_${uniq}${ext}"
            $finalDst = Join-Path $destDir $destName
        }
        $usedNames[$destDir] += $destName
    }

    $writeDst = if ($StagingDir -and -not $DryRun) {
        if (Test-Path -LiteralPath $StagingDir -PathType Leaf) {
            Write-Log "ERROR: StagingDir exists as a file: $StagingDir" "ERROR"
            $script:errTotal++
            $script:counterTotal++
            continue
        }
        $stagingName = "$([guid]::NewGuid().ToString('N'))_$($f.Name)"
        if (-not (Test-Path -LiteralPath $StagingDir)) {
            [System.IO.Directory]::CreateDirectory($StagingDir) | Out-Null
        }
        Join-Path $StagingDir $stagingName
    } else {
        $finalDst
    }

    # Mode 0 and 9: pre-check compression and move source to OLD_TIFFs/ before queuing
    # This way skipped files (Deflate/ZIP) are NOT moved
    if ($Mode -eq 0 -or $Mode -eq 9) {
        # Skip OLD_TIFFs folder itself (it's where we move originals to)
        if ($f.DirectoryName -match '(?i)[\\/]OLD_TIFFS?[\\/]|[\\/]OLD_TIFFS?$') {
            $script:skipTotal++
            continue
        }
        $argComp = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($argComp, "-s`n-s`n-s`n-Compression`n$($f.FullName)`n")
            $comp = exiftool -@ $argComp 2>$null
            $exifExit = $LASTEXITCODE
        } finally {
            Remove-Item $argComp -Force -ErrorAction SilentlyContinue
        }
        if ($exifExit -ne 0 -or -not $comp) {
            $script:errTotal++
            $script:counterTotal++
            Write-Log "ERROR (exiftool check) | $($f.Name) | cannot detect compression" "ERROR"
            continue
        }
        if ($comp -match $(if ($SkipLzwAsCompressed) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
            $script:skipTotal++
            $script:counterTotal++
            Write-Log "SKIP ($comp) | $($f.Name)"
            continue
        }
        # In dry-run, do not move -- just queue with original path
        if (-not $DryRun) {
            $oldTiffDir = Join-Path $f.DirectoryName "OLD_TIFFs"
            if (-not (Test-Path -LiteralPath $oldTiffDir)) {
                [System.IO.Directory]::CreateDirectory($oldTiffDir) | Out-Null
            }
            $oldSrc = Join-Path $oldTiffDir $f.Name
            if (Test-Path -LiteralPath $oldSrc) {
                $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
                $counter = 1
                $baseOldSrc = Join-Path $oldTiffDir "$($f.BaseName)_$timestamp"
                $oldSrc = "$baseOldSrc$($f.Extension)"
                while (Test-Path -LiteralPath $oldSrc) {
                    $oldSrc = "${baseOldSrc}_${counter}$($f.Extension)"
                    $counter++
                }
            }
            Move-Item -LiteralPath $f.FullName -Destination $oldSrc -Force
            if (-not (Test-Path -LiteralPath $oldSrc)) {
                $script:errTotal++
                $script:counterTotal++
                Write-Log "ERROR (move to OLD_TIFFs failed) | $($f.Name)" "ERROR"
                continue
            }
            $tasks += @{
                Src      = $oldSrc
                WriteDst = $writeDst
                FinalDst = $finalDst
                Name     = $f.Name
                MovedBy  = "mode0"
                OldTiffBackup = $f.FullName
                GenerateThumb = $script:GenerateThumbnail
                ThumbSize = $script:ThumbSize
                ThumbQuality = $script:ThumbQuality
                ThumbPage = $script:ThumbPage
            }
        } else {
            # Dry-run: don't move, task reads from original path
            $tasks += @{
                Src      = $f.FullName
                WriteDst = $writeDst
                FinalDst = $finalDst
                Name     = $f.Name
                GenerateThumb = $script:GenerateThumbnail
                ThumbSize = $script:ThumbSize
                ThumbQuality = $script:ThumbQuality
                ThumbPage = $script:ThumbPage
            }
        }
    } else {
        $tasks += @{
            Src      = $f.FullName
            WriteDst = $writeDst
            FinalDst = $finalDst
            Name     = $f.Name
            GenerateThumb = $script:GenerateThumbnail
            ThumbSize = $script:ThumbSize
            ThumbQuality = $script:ThumbQuality
            ThumbPage = $script:ThumbPage
        }
    }
}

if ($tasks.Count -eq 0) {
    Write-Log "No tasks to process (mode $Mode may have filtered all files)" "WARN"
    Write-Log "Log: $logFile"
    return
}

$groupedTasks = $tasks | Group-Object {
    $dir = [System.IO.Path]::GetDirectoryName($_.FinalDst)
    $parent = Split-Path $dir
    if ([string]::IsNullOrWhiteSpace($parent)) {
        $dir.ToLowerInvariant()
    } else {
        $parent.ToLowerInvariant()
    }
}

foreach ($group in $groupedTasks) {
    $groupDir = $group.Name
    $groupTasks = $group.Group

    if ($groupedTasks.Count -gt 1) {
        Write-Log ""
        Write-Log "-- Group: $groupDir ($($groupTasks.Count) file(s))"
    }

    if (-not (Test-Path -LiteralPath $groupDir)) {
        [System.IO.Directory]::CreateDirectory($groupDir) | Out-Null
    }

    $script:stagingMap = @{}
    $effectiveWorkers = if ($SafeMode) { [Math]::Min($Workers, 8) } else { $Workers }
    if ($SafeMode -and $Workers -gt 8) {
        Write-Log "Workers capped to $effectiveWorkers (SafeMode limit)"
    }

    # Enable parallel processing for all modes including thumbnail generation
    $useParallel = $script:IS_PS7
    
    if ($useParallel) {
        # PS7+: use parallel threads, collect results then process sequentially
        $parallelResults = $groupTasks | ForEach-Object -Parallel {
            $t = $_
            $srcPath = $t.Src
            $writeDst = $t.WriteDst
            $finalDst = $t.FinalDst
            $dryRun = $using:DryRun
            $overWrite = $using:Overwrite
            $safeMode = $using:SafeMode
            $skipLzw = $using:SkipLzwAsCompressed
            $deleteSource = $using:DeleteSource
            $mode = $using:Mode
            $magickTimeout = $using:MagickTimeout
            $generateThumb = $using:GenerateThumbnail
            $thumbSize = $using:ThumbSize
            $thumbQuality = $using:ThumbQuality
            $thumbPage = $using:ThumbPage

            $name = [System.IO.Path]::GetFileName($srcPath)

            $argComp = [System.IO.Path]::GetTempFileName()
            try {
                [System.IO.File]::WriteAllText($argComp, "-s`n-s`n-s`n-Compression`n$srcPath`n")
                $comp = exiftool -@ $argComp 2>$null
                $exifExit = $LASTEXITCODE
            } finally {
                Remove-Item $argComp -Force -ErrorAction SilentlyContinue
            }
            if ($exifExit -ne 0 -or -not $comp) {
                return @{ Result = "ERROR (exiftool check) | $name | cannot detect compression"; StagingName = $null; OriginalName = $name }
            }
            if ($comp -match $(if ($skipLzw) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
                return @{ Result = "SKIP ($comp) | $name"; StagingName = $null; OriginalName = $name }
            }
            if ((Test-Path -LiteralPath $finalDst) -and -not $overWrite -and ($finalDst -ne $srcPath)) {
                return @{ Result = "SKIP (exists) | $name"; StagingName = $null; OriginalName = $name }
            }
            if ($safeMode) {
                $srcCapture = $srcPath
                $pageCountJob = $null
                try {
                    $pageCountJob = Start-Job { param($path) magick identify -format "%n" $path 2>$null } -ArgumentList $srcCapture
                    $pageCountJob | Wait-Job -Timeout $magickTimeout | Out-Null
                    if ($pageCountJob.State -eq 'Running') {
                        Stop-Job $pageCountJob
                        return @{ Result = "ERROR (magick timeout) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name }
                    }
                    $pageCountStr = $pageCountJob | Receive-Job
                    if ([string]::IsNullOrWhiteSpace($pageCountStr)) {
                        return @{ Result = "ERROR (magick page count failed) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name }
                    }
                } finally {
                    if ($pageCountJob) {
                        Remove-Job $pageCountJob -Force -ErrorAction SilentlyContinue
                    }
                }
                $pageCountVal = if ($pageCountStr -is [array]) { $pageCountStr[0] } else { $pageCountStr }
                $pageCount = [int]$pageCountVal
                if ($pageCount -gt 1) {
                    # Check if all extra pages are thumbnails (subfiletype=1)
                    $hasOnlyThumbnails = $true
                    $subfileTypes = magick identify -format "%[tiff:subfiletype]\n" "$srcPath" 2>$null
                    if ($subfileTypes -is [array]) {
                        for ($i = 1; $i -lt $subfileTypes.Count -and $i -lt $pageCount; $i++) {
                            if ($subfileTypes[$i] -ne "1") {
                                $hasOnlyThumbnails = $false
                                break
                            }
                        }
                    } else {
                        $hasOnlyThumbnails = $false
                    }
                    if (-not $hasOnlyThumbnails) {
                        return @{ Result = "MULTI ($pageCount IFDs - skipped) | $name"; StagingName = $null; OriginalName = $name; MultiPagePath = $srcPath }
                    }
                    # If only thumbnails, continue processing page 0
                }
            }
            if ($dryRun) {
                return @{ Result = "DRY ($comp -> ZIP) | $name"; StagingName = $null; OriginalName = $name }
            }
            
            if ($generateThumb) {
                # Compress page 0 only, then add thumbnail as page 1
                $mainPage = "$srcPath[$thumbPage]"
                $tempTiff = [System.IO.Path]::GetTempFileName() + ".tif"
                $thumbTemp = [System.IO.Path]::GetTempFileName() + ".jpg"
                try {
                    $out = magick -quiet $mainPage -compress zip $tempTiff 2>&1
                    if ($LASTEXITCODE -ne 0) {
                        return @{ Result = "ERROR (magick compress) | $name | $out"; StagingName = $null; OriginalName = $name }
                    }
                    $thumbResult = magick -quiet $mainPage -colorspace sRGB -strip -thumbnail "${thumbSize}x${thumbSize}>" -quality $thumbQuality $thumbTemp 2>&1
                    if ($LASTEXITCODE -ne 0) {
                        Copy-Item -LiteralPath $tempTiff -Destination $writeDst -Force
                        return @{ Result = "OK ($comp -> ZIP) [no thumb] | $name"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
                    }
                    $out = magick -quiet $tempTiff $thumbTemp -compress zip $writeDst 2>&1
                    if ($LASTEXITCODE -ne 0) {
                        Copy-Item -LiteralPath $tempTiff -Destination $writeDst -Force
                        return @{ Result = "OK ($comp -> ZIP) [no thumb] | $name"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
                    }
                    $argThumb = [System.IO.Path]::GetTempFileName()
                    try {
                        [System.IO.File]::WriteAllText($argThumb, "-q`n-q`n-overwrite_original`n-IFD1:SubfileType=ReducedResolution`n$writeDst`n")
                        exiftool -@ $argThumb | Out-Null
                    } finally {
                        Remove-Item $argThumb -Force -ErrorAction SilentlyContinue
                    }
                } finally {
                    Remove-Item -LiteralPath $tempTiff -Force -ErrorAction SilentlyContinue
                    Remove-Item -LiteralPath $thumbTemp -Force -ErrorAction SilentlyContinue
                }
            } else {
                # Normal compression (all pages or page 0)
                $out = magick -quiet $srcPath -compress zip $writeDst 2>&1
                if ($LASTEXITCODE -ne 0) {
                    return @{ Result = "ERROR (magick) | $name | $out"; StagingName = $null; OriginalName = $name; FinalDst = $finalDst }
                }
            }
            $stagingName = [System.IO.Path]::GetFileName($writeDst)
            $argExif = [System.IO.Path]::GetTempFileName()
            try {
                [System.IO.File]::WriteAllText($argExif, "-s`n-s`n-s`n-EXIF:Make`n$writeDst`n")
                $hasExif = exiftool -@ $argExif 2>$null
            } finally {
                Remove-Item $argExif -Force -ErrorAction SilentlyContinue
            }
            if (-not $hasExif) {
                $argCopy = [System.IO.Path]::GetTempFileName()
                try {
                    [System.IO.File]::WriteAllText($argCopy, "-q`n-q`n-overwrite_original`n-tagsfromfile`n$srcPath`n-all:all`n-unsafe`n$writeDst`n")
                    exiftool -@ $argCopy | Out-Null
                } finally {
                    Remove-Item $argCopy -Force -ErrorAction SilentlyContinue
                }
                $stagingName = [System.IO.Path]::GetFileName($writeDst)
                if ($LASTEXITCODE -ne 0) {
                    return @{ Result = "WARN (exiftool failed, ZIP ok) | $name"; StagingName = $stagingName; OriginalName = $name; FinalDst = $finalDst }
                }
            }
            $deleted = $false
            if ($deleteSource -and $mode -eq 8) {
                # Inline ZIP integrity check (functions are not accessible in -Parallel scope)
                $verifyPath = if ($writeDst -ne $srcPath) { $writeDst } else { $srcPath }
                $integrityJob = Start-Job { param($p) magick "$p" null: 2>$null; $LASTEXITCODE } -ArgumentList $verifyPath
                $integrityJob | Wait-Job -Timeout $magickTimeout | Out-Null
                $integrityOk = $false
                if ($integrityJob.State -ne 'Running') {
                    $integrityOutput = Receive-Job $integrityJob
                    $integrityExit = if ($integrityOutput -is [array]) { $integrityOutput[-1] } else { $integrityOutput }
                    $integrityOk = [int]$integrityExit -eq 0
                }
                Remove-Job $integrityJob -Force -ErrorAction SilentlyContinue
                if ($integrityOk) {
                    if ($writeDst -ne $srcPath -and (Test-Path -LiteralPath $srcPath)) {
                        # Staging was used: srcPath is the original, writeDst is the ZIP
                        Remove-Item -LiteralPath $srcPath -Force
                        $deleted = $true
                    } elseif ($writeDst -eq $srcPath) {
                        # No staging: magick already compressed in-place, nothing to delete
                        $deleted = $true
                    }
                }
            }
            return @{
                Result = "OK ($comp -> ZIP)$(if ($deleted) { ' [SOURCE DELETED]' } else { '' }) | $name"
                StagingName = $stagingName
                OriginalName = $name
                FinalDst = $finalDst
            }
        } -ThrottleLimit $effectiveWorkers

        # Process results sequentially (avoids scope issues with Process-Results)
        # Use FinalDst as key to handle duplicate filenames across different folders
        $errBefore = $script:errTotal
        foreach ($r in $parallelResults) {
            if ($r.StagingName) { $script:stagingMap[$r.FinalDst.ToLowerInvariant()] = @{ StagingName = $r.StagingName; FinalDst = $r.FinalDst } }
            if ($r.MultiPagePath) { $script:multiPagePaths.Add($r.MultiPagePath) | Out-Null }
            Process-Results @($r.Result)
        }
        $groupErrs = $script:errTotal - $errBefore
        # Rollback: if errors occurred on files moved to OLD_TIFFs, restore originals
        if ($groupErrs -gt 0) {
            $failedCount = 0
            foreach ($t in $groupTasks) {
                if ($t.MovedBy -eq "mode0" -and $t.OldTiffBackup) {
                    # Only rollback if the ZIP wasn't created successfully
                    if (Test-Path -LiteralPath $t.FinalDst) {
                        Write-Log "SKIP rollback (ZIP exists) | $($t.Name)" "INFO"
                        continue
                    }
                    if (Test-Path -LiteralPath $t.Src) {
                        try {
                            $origDst = $t.OldTiffBackup
                            if (-not (Test-Path -LiteralPath (Split-Path $origDst -Parent))) {
                                [System.IO.Directory]::CreateDirectory((Split-Path $origDst -Parent)) | Out-Null
                            }
                            Move-Item -LiteralPath $t.Src -Destination $origDst -Force -ErrorAction Stop
                            Write-Log "ROLLBACK (restored from OLD_TIFFs) | $($t.Name)" "WARN"
                            $failedCount++
                        } catch {
                            $script:errTotal++
                            Write-Log "ROLLBACK FAILED | $($t.Name): $($_.Exception.Message)" "ERROR"
                        }
                    }
                }
            }
            if ($failedCount -gt 0) { Write-Log "  -> Rolled back $failedCount file(s) from OLD_TIFFs" "WARN" }
        }
    } else {
        # PS5.1: sequential (no -Parallel support)
        $errBefore = $script:errTotal
        foreach ($t in $groupTasks) {
            $result = Process-TiffJob $t.Src $t.WriteDst $t.FinalDst `
                                $DryRun $Overwrite $SafeMode `
                                $SkipLzwAsCompressed $DeleteSource $Mode `
                                $script:GenerateThumbnail $script:ThumbSize $script:ThumbQuality $script:ThumbPage
            if ($result.StagingName) { $script:stagingMap[$t.FinalDst.ToLowerInvariant()] = @{ StagingName = $result.StagingName; FinalDst = $t.FinalDst } }
            Process-Results @($result.Result)
        }
        $groupErrs = $script:errTotal - $errBefore
        # Rollback for PS5
        if ($groupErrs -gt 0) {
            $failedCount = 0
            foreach ($t in $groupTasks) {
                if ($t.MovedBy -eq "mode0" -and $t.OldTiffBackup) {
                    # Only rollback if the ZIP wasn't created successfully
                    if (Test-Path -LiteralPath $t.FinalDst) {
                        Write-Log "SKIP rollback (ZIP exists) | $($t.Name)" "INFO"
                        continue
                    }
                    if (Test-Path -LiteralPath $t.Src) {
                        try {
                            $origDst = $t.OldTiffBackup
                            if (-not (Test-Path -LiteralPath (Split-Path $origDst -Parent))) {
                                [System.IO.Directory]::CreateDirectory((Split-Path $origDst -Parent)) | Out-Null
                            }
                            Move-Item -LiteralPath $t.Src -Destination $origDst -Force -ErrorAction Stop
                            Write-Log "ROLLBACK (restored from OLD_TIFFs) | $($t.Name)" "WARN"
                            $failedCount++
                        } catch {
                            $script:errTotal++
                            $errMsg = if ($_.Exception -and $_.Exception.Message) { $_.Exception.Message } else { $_.ToString() }
                            Write-Log "ROLLBACK FAILED | $($t.Name): $errMsg" "ERROR"
                        }
                    }
                }
            }
            if ($failedCount -gt 0) { Write-Log "  -> Rolled back $failedCount file(s) from OLD_TIFFs" "WARN" }
        }
    }

    if ($StagingDir -and -not $DryRun) {
        $moved = 0
        foreach ($t in $groupTasks) {
            $key = $t.FinalDst.ToLowerInvariant()
            if (-not $script:stagingMap.ContainsKey($key)) { continue }
            $stagingName = $script:stagingMap[$key].StagingName
            $stagePath = Join-Path $StagingDir $stagingName
            $destPath  = $t.FinalDst

            if ((Test-Path -LiteralPath $stagePath) -and $stagePath -ne $destPath) {
                try {
                    $stageSize = (Get-Item -LiteralPath $stagePath).Length
                    Move-Item -Force -LiteralPath $stagePath -Destination $destPath -ErrorAction Stop
                    if (Test-Path -LiteralPath $destPath) {
                        $destSize = (Get-Item -LiteralPath $destPath).Length
                        if ($destSize -eq $stageSize) {
                            $moved++
                        } else {
                            $script:errTotal++
                            Write-Log "ERROR (size mismatch after move) | $([System.IO.Path]::GetFileName($destPath))" "ERROR"
                        }
                    } else {
                        $script:errTotal++
                        Write-Log "ERROR (move failed) | $([System.IO.Path]::GetFileName($destPath))" "ERROR"
                    }
                } catch {
                    $script:errTotal++
                    $errMsg = if ($_.Exception -and $_.Exception.Message) { $_.Exception.Message } else { $_.ToString() }
                    Write-Log "ERROR (move exception) | $([System.IO.Path]::GetFileName($destPath)): $errMsg" "ERROR"
                }
            }
        }
        if ($moved -gt 0) { Write-Log "  -> Moved $moved file(s) -> $groupDir" }
    }
}

Write-Log ""
Write-Log ("-" * 50)
Write-Log "Done: $($script:okTotal) OK | $($script:skipTotal) skipped | $($script:multiTotal) multi-page (not touched) | $($script:warnTotal) warnings | $($script:errTotal) errors | $($script:counterTotal)/$($script:total) processed"

if ($script:multiTotal -gt 0) {
    Write-Log ""
    Write-Log "-- Multi-page TIFFs found (not compressed - review manually):"
    foreach ($p in ($script:multiPagePaths | Sort-Object)) {
        Write-Log "   $p" "WARN"
    }
}

Write-Log "Log: $logFile"
