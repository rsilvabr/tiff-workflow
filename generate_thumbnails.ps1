# -- CLI PARAMETERS -----------------------------------------------
param(
    [string]$InputDir = ".",
    [int]$Size = 256,
    [string]$OutputDir = "",
    [switch]$Remove,
    [switch]$DryRun,
    [switch]$Recursive,
    [ValidateRange(1, 64)][int]$Workers = 4,
    [ValidatePattern('^(all|\d+)$')]
    [string]$Page = "0",
    [ValidatePattern('^(100|[1-9][0-9]?)$')]
    [string]$Quality = "85",
    [string]$Format = "jpg"
)
# -----------------------------------------------------------------

# -- Logging -------------------------------------------------------
$scriptName = "generate_thumbnails"
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
# -----------------------------------------------------------------

# -- Prerequisite checks -------------------------------------------
$missingTools = @()
if (-not (Get-Command magick -ErrorAction SilentlyContinue)) { $missingTools += "ImageMagick (magick)" }
if ($missingTools.Count -gt 0) {
    Write-Host "ERROR: Required tools not found in PATH: $($missingTools -join ', ')" -ForegroundColor Red
    Write-Host "Please install the missing tools and try again." -ForegroundColor Yellow
    exit 1
}
# -----------------------------------------------------------------

# -- Validate parameters -------------------------------------------
if ($Size -lt 32 -or $Size -gt 4096) {
    Write-Log "Invalid size: $Size. Must be between 32 and 4096." "ERROR"
    exit 1
}

if ($Format -notin @("jpg", "jpeg", "png", "tif", "tiff")) {
    Write-Log "Invalid format: $Format. Must be jpg, png, tif, or tiff." "ERROR"
    exit 1
}
# -----------------------------------------------------------------

# -- Resolve input -------------------------------------------------
# Accepts a ';' separated list like the other backends, and reports a bad path as an ERROR
# instead of a WARN: a typo used to look identical to "folder has no TIFFs" and exited 0.
$inputRoots   = @()
$missingRoots = @()
foreach ($dir in ($InputDir -split ';')) {
    $dir = $dir.Trim()
    if ([string]::IsNullOrWhiteSpace($dir)) { continue }
    if (-not [System.IO.Path]::IsPathRooted($dir)) { $dir = Join-Path $PWD.Path $dir }
    if (-not (Test-Path -LiteralPath $dir)) { $missingRoots += $dir; continue }
    $inputRoots += $dir
}
$recurseFlag = [bool]$Recursive

function Test-ThumbExists {
    <#
    .SYNOPSIS
        True when a thumbnail for $destPath is already on disk.
        With -Page all and a single-frame format ImageMagick writes name-0.jpg,
        name-1.jpg, ... so testing only the unsuffixed path never matched and every
        run regenerated everything.
    #>
    param([string]$destPath)

    if (Test-Path -LiteralPath $destPath) { return $true }
    $d = [System.IO.Path]::GetDirectoryName($destPath)
    if (-not $d -or -not (Test-Path -LiteralPath $d)) { return $false }
    $b = [System.IO.Path]::GetFileNameWithoutExtension($destPath)
    $e = [System.IO.Path]::GetExtension($destPath)
    return (@(Get-ChildItem -LiteralPath $d -Filter "$b-*$e" -File -ErrorAction SilentlyContinue).Count -gt 0)
}
$script:ThumbExistsFnDef = ${function:Test-ThumbExists}.ToString()

$allFiles = foreach ($root in $inputRoots) {
    $item = Get-Item -LiteralPath $root
    if ($item -is [System.IO.FileInfo]) {
        if ($item.Extension -match '^\.(tif|tiff)$') { $item }
    } else {
        Get-ChildItem -LiteralPath $root -File -Recurse:$recurseFlag |
            Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
    }
}

$files = @($allFiles | Where-Object { $_.DirectoryName -notmatch '(?i)[\\/]OLD_TIFFS?[\\/]|[\\/]OLD_TIFFS?$' -and $_.BaseName -notmatch '(?i)_thumb(-\d+)?$' })
$total = $files.Count

