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
    _is_real_16bit,
    _magick_spec,
    _work_timeout,
    _exiftool_copy_failed,
    _tiff_extra_pages_are_thumbnails,
    find_folders_by_pattern,
    run_purge_old_tiffs,
    run_undo_old_tiffs,
    run_free_compress,
    step_folder,
    main,
    ConfigManager,
    ToolConfig,
)

MAGICK_AVAILABLE = shutil.which("magick") is not None
EXIFTOOL_AVAILABLE = shutil.which("exiftool") is not None
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

    def test_cap_workers(self, tmp_path):
        workflow = {"cap_workers": 4}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-CapWorkers" in cmd
        assert "4" in cmd

    def test_cap_workers_zero_omitted(self, tmp_path):
        workflow = {"cap_workers": 0}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-CapWorkers" not in cmd

    def test_exclude_folders(self, tmp_path):
        workflow = {"exclude_folders": "_EXPORT;temp"}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-ExcludeFolders" in cmd
        assert "_EXPORT;temp" in cmd

    def test_exclude_folders_empty_omitted(self, tmp_path):
        workflow = {"exclude_folders": ""}
        cmd = build_compress_command(workflow, folders=[tmp_path])
        assert "-ExcludeFolders" not in cmd

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

    def test_extra_page_difference_blocks_match(self, tmp_path):
        """The purge gate deletes on this verdict: a difference on any page -- not
        just page 0 -- must block it. Whole-file `magick compare` pairs pages across
        lists and returns garbage, so pages are compared pairwise."""
        import subprocess

        main = tmp_path / "main.tif"
        thumb_a = tmp_path / "thumb_a.tif"
        thumb_b = tmp_path / "thumb_b.tif"
        file1 = tmp_path / "multi1.tif"
        file2 = tmp_path / "multi2.tif"

        subprocess.run(["magick", "-size", "10x10", "gradient:red-blue", str(main)], capture_output=True)
        subprocess.run(["magick", "-size", "4x4", "gradient:red-blue", str(thumb_a)], capture_output=True)
        subprocess.run(["magick", "-size", "4x4", "gradient:blue-red", str(thumb_b)], capture_output=True)
        subprocess.run(["magick", str(main), str(thumb_a), str(file1)], capture_output=True)
        subprocess.run(["magick", str(main), str(thumb_b), str(file2)], capture_output=True)

        match, detail = _compare_tiff_metadata(file1, file2)
        assert match is False
        assert "PIXEL_DIFF page 1" in detail


@pytest.mark.skipif(not MAGICK_AVAILABLE, reason="ImageMagick (magick) not on PATH")
class TestIsReal16Bit:
    def _make_c1_style(self, tmp_path, real_16bit):
        """TIFF with Capture One structure: main page + reduced thumbnail page."""
        import subprocess

        main8 = tmp_path / "main8.tif"
        main = tmp_path / "main.tif"
        thumb = tmp_path / "thumb.tif"
        out = tmp_path / "out.tif"

        base = ["magick", "-size", "64x64"]
        if real_16bit:
            subprocess.run(base + ["plasma:fractal", "-depth", "16", str(main)], capture_output=True)
        else:
            subprocess.run(base + ["gradient:red-blue", "-depth", "8", str(main8)], capture_output=True)
            subprocess.run(["magick", str(main8), "-depth", "16", str(main)], capture_output=True)
        subprocess.run(base + ["gradient:red-blue", "-resize", "16x16", str(thumb)], capture_output=True)
        subprocess.run(["magick", str(main), str(thumb), str(out)], capture_output=True)
        return out

    def test_padded_single_page_detected(self, tmp_path):
        import subprocess

        main8 = tmp_path / "m8.tif"
        padded = tmp_path / "padded.tif"
        subprocess.run(["magick", "-size", "64x64", "gradient:red-blue", "-depth", "8", str(main8)], capture_output=True)
        subprocess.run(["magick", str(main8), "-depth", "16", str(padded)], capture_output=True)

        is_real, rmse, msg = _is_real_16bit(padded, tmp_path)
        assert is_real is False
        assert "padded" in msg

    def test_padded_two_page_capture_one_style_detected(self, tmp_path):
        """Regression: whole-file RMSE on multi-page TIFFs returned garbage (~27%), so a
        padded 8-bit file with a thumbnail page was misclassified as real 16-bit."""
        padded = self._make_c1_style(tmp_path, real_16bit=False)
        is_real, rmse, msg = _is_real_16bit(padded, tmp_path)
        assert is_real is False
        assert "padded" in msg

    def test_real_16bit_two_page_detected(self, tmp_path):
        real = self._make_c1_style(tmp_path, real_16bit=True)
        is_real, rmse, msg = _is_real_16bit(real, tmp_path)
        assert is_real is True
        assert rmse > 0


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
            # The integrity gate asks for dimensions (%w %h) and page count (%n) separately
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if fmt.startswith("%n"):
                return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="100 100\n", stderr="")
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


