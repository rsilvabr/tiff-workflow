"""v3.3 (owner's decision 27/11/2026, plan item 11): a preset may run compress mode 8 unattended
(--run-preset) when it was saved with an explicit yes to that question (`"unattended_replace":
true`, a bool). --delete-source stays refused unattended whatever the preset says; a hand-edited
"yes" is no yes; Repeat last never carries it. Hostile folders, a real run twice."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import tifffile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiff_workflow import wizard as ct  # noqa: E402
from test_presets import _cfg, _compress_entry, _isolated_manager, calls  # noqa: E402,F401

TOOLS = shutil.which("magick") is not None and shutil.which("exiftool") is not None
HOSTILE = "伊豆沼 [FINAL] 50% x"
KEY = "unattended_replace"      # the key in the config file (a data format: written out)


def _mode8(folder, delete_source=False, **extra):
    entry = _compress_entry(folder, mode=8, delete_source=delete_source)
    entry.update(extra)
    return entry


@pytest.mark.parametrize("value,allowed", [(True, True), (None, False), ("yes", False),
                                           (1, False), (False, False)])
def test_mode8_unattended_needs_the_saved_yes(tmp_path, calls, value, allowed):  # noqa: F811
    folder = tmp_path / HOSTILE
    entry = _mode8(folder)
    if value is not None:
        entry[KEY] = value
    rc = ct._run_journal(_cfg(), entry, "Preset: m8", unattended=True)
    assert rc == (0 if allowed else 1)
    assert len(calls) == (1 if allowed else 0)
    if allowed:
        assert "--delete-source" not in calls[0]["cmd"]
        assert calls[0]["kw"].get("stdin") is subprocess.DEVNULL


def test_delete_source_is_refused_whatever_the_preset_says(tmp_path, calls):  # noqa: F811
    entry = _mode8(tmp_path / HOSTILE, delete_source=True, **{KEY: True})
    assert ct._run_journal(_cfg(), entry, "Preset: m8", unattended=True) == 1
    assert calls == []


def test_the_refusal_names_the_way_out(tmp_path, calls, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    assert ct._run_journal(_cfg(), _mode8(tmp_path / HOSTILE), "Preset: m8", unattended=True) == 1
    assert "main menu 5, [U]" in capsys.readouterr().out


def _save_last_mode8(cfg, folder, delete_source):
    entry = _mode8(folder, delete_source=delete_source)
    c = cfg.config
    c.last_run_kind, c.last_run_label = entry["kind"], entry["label"]
    c.last_run_commands, c.last_run_time = entry["commands"], entry["time"]
    cfg.save_config()


@pytest.mark.parametrize("answer", ["y", "n"])
@pytest.mark.parametrize("delete_source", [False, True])
def test_saving_a_mode8_preset_asks_once_default_no(tmp_path, monkeypatch, answer, delete_source):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = _isolated_manager(tmp_path, monkeypatch)
    _save_last_mode8(cfg, tmp_path / HOSTILE, delete_source)
    asked = []
    answers = iter(["S", "TIFF SEM COMPRESSAO [x]", answer, "B"])

    def inp(prompt=""):
        asked.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", inp)
    ct.presets_menu(cfg)
    preset = cfg.config.presets["TIFF SEM COMPRESSAO [x]"]
    question = [q for q in asked if q.startswith("Allow this preset to run unattended")]
    assert len(question) == 1 and question[0].endswith("[y/N]: ")
    assert ("--delete-source is taken out" in question[0]) == delete_source
    if answer == "y":
        assert preset[KEY] is True
        assert all("--delete-source" not in line for line in preset["commands"])
    else:
        assert KEY not in preset
        assert any("--delete-source" in line for line in preset["commands"]) == delete_source


def test_a_preset_that_is_not_mode8_is_not_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = _isolated_manager(tmp_path, monkeypatch)
    entry = _compress_entry(tmp_path / HOSTILE, mode=9)
    c = cfg.config
    c.last_run_kind, c.last_run_label = entry["kind"], entry["label"]
    c.last_run_commands, c.last_run_time = entry["commands"], entry["time"]
    answers = iter(["S", "p9", "B"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    ct.presets_menu(cfg)
    assert KEY not in cfg.config.presets["p9"]


def test_u_gives_an_existing_preset_the_answer_and_takes_it_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = _isolated_manager(tmp_path, monkeypatch)
    cfg.config.presets = {"TIFF16 RECOMPRIMIR": _mode8(tmp_path / HOSTILE)}
    answers = iter(["U", "1", "y", "U", "1", "n", "B"])
    seen = []

    def inp(prompt=""):
        a = next(answers)
        seen.append(a)
        if len(seen) == 4:      # after the first U: allowed
            assert cfg.config.presets["TIFF16 RECOMPRIMIR"][KEY] is True
        return a
    monkeypatch.setattr("builtins.input", inp)
    ct.presets_menu(cfg)
    assert KEY not in cfg.config.presets["TIFF16 RECOMPRIMIR"]


def test_u_takes_delete_source_out_and_a_no_puts_it_back_as_saved(tmp_path, monkeypatch):
    """Review of v3.3.0 (S4): after the yes the label kept `[DELETES SOURCES]` although nothing
    deleted any more; and a No left the preset a mode 8 without --delete-source under that
    label. The label follows the lines; a No restores the lines exactly as saved."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = _isolated_manager(tmp_path, monkeypatch)
    saved = _mode8(tmp_path / HOSTILE, delete_source=True)
    assert saved["label"].endswith(" [DELETES SOURCES]")
    cfg.config.presets = {"m8 [x]": json.loads(json.dumps(saved))}
    answers = iter(["U", "1", "y", "U", "1", "n", "B"])
    seen = []

    def inp(prompt=""):
        a = next(answers)
        seen.append(a)
        if len(seen) == 4:      # after the yes
            p = cfg.config.presets["m8 [x]"]
            assert p[KEY] is True
            assert all("--delete-source" not in line for line in p["commands"])
            assert p["label"].endswith(" [REPLACES IN PLACE]") and "DELETES" not in p["label"]
        return a
    monkeypatch.setattr("builtins.input", inp)
    ct.presets_menu(cfg)
    back = cfg.config.presets["m8 [x]"]
    assert KEY not in back
    assert back["commands"] == saved["commands"], "the lines as they were saved"
    assert back["label"] == saved["label"]
    assert ct._DELETE_SOURCE_LINES_KEY not in back
    # and it survives the config file
    again = _isolated_manager(tmp_path, monkeypatch).config.presets["m8 [x]"]
    assert again["commands"] == saved["commands"]


