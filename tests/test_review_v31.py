"""What the independent reviews of v3.1 (phases 5-8 and 9-10, 13/11/2026) found untested or
wrong in the wizard, pinned here.

V4 of phases 5-8: three questions that delete or replace photos default to "no" (T2), and
nothing pinned it -- a mutation to "yes" passed every test. Here each one is answered with
Enter on the plain screen (input() returns ""), through the real yes/no reader, and nothing
may run or be deleted. And a padded conversion whose page markers could not be restored counts
as a FAILED conversion (the Diagnose preset exits 1 for it, round 28).
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tiff_workflow import wizard as ct  # noqa: E402

HOSTILE = "伊豆沼 [FINAL] 50% x"


def _cfg():
    return SimpleNamespace(config=ct.ToolConfig(), save_config=lambda: None)


@pytest.fixture
def enter(monkeypatch):
    """The plain screen, and every question answered with Enter (its default). The prompts
    asked are recorded."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(str(prompt)) or "")
    return asked


def test_enter_at_the_mode8_delete_question_runs_nothing(tmp_path, monkeypatch, enter):
    """V4 (m58_15): "Mode 8 will DELETE source TIFFs ... Are you sure?" -- with a default of
    yes, two Enters (this one and "Proceed?") ran mode 8, which replaces the originals."""
    folder = tmp_path / HOSTILE
    folder.mkdir()
    ran = []
    monkeypatch.setattr(ct, "step_mode", lambda cfg: 8)
    monkeypatch.setattr(ct, "step_folder", lambda cfg, *a, **k: folder)
    monkeypatch.setattr(ct, "step_basic_params", lambda cfg, wf: None)
    monkeypatch.setattr(ct, "_ask_thumbnails", lambda wf: None)
    monkeypatch.setattr(ct, "step_confirm", lambda wf, cfg: ran.append("confirm") or True)
    monkeypatch.setattr(ct, "run_subprocess", lambda *a, **k: ran.append(a) or 0)
    assert ct.run_free_compress(_cfg()) is False
    assert ran == [], "Enter at the mode 8 question went on"
    assert any("Mode 8 will DELETE source TIFFs" in p for p in enter), enter


def test_enter_at_run_it_now_runs_no_preset(monkeypatch, enter):
    """V4 (m58_17): "Run it now?" of a Repeat or a preset from the menu -- also of one that
    deletes. Enter is no."""
    ran = []
    monkeypatch.setattr(ct, "run_subprocess", lambda *a, **k: ran.append(a) or 0)
    entry = {"kind": "compress", "label": "Compress: mode 8", "time": "2026-11-13 08:00",
             "commands": [["compress", "--mode", "8", "--input-dir", f"D:\\{HOSTILE}"]]}
    assert ct._run_journal(_cfg(), entry, "Preset: nightly") == 1
    assert ran == [], "Enter at 'Run it now?' ran the preset"
    assert any("Run it now?" in p for p in enter), enter


def test_enter_at_delete_old_tiffs_deletes_nothing(monkeypatch, enter):
    """V4 (m58_14): "Delete ALL OLD_TIFFs content? THIS CANNOT BE UNDONE." -- Enter is no, and
    the time is never asked."""
    assert ct._purge_confirm() is False
    assert len(enter) == 1 and "Delete ALL OLD_TIFFs content?" in enter[0], enter


