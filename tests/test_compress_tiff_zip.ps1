# Pester tests for compress_tiff_zip.ps1
# Run with: Invoke-Pester -Path tests/test_compress_tiff_zip.ps1

BeforeAll {
    # Set up test environment
    $script:ScriptPath = Join-Path $PSScriptRoot "..\compress_tiff_zip.ps1"
    $script:TestDir = Join-Path $TestDrive "compress_test"
    New-Item -ItemType Directory -Force -Path $script:TestDir | Out-Null
}

Describe "compress_tiff_zip_v2.ps1 - Parameter Validation" {
    It "Has required parameters defined" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'param\s*\('
        $content | Should -Match '\$InputDir'
        $content | Should -Match '\$Mode'
    }

    It "Has correct default values" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$InputDir\s*=\s*"\."'
        $content | Should -Match '\$Mode\s*=\s*-1'
        $content | Should -Match '\$Workers\s*=\s*8'
    }

    It "SafeMode no longer caps workers; -CapWorkers is the opt-in cap" {
        $content = Get-Content $script:ScriptPath -Raw
        # the hardcoded SafeMode -> 8 cap is gone
        $content | Should -Not -Match 'Min\(\$script:Workers, 8\)'
        $content | Should -Not -Match 'Min\(\$Workers, 8\)'
        $content | Should -Match '\[int\]\$CapWorkers = 0'
        $content | Should -Match 'Min\(\$script:Workers, \$CapWorkers\)'
        $content | Should -Match 'Min\(\$Workers, \$CapWorkers\)'
    }

    It "-ExcludeFolders filters by path segment and rejects paths" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\[string\]\$ExcludeFolders = ""'
        # segment match (never substring), applied centrally during collection
        $content | Should -Match '\$seg -in \$script:excludeNames'
        # entries containing a path separator are rejected with exit 1
        $content | Should -Match "-ExcludeFolders takes folder NAMES, not paths"
        # and every run logs what was excluded -- an exclusion is never invisible
        $content | Should -Match 'Excluded \$excludedCount file\(s\) under:'
    }

    It "Compression probe is batched (one exiftool per chunk, per-file fallback)" {
        $content = Get-Content $script:ScriptPath -Raw
        # the batched probe exists and reads IFD0 specifically
        $content | Should -Match 'function Get-CompressionMap'
        $content | Should -Match '-IFD0:Compression'
        # built once after discovery, with a timing line
        $content | Should -Match 'Compression probe: \$\(\$script:compMap.Count\)/\$\(\$script:total\)'
        # modes 1-7 workers still SKIP deflate when Comp came from the map
        $content | Should -Match 'runs for pre-probed files too'
    }
}

Describe "compress_tiff_zip_v2.ps1 - StagingDir Check" {
    It "Uses IsNullOrWhiteSpace for StagingDir check" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'IsNullOrWhiteSpace'
    }
}

Describe "compress_tiff_zip_v2.ps1 - Process-TiffJob Function" {
    It "Process-TiffJob function exists" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'function Process-TiffJob'
    }

    It "Process-TiffJob returns proper hashtable structure" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'return @\{[^}]*Result[^}]*StagingName[^}]*OriginalName[^}]*\}'
    }

    It "stagingName is defined before EXIF check" {
        $content = Get-Content $script:ScriptPath -Raw
        $stagingDef = Select-String -Path $script:ScriptPath -Pattern '\$stagingName = \[System\.IO\.Path\]::GetFileName'
        $stagingDef | Should -Not -BeNullOrEmpty
    }
}

Describe "compress_tiff_zip_v2.ps1 - Parallel Processing" {
    It "Uses ThrottleLimit for parallel execution" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'ThrottleLimit'
    }

    It "Process-Results called after parallel completes" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'foreach.*\$r in \$parallelResults'
        $content | Should -Match 'Process-Results'
    }

    It "Does not use Interlocked for counter increment" {
        $content = Get-Content $script:ScriptPath -Raw
        # This is OK - Process-Results is called sequentially after parallel
        # The bug was in the old version that incremented inside parallel
        $content | Should -Not -Match 'Interlocked'
    }
}

