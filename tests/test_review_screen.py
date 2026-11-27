"""The wizard by task (plan item 13): the command lines the wizard gives when every question is
answered with Enter are a contract. tests/data/review_default_argv.json holds them as v3.2.1
gave them (written ONCE by tests/review_golden.py from v3.2.1's code, never regenerated); every
task (Compress in each mode 0-9, the three Fuji tasks, Thumbnails create and remove) must give
the same lines on both screens, and the same journal."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiff_workflow import wizard as ct  # noqa: E402
import review_golden as rg  # noqa: E402

GOLDEN = json.loads((ROOT / "tests" / "data" / "review_default_argv.json").read_text(encoding="utf-8"))
UIS = ["plain", "rich"]
#: the tasks on the review screen: answered with just `r` (the others: Enter everywhere)
REVIEW_TASKS = {"compress", "copy_exif", "fuji_compress", "both"}


def test_the_golden_was_captured_from_v321_and_covers_every_case():
    assert GOLDEN["captured_from"] == "3.2.1"
    assert sorted(GOLDEN["cases"]) == sorted(rg.case_id(t, m) for t, m in rg.CASES)


@pytest.mark.parametrize("ui", UIS)
@pytest.mark.parametrize("task,mode", rg.CASES, ids=[rg.case_id(t, m) for t, m in rg.CASES])
def test_enter_everywhere_gives_the_command_lines_of_v321(tmp_path, monkeypatch, ui, task, mode):
    if ui == "rich":
        pytest.importorskip("rich")
    rg.make_tree(tmp_path)
    flow = "review" if task in REVIEW_TASKS else "enter"
    got = rg.capture(ct, tmp_path, ui, flow, monkeypatch, task, mode)
    asked = got.pop("_asked")
    assert got == GOLDEN["cases"][rg.case_id(task, mode)]
    if flow == "review":
        # only what cannot have a default, then the screen once (and mode 8's question)
        assert [q for q in asked if q.startswith("Review")] == [asked[-2 if mode == 8 else -1]], asked
        # Compress: the mode, the folder; Fuji: the pattern, the root, AutoFind's yes;
        # Thumbnails: the folder, create or remove
        first = {"compress": 2, "thumbnails": 2, "thumbnails_remove": 2}.get(task, 3)
        assert len(asked) == first + 1 + (mode == 8), asked


# --- the review screen itself ------------------------------------------------------------

class _Seq:
    """Answers in order ("" = Enter); records every question. Runs out -> the test fails."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, question):
        q = str(question)
        self.asked.append(q)
        if not self.answers:
            raise AssertionError(f"asked more than scripted: {q!r}")
        return self.answers.pop(0)


def _compress(tmp_path, monkeypatch, ui, answers, mode=9, cfg=None):
    """Compress on the hostile tree, answered `answers` after the mode and the folder: (the
    command lines run, the questions, the cfg)."""
    from types import SimpleNamespace
    lib = rg.make_tree(tmp_path)
    seq = _Seq([str(mode), str(lib)] + list(answers))
    rg._script(monkeypatch, ct, ui, seq)
    ran = []
    monkeypatch.setattr(ct, "run_subprocess",
                        lambda cmd, timeout=None, stdin=None: ran.append([str(a) for a in cmd]) or 0)
    cfg = cfg or SimpleNamespace(config=ct.ToolConfig(), save_config=lambda: None)
    result = ct.run_free_compress(cfg)
    assert not seq.answers, f"answers left: {seq.answers}"
    return result, ran, seq.asked, cfg, lib


def _arg(cmd, name):
    return cmd[cmd.index(name) + 1]


def _rows(monkeypatch, ui):
    """The Option -> Value of the last review table drawn."""
    drawn = []
    real = ct._table

    def table(columns, rows, **kw):
        if [c.get("name") for c in columns] == ["#", "Option", "Value", "From"]:
            drawn.append({r[1]: (r[2], r[3][0] if isinstance(r[3], tuple) else r[3]) for r in rows})
        return real(columns, rows, **kw)
    monkeypatch.setattr(ct, "_table", table)
    return drawn


