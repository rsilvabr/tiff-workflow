"""Round 22: -InputDirFile against the real backends, plus the wizard's spill.

An AutoFind folder list redone by a preset grows with every session; past 32767
characters CreateProcess fails and the wizard's run_subprocess returned -1 with
nothing done. The backends accept a UTF-8 list file now (one folder per line),
and run_subprocess swaps a too-long -InputDir for it at launch time only --
journals and presets keep the plain -InputDir."""

import os
import subprocess
from pathlib import Path

import pytest

import convert_tiff
from test_regression_classes import (
    SHELLS,
    make_tiff,
    requires_shell,
    requires_tools,
    run_ps,
)

_BACKEND_CASES = []
for _shell in SHELLS:
    _BACKEND_CASES.append((_shell, "compress_tiff_zip.ps1", ["-Mode", "1"]))
if "powershell" in SHELLS:
    _BACKEND_CASES.append(("powershell", "copy_exif_to_TIFF_ps5.ps1", []))
if "pwsh" in SHELLS:
    _BACKEND_CASES.append(("pwsh", "copy_exif_to_TIFF_ps7.ps1", []))


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell,script,extra", _BACKEND_CASES)
def test_backend_reads_the_list_file(shell, script, extra, tmp_path):
    """A non-ASCII folder named only in the list file is processed."""
    plain = tmp_path / "plain"
    jp = tmp_path / "東京駅_session"
    plain.mkdir()
    jp.mkdir()
    make_tiff(plain / "a.tif")
    # exiftool.exe mangles non-ASCII argv called straight from Python (the '?' of the
    # ANSI conversion reads as a wildcard), so the TIFF is built under a safe name and
    # moved in; the bytes are a plain make_tiff TIFF.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    make_tiff(scratch / "b.tif")
    (scratch / "b.tif").rename(jp / "b.tif")
    list_file = tmp_path / "dirs.txt"
    list_file.write_text(f"{plain}\n{jp}\n", encoding="utf-8", newline="\n")

    result = run_ps(shell, script,
                    ["-InputDirFile", str(list_file), "-DryRun", "-Workers", "1"] + extra,
                    tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a.tif" in result.stdout
    assert "b.tif" in result.stdout


@requires_shell
@pytest.mark.parametrize("shell", SHELLS)
def test_both_inputdir_and_file_is_refused(shell, tmp_path):
    result = run_ps(shell, "compress_tiff_zip.ps1",
                    ["-Mode", "1", "-InputDir", str(tmp_path), "-InputDirFile",
                     str(tmp_path / "dirs.txt"), "-DryRun"],
                    tmp_path)
    assert result.returncode == 1
    assert "not both" in result.stdout


@requires_shell
@pytest.mark.parametrize("shell", SHELLS)
def test_missing_or_empty_list_file_is_refused(shell, tmp_path):
    missing = run_ps(shell, "compress_tiff_zip.ps1",
                     ["-Mode", "1", "-InputDirFile", str(tmp_path / "nope.txt"),
                      "-DryRun"],
                     tmp_path)
    assert missing.returncode == 1

    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8", newline="\n")
    empty = run_ps(shell, "compress_tiff_zip.ps1",
                   ["-Mode", "1", "-InputDirFile", str(empty_file), "-DryRun"],
                   tmp_path)
    assert empty.returncode == 1


def _long_folders():
    return [Path("C:/Fixtures") / (f"folder_{i:03d}_" + "x" * 60) for i in range(600)]


def test_spill_rewrites_only_long_commands(tmp_path):
    short = convert_tiff.build_compress_command(
        {"mode": 1, "dry_run": True, "workers": 1}, [tmp_path / "one"], ps_name="pwsh")
    out, spill = convert_tiff._spill_long_input_dir(short)
    assert out == short
    assert spill is None

    folders = _long_folders()
    expected = [str(f) for f in folders]

    long_cmd = convert_tiff.build_compress_command(
        {"mode": 1, "dry_run": True, "workers": 1}, folders, ps_name="pwsh")
    assert len(subprocess.list2cmdline(long_cmd)) > 32767
    out, spill = convert_tiff._spill_long_input_dir(long_cmd)
    try:
        assert spill is not None
        assert "-InputDirFile" in out
        assert "-InputDir" not in out
        assert out[out.index("-InputDirFile") + 1] == spill
        assert Path(spill).read_text(encoding="utf-8").splitlines() == expected
    finally:
        if spill:
            os.unlink(spill)

    long_ps5 = convert_tiff.build_compress_command(
        {"mode": 1, "dry_run": True, "workers": 1}, folders, ps_name="powershell")
    out, spill = convert_tiff._spill_long_input_dir(long_ps5)
    try:
        assert spill is not None
        parts = convert_tiff._journal_parts(out)
        assert "-InputDirFile" in parts
        assert "-InputDir" not in parts
        assert parts[parts.index("-InputDirFile") + 1] == spill
    finally:
        if spill:
            os.unlink(spill)

    thumb_cmd = ["pwsh", "-NoProfile", "-File",
                 str(convert_tiff.SCRIPT_DIR / "generate_thumbnails.ps1"),
                 "-InputDir", "C:\\fixtures" + "\\x" * 15000, "-DryRun"]
    out, spill = convert_tiff._spill_long_input_dir(thumb_cmd)
    assert out == thumb_cmd
    assert spill is None

    base_args = ["-Mode", "1", "-InputDir", ";".join(expected), "-DryRun"]
    for ps_name in ("pwsh", "powershell"):
        rebuilt = convert_tiff._rebuild_journaled_command(
            [str(convert_tiff.SCRIPT_DIR / "compress_tiff_zip.ps1")] + base_args,
            ps_name, non_interactive=True)
        out, spill = convert_tiff._spill_long_input_dir(rebuilt)
        try:
            assert spill is not None
            assert "-NonInteractive" in out
            parts = convert_tiff._journal_parts(out)
            assert "-InputDirFile" in parts
            assert "-InputDir" not in parts
        finally:
            if spill:
                os.unlink(spill)


@requires_shell
@requires_tools
@pytest.mark.parametrize("shell", SHELLS)
def test_wizard_runs_a_command_past_the_limit(shell, tmp_path, monkeypatch, capsys):
    folders = []
    for k in range(500):
        folder = tmp_path / (f"s{k:03d}_" + "x" * 60)
        folder.mkdir()
        folders.append(folder)
    make_tiff(folders[0] / "first.tif")
    make_tiff(folders[-1] / "last.tif")

    cmd = convert_tiff.build_compress_command(
        {"mode": 1, "dry_run": True, "workers": 1}, folders, ps_name=shell)
    assert len(subprocess.list2cmdline(cmd)) > 32767

    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    monkeypatch.chdir(tmp_path)

    original = convert_tiff._spill_long_input_dir
    seen = {}

    def recorder(inner_cmd):
        result, path = original(inner_cmd)
        seen["path"] = path
        return result, path

    monkeypatch.setattr(convert_tiff, "_spill_long_input_dir", recorder)

    rc = convert_tiff.run_subprocess(cmd, timeout=600)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "first.tif" in out
    assert "last.tif" in out
    assert seen.get("path") is not None
    assert not os.path.exists(seen["path"])
