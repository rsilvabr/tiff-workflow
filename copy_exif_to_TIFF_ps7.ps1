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
    [string]$ExcludeFolders = "",    # ';'-separated folder NAMES to skip during discovery
    [double]$WorkerMemoryFraction = 0.8,  # Cap parallel workers so their ImageMagick processes fit in this share of free memory; 0 = no cap
    [string]$InputDirFile = ""       # UTF-8 text file, one input folder per line (the wizard uses it when -InputDir would exceed the command-line limit)
)
# The tiff-workflow release this file belongs to. Not a setting: tests/test_version_parity.py
# requires the same value in every script and in convert_tiff.py's TOOLKIT_VERSION.
$script:ToolkitVersion = '2.9.0'
$script:ToolkitUrl = 'https://github.com/rsilvabr/tiff-workflow'

# Settings overlay (.tiff_workflow_settings.json): the ONLY names it can change in this script,
# each with the type/range it must have. Never a safety or per-run setting (Mode, InputDir,
# OutputDir, StagingDir, Workers, DryRun, SafeMode, SkipLzwAsCompressed, Overwrite,
# DeleteSource, ...): tests/test_settings_overlay.py pins that. convert_tiff.py parses this
# block, one entry per line.
# OVERLAY-SPECS-BEGIN
$script:OverlaySpecs = @{
    'MagickTimeout' = 'int:1:'
    'WorkerMemoryFraction' = 'float:0:2'
}
# OVERLAY-SPECS-END

