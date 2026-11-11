"""A preset or last run saved by the PowerShell edition (up to v2.9.1) still replays.

Its journal holds a PowerShell command line: `pwsh -NoProfile -File <script>.ps1 args`, or
for Windows PowerShell 5.1 `powershell -NoProfile -Command "& '<script>.ps1' args"`. The
arguments mean the same to the Python backends (tiff_workflow_common.parse_args binds them
the way PowerShell did), so _journal_parts reduces either form to [script, *args] and
_rebuild_journaled_command runs today's script, the copy next to the wizard, under this
Python. Anything that is not one of the backends is refused, never guessed.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import convert_tiff  # noqa: E402
from convert_tiff import ToolConfig  # noqa: E402

ARGS = ["-Mode", "9", "-InputDir", "C:\\Fotos\\it's here", "-Workers", "8",
        "-SafeMode:$false", "-SkipLzwAsCompressed:$false"]


def expected(script):
    return [sys.executable, str(convert_tiff.SCRIPT_DIR / script)] + ARGS


@pytest.mark.parametrize("saved,script", [
    ("compress_tiff_zip.ps1", "compress_tiff_zip.py"),
    ("copy_exif_to_TIFF_ps7.ps1", "copy_exif_to_tiff.py"),
    ("copy_exif_to_TIFF_ps5.ps1", "copy_exif_to_tiff.py"),
    ("generate_thumbnails.ps1", "generate_thumbnails.py"),
])
def test_a_pwsh_file_journal_replays_under_python(saved, script):
    old = ["pwsh", "-NoProfile", "-NonInteractive", "-File", "D:\\old copy\\" + saved] + ARGS
    parts = convert_tiff._journal_parts(old)
    assert parts[1:] == ARGS
    assert convert_tiff._rebuild_journaled_command(parts) == expected(script)


def test_a_ps5_command_journal_replays_under_python():
    invocation = ("& 'D:\\old copy\\compress_tiff_zip.ps1' -Mode '9' -InputDir "
                  "'C:\\Fotos\\it''s here' -Workers '8' -SafeMode:$false -SkipLzwAsCompressed:$false")
    old = ["powershell", "-NoProfile", "-Command", invocation]
    parts = convert_tiff._journal_parts(old)
    assert parts[1:] == ARGS, "the quoting of the 5.1 wrapper must unwrap exactly"
    assert convert_tiff._rebuild_journaled_command(parts) == expected("compress_tiff_zip.py")


def test_todays_journal_round_trips():
    cmd = convert_tiff.build_compress_command(
        {"mode": 9, "workers": 8, "safe_mode": False, "skip_lzw": False},
        folders=[Path("C:\\Fotos\\it's here")])
    assert cmd == expected("compress_tiff_zip.py")
    parts = convert_tiff._journal_parts(cmd)
    assert parts == cmd[1:]
    assert convert_tiff._rebuild_journaled_command(parts) == cmd


def test_a_journal_saved_from_another_folder_runs_this_copy():
    """Round 24: a preset saved from another copy ran that copy's backend."""
    cmd = [sys.executable, "D:\\somewhere else\\copy_exif_to_tiff.py", "-InputDir", "x"]
    rebuilt = convert_tiff._rebuild_journaled_command(convert_tiff._journal_parts(cmd))
    assert rebuilt == [sys.executable, str(convert_tiff.SCRIPT_DIR / "copy_exif_to_tiff.py"),
                       "-InputDir", "x"]


@pytest.mark.parametrize("args,deletes", [
    (["-Mode", "8", "-InputDir", "x"], True),
    (["-Mode", "9", "-InputDir", "x", "-DeleteSource"], True),
    (["-Mode", "9", "-InputDir", "x"], False),
])
def test_a_powershell_journal_is_read_for_what_it_deletes(args, deletes):
    old = ["pwsh", "-NoProfile", "-File", "X:\\compress_tiff_zip.ps1"] + args
    assert convert_tiff._parts_replace_sources(convert_tiff._journal_parts(old)) is deletes


@pytest.mark.parametrize("commands", [
    [["pwsh", "-NoProfile", "-EncodedCommand", "AAA="]],
    [["pwsh", "-NoProfile", "-File", "C:\\x\\something_else.ps1", "-InputDir", "x"]],
    [[sys.executable, "C:\\x\\not_a_backend.py"]],
    [["powershell", "-NoProfile", "-Command", "Remove-Item C:\\x -Recurse"]],
    [["powershell", "-NoProfile", "-Command", "& 'C:\\x\\compress_tiff_zip.ps1' -InputDir 'unclosed"]],
])
def test_an_unrecognised_journal_is_refused(monkeypatch, commands):
    """Never guess, and never hand Python a script that is not one of the backends."""
    ran = []
    monkeypatch.setattr(convert_tiff, "run_subprocess", lambda *a, **k: ran.append(a) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ToolConfig())
    cfg.config.last_run_kind = "compress"
    cfg.config.last_run_label = "something"
    cfg.config.last_run_commands = commands
    assert convert_tiff.run_repeat_last(cfg) is False
    assert ran == []


def test_a_powershell_preset_runs_the_python_backend(monkeypatch):
    """The whole path: a v2.x compress preset, replayed from the menu."""
    ran = []
    monkeypatch.setattr(convert_tiff, "run_subprocess", lambda cmd, **k: ran.append(list(cmd)) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ToolConfig())
    entry = {"kind": "compress", "label": "Compress: mode 9", "time": "2026-11-01 10:00",
             "commands": [["pwsh", "-NoProfile", "-File", "D:\\gone\\compress_tiff_zip.ps1"] + ARGS]}
    assert convert_tiff._run_journal(cfg, entry, "Preset: old") == 0
    assert ran == [expected("compress_tiff_zip.py")]
