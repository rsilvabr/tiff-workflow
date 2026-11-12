"""v3.1, phase 3: what changed in the backends and needs REAL files to be seen.

The rules that can run with the tools replaced are in test_backend_invariants.py; here are
the ones only ImageMagick and exiftool themselves can answer. Fixtures are built under a
plain name and moved into a hostile folder (Japanese, [ ], a space, a %): the fixture's own
tool calls read their command line in the ANSI code page.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tiff_workflow_common as tw  # noqa: E402
from backends import LANES  # noqa: E402
from test_regression_classes import (  # noqa: E402
    make_multipage_tiff, numeric_subfiletypes, page_count, read_compression, requires_tools, run_ps,
)

HOSTILE = "190815_Film_伊豆沼 [FINAL] 50% x"
RED, GREEN = "srgb(255,0,0)", "srgb(0,128,0)"       # what identify prints for xc:red, xc:green


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


def solid(path, colour):
    """An uncompressed 16-bit TIFF of one colour, built where the tools can read the name."""
    subprocess.run(["magick", "-size", "64x48", f"xc:{colour}", "-depth", "16", "-type", "TrueColor",
                    "-compress", "none", str(path)], capture_output=True, check=True, timeout=120)
    return path


def first_pixel(path):
    r = subprocess.run(["magick", "identify", "-format", "%[pixel:p{0,0}]", f"{path}[0]"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout.strip()


@requires_tools
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("extra", [["-Overwrite"], ["-DuplicateAction", "Overwrite", "-Overwrite"]],
                         ids=["overwrite", "duplicates-overwrite"])
def test_mode2_never_replaces_a_photo_with_another_one(tmp_path, lane, extra):
    """Mode 2 without -OutputDir flattens into the input folder itself. With -Overwrite the
    red photo of the root was replaced by the blue one of a subfolder that has the same
    name -- the only copy of it gone, "OK", exit code 0 (v3.0.0, found by the review of
    phase 1). The blue one is refused; the red one is compressed in its own place."""
    plain = tmp_path / "plain"
    (plain / "NORITSU").mkdir(parents=True)
    solid(plain / "a.tif", "red")
    solid(plain / "NORITSU" / "a.tif", "blue")
    solid(plain / "NORITSU" / "c.tif", "green")
    root = tmp_path / HOSTILE
    plain.rename(root)
    (root / "NORITSU").rename(root / "NORITSU [1] 一時")
    sub = root / "NORITSU [1] 一時"
    blue_before = (sub / "a.tif").read_bytes()
    assert first_pixel_plain(root / "a.tif", tmp_path) == RED

    result = run_ps(lane, "compress_tiff_zip.py", ["-Mode", "2", "-InputDir", root] + extra, tmp_path)

    out = result.stdout
    assert result.returncode == 1, out
    assert "ERROR (mode 2 would replace another source file of this run - refused; pass -OutputDir) | a.tif" in out
    assert "Mode 2 writes into the input folder itself: 2 file(s) from its subfolders will be COPIED" in out
    assert first_pixel_plain(root / "a.tif", tmp_path) == RED, "the red photo is gone"
    assert read_compression(root / "a.tif").lower() == "zip", "and it is compressed in its own place"
    assert (sub / "a.tif").read_bytes() == blue_before
    assert first_pixel_plain(root / "c.tif", tmp_path) == GREEN, "the copy of c.tif in the root"
    assert sorted(p.name for p in root.iterdir()) == ["NORITSU [1] 一時", "a.tif", "c.tif"]


def first_pixel_plain(path, tmp_path):
    """The first pixel of `path`, read from a copy under a plain name (magick globs '[ ]' in
    a spec with a [0] selector, and the folder has them)."""
    copy = tmp_path / "px_probe.tif"
    copy.write_bytes(Path(path).read_bytes())
    try:
        return first_pixel(copy)
    finally:
        copy.unlink()


@requires_tools
@pytest.mark.parametrize("lane", LANES)
def test_copy_exif_zip_keeps_the_page_markers(tmp_path, hostile, lane):
    """v3.1 phase 4: copy_exif -CompressZip reads the TIFF's page markers in the same exiftool
    call as its compression and layout, and puts them back on the ZIP: an explicit 0 on the
    main image and the REDUCEDIMAGE preview."""
    plain, land = hostile
    src = make_multipage_tiff(plain / "DSCF0001 (2).tif", [0, 1])
    jpg = plain / "DSCF0001 (2).jpg"
    subprocess.run(["magick", "-size", "64x48", "gradient:", "-depth", "8", str(jpg)],
                   capture_output=True, check=True, timeout=120)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Make=FUJIFILM",
                    "-Model=FinePix S5Pro", str(jpg)], capture_output=True, check=True, timeout=120)
    before = numeric_subfiletypes(src)
    assert before == ["[IFD0] SubfileType: 0", "[IFD1] SubfileType: 1"]
    tif = land(src)
    land(jpg)
    result = run_ps(lane, "copy_exif_to_tiff.py", ["-InputDir", str(tif.parent), "-CompressZip"], tmp_path)
    assert result.returncode == 0, result.stdout
    assert "OK+ZIP | DSCF0001 (2).tif" in result.stdout, result.stdout
    probe = tmp_path / "probe.tif"
    probe.write_bytes(tif.read_bytes())
    assert read_compression(probe).lower() == "zipzip", "both pages ZIP (magick prints one per page)"
    assert numeric_subfiletypes(probe) == before


@requires_tools
@pytest.mark.parametrize("lane", LANES)
def test_mode5_never_replaces_a_photo_living_in_zip(tmp_path, lane):
    """v3.1 (review of phases 3+4, V4; so since v3.0.0): ZIP/x.tif (an uncompressed photo
    living in ZIP/) and sub/x.tif (another photo). Mode 5 sends sub/x.tif to ZIP/x.tif; with
    -Overwrite the red photo was replaced by the blue one, OK and exit code 0."""
    plain = tmp_path / "plain"
    (plain / "ZIP").mkdir(parents=True)
    (plain / "sub [1]").mkdir()
    solid(plain / "ZIP" / "x.tif", "red")
    solid(plain / "sub [1]" / "x.tif", "blue")
    root = tmp_path / HOSTILE
    plain.rename(root)
    blue_before = (root / "sub [1]" / "x.tif").read_bytes()
    result = run_ps(lane, "compress_tiff_zip.py", ["-Mode", "5", "-InputDir", root, "-Overwrite"], tmp_path)
    out = result.stdout
    assert result.returncode == 1, out
    assert ("ERROR (mode 5 would replace another source file of this run - refused) | x.tif | "
            + str(root / "ZIP" / "x.tif")) in out, out
    assert first_pixel_plain(root / "ZIP" / "x.tif", tmp_path) == RED, "the red photo is gone"
    assert read_compression(root / "ZIP" / "x.tif").lower() == "zip", "compressed in its own place"
    assert (root / "sub [1]" / "x.tif").read_bytes() == blue_before
    assert sorted(p.name for p in (root / "ZIP").iterdir()) == ["x.tif"], "no x_v2.tif either"


#: an exiftool older than 12.15: the -echo3/-echo4 text comes back without the exit status
_OLD_EXIFTOOL = r"""import os, subprocess, sys
data = sys.stdin.buffer.read() if "-" in sys.argv[1:] else None
if data:
    data = data.replace(b"${status}", b"")
