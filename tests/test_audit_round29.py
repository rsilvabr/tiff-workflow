"""Audit round 29: the final review of v3.1 (Fable, 2026-11-14) -- the "worth fixing" items
and the small ones. V1 (mode 8 --delete-source after a landing that failed) and V2 (copy_exif's
gate order) are pinned in tests/test_backend_invariants.py, the twin of a restored .tiff in
tests/test_regression_classes.py (TestUndo...), the -rcN version key in
tests/test_release_notice.py."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiff_workflow import common as tw  # noqa: E402
from tiff_workflow import compress_tiff_zip as cz  # noqa: E402
from tiff_workflow import wizard as ct  # noqa: E402

HOSTILE = "伊豆沼 [FINAL] 50% x"


def test_padded_extra_pages_that_cannot_be_classified_are_refused(tmp_path, monkeypatch):
    """V3: `if extras_ok is not True` -> `if extras_ok is False` (an unreadable page layout
    converted) failed no test. The file is refused before anything reads or writes it."""
    folder = tmp_path / HOSTILE
    folder.mkdir()
    src = folder / "0014_14 (2).tif"
    src.write_bytes(b"A SCAN WITH PAGES NOBODY COULD CLASSIFY")
    staging = tmp_path / "stg"
    staging.mkdir()
    reached = []
    monkeypatch.setattr(ct, "_tiff_extra_pages_are_thumbnails", lambda p: None)
    monkeypatch.setattr(ct, "_tiff_sample_layout", lambda p: reached.append(p) or ["RGB"])
    result = ct._process_single_padded(src, staging)
    assert result.status == "page_layout_unreadable", result.status
    assert reached == [], "nothing after the page check may run"
    assert src.read_bytes() == b"A SCAN WITH PAGES NOBODY COULD CLASSIFY"
    assert list(staging.iterdir()) == []


def test_pixel_identical_never_passes_without_comparing_a_page(tmp_path, monkeypatch):
    """Small one: a page count of 0 on both files returned True with nothing compared. The
    gate authorises destroying the source: no page compared is no proof."""
    compares = []
    monkeypatch.setattr(tw, "tiff_page_count", lambda p, t=30: tw.PageCount(True, 0, ""))
    monkeypatch.setattr(tw, "magick_probe", lambda args, timeout=30: compares.append(args))
    a, b = tmp_path / f"{HOSTILE} a.tif", tmp_path / f"{HOSTILE} b.tif"
    a.write_bytes(b"A")
    b.write_bytes(b"B")
    assert tw.pixel_identical(str(a), str(b)) is False
    assert compares == []


@pytest.mark.parametrize("argv,hint", [
    (["--mode", "1", "--exclude-folders", "-old"], "is written --exclude-folders=-old"),
    (["--mode", "1", "-InputDir", "D:\\x"], "the command line of v3.0.0 (-Name value)"),
])
def test_a_value_that_starts_with_a_dash_gets_its_own_hint(argv, hint):
    """Small one: `--exclude-folders -old` was refused (right) with the hint about the command
    line of v3.0.0, because -old looks like -Name; the hint says how to write such a value."""
    with pytest.raises(tw.UsageError) as e:
        tw.parse_args(argv, cz.PARAMS)
    assert hint in str(e.value), str(e.value)
    assert ["--exclude-folders=-old"] == tw.cli_value("exclude_folders", "-old"), \
        "the form the hint names is the one the wizard writes"


class _ExistsLater(type(Path())):
    """A target the check sees as free, and that is taken when the move comes: a file that
    appeared after the check."""

    def exists(self, *args, **kwargs):
        return False


def test_restore_stranded_never_moves_over_a_file_that_appeared(tmp_path, capsys, monkeypatch):
    """Small one: the wizard's Restore of stranded originals checked target.exists() and then
    moved with _safe_move, which REPLACES the destination. It renames now, as the backend's
    --restore-stranded does: a file that appeared after the check stays, and so does the
    backup."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cam = tmp_path / HOSTILE
    old = cam / "OLD_TIFFs"
    old.mkdir(parents=True)
    backup = old / "a 50%.tif"
    backup.write_bytes(b"THE ORIGINAL")
    target = cam / "a 50%.tif"
    target.write_bytes(b"A NEWER FILE THAT APPEARED")
    assert ct._restore_stranded_only([(backup, _ExistsLater(target))]) is True
    assert target.read_bytes() == b"A NEWER FILE THAT APPEARED"
    assert backup.read_bytes() == b"THE ORIGINAL"
    assert "KEPT (a 50%.tif appeared meanwhile)" in capsys.readouterr().out
    os.remove(target)
    assert ct._restore_stranded_only([(backup, target)]) is True
    assert target.read_bytes() == b"THE ORIGINAL" and not backup.exists()
