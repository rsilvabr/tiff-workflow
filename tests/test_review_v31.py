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