r = subprocess.run([os.environ["TW_REAL_EXIFTOOL"]] + sys.argv[1:], input=data, capture_output=True)
sys.stdout.buffer.write(r.stdout)
sys.stderr.buffer.write(r.stderr)
sys.exit(r.returncode)
"""


@requires_tools
@pytest.mark.skipif(os.name != "nt", reason="the old exiftool stand-in is a .cmd")
@pytest.mark.parametrize("script, args", [
    ("compress_tiff_zip.py", ["-Mode", "9"]),
    ("copy_exif_to_tiff.py", ["-CompressZip"]),
], ids=["compress", "copy_exif-zip"])
def test_an_exiftool_without_the_status_of_each_command_is_refused(tmp_path, hostile, script, args):
    """Review of v3.1 phases 3+4, V1: the writes after a conversion read each command's exit
    status from `${status}`; an older exiftool prints the marker without it and every file
    would end as an ERROR or a WARN although the writes were done. The run stops first, with
    a sentence, and touches nothing."""
    plain, land = hostile
    photo = land(make_multipage_tiff(plain / "x 50%.tif", [None]))
    before = photo.read_bytes()
    shim = tmp_path / "old exiftool"
    shim.mkdir()
    (shim / "old_exiftool.py").write_text(_OLD_EXIFTOOL, encoding="utf-8")
    (shim / "exiftool.cmd").write_text(
        '@echo off\r\n"' + sys.executable + '" "%~dp0old_exiftool.py" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="ascii", newline="")
    env = dict(os.environ, TW_REAL_EXIFTOOL=shutil.which("exiftool"), TIFFWORKFLOW_SETTINGS="")
    env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    r = subprocess.run([sys.executable, str(REPO / script), "-InputDir", str(photo.parent)] + args,
                       cwd=str(tmp_path), env=env, capture_output=True, timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 1, out
    assert "does not report the result of each command it runs; update it to 12.15 or later" in out, out
    assert photo.read_bytes() == before
    assert sorted(x.name for x in photo.parent.iterdir()) == ["x 50%.tif"], "nothing written"
