"""The wizard's one screen (v3.1 phase 7, T4): _say, _error, _ask and _ask_yes_no are the only
code that knows whether Rich is there. What they promise: the Rich screen shows the very
words the plain screen prints, whatever a folder or preset name holds ([bold], [/b],
[FINAL], a backslash at the end, %); a question reads its answers the same way on both
screens."""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import convert_tiff as ct  # noqa: E402

rich = pytest.importorskip("rich")
from rich.console import Console  # noqa: E402

NAMES = ["190815_Film_伊豆沼_夏 [FINAL]\\NORITSU\\0014_14 (2) 50%.tif", "[bold]x[/bold]", "a [/b] b",
         "\\[escaped]", "[red]", "trail\\", "G:\\"]


def _console(out):
    return Console(file=out, force_terminal=False, color_system=None, width=400)


def _shown(renderable) -> str:
    out = io.StringIO()
    _console(out).print(renderable)
    return out.getvalue()


@pytest.mark.parametrize("name", NAMES)
def test_the_rich_screen_shows_the_plain_words(name):
    for text, style in [(f"Folder: {name}", ""), (f"\nRunning: {name}\n", "dim"),
                        (f"{name}", "dim"), ([("Saved to:", "green"), " ", name], ""),
                        ([("Mode 9", "bold"), f" | {name}"], "yellow"), ([(name, "red")], "")]:
        assert _shown(ct._rich_text(text, style)) == ct._plain(text) + "\n", (text, style)


def test_a_parts_style_wins_over_the_lines():
    t = ct._rich_text([("OK", "green"), " done"], "red")
    c = _console(io.StringIO())
    assert t.get_style_at_offset(c, 0).color.name == "green"
    assert t.get_style_at_offset(c, 3).color.name == "red"


def test_the_screen_keeps_its_highlighting(monkeypatch):
    """console.print highlights a string (numbers, paths); _say keeps that under its styles."""
    monkeypatch.setattr(ct, "console", _console(io.StringIO()))
    t = ct._highlighted(ct._rich_text("3 file(s) in C:\\x", "dim"))
    assert t.plain == "3 file(s) in C:\\x"
    assert {s.style for s in t.spans if s.start == 0 and s.end == 1} - {"dim"}, "the 3 lost its highlight"
    monkeypatch.setattr(ct, "console", type("C", (), {"print": staticmethod(lambda *a, **k: None)})())
    assert ct._highlighted(ct._rich_text("x")).plain == "x", "a console without a highlighter"


@pytest.mark.parametrize("screen", ["plain", "rich"])
def test_say_and_error_print_the_same_words(monkeypatch, capsys, screen):
    out = io.StringIO()
    monkeypatch.setattr(ct, "RICH_AVAILABLE", screen == "rich")
    monkeypatch.setattr(ct, "console", _console(out))
    ct._say([("Saved to:", "green"), " ", NAMES[0]])
    ct._say("Cancelled.", "dim")
    ct._error("Folder not found: [x]")
    text = out.getvalue() if screen == "rich" else capsys.readouterr().out
    lines = text.splitlines()
    assert lines[0] == f"Saved to: {NAMES[0]}" and lines[1] == "Cancelled."
    assert lines[2] == ("Folder not found: [x]" if screen == "rich" else "ERROR: Folder not found: [x]")


def test_plain_questions(monkeypatch, capsys):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    asked = []

    def answer(*words):
        it = iter(words)
        monkeypatch.setattr("builtins.input", lambda p="": asked.append(p) or next(it))

    answer("sim")
    assert ct._ask_yes_no("Delete preset 'x [y]'?", False) is True
    answer("")
    assert ct._ask_yes_no([("\nMove all files back?", "yellow")], True) is True
    assert asked[-2:] == ["Delete preset 'x [y]'? [y/N]: ", "\nMove all files back? [Y/n]: "]
    answer("")
    assert ct._ask("Output folder", "D:\\out 伊豆沼") == "D:\\out 伊豆沼"
    answer("")
    assert ct._ask("Preset name") == ""
    assert asked[-2:] == ["Output folder [D:\\out 伊豆沼]: ", "Preset name: "]
    answer("overwrite", "Skip")                       # not a choice: asked again, as on Rich
    assert ct._ask("Duplicate filenames", "Numbered", choices=["Numbered", "Skip", "Overwrite"]) == "Skip"
    assert asked[-2:] == ["Duplicate filenames (Numbered/Skip/Overwrite) [Numbered]: "] * 2
    assert "Please select one of the available options" in capsys.readouterr().out


def test_rich_questions_get_the_text_and_the_default(monkeypatch):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", True)
    seen = []
    monkeypatch.setattr(ct.Confirm, "ask", classmethod(lambda cls, p, **k: seen.append((p, k)) or True))
    monkeypatch.setattr(ct.Prompt, "ask", classmethod(lambda cls, p, **k: seen.append((p, k)) or " v "))
    assert ct._ask_yes_no([("Safe mode?", "cyan"), " (skip multi-page TIFFs)"], True) is True
    assert ct._ask("Name [x]", style="") == "v"
    assert ct._ask("Dup", "Numbered", choices=["Numbered", "Skip"]) == "v"
    assert [(p.plain, k) for p, k in seen] == [
        ("Safe mode? (skip multi-page TIFFs)", {"default": True}),
        ("Name [x]", {}),
        ("Dup", {"default": "Numbered", "choices": ["Numbered", "Skip"]})]
    assert [(s.start, s.end, s.style) for s in seen[0][0].spans] == [(0, 10, "cyan")]