# -- Statistics ----------------------------------------------------
$script:okTotal     = 0
$script:skipTotal   = 0
$script:errTotal    = 0
$script:counterTotal= 0

# Counted after the reset above so a bad path still yields exit code 1
foreach ($m in $missingRoots) { Write-Log "ERROR: input directory not found: $m" "ERROR" }
$script:errTotal += $missingRoots.Count

if ($inputRoots.Count -eq 0) {
    Write-Log "No valid input directories specified." "ERROR"
    Write-Log "Log: $logFile"
    exit 1
}

# -- Remove thumbnails ---------------------------------------------
# Enumerates the thumbnails themselves rather than deriving them from the source TIFFs:
# a thumbnail whose TIFF was deleted or moved could never be cleaned up before, and the
# counters mixed "source files" with "thumbnails removed".
if ($Remove) {
    $allFormats = @("jpg", "jpeg", "png", "tif", "tiff")
    $searchRoots = if ($OutputDir) {
        if ([System.IO.Path]::IsPathRooted($OutputDir)) {
            @($OutputDir)
        } else {
            @($inputRoots | ForEach-Object { Join-Path $_ $OutputDir })
        }
    } else {
        $inputRoots
    }

    $thumbs = @(foreach ($root in ($searchRoots | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -File -Recurse:$recurseFlag -ErrorAction SilentlyContinue |
            Where-Object {
                $_.BaseName -match '(?i)_thumb(-\d+)?$' -and
                ($_.Extension.TrimStart('.').ToLowerInvariant() -in $allFormats)
            }
    })

    $total = $thumbs.Count
    Write-Log "Found: $total thumbnail(s) to remove"
    if ($total -eq 0) {
        Write-Log "No thumbnails found in: $(($searchRoots | Select-Object -Unique) -join '; ')" "WARN"
        Write-Log "Log: $logFile"
        if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }
    }

    foreach ($thumb in $thumbs) {
        $script:counterTotal++
        if ($DryRun) {
            $script:skipTotal++
            Write-Log "[$($script:counterTotal)/$total] DRY-RUN (would remove) | $($thumb.Name)"
            continue
        }
        try {
            Remove-Item -LiteralPath $thumb.FullName -Force
            $script:okTotal++
            Write-Log "[$($script:counterTotal)/$total] REMOVED | $($thumb.Name)"
        } catch {
            $script:errTotal++
            Write-Log "[$($script:counterTotal)/$total] ERROR (remove failed) | $($thumb.Name) | $($_.Exception.Message)" "ERROR"
        }
    }

    Write-Log ""
    Write-Log "Done: $($script:okTotal) removed | $($script:skipTotal) skipped | $($script:errTotal) errors | $($script:counterTotal)/$total processed"
    Write-Log "Log: $logFile"
    if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }
}

if ($total -eq 0) {
    Write-Log "No TIFF files found in: $($inputRoots -join '; ')" "WARN"
    Write-Log "Log: $logFile"
    if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }
}

Write-Log "Found: $total TIFF(s)"
Write-Log "Size: ${Size}px | Format: $Format | Quality: $Quality | Page: $Page"
Write-Log "Mode: $(if ($Remove) { 'REMOVE thumbnails' } else { 'GENERATE thumbnails' })"
# -----------------------------------------------------------------

function Process-Results($lines) {
    foreach ($line in $lines) {
        $script:counterTotal++
        if ($line -match '^OK') {
            $script:okTotal++
            Write-Log "[$($script:counterTotal)/$total] $line"
        } elseif ($line -match '^SKIP') {
            $script:skipTotal++
            Write-Log "[$($script:counterTotal)/$total] $line"
        } elseif ($line -match '^ERROR') {
            $script:errTotal++
            Write-Log "[$($script:counterTotal)/$total] $line" "ERROR"
        } else {
            Write-Log "[$($script:counterTotal)/$total] $line"
        }
    }
}
# -----------------------------------------------------------------

