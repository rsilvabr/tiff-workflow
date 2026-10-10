"""
Tests for presets, --run-preset and the shared journal runner (audit round 19).

The plan's T1-T10: a manifest repeat keeps the run's answers (P1), an old
manifest journal is refused, the preset menu saves/runs/deletes through the
same _run_journal as "Repeat last workflow", a preset is a copy, Fuji journals
redo the AutoFind (P2), unattended runs refuse deleting presets and never
prompt, malformed presets in a hand-edited config are discarded, and the CLI
(--run-preset/--list-presets/--dry-run) drives a real compression end to end.

The mock tests are fast; the CLI tests skip without pwsh + ImageMagick +
exiftool and run a real mode-1 compression in a temp folder.
"""

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import convert_tiff
from convert_tiff import (
    ToolConfig,
    build_compress_command,
    build_copy_exif_command,
)
from test_regression_classes import make_tiff

REPO = Path(__file__).resolve().parent.parent

requires_preset_cli = pytest.mark.skipif(
    shutil.which("pwsh") is None or shutil.which("magick") is None
    or shutil.which("exiftool") is None,
    reason="pwsh + ImageMagick + exiftool required for the CLI end-to-end run",
)


# --- Helpers --------------------------------------------------------


def _cfg(**overrides):
    """Config stand-in with a no-op save (never touches the real JSON)."""
    return SimpleNamespace(
        config=ToolConfig(ps_name="pwsh", ps_major=7, **overrides),
        save_config=lambda: None,
    )


