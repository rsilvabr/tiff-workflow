# Pester tests for copy_exif_to_TIFF_ps5.ps1
# Run with: Invoke-Pester -Path tests/test_copy_exif_to_TIFF_ps5.ps1

BeforeAll {
    $script:ScriptPath = Join-Path $PSScriptRoot "..\copy_exif_to_TIFF_ps5.ps1"
}

Describe "copy_exif_to_TIFF_ps5.ps1 - Parameter Validation" {
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

Describe "copy_exif_to_TIFF_ps5.ps1 - stagingMap" {
    It "stagingMap is initialized as @{}" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$script:stagingMap = @\{\}'
    }
}

Describe "copy_exif_to_TIFF_ps5.ps1 - Page Count" {
    It "Does not use Measure-Object -Line" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match 'Measure-Object -Line'
    }

    It "Uses [int] cast for page count" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$pageCount = \[int\]'
    }
}

Describe "copy_exif_to_TIFF_ps5.ps1 - Original Name" {
    It "Uses `$tif.Name not undefined `$originalName" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '\$originalName(?!\s*\])'  # $originalName not followed by ] (could be array access)
        # Should have $tif.Name in the dest path assignment
        $content | Should -Match '\$destPath.*\$tif\.Name'
    }
}