class TestWrapPs5Command:
    def test_switch_bool_values_stay_unquoted(self):
        from convert_tiff import _wrap_ps5_command
        cmd = ["powershell", "-NoProfile", "-File", "C:\\s\\script.ps1",
               "-InputDir", "F:\\My Photos", "-SafeMode:$false", "-DryRun"]
        wrapped = _wrap_ps5_command(cmd)
        assert wrapped[0] == "powershell"
        assert "-Command" in wrapped
        assert "-File" not in wrapped
        invocation = wrapped[-1]
        assert invocation.startswith("& '")
        assert "-SafeMode:$false" in invocation  # unquoted, must be evaluated by the parser
        assert "'-SafeMode:$false'" not in invocation
        assert "-DryRun" in invocation  # param names unquoted
        assert "'-DryRun'" not in invocation
        assert "'F:\\My Photos'" in invocation  # values with spaces quoted

    def test_single_quotes_in_paths_escaped(self):
        from convert_tiff import _wrap_ps5_command
        cmd = ["powershell", "-NoProfile", "-File", "C:\\s\\script.ps1", "-InputDir", "F:\\O'Brien"]
        invocation = _wrap_ps5_command(cmd)[-1]
        assert "'F:\\O''Brien'" in invocation

    def test_pwsh_commands_not_wrapped(self):
        cmd = build_compress_command({"mode": 3, "safe_mode": False}, [Path("F:\\x")], ps_name="pwsh")
        assert "-File" in cmd
        assert "-Command" not in cmd

    def test_powershell_builder_wraps(self):
        cmd = build_compress_command({"mode": 3, "safe_mode": False}, [Path("F:\\x")], ps_name="powershell")
        assert "-Command" in cmd
        assert "-File" not in cmd