def _isolated_manager(tmp_path, monkeypatch):
    """A real ConfigManager whose JSON lives under tmp_path."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return convert_tiff.ConfigManager()


def _write_manifest(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Source", "Destination", "Mode"])
        writer.writerows(rows)
    return path


def _entry(kind, label, commands=None, **extra):
    entry = {
        "kind": kind, "label": label, "commands": commands,
        "time": "2026-10-10 03:00:00", "manifest_path": None,
        "manifest_workflow": None, "manifest_default_mode": None, "autofind": None,
    }
    entry.update(extra)
    return entry


def _confirm(monkeypatch, value):
    """Confirm.ask exists only when rich is importable (see test_manifest)."""
    if convert_tiff.RICH_AVAILABLE:
        monkeypatch.setattr(convert_tiff.Confirm, "ask",
                            staticmethod(lambda *a, **k: value))
    else:
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y" if value else "n")


def _prompt(monkeypatch, value):
    if convert_tiff.RICH_AVAILABLE:
        monkeypatch.setattr(convert_tiff.Prompt, "ask",
                            staticmethod(lambda *a, **k: value))
    else:
        monkeypatch.setattr("builtins.input", lambda *a, **k: str(value))


@pytest.fixture
def calls(monkeypatch):
    """Spy on run_subprocess: each call recorded as {'cmd': [...], 'kw': {...}}."""
    recorded = []

    def fake(cmd, timeout=None, **kw):
        recorded.append({"cmd": cmd, "kw": kw})
        return 0

    monkeypatch.setattr(convert_tiff, "run_subprocess", fake)
    return recorded


def _compress_entry(in_dir, mode=1, delete_source=False):
    wf = {"mode": mode, "workers": 1, "staging": "", "dry_run": False}
    if delete_source:
        wf["delete_source"] = True
    cmd = build_compress_command(wf, folders=[Path(in_dir)], ps_name="pwsh")
    label = f"Compress TIFFs -- mode {mode} -- {in_dir}"
    if delete_source:
        label += " [DELETES SOURCES]"
    return _entry("compress", label, [cmd])


def _run_cli(args, tmp_path, timeout=300):
    env = os.environ.copy()
    env["USERPROFILE"] = str(tmp_path)
    env["HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, str(REPO / "convert_tiff.py")] + args,
        cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )


def _write_preset_config(tmp_path, presets):
    (tmp_path / ".convert_tiff_config.json").write_text(
        json.dumps({"presets": presets}), encoding="utf-8")


# --- T1: P1 -- a manifest repeat keeps the run's answers -------------


class TestRepeatManifestKeepsRunSettings:
    def test_repeat_manifest_keeps_the_run_settings(self, tmp_path, monkeypatch, calls):
        csv1 = tmp_path / "manifest_1.csv"
        src1 = tmp_path / "s1"
        src1.mkdir()
        _write_manifest(csv1, [(str(src1), str(src1), "")])  # no Mode -> default asked
        csv2 = tmp_path / "manifest_2.csv"
        src2 = tmp_path / "s2"
        src2.mkdir()
        _write_manifest(csv2, [(str(src2), str(src2 / "flat"), 2)])  # mode 2 -> duplicate asked

        cfg = _cfg()
        cfg.config.last_mode = 8  # must NOT leak into the repeat (P1)

        def fake_basic_params(c, wf):
            wf["workers"] = 8
            wf["staging"] = ""
            wf["safe_mode"] = False
            wf["exclude_folders"] = "RAW"
            wf["cap_workers"] = 2
            return True

        monkeypatch.setattr(convert_tiff, "step_basic_params", fake_basic_params)
        monkeypatch.setattr(convert_tiff, "confirm_manifest_entries", lambda p, e: True)

        # Run 1: a row without Mode -> the default mode is asked once
        monkeypatch.setattr(convert_tiff, "pick_manifest", lambda: str(csv1))
        _prompt(monkeypatch, "3")
        assert convert_tiff.run_manifest_workflow(cfg) is True

        # Repeat 1: mode 3 (the saved default), not last_mode=8
        _confirm(monkeypatch, True)
        assert convert_tiff.run_repeat_last(cfg) is True
        cmd1 = calls[-1]["cmd"]
        assert cmd1[cmd1.index("-Mode") + 1] == "3", \
            "rows without Mode must use the mode the run saved, not last_mode"
        assert "-SafeMode:$false" in cmd1, "the repeat lost the Safe mode answer"
        assert cmd1[cmd1.index("-ExcludeFolders") + 1] == "RAW", \
            "the repeat lost the -ExcludeFolders answer"
        assert cmd1[cmd1.index("-CapWorkers") + 1] == "2", \
            "the repeat lost the worker cap"

        saved = getattr(cfg.config, "last_manifest_workflow", None)
        assert saved is not None, "the manifest run must save its settings with the journal"
        assert saved["safe_mode"] is False

        # Run 2: a mode-2 manifest, duplicate policy answered
        monkeypatch.setattr(convert_tiff, "pick_manifest", lambda: str(csv2))
        _prompt(monkeypatch, "Skip")
        assert convert_tiff.run_manifest_workflow(cfg) is True

        # Repeat 2: the same answers, including -DuplicateAction Skip
        _confirm(monkeypatch, True)
        assert convert_tiff.run_repeat_last(cfg) is True
        cmd2 = calls[-1]["cmd"]
        assert cmd2[cmd2.index("-Mode") + 1] == "2"
        assert cmd2[cmd2.index("-DuplicateAction") + 1] == "Skip", \
            "the repeat forced its own duplicate policy instead of the run's"
        assert "-SafeMode:$false" in cmd2


# --- T2: an old manifest journal is refused --------------------------


class TestOldManifestJournalRefused:
    def test_old_manifest_journal_is_refused(self, tmp_path, monkeypatch, calls):
        src = tmp_path / "a"
        src.mkdir()
        manifest = _write_manifest(tmp_path / "manifest_1.csv", [(str(src), str(src), 0)])
        cfg = _cfg()
        cfg.config.last_run_kind = "manifest"
        cfg.config.last_run_label = "Manifest: manifest_1.csv (1 entries)"
        cfg.config.last_manifest_path = str(manifest)
        cfg.config.last_mode = 8
        _confirm(monkeypatch, True)

        assert convert_tiff.run_repeat_last(cfg) is False, \
            "a journal without the run's settings must fail closed"
        assert calls == [], "an old journal must be refused before anything runs"


# --- T3/T4: the preset menu ------------------------------------------


class TestPresetsMenu:
    def test_save_run_and_delete_preset(self, tmp_path, monkeypatch, calls):
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        cfg = _isolated_manager(tmp_path, monkeypatch)
        cfg.config.ps_name = "pwsh"  # keep the rebuilt command a plain -File list
        cmd = [cfg.config.ps_name, "-NoProfile", "-File",
               str(convert_tiff.SCRIPT_DIR / "compress_tiff_zip.ps1"),
               "-Mode", "1", "-InputDir", str(tmp_path / "in")]
        cfg.config.last_run_kind = "compress"
        cfg.config.last_run_label = "Compress TIFFs -- mode 1"
        cfg.config.last_run_commands = [cmd]
        cfg.config.last_run_time = "2026-10-10 03:00:00"
        cfg.save_config()

        config_path = cfg.config_path
        saves = []
        real_save = cfg.save_config

        def tracking_save():
            real_save()
            saves.append(json.loads(config_path.read_text(encoding="utf-8")))

        monkeypatch.setattr(cfg, "save_config", tracking_save)
        answers = iter(["S", "nightly", "1", "y", "D", "1", "y", "B"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

        convert_tiff.presets_menu(cfg)

        assert len(calls) == 1, "the preset must run exactly once"
        run_cmd = calls[0]["cmd"]
        assert run_cmd[run_cmd.index("-Mode") + 1] == "1"
        assert len(saves) == 2, "one save for the preset, one for the delete"
        assert "nightly" in saves[0]["presets"]
        assert saves[0]["presets"]["nightly"]["commands"] == [cmd]
        assert saves[0]["presets"]["nightly"]["saved_at"]
        assert saves[-1]["presets"] == {}, "the delete must be persisted"

    def test_preset_is_a_copy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        cfg = _isolated_manager(tmp_path, monkeypatch)
        cfg.config.ps_name = "pwsh"  # keep the rebuilt command a plain -File list
        cmd = [cfg.config.ps_name, "-NoProfile", "-File",
               str(convert_tiff.SCRIPT_DIR / "compress_tiff_zip.ps1"),
               "-Mode", "1", "-InputDir", str(tmp_path / "in")]
        cfg.config.last_run_kind = "compress"
        cfg.config.last_run_label = "Compress TIFFs -- mode 1"
        cfg.config.last_run_commands = [cmd]
        cfg.config.last_run_time = "2026-10-10 03:00:00"
        cfg.save_config()

        answers = iter(["S", "snap", "B"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        convert_tiff.presets_menu(cfg)

        # a later run mutates the live journal in place, then replaces it
        cfg.config.last_run_commands[0].append("-Overwrite")
        cfg.config.last_run_label = "a different run"
        saved = cfg.config.presets["snap"]
        assert "-Overwrite" not in saved["commands"][0], \
            "the preset shares the journal's list instead of a copy"
        assert saved["label"] == "Compress TIFFs -- mode 1"
        assert set(saved) == set(convert_tiff._JOURNAL_FIELDS) | {"saved_at"}


# --- T5: P2 -- Fuji journals redo the AutoFind -----------------------


class TestAutoFindRedone:
    def _fuji_entry(self, root, first_folder):
        cmd = build_copy_exif_command({"workers": 4, "staging": ""},
                                      folders=[first_folder], ps_name="pwsh")
        return _entry("copy_exif", "Fuji: Copy EXIF -- pattern 'S5pro'", [cmd],
                      autofind={"root": str(root), "patterns": ["S5pro"]})

    def test_autofind_is_redone(self, tmp_path, calls):
        root = tmp_path / "root"
        a = root / "S5pro_a"
        a.mkdir(parents=True)
        (a / "foto.tif").write_bytes(b"x")
        cfg = _cfg()
        entry = self._fuji_entry(root, a)

        # a session added after the journal was saved must be included
        b = root / "S5pro_b"
        b.mkdir()
        (b / "foto.tif").write_bytes(b"x")

        assert convert_tiff._run_journal(cfg, entry, "Preset: fuji", unattended=True) == 0
        cmd = calls[-1]["cmd"]
        input_dir = cmd[cmd.index("-InputDir") + 1]
        assert str(a) in input_dir and str(b) in input_dir, \
            f"the AutoFind must see sessions added since: {input_dir}"

        # the root is gone -> refuse
        calls.clear()
        shutil.rmtree(root)
        assert convert_tiff._run_journal(cfg, entry, "Preset: fuji", unattended=True) == 1
        assert calls == []

        # no matching folders -> nothing to do, success, nothing runs
        empty = tmp_path / "empty_root"
        empty.mkdir()
        entry2 = dict(entry)
        entry2["autofind"] = {"root": str(empty), "patterns": ["S5pro"]}
        assert convert_tiff._run_journal(cfg, entry2, "Preset: fuji", unattended=True) == 0
        assert calls == []

    def test_free_compress_preset_has_no_autofind_note(self, tmp_path, monkeypatch,
                                                       capsys, calls):
        """R19-1: menu 1 also journals kind "compress", but with a fixed folder and
        no AutoFind -- it must not print the Fuji journals' "saved by an older
        version" note on every scheduled run."""
        monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
        cfg = _cfg()
        cmd = build_compress_command({"mode": 9, "workers": 1, "staging": "",
                                      "dry_run": False},
                                     folders=[tmp_path], ps_name="pwsh")
        entry = _entry("compress", "Compress TIFFs -- mode 9 -- X", [cmd])

        assert convert_tiff._run_journal(cfg, entry, "Preset: compress",
                                         unattended=True) == 0
        out = capsys.readouterr().out
        assert "older version" not in out, (
            f"a menu-1 compress journal has no AutoFind; the Fuji note is false:\n{out}")
        run_cmd = calls[-1]["cmd"]
        assert run_cmd[run_cmd.index("-InputDir") + 1] == str(tmp_path), \
            "the fixed folder of a menu-1 journal must be replayed unchanged"

        # The same kind with a Fuji label still gets the note when its journal from
        # an older version carries no AutoFind data (folder list stays as saved).
        fuji = _entry("compress", "Fuji: Compress -- pattern 'S5pro' under X", [cmd])
        assert convert_tiff._run_journal(cfg, fuji, "Preset: fuji",
                                         unattended=True) == 0
        out2 = capsys.readouterr().out
        assert "older version" in out2, (
            f"a Fuji journal without autofind data must keep the warning:\n{out2}")


