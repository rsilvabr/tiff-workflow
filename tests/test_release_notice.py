"""The menu's new-release notice (convert_tiff._update_check_start and friends).

NO network: every test monkeypatches convert_tiff._latest_release_tag. The autouse fixture in
conftest.py sets TIFFWORKFLOW_NO_UPDATE_CHECK=1; the tests that exercise the check delete it.
Model: jxl-photo's _update_notice family (v2.25.1), without the jxl-thumb part.
"""

import json
import sys
import threading
from pathlib import Path

import pytest

from tiff_workflow import wizard as ct

MINE = ct.TOOLKIT_VERSION       # e.g. "2.7.4"
TODAY = __import__("datetime").date.today().isoformat()


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    """A real ConfigManager whose JSON lives under tmp_path, the check switched ON."""
    monkeypatch.setattr(ct, "_config_base", lambda: tmp_path)
    monkeypatch.delenv(ct._UPDATE_ENV, raising=False)
    return ct.ConfigManager()


def _bump(minor=0, patch=0, major=0):
    a, b, c = (int(x) for x in MINE.split("."))
    return f"v{a + major}.{b + minor}.{c + patch}"


def _answering(monkeypatch, tag, calls=None):
    def fake(repo, timeout=3):
        if calls is not None:
            calls.append(repo)
        return tag
    monkeypatch.setattr(ct, "_latest_release_tag", fake)


# --- version keys ------------------------------------------------------------------------

@pytest.mark.parametrize("tag,expected", [
    ("v2.7.4", (2, 7, 4, 1)),
    ("2.7.4", (2, 7, 4, 1)),
    ("v2.7.4-exe3", (2, 7, 4, 1)),            # the exe's package revision is ignored
    ("v2.8.0_beta1", (2, 8, 0, 0)),
    ("nonsense", None), ("", None), (None, None), ("v2.7", None),
])
def test_version_key(tag, expected):
    assert ct._version_key(tag) == expected


def test_beta_sorts_before_its_release_and_exe_revision_is_not_news():
    assert ct._version_key("v2.8.0_beta1") < ct._version_key("v2.8.0")
    assert ct._version_key("v2.7.4-exe2") == ct._version_key("v2.7.4")


# --- newer / same / older / beta / exe tags -----------------------------------------------

@pytest.mark.parametrize("tag,expect_notice", [
    (_bump(patch=1), True),
    (_bump(minor=1), True),
    (_bump(major=1), True),
    ("v" + MINE, False),                      # same
    ("v" + MINE + "-exe1", False),            # the exe's revision of this very release
    ("v1.0.0", False),                        # older
    (_bump(minor=1) + "_beta1", True),        # a beta of a LATER release is newer than this one
    ("v" + MINE + "_beta1", False),           # a beta sorts before its own release
])
def test_notice_for_a_tag(mgr, monkeypatch, tag, expect_notice):
    _answering(monkeypatch, tag)
    mgr.save_update_answer(TODAY, tag)
    notice = ct._kept_update_notice(mgr)
    assert (notice is not None) == expect_notice, notice
    if notice:
        line, url = notice
        assert line.startswith(f"A new release is out: {tag} (this copy is {MINE}) -- [U] in the menu")
        assert "\u2014" not in line and line.isascii(), "ASCII only: the console may be cp1252"
        assert url == "https://github.com/rsilvabr/tiff-workflow/releases/latest"


