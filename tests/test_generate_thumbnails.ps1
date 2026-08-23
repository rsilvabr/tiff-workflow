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
        # Round 7 moved the literal into one $script:ThumbNamePattern shared with -Remove: the
        # two had drifted, so the _v2 collision rename matched neither.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$_\.BaseName -notmatch \$script:ThumbNamePattern'
    }

    It "Excludes OLD_TIFFs folders from input scan" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'OLD_TIFFS'
    }

    It "Matches the tool-output folders RELATIVE to the scan root, not absolutely" {
        # Round 15: the exclusion was a regex over the ABSOLUTE directory, so an ancestor
        # named zip/_EXPORT/OLD_TIFFs hid every file under it and pointing -InputDir AT such
        # a folder found nothing. Same fix the three backends got in round 14.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'function Test-PathExcluded'
        $content | Should -Match 'Test-PathExcluded -Directory \$_\.DirectoryName -Root \$_\.InputRoot'
        $content | Should -Not -Match '\$_\.DirectoryName -notmatch'
    }
}

Describe "generate_thumbnails.ps1 - Suffixed-Frame Fallback" {
    It "Checks for suffixed frames by ordinal name match when exact dest not created" {
        # Round 15: this used to pass a wildcard-escaped pattern to -Filter, which is the
        # FileSystem provider's DOS filter -- it has no escape syntax, so the backticks were
        # taken literally and a basename containing '[' matched nothing. Frames that HAD
        # been written were reported as "output not created" on every run.
        $matches = Select-String -Path $script:ScriptPath -Pattern '\$_\.Name\.StartsWith\(\$framePrefix, \[System\.StringComparison\]::OrdinalIgnoreCase\)'
        $matches.Count | Should -BeGreaterOrEqual 2  # parallel + sequential paths
    }

    It "Never routes a name pattern through -Filter" {
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '-Filter \$framePattern'
        $content | Should -Not -Match '-Filter \$pattern'
        # The call form, so the explanation left in the source comment does not match
        $content | Should -Not -Match 'WildcardPattern\]::Escape\('
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
        $content | Should -Match '_thumb\(_v\\d\+\)\?\(-\\d\+\)\?\$'
    }
}

Describe "generate_thumbnails.ps1 - Audit Round 7" {
    It "One pattern covers every name this script can produce, including _v2" {
        # The collision rename (_thumb_v2) matched neither the self-exclusion nor -Remove, so
        # a renamed .tif thumbnail came back as a source on the next run and never got cleaned up.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$script:ThumbNamePattern = ''\(\?i\)_thumb\(_v\\d\+\)\?\(-\\d\+\)\?\$'''
        $uses = Select-String -Path $script:ScriptPath -Pattern '\$script:ThumbNamePattern'
        $uses.Count | Should -Be 3   # the definition, the input scan and the -Remove filter
    }

    It "-Remove resolves a relative -OutputDir the same way generation does" {
        # Generation joins it to each FILE's directory, so with -Recursive the thumbnails sit
        # in <any subfolder>\<OutputDir>; -Remove only looked at <root>\<OutputDir>.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match 'Get-ChildItem -LiteralPath \$r -Directory -Recurse'
    }
}

Describe "generate_thumbnails.ps1 - Audit Round 16" {
    It "-Remove matches the relative -OutputDir tail literally, not as a wildcard" {
        # Was `-like "*\$suffix"`: a '[' in -OutputDir became a wildcard character class, so
        # the literal folder never matched and folders like 'xy' matched pattern 'x[y]'.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Not -Match '-like "\*\\\$suffix"'
        $content | Should -Match '\.EndsWith\(\$suffixTail, \[System\.StringComparison\]::OrdinalIgnoreCase\)'
    }

    It "Frame-name checks require digits between prefix and extension (all 3 copies)" {
        # Test-ThumbExists plus the parallel/sequential frame fallbacks: any name_thumb-* file
        # used to count as a -Page all frame, so a manually saved name_thumb-old.jpg made
        # every future run report SKIP forever.
        $digits = Select-String -Path $script:ScriptPath -Pattern "-match '\^\\d\+\$'"
        $digits.Count | Should -Be 3
    }

    It "Dedupes and sorts discovered sources before claiming destination names" {
        # A repeated -InputDir (A;A) or roots nested under a -Recursive root enumerated the
        # same source twice and the second copy became _v2; unsorted Get-ChildItem order also
        # made the photo_thumb.jpg vs photo_thumb_v2.jpg assignment non-reproducible.
        $content = Get-Content $script:ScriptPath -Raw
        $content | Should -Match '\$allFiles = @\(\$allFiles \| Sort-Object FullName -Unique\)'
    }
}