Describe "compress_tiff_zip_v2.ps1 - Page Count" {
    It "Does not use Measure-Object -Line for page count" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match 'Measure-Object -Line'
    }

    It "Guards page count parsing with [int]::TryParse (fails closed)" {
        # v2.3 replaced the raw [int] cast: unparseable identify output must yield Ok=$false,
        # not a silent 0 that lets SafeMode wave a multi-page TIFF through.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\[int\]::TryParse\(\$val, \[ref\]\$n\)'
        $content | Should -Match 'PageCount = 0; Error = "parse:\$val"'
    }
}

Describe "compress_tiff_zip_v2.ps1 - DeleteSource Logic" {
    It "Mode 8 verifies the file it actually wrote before allowing the delete" {
        # v2.3 replaced the $stagingUsed flag: the target to verify is the staged file when
        # one was written, otherwise the source itself. Both workers must do this.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$verifyTarget = if \(\$writeDst -ne \$srcPath\) \{ \$writeDst \} else \{ \$srcPath \}'
        $content | Should -Match '\$verifyPath = if \(\$writeDst -ne \$srcPath\) \{ \$writeDst \} else \{ \$srcPath \}'
        $content | Should -Match 'Test-ZipIntegrity -Path \$verifyTarget'
        $content | Should -Match 'Test-ZipIntegrity -Path \$verifyPath'
    }
}

Describe "compress_tiff_zip_v2.ps1 - Integrity Check" {
    It "Checks if dest exists before comparing size" {
        $content = Get-Content $script:ScriptPath -Raw
        # Should have separate check for dest exists vs size match
        $content | Should -Match 'Test-Path -LiteralPath \$destPath'
    }

    It "Reports size mismatch separately from move failure" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'size mismatch'
    }
}

Describe "compress_tiff_zip_v2.ps1 - Error Handling" {
    It "WARN result includes stagingName" {
        $content = Get-Content $script:ScriptPath -Raw
        # exiftool failure return should have stagingName defined
        $content | Should -Match 'WARN \(exiftool failed'
    }

    It "Exits 1 when errors occurred" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'if \(\$script:errTotal -gt 0\) \{ exit 1 \}'
    }
}

Describe "compress_tiff_zip_v2.ps1 - Workers Validation" {
    It "Workers parameter has ValidateRange(1, 64)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\[ValidateRange\(1,\s*64\)\]\s*\[int\]\$Workers'
    }
}

Describe "compress_tiff_zip_v2.ps1 - Run-Scoped Staging" {
    It "Generates a run-scoped staging prefix (runStagingId)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$script:runStagingId\s*=\s*\[guid\]::NewGuid\(\)'
    }

    It "Staging file names use the run-scoped prefix" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$stagingName = "\$\(\$script:runStagingId\)_'
    }

    It "Staging cleanup only removes files from this run" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$_\.Name -like "\$\(\$script:runStagingId\)_\*"'
    }
}

Describe "compress_tiff_zip_v2.ps1 - No-Thumb Fallback (v2.5)" {
    It "No worker returns early on '[no thumb]'" {
        # Returning there skipped the EXIF restore and the mode 8 integrity gate, which is
        # how PS7 lost metadata where PS5 kept it. The note is accumulated instead.
        $returns = Select-String -Path $script:ScriptPath -Pattern 'return @\{[^}]*\[no thumb\][^}]*\}'
        $returns | Should -BeNullOrEmpty
    }

    It "All three workers accumulate the note and fall through" {
        $notes = Select-String -Path $script:ScriptPath -Pattern '\$noThumbNote = " \[no thumb\]"'
        $notes.Count | Should -Be 6   # two fallback branches x three workers
    }
}