def test_a_yes_then_another_yes_keeps_the_saved_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    saved = _mode8(tmp_path / HOSTILE, delete_source=True)
    answers = iter(["y", "y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    once = ct._ask_unattended_replace(saved)
    twice = ct._ask_unattended_replace(once)
    assert twice[KEY] is True and twice[ct._DELETE_SOURCE_LINES_KEY] == saved["commands"]
    assert ct._ask_unattended_replace(twice)["commands"] == saved["commands"]


def test_a_mode8_preset_without_delete_source_is_labelled_as_it_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    entry = _mode8(tmp_path / HOSTILE)
    entry["label"] += " [DELETES SOURCES]"      # how the menu labelled every mode-8 run
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    out = ct._ask_unattended_replace(entry)
    assert out["label"].endswith("x [REPLACES IN PLACE]") and out["commands"] == entry["commands"]
    assert ct._DELETE_SOURCE_LINES_KEY not in out


def test_u_on_a_manifest_preset_says_it_cannot_be_allowed(tmp_path, monkeypatch, capsys):
    """Review of v3.3.0 (N4): it said "does not run mode 8: it needs no permission"."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = _isolated_manager(tmp_path, monkeypatch)
    cfg.config.presets = {"sync": {"kind": "manifest", "label": "Manifest: m.csv (1 entries) "
                                   "[DELETES SOURCES]", "commands": None,
                                   "manifest_path": str(tmp_path / "m.csv")}}
    answers = iter(["U", "1", "B"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    ct.presets_menu(cfg)
    out = capsys.readouterr().out
    assert "is a manifest: it cannot be allowed" in out and "needs no permission" not in out
    assert KEY not in cfg.config.presets["sync"]


def test_repeat_last_never_carries_it(tmp_path, monkeypatch):
    cfg = _isolated_manager(tmp_path, monkeypatch)
    _save_last_mode8(cfg, tmp_path / HOSTILE, False)
    assert KEY not in ct._journal_snapshot(cfg)


def test_list_presets_marks_it(tmp_path, monkeypatch, capsys):
    cfg = _isolated_manager(tmp_path, monkeypatch)
    cfg.config.presets = {"a": _mode8(tmp_path / HOSTILE, **{KEY: True}),
                          "b": _mode8(tmp_path / HOSTILE)}
    cfg.save_config()
    monkeypatch.setattr(sys, "argv", ["convert_tiff.py", "--list-presets"])
    with pytest.raises(SystemExit):
        ct.main()
    out = capsys.readouterr().out
    assert out.count("[runs unattended: replaces in place]") == 1
    assert out.index("[runs unattended") < out.index("\nb\n")


@pytest.mark.skipif(not TOOLS, reason="ImageMagick and exiftool required")
def test_a_real_run_preset_twice(tmp_path):
    """--run-preset on a hostile folder: mode 8 replaces each TIFF by a pixel-identical ZIP; the
    second run over its own output changes nothing."""
    folder = tmp_path / HOSTILE
    folder.mkdir()
    src = folder / "0014_14 (2) 50%.tif"
    tmp = folder / "_f.tif"
    data = np.random.default_rng(3).integers(0, 255, (40, 56, 3), dtype=np.uint8)
    tifffile.imwrite(str(tmp), data, photometric="rgb")
    tmp.rename(src)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".convert_tiff_config.json").write_text(json.dumps(
        {"presets": {"m8": _mode8(folder, **{KEY: True})}}), encoding="utf-8")
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), TIFFWORKFLOW_SETTINGS="",
               TIFFWORKFLOW_NO_UPDATE_CHECK="1")

    def run():
        r = subprocess.run([sys.executable, str(ROOT / "convert_tiff.py"), "--run-preset", "m8"],
                           cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
        return r.returncode, r.stdout + r.stderr
    rc, out = run()
    assert rc == 0, out[-3000:]
    assert np.array_equal(tifffile.imread(str(src)), data)
    first = hashlib.sha256(src.read_bytes()).hexdigest()
    rc, out = run()
    assert rc == 0, out[-3000:]
    assert hashlib.sha256(src.read_bytes()).hexdigest() == first
    assert sorted(p.name for p in folder.iterdir()) == [src.name]