class TestComparePageCount:
    def test_page_count_mismatch_detected(self, tmp_path, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if fmt == "%n\n":
                old = "old" in cmd[-1]
                return SimpleNamespace(returncode=0, stdout=("2\n2\n" if old else "1\n"), stderr="")
            if fmt == "%w %h":
                return SimpleNamespace(returncode=0, stdout="100 100", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(convert_tiff.subprocess, "run", fake_run)
        match, detail = _compare_tiff_metadata(Path("old.tif"), Path("new.tif"))
        assert match is False
        assert "PAGE_COUNT_MISMATCH" in detail

    def test_matching_page_counts_pass(self, tmp_path, monkeypatch):
        def fake_run(cmd, **kwargs):
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if fmt == "%w %h":
                return SimpleNamespace(returncode=0, stdout="100 100", stderr="")
            if fmt == "%n\n":
                return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
            # magick compare -> RMSE 0 output
            return SimpleNamespace(returncode=0, stdout="0 (0)", stderr="")

        monkeypatch.setattr(convert_tiff.subprocess, "run", fake_run)
        match, detail = _compare_tiff_metadata(Path("old.tif"), Path("new.tif"))
        assert match is True


class TestPurgeSidecarVerification:
    def test_sidecar_without_parent_is_never_deleted(self, tmp_path, monkeypatch, capsys):
        """The property that matters is that the file survives: with no counterpart in the
        parent folder it is the only copy left.

        Round 13 changed only HOW that is enforced. It used to be reported as a mismatch,
        which aborted the whole purge -- a dead end a healthy folder reaches routinely,
        because modes 0/9 rename a backup when one of that name already exists and a .tiff
        source produces a .tif output. It is now kept and reported; here it is the only
        entry, so there is simply nothing left to purge."""
        old_dir = tmp_path / "OLD_TIFFs"
        old_dir.mkdir()
        (old_dir / "notes.txt").write_text("sidecar without parent copy")

        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, *a, **k: tmp_path)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        cfg = SimpleNamespace(config=ToolConfig())
        result = run_purge_old_tiffs(cfg)
        assert (old_dir / "notes.txt").exists(), "the only copy of the sidecar was deleted"
        assert result is True
        out = capsys.readouterr().out
        assert "KEPT" in out and "Nothing can be purged" in out, out

    def test_sidecar_with_matching_parent_does_not_block(self, tmp_path, monkeypatch):
        old_dir = tmp_path / "OLD_TIFFs"
        old_dir.mkdir()
        (old_dir / "notes.txt").write_text("same content")
        (tmp_path / "notes.txt").write_text("same content")

        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, *a, **k: tmp_path)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
        cfg = SimpleNamespace(config=ToolConfig())
        result = run_purge_old_tiffs(cfg)
        assert result is False  # user cancelled at confirmation
        assert (old_dir / "notes.txt").exists()


