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
}

Describe "copy_exif_to_TIFF_ps7.ps1 - Original Name" {
    It "Uses `$tif.Name not undefined `$originalName" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '\$originalName(?!\s*\])'
        $content | Should -Match '\$destPath.*\$tif\.Name'
    }
}