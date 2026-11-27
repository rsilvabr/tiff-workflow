"""The wizard's command lines when every question is answered with Enter -- captured ONCE from
v3.2.1 (tests/data/review_default_argv.json), the contract of the review screen (v3.4 plan,
item 13): answered with just `r`, the new flow must give the same command lines.

    PYTHONIOENCODING=utf-8 python tests/review_golden.py <installation> <out.json>   # once

<installation> is a folder holding convert_tiff.py and tiff_workflow/ of the version to capture
(git archive of v3.2.1). The golden is NEVER regenerated afterwards (the rule of
wizard_registry_golden.json): a difference is a change of what Enter does.

`capture(ct, root, ui, flow)` drives the wizard module `ct` on the folder tree made by
`make_tree(root)`; `ui` is "plain" or "rich" (a stand-in for Rich's questions that, like Rich,
gives the default for an empty answer); `flow` is "enter" (every question answered with Enter,
v3.2.1) or "review" (the review screen answered `r`). Every question is answered by its text:
the up-front answers (the mode, the folder, the yes to mode 8's red question), Enter for the
rest. Paths are written relative: <ROOT>, <PY>, <ENTRY>."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

#: (task, mode): the eight tasks' five that run a backend, every compress mode
CASES = ([("compress", m) for m in range(10)]
         + [("copy_exif", None), ("fuji_compress", None), ("both", None), ("thumbnails", None),
            ("thumbnails_remove", None)])

ROOT_NAME = "伊豆沼 [FINAL] 50% x"


def make_tree(root: Path):
    """root/<ROOT_NAME>/: two S5pro sessions and a TIFF in the root (bytes only: no tool runs)."""
    lib = root / ROOT_NAME
    for rel in ("070101 S5pro [1]", "070102 S5pro (2)"):
        d = lib / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "DSCF0001 50%.tif").write_bytes(b"TIFF")
        (d / "DSCF0001 50%.jpg").write_bytes(b"JPEG")
    (lib / "0014_14 (2).tif").write_bytes(b"TIFF")
    return lib


def case_id(task, mode):
    return task if mode is None else f"{task}-mode{mode}"


class _Answers:
    """Answers by the text of the question."""

    def __init__(self, lib, mode, flow, remove=False):
        self.lib, self.mode, self.flow, self.remove = lib, mode, flow, remove
        self.asked = []

    def __call__(self, question):
        q = str(question)
        self.asked.append(q)
        if len(self.asked) > 200:
            raise AssertionError(f"more than 200 questions; the last: {q!r}")
        if q.startswith("Select mode"):
            return str(self.mode)
        if q.startswith("Input folder") or q.startswith("Input directory"):
            return str(self.lib)
        if q.startswith("Mode 8 will DELETE"):
            return "y"
        if self.remove and q.startswith("Remove existing thumbnails"):
            return "y"
        if self.flow == "review" and q.startswith("Review"):
            return "r"
        return ""


def _script(monkeypatch, ct, ui, answers):
    if ui == "plain":
        monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
        monkeypatch.setattr("builtins.input", lambda prompt="": answers(prompt.rstrip()))
        return
    monkeypatch.setattr(ct, "RICH_AVAILABLE", True)
    monkeypatch.setattr(ct, "console", type("C", (), {"print": staticmethod(lambda *a, **k: None)})())

    def ask(prompt, default=None, choices=None, **k):
        a = answers(prompt)
        return default if a == "" and default is not None else a

    def confirm(prompt, default=False, **k):
        a = answers(prompt)
        if a == "":
            return default
        return a.lower() in ("y", "yes", "s", "sim")
    monkeypatch.setattr(ct.Prompt, "ask", staticmethod(ask))
    monkeypatch.setattr(ct.Confirm, "ask", staticmethod(confirm))


def _relative(cmds, root, ct):
    out = []
    for cmd in cmds:
        line = []
        for a in cmd:
            a = str(a)
            if a == sys.executable:
                a = "<PY>"
            elif a == str(ct.ENTRY):
                a = "<ENTRY>"
            else:
                a = a.replace(str(root), "<ROOT>")
            line.append(a)
        out.append(line)
    return out


def capture(ct, root: Path, ui: str, flow: str, monkeypatch, task, mode):
    """{"run": [the command lines run], "journal": [the journal saved]} of one case."""
    lib = root / ROOT_NAME
    answers = _Answers(lib, mode, flow, remove=task == "thumbnails_remove")
    _script(monkeypatch, ct, ui, answers)
    ran = []

    def run_subprocess(cmd, timeout=None, stdin=None):
        ran.append([str(a) for a in cmd])
        return 0
    monkeypatch.setattr(ct, "run_subprocess", run_subprocess)
    cfg = SimpleNamespace(config=ct.ToolConfig(), save_config=lambda: None)
    start = {"compress": lambda: ct.run_free_compress(cfg),
             "copy_exif": lambda: ct._run_exif_or_compress(cfg, "copy_exif"),
             "fuji_compress": lambda: ct._run_exif_or_compress(cfg, "compress"),
             "both": lambda: ct._run_exif_or_compress(cfg, "both"),
             "thumbnails": lambda: ct.run_generate_thumbnails(cfg),
             "thumbnails_remove": lambda: ct.run_generate_thumbnails(cfg)}[task]
    start()
    journal = [[str(a).replace(str(root), "<ROOT>") for a in line]
               for line in (cfg.config.last_run_commands or [])]
    return {"run": _relative(ran, root, ct), "journal": journal}


def main(install, out):
    """Write the golden from the wizard of `install` (run once, from v3.2.1's code)."""
    import tempfile

    import pytest
    sys.path.insert(0, str(install))
    from tiff_workflow import wizard as ct
    assert Path(ct.__file__).resolve().is_relative_to(Path(install).resolve()), ct.__file__
    result = {"captured_from": ct.TOOLKIT_VERSION, "cases": {}}
    for ui in ("plain", "rich"):
        for task, mode in CASES:
            with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as mp:
                root = Path(td)
                make_tree(root)
                mp.setenv("TIFFWORKFLOW_SETTINGS", "")
                mp.setattr(ct, "step_advanced_options", lambda cfg, wf: True)
                got = capture(ct, root, ui, "enter", mp, task, mode)
            key = case_id(task, mode)
            if ui == "plain":
                result["cases"][key] = got
            else:
                assert result["cases"][key] == got, f"{key}: the two screens differ"
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(result['cases'])} cases from {ct.TOOLKIT_VERSION} -> {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
