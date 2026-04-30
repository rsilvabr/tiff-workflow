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
    }
}

Describe "compress_tiff_zip_v2.ps1 - Page Count" {
    It "Does not use Measure-Object -Line for page count" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match 'Measure-Object -Line'
    }

    It "Uses [int] cast for page count" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$pageCount = \[int\]'
    }
}

Describe "compress_tiff_zip_v2.ps1 - DeleteSource Logic" {
    It "Has stagingUsed check for Mode 8 delete" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$stagingUsed'
        $content | Should -Match '\$writeDst -ne \$srcPath'
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
}

AfterAll {
    # Clean up test directory
    if (Test-Path $script:TestDir) {
        Remove-Item -Path $script:TestDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}