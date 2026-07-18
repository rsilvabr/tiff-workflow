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
