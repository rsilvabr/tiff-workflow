"""
Tests for convert_tiff.py - Python orchestration layer.
Tests functions that don't require external tools (magick, exiftool).
"""

import pytest
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import os
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import convert_tiff
from convert_tiff import (
    _format_size,
    truncate_path,
    detect_powershell_version,
    build_compress_command,
    build_copy_exif_command,
    _compare_tiff_metadata,
    _compress_padded_files,
    run_purge_old_tiffs,
    step_folder,
    main,
    ConfigManager,
    ToolConfig,
)

MAGICK_AVAILABLE = shutil.which("magick") is not None
POWERSHELL_AVAILABLE = (
    shutil.which("pwsh") is not None or shutil.which("powershell") is not None
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

    @pytest.mark.parametrize("max_len", [1, 2, 3])
    def test_tiny_max_len(self, max_len):
        path = Path("C:/some/deep/path/structure/file.tif")
        result = truncate_path(path, max_len=max_len)
        assert len(result) <= max_len

    def test_never_exceeds_max_len(self):
        path = Path("C:/very/long/path/that/exceeds/maximum/length/and/needs/to/be/truncated/file.tif")
        for max_len in range(1, 60):
            assert len(truncate_path(path, max_len=max_len)) <= max_len


@pytest.mark.skipif(not POWERSHELL_AVAILABLE, reason="no powershell/pwsh on PATH")
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


@pytest.mark.skipif(not MAGICK_AVAILABLE, reason="ImageMagick (magick) not on PATH")
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


class TestSaveConfig:
    def test_ps_fields_not_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        cm = ConfigManager()
        cm.config.ps_major = 7
        cm.config.ps_name = "pwsh"
        cm.save_config()

        config_file = tmp_path / ".convert_tiff_config.json"
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert "ps_major" not in data
        assert "ps_name" not in data

    def test_other_fields_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        cm = ConfigManager()
        cm.config.last_input_dir = str(tmp_path)
        cm.save_config()

        data = json.loads((tmp_path / ".convert_tiff_config.json").read_text())
        assert data["default_workers"] == 8
        assert data["last_input_dir"] == str(tmp_path)


class TestCompressPaddedFiles:
    def _fake_run(self, cmd, **kwargs):
        if cmd[0] == "magick" and "-depth" in cmd:
            # Simulate 16->8 bit ZIP conversion producing a smaller output
            Path(cmd[-1]).write_bytes(b"z" * 100)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "exiftool":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "null:" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "identify" in cmd:
            return SimpleNamespace(returncode=0, stdout="100 100", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_original_backed_up_before_replacement(self, tmp_path, monkeypatch):
        src = tmp_path / "photo.tif"
        src.write_bytes(b"x" * 2000)
        monkeypatch.setattr(subprocess, "run", self._fake_run)

        cfg = SimpleNamespace(config=ToolConfig())
        _compress_padded_files([src], tmp_path / "staging_root", workers=1, cfg=cfg)

        backup = tmp_path / "OLD_PADDED" / "photo.tif"
        assert backup.exists()
        assert backup.read_bytes() == b"x" * 2000
        assert src.exists()
        assert src.read_bytes() == b"z" * 100

    def test_backup_collision_gets_v2_suffix(self, tmp_path, monkeypatch):
        src = tmp_path / "photo.tif"
        src.write_bytes(b"x" * 2000)
        old_dir = tmp_path / "OLD_PADDED"
        old_dir.mkdir()
        (old_dir / "photo.tif").write_bytes(b"old")
        monkeypatch.setattr(subprocess, "run", self._fake_run)

        cfg = SimpleNamespace(config=ToolConfig())
        _compress_padded_files([src], tmp_path / "staging_root", workers=1, cfg=cfg)

        assert (old_dir / "photo.tif").read_bytes() == b"old"
        backup_v2 = old_dir / "photo_v2.tif"
        assert backup_v2.exists()
        assert backup_v2.read_bytes() == b"x" * 2000
        assert src.read_bytes() == b"z" * 100


class TestPurgeOldTiffs:
    def test_sidecar_files_not_sent_to_magick(self, tmp_path, monkeypatch):
        folder = tmp_path / "root"
        old_dir = folder / "OLD_TIFFs"
        old_dir.mkdir(parents=True)
        (old_dir / "img.tif").write_bytes(b"tiffdata")
        (old_dir / "img.jpg").write_bytes(b"jpegdata")
        (old_dir / "notes.txt").write_text("notes")
        (folder / "img.tif").write_bytes(b"tiffdata")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append([str(a) for a in cmd])
            if "compare" in cmd:
                return SimpleNamespace(returncode=0, stdout="0 (0)", stderr="")
            return SimpleNamespace(returncode=0, stdout="10 10", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, prompt: folder)
        if convert_tiff.RICH_AVAILABLE:
            monkeypatch.setattr(
                convert_tiff.Confirm, "ask",
                classmethod(lambda cls, *a, **k: False),
            )
        else:
            monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

        cfg = SimpleNamespace(config=ToolConfig())
        result = run_purge_old_tiffs(cfg)
        assert result is False  # cancelled at confirmation

        flat = [arg for cmd in calls for arg in cmd]
        assert any("img.tif" in arg for arg in flat)
        assert not any("img.jpg" in arg for arg in flat)
        assert not any("notes.txt" in arg for arg in flat)


class TestStepFolder:
    def _set_input(self, monkeypatch, value):
        if convert_tiff.RICH_AVAILABLE:
            monkeypatch.setattr(
                convert_tiff.Prompt, "ask",
                classmethod(lambda cls, *a, **k: value),
            )
        else:
            monkeypatch.setattr("builtins.input", lambda *a, **k: value)

    def test_strips_double_quotes(self, tmp_path, monkeypatch):
        self._set_input(monkeypatch, f'"{tmp_path}"')
        cfg = SimpleNamespace(config=ToolConfig())
        result = step_folder(cfg)
        assert result == Path(str(tmp_path))

    def test_strips_single_quotes(self, tmp_path, monkeypatch):
        self._set_input(monkeypatch, f"'{tmp_path}'")
        cfg = SimpleNamespace(config=ToolConfig())
        result = step_folder(cfg)
        assert result == Path(str(tmp_path))

    def test_rejects_semicolon_path(self, tmp_path, monkeypatch):
        semicolon_dir = tmp_path / "a;b"
        semicolon_dir.mkdir()
        self._set_input(monkeypatch, str(semicolon_dir))
        cfg = SimpleNamespace(config=ToolConfig())
        assert step_folder(cfg) is None


class TestMainArgparse:
    def test_help_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["convert_tiff.py", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "TIFF Workflow Manager" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])