# --- T6/T7: unattended runs ------------------------------------------


class TestUnattendedRuns:
    def test_unattended_refuses_deleting_presets(self, tmp_path, calls):
        cfg = _cfg()
        entry = _compress_entry(tmp_path, mode=8, delete_source=True)

        assert convert_tiff._run_journal(cfg, entry, "Preset: delete", unattended=True) == 1
        assert calls == [], "a deleting preset must be refused without a console"

        assert convert_tiff._run_journal(cfg, entry, "Preset: delete", unattended=True,
                                         dry_run=True) == 0
        assert len(calls) == 1
        cmd, kw = calls[0]["cmd"], calls[0]["kw"]
        assert "-DryRun" in cmd and "-NonInteractive" in cmd
        assert kw.get("stdin") is subprocess.DEVNULL

        # same for a manifest with a mode-8 row
        calls.clear()
        src = tmp_path / "src"
        src.mkdir()
        manifest = _write_manifest(tmp_path / "manifest_8.csv", [(str(src), str(src), 8)])
        m_entry = _entry(
            "manifest", "Manifest: manifest_8.csv (1 entries) [DELETES SOURCES]", None,
            manifest_path=str(manifest),
            manifest_workflow={"origin": "free_compress", "dest": "zip", "workers": 1,
                               "staging": "", "dry_run": False})
        assert convert_tiff._run_journal(cfg, m_entry, "Preset: m8", unattended=True) == 1
        assert calls == []
        assert convert_tiff._run_journal(cfg, m_entry, "Preset: m8", unattended=True,
                                         dry_run=True) == 0
        assert len(calls) == 1
        cmd = calls[0]["cmd"]
        assert "-DryRun" in cmd and "-NonInteractive" in cmd
        assert calls[0]["kw"].get("stdin") is subprocess.DEVNULL

    def test_unattended_never_prompts(self, tmp_path, monkeypatch, calls):
        def boom(*a, **k):
            raise AssertionError("a prompt was attempted on the unattended path")

        monkeypatch.setattr("builtins.input", boom)
        if convert_tiff.RICH_AVAILABLE:
            monkeypatch.setattr(convert_tiff.Confirm, "ask", staticmethod(boom))
            monkeypatch.setattr(convert_tiff.Prompt, "ask", staticmethod(boom))

        cfg = _cfg()
        entry = _compress_entry(tmp_path, mode=1)
        assert convert_tiff._run_journal(cfg, entry, "Preset: ok", unattended=True) == 0
        assert len(calls) == 1

        # overlapping manifest Sources: refused, never "Run anyway?"
        sub = tmp_path / "sub"
        sub.mkdir()
        manifest = _write_manifest(tmp_path / "manifest_overlap.csv",
                                   [(str(tmp_path), str(tmp_path), 3),
                                    (str(sub), str(sub), 3)])
        m_entry = _entry(
            "manifest", "Manifest: overlap", None, manifest_path=str(manifest),
            manifest_workflow={"origin": "free_compress", "dest": "zip", "workers": 1,
                               "staging": "", "dry_run": False})
        assert convert_tiff._run_journal(cfg, m_entry, "Preset: overlap",
                                         unattended=True) == 1
        assert len(calls) == 1, "the refused manifest must not run anything"


