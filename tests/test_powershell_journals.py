"""What a journal (a preset, the last run) may hold -- and that the PowerShell form is refused.

A journalled command is `[<python>, <folder>/<backend>.py, *args]`: _journal_parts reduces it
to [script, *args] and _rebuild_journaled_command runs today's script, the copy next to the
wizard, under this Python. Until v3.0.0 the wizard also read what the PowerShell edition (up
to v2.9.1) had saved: `pwsh -NoProfile -File <script>.ps1 args`, or for Windows PowerShell 5.1
`powershell -NoProfile -Command "& '<script>.ps1' args"`. Since v3.1 it does not: such a
preset stays in the config, and running it is refused -- nothing runs, never a guess.
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

# The well-formed lines v3.0.0 still replayed.
PS5_INVOCATION = ("& 'D:\\old copy\\compress_tiff_zip.ps1' -Mode '9' -InputDir "
                  "'C:\\Fotos\\it''s here' -Workers '8' -SafeMode:$false -SkipLzwAsCompressed:$false")
POWERSHELL_FORMS = [
    ["pwsh", "-NoProfile", "-NonInteractive", "-File", "D:\\old copy\\compress_tiff_zip.ps1"] + ARGS,
    ["pwsh", "-NoProfile", "-NonInteractive", "-File", "D:\\old copy\\copy_exif_to_TIFF_ps7.ps1"] + ARGS,
    ["pwsh", "-NoProfile", "-NonInteractive", "-File", "D:\\old copy\\copy_exif_to_TIFF_ps5.ps1"] + ARGS,
    ["pwsh", "-NoProfile", "-NonInteractive", "-File", "D:\\old copy\\generate_thumbnails.ps1"] + ARGS,
    ["powershell", "-NoProfile", "-Command", PS5_INVOCATION],
]
POWERSHELL_IDS = ["pwsh-compress", "pwsh-copy_exif_ps7", "pwsh-copy_exif_ps5", "pwsh-thumbnails",
                  "ps5-command"]


def expected(script):
    return [sys.executable, str(convert_tiff.SCRIPT_DIR / script)] + ARGS


@pytest.mark.parametrize("old", POWERSHELL_FORMS, ids=POWERSHELL_IDS)
def test_a_powershell_journal_is_not_read(old):
    assert convert_tiff._journal_parts(old) is None


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


@pytest.mark.parametrize("script", ["C:\\x\\not_a_backend.py", "D:\\old copy\\compress_tiff_zip.ps1",
                                    "C:\\Windows\\System32\\cmd.exe"])
def test_only_a_backend_becomes_a_script_path(script):
    """_journal_parts lets nothing else through; if something ever did, the path is not
    built -- a foreign script is never handed to Python."""
    with pytest.raises(ValueError):
        convert_tiff._journaled_script_path([script, "-InputDir", "x"])
    with pytest.raises(ValueError):
        convert_tiff._rebuild_journaled_command([script, "-InputDir", "x"])


@pytest.mark.parametrize("args,deletes", [
    (["-Mode", "8", "-InputDir", "x"], True),
    (["-Mode", "9", "-InputDir", "x", "-DeleteSource"], True),
    (["-Mode", "9", "-InputDir", "x"], False),
])
def test_a_journal_is_read_for_what_it_deletes(args, deletes):
    saved = [sys.executable, "X:\\compress_tiff_zip.py"] + args
    assert convert_tiff._parts_replace_sources(convert_tiff._journal_parts(saved)) is deletes


@pytest.mark.parametrize("commands", [
    [["pwsh", "-NoProfile", "-EncodedCommand", "AAA="]],
    [["pwsh", "-NoProfile", "-File", "C:\\x\\something_else.ps1", "-InputDir", "x"]],
    [[sys.executable, "C:\\x\\not_a_backend.py"]],
    [["powershell", "-NoProfile", "-Command", "Remove-Item C:\\x -Recurse"]],
    [["powershell", "-NoProfile", "-Command", "& 'C:\\x\\compress_tiff_zip.ps1' -InputDir 'unclosed"]],
    # the two forms v3.0.0 replayed
    [POWERSHELL_FORMS[0]],
    [POWERSHELL_FORMS[4]],
    # one good command does not carry a bad one
    [expected("compress_tiff_zip.py"), POWERSHELL_FORMS[0]],
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


def test_a_powershell_preset_is_refused(monkeypatch, capsys):
    """The whole path: a v2.x compress preset, replayed from the menu. It ran the Python
    backend until v3.0.0; now nothing runs and the message says to save it again."""
    ran = []
    monkeypatch.setattr(convert_tiff, "run_subprocess", lambda cmd, **k: ran.append(list(cmd)) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ToolConfig())
    entry = {"kind": "compress", "label": "Compress: mode 9", "time": "2026-11-01 10:00",
             "commands": [["pwsh", "-NoProfile", "-File", "D:\\gone\\compress_tiff_zip.ps1"] + ARGS]}
    assert convert_tiff._journal_runnable(entry), "an old preset is kept in the config"
    assert convert_tiff._run_journal(cfg, entry, "Preset: old") == 1
    assert ran == []
    assert "cannot be replayed" in capsys.readouterr().out


def test_the_same_preset_in_todays_form_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(convert_tiff, "run_subprocess", lambda cmd, **k: ran.append(list(cmd)) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ToolConfig())
    entry = {"kind": "compress", "label": "Compress: mode 9", "time": "2026-11-01 10:00",
             "commands": [["python", "D:\\gone\\compress_tiff_zip.py"] + ARGS]}
    assert convert_tiff._run_journal(cfg, entry, "Preset: converted") == 0
    assert ran == [expected("compress_tiff_zip.py")]
