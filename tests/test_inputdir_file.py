"""Round 22: --input-dir-file against the real backends, plus the wizard's spill.

An AutoFind folder list redone by a preset grows with every session; past 32767
characters CreateProcess fails and the wizard's run_subprocess returned -1 with
nothing done. The backends accept a UTF-8 list file now (one folder per line),
and run_subprocess swaps a too-long --input-dir for it at launch time only --
journals and presets keep the plain --input-dir."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import convert_tiff
from test_regression_classes import (
    LANES,
    make_tiff,
    requires_tools,
    run_ps,
)

_BACKEND_CASES = []
for _lane in LANES:
    _BACKEND_CASES.append((_lane, "compress_tiff_zip.py", ["--mode", "1"]))
    _BACKEND_CASES.append((_lane, "copy_exif_to_tiff.py", []))


@requires_tools
@pytest.mark.parametrize("lane,script,extra", _BACKEND_CASES)
def test_backend_reads_the_list_file(lane, script, extra, tmp_path):
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

    result = run_ps(lane, script,
                    ["--input-dir-file", str(list_file), "--dry-run", "--workers", "1"] + extra,
                    tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a.tif" in result.stdout
    assert "b.tif" in result.stdout


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("script, mode", [("compress_tiff_zip.py", ["--mode", "1"]),
                                          ("copy_exif_to_tiff.py", [])])
def test_both_inputdir_and_file_is_refused(lane, tmp_path, script, mode):
    """A usage error since v3.1 (T5): exit 2, said before the run starts -- no log."""
    result = run_ps(lane, script,
                    mode + ["--input-dir", str(tmp_path), "--input-dir-file",
                            str(tmp_path / "dirs.txt"), "--dry-run"],
                    tmp_path)
    assert result.returncode == 2, result.stdout
    assert "ERROR: pass --input-dir or --input-dir-file, not both" in result.stdout
    assert not (tmp_path / "Logs").exists()


@pytest.mark.parametrize("lane", LANES)
def test_missing_or_empty_list_file_is_refused(lane, tmp_path):
    missing = run_ps(lane, "compress_tiff_zip.py",
                     ["--mode", "1", "--input-dir-file", str(tmp_path / "nope.txt"),
                      "--dry-run"],
                     tmp_path)
    assert missing.returncode == 1

    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8", newline="\n")
    empty = run_ps(lane, "compress_tiff_zip.py",
                   ["--mode", "1", "--input-dir-file", str(empty_file), "--dry-run"],
                   tmp_path)
    assert empty.returncode == 1


def _long_folders():
    return [Path("C:/Fixtures") / (f"folder_{i:03d}_" + "x" * 60) for i in range(600)]


def test_spill_rewrites_only_long_commands(tmp_path):
    short = convert_tiff.build_compress_command(
        {"mode": 1, "dry_run": True, "workers": 1}, [tmp_path / "one"])
    out, spill = convert_tiff._spill_long_input_dir(short)
    assert out == short
    assert spill is None

    folders = _long_folders()
    expected = [str(f) for f in folders]

    long_cmd = convert_tiff.build_compress_command(
        {"mode": 1, "dry_run": True, "workers": 1}, folders)
    assert len(subprocess.list2cmdline(long_cmd)) > 32767
    out, spill = convert_tiff._spill_long_input_dir(long_cmd)
    try:
        assert spill is not None
        assert "--input-dir-file" in out
        assert "--input-dir" not in out
        assert out[out.index("--input-dir-file") + 1] == spill
        assert Path(spill).read_text(encoding="utf-8").splitlines() == expected
    finally:
        if spill:
            os.unlink(spill)

    # generate_thumbnails.py has no -InputDirFile: its command line is left alone
    thumb_cmd = convert_tiff._backend_command(
        "generate_thumbnails.py", ["--input-dir", "C:\\fixtures" + "\\x" * 15000, "--dry-run"])
    out, spill = convert_tiff._spill_long_input_dir(thumb_cmd)
    assert out == thumb_cmd
    assert spill is None

    # a preset: its saved step [compress, *args], run with this copy's backend
    base_args = ["--mode", "1", "--input-dir", ";".join(expected), "--dry-run"]
    step = convert_tiff._journal_step(["compress"] + base_args)
    rebuilt = convert_tiff._backend_command(step.script, step.args)
    out, spill = convert_tiff._spill_long_input_dir(rebuilt)
    try:
        assert spill is not None
        assert out[:3] == rebuilt[:3] == [sys.executable, str(convert_tiff.ENTRY), "compress"]
        assert out[3:] == ["--mode", "1", "--input-dir-file", spill, "--dry-run"]
    finally:
        if spill:
            os.unlink(spill)


@requires_tools
@pytest.mark.parametrize("lane", LANES)
def test_wizard_runs_a_command_past_the_limit(lane, tmp_path, monkeypatch, capsys):
    folders = []
    for k in range(500):
        folder = tmp_path / (f"s{k:03d}_" + "x" * 60)
        folder.mkdir()
        folders.append(folder)
    make_tiff(folders[0] / "first.tif")
    make_tiff(folders[-1] / "last.tif")

    cmd = convert_tiff.build_compress_command(
        {"mode": 1, "dry_run": True, "workers": 1,
         "force_sequential": lane == "sequential"}, folders)
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
