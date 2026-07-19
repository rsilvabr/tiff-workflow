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

    It "Uses [int] cast for page count" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$pageCount = \[int\]'
    }

    It "Uses [int]::TryParse to guard page count parsing" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\[int\]::TryParse\("\$pageCountVal", \[ref\]\$pageCount\)'
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
    It "Uses `$tif.Name not undefined `$originalName" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '\$originalName(?!\s*\])'
        $content | Should -Match '\$destPath.*\$tif\.Name'
    }
}
Describe "copy_exif_to_TIFF_ps7.ps1 - Audit Round 4" {
    It "Exists-check skips files copied by this run (-not `$tiffCopied)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'Test-Path -LiteralPath \$finalDst.*-not \$tiffCopied'
    }

    It "Page count uses '%n\n' (no concatenated digits)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match 'identify -format "%n"'
        $content | Should -Match 'identify -format "%n\\n"'
    }
}
