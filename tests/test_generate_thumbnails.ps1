# Pester tests for generate_thumbnails.ps1
# Run with: Invoke-Pester -Path tests/test_generate_thumbnails.ps1

BeforeAll {
    $script:ScriptPath = Join-Path $PSScriptRoot "..\generate_thumbnails.ps1"
}

Describe "generate_thumbnails.ps1 - Parameter Validation" {
    It "Has param block with InputDir" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'param\s*\('
        $content | Should -Match '\$InputDir'
    }

    It "Has ValidatePattern on Page (all or digits)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\[ValidatePattern\(''\^\(all\|\\d\+\)\$''\)\]'
        $content | Should -Match '\$Page\s*=\s*"0"'
    }

    It "Has ValidatePattern on Quality (1-100)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\[ValidatePattern\(''\^\(100\|\[1-9\]\[0-9\]\?\)\$''\)\]'
        $content | Should -Match '\$Quality\s*=\s*"85"'
    }

    It "Validates Format whitelist and exits 1 on invalid format" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$Format -notin @\("jpg", "jpeg", "png", "tif", "tiff"\)'
    }
}

Describe "generate_thumbnails.ps1 - Input Scan" {
    It "Excludes _thumb files from input scan (no _thumb_thumb)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$_\.BaseName -notmatch ''\(\?i\)_thumb\$'''
    }

    It "Excludes OLD_TIFFs folders from input scan" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'OLD_TIFFS'
    }
}

Describe "generate_thumbnails.ps1 - Suffixed-Frame Fallback" {
    It "Checks for suffixed frames (-Filter with -*) when exact dest not created" {
        $matches = Select-String -Path $script:ScriptPath -Pattern '-Filter "\$destBase-\*\$destExt"'
        $matches.Count | Should -BeGreaterOrEqual 2  # parallel + sequential paths
    }

    It "Reports frame count in OK result for multi-page output" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$\(\$frames\.Count\) frames'
    }
}

Describe "generate_thumbnails.ps1 - Error Handling" {
    It "Exits 1 when errors occurred" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'if \(\$script:errTotal -gt 0\) \{ exit 1 \}'
    }

    It "Exits 1 on missing required tools" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'Required tools not found'
        $exits = Select-String -Path $script:ScriptPath -Pattern 'exit 1'
        $exits.Count | Should -BeGreaterOrEqual 3
    }

    It "Exits 0 when no TIFF files found" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'No TIFF files found'
        $content | Should -Match 'exit 0'
    }
}

Describe "generate_thumbnails.ps1 - Magick Invocation" {
    It "Uses magick directly (no legacy 'magick convert')" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match 'magick convert'
        $content | Should -Match '& magick @allArgs'
    }

    It "Builds page selector suffix from Page parameter" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$pageSuffix = if \(\$t\.Page -eq "all"\) \{ "" \} else \{ "\[\$\(\$t\.Page\)\]" \}'
    }
}

Describe "generate_thumbnails.ps1 - Audit Round 4" {
    It "Self-exclusion covers multi-frame thumbs (_thumb-0, _thumb-1, ...)" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '_thumb\(-\\d\+\)\?\$'
    }
}