class TestMagickSpec:
    """audit round 16: '[' and ']' in a filename are glob characters to ImageMagick once
    a '[n]' page selector is appended, and a glob match WINS over the literal file. No
    bracket escape exists on Windows IM (verified live on 7.1.2-27), so _magick_spec
    routes bracketed paths through a bracket-free alias; callers append the selector
    AFTER the function returns, so selectors must never be touched."""

    def test_percent_doubled(self):
        assert _magick_spec("50%off.tif") == "50%%off.tif"

    def test_plain_path_unchanged(self):
        assert _magick_spec("plain.tif") == "plain.tif"

    def test_bracket_path_aliased(self, tmp_path):
        p = tmp_path / "foo[1].tif"
        p.write_bytes(b"x" * 100)
        spec = _magick_spec(p)
        assert "[" not in spec and "]" not in spec
        alias = Path(spec)
        assert alias.exists() and alias.read_bytes() == b"x" * 100

    def test_selector_appended_afterwards_is_untouched(self):
        spec = f"{_magick_spec('foo.tif')}[0]"
        assert spec == "foo.tif[0]"

    @pytest.mark.skipif(not MAGICK_AVAILABLE, reason="ImageMagick (magick) not on PATH")
    def test_literal_bracket_file_wins_over_glob(self, tmp_path):
        """foo[1].tif (8x8) next to foo1.tif (20x20): unescaped, `foo[1].tif[0]` reads
        foo1.tif -- the glob match. The spec must address the literal file."""
        big = tmp_path / "big.tif"
        subprocess.run(["magick", "-size", "8x8", "gradient:red-blue", str(big)],
                       capture_output=True, check=True)
        bracket = tmp_path / "foo[1].tif"
        shutil.copy(big, bracket)
        subprocess.run(["magick", "-size", "20x20", "gradient:red-blue",
                        str(tmp_path / "foo1.tif")], capture_output=True, check=True)

        r = subprocess.run(["magick", "identify", "-format", "%w %h",
                            f"{_magick_spec(bracket)}[0]"], capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.strip() == "8 8", r.stderr


@pytest.mark.skipif(not (MAGICK_AVAILABLE and EXIFTOOL_AVAILABLE),
                    reason="ImageMagick (magick) and exiftool required")
class TestPurgeMetadataGate:
    """audit round 16: the purge gate verified pixels only -- a conversion that dropped
    metadata (per-page SubfileType, ICC profile) while leaving pixels intact purged the
    last good backup."""

    def _two_page_with_exif(self, tmp_path, name):
        """magick two-page TIFF (both pages stamped PAGE by magick) + EXIF tags."""
        main = tmp_path / f"main_{name}"
        thumb = tmp_path / f"thumb_{name}"
        subprocess.run(["magick", "-size", "64x64", "gradient:red-blue", "-depth", "16",
                        str(main)], capture_output=True, check=True)
        subprocess.run(["magick", "-size", "16x16", "gradient:red-blue",
                        str(thumb)], capture_output=True, check=True)
        out = tmp_path / name
        subprocess.run(["magick", str(main), str(thumb), str(out)],
                       capture_output=True, check=True)
        subprocess.run(["exiftool", "-q", "-overwrite_original",
                        "-EXIF:Make=TestCam", "-EXIF:Artist=Tester", str(out)],
                       capture_output=True, check=True)
        return out

    def _zip_with_tags_restored(self, old, new):
        """What the compress backends do: magick recompress + exiftool tag restore."""
        subprocess.run(["magick", str(old), "-compress", "zip", str(new)],
                       capture_output=True, check=True)
        subprocess.run(["exiftool", "-q", "-overwrite_original",
                        "-tagsfromfile", str(old), "-all:all", "-unsafe",
                        "--Compression", "--BitsPerSample", "--SampleFormat", str(new)],
                       capture_output=True, check=True)
        return new

    def test_backend_pipeline_pair_passes(self, tmp_path):
        """Guard against the metadata gate being tightened into always-False: a real
        uncompressed -> ZIP pair with tags restored must still verify."""
        old = self._two_page_with_exif(tmp_path, "old.tif")
        new = self._zip_with_tags_restored(old, tmp_path / "new.tif")
        match, detail = _compare_tiff_metadata(old, new)
        assert match is True, detail

    def test_dropped_subfiletype_blocks_match(self, tmp_path):
        """Pixel-identical pages but a dropped SubfileType marker must NOT be purged."""
        old = self._two_page_with_exif(tmp_path, "old.tif")
        new = self._zip_with_tags_restored(old, tmp_path / "new.tif")
        subprocess.run(["exiftool", "-q", "-overwrite_original",
                        "-IFD1:SubfileType=", str(new)], capture_output=True, check=True)
        match, detail = _compare_tiff_metadata(old, new)
        assert match is False
        assert "SUBFILETYPE" in detail

    def test_dropped_icc_profile_blocks_match(self, tmp_path):
        icm = Path(r"C:\Windows\System32\spool\drivers\color\sRGB Color Space Profile.icm")
        if not icm.exists():
            pytest.skip("no system sRGB profile to embed")
        old = tmp_path / "old.tif"
        new = tmp_path / "new.tif"
        subprocess.run(["magick", "-size", "32x32", "gradient:red-blue",
                        "-profile", str(icm), str(old)], capture_output=True, check=True)
        subprocess.run(["magick", str(old), "+profile", "*", str(new)],
                       capture_output=True, check=True)
        match, detail = _compare_tiff_metadata(old, new)
        assert match is False
        assert "ICC" in detail


class TestRestoreSubfileTypesHonored:
    """audit round 16: _process_single_padded ignored _restore_subfile_types' False
    return and replaced the original as if the markers had been restored."""

    def _fake_run(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        if cmd[0] == "exiftool":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "magick" and "-depth" in cmd:
            Path(cmd[-1]).write_bytes(b"z" * 100)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "null:" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "identify" in cmd:
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if fmt.startswith("%[tiff:subfiletype]"):
                return SimpleNamespace(returncode=0, stdout="|100|100\n", stderr="")
            if fmt.startswith("%n"):
                return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="100 100\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_failed_restore_preserves_original(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "photo.tif"
        src.write_bytes(b"x" * 2000)
        monkeypatch.setattr(subprocess, "run", self._fake_run)
        monkeypatch.setattr(convert_tiff, "_restore_subfile_types", lambda a, b: False)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)

        cfg = SimpleNamespace(config=ToolConfig())
        _compress_padded_files([src], tmp_path / "temp", workers=1, cfg=cfg)

        assert src.read_bytes() == b"x" * 2000, "original replaced despite failed restore"
        assert not (tmp_path / "OLD_PADDED").exists()
        assert "WARN" in capsys.readouterr().out

    def test_successful_restore_still_replaces(self, tmp_path, monkeypatch):
        src = tmp_path / "photo.tif"
        src.write_bytes(b"x" * 2000)
        monkeypatch.setattr(subprocess, "run", self._fake_run)
        monkeypatch.setattr(convert_tiff, "_restore_subfile_types", lambda a, b: True)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)

        cfg = SimpleNamespace(config=ToolConfig())
        _compress_padded_files([src], tmp_path / "temp", workers=1, cfg=cfg)

        assert src.read_bytes() == b"z" * 100
        assert (tmp_path / "OLD_PADDED" / "photo.tif").read_bytes() == b"x" * 2000


