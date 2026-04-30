# -- CLI PARAMETERS -----------------------------------------------
param(
    [string]$InputDir = ".",
    [int]$Size = 256,
    [string]$OutputDir = "",
    [switch]$Remove,
    [switch]$DryRun,
    [switch]$Recursive,
    [int]$Workers = 4,
    [string]$Page = "0",
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
$inputRoots = if ([System.IO.Path]::IsPathRooted($InputDir)) { @($InputDir) } else { @(Join-Path $PWD.Path $InputDir) }
$allFiles = foreach ($root in $inputRoots) {
    if (-not (Test-Path -LiteralPath $root)) {
        Write-Log "InputDir not found: $root" "WARN"
        continue
    }
    $item = Get-Item -LiteralPath $root
    if ($item -is [System.IO.FileInfo]) {
        if ($item.Extension -match '^\.(tif|tiff)$') { $item }
    } else {
        $recurseFlag = if ($Recursive) { $true } else { $false }
        Get-ChildItem -LiteralPath $root -File -Recurse:$recurseFlag |
            Where-Object { $_.Extension -match '^\.(tif|tiff)$' }
    }
}

$files = $allFiles | Where-Object { $_.DirectoryName -notmatch '(?i)[\\/]OLD_TIFFS?[\\/]|[\\/]OLD_TIFFS?$' }
$total = $files.Count

if ($total -eq 0) {
    Write-Log "No TIFF files found in: $($inputRoots -join '; ')" "WARN"
    Write-Log "Log: $logFile"
    exit 0
}

Write-Log "Found: $total TIFF(s)"
Write-Log "Size: ${Size}px | Format: $Format | Quality: $Quality | Page: $Page"
Write-Log "Mode: $(if ($Remove) { 'REMOVE thumbnails' } else { 'GENERATE thumbnails' })"
# -----------------------------------------------------------------

# -- Statistics ----------------------------------------------------
$script:okTotal     = 0
$script:skipTotal   = 0
$script:errTotal    = 0
$script:counterTotal= 0

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

# -- Remove thumbnails ---------------------------------------------
if ($Remove) {
    foreach ($f in $files) {
        $thumbName = "$($f.BaseName)_thumb.$Format"
        $thumbPath = if ($OutputDir) {
            $outDir = if ([System.IO.Path]::IsPathRooted($OutputDir)) { $OutputDir } else { Join-Path $f.DirectoryName $OutputDir }
            Join-Path $outDir $thumbName
        } else {
            Join-Path $f.DirectoryName $thumbName
        }
        
        $script:counterTotal++
        if (Test-Path -LiteralPath $thumbPath) {
            if (-not $DryRun) {
                try {
                    Remove-Item -LiteralPath $thumbPath -Force
                    $script:okTotal++
                    Write-Log "[$($script:counterTotal)/$total] REMOVED | $thumbName"
                } catch {
                    $script:errTotal++
                    Write-Log "[$($script:counterTotal)/$total] ERROR (remove failed) | $thumbName | $($_.Exception.Message)" "ERROR"
                }
            } else {
                $script:skipTotal++
                Write-Log "[$($script:counterTotal)/$total] DRY-RUN (would remove) | $thumbName"
            }
        } else {
            $script:skipTotal++
            Write-Log "[$($script:counterTotal)/$total] SKIP (not found) | $thumbName"
        }
    }
    
    Write-Log ""
    Write-Log "Done: $($script:okTotal) removed | $($script:skipTotal) skipped | $($script:errTotal) errors | $total processed"
    Write-Log "Log: $logFile"
    exit 0
}
# -----------------------------------------------------------------

# -- Generate thumbnails -------------------------------------------
$effectiveWorkers = [Math]::Min($Workers, 16)
$isPS7 = $PSVersionTable.PSVersion.Major -ge 7

# Prepare tasks
$tasks = foreach ($f in $files) {
    $thumbName = "$($f.BaseName)_thumb.$Format"
    $destPath = if ($OutputDir) {
        $outDir = if ([System.IO.Path]::IsPathRooted($OutputDir)) { $OutputDir } else { Join-Path $f.DirectoryName $OutputDir }
        Join-Path $outDir $thumbName
    } else {
        Join-Path $f.DirectoryName $thumbName
    }
    
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
        $name = [System.IO.Path]::GetFileName($t.SrcPath)
        
        if ((Test-Path -LiteralPath $t.DestPath) -and -not $t.DryRun) {
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
                $result = Start-Process -FilePath "magick" -ArgumentList $allArgs -Wait -NoNewWindow -PassThru
                
                if ($result.ExitCode -ne 0) {
                    "ERROR (magick failed) | $name"
                } elseif (Test-Path -LiteralPath $t.DestPath) {
                    $thumbSize = (Get-Item -LiteralPath $t.DestPath).Length
                    "OK | $name -> $([System.IO.Path]::GetFileName($t.DestPath)) ($thumbSize bytes)"
                } else {
                    "ERROR (output not created) | $name"
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
        
        if ((Test-Path -LiteralPath $t.DestPath) -and -not $t.DryRun) {
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
                $proc = Start-Process -FilePath "magick" -ArgumentList $allArgs -Wait -NoNewWindow -PassThru
                
                if ($proc.ExitCode -ne 0) {
                    $result = "ERROR (magick failed) | $name"
                } elseif (Test-Path -LiteralPath $t.DestPath) {
                    $thumbSize = (Get-Item -LiteralPath $t.DestPath).Length
                    $result = "OK | $name -> $([System.IO.Path]::GetFileName($t.DestPath)) ($thumbSize bytes)"
                } else {
                    $result = "ERROR (output not created) | $name"
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