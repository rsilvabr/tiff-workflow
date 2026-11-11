"""Workflow 8 of the wizard, Generate Thumbnails (run_generate_thumbnails), on both screens.

What the wizard is for here: the questions in order, what each answer becomes on the command
line of generate_thumbnails.py, and what is journalled for Repeat last and the presets.

The answers go in through builtins.input -- where the plain-text prompts AND Rich's
Prompt.ask / Confirm.ask read -- so Rich's own handling of an answer (its choices, its
yes/no) is part of what runs. The test fails when the screen asks more than scripted, and
when an answer is left over.
"""

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import convert_tiff as ct  # noqa: E402

HOSTILE = "写真 [FINAL] 50% x"
SCRIPT = str(ct.SCRIPT_DIR / "generate_thumbnails.py")


def default(name):
    """A default of generate_thumbnails.py, read from its own PARAMS table."""
    return str(ct._backend_default("generate_thumbnails.py", name))


class Wizard:
    def __init__(self, screen):
        self.screen = screen
        self.answers = []
        self.asked = []
        self.ran = []
        self.rc = 0
        self.out = io.StringIO()
        self.capsys = None           # the plain screen prints on stdout
        self.cfg = None

    def input(self, prompt=""):
        self.asked.append(str(prompt))
        self.out.write(str(prompt))      # plain: the question is input()'s; Rich printed its own
        if not self.answers:
            raise AssertionError(f"the screen asked more than scripted: {prompt!r} after {self.asked!r}")
        return self.answers.pop(0)

    def run(self, *answers):
        self.answers = [str(a) for a in answers]
        result = ct.run_generate_thumbnails(self.cfg)
        assert self.answers == [], f"answers left over: {self.answers!r}"
        return result

    @property
    def text(self):
        if self.capsys is not None:
            self.out.write(self.capsys.readouterr().out)
        return self.out.getvalue()

    def argv(self):
        assert len(self.ran) == 1, self.ran
        assert self.ran[0][:2] == [sys.executable, SCRIPT]
        return self.ran[0][2:]


@pytest.fixture(params=["plain", "rich"])
def wiz(request, monkeypatch, tmp_path, capsys):
    w = Wizard(request.param)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("builtins.input", w.input)
    monkeypatch.setattr(ct, "run_subprocess", lambda cmd, **k: w.ran.append([str(c) for c in cmd]) or w.rc)
    if request.param == "plain":
        monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
        monkeypatch.setattr(ct, "console", None)
        w.capsys = capsys
    else:
        if not ct.RICH_AVAILABLE:
            pytest.skip("rich is not installed")
        from rich.console import Console
        screen = Console(file=w.out, force_terminal=False, color_system=None, width=300,
                         legacy_windows=False)
        monkeypatch.setattr(ct, "console", screen)
        # Prompt.ask / Confirm.ask print on rich's own console
        monkeypatch.setattr("rich.prompt.get_console", lambda: screen)
    w.cfg = ct.ConfigManager()
    return w


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / HOSTILE
    d.mkdir()
    return d


# The nine questions of a generation run that is not recursive, all answered with Enter.
ENTER = [""] * 8


def test_enter_everywhere_runs_the_backend_with_its_own_defaults(wiz, folder):
    assert wiz.run(folder, *ENTER) is True
    assert wiz.argv() == ["-InputDir", str(folder), "-Size", default("Size"),
                          "-Quality", default("Quality"), "-Format", default("Format"),
                          "-Page", default("Page"), "-Workers", "8"]
    assert len(wiz.asked) == 9, wiz.asked
    assert "Running: " in wiz.text


def test_the_run_is_journalled_for_repeat_last(wiz, folder):
    wiz.run(folder, *ENTER)
    c = wiz.cfg.config
    assert c.last_run_kind == "thumbnails"
    assert c.last_run_label.startswith(f"Thumbnails: {default('Size')}px {default('Format')} under ")
    assert c.last_run_commands == [wiz.ran[0]]
    assert c.last_input_dir == str(folder)
    # and it reached the file: a new manager reads it back
    again = ct.ConfigManager().config
    assert again.last_run_commands == [wiz.ran[0]] and again.last_input_dir == str(folder)