class TestExiftoolCopyGate:
    """audit round 16: exiftool exits 0 on partial copies; the lossy 16->8 gate must
    catch 'not written' warnings, not just 'Error opening file'."""

    def test_clean_stderr_passes(self):
        assert not _exiftool_copy_failed("")

    def test_benign_minor_warning_passes(self):
        assert not _exiftool_copy_failed("Warning: [minor] Unrecognized char in EXIF:Foo")

    def test_error_opening_file_fails(self):
        assert _exiftool_copy_failed("Warning: Error opening file - foo.tif")

    def test_sorry_not_written_fails(self):
        assert _exiftool_copy_failed("Warning: Sorry, EXIF:Make doesn't exist or isn't writable")
        assert _exiftool_copy_failed("Warning: Tag 'EXIF:Make' was not written")

    def test_cant_fails(self):
        assert _exiftool_copy_failed("Warning: Can't write TIFF:Foo - foo.tif")


class TestExtraPagesClassifier:
    """audit round 16: an untagged page that is merely smaller than page 0 was excused
    as a thumbnail here while the purge-path classifier (_image_page_indices) requires
    the REDUCEDIMAGE tag. Both now use tag AND strictly-smaller."""

    def _identify(self, monkeypatch, stdout):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_untagged_smaller_page_refused(self, monkeypatch):
        self._identify(monkeypatch, "|4000|3000\n|400|300\n")
        assert _tiff_extra_pages_are_thumbnails(Path("x.tif")) is False

    def test_tagged_reduced_smaller_page_accepted(self, monkeypatch):
        self._identify(monkeypatch, "|4000|3000\nREDUCEDIMAGE|400|300\n")
        assert _tiff_extra_pages_are_thumbnails(Path("x.tif")) is True

    def test_tagged_but_fullsize_page_refused(self, monkeypatch):
        self._identify(monkeypatch, "|4000|3000\nREDUCEDIMAGE|4000|3000\n")
        assert _tiff_extra_pages_are_thumbnails(Path("x.tif")) is False

    def test_single_page_accepted(self, monkeypatch):
        self._identify(monkeypatch, "|4000|3000\n")
        assert _tiff_extra_pages_are_thumbnails(Path("x.tif")) is True

    def test_unreadable_returns_none(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _tiff_extra_pages_are_thumbnails(Path("x.tif")) is None


class TestFindFoldersExcludesOldPadded:
    """audit round 16: AutoFind walked into OLD_PADDED/ and reprocessed the backups."""

    def test_old_padded_excluded(self, tmp_path):
        backup = tmp_path / "OLD_PADDED"
        backup.mkdir()
        (backup / "a.tif").write_bytes(b"x")
        session = tmp_path / "my_padded_session"
        session.mkdir()
        (session / "b.tif").write_bytes(b"x")

        found = find_folders_by_pattern(tmp_path, ["padded"])
        assert session in found
        assert backup not in found


class TestUndoSkipsNonFiles:
    """audit round 16: a DIRECTORY named *.tif inside OLD_TIFFs made _safe_move raise
    IsADirectoryError and count as FAILED; non-files must be skipped by the move loop."""

    def test_directory_named_tif_is_skipped(self, tmp_path, monkeypatch):
        old_dir = tmp_path / "OLD_TIFFs"
        old_dir.mkdir()
        (old_dir / "dir.tif").mkdir()

        monkeypatch.setattr(convert_tiff, "step_folder", lambda cfg, *a, **k: tmp_path)
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        inputs = iter(["n", "y", "n"])  # overwrite? move? delete empty folders?
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(inputs))

        cfg = SimpleNamespace(config=ToolConfig())
        result = run_undo_old_tiffs(cfg)
        assert result is True
        assert (old_dir / "dir.tif").is_dir()


