"""tools/move_old_backups.py (audit round 25; move_old_padded.py before): a backup whose
original place is empty is the ONLY copy and is never moved off the disk. On 2026-10-17
this script moved 365 such originals (stranded in OLD_TIFFs by an interrupted compress run)
to the backup drive. Hostile folder names throughout (Japanese, [ ], a space, %)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import move_old_backups as mop  # noqa: E402

HOSTILE = "伊豆沼_夏 [FINAL] 5% off"


def put(path: Path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def library(tmp_path):
    src = tmp_path / "G"
    cam = src / "2025" / HOSTILE / "01_D810"
    old = cam / "OLD_TIFFs"
    f = {
        "healthy": put(old / "a.tif", b"orig-a"),             # a.tif in place
        "only": put(old / "b.tif", b"orig-b"),                # nothing in place
        "tiffext": put(old / "c.tiff", b"orig-c"),            # c.tif in place
        # round 28: a stamp is a rename only next to the backup it collided with
        "stamped_first": put(old / "d.tif", b"orig-d"),        # d.tif in place
        "stamped": put(old / "d_20260803_081300.tif", b"d"),  # d.tif in place
        "stamped_only": put(old / "e_20260803_081300.tif", b"e"),
        # round 28: its OWN name has the stamp; an unrelated f.tif sits in the parent
        "stamped_own_name": put(old / "f_20260803_081300.tif", b"f"),
        # round 28: _v2 is not a rename compress makes; an unrelated g.tif in the parent
        "v2_in_old_tiffs": put(old / "g_v2.tif", b"g"),
        "sidecar": put(old / "a.xmp", b"<x/>"),               # a.xmp in place
        "sidecar_only": put(old / "z.xmp", b"<z/>"),
    }
    for n in ("a.tif", "c.tif", "d.tif", "f.tif", "g.tif", "a.xmp"):
        put(cam / n, b"zip-" + n.encode())
    padded = cam / "OLD_PADDED"
    put(padded / "p.tif", b"p1")                                # the backup p_v2 collided with
    f["v2"] = put(padded / "p_v2.tif", b"p2")                  # p.tif in place
    put(cam / "p.tif", b"p8")
    return src, cam, f


def test_place_is_empty_rules(library):
    src, cam, f = library
    assert not mop.place_is_empty(f["healthy"])
    assert mop.place_is_empty(f["only"])
    assert not mop.place_is_empty(f["tiffext"])
    assert not mop.place_is_empty(f["stamped"])
    assert mop.place_is_empty(f["stamped_only"])
    assert not mop.place_is_empty(f["sidecar"])
    assert mop.place_is_empty(f["sidecar_only"])
    assert not mop.place_is_empty(f["v2"])
    assert mop.place_is_empty(f["stamped_own_name"])
    assert mop.place_is_empty(f["v2_in_old_tiffs"])


def test_folder_is_required_and_picks_its_own_destination(library, monkeypatch, capsys):
    src, cam, f = library
    with pytest.raises(SystemExit):           # no silent default folder any more
        mop.main(["--source", str(src), "--roots", "2025"])
    with pytest.raises(SystemExit):
        mop.main(["--source", str(src), "--roots", "2025", "--folder", "ZIP"])
    capsys.readouterr()
    for folder, dest in mop.DEFAULT_DESTS.items():
        mop.main(["--source", str(src), "--roots", "2025", "--folder", folder])
        out = capsys.readouterr().out
        if folder == "OLD_PADDED":            # the one healthy OLD_PADDED backup is listed
            assert f"->  {dest}" in out


def test_dry_run_lists_the_only_copies_and_moves_nothing(library, tmp_path, capsys):
    src, cam, f = library
    rc = mop.main(["--source", str(src), "--roots", "2025", "--folder", "OLD_TIFFs",
                   "--dest", str(tmp_path / "H")])
    out = capsys.readouterr().out
    assert rc == 1                      # something needs the owner's attention
    assert "KEPT: 5 backup(s) have NOTHING in their original place" in out
    assert all(p.exists() for p in f.values())
    assert not (tmp_path / "H").exists()


def test_go_moves_only_backups_whose_original_is_in_place(library, tmp_path, capsys):
    src, cam, f = library
    dest = tmp_path / "H"
    rc = mop.main(["--source", str(src), "--roots", "2025", "--folder", "OLD_TIFFs",
                   "--dest", str(dest), "--go"])
    out = capsys.readouterr().out
    assert rc == 1
    for k in ("only", "stamped_only", "sidecar_only", "stamped_own_name", "v2_in_old_tiffs"):
        assert f[k].exists(), k                       # the only copies stay on the disk
        assert not (dest / f[k].relative_to(src)).exists()
    for k in ("healthy", "tiffext", "stamped_first", "stamped", "sidecar"):
        assert not f[k].exists(), k
        assert (dest / f[k].relative_to(src)).read_bytes() in (b"orig-a", b"orig-c", b"orig-d", b"d", b"<x/>")
    assert "5 kept (only copy)" in out
    assert f["v2"].exists()                           # another folder name: not touched


def test_an_original_deleted_after_planning_keeps_its_backup(library, tmp_path, monkeypatch, capsys):
    src, cam, f = library
    real = mop.place_is_empty
    calls = {"n": 0}

    def flaky(p):
        # planning sees a.tif in place; by the time it is moved the original is gone
        if Path(p) == f["healthy"]:
            calls["n"] += 1
            if calls["n"] > 1:
                return True
        return real(p)
    monkeypatch.setattr(mop, "place_is_empty", flaky)
    mop.main(["--source", str(src), "--roots", "2025", "--folder", "OLD_TIFFs",
              "--dest", str(tmp_path / "H"), "--go"])
    assert f["healthy"].exists()
    assert "nothing in its original place any more" in capsys.readouterr().out