def test_every_answer_reaches_the_command_line(wiz, folder, tmp_path):
    out_dir = tmp_path / "thumbs out [x] 一時"
    assert wiz.run(folder, "n", "512", "70", "png", "all", out_dir, "y", "y", "sub [x];deep") is True
    assert wiz.argv() == ["-InputDir", str(folder), "-Size", "512", "-Quality", "70", "-Format", "png",
                          "-Page", "all", "-Workers", "8", "-OutputDir", str(out_dir),
                          "-ExcludeFolders", "sub [x];deep", "-Recursive", "-DryRun"]
    assert wiz.cfg.config.last_exclude_folders == "sub [x];deep"
    assert not out_dir.exists(), "the wizard must not create the output folder"


def test_a_dry_run_is_never_journalled(wiz, folder):
    """Repeat last and a preset run for real unless --dry-run is asked again."""
    wiz.run(folder, "", "", "", "", "", "", "", "y")
    assert wiz.argv()[-1] == "-DryRun"
    journalled = wiz.cfg.config.last_run_commands
    assert journalled == [wiz.ran[0][:-1]] and "-DryRun" not in journalled[0]


def test_exclude_folders_is_asked_only_for_a_recursive_run(wiz, folder):
    wiz.run(folder, "", "", "", "", "", "", "y", "", "-")
    assert len(wiz.asked) == 10 and "Exclude folders?" in wiz.text
    assert "-ExcludeFolders" not in wiz.argv() and "-Recursive" in wiz.argv()
    assert wiz.cfg.config.last_exclude_folders is None


def test_the_last_exclusion_is_offered_again(wiz, folder):
    wiz.cfg.config.last_exclude_folders = "ZIP;_EXPORT"
    wiz.run(folder, "", "", "", "", "", "", "y", "", "")
    argv = wiz.argv()
    assert argv[argv.index("-ExcludeFolders") + 1] == "ZIP;_EXPORT"


def test_remove_asks_three_things_and_no_generation_setting(wiz, folder, tmp_path):
    out_dir = tmp_path / "thumbs out"
    assert wiz.run(folder, "y", out_dir, "y", "n") is True
    assert wiz.argv() == ["-InputDir", str(folder), "-Remove", "-OutputDir", str(out_dir), "-Recursive"]
    assert len(wiz.asked) == 5, wiz.asked
    c = wiz.cfg.config
    assert c.last_run_kind == "thumbnails_remove" and c.last_run_label.startswith("Thumbnails: REMOVE under ")
    assert c.last_run_commands == [wiz.ran[0]]


def test_remove_dry_run(wiz, folder):
    wiz.run(folder, "y", "", "n", "y")
    assert wiz.argv() == ["-InputDir", str(folder), "-Remove", "-DryRun"]
    assert wiz.cfg.config.last_run_commands == [wiz.ran[0][:-1]]


def test_the_backends_exit_code_is_the_answer(wiz, folder):
    wiz.rc = 1
    assert wiz.run(folder, *ENTER) is False
    assert len(wiz.ran) == 1


def test_a_folder_that_does_not_exist_ends_the_workflow(wiz, tmp_path):
    missing = tmp_path / HOSTILE / "nowhere"
    assert wiz.run(missing) is False
    assert wiz.ran == [] and "Directory does not exist" in wiz.text
    assert wiz.cfg.config.last_run_kind is None and wiz.cfg.config.last_input_dir is None


def test_a_semicolon_in_the_folder_is_refused(wiz, tmp_path):
    """The backend splits -InputDir on ';': the path would become two bogus roots."""
    odd = tmp_path / "a;b"
    odd.mkdir()
    assert wiz.run(odd) is False
    assert wiz.ran == [] and "contains ';'" in wiz.text


def test_a_semicolon_in_the_output_folder_is_refused(wiz, folder):
    assert wiz.run(folder, "", "", "", "", "", "C:\\out;C:\\other") is False
    assert wiz.ran == [] and "Output path contains ';'" in wiz.text
    assert wiz.cfg.config.last_run_kind is None