def test_the_frozen_build_follows_the_exe_repository(mgr, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert ct._update_repo() == "tiff-workflow-exe"
    mgr.save_update_answer(TODAY, _bump(patch=1))
    assert ct._kept_update_notice(mgr)[1].endswith("/tiff-workflow-exe/releases/latest")
    monkeypatch.delattr(sys, "frozen")
    assert ct._update_repo() == "tiff-workflow"


# --- when it asks ---------------------------------------------------------------------------

def test_no_answer_leaves_the_day_unmarked(mgr, monkeypatch):
    _answering(monkeypatch, None)
    poll = ct._update_check_start(mgr)
    assert poll(2.0) is None
    assert mgr.config.update_checked_on == "", "a day without an answer must not count"
    assert not mgr.config_path.exists() or "update_checked_on" not in json.loads(
        mgr.config_path.read_text())


def test_one_answer_per_day(mgr, monkeypatch):
    calls = []
    _answering(monkeypatch, _bump(patch=1), calls)
    poll = ct._update_check_start(mgr)
    notice = poll(2.0)
    assert notice and mgr.config.update_checked_on == TODAY
    assert len(calls) == 1
    # a second start the same day: no new request, the kept answer (with its date)
    poll2 = ct._update_check_start(mgr)
    notice2 = poll2(0.0)
    assert len(calls) == 1, "asked GitHub twice in one day"
    assert notice2 and f"Checked by an earlier run on {TODAY}" in notice2[0]


def test_an_old_answer_is_asked_again(mgr, monkeypatch):
    calls = []
    _answering(monkeypatch, _bump(patch=1), calls)
    mgr.save_update_answer("2000-01-01", "v0.0.1")
    ct._update_check_start(mgr)(2.0)
    assert len(calls) == 1 and mgr.config.update_checked_on == TODAY


def test_the_background_thread_keeps_a_late_answer(mgr, monkeypatch):
    """The menu waits at most `wait`; the thread keeps the answer itself, so the next run
    (or the next menu draw) shows it."""
    gate = threading.Event()

    def slow(repo, timeout=3):
        gate.wait(5)
        return _bump(patch=1)
    monkeypatch.setattr(ct, "_latest_release_tag", slow)
    poll = ct._update_check_start(mgr)
    assert poll(0.05) is None                 # nothing yet, and it did not block
    gate.set()
    notice = poll(2.0)
    assert notice is not None
    assert json.loads(mgr.config_path.read_text())["update_latest_seen"] == _bump(patch=1)


def test_save_update_answer_keeps_the_other_config_keys(mgr):
    mgr.config_path.write_text(json.dumps({
        "last_input_dir": "G:\\2019\\x", "presets": {"p": {"label": "l", "commands": [["a"]]}},
        "default_workers": 3}), encoding="utf-8")
    mgr.save_update_answer(TODAY, "v9.9.9")
    data = json.loads(mgr.config_path.read_text(encoding="utf-8"))
    assert data["last_input_dir"] == "G:\\2019\\x" and data["default_workers"] == 3
    assert data["presets"] == {"p": {"label": "l", "commands": [["a"]]}}
    assert data["update_checked_on"] == TODAY and data["update_latest_seen"] == "v9.9.9"
    assert not list(mgr.config_path.parent.glob(".convert_tiff_config.*.tmp")), "temp file left"


def test_save_update_answer_leaves_an_unparseable_file_alone(mgr):
    mgr.config_path.write_text("{ not json", encoding="utf-8")
    mgr.save_update_answer(TODAY, "v9.9.9")
    assert mgr.config_path.read_text(encoding="utf-8") == "{ not json"


# --- off switches ----------------------------------------------------------------------------

def test_env_switches_it_off(mgr, monkeypatch):
    calls = []
    _answering(monkeypatch, _bump(patch=1), calls)
    monkeypatch.setenv(ct._UPDATE_ENV, "1")
    assert ct._update_check_start(mgr)(1.0) is None
    assert calls == []
    line, notice = ct._update_check_now(mgr)
    assert notice is None and "off" in line and calls == []


def test_config_switches_it_off(mgr, monkeypatch):
    calls = []
    _answering(monkeypatch, _bump(patch=1), calls)
    mgr.config.check_for_updates = False
    assert ct._update_check_start(mgr)(1.0) is None
    assert calls == []
    assert ct._update_check_now(mgr)[1] is None and calls == []


def test_check_now_asks_even_after_today_was_answered(mgr, monkeypatch):
    calls = []
    _answering(monkeypatch, _bump(patch=1), calls)
    mgr.save_update_answer(TODAY, "v0.0.1")
    line, notice = ct._update_check_now(mgr)
    assert calls and notice is not None and "A new release is out" in line
    _answering(monkeypatch, "v" + MINE)
    line, notice = ct._update_check_now(mgr)
    assert notice is None and "up to date" in line
    _answering(monkeypatch, None)
    line, notice = ct._update_check_now(mgr)
    assert notice is None and "did not answer" in line


# --- the entry points that must never ask ---------------------------------------------------

def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["convert_tiff.py", *argv])
    with pytest.raises(SystemExit) as exc:
        ct.main()
    return exc.value.code


def test_run_preset_list_presets_and_version_never_ask(mgr, monkeypatch, capsys):
    calls = []
    monkeypatch.delenv(ct._UPDATE_ENV, raising=False)
    _answering(monkeypatch, _bump(patch=1), calls)
    monkeypatch.setattr(ct, "_run_journal", lambda *a, **k: 0)
    mgr.config.presets = {"p": {"label": "l", "commands": [["x"]], "kind": "compress"}}
    mgr.save_config()
    assert _run_main(monkeypatch, ["--list-presets"]) == 0
    assert _run_main(monkeypatch, ["--run-preset", "p"]) == 0
    assert _run_main(monkeypatch, ["--version"]) == 0
    assert calls == [], "a non-interactive entry point asked GitHub"


def test_the_menu_run_does_ask(mgr, monkeypatch, capsys):
    """The positive control of the test above: the interactive menu is the one that asks."""
    calls = []
    monkeypatch.delenv(ct._UPDATE_ENV, raising=False)
    _answering(monkeypatch, _bump(patch=1), calls)
    monkeypatch.setattr(ct, "show_environment_panel", lambda cfg: None)
    shown = []

    def menu(cfg):
        shown.append(getattr(cfg, "update_available", None))
        return "0"
    monkeypatch.setattr(ct, "show_main_menu", menu)
    monkeypatch.setattr(sys, "argv", ["convert_tiff.py"])
    ct.main()
    assert calls == ["tiff-workflow"]
    assert shown and shown[0] and shown[0][0] == _bump(patch=1)
    assert "A new release is out" in capsys.readouterr().out
