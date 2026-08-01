# Pester tests for copy_exif_to_TIFF_ps7.ps1
# Run with: Invoke-Pester -Path tests/test_copy_exif_to_TIFF_ps7.ps1

BeforeAll {
    $script:ScriptPath = Join-Path $PSScriptRoot "..\copy_exif_to_TIFF_ps7.ps1"
}

Describe "copy_exif_to_TIFF_ps7.ps1 - Parameter Validation" {
    It "Has param block with InputDir" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'param\s*\('
        $content | Should -Match '\$InputDir'
    }

    It "Uses IsNullOrWhiteSpace for StagingDir check" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'IsNullOrWhiteSpace'
    }
}

Describe "copy_exif_to_TIFF_ps7.ps1 - groupStagingMap" {
    It "groupStagingMap is initialized as @{}" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$script:groupStagingMap = @\{\}'
    }

    It "Uses SrcPath as key, not FinalDst" {
        $content = Get-Content $script:ScriptPath -Raw
        # SrcPath should be used as key
        $content | Should -Match 'groupStagingMap\[\$r\.SrcPath\]'
    }
}

Describe "copy_exif_to_TIFF_ps7.ps1 - Parallel Returns" {
    It "All returns include SrcPath" {
        $content = Get-Content $script:ScriptPath -Raw
        # MISS, SKIP, DRY, ERROR returns should all have SrcPath
        # Check that lines with "return @{" and "OriginalName" also have SrcPath
        $returns = Select-String -Path $script:ScriptPath -Pattern 'return @\{[^}]*OriginalName[^}]*\}'
        foreach ($r in $returns) {
            $r.Line | Should -Match 'SrcPath'
        }
    }

    It "MULTI return includes MultiPagePath" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'MultiPagePath.*=.*\$p\.Tiff'
    }
}

Describe "copy_exif_to_TIFF_ps7.ps1 - Page Count" {
    It "Does not use Measure-Object -Line" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match 'Measure-Object -Line'
    }

    It "Uses [int]::TryParse to guard page count parsing (fails closed)" {
        # Must stay identical to the PS5 script -- divergence between the two is a bug class.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\[int\]::TryParse\(\$val, \[ref\]\$n\)'
        $content | Should -Match 'PageCount = 0; Error = "parse:\$val"'
    }
}

Describe "copy_exif_to_TIFF_ps7.ps1 - CopiedTiffPath" {
    It "copiedTiffPath is initialized to `$null before processing" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$copiedTiffPath = \$null'
    }
}

Describe "copy_exif_to_TIFF_ps7.ps1 - Error Handling" {
    It "Exits 1 when errors occurred" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'if \(\$script:errTotal -gt 0\) \{ exit 1 \}'
    }
}

Describe "copy_exif_to_TIFF_ps7.ps1 - Original Name" {
    It "Destination name comes from the file, never from an undefined variable" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '\$originalName(?!\s*\])'
        # v2.4 added $destNameMap for -OutputDir collisions; it falls back to $tif.Name
        $content | Should -Match '\$destName = if \(\$destNameMap\.ContainsKey\(\$tif\.FullName\)\).*else \{ \$tif\.Name \}'
        $content | Should -Match '\$destPath\s+= Join-Path \$finalDir\s+\$destName'
    }
}
Describe "copy_exif_to_TIFF_ps7.ps1 - Audit Round 4" {
    It "Exists-check skips files copied by this run (-not `$tiffCopied)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'Test-Path -LiteralPath \$finalDst.*-not \$tiffCopied'
    }

    It "Page count uses '%n\n' (no concatenated digits)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '"-format", "%n"'
        $content | Should -Match '@\("identify", "-format", "%n\\n", \$Path\)'
    }
}

Describe "copy_exif_to_TIFF_ps7.ps1 - Audit Round 7" {
    It "Validates every input directory and counts a bad one as an error" {
        # A wrong path fell through to "No TIFFs found" and exited 0, so convert_tiff.py let
        # Step 2 of workflow 4 run after a Copy EXIF that had touched nothing.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'Test-Path -LiteralPath \$dir -PathType Container'
        $content | Should -Match '\$missingRoots \+= \$dir'
        $content | Should -Match 'ERROR: input directory not found'
        $content | Should -Match '\$script:errTotal \+= \$missingRoots\.Count'
        $content | Should -Match 'No valid input directories specified'
    }

    It "Staging move has no unprefixed fallback" {
        # <writeDir>\<tif.Name> is a file this run never staged -- with a shared -StagingDir
        # that is another session's in-flight output, moved on top of the user's TIFF.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match 'Join-Path \$writeDir \$tif\.Name'
    }
}