def test_quotes_around_a_pasted_path_are_dropped(wiz, folder, tmp_path):
    out_dir = tmp_path / "o"
    wiz.run(f'"{folder}"', "", "", "", "", "", f"'{out_dir}'", "", "")
    argv = wiz.argv()
    assert argv[1] == str(folder) and argv[argv.index("-OutputDir") + 1] == str(out_dir)


def test_a_relative_folder_is_made_absolute(wiz, folder, monkeypatch):
    """Round 28: the backend (and a preset) gets the absolute path, never '.'."""
    monkeypatch.chdir(folder)
    wiz.run(".", *ENTER)
    assert wiz.argv()[1] == str(folder)
    assert wiz.cfg.config.last_run_commands[0][3] == str(folder)


def test_the_last_folder_is_the_default(wiz, folder):
    wiz.cfg.config.last_input_dir = str(folder)
    wiz.run("", *ENTER)
    assert wiz.argv()[1] == str(folder)


@pytest.mark.parametrize("answer", ["8", "5000", "abc", "-1", "31", "4097"])
def test_a_size_out_of_range_falls_back_to_the_backends_default(wiz, folder, answer):
    wiz.run(folder, "", answer, "", "", "", "", "", "")
    argv = wiz.argv()
    assert argv[argv.index("-Size") + 1] == default("Size")
    if answer != "abc":
        assert f"Size {answer} out of range (32-4096)" in wiz.text


@pytest.mark.parametrize("answer", ["32", "4096", "1024"])
def test_a_size_in_range_is_taken(wiz, folder, answer):
    wiz.run(folder, "", answer, "", "", "", "", "", "")
    argv = wiz.argv()
    assert argv[argv.index("-Size") + 1] == answer


@pytest.mark.parametrize("answer", ["0", "101", "abc", "8.5", "-3"])
def test_a_bad_quality_falls_back_to_the_backends_default(wiz, folder, answer):
    wiz.run(folder, "", "", answer, "", "", "", "", "")
    argv = wiz.argv()
    assert argv[argv.index("-Quality") + 1] == default("Quality")
    assert f"Invalid quality '{answer}'" in wiz.text


def test_the_workers_come_from_the_config_and_are_clamped(wiz, folder):
    wiz.cfg.config.last_workers = 3
    wiz.run(folder, *ENTER)
    argv = wiz.argv()
    assert argv[argv.index("-Workers") + 1] == "3"
    wiz.ran.clear()
    wiz.cfg.config.last_workers = 500
    wiz.run(folder, *ENTER)
    argv = wiz.argv()
    assert argv[argv.index("-Workers") + 1] == str(ct.clamp_workers(500)) != "500"


# --- where the two screens differ today (v3.0.0); phase 6 of v3.1 gives them one behaviour ---

def test_a_format_that_is_not_offered(wiz, folder):
    """Plain text: a message and the default. Rich: the question again, until a valid one."""
    if wiz.screen == "plain":
        wiz.run(folder, "", "", "", "gif", "", "", "", "")
        assert "Invalid format 'gif'" in wiz.text
        fmt = default("Format")
    else:
        wiz.run(folder, "", "", "", "gif", "tif", "", "", "", "")
        fmt = "tif"
    argv = wiz.argv()
    assert argv[argv.index("-Format") + 1] == fmt


def test_a_page_that_is_not_offered(wiz, folder):
    """Plain text takes any number; Rich offers 0 and all, and asks again."""
    if wiz.screen == "plain":
        wiz.run(folder, "", "", "", "", "7", "", "", "")
        page = "7"
    else:
        wiz.run(folder, "", "", "", "", "7", "all", "", "", "")
        page = "all"
    argv = wiz.argv()
    assert argv[argv.index("-Page") + 1] == page


def test_the_plain_screen_takes_the_long_format_names(wiz, folder):
    if wiz.screen != "plain":
        pytest.skip("Rich offers jpg / png / tif only")
    wiz.run(folder, "", "", "", "JPEG", "", "", "", "")
    argv = wiz.argv()
    assert argv[argv.index("-Format") + 1] == "jpeg"
