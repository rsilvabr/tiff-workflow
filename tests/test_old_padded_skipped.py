"""Audit round 31: the backends skip OLD_PADDED/ as compress skips its own OLD_TIFFs/.

OLD_PADDED/ is where the wizard's padded conversion (Diagnose, workflow 7) keeps each 16-bit
original it replaced with an 8-bit copy. The wizard's own discovery skipped it (round 16);
the backends did not. The owner's mode-8 preset, run over a folder the Diagnose had
converted, rewrote the originals kept there in place -- lossless, the pixel gate passed, but
the backup was no longer the original's bytes. Mode 9 made OLD_PADDED/OLD_TIFFs/, mode 3
OLD_PADDED/ZIP/, and generate_thumbnails --recursive wrote its JPEGs into it. Found on
2026-11-14 by the check of the owner's install. Real files, both lanes, a hostile path.
"""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backends import LANES  # noqa: E402
from test_exiftool_stdin import compression  # noqa: E402
from test_regression_classes import make_tiff, requires_tools, run_ps  # noqa: E402

NAME = "写真 [x] 50% 伊豆沼"
PHOTO = "0014_14 (2).tif"
DEFLATE = ("deflate", "adobe deflate")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _library(tmp_path):
    """<NAME>/0014_14 (2).tif and <NAME>/OLD_PADDED/0014_14 (2).tif, both uncompressed.
    Built under an ASCII name and renamed: the fixture's exiftool takes no Japanese path."""
    build = tmp_path / "build"
    (build / "OLD_PADDED").mkdir(parents=True)
    make_tiff(build / PHOTO)
    make_tiff(build / "OLD_PADDED" / PHOTO)
    root = tmp_path / NAME
    build.rename(root)
    return root


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestOldPaddedIsSkipped:

    @pytest.mark.parametrize("mode", [3, 8, 9])
    def test_compress_leaves_old_padded_alone(self, tmp_path, lane, mode):
        """Mode 3 copies into ZIP/, 8 rewrites in place, 9 rewrites in place and keeps the
        original in OLD_TIFFs/: every recursive shape. Modes 0 and 1 never enter a subfolder."""
        root = _library(tmp_path)
        backup = root / "OLD_PADDED" / PHOTO
        before = _sha(backup)

        r = run_ps(lane, "compress_tiff_zip.py",
                   ["--mode", str(mode), "--input-dir", str(root), "--workers", "1"], tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert sorted(p.name for p in backup.parent.iterdir()) == [PHOTO], \
            f"mode {mode} wrote into OLD_PADDED\n{r.stdout}"
        assert _sha(backup) == before, f"mode {mode} rewrote the original kept in OLD_PADDED"
        assert "Skipped 1 file(s) in OLD_TIFFs/OLD_TIFF/OLD_PADDED" in r.stdout, r.stdout
        done = root / "ZIP" / PHOTO if mode == 3 else root / PHOTO
        assert compression(done).lower() in DEFLATE, f"the photo itself was not compressed\n{r.stdout}"

    def test_compress_pointed_at_old_padded_still_works(self, tmp_path, lane):
        """Matched BELOW the scan root, as OLD_TIFFs: a run pointed AT the folder on purpose
        compresses what is in it (round 14: an absolute match hid whole trees)."""
        root = _library(tmp_path)

        r = run_ps(lane, "compress_tiff_zip.py",
                   ["--mode", "1", "--input-dir", str(root / "OLD_PADDED"), "--workers", "1"],
                   tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert compression(root / "OLD_PADDED" / "ZIP" / PHOTO).lower() in DEFLATE, r.stdout

    def test_thumbnails_leave_old_padded_alone(self, tmp_path, lane):
        root = _library(tmp_path)
        backup = root / "OLD_PADDED" / PHOTO
        before = _sha(backup)

        r = run_ps(lane, "generate_thumbnails.py",
                   ["--input-dir", str(root), "--recursive", "--size", "32"], tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert (root / "0014_14 (2)_thumb.jpg").exists(), r.stdout
        assert sorted(p.name for p in backup.parent.iterdir()) == [PHOTO], \
            f"a thumbnail was written into OLD_PADDED\n{r.stdout}"
        assert _sha(backup) == before
