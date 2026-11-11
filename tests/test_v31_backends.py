"""v3.1, phase 3: what changed in the backends and needs REAL files to be seen.

The rules that can run with the tools replaced are in test_backend_invariants.py; here are
the ones only ImageMagick and exiftool themselves can answer. Fixtures are built under a
plain name and moved into a hostile folder (Japanese, [ ], a space, a %): the fixture's own
tool calls read their command line in the ANSI code page.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tiff_workflow_common as tw  # noqa: E402
from test_regression_classes import (  # noqa: E402
    make_multipage_tiff, page_count, requires_tools,
)

HOSTILE = "190815_Film_伊豆沼 [FINAL] 50% x"


@pytest.fixture
def hostile(tmp_path):
    """(the plain folder to build in, a function that moves a built file into the hostile one)."""
    plain = tmp_path / "plain"
    plain.mkdir()
    target = tmp_path / HOSTILE
    target.mkdir()

    def land(path, name=None):
        final = target / (name or path.name)
        path.rename(final)
        return final
    return plain, land


@requires_tools
class TestPageCountWithPing:
    """tw.tiff_page_count is `identify -ping`: the directories of the file, no pixel decoded.
    It has to count what the full identify counted, on every layout the gates meet."""

    @pytest.mark.parametrize("subfiletypes, pages", [
        ([None], 1),
        ([None, 1], 2),             # image + reduced preview
        ([None, 1, 4], 3),          # scanner: image, preview, IR mask
        ([None, None, None], 3),    # three full pages, no markers
    ], ids=["one", "preview", "scanner-ir", "three-unmarked"])
    def test_it_counts_every_page_of_a_layout(self, hostile, subfiletypes, pages):
        plain, land = hostile
        built = plain / "scan.tif"
        make_multipage_tiff(built, subfiletypes)
        assert page_count(built) == pages, "the fixture itself"
        photo = land(built, "0014_14 (2) 50%off.tif")
        assert tw.tiff_page_count(str(photo)) == tw.PageCount(True, pages, "")

    def test_a_file_cut_short_is_not_counted(self, hostile):
        """The directory of a TIFF that ImageMagick wrote is at the end: a file that lost its
        tail has none. Fails closed, as before."""
        plain, land = hostile
        built = plain / "cut.tif"
        subprocess.run(["magick", "-size", "600x400", "plasma:fractal", "-depth", "16", str(built)],
                       capture_output=True, check=True, timeout=120)
        data = built.read_bytes()
        built.write_bytes(data[: len(data) * 6 // 10])
        pc = tw.tiff_page_count(str(land(built)))
        assert not pc.ok and pc.count == 0 and pc.error == "failed"

    def test_the_count_is_not_a_judgement_on_the_pixels(self, hostile):
        """Damaged pixel data, directory intact: the count answers 1 -- as the full identify
        always did here (it prints the count and only its exit code tells; the count never
        read the exit code). Whether the pixels decode is the business of zip_integrity and
        pixel_identical, not of this probe."""
        plain, land = hostile
        built = plain / "damaged.tif"
        subprocess.run(["magick", "-size", "600x400", "-seed", "1", "plasma:fractal", "-depth", "16",
                        "-compress", "zip", str(built)], capture_output=True, check=True, timeout=120)
        data = bytearray(built.read_bytes())
        for i in range(len(data) // 3, len(data) // 3 + 20000):
            data[i] = 0x55
        built.write_bytes(bytes(data))
        assert page_count(built) == 1, "the full identify, on the plain name"
        assert tw.tiff_page_count(str(land(built))) == tw.PageCount(True, 1, "")
