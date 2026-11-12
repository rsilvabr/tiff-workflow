"""v3.1 phase 8: one entry point, and --help.

`python convert_tiff.py` is the wizard; `python convert_tiff.py compress|copy-exif|thumbnails
...` runs that backend, in the same process, exactly as its module would run with the same
arguments (`python -m tiff_workflow.compress_tiff_zip ...` runs too; v3.1 phase 9: the
backends are modules of the package `tiff_workflow`). Each backend prints its options with
--help; the parser that reads the command line has no --help of its own, because the wizard
binds saved command lines with it (tests/test_journal_lines.py)."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tiff_workflow import compress_tiff_zip as cz  # noqa: E402
import convert_tiff  # noqa: E402
from tiff_workflow import copy_exif_to_tiff as cx  # noqa: E402
from tiff_workflow import generate_thumbnails as gt  # noqa: E402
from tiff_workflow import common as tw  # noqa: E402

MODULES = {"compress": cz, "copy-exif": cx, "thumbnails": gt}
HOSTILE = "写真 [FINAL] 50% x"


def _wizard(args, cwd, timeout=300):
    r = subprocess.run([sys.executable, str(ROOT / "convert_tiff.py")] + [str(a) for a in args],
                       cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=timeout, env=dict(os.environ, TIFFWORKFLOW_SETTINGS=""))
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _script(script, args, cwd, timeout=300):
    """The backend run as its module, `python -m tiff_workflow.<module>`."""
    r = subprocess.run([sys.executable, "-m", "tiff_workflow." + Path(script).stem]
                       + [str(a) for a in args],
                       cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=timeout, env=dict(os.environ, TIFFWORKFLOW_SETTINGS="",
                                                 PYTHONPATH=str(ROOT)))
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def test_the_subcommands_are_the_three_backends():
    assert {s: "tiff_workflow." + Path(f).stem for s, f in convert_tiff.SUBCOMMANDS.items()} == \
        {s: m.__name__ for s, m in MODULES.items()}


@pytest.mark.parametrize("sub", sorted(MODULES))
def test_help_lists_every_option(sub, tmp_path, monkeypatch, capsys):
    """In the process (the backend's main) and through the entry point: exit 0, every option
    with its --no- form where it has one, and nothing made on disk."""
    module = MODULES[sub]
    monkeypatch.chdir(tmp_path)
    for asked in (["--help"], ["-h"], ["--mode", "9", "--help"]):
        assert module.main(asked) == 0
        out = capsys.readouterr().out
        for p in module.PARAMS:
            assert tw.cli_name(p.name) in out, (sub, p.name)
            if p.kind in ("bool", "switch"):
                assert "--no-" + tw.cli_name(p.name)[2:] in out
    assert os.listdir(tmp_path) == [], "--help made a log"
    rc, out = _wizard([sub, "--help"], tmp_path)
    assert rc == 0 and "usage: " in out and all(tw.cli_name(p.name) in out for p in module.PARAMS)


def test_the_wizard_help_names_the_subcommands(tmp_path):
    rc, out = _wizard(["--help"], tmp_path)
    assert rc == 0 and "compress|copy-exif|thumbnails" in out


@pytest.mark.parametrize("sub, args, code", [
    ("copy-exif", ["--no-such-option"], 2),
    ("compress", ["--input-dir", "x"], 2),                   # no --mode
    ("thumbnails", ["--size", "8"], 2),
    ("compress", ["--mode", "1", "--input-dir", "does not exist 伊豆沼", "--dry-run"], 1),
])
def test_the_exit_code_is_the_backends(sub, args, code, tmp_path):
    def first(text):            # the first line, without the time of a log line
        return re.sub(r"^\d\d:\d\d:\d\d \| ", "", text.splitlines()[0])

    rc, out = _wizard([sub] + args, tmp_path)
    assert rc == code, out
    rc2, out2 = _script(convert_tiff.SUBCOMMANDS[sub], args, tmp_path)
    assert rc2 == code and first(out) == first(out2), (out, out2)


def test_the_wizard_checks_the_package_next_to_it(tmp_path, monkeypatch):
    """The startup panel's "backend scripts" line: the files of the package next to
    convert_tiff.py, and which one is missing (v3.1 phase 9). A hostile installation folder."""
    from backends import PACKAGE, install_copy, wizard_uses_install
    monkeypatch.setattr(convert_tiff, "_probe_version", lambda cmd: None)
    install = wizard_uses_install(monkeypatch, convert_tiff, install_copy(tmp_path / HOSTILE))

    def backend_scripts():
        return {n: (ok, detail) for n, ok, detail in convert_tiff.check_dependencies(None)}["backend scripts"]

    assert backend_scripts() == (True, "all present")
    (install / PACKAGE / "common.py").unlink()
    (install / PACKAGE / "generate_thumbnails.py").unlink()
    assert backend_scripts() == (False, "missing: generate_thumbnails.py, common.py")


def test_the_entry_point_gives_argv_back(monkeypatch, capsys):
    before = list(sys.argv)
    assert convert_tiff._run_backend("thumbnails", ["--help"]) == 0
    assert sys.argv == before
    assert "--size" in capsys.readouterr().out


@pytest.mark.skipif(not (shutil.which("magick") and shutil.which("exiftool")),
                    reason="ImageMagick and exiftool required")
def test_a_real_run_through_the_entry_point_is_the_scripts(tmp_path):
    """The same compression, twice, from a hostile folder: through convert_tiff.py compress
    and through python -m tiff_workflow.compress_tiff_zip. Same exit code, same result
    lines, same files."""
    from test_regression_classes import make_tiff
    outs = []
    for how in ("entry", "script"):
        plain = tmp_path / how / "plain"
        plain.mkdir(parents=True)
        make_tiff(plain / "a.tif")
        work = tmp_path / how / HOSTILE
        work.mkdir()
        shutil.move(str(plain / "a.tif"), str(work / "a.tif"))
        args = ["--mode", "1", "--input-dir", work, "--workers", "1"]
        rc, out = (_wizard(["compress"] + args, tmp_path / how) if how == "entry"
                   else _script("compress_tiff_zip.py", args, tmp_path / how))
        assert rc == 0, out
        assert (work / "ZIP" / "a.tif").exists()
        # "HH:MM:SS | LEVEL | RESULT | a.tif | ...": the level, the result and the file
        outs.append(sorted(" | ".join(p.strip() for p in ln.split("|")[1:4])
                           for ln in out.splitlines() if "| a.tif" in ln))
    assert outs[0] == outs[1] and outs[0], outs