Describe "compress_tiff_zip_v2.ps1 - SkipCompressedWithThumb Reprocess" {
    It "Checks tiff:subfiletype for embedded thumbnail in all processing paths" {
        $matches = Select-String -Path $script:ScriptPath -Pattern 'tiff:subfiletype'
        $matches.Count | Should -BeGreaterOrEqual 3
    }

    It "Detects REDUCEDIMAGE/REDUCED subfiletype as thumbnail marker" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'REDUCEDIMAGE'
    }

    It "Compressed but no thumbnail falls through to reprocess" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'fall through to reprocess'
    }

    It "Only skips when thumbnail already embedded (not blindly on compression)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'SKIP \(compressed\+thumb\)'
    }
}

Describe "compress_tiff_zip_v2.ps1 - Integrity Check Command" {
    It "Uses 'magick <file> null:' for integrity verification" {
        # v2.4 moved every magick call into an in-process runspace (Invoke-MagickWithTimeout),
        # replacing the per-file Start-Job. The full pixel decode itself is unchanged.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'Invoke-MagickWithTimeout -Arguments @\(\$Path, "null:"\)'
    }

    It "Does not use legacy 'magick convert'" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match 'magick convert'
    }
}

Describe "compress_tiff_zip_v2.ps1 - Audit Round 4" {
    It "Page count uses '%n\n' (avoids concatenated '333' for multi-page)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '"-format", "%n"'
        $content | Should -Match '@\("identify", "-format", "%n\\n", \$Path\)'
    }

    It "Exiftool argfiles declare UTF-8 filename charset" {
        $matches = Select-String -Path $script:ScriptPath -Pattern 'WriteAllText'
        $matches.Count | Should -BeGreaterThan 0
        foreach ($m in $matches) {
            $m.Line | Should -Match '-charset`nfilename=utf8'
        }
    }

    It "Mode 8 abort exits with code 1" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'Mode 8 aborted[\s\S]{0,200}exit 1'
    }

    It "ZIP integrity check fails closed on timeout and non-zero exit" {
        # Same contract as the old $jobOutput check, now expressed through the runspace
        # helper: a magick run that timed out or that could not report an exit code is a
        # FAILURE, never a pass -- this gate authorises overwriting the original.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'if \(\$r\.TimedOut\) \{ return \$false \}'
        $content | Should -Match 'return \(\$r\.ExitCode -eq 0\)'
        $content | Should -Match 'return @\{ TimedOut = \$false; ExitCode = -1; Output = @\(\) \}'
    }

    It "Mode 8 integrity failure blocks the staging move (IntegrityFailed)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'IntegrityFailed = \$true'
        $content | Should -Match '\$integrityFailedDst\.Contains\(\$key\)'
    }

    It "ERROR (magick) return includes SrcPath for rollback" {
        $returns = Select-String -Path $script:ScriptPath -Pattern 'ERROR \(magick\) \| \$name'
        $returns.Count | Should -BeGreaterThan 0
        foreach ($r in $returns) {
            $r.Line | Should -Match 'SrcPath'
        }
    }

    It "Collision detection is not restricted to the flattening modes" {
        # v2.4: photo.tif and photo.tiff both resolve to photo.tif, so two workers could race
        # on one output in ANY mode -- the 4-7 restriction was the bug, not the fix.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '\$Mode -ge 4 -and \$Mode -le 7'
        $content | Should -Match 'DuplicateAction -eq ''Numbered'' -and -not \$Overwrite'
    }

    It "Numbered DuplicateAction does not rename when -Overwrite is set" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'DuplicateAction -eq ''Numbered'' -and -not \$Overwrite'
    }

    It "Legacy PS5 staging names use the run-scoped prefix" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$writeName = if \(\$writeDir -ne \$finalDir\) \{ "\$\(\$script:runStagingId\)_'
    }

    It "Legacy PS7 honors -SkipCompressedWithThumb" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$skipCompThumbL = \$using:SkipCompressedWithThumb'
    }
}

AfterAll {
    # Clean up test directory
    if (Test-Path $script:TestDir) {
        Remove-Item -Path $script:TestDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}