def test_a_page_marker_restore_failure_is_a_failed_conversion(tmp_path, monkeypatch, capsys):
    """V4 (m58_07): a conversion whose page markers could not be restored replaced nothing and
    FAILED -- not a refusal. Left out of _PADDED_FAILED_STATUSES, the Diagnose preset went back
    to exit code 0 with a failed conversion, the defect round 28 fixed."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    monkeypatch.setattr(ct, "_memory_budget", lambda *a, **k: (None, {}))
    src = tmp_path / HOSTILE / "p 1.tif"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"ORIGINAL" * 250)

    def fake_single(tiff_path, staging):
        tmp8 = staging / f"tmp8_{tiff_path.stem}.tif"
        tmp8.write_bytes(b"z" * 100)
        return ct.PaddedResult(tiff_path.name, tiff_path.parent, "subfile_restore_failed",
                               2000, 100, 95.0, True, None, tmp8)
    monkeypatch.setattr(ct, "_process_single_padded", fake_single)

    done = ct._compress_padded_files([src], tmp_path / "staging", workers=1, cfg=_cfg())

    assert done == {"converted": 0, "failed": [(src, "subfile_restore_failed")]}, done
    assert src.read_bytes() == b"ORIGINAL" * 250
    assert "could not restore per-page SubfileType markers -- original preserved" in capsys.readouterr().out


# --- V3 of phases 5-8: the config file is read as UTF-8 ------------------------------------

PRESETS = {"presets": {"夏の伊豆沼": {"kind": "compress", "label": "Compress: 伊豆沼 [FINAL]",
                                     "commands": [["compress", "--mode", "9", "--input-dir", "G:/2019/伊豆沼 [FINAL]"]],
                                     "time": "2026-11-13 08:00"}},
           "last_input_dir": "G:/2019/伊豆沼 [FINAL] 50%"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home 伊豆沼 [x] 50%"
    h.mkdir()
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_a_hand_edited_utf8_config_with_japanese_is_read(home, encoding, capsys):
    """V3: the config was read in the ANSI code page, so a file edited by hand in UTF-8 (what
    every editor saves today) with a Japanese name was "unreadable"; the wizard went on with an
    empty config and its first save wrote it over: every preset gone."""
    import json
    (home / ".convert_tiff_config.json").write_bytes(
        json.dumps(PRESETS, ensure_ascii=False, indent=2).encode(encoding))
    cfg = ct.ConfigManager()
    assert list(cfg.config.presets) == ["夏の伊豆沼"]
    assert cfg.config.last_input_dir == "G:/2019/伊豆沼 [FINAL] 50%"
    assert "unreadable" not in capsys.readouterr().err
    cfg.save_config()
    assert list(ct.ConfigManager().config.presets) == ["夏の伊豆沼"]


def test_a_config_saved_in_the_ansi_code_page_still_reads(home):
    import json
    import locale
    text = json.dumps({"last_input_dir": "F:/2025/São Paulo"}, ensure_ascii=False)
    (home / ".convert_tiff_config.json").write_bytes(text.encode(locale.getpreferredencoding(False)))
    assert ct.ConfigManager().config.last_input_dir == "F:/2025/São Paulo"


def test_an_unreadable_config_is_kept_before_the_first_save(home, capsys):
    """An unreadable config (here a truncated one) is still not repaired -- but the first save no
    longer writes the defaults over it without a copy: it may hold every preset."""
    raw = b'{"presets": {"nightly": {"kind": "compress", "label": "trunc'
    (home / ".convert_tiff_config.json").write_bytes(raw)
    cfg = ct.ConfigManager()
    assert "ignoring unreadable config file" in capsys.readouterr().err
    cfg.save_config()
    assert (home / ".convert_tiff_config.json.unreadable").read_bytes() == raw
    assert "was kept as" in capsys.readouterr().err
    cfg.save_config()                                     # once per run
    assert not list(home.glob(".convert_tiff_config.json.unreadable.*"))


# --- V1 of phases 5-8: a manifest journal of an older version is refused (D1) --------------

def _manifest_entry(tmp_path, advanced):
    src = tmp_path / HOSTILE / "scans 01"
    src.mkdir(parents=True)
    csv = tmp_path / HOSTILE / "manifest.csv"
    csv.write_text(f"Source,Destination,Mode\n{src},{src},9\n", encoding="utf-8")
    workflow = {"workers": 4, "safe_mode": True, "skip_lzw": False}
    if advanced is not None:
        workflow["advanced"] = advanced
    return {"kind": "manifest", "label": "Manifest: nightly", "time": "2026-11-01 03:00",
            "commands": None, "manifest_path": str(csv), "manifest_workflow": workflow,
            "manifest_default_mode": None, "autofind": None}


@pytest.mark.parametrize("advanced, runs", [
    ({"compress": {"MagickTimeout": 600, "WorkerMemoryFraction": 0.5}}, False),    # v3.0.0
    ({"compress": {"magick_timeout": 600, "worker_memory_fraction": 0.5}}, True),  # v3.1
    ({"compress": {"magick_timeout": 600}, "copy_exif": {"SrgbProfile": "x.icc"}}, False),
    (None, True),
], ids=["v300-names", "v31-names", "one-old-name", "no-advanced"])
def test_a_manifest_journal_with_names_of_an_older_version_is_refused(tmp_path, monkeypatch,
                                                                       capsys, advanced, runs):
    """V1: a journal of a manifest run keeps the Advanced answers by name. One saved by v3.0.0
    (MagickTimeout, WorkerMemoryFraction, SrgbProfile) RAN -- also unattended -- with those
    answers dropped without a word: the backend used its own timeout, memory share and sRGB
    profile. Refused now, as every other journal of an older version (D1)."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    ran = []
    monkeypatch.setattr(ct, "execute_manifest_workflow",
                        lambda cfg, entries, wf, **k: ran.append(wf) or True)
    rc = ct._run_journal(_cfg(), _manifest_entry(tmp_path, advanced), "Preset: nightly",
                         unattended=True)
    out = capsys.readouterr().out
    if runs:
        assert rc == 0 and len(ran) == 1, out
    else:
        assert rc == 1 and ran == [], out
        assert "cannot be replayed" in out and "save it again" in out, out


# --- V2 of phases 5-8: the plain screen asks again ----------------------------------------

@pytest.mark.parametrize("answers, default, want, asked", [
    (["nope", "n"], True, False, 2),
    (["não!", "0", "sim"], True, True, 3),
    (["cancel", ""], True, True, 2),                  # Enter after it: the default
    (["x", "n"], False, False, 2),
])
def test_the_plain_screen_asks_again_after_an_unknown_answer(monkeypatch, capsys, answers, default,
                                                             want, asked):
    """V2: an answer that is neither yes nor no took the question's default on the plain screen,
    so `nope`, `cancel` or `não!` at "Move all files back to parent folder? [Y/n]" moved the
    files. It is asked again now, as on the Rich screen (T2)."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    it = iter(answers)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or next(it))
    assert ct._ask_yes_no("Move all files back to parent folder?", default) is want
    assert len(prompts) == asked, prompts
    assert capsys.readouterr().out.count("Please enter Y or N") == asked - 1
