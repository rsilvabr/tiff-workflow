# Behavioral Pester tests for generate_thumbnails.ps1 (requires ImageMagick)
# Run with: Invoke-Pester -Path tests/test_generate_thumbnails_behavior.ps1

BeforeAll {
    $script:ScriptPath = (Resolve-Path (Join-Path $PSScriptRoot "..\generate_thumbnails.ps1")).Path
    $script:PsExe = (Get-Process -Id $PID).Path
    $script:WorkDir = Join-Path $TestDrive "thumbs"
    New-Item -ItemType Directory -Force -Path $script:WorkDir | Out-Null

    # Keep Logs out of the repo: the script writes Logs\ under $PWD
    Push-Location $TestDrive

    # Create a 2-page TIFF
    $script:MultiTif = Join-Path $script:WorkDir "multi.tif"
    & magick -size 64x64 xc:red -size 64x64 xc:blue $script:MultiTif
    if ($LASTEXITCODE -ne 0) { throw "magick failed to create test TIFF" }
}

AfterAll {
    Pop-Location
}

Describe "generate_thumbnails.ps1 - Behavioral (all pages to jpg)" -Skip:($null -eq (Get-Command magick -ErrorAction SilentlyContinue)) {
    It "Exits 0 and creates per-frame jpg thumbnails for -Page all" {
        & $script:PsExe -NoProfile -File $script:ScriptPath -InputDir $script:WorkDir -Page all -Format jpg
        $LASTEXITCODE | Should -Be 0
        Join-Path $script:WorkDir "multi_thumb-0.jpg" | Should -Exist
        Join-Path $script:WorkDir "multi_thumb-1.jpg" | Should -Exist
    }

    It "Re-run with -Format tif -Recursive exits 0 and never creates _thumb_thumb files" {
        & $script:PsExe -NoProfile -File $script:ScriptPath -InputDir $script:WorkDir -Page all -Format tif -Recursive
        $LASTEXITCODE | Should -Be 0
        Join-Path $script:WorkDir "multi_thumb.tif" | Should -Exist
        @(Get-ChildItem -LiteralPath $script:WorkDir -Recurse -Filter "*_thumb_thumb*").Count | Should -Be 0
    }
}

Describe "generate_thumbnails.ps1 - Audit Round 16" -Skip:($null -eq (Get-Command magick -ErrorAction SilentlyContinue)) {
    It "A non-frame name_thumb-<text> file does not suppress -Page all generation" {
        # name_thumb-old.jpg used to count as an existing frame set, so every run reported
        # SKIP and the real frames were never written.
        $dir = Join-Path $TestDrive "round16-frames"
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        & magick -size 32x32 xc:green -size 32x32 xc:blue (Join-Path $dir "photo.tif")
        if ($LASTEXITCODE -ne 0) { throw "magick failed to create test TIFF" }
        Set-Content -LiteralPath (Join-Path $dir "photo_thumb-old.jpg") -Value "not a frame"
        & $script:PsExe -NoProfile -File $script:ScriptPath -InputDir $dir -Page all -Format jpg
        $LASTEXITCODE | Should -Be 0
        Join-Path $dir "photo_thumb-0.jpg" | Should -Exist
        Join-Path $dir "photo_thumb-1.jpg" | Should -Exist
        Join-Path $dir "photo_thumb-old.jpg" | Should -Exist
    }

    It "-Remove with a literal '[' in -OutputDir removes only the literal folder" {
        # The old `-like "*\$suffix"` read "x[y]" as a character class: the literal x[y]
        # folder never matched, and a folder named xy matched instead.
        $root = Join-Path $TestDrive "round16-remove"
        $literal = Join-Path $root "sub1\x[y]"
        $decoy   = Join-Path $root "sub2\xy"
        New-Item -ItemType Directory -Force -Path $literal, $decoy | Out-Null
        Set-Content -LiteralPath (Join-Path $literal "keep_thumb.jpg") -Value "x"
        Set-Content -LiteralPath (Join-Path $decoy "decoy_thumb.jpg") -Value "x"
        & $script:PsExe -NoProfile -File $script:ScriptPath -InputDir $root -OutputDir "x[y]" -Recursive -Remove
        $LASTEXITCODE | Should -Be 0
        Join-Path $literal "keep_thumb.jpg" | Should -Not -Exist
        Join-Path $decoy "decoy_thumb.jpg" | Should -Exist
    }

    It "A duplicated -InputDir entry does not thumbnail the same source twice" {
        # A;A enumerated photo.tif twice and the second copy became photo_thumb_v2.jpg.
        $dir = Join-Path $TestDrive "round16-dedupe"
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        & magick -size 32x32 xc:yellow (Join-Path $dir "photo.tif")
        if ($LASTEXITCODE -ne 0) { throw "magick failed to create test TIFF" }
        & $script:PsExe -NoProfile -File $script:ScriptPath -InputDir "$dir;$dir" -Format jpg
        $LASTEXITCODE | Should -Be 0
        Join-Path $dir "photo_thumb.jpg" | Should -Exist
        Join-Path $dir "photo_thumb_v2.jpg" | Should -Not -Exist
    }
}