function Get-SettingsOverlay {
    param([string]$Section, [hashtable]$Specs, [string]$ScriptRoot)
    # The settings overlay: .tiff_workflow_settings.json changes the settings at the top of the
    # scripts without editing them. Returns @{ Path; Values; Warnings }; NEVER throws -- a file
    # that cannot be read changes nothing and says so. Only names in $Specs are taken, each
    # checked against its spec (int/float/intstr:lo:hi, bool, name, path?); nothing that deletes,
    # overwrites or is chosen per run is ever in a spec table. The file is the env variable
    # TIFFWORKFLOW_SETTINGS (set but blank = no overlay), else next to the scripts, else the
    # user profile. [System.IO.File], not Test-Path/Get-Content: "[ ]" in a folder name is a
    # wildcard to them.
    $result = @{ Path = ''; Values = @{}; Warnings = @() }
    try {
        $fileName = '.tiff_workflow_settings.json'
        $envPath = [System.Environment]::GetEnvironmentVariable('TIFFWORKFLOW_SETTINGS')
        if ($null -ne $envPath) {
            if ([string]::IsNullOrWhiteSpace($envPath)) { return $result }
            $path = $envPath
        } else {
            $path = $fileName
            if ($ScriptRoot) { $path = [System.IO.Path]::Combine($ScriptRoot, $fileName) }
            if (-not [System.IO.File]::Exists($path)) {
                $userHome = $env:USERPROFILE
                if (-not $userHome) { $userHome = [System.Environment]::GetFolderPath('UserProfile') }
                if ($userHome) { $path = [System.IO.Path]::Combine($userHome, $fileName) }
            }
        }
        $result.Path = $path
        if (-not [System.IO.File]::Exists($path)) { return $result }
        # Audit round 28: the three readers (the wizard and PowerShell 5.1/7) must agree on what
        # a bad file is, or the wizard plans with one value and the backend runs another.
        # ConvertFrom-Json takes comments, trailing commas, single quotes, bare keys, 0x10 and
        # 010 (7 reads it as octal), and ReadAllText took UTF-16 and turned ANSI bytes into
        # U+FFFD; Python's json refuses all of them. So: strict UTF-8 (a UTF-8 BOM is allowed),
        # then the JSON grammar Python's json.loads accepts, then ConvertFrom-Json.
        $bytes = [System.IO.File]::ReadAllBytes($path)
        $text = [System.Text.UTF8Encoding]::new($false, $true).GetString($bytes)
        if ($text.Length -gt 0 -and [int]$text[0] -eq 0xFEFF) { $text = $text.Substring(1) }
        function Skip-JsonSpace($st) {
            while ($st.P -lt $st.T.Length) {
                $c = [int]$st.T[$st.P]
                if ($c -ne 32 -and $c -ne 9 -and $c -ne 10 -and $c -ne 13) { break }
                $st.P++
            }
        }
        function Read-JsonString($st) {
            $m = $st.Str.Match($st.T, $st.P)
            if (-not $m.Success) { throw "not valid JSON (a string at character $($st.P))" }
            $st.P += $m.Length
        }
        function Read-JsonValue($st, [int]$depth) {
            if ($depth -gt 64) { throw 'not valid JSON (nested too deeply)' }
            Skip-JsonSpace $st
            if ($st.P -ge $st.T.Length) { throw 'not valid JSON (it ends too early)' }
            $c = [int]$st.T[$st.P]
            if ($c -eq 123 -or $c -eq 91) {
                $close = 93
                if ($c -eq 123) { $close = 125 }
                $st.P++
                Skip-JsonSpace $st
                if ($st.P -lt $st.T.Length -and [int]$st.T[$st.P] -eq $close) { $st.P++; return }
                while ($true) {
                    if ($c -eq 123) {
                        Skip-JsonSpace $st
                        Read-JsonString $st
                        Skip-JsonSpace $st
                        if ($st.P -ge $st.T.Length -or [int]$st.T[$st.P] -ne 58) { throw "not valid JSON (':' expected at character $($st.P))" }
                        $st.P++
                    }
                    Read-JsonValue $st ($depth + 1)
                    Skip-JsonSpace $st
                    if ($st.P -ge $st.T.Length) { throw 'not valid JSON (it ends too early)' }
                    $d = [int]$st.T[$st.P]
                    $st.P++
                    if ($d -eq $close) { return }
                    if ($d -ne 44) { throw "not valid JSON (character $($st.P - 1))" }
                }
            }
            if ($c -eq 34) { Read-JsonString $st; return }
            foreach ($word in @('true', 'false', 'null', 'NaN', 'Infinity', '-Infinity')) {
                if ([string]::CompareOrdinal($st.T, $st.P, $word, 0, $word.Length) -eq 0) { $st.P += $word.Length; return }
            }
            $m = $st.Num.Match($st.T, $st.P)
            if (-not $m.Success) { throw "not valid JSON (character $($st.P))" }
            $st.P += $m.Length
        }
        $st = @{
            T = $text
            P = 0
            Str = [regex]::new('\G"(?:[^"\\\x00-\x1F]|\\["\\/bfnrt]|\\u[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F])*"')
            Num = [regex]::new('\G-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
        }
        Read-JsonValue $st 0
        Skip-JsonSpace $st
        if ($st.P -lt $st.T.Length) { throw "not valid JSON (text after the end, character $($st.P))" }
        # PS 5.1 unrolls a root array into its elements: look at the text, not at the object
        $first = $text.TrimStart([char[]]@(0xFEFF, 32, 9, 13, 10))
        if ($first.Length -eq 0 -or $first[0] -ne [char]123) { throw 'not a JSON object' }
        $root = ConvertFrom-Json -InputObject $text
        $sect = $null
        foreach ($p in $root.PSObject.Properties) { if ($p.Name -ceq $Section) { $sect = $p.Value } }
        if ($null -eq $sect) { return $result }
        if ($sect -isnot [pscustomobject]) { throw "the '$Section' entry is not an object" }
        foreach ($p in $sect.PSObject.Properties) {
            $name = $p.Name
            $v = $p.Value
            if (-not (@($Specs.Keys) -ccontains $name)) {
                $result.Warnings += "$name is not a setting the overlay can change in $Section; ignored"
                continue
            }
            $parts = ([string]$Specs[$name]).Split(':')
            $kind = $parts[0]
            $lo = $null
            $hi = $null
            if ($parts.Count -ge 3) {
                if ($parts[1] -ne '') { $lo = [double]::Parse($parts[1], [System.Globalization.CultureInfo]::InvariantCulture) }
                if ($parts[2] -ne '') { $hi = [double]::Parse($parts[2], [System.Globalization.CultureInfo]::InvariantCulture) }
            }
            $ok = $false
            $checked = $null
            $expected = 'a setting the overlay cannot change'
            $isWhole = ($v -is [int]) -or ($v -is [long])
            $isNum = $isWhole -or ($v -is [double]) -or ($v -is [decimal])
            if ($kind -eq 'int' -or $kind -eq 'intstr' -or $kind -eq 'float') {
                $expected = 'a whole number'
                if ($kind -eq 'float') { $expected = 'a number' }
                if ($null -ne $lo -and $null -ne $hi) { $expected += " from $($parts[1]) to $($parts[2])" }
                elseif ($null -ne $lo) { $expected += " of at least $($parts[1])" }
                if ($isWhole -or ($kind -eq 'float' -and $isNum)) {
                    $num = [double]$v
                    $inRange = (($null -eq $lo) -or ($num -ge $lo)) -and (($null -eq $hi) -or ($num -le $hi))
                    # the scripts' [int] parameters: nothing outside Int32
                    if ($kind -ne 'float' -and ($num -lt -2147483648 -or $num -gt 2147483647)) { $inRange = $false }
                    if ($inRange) {
                        $ok = $true
                        if ($kind -eq 'float') { $checked = $num }
                        elseif ($kind -eq 'int') { $checked = [int]$v }
                        else { $checked = ([int]$v).ToString([System.Globalization.CultureInfo]::InvariantCulture) }
                    }
                }
            } elseif ($kind -eq 'bool') {
                $expected = 'true or false'
                if ($v -is [bool]) { $ok = $true; $checked = $v }
            } elseif ($kind -eq 'name') {
                $expected = 'a folder or file name (no path separators)'
                if ($v -is [string]) {
                    $s = $v.Trim()
                    if ($s -ne '' -and $s -ne '.' -and $s -ne '..' -and $s.IndexOfAny([char[]]'/\:*?"<>|') -lt 0) { $ok = $true; $checked = $s }
                }
            } elseif ($kind -eq 'path?') {
                $expected = 'a path, or null'
                if ($null -eq $v) { $ok = $true; $checked = '' }
                elseif ($v -is [string] -and $v.Trim() -ne '') { $ok = $true; $checked = $v.Trim() }
            }
            if ($ok) {
                $result.Values[$name] = $checked
            } else {
                $shown = 'null'
                if ($null -ne $v) { $shown = ConvertTo-Json -InputObject $v -Compress -Depth 2 }
                $result.Warnings += "$name = $shown is not valid ($expected); the script's value is kept"
            }
        }
    } catch {
        $result.Values = @{}
        $result.Warnings = @("$($result.Path): not read ($($_.Exception.Message)); no setting changed")
    }
    return $result
}

# Console output as UTF-8 when it goes to a pipe (the wizard reads it). Without this the
# OEM code page turned every Japanese folder name into '?' on screen -- the log file,
# written as UTF-8, was always right. Only when redirected: an interactive console
# keeps its own code page.
if ([Console]::IsOutputRedirected) { try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { } }
# exiftool gets its argfile on stdin (`-@ -`): an argfile PATH on its command line is read in
# the ANSI code page and breaks for a %TEMP% outside it (audit round 27). PS 5.1 encodes what it
# pipes to a native program with $OutputEncoding, which is us-ascii there.
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
# PS 5.1 cannot enter a start-up folder whose name has "[ ]" (wildcards to it) and
# silently starts in $PSHOME (System32) instead: the log, a relative -InputDir or
# -OutputDir all pointed there. Re-enter the process's real folder, literally.
$startDir = [System.IO.Directory]::GetCurrentDirectory()
if ($PWD.ProviderPath -ne $startDir) { try { Set-Location -LiteralPath $startDir } catch { } }
# The overlay is applied before anything reads a parameter. A -Param given on the command line
# always beats it; an invalid entry never stops the run (Get-SettingsOverlay warns instead).
$script:Overlay = Get-SettingsOverlay -Section 'copy_exif' -Specs $script:OverlaySpecs -ScriptRoot $PSScriptRoot
$script:OverlayApplied = @()
foreach ($overlayName in @($script:Overlay.Values.Keys)) {
    if ($PSBoundParameters.ContainsKey($overlayName)) { continue }
    try {
        Set-Variable -Name $overlayName -Value $script:Overlay.Values[$overlayName]
        $script:OverlayApplied += $overlayName
    } catch {
        $script:Overlay.Warnings += "$overlayName could not be set ($($_.Exception.Message)); the script's value is kept"
    }
}
$script:OverlayInfo = ''
if ($script:OverlayApplied.Count -gt 0) {
    $script:OverlayInfo = "Script settings overlay: $($script:Overlay.Path): $($script:OverlayApplied.Count) value(s): $((@($script:OverlayApplied) | Sort-Object) -join ', ')"
    Write-Host $script:OverlayInfo
}
foreach ($overlayWarning in $script:Overlay.Warnings) { Write-Host "WARNING: settings overlay: $overlayWarning" -ForegroundColor Yellow }
# ------------------------------------------------------------------

# Peak memory of one ImageMagick process per pixel of the main page, measured on an
# 84 MP 16-bit scan (IM 7.1.2 Q16-HDRI): compare 68 B/px, conversion/decode 12.7 B/px.
# Plan AI_tools/261010_Claude_plan_memory-subprocess.md. +~10% margin.
$script:CompareBytesPerPixel = 75
$script:UnknownImagePixels   = 150000000

function Get-AvailableMemoryBytes {
    <#
    .SYNOPSIS
        The smaller of the memory the system can still commit and the physical RAM
        still available (incl. standby), in bytes; $null when either cannot be read.
        CIM class names are not localised (performance-counter names are).
    #>
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        $pm = Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_Memory -ErrorAction Stop
        $commit = [double]$os.FreeVirtualMemory * 1KB
        $phys   = [double]$pm.AvailableBytes
        if ($commit -le 0 -or $phys -le 0) { return $null }
        return [Math]::Min($commit, $phys)
    } catch { return $null }
}