# -- Generate thumbnails -------------------------------------------
$effectiveWorkers = [Math]::Min($Workers, 16)
$isPS7 = $PSVersionTable.PSVersion.Major -ge 7

# Prepare tasks
# claimedDest: with -OutputDir + -Recursive, a\photo.tif and b\photo.tif both map to
# photo_thumb.jpg. The second one used to report "SKIP (exists)" and never be generated,
# so number later claimants instead (same policy as compress_tiff_zip.ps1).
$claimedDest = @{}
$tasks = foreach ($f in $files) {
    $thumbName = "$($f.BaseName)_thumb.$Format"
    $outDir = if ($OutputDir) {
        if ([System.IO.Path]::IsPathRooted($OutputDir)) { $OutputDir } else { Join-Path $f.DirectoryName $OutputDir }
    } else {
        $f.DirectoryName
    }
    $destPath = Join-Path $outDir $thumbName

    $dKey = $destPath.ToLowerInvariant()
    if ($claimedDest.ContainsKey($dKey)) {
        $n = 2
        do {
            $candidateName = "$($f.BaseName)_thumb_v${n}.$Format"
            $candidatePath = Join-Path $outDir $candidateName
            $n++
        } while ($claimedDest.ContainsKey($candidatePath.ToLowerInvariant()))
        Write-Log "RENAME (thumbnail name already taken) | $($f.Name) -> $candidateName" "WARN"
        $destPath = $candidatePath
        $dKey = $candidatePath.ToLowerInvariant()
    }
    $claimedDest[$dKey] = $f.FullName

    @{
        SrcPath = $f.FullName
        DestPath = $destPath
        Size = $Size
        Quality = $Quality
        Format = $Format
        Page = $Page
        DryRun = $DryRun.IsPresent
    }
}