def _row_number(label_start, mode=9, task="compress"):
    wf = {"origin": "free_compress" if task == "compress" else task, "mode": mode}
    rows = ct._review_rows(wf)
    return str(next(i for i, r in enumerate(rows, 1) if r.label.startswith(label_start)))


@pytest.mark.parametrize("ui", UIS)
def test_each_editor_valid_invalid_and_clamped(tmp_path, monkeypatch, ui, capsys):
    if ui == "rich":
        pytest.importorskip("rich")
    w, cap, safe = _row_number("Workers"), _row_number("Cap workers"), _row_number("Safe mode")
    excl, incl = _row_number("Exclude folders"), _row_number("Only folders")
    answers = [w, "500",            # workers clamped to 64, said
               cap, "abc",          # not a number: not changed
               cap, "-3",           # clamped to 0, said
               safe, "n",
               excl, "temp 一時;old [x]",
               incl, "TIFF16",
               "r"]
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, ui, answers)
    assert ok is True
    (cmd,) = ran
    assert _arg(cmd, "--workers") == "64" and _arg(cmd, "--cap-workers") == "0"
    assert "--no-safe-mode" in cmd
    assert _arg(cmd, "--exclude-folders") == "temp 一時;old [x]"
    assert _arg(cmd, "--include-folders") == "TIFF16"
    assert cfg.config.last_workers == 64 and cfg.config.last_include_folders == "TIFF16"


@pytest.mark.parametrize("ui", UIS)
def test_the_thumbnail_rows_appear_with_the_thumbnail_and_pass_only_what_was_edited(
        tmp_path, monkeypatch, ui):
    if ui == "rich":
        pytest.importorskip("rich")
    gen = _row_number("Generate embedded thumbnails")
    wf_on = {"origin": "free_compress", "mode": 9, "generate_thumbnail": True}
    size = str(next(i for i, r in enumerate(ct._review_rows(wf_on), 1) if r.key == "thumb_size"))
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, ui,
                                         [gen, "y", size, "abc", size, "512", "r"])
    (cmd,) = ran
    assert "--generate-thumbnail" in cmd and _arg(cmd, "--thumb-size") == "512"
    assert "--thumb-quality" not in cmd and "--thumb-format" not in cmd, \
        "not edited: the backend's own setting, as Configure? No"


@pytest.mark.parametrize("ui", UIS)
def test_d_runs_the_dry_run_and_comes_back_then_r_runs(tmp_path, monkeypatch, ui):
    if ui == "rich":
        pytest.importorskip("rich")
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, ui, ["d", "r"])
    assert ok is True and len(ran) == 2
    assert "--dry-run" in ran[0] and "--dry-run" not in ran[1]
    assert [a for a in ran[0] if a != "--dry-run"] == ran[1], "the dry run is the run plus --dry-run"
    assert len([q for q in asked if q.startswith("Review")]) == 2


@pytest.mark.parametrize("ui", UIS)
def test_b_goes_back_to_the_mode_with_the_answers_kept(tmp_path, monkeypatch, ui):
    if ui == "rich":
        pytest.importorskip("rich")
    w = _row_number("Workers", mode=2)
    out = _row_number("Output folder", mode=2)
    lib = rg.make_tree(tmp_path)
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, ui,
                                         [w, "3", out, str(lib / "flat 一時"), "b", "9", "", "r"],
                                         mode=2)
    (cmd,) = ran
    assert _arg(cmd, "--mode") == "9" and _arg(cmd, "--workers") == "3", "the edit was kept"
    assert "--output-dir" not in cmd and "--duplicate-action" not in cmd, \
        "mode 2's rows went with mode 2"


@pytest.mark.parametrize("ui", UIS)
def test_q_cancels_and_runs_nothing(tmp_path, monkeypatch, ui):
    if ui == "rich":
        pytest.importorskip("rich")
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, ui, ["q"])
    assert ok is False and ran == []


