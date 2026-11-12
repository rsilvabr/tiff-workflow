"""What a journal (a preset, the last run) holds, and what it refuses (v3.1, T8).

A journalled step is [subcommand, *args] -- ["compress", "--mode", "9", ...] --, written by
_journal_line from the command line the wizard ran. _journal_step reads it back with the
backend's own table and reader (tw.parse_args), and the wizard runs the backend next to it
with this Python. No folder is kept, so a preset saved from another copy runs this copy's
backend (round 24). Anything else is refused whole -- nothing runs, never a guess: what
v3.0.0 saved ([<python>, <folder>/compress_tiff_zip.py, -Mode 9 ...]), what the PowerShell
edition saved before it (pwsh -File <script>.ps1, powershell -Command), a line its backend
would refuse.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import convert_tiff  # noqa: E402
from convert_tiff import ToolConfig  # noqa: E402

FOLDER = "C:\\Fotos\\it's 写真 [x] 50%"
ARGS = ["--mode", "9", "--input-dir", FOLDER, "--workers", "8", "--no-safe-mode",
        "--no-skip-lzw-as-compressed"]

# What v3.0.0 and the PowerShell edition saved: the same run.
OLD_ARGS = ["-Mode", "9", "-InputDir", FOLDER, "-Workers", "8", "-SafeMode:$false",
            "-SkipLzwAsCompressed:$false"]
PS5_INVOCATION = ("& 'D:\\old copy\\compress_tiff_zip.ps1' -Mode '9' -InputDir "
                  "'C:\\Fotos\\it''s 写真 [x] 50%' -Workers '8' -SafeMode:$false "
                  "-SkipLzwAsCompressed:$false")
OLD_FORMS = [
    [sys.executable, "D:\\old copy\\compress_tiff_zip.py"] + OLD_ARGS,
    ["python", "compress_tiff_zip.py"] + OLD_ARGS,
    ["compress"] + OLD_ARGS,
    ["pwsh", "-NoProfile", "-NonInteractive", "-File", "D:\\old copy\\compress_tiff_zip.ps1"] + OLD_ARGS,
    ["pwsh", "-NoProfile", "-NonInteractive", "-File", "D:\\old copy\\copy_exif_to_TIFF_ps7.ps1"] + OLD_ARGS,
    ["pwsh", "-NoProfile", "-NonInteractive", "-File", "D:\\old copy\\generate_thumbnails.ps1"] + OLD_ARGS,
    ["powershell", "-NoProfile", "-Command", PS5_INVOCATION],
]
OLD_IDS = ["v3.0.0", "v3.0.0-relative", "v3.0.0-args", "pwsh-compress", "pwsh-copy_exif_ps7",
           "pwsh-thumbnails", "ps5-command"]


def _todays_cmd():
    return convert_tiff.build_compress_command(
        {"mode": 9, "workers": 8, "safe_mode": False, "skip_lzw": False},
        folders=[Path(FOLDER)])


def test_todays_journal_round_trips():
    cmd = _todays_cmd()
    assert cmd == [sys.executable, str(convert_tiff.SCRIPT_DIR / "compress_tiff_zip.py")] + ARGS
    line = convert_tiff._journal_line(cmd)
    assert line == ["compress"] + ARGS
    step = convert_tiff._journal_step(line)
    assert step.script == "compress_tiff_zip.py" and step.args == ARGS
    assert step.values["mode"] == 9 and step.values["safe_mode"] is False
    assert convert_tiff._backend_command(step.script, step.args) == cmd


@pytest.mark.parametrize("sub, script", sorted(convert_tiff.SUBCOMMANDS.items()))
def test_every_backend_has_its_journal_name(sub, script):
    cmd = convert_tiff._backend_command(script, [])
    assert convert_tiff._journal_line(cmd) == [sub]
    assert convert_tiff._journal_step([sub]).script == script


@pytest.mark.parametrize("cmd", [
    [sys.executable, "C:\\x\\not_a_backend.py", "--mode", "9"],
    ["pwsh", "-File", "compress_tiff_zip.ps1"], [sys.executable], []])
def test_only_a_backend_command_line_is_journalled(cmd):
    with pytest.raises(ValueError):
        convert_tiff._journal_line(cmd)


@pytest.mark.parametrize("old", OLD_FORMS, ids=OLD_IDS)
def test_an_older_form_is_not_read(old):
    assert convert_tiff._journal_step(old) is None


@pytest.mark.parametrize("line", [
    ["compress", "--mode", "9", "--no-such-option"],
    ["compress", "--mo", "9"],                      # an abbreviation
    ["compress", "--Mode", "9"],                    # another case
    ["compress", "--workers", "65"],                # out of range
    ["compress", "--safe-mode=no"],
    ["compress", "9"],
    ["copy-exif", "--mode", "9"],                   # an option of another backend
    ["Compress", "--mode", "9"], ["compress_tiff_zip.py", "--mode", "9"],
    "compress --mode 9", None,
])
def test_a_line_its_backend_would_refuse_is_not_read(line):
    assert convert_tiff._journal_step(line) is None


def test_a_backend_whose_table_cannot_be_read_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", tmp_path)
    convert_tiff._backend_tables.cache_clear()
    try:
        assert convert_tiff._journal_step(["compress"] + ARGS) is None
    finally:
        convert_tiff._backend_tables.cache_clear()


@pytest.mark.parametrize("args, deletes", [
    (["--mode", "8", "--input-dir", "x"], True),
    (["--mode=8", "--input-dir", "x"], True),
    (["--mode", " 08 ", "--input-dir", "x"], True),
    (["--mode", "9", "--input-dir", "x", "--delete-source"], True),
    (["--mode", "9", "--input-dir", "x", "--no-delete-source", "--delete-source"], True),
    (["--mode", "9", "--input-dir", "x", "--delete-source", "--no-delete-source"], False),
    (["--mode", "8", "--mode", "9", "--input-dir", "x"], False),
    (["--mode", "9", "--input-dir", "x"], False),
])
def test_a_journal_is_read_for_what_it_deletes(args, deletes):
    step = convert_tiff._journal_step(["compress"] + args)
    assert convert_tiff._step_replaces_sources(step) is deletes


def test_the_option_changed_is_the_one_the_backend_binds():
    """AutoFind and the long-line spill replace --input-dir: the last one given, in either
    form, never --input-dir-file."""
    at = convert_tiff._option_at
    assert at(["--input-dir", "a", "--mode", "9", "--input-dir=b"], "input_dir") == (4, 1, "b")
    assert at(["--input-dir=a", "--input-dir", "b"], "input_dir") == (1, 2, "b")
    assert at(["--input-dir-file", "x", "--input-dir"], "input_dir") is None
    assert at(["--mode", "9"], "input_dir") is None


def test_autofind_replaces_the_folders_the_backend_reads(tmp_path, monkeypatch):
    found = [tmp_path / "S5pro_a", tmp_path / "S5pro_b"]
    for f in found:
        f.mkdir()
    monkeypatch.setattr(convert_tiff, "find_folders_by_pattern", lambda root, pats: found)
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    step = convert_tiff._journal_step(["copy-exif", "--input-dir", "old 1", "--workers", "4",
                                       "--input-dir=old 2"])
    entry = {"kind": "copy_exif", "label": "Fuji: Copy EXIF", "autofind":
             {"root": str(tmp_path), "patterns": ["S5pro"]}}
    assert convert_tiff._redo_autofind(entry, [step]) is None
    values, _ = convert_tiff.tw.parse_args(step.args, convert_tiff._backend_tables(step.script)[0])
    assert values["input_dir"] == ";".join(str(f) for f in found)


def _repeat(monkeypatch, commands):
    ran = []
    monkeypatch.setattr(convert_tiff, "run_subprocess", lambda *a, **k: ran.append(a) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ToolConfig())
    cfg.config.last_run_kind = "compress"
    cfg.config.last_run_label = "something"
    cfg.config.last_run_commands = commands
    return convert_tiff.run_repeat_last(cfg), ran


@pytest.mark.parametrize("commands", [
    [["pwsh", "-NoProfile", "-EncodedCommand", "AAA="]],
    [["pwsh", "-NoProfile", "-File", "C:\\x\\something_else.ps1", "-InputDir", "x"]],
    [[sys.executable, "C:\\x\\not_a_backend.py"]],
    [["powershell", "-NoProfile", "-Command", "Remove-Item C:\\x -Recurse"]],
    [["powershell", "-NoProfile", "-Command", "& 'C:\\x\\compress_tiff_zip.ps1' -InputDir 'unclosed"]],
    [OLD_FORMS[0]], [OLD_FORMS[3]], [OLD_FORMS[6]],
    # one good step does not carry a bad one
    [["compress"] + ARGS, OLD_FORMS[0]],
    [["compress"] + ARGS, ["compress", "--mode", "9", "--no-such-option"]],
])
def test_an_unrecognised_journal_is_refused(monkeypatch, commands):
    """Never guess, and never hand Python a script that is not one of the backends."""
    ok, ran = _repeat(monkeypatch, commands)
    assert ok is False and ran == []


def test_an_old_preset_is_refused(monkeypatch, capsys):
    """The whole path: a v3.0.0 compress preset, replayed from the menu. Nothing runs, the
    message says to save it again, and the preset stays in the config."""
    ran = []
    monkeypatch.setattr(convert_tiff, "run_subprocess", lambda cmd, **k: ran.append(list(cmd)) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ToolConfig())
    entry = {"kind": "compress", "label": "Compress: mode 9", "time": "2026-11-01 10:00",
             "commands": [OLD_FORMS[0]]}
    assert convert_tiff._journal_runnable(entry), "an old preset is kept in the config"
    assert convert_tiff._run_journal(cfg, entry, "Preset: old") == 1
    assert ran == []
    out = capsys.readouterr().out
    assert "cannot be replayed" in out and "save it again" in out


def test_the_same_preset_in_todays_form_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(convert_tiff, "run_subprocess", lambda cmd, **k: ran.append(list(cmd)) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ToolConfig())
    entry = {"kind": "compress", "label": "Compress: mode 9", "time": "2026-11-01 10:00",
             "commands": [["compress"] + ARGS]}
    assert convert_tiff._run_journal(cfg, entry, "Preset: today") == 0
    assert ran == [_todays_cmd()]


def test_a_dry_run_of_a_preset_adds_the_switch_once(monkeypatch):
    ran = []
    monkeypatch.setattr(convert_tiff, "run_subprocess", lambda cmd, **k: ran.append(list(cmd)) or 0)
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ToolConfig())
    for line in (["compress"] + ARGS, ["compress"] + ARGS + ["--dry-run"]):
        ran.clear()
        entry = {"kind": "compress", "label": "Compress: mode 9", "time": "t", "commands": [line]}
        assert convert_tiff._run_journal(cfg, entry, "Preset", unattended=True, dry_run=True) == 0
        assert ran[0][2:].count("--dry-run") == 1 and ran[0][-1] == "--dry-run"
