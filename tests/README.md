# Test Suite for tiff-workflow

## Python Tests (pytest)

### Setup
```bash
cd tests
pip install pytest
```

### Run Python tests
```bash
# Run all tests
pytest test_convert_tiff.py -v

# Run specific test class
pytest test_convert_tiff.py::TestBuildCompressCommand -v

# Run with coverage
pytest test_convert_tiff.py --cov=..
```

### Python tests cover
- `_format_size()` - file size formatting
- `truncate_path()` - path truncation
- `detect_powershell_version()` - PS version detection
- `build_compress_command()` - command building
- `build_copy_exif_command()` - command building
- `_compare_tiff_metadata()` - TIFF comparison using magick compare RMSE

## PowerShell Tests (Pester)

### Setup
```powershell
# Install Pester if not installed
Install-Module -Name Pester -Force -SkipPublisherCheck
```

### Run PowerShell tests
```powershell
# From project root
Invoke-Pester -Path tests/test_compress_tiff_zip.ps1

# Run all PS tests
Invoke-Pester -Path tests/*.ps1

# Run with detailed output
Invoke-Pester -Path tests/test_compress_tiff_zip.ps1 -Output Detailed
```

### PowerShell tests cover
- Parameter validation
- StagingDir cleanup check (IsNullOrWhiteSpace)
- stagingMap initialization
- Page count method (not Measure-Object -Line)
- Parallel processing structure
- DeleteSource logic for Mode 8
- Integrity check (size vs move failure)
- Error handling

## Test Philosophy

These tests are **not** unit tests that require actual TIFF files or external tools. They are:

1. **Static analysis tests** - verify code patterns exist/not exist
2. **Contract tests** - verify functions return expected structure
3. **Smoke tests** - verify basic functionality without full workflow

For full integration testing, run the scripts with real files in a safe environment.