# Process tasks
if ($isPS7 -and $effectiveWorkers -gt 1) {
    # Parallel processing
    $results = $tasks | ForEach-Object -Parallel {
        $t = $_
        # Functions from the parent scope are not visible inside -Parallel runspaces
        ${function:Test-ThumbExists} = $using:ThumbExistsFnDef
        $name = [System.IO.Path]::GetFileName($t.SrcPath)

        if ((Test-ThumbExists $t.DestPath) -and -not $t.DryRun) {
            "SKIP (exists) | $name"
        } elseif ($t.DryRun) {
            "DRY-RUN | $name -> $([System.IO.Path]::GetFileName($t.DestPath))"
        } else {
            try {
                $destDir = [System.IO.Path]::GetDirectoryName($t.DestPath)
                if (-not (Test-Path -LiteralPath $destDir)) {
                    [System.IO.Directory]::CreateDirectory($destDir) | Out-Null
                }
                
                $pageSuffix = if ($t.Page -eq "all") { "" } else { "[$($t.Page)]" }
                $inputWithPage = "$($t.SrcPath)$pageSuffix"
                
                $magickArgs = @(
                    "-colorspace", "sRGB",
                    "-strip",
                    "-thumbnail", "$($t.Size)x$($t.Size)>",
                    "-quality", $t.Quality
                )
                
                if ($t.Format -in @("jpg", "jpeg")) {
                    $magickArgs += @("-interlace", "Plane")
                } elseif ($t.Format -in @("tif", "tiff")) {
                    $magickArgs += @("-compress", "zip")
                }
                
                $magickArgs += $t.DestPath
                
                $allArgs = @($inputWithPage) + $magickArgs
                $magickOutput = & magick @allArgs 2>&1
                $exitCode = $LASTEXITCODE
                
                if ($exitCode -ne 0) {
                    $errLine = ($magickOutput | ForEach-Object {
                        if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
                    } | Where-Object { $_.Trim() } | Select-Object -First 1)
                    "ERROR (magick failed) | $name | $errLine"
                } elseif (Test-Path -LiteralPath $t.DestPath) {
                    $thumbSize = (Get-Item -LiteralPath $t.DestPath).Length
                    "OK | $name -> $([System.IO.Path]::GetFileName($t.DestPath)) ($thumbSize bytes)"
                } else {
                    $destBase = [System.IO.Path]::GetFileNameWithoutExtension($t.DestPath)
                    $destExt = [System.IO.Path]::GetExtension($t.DestPath)
                    $frames = @(Get-ChildItem -LiteralPath $destDir -Filter "$destBase-*$destExt" -File -ErrorAction SilentlyContinue)
                    if ($frames.Count -gt 0) {
                        "OK | $name -> $destBase-*$destExt ($($frames.Count) frames)"
                    } else {
                        "ERROR (output not created) | $name"
                    }
                }
            } catch {
                $errMsg = if ($_.Exception -and $_.Exception.Message) { $_.Exception.Message } else { $_.ToString() }
                "ERROR | $name | $errMsg"
            }
        }
    } -ThrottleLimit $effectiveWorkers
    
    foreach ($r in $results) {
        Process-Results @($r)
    }
} else {
    # Sequential processing
    foreach ($t in $tasks) {
        $name = [System.IO.Path]::GetFileName($t.SrcPath)

        if ((Test-ThumbExists $t.DestPath) -and -not $t.DryRun) {
            $result = "SKIP (exists) | $name"
        } elseif ($t.DryRun) {
            $result = "DRY-RUN | $name -> $([System.IO.Path]::GetFileName($t.DestPath))"
        } else {
            try {
                $destDir = [System.IO.Path]::GetDirectoryName($t.DestPath)
                if (-not (Test-Path -LiteralPath $destDir)) {
                    [System.IO.Directory]::CreateDirectory($destDir) | Out-Null
                }
                
                $pageSuffix = if ($t.Page -eq "all") { "" } else { "[$($t.Page)]" }
                $inputWithPage = "$($t.SrcPath)$pageSuffix"
                
                $magickArgs = @(
                    "-colorspace", "sRGB",
                    "-strip",
                    "-thumbnail", "$($t.Size)x$($t.Size)>",
                    "-quality", $t.Quality
                )
                
                if ($t.Format -in @("jpg", "jpeg")) {
                    $magickArgs += @("-interlace", "Plane")
                } elseif ($t.Format -in @("tif", "tiff")) {
                    $magickArgs += @("-compress", "zip")
                }
                
                $magickArgs += $t.DestPath
                
                $allArgs = @($inputWithPage) + $magickArgs
                $magickOutput = & magick @allArgs 2>&1
                $exitCode = $LASTEXITCODE
                
                if ($exitCode -ne 0) {
                    $errLine = ($magickOutput | ForEach-Object {
                        if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
                    } | Where-Object { $_.Trim() } | Select-Object -First 1)
                    $result = "ERROR (magick failed) | $name | $errLine"
                } elseif (Test-Path -LiteralPath $t.DestPath) {
                    $thumbSize = (Get-Item -LiteralPath $t.DestPath).Length
                    $result = "OK | $name -> $([System.IO.Path]::GetFileName($t.DestPath)) ($thumbSize bytes)"
                } else {
                    $destBase = [System.IO.Path]::GetFileNameWithoutExtension($t.DestPath)
                    $destExt = [System.IO.Path]::GetExtension($t.DestPath)
                    $frames = @(Get-ChildItem -LiteralPath $destDir -Filter "$destBase-*$destExt" -File -ErrorAction SilentlyContinue)
                    if ($frames.Count -gt 0) {
                        $result = "OK | $name -> $destBase-*$destExt ($($frames.Count) frames)"
                    } else {
                        $result = "ERROR (output not created) | $name"
                    }
                }
            } catch {
                $errMsg = if ($_.Exception -and $_.Exception.Message) { $_.Exception.Message } else { $_.ToString() }
                $result = "ERROR | $name | $errMsg"
            }
        }
        
        Process-Results @($result)
    }
}

Write-Log ""
Write-Log "Done: $($script:okTotal) OK | $($script:skipTotal) skipped | $($script:errTotal) errors | $($script:counterTotal)/$total processed"
Write-Log "Log: $logFile"
if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }