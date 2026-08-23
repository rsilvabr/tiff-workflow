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
    [string]$Format = "jpg",
    [string]$SrgbProfile = "",  # ICC profile used to convert wide-gamut sources; "" = auto-detect
    [string]$ExcludeFolders = ""# ';'-separated folder NAMES to skip during discovery
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

# -- sRGB profile for colour-managed thumbnails --------------------
# `-colorspace sRGB` converts between COLORSPACES, not between ICC profiles: on a TIFF that
# ImageMagick already reads as RGB it is a no-op, and `-strip` then throws the source profile
# away. A ProPhoto/AdobeRGB export therefore produced a thumbnail whose numbers were
# reinterpreted as sRGB -- oversaturated and hue-shifted (measured 10% RMSE against a
# colour-managed conversion; the tagged and untagged sources gave byte-identical output).
# `-profile <icc>` does the real conversion. On a source with no embedded profile ImageMagick
# treats it as "assign", so untagged files behave exactly as before.
function Resolve-SrgbProfile {
    param([string]$Explicit)
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit -PathType Leaf) { return (Resolve-Path -LiteralPath $Explicit).Path }
        return $null   # caller reports it; an explicit bad path must not be silently ignored
    }
    $winDir = if ($env:SystemRoot) { $env:SystemRoot } else { "C:\Windows" }
    foreach ($cand in @(
        (Join-Path $winDir "System32\spool\drivers\color\sRGB Color Space Profile.icm"),
        (Join-Path $winDir "System32\spool\drivers\color\sRGB_ICC_v4_Appearance.icc")
    )) {
        if (Test-Path -LiteralPath $cand -PathType Leaf) { return $cand }
    }
    return $null
}