function Get-MaxImagePixels {
    <#
    .SYNOPSIS
        Largest IFD0 width*height among the files, read by exiftool in chunks of 400.
        A file exiftool does not report, or reports without a size, counts as
        $script:UnknownImagePixels: an unknown size must never shrink the estimate.
    #>
    param([string[]]$Paths, [int]$ChunkSize = 400)
    $best = [double]0
    if (-not $Paths -or $Paths.Count -eq 0) { return $best }
    for ($off = 0; $off -lt $Paths.Count; $off += $ChunkSize) {
        $end = [Math]::Min($off + $ChunkSize, $Paths.Count) - 1
        $chunk = @($Paths[$off..$end])
        $seen = @{}
        $argFile = [System.IO.Path]::GetTempFileName()
        try {
            $argLines = @("-charset", "filename=utf8", "-T", "-n", "-Directory", "-FileName",
                          "-IFD0:ImageWidth", "-IFD0:ImageHeight") + $chunk
            [System.IO.File]::WriteAllLines($argFile, $argLines, [System.Text.UTF8Encoding]::new($false))
            # exiftool prints UTF-8 (-charset filename=utf8), but PowerShell decodes a
            # native command's output with [Console]::OutputEncoding -- the OEM code page
            # (850) on a scheduled `cmd /k`. A non-ASCII path then never matched and every
            # file counted as UnknownImagePixels.
            $prevEnc = $null
            try { $prevEnc = [Console]::OutputEncoding; [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
            try {
                $out = [System.IO.File]::ReadAllText($argFile) | exiftool -@ - 2>$null
            } finally {
                if ($prevEnc) { try { [Console]::OutputEncoding = $prevEnc } catch { } }
            }
            foreach ($line in @($out)) {
                $cols = "$line" -split "`t"
                if ($cols.Count -lt 4) { continue }
                $full = (Join-Path $cols[0] $cols[1]).Replace('/', '\').ToLowerInvariant()
                $w = 0; $h = 0
                if ([int]::TryParse($cols[2].Trim(), [ref]$w) -and [int]::TryParse($cols[3].Trim(), [ref]$h)) {
                    $seen[$full] = [double]$w * [double]$h
                }
            }
        } finally {
            Remove-Item $argFile -Force -ErrorAction SilentlyContinue
        }
        foreach ($p in $chunk) {
            $px = $seen[$p.Replace('/', '\').ToLowerInvariant()]
            if (-not $px -or $px -le 0) { $px = [double]$script:UnknownImagePixels }
            if ($px -gt $best) { $best = $px }
        }
    }
    return $best
}

function Get-MemoryCappedWorkers {
    <#
    .SYNOPSIS
        $Requested lowered so that many parallel ImageMagick workers fit in
        $Fraction of the free memory (Get-AvailableMemoryBytes). Logs the estimate;
        -Quiet logs only when it lowers the count. 0/negative fraction = no cap.
    #>
    param([int]$Requested, [string[]]$Paths, [double]$BytesPerPixel, [double]$Fraction, [switch]$Quiet)
    if ($Fraction -le 0 -or $Requested -le 1 -or -not $Paths -or $Paths.Count -eq 0) { return $Requested }
    $avail = Get-AvailableMemoryBytes
    if ($null -eq $avail) {
        Write-Log "Memory: could not read the available memory -- workers not capped" "WARN"
        return $Requested
    }
    $px  = Get-MaxImagePixels -Paths $Paths
    $per = $px * $BytesPerPixel
    $cap = [Math]::Max(1, [int][Math]::Floor($avail * $Fraction / $per))
    $w   = [Math]::Min($Requested, $cap)
    if (-not $Quiet -or $cap -lt $Requested) {
        Write-Log ("Memory: ~{0:N1} GB per worker ({1:N0} MP) | {2:N1} GB available | workers {3}" -f ($per / 1GB), ($px / 1e6), ($avail / 1GB), $w)
    }
    if ($cap -lt $Requested) {
        Write-Log ("Workers reduced from {0} to {1}: {0} ImageMagick processes at ~{2:N1} GB each would not fit (WorkerMemoryFraction={3})" -f $Requested, $w, ($per / 1GB), $Fraction) "WARN"
    }
    return $w
}


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
# [System.IO.Path], not Join-Path: PS 5.1 reads "[ ]" in the current folder as a
# wildcard and the script died here, before Write-Log existed.
$logDir     = [System.IO.Path]::Combine($PWD.ProviderPath, "Logs", $scriptName)
[System.IO.Directory]::CreateDirectory($logDir) | Out-Null
$logFile    = [System.IO.Path]::Combine($logDir, "$(Get-Date -Format 'yyyyMMdd_HHmmss').log")

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
$script:multiTotal    = 0
$script:multiPagePaths = [System.Collections.Concurrent.ConcurrentBag[string]]::new()
# Destinations already claimed in this run (only used with -OutputDir, where every
# session writes into the same folder). Shared across folders on purpose.
$script:claimedDest   = @{}

function Process-Line {
    param([string]$line)
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

function Invoke-ExifTool {
    <#
    .SYNOPSIS
        Runs exiftool with its arguments routed through a UTF-8 `-@` argfile read on stdin and
        returns @{ ExitCode = [int]; Output = $null/string/array }.

    .DESCRIPTION
        The main copy/probe/restore calls used to pass file names as raw argv, so a non-ASCII
        name reached exiftool in the console code page and could not be found. Every invocation
        now goes through a per-call argfile: `-charset filename=utf8` first, then one argument
        per line (options and operands on their own lines, paths as-is), UTF-8 without BOM
        (WriteAllText's default encoding) -- the same pattern compress_tiff_zip.ps1 uses for
        every exiftool call. The argfile's CONTENT is piped to `exiftool -@ -`: its PATH on the
        command line is read in the ANSI code page and breaks for a %TEMP% outside it (audit
        round 27). -MergeStderr folds stderr into Output, for the callers whose
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
            $out = [System.IO.File]::ReadAllText($argFile) | exiftool -@ - 2>&1
        } else {
            $out = [System.IO.File]::ReadAllText($argFile) | exiftool -@ - 2>$null
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
        [System.IO.File]::WriteAllText($argMeta, "-charset`nfilename=utf8`n-q`n-q`n-all:all`n-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet`n-o`n$metaBackup`n$SrcPath`n")
        [System.IO.File]::ReadAllText($argMeta) | exiftool -@ - | Out-Null
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

function New-TempSiblingPath {
    <#
    .SYNOPSIS
        A temporary name next to $Path (same folder, so same volume): <Path>.<run id>_<12 hex>.tif.
        $script:leftoverNamePattern matches it, so a copy an interrupted run leaves behind is
        never taken for a photo (nor for a stranded original).
    #>
    param([string]$Path)
    return "$Path.$($script:runStagingId)_$([guid]::NewGuid().ToString('N').Substring(0, 12)).tif"
}

function Move-FileIntoPlace {
    <#
    .SYNOPSIS
        Puts $From at $To so that $To is never a half-written file. Returns $null or an error.

    .DESCRIPTION
        Audit round 26. A move between two volumes is a copy then a delete, and Move-Item
        -Force over an existing $To wrote the copy straight into it: a run killed mid-copy (a
        cross-drive -StagingDir: mode 8 replacing an original, mode 9's staged output) left a
        truncated file where the only copy of the image had been. jxl-photo's rule: the
        irreversible step comes last, as an atomic rename on the destination volume. So a
        file from another volume is first copied to a temp sibling of $To (size checked), and
        only that sibling is renamed over $To. The source is removed after the rename.
    #>
    param([string]$From, [string]$To)
    $fromRoot = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($From))
    $toRoot   = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($To))
    # Audit round 28: Move-FileReplacing, never Move-Item -Force -- that deletes $To first and
    # renames after, so a kill in between left the place of the photo empty.
    if ($fromRoot -ieq $toRoot) {
        try {
            Move-FileReplacing -From $From -To $To
            return $null
        } catch { return "move failed: $($_.Exception.Message)" }
    }
    $tmp = New-TempSiblingPath -Path $To
    try {
        $size = (Get-Item -LiteralPath $From).Length
        Copy-Item -LiteralPath $From -Destination $tmp -ErrorAction Stop
        if ((Get-Item -LiteralPath $tmp).Length -ne $size) {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
            return "size mismatch after copy"
        }
        Move-FileReplacing -From $tmp -To $To
    } catch {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        return "copy failed: $($_.Exception.Message)"
    }
    Remove-Item -LiteralPath $From -Force -ErrorAction SilentlyContinue
    return $null
}

function Move-FileReplacing {
    <#
    .SYNOPSIS
        Audit round 28. Renames $From onto $To, replacing $To in one step: there is never a
        moment with nothing at $To. Throws on failure. Both paths must be on one volume.

    .DESCRIPTION
        Move-Item -Force over an existing file DELETES the destination and then renames, on
        PowerShell 5.1 and 7 alike: a swap loop saw the destination missing 4792 times in
        60764 looks. A kill or a power cut between the two left the place of a photo empty,
        with its verified replacement under a leftover name every later run ignores.
        MoveFileEx(REPLACE_EXISTING) is one rename: [System.IO.File]::Move(a, b, $true) on
        PowerShell 7, and Microsoft.VisualBasic's FileSystem.MoveFile(a, b, $true) -- which
        calls MoveFileEx with that flag, and handles paths over 260 characters -- on 5.1,
        where File.Move has no overwrite overload. 0 misses in 39766 looks.

    .NOTES
        Duplicated in compress_tiff_zip.ps1 and both copy_exif scripts; keep them identical.
        Re-inject into -Parallel runspaces with:
            ${function:Move-FileReplacing} = $using:MoveReplaceFnDef
    #>
    param([string]$From, [string]$To)

    # [System.IO.File] resolves a relative path against the PROCESS directory, not $PWD
    $From = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($From)
    $To = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($To)
    $move3 = [System.IO.File].GetMethod('Move', [type[]]@([string], [string], [bool]))
    if ($move3) {
        [System.IO.File]::Move($From, $To, $true)
        return
    }
    if (-not ('Microsoft.VisualBasic.FileIO.FileSystem' -as [type])) { Add-Type -AssemblyName Microsoft.VisualBasic }
    [Microsoft.VisualBasic.FileIO.FileSystem]::MoveFile($From, $To, $true)
}

function Get-TiffSampleLayout {
    <#
    .SYNOPSIS
        Audit round 28. The sample layout of every page of a TIFF, read with exiftool:
        @{ Ok = [bool]; Pages = [string[]] }, one "<class>/<spp>/<bits>/<alpha>" per IFD in
        page order. class: gray (Photometric 0/1), rgb (2, and 6 = YCbCr, which decodes to
        RGB), else "p<n>". alpha: the ExtraSamples values that are alpha (1 associated,
        2 unassociated); 0 = unspecified (a scanner's IR channel) and an absent tag both count
        as no alpha.

    .DESCRIPTION
        ImageMagick's TIFF writer picks the output layout from the PIXELS, not from the source
        tags: an RGB file whose every pixel has R=G=B (black-and-white film scanned in RGB)
        was written as a ONE-channel grayscale TIFF with the RGB ICC profile still embedded,
        and a VueScan RGB+IR file in the same case came out with 2 samples and half of every
        row lost. The pixel gates decode both files through magick, where gray and neutral RGB
        are equal, so every one of them said OK. Get-MagickKeepTypeArgs stops the rewrite;
        Test-TiffSampleLayoutKept is the gate that sees it when it happens anyway.
        FAIL-CLOSED: any read or parse failure returns Ok = $false.

    .NOTES
        Duplicated in compress_tiff_zip.ps1 and both copy_exif scripts; keep them identical.
        Re-inject into -Parallel runspaces with:
            ${function:Get-TiffSampleLayout} = $using:SampleLayoutFnDef
    #>
    param([string]$Path)

    $fail = @{ Ok = $false; Pages = @() }
    $argRead = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($argRead, "-charset`nfilename=utf8`n-a`n-G1`n-s`n-s`n-n`n-SamplesPerPixel`n-PhotometricInterpretation`n-ExtraSamples`n-BitsPerSample`n$Path`n")
        $readOut = @([System.IO.File]::ReadAllText($argRead) | exiftool -@ - 2>$null)
        if ($LASTEXITCODE -ne 0) { return $fail }
    } catch {
        return $fail
    } finally {
        Remove-Item $argRead -Force -ErrorAction SilentlyContinue
    }

    $ifds = @{}
    foreach ($line in $readOut) {
        if ("$line" -match '^\[IFD(\d+)\]\s+(SamplesPerPixel|PhotometricInterpretation|ExtraSamples|BitsPerSample):\s*(.*?)\s*$') {
            $i = [int]$Matches[1]
            if (-not $ifds.ContainsKey($i)) { $ifds[$i] = @{} }
            $ifds[$i][$Matches[2]] = $Matches[3]
        }
    }
    $pages = @()
    for ($i = 0; $ifds.ContainsKey($i); $i++) {
        $t = $ifds[$i]
        if (-not $t.ContainsKey('PhotometricInterpretation')) { return $fail }
        $ph = "$($t['PhotometricInterpretation'])".Trim()
        $class = "p$ph"
        if ($ph -eq '0' -or $ph -eq '1') { $class = 'gray' }
        elseif ($ph -eq '2' -or $ph -eq '6') { $class = 'rgb' }
        $spp = '1'
        if ($t.ContainsKey('SamplesPerPixel')) { $spp = "$($t['SamplesPerPixel'])".Trim() }
        $bits = '1'
        if ($t.ContainsKey('BitsPerSample')) { $bits = ("$($t['BitsPerSample'])".Trim() -split '\s+') -join ',' }
        $alpha = @()
        if ($t.ContainsKey('ExtraSamples')) {
            foreach ($v in ("$($t['ExtraSamples'])".Trim() -split '\s+')) {
                if ($v -eq '1' -or $v -eq '2') { $alpha += $v }
            }
        }
        $pages += "$class/$spp/$bits/$($alpha -join ',')"
    }
    # A gap in the IFD numbering (or no IFD0 at all) is not a layout this can vouch for
    if ($pages.Count -eq 0 -or $pages.Count -ne $ifds.Count) { return $fail }
    return @{ Ok = $true; Pages = $pages }
}

function Get-MagickKeepTypeArgs {
    <#
    .SYNOPSIS
        Audit round 28. The magick write options that keep the source's channel layout, from
        Get-TiffSampleLayout pages: "-type TrueColor" when every page is RGB without alpha (a
        scanner IR channel included: it comes back as ExtraSamples=0, samples unchanged),
        "-type TrueColorAlpha" when every page is RGB with alpha, plus
        "-define tiff:alpha=associated" when all that alpha is associated (magick writes it
        back as unassociated otherwise). Anything else -- a gray page, a mixed file -- returns
        no options: -type applies to every page of the write, so a gray IR page next to a
        neutral RGB page cannot be kept both ways, and Test-TiffSampleLayoutKept refuses the
        result instead. Callers wrap the call in @().

    .NOTES
        Duplicated in compress_tiff_zip.ps1 and both copy_exif scripts; keep them identical.
        Re-inject into -Parallel runspaces with:
            ${function:Get-MagickKeepTypeArgs} = $using:KeepTypeFnDef
    #>
    param([string[]]$Pages)

    if (-not $Pages -or $Pages.Count -eq 0) { return @() }
    $withAlpha = 0
    $associatedOnly = $true
    foreach ($p in $Pages) {
        $parts = "$p" -split '/'
        if ($parts[0] -ne 'rgb') { return @() }
        $alpha = ''
        if ($parts.Count -ge 4) { $alpha = $parts[3] }
        if ($alpha) {
            $withAlpha++
            foreach ($a in ($alpha -split ',')) { if ($a -ne '1') { $associatedOnly = $false } }
        }
    }
    if ($withAlpha -eq 0) { return @('-type', 'TrueColor') }
    if ($withAlpha -ne $Pages.Count) { return @() }
    $typeArgs = @('-type', 'TrueColorAlpha')
    if ($associatedOnly) { $typeArgs += @('-define', 'tiff:alpha=associated') }
    return $typeArgs
}

function Test-AssociatedAlphaRewritable {
    <#
    .SYNOPSIS
        Audit round 28. $false when a page of the source carries ASSOCIATED alpha and the image
        is not fully opaque: ImageMagick un-premultiplies on read and re-premultiplies on write,
        and the alpha samples come back changed (1791 of a real VueScan scan's), even with
        -define tiff:alpha=associated. Neither the layout gate nor a pixel compare through magick
        sees it, so such a file is refused. Opaque associated alpha comes back identical.
        Unreadable fails CLOSED.

    .NOTES
        Duplicated in compress_tiff_zip.ps1 and both copy_exif scripts; keep them identical.
        Re-inject into -Parallel runspaces with:
            ${function:Test-AssociatedAlphaRewritable} = $using:AssocAlphaFnDef
        (Invoke-MagickWithTimeout must be injected too.)
    #>
    param([string]$Path, [string[]]$Pages, [int]$TimeoutSec = 30)

    $assoc = $false
    foreach ($p in $Pages) {
        $parts = "$p" -split '/'
        if ($parts.Count -ge 4 -and (($parts[3] -split ',') -contains '1')) { $assoc = $true }
    }
    if (-not $assoc) { return $true }
    $r = Invoke-MagickWithTimeout -Arguments @("identify", "-format", "%[opaque]`n", $Path) -TimeoutSec $TimeoutSec
    if ($r.TimedOut -or $r.ExitCode -ne 0) { return $false }
    $lines = @($r.Output | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") })
    if ($lines.Count -eq 0) { return $false }
    foreach ($l in $lines) { if ("$l".Trim() -ne 'True') { return $false } }
    return $true
}

function Test-TiffSampleLayoutKept {
    <#
    .SYNOPSIS
        Audit round 28. $true only when $DstPath has the sample layout $SrcPages describe:
        the same number of pages, and per page the same color class, samples per pixel, bits
        per sample and alpha kind. -MainPageOnly compares page 0 alone (the -GenerateThumbnail
        path builds a new page layout on purpose). Also the page-count gate of modes 0/9 and
        legacy, which used to have none. Anything unreadable fails CLOSED.

    .NOTES
        Duplicated in compress_tiff_zip.ps1 and both copy_exif scripts; keep them identical.
        Re-inject into -Parallel runspaces with:
            ${function:Test-TiffSampleLayoutKept} = $using:LayoutKeptFnDef
        (Get-TiffSampleLayout must be injected too.)
    #>
    param([string[]]$SrcPages, [string]$DstPath, [switch]$MainPageOnly)

    if (-not $SrcPages -or $SrcPages.Count -eq 0) { return $false }
    $dst = Get-TiffSampleLayout -Path $DstPath
    if (-not $dst.Ok) { return $false }
    if ($MainPageOnly) { return ("$($dst.Pages[0])" -ceq "$($SrcPages[0])") }
    if ($dst.Pages.Count -ne $SrcPages.Count) { return $false }
    for ($i = 0; $i -lt $SrcPages.Count; $i++) {
        if ("$($dst.Pages[$i])" -cne "$($SrcPages[$i])") { return $false }
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
        $readOut = [System.IO.File]::ReadAllText($argRead) | exiftool -@ - 2>$null
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
        $out = [System.IO.File]::ReadAllText($argFile) | exiftool -@ - 2>&1
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
$script:ExifToolFnDef      = ${function:Invoke-ExifTool}.ToString()
$script:SampleLayoutFnDef  = ${function:Get-TiffSampleLayout}.ToString()
$script:KeepTypeFnDef      = ${function:Get-MagickKeepTypeArgs}.ToString()
$script:LayoutKeptFnDef    = ${function:Test-TiffSampleLayoutKept}.ToString()
$script:AssocAlphaFnDef    = ${function:Test-AssociatedAlphaRewritable}.ToString()

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
    # Audit round 28: a temp sibling (<name>.<8 hex>_<12 hex>.tif) or a staged file a killed run
    # left behind is not a photo -- the same names compress_tiff_zip.ps1 ignores.
    $tiffFiles = @($allFiles | Where-Object { $_.Extension -match '^\.(tif|tiff)$' -and $_.Name -notmatch '(?i)^[0-9a-f]{8}_[0-9a-f]{12}_.+\.tiff?$|(?i)\.[0-9a-f]{8}_[0-9a-f]{12}\.tiff?$' })

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

        # -Parallel only; the cap is about how many ImageMagick compares run at once.
        # -DryRun never starts one, so the cap is skipped there.
        $cxWorkers = $Workers
        if (-not $DryRun) {
            $cxWorkers = Get-MemoryCappedWorkers -Requested $Workers -Paths @($pairs | ForEach-Object { $_.Tiff }) -BytesPerPixel $script:CompareBytesPerPixel -Fraction $WorkerMemoryFraction -Quiet
        }

        $results = $pairs | ForEach-Object -ThrottleLimit $cxWorkers -Parallel {
            $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
            $p         = $_
            # Functions from the parent scope are not visible inside -Parallel runspaces
            ${function:Invoke-MagickWithTimeout}      = $using:MagickTimeoutFnDef
            ${function:ConvertTo-MagickPathSpec}     = $using:MagickPathFnDef
            ${function:Get-TiffPageCount}            = $using:PageCountFnDef
            ${function:Test-PixelIdentical}          = $using:PixelCompareFnDef
            ${function:Test-TiffHasOnlySubfilePages} = $using:TestSubfileFnDef
            ${function:Restore-TiffSubfileTypes}     = $using:RestoreSubfileFnDef
            ${function:Backup-TiffMetadata}          = $using:MetaBackupFnDef
            ${function:Invoke-ExifTool}              = $using:ExifToolFnDef
            ${function:Get-TiffSampleLayout}         = $using:SampleLayoutFnDef
            ${function:Get-MagickKeepTypeArgs}       = $using:KeepTypeFnDef
            ${function:Test-TiffSampleLayoutKept}    = $using:LayoutKeptFnDef
            ${function:Test-AssociatedAlphaRewritable} = $using:AssocAlphaFnDef
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
                return @{ Result = "MISS | $($p.TifName) | no matching JPEG or RAF (base: $($p.TifBase))"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
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
                # Camera EXIF only (the Exif IFD): -EXIF:all includes IFD0 -- the size and
                # resolution every TIFF has -- and skipped every TIFF (round 23).
                $exifProbeRes = Invoke-ExifTool -Arguments @('-q','-q','-G1','-s','-ExifIFD:all',$p.Tiff)
                $exifProbeExit = $exifProbeRes.ExitCode
                $firstExif = @($exifProbeRes.Output | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") })[0]
                # Fail-closed: an unreadable TIFF must not be overwritten by the copy below
                if ($exifProbeExit -ne 0) { return @{ Result = "ERROR (exiftool EXIF check) | $($p.TifName) | cannot inspect TIFF, not overwriting"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false } }
                if ($firstExif) { return @{ Result = "SKIP (already has EXIF) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false } }
            }

            if ($dryL) {
                $zipInfo = if ($compressL) { " + ZIP" } else { "" }
                return @{ Result = "DRY (EXIF$zipInfo) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$rawSuffix"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
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
                return @{ Result = "ERROR (exiftool EXIF) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            if (-not $compressL) {
                $copyNote = if ($tiffCopied) { " -> $finalDirL" } else { "" }
                return @{ Result = "$okPrefix | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$copyNote$heurSuffix$rawSuffix"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
            }

            # -IFD0:Compression, and collapsed to the first line below: exiftool can print one
            # line per IFD and `-match` on an ARRAY is truthy when ANY element matches, so an
            # uncompressed main image whose thumbnail page happened to be compressed was
            # reported "already ZIP" and never compressed. compress_tiff_zip.ps1 fixed this in
            # four places; these two backends were the copies left behind.
            $compRes = Invoke-ExifTool -Arguments @('-s','-s','-s','-IFD0:Compression',$tiffTarget)
            if ($compRes.ExitCode -ne 0 -or -not $compRes.Output) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (exiftool check) | $($p.TifName) | cannot detect compression"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }
            $comp = "$(@($compRes.Output)[0])".Trim()
            if ($comp -match $(if ($skipLzwL) { 'Deflate|ZIP|Adobe|LZW' } else { 'Deflate|ZIP|Adobe' })) {
                return @{ Result = "$(if ($heurMatch) { "WARN (heuristic JPEG match, already $comp -- ZIP skipped)" } else { "OK+SKIP-ZIP ($comp)" }) | $($p.TifName)$heurSuffix$rawSuffix"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = $false }
            }

            $runIdL = $using:runStagingIdCapture
            # Run-scoped prefix so the interrupt trap only cleans up this run's staged files
            $stagingName = "${runIdL}_$([guid]::NewGuid().ToString('N').Substring(0, 12))_$($p.DestName)"
            $writeDst = Join-Path $writeDirL $stagingName
            $finalDst = Join-Path $finalDirL $p.DestName

            if ((Test-Path -LiteralPath $finalDst) -and -not $overL -and ($finalDst -ine $p.Tiff) -and -not $tiffCopied) {
                # No cleanup here: the guard above already requires -not $tiffCopied
                return @{ Result = "OK+SKIP-ZIP (exists) | $($p.TifName)$rawSuffix"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            # Audit round 28: the source layout picks the -type that keeps it, and is the
            # reference for the layout gate after the write (see the PS5 twin).
            $srcLayout = Get-TiffSampleLayout -Path $tiffTarget
            if (-not $srcLayout.Ok) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (sample layout unreadable) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }
            $typeArgs = @(Get-MagickKeepTypeArgs -Pages $srcLayout.Pages)
            if (-not (Test-AssociatedAlphaRewritable -Path $tiffTarget -Pages $srcLayout.Pages -TimeoutSec $magickTimeoutSec)) {
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (associated alpha that is not opaque: ImageMagick would change it - refused) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            # Input literal, OUTPUT escaped: a '%' in the name sends the output somewhere
            # else while magick still exits 0 (see ConvertTo-MagickPathSpec).
            $magickErr = magick -quiet $tiffTarget -compress zip @typeArgs (ConvertTo-MagickPathSpec $writeDst) 2>&1
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
            if (-not (Test-TiffSampleLayoutKept -SrcPages $srcLayout.Pages -DstPath $writeDst)) {
                Remove-Item -LiteralPath $writeDst -Force -ErrorAction SilentlyContinue
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (sample layout changed: $($srcLayout.Pages -join ' ') - original untouched) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }
            if (-not (Test-PixelIdentical -SrcPath $tiffTarget -DstPath $writeDst -TimeoutSec $magickTimeoutSec -LoadFactor $using:cxWorkers)) {
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
                # IPTC:CodedCharacterSet is not part of -all:all: without it UTF-8 IPTC text came
                # back as '?' (audit round 28).
                $restoreRes = Invoke-ExifTool -Arguments @('-q','-overwrite_original','-tagsfromfile',$tiffTagSource,'-all:all','-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet','-unsafe',$writeDst) -MergeStderr
                $restoreOut = "$($restoreRes.Output)"
                $restoreExit = $restoreRes.ExitCode
                if ($restoreOut -match 'Error opening file') { $restoreExit = 1 }
            }
            if ($tiffMeta.Path) { Remove-Item -LiteralPath $tiffMeta.Path -Force -ErrorAction SilentlyContinue }
            # The staging mapping is handed back ONLY after every restore gate below has passed:
            # the move loop moves whatever is registered, so a staged ZIP whose metadata restore
            # failed was moved ON TOP of the EXIF-bearing original and reported as a mere WARN
            # (exit 0). On failure the staged file is deleted, the original stays untouched and
            # the file counts as an error.
            if ($restoreExit -ne 0) {
                Remove-Item -LiteralPath $writeDst -Force -ErrorAction SilentlyContinue
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (exiftool metadata restore failed - original untouched) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; FinalDst = $finalDst; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            if (-not (Restore-TiffSubfileTypes -SrcPath $tiffTarget -DstPath $writeDst -TimeoutSec $magickTimeoutSec)) {
                Remove-Item -LiteralPath $writeDst -Force -ErrorAction SilentlyContinue
                if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
                return @{ Result = "ERROR (subfiletype restore failed - original untouched) | $($p.TifName)"; StagingName = $null; OriginalName = $p.TifName; FinalDst = $finalDst; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
            }

            if ($tiffCopied) { Remove-Item -LiteralPath $destTiff -Force -ErrorAction SilentlyContinue }
            return @{ Result = "$(if ($heurMatch) { 'WARN (heuristic JPEG match)' } else { 'OK+ZIP' }) | $($p.TifName) <= $([IO.Path]::GetFileName($p.Jpeg))$heurSuffix$rawSuffix"; StagingName = $stagingName; OriginalName = $p.TifName; FinalDst = $finalDst; SrcPath = $p.Tiff; CopiedTiffPath = $copiedTiffPath; IsIntermediate = ($null -ne $copiedTiffPath) }
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
                if ((Test-Path -LiteralPath $stagePath) -and $stagePath -ine $destPath) {
                    try {
                        # In-place, $destPath IS $tif.FullName: the original mtime must be
                        # captured BEFORE the move -- reading it afterwards read the staged
                        # file's own fresh timestamp and preserved nothing.
                        $origMtime = $null
                        try { $origMtime = (Get-Item -LiteralPath $tif.FullName).LastWriteTime } catch { }
                        $stageSize = (Get-Item -LiteralPath $stagePath).Length
                        # Audit round 28: Move-FileIntoPlace, not Move-Item -Force -- see the PS5 twin.
                        $mvErr = Move-FileIntoPlace -From $stagePath -To $destPath
                        if ($mvErr) { throw $mvErr }
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

# -- Entry point -----------------------------------------------------
if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir.TrimEnd('\', '/'))
}

# A bad path used to fall through to "No TIFFs found" and exit 0, so the wizard could not tell
# a typo from an empty folder -- and convert_tiff.py gates Step 2 of workflow 4 on this exit
# code, so a Copy EXIF that touched nothing read as success. Same contract as the other backends.
$inputDirs    = @()
$missingRoots = @()
# -InputDirFile (round 22): see compress_tiff_zip.ps1.
$inputDirEntries = @()
if ($InputDirFile) {
    if ($InputDir) {
        Write-Log "ERROR: pass -InputDir or -InputDirFile, not both" "ERROR"
        exit 1
    }
    if (-not (Test-Path -LiteralPath $InputDirFile -PathType Leaf)) {
        Write-Log "ERROR: -InputDirFile not found: $InputDirFile" "ERROR"
        exit 1
    }
    try {
        $inputDirEntries = @(Get-Content -LiteralPath $InputDirFile -Encoding UTF8 -ErrorAction Stop |
                             ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    } catch {
        Write-Log "ERROR: cannot read -InputDirFile: $InputDirFile ($($_.Exception.Message))" "ERROR"
        exit 1
    }
    if ($inputDirEntries.Count -eq 0) {
        Write-Log "ERROR: -InputDirFile lists no folders: $InputDirFile" "ERROR"
        exit 1
    }
} elseif ($InputDir) {
    $inputDirEntries = @($InputDir -split ';')
}
if ($inputDirEntries.Count -gt 0) {
    foreach ($dir in $inputDirEntries) {
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

Write-Log "tiff-workflow $script:ToolkitVersion | copy_exif_to_TIFF_ps7.ps1 | PowerShell $($PSVersionTable.PSVersion)"
if ($script:OverlayInfo) { Write-Log $script:OverlayInfo }
foreach ($overlayWarning in $script:Overlay.Warnings) { Write-Log "settings overlay: $overlayWarning" "WARN" }
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
        # Audit round 28: the tool-output folders are matched as segments BELOW the root (round
        # 14's rule, Test-PathExcluded). The absolute-path -notlike found nothing at all under a
        # root that itself sat below a ZIP or _EXPORT folder.
        $matchingFolders = Get-ChildItem -LiteralPath $root -Directory -Recurse |
                           Where-Object { $_.Name -like "*$folderPatternEsc*" -and -not (Test-PathExcluded -Directory $_.FullName -Root $root -Names @('Logs', 'OLD_TIFFs', 'ZIP', '_EXPORT', 'converted_zip')) }

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