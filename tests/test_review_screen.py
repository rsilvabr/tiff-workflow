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
FLOW = "enter"


def test_the_golden_was_captured_from_v321_and_covers_every_case():
    assert GOLDEN["captured_from"] == "3.2.1"
    assert sorted(GOLDEN["cases"]) == sorted(rg.case_id(t, m) for t, m in rg.CASES)


@pytest.mark.parametrize("ui", UIS)
@pytest.mark.parametrize("task,mode", rg.CASES, ids=[rg.case_id(t, m) for t, m in rg.CASES])
def test_enter_everywhere_gives_the_command_lines_of_v321(tmp_path, monkeypatch, ui, task, mode):
    if ui == "rich":
        pytest.importorskip("rich")
    rg.make_tree(tmp_path)
    got = rg.capture(ct, tmp_path, ui, FLOW, monkeypatch, task, mode)
    assert got == GOLDEN["cases"][rg.case_id(task, mode)]
