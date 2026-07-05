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
    [ValidateSet("Skip", "Numbered", "Overwrite")][string]$DuplicateAction = "Numbered",
    [int]$MagickTimeout = 30,

    # Thumbnail generation
    [switch]$GenerateThumbnail,     # Generate embedded thumbnail in TIFF
    [int]$ThumbSize = 256,          # Thumbnail size in pixels
    [string]$ThumbQuality = "85",   # JPEG quality for thumbnail
    [string]$ThumbFormat = "jpg",   # Thumbnail format (jpg, png, etc.)
    [int]$ThumbPage = 1,            # Output page position for the embedded thumbnail (0=first, 1=after main, etc.)
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
$script:DeleteSource = $DeleteSource.IsPresent
$script:MagickTimeout = $MagickTimeout
$script:GenerateThumbnail = $GenerateThumbnail.IsPresent
$script:ThumbSize = $ThumbSize
$script:ThumbQuality = $ThumbQuality
$script:ThumbPage = $ThumbPage
$script:SkipCompressedWithThumb = $SkipCompressedWithThumb.IsPresent
$script:DuplicateAction = $DuplicateAction

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
            return Join-Path $root "$stem.tif"
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
            $relParts = if (($exportIdx + 1) -le ($parts.Count - 1)) { $parts[($exportIdx + 1)..($parts.Count - 1)] } else { @() }
            $newParent = Join-Path (Join-Path $inputRootP $exportMarker) $exportZipSubfolder
            if ($relParts.Count -gt 0 -and $relParts[0]) {
                $relPath = $relParts -join '/'
                $newParent = Join-Path $newParent $relPath
            }
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
            $relParts = if (($tifIdx + 1) -le ($parts.Count - 1)) { $parts[($tifIdx + 1)..($parts.Count - 1)] } else { @() }
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

        Inside ForEach-Object -Parallel runspaces, functions defined in the parent script are not
        visible. Re-inject the function at the top of each parallel block with:
            ${function:Test-TiffHasOnlySubfilePages} = $using:TestSubfileFnDef
    #>
    param(
        [string]$Path,
        [int]$PageCount,
        [string[]]$AllowedSubfileTypes = @("REDUCEDIMAGE", "REDUCED", "MASK", "PAGE")
    )

    if ($PageCount -le 1) { return $true }

    $subfileTypes = magick identify -format "%[tiff:subfiletype]\n" "$Path" 2>$null
    if (-not ($subfileTypes -is [array])) { return $false }

    for ($i = 1; $i -lt $subfileTypes.Count -and $i -lt $PageCount; $i++) {
        $st = if ($subfileTypes[$i]) { $subfileTypes[$i].Trim() } else { "" }
        if ($st -notin $AllowedSubfileTypes) {
            return $false
        }
    }
    return $true
}