@pytest.mark.parametrize("ui", UIS)
def test_mode8_asks_its_question_at_r_and_no_returns_to_the_screen(tmp_path, monkeypatch, ui):
    if ui == "rich":
        pytest.importorskip("rich")
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, ui, ["d", "r", "n", "q"], mode=8)
    assert ok is False
    (dry,) = ran
    assert "--dry-run" in dry and "--delete-source" in dry, "the dry run shows what r would do"
    assert sum(q.startswith("Mode 8 will DELETE") for q in asked) == 1, "asked at r, not at d"
    ok, ran, asked, cfg, lib = _compress(tmp_path / "2", monkeypatch, ui, ["r", "y"], mode=8)
    (cmd,) = ran
    assert "--delete-source" in cmd and "--dry-run" not in cmd


@pytest.mark.parametrize("ui", UIS)
def test_c_shows_the_command_line_without_running(tmp_path, monkeypatch, ui, capsys):
    if ui == "rich":
        pytest.importorskip("rich")
    shown = []
    monkeypatch.setattr(ct, "_say", lambda text, style="": shown.append(ct._plain(text)))
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, ui, ["c", "q"])
    assert ran == []
    assert any("compress --mode 9 --input-dir" in line and "伊豆沼 [FINAL] 50% x" in line
               for line in shown), shown


def test_s_saves_a_preset_that_runs_like_r(tmp_path, monkeypatch):
    """`s` saves the journal of the current answers without running; --run-preset then runs
    the line `r` runs."""
    cfg = _manager(tmp_path, monkeypatch)
    w = _row_number("Workers")
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, "plain",
                                         [w, "2", "s", "nightly [x]", "r"], cfg=cfg)
    (run_cmd,) = ran
    preset = cfg.config.presets["nightly [x]"]
    assert preset["commands"] == [ct._journal_line(run_cmd)]
    calls = []
    monkeypatch.setattr(ct, "run_subprocess", lambda cmd, timeout=None, stdin=None: calls.append(cmd) or 0)
    assert ct._run_journal(cfg, preset, "Preset: nightly [x]", unattended=True) == 0
    assert [str(a) for a in calls[0]] == run_cmd


def test_s_on_mode8_asks_whether_it_may_run_unattended(tmp_path, monkeypatch):
    cfg = _manager(tmp_path, monkeypatch)
    ok, ran, asked, cfg, lib = _compress(tmp_path, monkeypatch, "plain",
                                         ["s", "m8", "y", "q"], mode=8, cfg=cfg)
    preset = cfg.config.presets["m8"]
    assert preset["unattended_replace"] is True
    assert all("--delete-source" not in line for line in preset["commands"])
    assert ran == []


