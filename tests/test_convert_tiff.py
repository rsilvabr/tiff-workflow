"""
Tests for convert_tiff.py - Python orchestration layer.
Tests functions that don't require external tools (magick, exiftool).
"""

import pytest
from pathlib import Path
import tempfile
import os
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from convert_tiff import (
    _format_size,
    truncate_path,
    detect_powershell_version,
    build_compress_command,
    build_copy_exif_command,
    _compare_tiff_metadata,
)


class TestFormatSize:
    def test_bytes(self):
        result = _format_size(500)
        assert "B" in result
        assert "500" in result

    def test_kilobytes(self):
        result = _format_size(1024)
        assert "KB" in result
        assert "1" in result

    def test_megabytes(self):
        result = _format_size(1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = _format_size(1024 * 1024 * 1024)
        assert "GB" in result

    def test_large_values(self):
        assert "B" in _format_size(0)
        assert "B" in _format_size(1)


class TestTruncatePath:
    def test_short_path(self):
        path = Path("C:/short/path/file.tif")
        result = truncate_path(path, max_len=50)
        assert "file.tif" in result
        assert len(result) <= 50

    def test_long_path_truncated(self):
        path = Path("C:/very/long/path/that/exceeds/maximum/length/and/needs/to/be/truncated/file.tif")
        result = truncate_path(path, max_len=40)
        assert "file.tif" in result
        assert len(result) <= 43  # includes ellipsis


class TestDetectPowershellVersion:
    def test_returns_tuple(self):
        major, name, version = detect_powershell_version()
        assert isinstance(major, int)
        assert isinstance(name, str)
        assert isinstance(version, str)

    def test_major_is_5_or_7(self):
        major, _, _ = detect_powershell_version()
        assert major in (5, 7)


class TestBuildCompressCommand:
    def test_basic_command(self, tmp_path):
        workflow = {"mode": 0}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "compress_tiff_zip.ps1" in cmd[3]
        assert "-Mode" in cmd
        assert "0" in cmd

    def test_single_folder(self, tmp_path):
        workflow = {}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-InputDir" in cmd

    def test_workers(self, tmp_path):
        workflow = {"workers": 8}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-Workers" in cmd
        assert "8" in cmd

    def test_dry_run_flag(self, tmp_path):
        workflow = {"dry_run": True}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-DryRun" in cmd

    def test_safe_mode_false(self, tmp_path):
        workflow = {"safe_mode": False}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-SafeMode:$false" in cmd

    def test_skip_lzw(self, tmp_path):
        workflow = {"skip_lzw": True}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-SkipLzwAsCompressed:$true" in cmd

    def test_delete_source(self, tmp_path):
        workflow = {"delete_source": True}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-DeleteSource" in cmd

    def test_overwrite(self, tmp_path):
        workflow = {"overwrite": True}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-Overwrite" in cmd

    def test_force_parallel(self, tmp_path):
        workflow = {"force_parallel": True}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-ForceParallel" in cmd

    def test_force_sequential(self, tmp_path):
        workflow = {"force_sequential": True}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-ForceSequential" in cmd

    def test_no_folders(self):
        workflow = {}
        cmd = build_compress_command(workflow, folders=None)
        assert "-InputDir" not in cmd


class TestBuildCopyExifCommand:
    def test_basic_command(self, tmp_path):
        workflow = {}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="pwsh")
        assert "copy_exif_to_TIFF_ps7.ps1" in cmd[3]
        assert "-File" in cmd

    def test_ps5_script_for_powershell(self, tmp_path):
        workflow = {}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="powershell")
        assert "copy_exif_to_TIFF_ps5.ps1" in cmd[3]

    def test_ps7_script_for_pwsh(self, tmp_path):
        workflow = {}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="pwsh")
        assert "copy_exif_to_TIFF_ps7.ps1" in cmd[3]

    def test_workers(self, tmp_path):
        workflow = {"workers": 4}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="pwsh")
        assert "-Workers" in cmd
        assert "4" in cmd

    def test_dry_run(self, tmp_path):
        workflow = {"dry_run": True}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="pwsh")
        assert "-DryRun" in cmd

    def test_skip_exif(self, tmp_path):
        workflow = {"skip_exif": True}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="pwsh")
        assert "-SkipIfTiffHasExif" in cmd

    def test_compress_zip(self, tmp_path):
        workflow = {"compress_zip": True}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="pwsh")
        assert "-CompressZip" in cmd

    def test_overwrite(self, tmp_path):
        workflow = {"overwrite": True}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="pwsh")
        assert "-Overwrite" in cmd

    def test_compress_zip_false_not_added(self, tmp_path):
        workflow = {"compress_zip": False}
        cmd = build_copy_exif_command(workflow, folders=[tmp_path], ps_name="pwsh")
        assert "-CompressZip" not in cmd


class TestCompareTiffMetadata:
    def test_dimension_mismatch_detection(self, tmp_path):
        """Create two files with different dimensions to test mismatch detection."""
        import subprocess

        file1 = tmp_path / "test1.tif"
        file2 = tmp_path / "test2.tif"

        subprocess.run([
            "magick", "-size", "10x10", "gradient:red-blue", str(file1)
        ], capture_output=True)
        subprocess.run([
            "magick", "-size", "20x20", "gradient:red-blue", str(file2)
        ], capture_output=True)

        match, detail = _compare_tiff_metadata(file1, file2)
        assert match is False
        assert "DIMENSION_MISMATCH" in detail

    def test_identical_images(self, tmp_path):
        """Create two identical images and verify they match."""
        import subprocess

        file1 = tmp_path / "identical1.tif"
        file2 = tmp_path / "identical2.tif"

        subprocess.run([
            "magick", "-size", "10x10", "gradient:red-blue", str(file1)
        ], capture_output=True)
        subprocess.run([
            "magick", "-size", "10x10", "gradient:red-blue", str(file2)
        ], capture_output=True)

        match, detail = _compare_tiff_metadata(file1, file2)
        assert match is True
        assert "IDENTICAL" in detail


if __name__ == "__main__":
    pytest.main([__file__, "-v"])