# Capture function definition once so it can be re-injected into -Parallel runspaces
$script:TestSubfileFnDef = ${function:Test-TiffHasOnlySubfilePages}.ToString()

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
        [string]$thumbFormat = "jpg",
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
        return @{ Result = "ERROR (exiftool check) | $name | cannot detect compression"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
    }
    if ($comp -match $(if ($skipLzw) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
        return @{ Result = "SKIP ($comp) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
    }

    if ((Test-Path -LiteralPath $finalDst) -and -not $overWrite -and ($finalDst -ne $srcPath)) {
        return @{ Result = "SKIP (exists) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
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
                return @{ Result = "ERROR (magick timeout) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
            }
            $pageCountStr = $pageCountJob | Receive-Job
            if ([string]::IsNullOrWhiteSpace($pageCountStr)) {
                return @{ Result = "ERROR (magick page count failed) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
            }
        } finally {
            if ($pageCountJob) {
                Remove-Job $pageCountJob -Force -ErrorAction SilentlyContinue
            }
        }
        $pageCountVal = if ($pageCountStr -is [array]) { $pageCountStr[0] } else { $pageCountStr }
        $pageCount = [int]$pageCountVal
        if ($pageCount -gt 1) {
            if (-not (Test-TiffHasOnlySubfilePages -Path $srcPath -PageCount $pageCount -AllowedSubfileTypes @("REDUCEDIMAGE", "REDUCED"))) {
                $script:multiPagePaths.Add($srcPath) | Out-Null
                return @{ Result = "MULTI ($pageCount pages - skipped) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath; MultiPagePath = $srcPath }
            }
            # If only thumbnails, continue processing page 0
        }
    }

    if ($dryRun) {
        return @{ Result = "DRY ($comp -> ZIP) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath; FinalDst = $finalDst }
    }

    # Build compression command
    if ($generateThumb) {
        # Always read the main image from page 0; thumbnail will be placed at $thumbPage
        $mainPage = "$srcPath[0]"
        $thumbExt = if ($thumbFormat) { $thumbFormat.ToLowerInvariant() } else { "jpg" }
        $tempTiff = [System.IO.Path]::GetTempFileName() + ".tif"
        $thumbTemp = [System.IO.Path]::GetTempFileName() + ".$thumbExt"
        
        try {
            # First: compress main image
            $out = magick -quiet $mainPage -compress zip $tempTiff 2>&1
            if ($LASTEXITCODE -ne 0) {
                return @{ Result = "ERROR (magick compress) | $name | $out"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
            }
            
            # Generate thumbnail: convert to sRGB, strip ICC, resize
            $thumbCmd = @("-quiet", $mainPage, "-colorspace", "sRGB", "-strip", "-thumbnail", "${thumbSize}x${thumbSize}>")
            if ($thumbExt -eq "jpg") { $thumbCmd += "-quality", $thumbQuality }
            $thumbCmd += "$thumbExt`:$thumbTemp"
            $thumbResult = magick @thumbCmd 2>&1
            if ($LASTEXITCODE -ne 0) {
                # If thumbnail fails, just copy the compressed TIFF
                Copy-Item -LiteralPath $tempTiff -Destination $writeDst -Force
                return @{ Result = "OK ($comp -> ZIP) [no thumb] | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath; FinalDst = $finalDst }
            }
            
            # Combine: main TIFF + thumbnail at configured page position
            if ($thumbPage -le 0) {
                $out = magick -quiet "$thumbExt`:$thumbTemp" $tempTiff -compress zip $writeDst 2>&1
            } else {
                $out = magick -quiet $tempTiff "$thumbExt`:$thumbTemp" -compress zip $writeDst 2>&1
            }
            if ($LASTEXITCODE -ne 0) {
                # Fallback: just use compressed main
                Copy-Item -LiteralPath $tempTiff -Destination $writeDst -Force
                return @{ Result = "OK ($comp -> ZIP) [no thumb] | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath; FinalDst = $finalDst }
            }
            
            # Mark thumbnail page as ReducedResolution (subfiletype=1)
            # When thumbPage <= 0 the thumbnail is page 0; otherwise it's page 1
            $thumbIfd = if ($thumbPage -le 0) { "IFD0" } else { "IFD1" }
            $argThumb = [System.IO.Path]::GetTempFileName()
            try {
                [System.IO.File]::WriteAllText($argThumb, "-q`n-q`n-overwrite_original`n-${thumbIfd}:SubfileType#=1`n$writeDst`n")
                $thumbExifOut = exiftool -@ $argThumb 2>&1
                $thumbExifExit = $LASTEXITCODE
            } finally {
                Remove-Item $argThumb -Force -ErrorAction SilentlyContinue
            }
            if ($thumbExifExit -ne 0) {
                Write-Log "WARN (thumbnail SubfileType marker failed) | $name | $thumbExifOut" "WARN"
            }
        } finally {
            Remove-Item -LiteralPath $tempTiff -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $thumbTemp -Force -ErrorAction SilentlyContinue
        }
    } else {
        # Normal compression (all pages or page 0)
        $out = magick -quiet $srcPath -compress zip $writeDst 2>&1
        if ($LASTEXITCODE -ne 0) {
            return @{ Result = "ERROR (magick) | $name | $out"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
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
            return @{ Result = "WARN (exiftool failed, ZIP ok) | $name"; StagingName = $stagingName; OriginalName = $name; SrcPath = $srcPath; FinalDst = $finalDst }
        }
    }

    $canDeleteSource = $false
    if ($deleteSource -and $mode -eq 8) {
        $stagingUsed = ($writeDst -ne $srcPath)
        if ($stagingUsed) {
            if ((_Verify-ZipIntegrity $writeDst) -and (Test-Path -LiteralPath $srcPath)) {
                $canDeleteSource = $true
            }
        } else {
            if ((_Verify-ZipIntegrity $srcPath) -and (Test-Path -LiteralPath $srcPath)) {
                $canDeleteSource = $true
            }
        }
    }

    return @{
        Result = "OK ($comp -> ZIP)$(if ($canDeleteSource) { ' [SOURCE DELETED]' } else { '' }) | $name"
        StagingName = $stagingName
        OriginalName = $name
        SrcPath = $srcPath
        FinalDst = $finalDst
        CanDeleteSource = $canDeleteSource
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
            if ($script:OutputDir -and -not $script:DryRun) { 
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
                    ${function:Test-TiffHasOnlySubfilePages} = $using:TestSubfileFnDef
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
                            if (-not (Test-TiffHasOnlySubfilePages -Path $src -PageCount $pageCount -AllowedSubfileTypes @("REDUCEDIMAGE", "REDUCED"))) {
                                return @{ Result = "MULTI ($pageCount IFDs - skipped) | $name"; StagingName = $null; OriginalName = $name; MultiPagePath = $src }
                            }
                        }
                    }

                    if ($dryL) { return @{ Result = "DRY ($comp -> ZIP) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath } }

                    $out = magick -quiet $src -compress zip $writeDst 2>&1
                    if ($LASTEXITCODE -ne 0) { return @{ Result = "ERROR (magick) | $name | $out"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath } }

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
                                $script:SkipLzwAsCompressed $script:DeleteSource $script:Mode `
                                $script:GenerateThumbnail $script:ThumbSize $script:ThumbQuality $script:ThumbFormat $script:ThumbPage
                    if ($result.StagingName) { $script:stagingMap[$result.FinalDst.ToLowerInvariant()] = @{ StagingName = $result.StagingName; FinalDst = $result.FinalDst } }
                    Process-Results @($result.Result)
                }
            }

            # Move from staging to final destination (legacy mode)
            if ($writeDir -ne $finalDir -and -not $script:DryRun) {
                $moved = 0
                foreach ($key in $script:stagingMap.Keys) {
                    $stagePath = Join-Path $writeDir $script:stagingMap[$key].StagingName
                    $destPath  = $script:stagingMap[$key].FinalDst
                    if ((Test-Path -LiteralPath $stagePath) -and $stagePath -ne $destPath) {
                        try {
                            $stageSize = (Get-Item -LiteralPath $stagePath).Length
                            if (-not (Test-Path -LiteralPath (Split-Path $destPath -Parent))) {
                                [System.IO.Directory]::CreateDirectory((Split-Path $destPath -Parent)) | Out-Null
                            }
                            Move-Item -Force -LiteralPath $stagePath -Destination $destPath -ErrorAction Stop
                            if ((Test-Path -LiteralPath $destPath) -and ((Get-Item -LiteralPath $destPath).Length -eq $stageSize)) {
                                $moved++
                            } else {
                                $script:errTotal++
                                Write-Log "ERROR (legacy move failed) | $([System.IO.Path]::GetFileName($destPath))" "ERROR"
                            }
                        } catch {
                            $script:errTotal++
                            Write-Log "ERROR (legacy move failed) | $([System.IO.Path]::GetFileName($destPath)): $($_.Exception.Message)" "ERROR"
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
    foreach ($df in $dirFiles) {
        $df | Add-Member -NotePropertyName 'InputRoot' -NotePropertyValue $inputRoot -Force
        $allFiles += $df
    }
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

# Mode 8 always requires staging to protect originals until verification.
if ($Mode -eq 8 -and -not $DryRun -and [string]::IsNullOrWhiteSpace($StagingDir)) {
    $defaultStaging = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "compress_tiff_zip_mode8_$([guid]::NewGuid().ToString('N'))")
    $msg = "Mode 8 deletes source files after compression. A staging directory is required to avoid overwriting originals before verification."
    Write-Log $msg "WARN"
    Write-Log "Recommended: provide -StagingDir on a fast local drive." "WARN"
    Write-Log "Default temporary staging will be used: $defaultStaging" "WARN"

    $useDefault = $true
    $nonInteractiveArg = [Environment]::GetCommandLineArgs() | Where-Object { $_ -ieq '-NonInteractive' }
    $isInteractive = ($Host.Name -eq 'ConsoleHost') -and [Environment]::UserInteractive -and (-not $nonInteractiveArg)
    if ($isInteractive) {
        Write-Host ""
        Write-Host $msg -ForegroundColor Yellow
        Write-Host "Staging directory: $defaultStaging" -ForegroundColor Yellow
        $answer = Read-Host "Use default temporary staging? [Y/n]"
        if ($answer -and $answer.Trim().ToLower().StartsWith('n')) {
            $useDefault = $false
        }
    }

    if (-not $useDefault) {
        Write-Log "Mode 8 aborted: no staging directory selected." "ERROR"
        return
    }
    $StagingDir = $defaultStaging
    $script:StagingDir = $StagingDir
    if (-not (Test-Path -LiteralPath $StagingDir)) {
        [System.IO.Directory]::CreateDirectory($StagingDir) | Out-Null
    }
    # Ensure the temp staging dir is cleaned up on interrupt
    $script:cleanupDirs += $StagingDir
}

# Build (src, writeDst, finalDst) tasks grouped by output directory
$tasks = @()
$usedNames = @{}  # Track allocated names per output directory to avoid collisions
foreach ($f in $files) {
    # Use the original input root provided by the user for path resolution
    $fileInputRoot = if ($f.InputRoot) { $f.InputRoot } else { $f.DirectoryName }
    $finalDst = Resolve-Output $f $Mode $fileInputRoot $OutputDir $ZipSuffix $ZipSubfolderName $ExportMarker $ExportZipSubfolder $Overwrite.IsPresent
    if (-not $finalDst) { continue }

    # Ensure the final output directory exists before queueing the task
    $finalDstDir = [System.IO.Path]::GetDirectoryName($finalDst)
    if (-not $DryRun -and -not [string]::IsNullOrWhiteSpace($finalDstDir) -and -not (Test-Path -LiteralPath $finalDstDir)) {
        [System.IO.Directory]::CreateDirectory($finalDstDir) | Out-Null
    }

    # Mode 2: Detect name collisions across different source folders
    if ($Mode -eq 2) {
        $destDir = [System.IO.Path]::GetDirectoryName($finalDst)
        $destName = [System.IO.Path]::GetFileName($finalDst)
        if (-not $usedNames.ContainsKey($destDir)) {
            $usedNames[$destDir] = @()
        }
        $skipThisFile = $false
        if ($destName -in $usedNames[$destDir]) {
            $stem = [System.IO.Path]::GetFileNameWithoutExtension($destName)
            $ext = [System.IO.Path]::GetExtension($destName)
            switch ($script:DuplicateAction) {
                'Skip' {
                    Write-Log "SKIP (duplicate filename in flat output) | $($f.Name)" "INFO"
                    $script:skipTotal++
                    $script:counterTotal++
                    $skipThisFile = $true
                }
                'Overwrite' {
                    # keep candidate as-is; Overwrite switch will allow replacement
                }
                default { # Numbered
                    $counter = 2
                    do {
                        $candidateName = "${stem}_v${counter}${ext}"
                        $candidatePath = Join-Path $destDir $candidateName
                        $counter++
                    } while (($candidateName -in $usedNames[$destDir]) -or (Test-Path -LiteralPath $candidatePath))
                    $destName = $candidateName
                    $finalDst = $candidatePath
                }
            }
        }
        if ($skipThisFile) { continue }
        if ($destName) {
            $usedNames[$destDir] += $destName
        }
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
            $script:counterTotal++
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

    if (-not $DryRun -and -not (Test-Path -LiteralPath $groupDir)) {
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
            ${function:Test-TiffHasOnlySubfilePages} = $using:TestSubfileFnDef
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
            $thumbFormat = $using:ThumbFormat
            $thumbPage = $using:ThumbPage
            $skipCompressedWithThumb = $using:SkipCompressedWithThumb

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
                return @{ Result = "ERROR (exiftool check) | $name | cannot detect compression"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
            }
            if ($comp -match $(if ($skipLzw) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
                # Check if already has thumbnail when SkipCompressedWithThumb is enabled
                if ($skipCompressedWithThumb) {
                    $subfileTypes = magick identify -format "%[tiff:subfiletype]\n" "$srcPath" 2>$null
                    $hasThumb = $false
                    if ($subfileTypes -is [array]) {
                        for ($i = 0; $i -lt $subfileTypes.Count; $i++) {
                            $st = if ($subfileTypes[$i]) { $subfileTypes[$i].Trim() } else { "" }
                            if ($st -in @("REDUCEDIMAGE", "REDUCED")) {
                                $hasThumb = $true
                                break
                            }
                        }
                    }
                    if ($hasThumb) {
                        return @{ Result = "SKIP (compressed+thumb) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
                    }
                }
                return @{ Result = "SKIP ($comp) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
            }
            if ((Test-Path -LiteralPath $finalDst) -and -not $overWrite -and ($finalDst -ne $srcPath)) {
                return @{ Result = "SKIP (exists) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
            }
            if ($safeMode) {
                $srcCapture = $srcPath
                $pageCountJob = $null
                try {
                    $pageCountJob = Start-Job { param($path) magick identify -format "%n" $path 2>$null } -ArgumentList $srcCapture
                    $pageCountJob | Wait-Job -Timeout $magickTimeout | Out-Null
                    if ($pageCountJob.State -eq 'Running') {
                        Stop-Job $pageCountJob
                        return @{ Result = "ERROR (magick timeout) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
                    }
                    $pageCountStr = $pageCountJob | Receive-Job
                    if ([string]::IsNullOrWhiteSpace($pageCountStr)) {
                        return @{ Result = "ERROR (magick page count failed) | $name | possibly corrupted"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
                    }
                } finally {
                    if ($pageCountJob) {
                        Remove-Job $pageCountJob -Force -ErrorAction SilentlyContinue
                    }
                }
                $pageCountVal = if ($pageCountStr -is [array]) { $pageCountStr[0] } else { $pageCountStr }
                $pageCount = [int]$pageCountVal
                if ($pageCount -gt 1) {
                    # Check if all extra pages are thumbnails (subfiletype=ReducedImage)
                    if (-not (Test-TiffHasOnlySubfilePages -Path $srcPath -PageCount $pageCount -AllowedSubfileTypes @("REDUCEDIMAGE", "REDUCED"))) {
                        return @{ Result = "MULTI ($pageCount IFDs - skipped) | $name"; StagingName = $null; OriginalName = $name; MultiPagePath = $srcPath }
                    }
                    # If only thumbnails, continue processing page 0
                }
            }
            if ($dryRun) {
                return @{ Result = "DRY ($comp -> ZIP) | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
            }
            
            if ($generateThumb) {
                # Always read the main image from page 0; thumbnail will be placed at $thumbPage
                $mainPage = "$srcPath[0]"
                $thumbExt = if ($thumbFormat) { $thumbFormat.ToLowerInvariant() } else { "jpg" }
                $tempTiff = [System.IO.Path]::GetTempFileName() + ".tif"
                $thumbTemp = [System.IO.Path]::GetTempFileName() + ".$thumbExt"
                $thumbMarkerFailed = $false
                try {
                    $out = magick -quiet $mainPage -compress zip $tempTiff 2>&1
                    if ($LASTEXITCODE -ne 0) {
                        return @{ Result = "ERROR (magick compress) | $name | $out"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath }
                    }
                    $thumbCmd = @("-quiet", $mainPage, "-colorspace", "sRGB", "-strip", "-thumbnail", "${thumbSize}x${thumbSize}>")
                    if ($thumbExt -eq "jpg") { $thumbCmd += "-quality", $thumbQuality }
                    $thumbCmd += "$thumbExt`:$thumbTemp"
                    $thumbResult = magick @thumbCmd 2>&1
                    if ($LASTEXITCODE -ne 0) {
                        Copy-Item -LiteralPath $tempTiff -Destination $writeDst -Force
                        return @{ Result = "OK ($comp -> ZIP) [no thumb] | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath; FinalDst = $finalDst }
                    }
                    if ($thumbPage -le 0) {
                        $out = magick -quiet "$thumbExt`:$thumbTemp" $tempTiff -compress zip $writeDst 2>&1
                    } else {
                        $out = magick -quiet $tempTiff "$thumbExt`:$thumbTemp" -compress zip $writeDst 2>&1
                    }
                    if ($LASTEXITCODE -ne 0) {
                        Copy-Item -LiteralPath $tempTiff -Destination $writeDst -Force
                        return @{ Result = "OK ($comp -> ZIP) [no thumb] | $name"; StagingName = $null; OriginalName = $name; SrcPath = $srcPath; FinalDst = $finalDst }
                    }
                    $argThumb = [System.IO.Path]::GetTempFileName()
                    $thumbIfd = if ($thumbPage -le 0) { "IFD0" } else { "IFD1" }
                    try {
                        [System.IO.File]::WriteAllText($argThumb, "-q`n-q`n-overwrite_original`n-${thumbIfd}:SubfileType#=1`n$writeDst`n")
                        $thumbExifOut = exiftool -@ $argThumb 2>&1
                        $thumbExifExit = $LASTEXITCODE
                    } finally {
                        Remove-Item $argThumb -Force -ErrorAction SilentlyContinue
                    }
                    if ($thumbExifExit -ne 0) {
                        # Cannot log via Write-Log inside -Parallel; surface as warning in result string later
                        $thumbMarkerFailed = $true
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
            $canDeleteSource = $false
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
                if ($integrityOk -and (Test-Path -LiteralPath $srcPath)) {
                    $canDeleteSource = $true
                }
            }
            if ($thumbMarkerFailed) {
                return @{
                    Result = "WARN (exiftool failed, ZIP ok) [thumb marker failed] | $name"
                    StagingName = $stagingName
                    OriginalName = $name
                    SrcPath = $srcPath
                    FinalDst = $finalDst
                    CanDeleteSource = $canDeleteSource
                }
            }
            return @{
                Result = "OK ($comp -> ZIP)$(if ($canDeleteSource) { ' [SOURCE DELETED]' } else { '' }) | $name"
                StagingName = $stagingName
                OriginalName = $name
                SrcPath = $srcPath
                FinalDst = $finalDst
                CanDeleteSource = $canDeleteSource
            }
        } -ThrottleLimit $effectiveWorkers

        # Process results sequentially (avoids scope issues with Process-Results)
        # Use FinalDst as key to handle duplicate filenames across different folders
        $errBefore = $script:errTotal
        $errorSrcPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($r in $parallelResults) {
            if ($r.StagingName) { $script:stagingMap[$r.FinalDst.ToLowerInvariant()] = @{ StagingName = $r.StagingName; FinalDst = $r.FinalDst } }
            if ($r.MultiPagePath) { $script:multiPagePaths.Add($r.MultiPagePath) | Out-Null }
            if ($r.SrcPath -and $r.Result -and $r.Result.StartsWith("ERROR")) { $errorSrcPaths.Add($r.SrcPath) | Out-Null }
            Process-Results @($r.Result)
        }
        $groupErrs = $script:errTotal - $errBefore

        # Move from staging to final destination before rollback, so rollback can check FinalDst
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

        # Mode 8: delete source files only after successful move to final destination
        if ($Mode -eq 8 -and -not $DryRun) {
            $deletedCount = 0
            foreach ($r in $parallelResults) {
                if ($r.CanDeleteSource -and $r.SrcPath -and (Test-Path -LiteralPath $r.SrcPath)) {
                    if ($r.SrcPath -eq $r.FinalDst) {
                        # In-place move: the original was already replaced by the compressed file
                        continue
                    }
                    if (Test-Path -LiteralPath $r.FinalDst) {
                        try {
                            Remove-Item -LiteralPath $r.SrcPath -Force
                            $deletedCount++
                        } catch {
                            Write-Log "ERROR (failed to delete source after move) | $([System.IO.Path]::GetFileName($r.SrcPath))" "ERROR"
                        }
                    } else {
                        Write-Log "ERROR (final destination missing, source preserved) | $([System.IO.Path]::GetFileName($r.SrcPath))" "ERROR"
                    }
                }
            }
            if ($deletedCount -gt 0) { Write-Log "  -> Deleted $deletedCount source file(s) after successful move" }
        }

        # Rollback: if errors occurred on files moved to OLD_TIFFs, restore originals only when FinalDst is missing
        if ($groupErrs -gt 0) {
            $failedCount = 0
            foreach ($t in $groupTasks) {
                if ($t.MovedBy -eq "mode0" -and $t.OldTiffBackup -and $errorSrcPaths.Contains($t.Src)) {
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
        $errorSrcPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        $sequentialResults = @()
        foreach ($t in $groupTasks) {
            $result = Process-TiffJob $t.Src $t.WriteDst $t.FinalDst `
                                $DryRun $Overwrite $SafeMode `
                                $SkipLzwAsCompressed $DeleteSource $Mode `
                                $script:GenerateThumbnail $script:ThumbSize $script:ThumbQuality $script:ThumbFormat $script:ThumbPage
            if ($result.StagingName) { $script:stagingMap[$t.FinalDst.ToLowerInvariant()] = @{ StagingName = $result.StagingName; FinalDst = $t.FinalDst } }
            if ($result.SrcPath -and $result.Result -and $result.Result.StartsWith("ERROR")) { $errorSrcPaths.Add($result.SrcPath) | Out-Null }
            $sequentialResults += $result
            Process-Results @($result.Result)
        }
        $groupErrs = $script:errTotal - $errBefore

        # Move from staging to final destination before rollback, so rollback can check FinalDst
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

        # Mode 8: delete source files only after successful move to final destination
        if ($Mode -eq 8 -and -not $DryRun) {
            $deletedCount = 0
            foreach ($r in $sequentialResults) {
                if ($r.CanDeleteSource -and $r.SrcPath -and (Test-Path -LiteralPath $r.SrcPath)) {
                    if ($r.SrcPath -eq $r.FinalDst) {
                        # In-place move: the original was already replaced by the compressed file
                        continue
                    }
                    if (Test-Path -LiteralPath $r.FinalDst) {
                        try {
                            Remove-Item -LiteralPath $r.SrcPath -Force
                            $deletedCount++
                        } catch {
                            Write-Log "ERROR (failed to delete source after move) | $([System.IO.Path]::GetFileName($r.SrcPath))" "ERROR"
                        }
                    } else {
                        Write-Log "ERROR (final destination missing, source preserved) | $([System.IO.Path]::GetFileName($r.SrcPath))" "ERROR"
                    }
                }
            }
            if ($deletedCount -gt 0) { Write-Log "  -> Deleted $deletedCount source file(s) after successful move" }
        }

        # Rollback for PS5: restore originals only for tasks that reported ERROR and have no FinalDst
        if ($groupErrs -gt 0) {
            $failedCount = 0
            foreach ($t in $groupTasks) {
                if ($t.MovedBy -eq "mode0" -and $t.OldTiffBackup -and $errorSrcPaths.Contains($t.Src)) {
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
