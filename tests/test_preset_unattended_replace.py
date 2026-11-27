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