# --- T8: malformed presets in a hand-edited config -------------------


class TestSanitisePresets:
    def _write_config(self, tmp_path, presets):
        (tmp_path / ".convert_tiff_config.json").write_text(
            json.dumps({"presets": presets}), encoding="utf-8")

    def test_sanitise_presets(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        # a list instead of a name -> settings mapping
        self._write_config(tmp_path, ["nightly"])
        cfg = convert_tiff.ConfigManager()
        err = capsys.readouterr().err
        assert "presets" in err and "Ignoring it" in err
        assert cfg.config.presets == {}

        # a preset saved as a string + one with no runnable journal + one good
        good = {"kind": "compress", "label": "Compress TIFFs -- mode 1",
                "commands": [["pwsh", "-NoProfile", "-File", "x.ps1"]],
                "time": "t", "manifest_path": None, "manifest_workflow": None,
                "manifest_default_mode": None, "autofind": None}
        self._write_config(tmp_path, {"good": good, "bad": "not a dict",
                                      "empty": {"kind": "compress"}})
        cfg = convert_tiff.ConfigManager()
        err = capsys.readouterr().err
        assert cfg.config.presets == {"good": good}, \
            "valid presets must survive the malformed ones"
        assert "bad" in err and "empty" in err


# --- T9/T10: the real CLI --------------------------------------------


@requires_preset_cli
class TestRunPresetCliEndToEnd:
    def test_run_preset_cli_end_to_end(self, tmp_path):
        in_dir = tmp_path / "in"
        in_dir.mkdir()
        make_tiff(in_dir / "a.tif")
        make_tiff(in_dir / "b.tif")
        _write_preset_config(tmp_path, {"nightly": _compress_entry(in_dir)})

        first = _run_cli(["--run-preset", "nightly"], tmp_path)
        assert first.returncode == 0, first.stdout + first.stderr
        outs = sorted((in_dir / "ZIP").glob("*.tif"))
        assert len(outs) == 2, \
            f"mode 1 must write both outputs: {list((in_dir / 'ZIP').glob('*'))}"
        mtimes = {p.name: p.stat().st_mtime_ns for p in outs}

        # run twice over its own output: a clean no-op, nothing rewritten
        second = _run_cli(["--run-preset", "nightly"], tmp_path)
        assert second.returncode == 0, second.stdout + second.stderr
        assert {p.name: p.stat().st_mtime_ns for p in outs} == mtimes, \
            "the second run must not rewrite the outputs"

        # --dry-run writes nothing at all
        in3 = tmp_path / "in3"
        in3.mkdir()
        make_tiff(in3 / "c.tif")
        _write_preset_config(tmp_path, {"dry": _compress_entry(in3)})
        dry = _run_cli(["--run-preset", "dry", "--dry-run"], tmp_path)
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert not (in3 / "ZIP").exists(), "a dry run must not create outputs"


@requires_preset_cli
class TestCliExitCodes:
    def test_cli_exit_codes(self, tmp_path):
        in_dir = tmp_path / "in"
        in_dir.mkdir()
        make_tiff(in_dir / "a.tif")

        del_dir = tmp_path / "del"
        del_dir.mkdir()
        src = del_dir / "keep.tif"
        src.write_bytes(b"x")
        _write_preset_config(tmp_path, {
            "nightly": _compress_entry(in_dir),
            "danger": _compress_entry(del_dir, mode=8, delete_source=True),
        })

        missing = _run_cli(["--run-preset", "ghost"], tmp_path)
        assert missing.returncode == 2
        out = missing.stdout + missing.stderr
        assert "ghost" in out and "nightly" in out, "the listing must name the available presets"

        refused = _run_cli(["--run-preset", "danger"], tmp_path)
        assert refused.returncode == 1
        assert src.exists(), "a refused preset must not touch the sources"

        stray = _run_cli(["--dry-run"], tmp_path)
        assert stray.returncode == 2, "a stray --dry-run must be an error, never ignored"
        assert "--run-preset" in (stray.stdout + stray.stderr)

        listing = _run_cli(["--list-presets"], tmp_path)
        assert listing.returncode == 0
        assert "danger" in listing.stdout and "nightly" in listing.stdout