class TestConfigCorruptWarning:
    """audit round 16: a corrupt config file was swallowed silently."""

    def test_corrupt_config_warns_on_stderr(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / ".convert_tiff_config.json").write_text("{ not json")
        ConfigManager()
        assert "WARNING" in capsys.readouterr().err


class TestThumbFormatPlainTextFallback:
    """audit round 16: the plain-text path of workflow [1] passed any string to
    -ThumbFormat and the backend failed at parameter binding."""

    def test_invalid_format_falls_back_to_jpg(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        captured = []

        def fake_run_subprocess(cmd, timeout=None):
            captured.append(cmd)
            return 0

        monkeypatch.setattr(convert_tiff, "run_subprocess", fake_run_subprocess)
        answers = iter([
            "0",            # mode
            str(tmp_path),  # input folder
            "n",            # generate manifest?
            "", "", "n", "", "", "", "", "",  # workers/staging/dry/safe/skip_lzw/cap/exclude/force-parallel
            "y", "y",       # generate thumbnails? configure?
            "", "", "bmp",  # size, quality, FORMAT (invalid)
            "n",            # skip compressed with thumb?
            "y",            # proceed?
        ])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

        cfg = SimpleNamespace(config=ToolConfig(), save_config=lambda: None)
        cfg.config.ps_name = "pwsh"
        assert run_free_compress(cfg) is True

        assert "Invalid format 'bmp', using jpg." in capsys.readouterr().out
        assert captured, "the backend command was never built"
        cmd = captured[0]
        assert cmd[cmd.index("-ThumbFormat") + 1] == "jpg"


class TestWorkTimeout:
    """audit round 16: fixed timeouts false-failed multi-GB scans; whole-file magick/
    exiftool calls scale as base + size_mb * TIMEOUT_S_PER_MB with the base as floor."""

    def test_missing_path_keeps_floor(self):
        assert _work_timeout(60, "does/not/exist.tif") == 60

    def test_small_file_keeps_floor(self, tmp_path):
        f = tmp_path / "tiny.tif"
        f.write_bytes(b"x" * 100)
        assert _work_timeout(60, f) == pytest.approx(60)

    def test_scales_with_size(self, tmp_path):
        f = tmp_path / "big.tif"
        with open(f, "wb") as fh:
            fh.truncate(10 * 1024 * 1024)
        expected = 60 + 10 * convert_tiff.TIMEOUT_S_PER_MB
        assert _work_timeout(60, f) == pytest.approx(expected)

    def test_multiple_paths_add_up(self, tmp_path):
        a = tmp_path / "a.tif"
        b = tmp_path / "b.tif"
        for p in (a, b):
            with open(p, "wb") as fh:
                fh.truncate(10 * 1024 * 1024)
        expected = 120 + 20 * convert_tiff.TIMEOUT_S_PER_MB
        assert _work_timeout(120, a, b) == pytest.approx(expected)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])