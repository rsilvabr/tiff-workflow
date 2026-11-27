"""v3.3.0, the tidy commit (plan item 9): the padded gate reads the dimensions and the pages
with the module's own readers (_tiff_dimensions, _tiff_page_count) instead of nested copies,
and the Diagnose cache is written through the config's JSON writer (_write_json_file). No
behaviour change: the padded tests and the alias rule are the proof, plus what is pinned here."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiff_workflow import wizard as ct  # noqa: E402

HOSTILE = "伊豆沼 [FINAL] 50% x"


def _fake_run(identify_fails):
    """A subprocess.run for the padded conversion: the conversion writes a smaller file, every
    gate passes -- except `identify`, which fails when `identify_fails` says so."""
    from test_convert_tiff import _fake_exiftool_round28

    def run(cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        if "compare" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="0 (0)")
        if cmd[0] == "magick" and "-depth" in cmd:
            Path(cmd[-1].replace("%%", "%")).write_bytes(b"z" * 100)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "exiftool":
            return _fake_exiftool_round28(kwargs) or SimpleNamespace(returncode=0, stdout="", stderr="")
        if "null:" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "identify" in cmd:
            fmt = cmd[cmd.index("-format") + 1] if "-format" in cmd else ""
            if identify_fails(fmt, cmd):
                return SimpleNamespace(returncode=1, stdout="", stderr="identify: boom")
            if fmt.startswith("%n"):
                return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="100 100\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


@pytest.mark.parametrize("which,status", [
    ("dims-new", "dimension_unreadable"), ("dims-orig", "dimension_unreadable"),
    ("pages-new", "page_count_mismatch"), ("pages-orig", "page_count_mismatch"),
])
def test_an_identify_that_fails_never_replaces_the_original(tmp_path, monkeypatch, which, status):
    import subprocess
    folder = tmp_path / HOSTILE
    folder.mkdir()
    src = folder / "0014_14 (2) 50%.tif"
    src.write_bytes(b"x" * 2000)
    kind, whose = which.split("-")

    def fails(fmt, cmd):
        on_new = "tmp8_" in cmd[-1]          # the 8-bit file is staged as tmp8_<id>_<name>
        right_kind = fmt.startswith("%n") if kind == "pages" else fmt.startswith("%w")
        return right_kind and (on_new if whose == "new" else not on_new)
    monkeypatch.setattr(subprocess, "run", _fake_run(fails))
    staging = tmp_path / "stg"
    staging.mkdir()
    r = ct._process_single_padded(src, staging)
    assert r.status == status, r.status
    assert src.read_bytes() == b"x" * 2000
    assert not (folder / "OLD_PADDED").exists()


def test_a_cache_written_by_v321_still_loads(tmp_path, monkeypatch):
    path = tmp_path / ".convert_tiff_diagnose_cache.json"
    monkeypatch.setattr(ct, "_diagnose_cache_path", lambda: path)
    old = {"c:\\x\\伊豆沼 [x] 50%.tif|12|34": {"real": True, "detail": "ok"}}
    path.write_text(json.dumps(old), encoding="utf-8")         # how v3.2.1 wrote it
    assert ct._load_diagnose_cache() == old
    ct._save_diagnose_cache(old)
    assert ct._load_diagnose_cache() == old
    assert path.read_text(encoding="utf-8").count("\n") == 1, "one line, as before"
    assert [p.name for p in tmp_path.iterdir()] == [path.name], "no temp file left"


def test_a_save_that_fails_leaves_the_old_file_whole(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".convert_tiff_diagnose_cache.json"
    monkeypatch.setattr(ct, "_diagnose_cache_path", lambda: path)
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(ct.tw, "move_file_replacing", boom)
    ct._save_diagnose_cache({"a": 2, "b": 3})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert "could not save the Diagnose cache" in capsys.readouterr().out
    assert [p.name for p in tmp_path.iterdir()] == [path.name], "the temp file stayed"