$script:SrgbProfilePath = Resolve-SrgbProfile -Explicit $SrgbProfile
if ($SrgbProfile -and -not $script:SrgbProfilePath) {
    Write-Log "ERROR: -SrgbProfile not found: $SrgbProfile" "ERROR"
    exit 1
}
if (-not $script:SrgbProfilePath) {
    Write-Log "WARN: no sRGB ICC profile found -- wide-gamut sources (ProPhoto, AdobeRGB) will produce thumbnails with shifted colours. Pass -SrgbProfile <path.icc> to fix." "WARN"
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

function ConvertTo-MagickPathSpec {
    <#
    .SYNOPSIS
        Escapes '%' for the filename SPECS ImageMagick expands, so a path containing one
        addresses the file the caller meant.

    .DESCRIPTION
        ImageMagick reads printf-style scene specifiers in a filename spec: `50%off.tif`
        writes `500ff.tif` ('%o'), `100%dpi.tif` writes `1000pi.tif`. magick still exits 0,
        so the caller sees a success while the output sits under a name nobody asked for.
        '%%' is the escape and round-trips to a literal '%'.

        WHERE THIS APPLIES, exactly:
          - EVERY output path.
          - An INPUT that carries a '[n]' page suffix -- appending the suffix turns the
            argument into a filename spec.
        And where it must NOT be used:
          - A PLAIN input path (here: -Page all, which passes no suffix). ImageMagick opens
            that name literally, so an escaped name is simply not found.

    .NOTES
        Re-inject into -Parallel runspaces with:
            ${function:ConvertTo-MagickPathSpec} = $using:MagickPathFnDef
        Duplicated across compress_tiff_zip.ps1, copy_exif_to_TIFF_ps*.ps1 and this script
        so each stays self-contained. Keep the implementations identical.
    #>
    param([string]$Path)

    if ([string]::IsNullOrEmpty($Path)) { return $Path }
    return $Path.Replace('%', '%%')
}
$script:MagickPathFnDef = ${function:ConvertTo-MagickPathSpec}.ToString()

function Test-PathExcluded {
    <#
    .SYNOPSIS
        True when any folder segment BELOW $Root matches one of $Names (case-insensitive).

    .DESCRIPTION
        The tool-output filter (ZIP, OLD_TIFFs, converted_zip, _EXPORT) used to be a regex
        over the ABSOLUTE directory, so it also matched every folder ABOVE the one the user
        pointed at: an archive under `D:\Backup\zip\2019\` produced "No TIFF files found"
        and exit 0, and pointing the tool straight AT a folder named `ZIP` could never work.
        Same bug the three compression/EXIF backends fixed in audit round 14; this script
        and the Python diagnose workflow were the copies left behind.

        Matching relative to the root keeps the intent (skip the tool's own output nested
        below the tree being scanned) and makes an explicit -InputDir win.

    .NOTES
        Used from script scope only (discovery), so no runspace re-injection is needed.
        Duplicated across compress_tiff_zip.ps1, copy_exif_to_TIFF_ps*.ps1 and this script
        so each stays self-contained. Keep the implementations identical.
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
        # from the root. Fall back to the whole path: in doubt, skip the file.
        $rel = $dirN
    }

    if (-not $rel) { return $false }
    foreach ($seg in ($rel -split '\\')) {
        if ($seg -and ($seg -in $Names)) { return $true }
    }
    return $false
}

function Test-ThumbExists {
    <#
    .SYNOPSIS
        True when a thumbnail for $destPath is already on disk.
        With -Page all and a single-frame format ImageMagick writes name-0.jpg,
        name-1.jpg, ... so testing only the unsuffixed path never matched and every
        run regenerated everything.

    .PARAMETER FrameNames
        Only look for the `name-0.jpg, name-1.jpg, ...` frame set when the caller asked for
        -Page all. It used to be checked unconditionally, so a `-Page all` run left `-Page 0`
        permanently unrunnable: the frames counted as "the thumbnail exists", every file came
        back SKIP (exists), and `name.jpg` could only be produced by deleting the frames by hand.
    #>
    param([string]$destPath, [bool]$FrameNames = $false)

    if (Test-Path -LiteralPath $destPath) { return $true }
    if (-not $FrameNames) { return $false }
    $d = [System.IO.Path]::GetDirectoryName($destPath)
    if (-not $d -or -not (Test-Path -LiteralPath $d)) { return $false }
    $b = [System.IO.Path]::GetFileNameWithoutExtension($destPath)
    $e = [System.IO.Path]::GetExtension($destPath)
    # Enumerate and compare the names ordinally rather than passing a pattern to -Filter.
    # -Filter is the FileSystem provider's DOS filter: it understands only '*' and '?' and
    # has NO escape syntax, so the backtick-escaped form [WildcardPattern]::Escape produces
    # matched nothing at all for a basename containing '[' or ']'. The frames were written,
    # reported as "output not created", and regenerated (and re-reported) on every run.
    $prefix = "$b-"
    return (@(Get-ChildItem -LiteralPath $d -File -ErrorAction SilentlyContinue | Where-Object {
        $_.Name.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        $_.Name.EndsWith($e, [System.StringComparison]::OrdinalIgnoreCase) -and
        # Round 16: require digits between prefix and extension. Any `name_thumb-*` used to
        # count as a -Page all frame, so a manually saved name_thumb-old.jpg made every
        # future run report SKIP forever.
        $_.Name.Substring($prefix.Length, $_.Name.Length - $prefix.Length - $e.Length) -match '^\d+$'
    }).Count -gt 0)
}
$script:ThumbExistsFnDef = ${function:Test-ThumbExists}.ToString()

# Every thumbnail this script can produce, in one pattern:
#   name_thumb        plain
#   name_thumb-0      one frame per page (-Page all)
#   name_thumb_v2     collision rename under -OutputDir
#   name_thumb_v2-0   both
# Matching only '_thumb(-\d+)?$' left the _v2 variants out of BOTH the self-exclusion below
# and the -Remove filter, so a renamed .tif thumbnail was rescanned as a source on the next
# run (producing _thumb_v2_thumb) and could never be cleaned up.
$script:ThumbNamePattern = '(?i)_thumb(_v\d+)?(-\d+)?$'

# Folder names skipped during discovery, matched RELATIVE to the root each file was found
# under -- never against the absolute path, which also matched every ancestor (see
# Test-PathExcluded).
#
# `ZIP` and `_EXPORT` used to be on this list and are deliberately NOT any more. They are not
# this tool's leavings: `_EXPORT` is what compress modes 6/7 exist to process, and `ZIP/` is
# where modes 1/3/5/6/7 PUT the compressed library. Excluding them meant "generate thumbnails
# for my compressed library" and "...for my _EXPORT tree" silently did nothing -- the run just
# reported a smaller Found: count, with no flag to override and nothing in the docs.
# What is left really is backup/output nobody wants a thumbnail of.
$script:ToolOutputNames = @('OLD_TIFFs', 'OLD_TIFF', 'converted_zip')

# ';'-separated folder NAMES the caller wants skipped as well, same contract as the other
# three backends (segment match, relative to the root, paths refused).
foreach ($entry in ($ExcludeFolders -split ';')) {
    $entry = $entry.Trim()
    if (-not $entry) { continue }
    if ($entry -match '[\\/]') {
        Write-Host "ERROR: -ExcludeFolders takes folder NAMES, not paths: '$entry'" -ForegroundColor Red
        Write-Host "       Use a ';'-separated list of bare names, e.g. -ExcludeFolders '_EXPORT;temp'" -ForegroundColor Yellow
        exit 1
    }
    $script:ToolOutputNames += $entry
}

# Each file carries the root it was discovered under, so the exclusion can be evaluated
# relative to that root. A file passed directly as -InputDir is its own root: an explicit
# path always wins.
$allFiles = foreach ($root in $inputRoots) {
    $item = Get-Item -LiteralPath $root
    if ($item -is [System.IO.FileInfo]) {
        if ($item.Extension -match '^\.(tif|tiff)$') {
            $item | Add-Member -NotePropertyName 'InputRoot' -NotePropertyValue $item.DirectoryName -Force -PassThru
        }
    } else {
        Get-ChildItem -LiteralPath $root -File -Recurse:$recurseFlag |
            Where-Object { $_.Extension -match '^\.(tif|tiff)$' } |
            ForEach-Object { $_ | Add-Member -NotePropertyName 'InputRoot' -NotePropertyValue $root -Force -PassThru }
    }
}

# Round 16: Sort-Object -Unique both dedupes and orders.
#  - Dedupe: a repeated -InputDir entry (A;A) or roots nested under a -Recursive root
#    enumerate the same source twice, and the second copy was thumbnailed again as _v2.
#  - Order: Get-ChildItem order is filesystem-dependent, so which of two same-name sources
#    claimed photo_thumb.jpg vs photo_thumb_v2.jpg varied between runs.
$allFiles = @($allFiles | Sort-Object FullName -Unique)
$allTiffCount = $allFiles.Count
$files = @($allFiles | Where-Object {
    -not (Test-PathExcluded -Directory $_.DirectoryName -Root $_.InputRoot -Names $script:ToolOutputNames) -and
    $_.BaseName -notmatch $script:ThumbNamePattern
})
# Say what was left out. The exclusion used to be silent, so a run that skipped every file in
# an _EXPORT tree looked exactly like a folder that had no TIFFs in it.
$excludedCount = $allTiffCount - @($files).Count
if ($excludedCount -gt 0) {
    Write-Log "Excluded $excludedCount file(s): existing thumbnails, or under $($script:ToolOutputNames -join '; ')"
}
# $script:, not a bare $total: Process-Results reads it, and a function only sees a bare
# script variable through dynamic scoping -- which stops working under Set-StrictMode or if
# the function is ever dot-sourced or injected into a runspace.
$script:total = $files.Count

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
            # Generation resolves a relative -OutputDir per FILE directory
            # (Join-Path $f.DirectoryName $OutputDir), so under -Recursive the thumbnails sit in
            # <any subfolder>\<OutputDir>, not only <root>\<OutputDir>. Looking only at the
            # latter meant -Remove with the exact flags that created them found nothing.
            $suffix = $OutputDir.TrimEnd('\', '/').Replace('/', '\')
            # Round 16: used to be a -like pattern over the path tail, so a '[' in -OutputDir
            # became a wildcard character class and matched the wrong folders (and never the
            # literal one). Literal ordinal tail match on a segment boundary instead.
            $suffixTail = "\$suffix"
            @(foreach ($r in $inputRoots) {
                Join-Path $r $suffix
                if ($recurseFlag) {
                    Get-ChildItem -LiteralPath $r -Directory -Recurse -ErrorAction SilentlyContinue |
                        Where-Object { $_.FullName.Replace('/', '\').TrimEnd('\').EndsWith($suffixTail, [System.StringComparison]::OrdinalIgnoreCase) } |
                        ForEach-Object { $_.FullName }
                }
            })
        }
    } else {
        $inputRoots
    }

    $thumbs = @(foreach ($root in ($searchRoots | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -File -Recurse:$recurseFlag -ErrorAction SilentlyContinue |
            Where-Object {
                $_.BaseName -match $script:ThumbNamePattern -and
                ($_.Extension.TrimStart('.').ToLowerInvariant() -in $allFormats)
            }
    })

    # Search roots can nest (root\thumbs and root\sub\thumbs), so the same file can be
    # enumerated twice -- deleting it twice would count a phantom error.
    $thumbs = @($thumbs | Sort-Object -Property FullName -Unique)

    $script:total = $thumbs.Count
    Write-Log "Found: $($script:total) thumbnail(s) to remove"
    if ($script:total -eq 0) {
        Write-Log "No thumbnails found in: $(($searchRoots | Select-Object -Unique) -join '; ')" "WARN"
        Write-Log "Log: $logFile"
        if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }
    }

    foreach ($thumb in $thumbs) {
        $script:counterTotal++
        if ($DryRun) {
            $script:skipTotal++
            Write-Log "[$($script:counterTotal)/$($script:total)] DRY-RUN (would remove) | $($thumb.Name)"
            continue
        }
        try {
            Remove-Item -LiteralPath $thumb.FullName -Force
            $script:okTotal++
            Write-Log "[$($script:counterTotal)/$($script:total)] REMOVED | $($thumb.Name)"
        } catch {
            $script:errTotal++
            Write-Log "[$($script:counterTotal)/$($script:total)] ERROR (remove failed) | $($thumb.Name) | $($_.Exception.Message)" "ERROR"
        }
    }

    Write-Log ""
    Write-Log "Done: $($script:okTotal) removed | $($script:skipTotal) skipped | $($script:errTotal) errors | $($script:counterTotal)/$($script:total) processed"
    Write-Log "Log: $logFile"
    if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }
}

if ($script:total -eq 0) {
    Write-Log "No TIFF files found in: $($inputRoots -join '; ')" "WARN"
    Write-Log "Log: $logFile"
    if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }
}

Write-Log "Found: $($script:total) TIFF(s)"
Write-Log "Size: ${Size}px | Format: $Format | Quality: $Quality | Page: $Page"
Write-Log "Mode: $(if ($Remove) { 'REMOVE thumbnails' } else { 'GENERATE thumbnails' })"
# -----------------------------------------------------------------

function Process-Results($lines) {
    foreach ($line in $lines) {
        $script:counterTotal++
        if ($line -match '^OK') {
            $script:okTotal++
            Write-Log "[$($script:counterTotal)/$($script:total)] $line"
        } elseif ($line -match '^SKIP') {
            $script:skipTotal++
            Write-Log "[$($script:counterTotal)/$($script:total)] $line"
        } elseif ($line -match '^ERROR') {
            $script:errTotal++
            Write-Log "[$($script:counterTotal)/$($script:total)] $line" "ERROR"
        } else {
            Write-Log "[$($script:counterTotal)/$($script:total)] $line"
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
        SrgbProfile = $script:SrgbProfilePath
    }
}

# Process tasks
if ($isPS7 -and $effectiveWorkers -gt 1) {
    # Parallel processing
    $results = $tasks | ForEach-Object -Parallel {
        $t = $_
        # Functions from the parent scope are not visible inside -Parallel runspaces
        ${function:Test-ThumbExists} = $using:ThumbExistsFnDef
        ${function:ConvertTo-MagickPathSpec} = $using:MagickPathFnDef
        $name = [System.IO.Path]::GetFileName($t.SrcPath)

        if ((Test-ThumbExists $t.DestPath ($t.Page -eq "all")) -and -not $t.DryRun) {
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
                # A '[n]' suffix turns the input into a filename SPEC, where '%' is expanded;
                # a plain input (-Page all) is opened literally and must stay unescaped.
                $inputWithPage = if ($pageSuffix) { "$(ConvertTo-MagickPathSpec $t.SrcPath)$pageSuffix" } else { $t.SrcPath }
                
                # -profile converts wide-gamut sources through their embedded ICC; -colorspace
                # after it is a no-op for RGB but still normalises grayscale/CMYK sources.
                $magickArgs = @()
                if ($t.SrgbProfile) { $magickArgs += @("-profile", $t.SrgbProfile) }
                $magickArgs += @(
                    "-colorspace", "sRGB",
                    "-strip",
                    "-thumbnail", "$($t.Size)x$($t.Size)>"
                )

                # -quality only where it means JPEG quality. ImageMagick reinterprets it for
                # PNG as zlib level + filter (85 -> level 8, filter 5), so -Quality 85 was
                # quietly choosing a worse PNG compression than the default; TIFF ignores it.
                if ($t.Format -in @("jpg", "jpeg")) {
                    $magickArgs += @("-quality", $t.Quality, "-interlace", "Plane")
                } elseif ($t.Format -in @("tif", "tiff")) {
                    $magickArgs += @("-compress", "zip")
                }
                
                # Output path is always escaped: a '%' in the name would send the thumbnail
                # somewhere else while magick still exits 0 (see ConvertTo-MagickPathSpec).
                $magickArgs += (ConvertTo-MagickPathSpec $t.DestPath)
                
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
                    # Ordinal name match, not -Filter (see Test-ThumbExists): with a '[' in the
                    # basename the escaped pattern matched nothing, so frames that HAD been
                    # written were reported as "output not created" on every single run.
                    $framePrefix = "$destBase-"
                    $frames = @(Get-ChildItem -LiteralPath $destDir -File -ErrorAction SilentlyContinue | Where-Object {
                        $_.Name.StartsWith($framePrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
                        $_.Name.EndsWith($destExt, [System.StringComparison]::OrdinalIgnoreCase) -and
                        # Round 16: digits only between prefix and extension (see Test-ThumbExists).
                        $_.Name.Substring($framePrefix.Length, $_.Name.Length - $framePrefix.Length - $destExt.Length) -match '^\d+$'
                    })
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

        if ((Test-ThumbExists $t.DestPath ($t.Page -eq "all")) -and -not $t.DryRun) {
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
                # A '[n]' suffix turns the input into a filename SPEC, where '%' is expanded;
                # a plain input (-Page all) is opened literally and must stay unescaped.
                $inputWithPage = if ($pageSuffix) { "$(ConvertTo-MagickPathSpec $t.SrcPath)$pageSuffix" } else { $t.SrcPath }
                
                # -profile converts wide-gamut sources through their embedded ICC; -colorspace
                # after it is a no-op for RGB but still normalises grayscale/CMYK sources.
                $magickArgs = @()
                if ($t.SrgbProfile) { $magickArgs += @("-profile", $t.SrgbProfile) }
                $magickArgs += @(
                    "-colorspace", "sRGB",
                    "-strip",
                    "-thumbnail", "$($t.Size)x$($t.Size)>"
                )

                # -quality only where it means JPEG quality. ImageMagick reinterprets it for
                # PNG as zlib level + filter (85 -> level 8, filter 5), so -Quality 85 was
                # quietly choosing a worse PNG compression than the default; TIFF ignores it.
                if ($t.Format -in @("jpg", "jpeg")) {
                    $magickArgs += @("-quality", $t.Quality, "-interlace", "Plane")
                } elseif ($t.Format -in @("tif", "tiff")) {
                    $magickArgs += @("-compress", "zip")
                }
                
                # Output path is always escaped: a '%' in the name would send the thumbnail
                # somewhere else while magick still exits 0 (see ConvertTo-MagickPathSpec).
                $magickArgs += (ConvertTo-MagickPathSpec $t.DestPath)
                
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
                    # Ordinal name match, not -Filter (see Test-ThumbExists): with a '[' in the
                    # basename the escaped pattern matched nothing, so frames that HAD been
                    # written were reported as "output not created" on every single run.
                    $framePrefix = "$destBase-"
                    $frames = @(Get-ChildItem -LiteralPath $destDir -File -ErrorAction SilentlyContinue | Where-Object {
                        $_.Name.StartsWith($framePrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
                        $_.Name.EndsWith($destExt, [System.StringComparison]::OrdinalIgnoreCase) -and
                        # Round 16: digits only between prefix and extension (see Test-ThumbExists).
                        $_.Name.Substring($framePrefix.Length, $_.Name.Length - $framePrefix.Length - $destExt.Length) -match '^\d+$'
                    })
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
Write-Log "Done: $($script:okTotal) OK | $($script:skipTotal) skipped | $($script:errTotal) errors | $($script:counterTotal)/$($script:total) processed"
Write-Log "Log: $logFile"
if ($script:errTotal -gt 0) { exit 1 } else { exit 0 }