def _manager(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(ct, "_config_base", lambda: home)
    return ct.ConfigManager()


def test_the_screen_shows_hostile_names_as_they_are_on_rich(tmp_path, monkeypatch):
    """A folder named `伊豆沼 [FINAL] 50% x` in a Rich table: markup must not eat `[FINAL]`."""
    pytest.importorskip("rich")
    import io

    from rich.console import Console
    out = io.StringIO()
    lib = rg.make_tree(tmp_path)
    seq = _Seq(["9", str(lib), _row_number("Exclude folders"), "[bold]x[/bold];a [/b]", "q"])
    rg._script(monkeypatch, ct, "rich", seq)
    monkeypatch.setattr(ct, "console", Console(file=out, force_terminal=False, color_system=None,
                                               width=300))
    from types import SimpleNamespace
    ct.run_free_compress(SimpleNamespace(config=ct.ToolConfig(), save_config=lambda: None))
    text = out.getvalue()
    assert "伊豆沼 [FINAL] 50% x" in text, text
    assert "[bold]x[/bold];a [/b]" in text, text


def test_every_asked_option_is_a_row_or_asked_first():
    """A question never silently dropped: every REGISTRY option a wizard step asks is a
    review row of a task of its backend, or named in _REVIEW_NOT_ROWS with where it is asked."""
    backend_tasks = {"compress": ("compress", "fuji_compress", "both"),
                     "copy_exif": ("copy_exif", "both"),
                     "thumbnails": ("thumbnails", "thumbnails_remove")}
    keys_of = {}
    for row in ct._REVIEW_ROWS:
        for t in row.tasks:
            keys_of.setdefault(t, set()).add(row.key)
    for backend, opts in ct.REGISTRY.items():
        for opt in opts:
            if opt.level != "asked":
                continue
            row_keys = set().union(*(keys_of.get(t, set()) for t in backend_tasks[backend]))
            assert opt.key in row_keys or opt.param in ct._REVIEW_NOT_ROWS.get(backend, {}), \
                f"{backend} {opt.param}: asked by no row and not named in _REVIEW_NOT_ROWS"


# --- the Fuji tasks ----------------------------------------------------------------------

def _fuji(tmp_path, monkeypatch, ui, task, answers, rcs=None):
    from types import SimpleNamespace
    lib = rg.make_tree(tmp_path)
    seq = _Seq(["", str(lib), ""] + list(answers))      # pattern 1 (S5pro), the root, AutoFind yes
    rg._script(monkeypatch, ct, ui, seq)
    ran = []
    codes = iter(rcs or [])
    monkeypatch.setattr(ct, "run_subprocess", lambda cmd, timeout=None, stdin=None:
                        ran.append([str(a) for a in cmd]) or next(codes, 0))
    cfg = SimpleNamespace(config=ct.ToolConfig(), save_config=lambda: None)
    origin = {"fuji_compress": "compress"}.get(task, task)
    result = ct._run_exif_or_compress(cfg, origin)
    assert not seq.answers, f"answers left: {seq.answers}"
    return result, ran, seq.asked, cfg


@pytest.mark.parametrize("ui", UIS)
def test_both_shows_its_two_command_lines(tmp_path, monkeypatch, ui):
    if ui == "rich":
        pytest.importorskip("rich")
    shown = []
    monkeypatch.setattr(ct, "_say", lambda text, style="": shown.append(ct._plain(text)))
    ok, ran, asked, cfg = _fuji(tmp_path, monkeypatch, ui, "both", ["c", "q"])
    assert ran == [] and ok is False
    lines = [s for s in shown if " copy-exif --input-dir " in s or " compress --mode 9 " in s]
    assert len(lines) == 2 and "copy-exif" in lines[0] and "compress" in lines[1], shown


@pytest.mark.parametrize("ui", UIS)
def test_both_dry_then_run_keeps_the_gate(tmp_path, monkeypatch, ui):
    if ui == "rich":
        pytest.importorskip("rich")
    ok, ran, asked, cfg = _fuji(tmp_path, monkeypatch, ui, "both", ["d", "r"], rcs=[0, 0, 1])
    assert ok is False and len(ran) == 3, "compress ran after the real Copy EXIF failed"
    assert "--dry-run" in ran[0] and "--dry-run" in ran[1] and "--dry-run" not in ran[2]


@pytest.mark.parametrize("ui", UIS)
def test_fuji_b_goes_back_to_the_pattern_with_the_answers_kept(tmp_path, monkeypatch, ui):
    if ui == "rich":
        pytest.importorskip("rich")
    w = str(next(i for i, r in enumerate(ct._review_rows({"origin": "copy_exif"}), 1)
                 if r.key == "workers"))
    lib = rg.make_tree(tmp_path)
    ok, ran, asked, cfg = _fuji(tmp_path, monkeypatch, ui, "copy_exif",
                                [w, "5", "b", "", str(lib), "", "r"])
    (cmd,) = ran
    assert _arg(cmd, "--workers") == "5"
    assert sum(q.startswith("Choice") for q in asked) == 2
