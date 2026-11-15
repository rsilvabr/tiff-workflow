"""v3.2: compress --recompress-zip, the kept date and --include-folders, end to end on real
TIFFs, both lanes, in a hostile folder (Japanese, [ ], a space, %).

Capture One writes its TIFF exports with Deflate at zlib level 6 and NO predictor (measured on
the owner's exports, 15/11/2026); ImageMagick adds the horizontal predictor for 8 and 16 bits,
and the same image comes out 5-11 % smaller. --recompress-zip rewrites such a file through the
normal path -- the same gates, the same landing -- and keeps it only when it is smaller; a file
that takes its original's place keeps the original's modification time (jxl-photo's smart sync
re-encodes every master whose TIFF looks newer). The fixture is a Capture One export in small:
a 16-bit RGB page in Deflate without a predictor, a 160 px reduced page (SubfileType 1), EXIF,
XMP, UTF-8 IPTC with Japanese text, an ICC profile.

The rules that need no real file (which gate runs first, what a failed drop must not land, the
no-gain memory) are in tests/test_backend_invariants.py.
"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backends import LANES  # noqa: E402
from test_exiftool_stdin import compression, exif_stdin  # noqa: E402
from test_regression_classes import make_tiff, requires_tools, rmse_pages, run_ps  # noqa: E402
from tiff_workflow import common as tw  # noqa: E402

FOLDER = "写真 [x] 50% 伊豆沼"
PHOTO = "0014_14 (2) 50%.tif"
OLD_NS = 1_700_000_000 * 10 ** 9            # the export's date (NTFS keeps 100 ns)
CAPTION = "伊豆沼の夏 [FINAL] 100%"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _magick(*args):
    tw.magick_run([str(a) for a in args])


def make_c1_like(path, build):
    """A Capture One TIFF export in small, at `path`, with the export's old date. Built in
    `build`, a folder with a plain name, and moved: the fixture's magick expands the '%' of an
    output name, and its exiftool takes no Japanese path on the command line."""
    build.mkdir(parents=True)
    p0, p1, out = build / "p0.tif", build / "p1.tif", build / "c1.tif"
    _magick("-size", "480x320", "-seed", "7", "plasma:fractal", "-depth", "16",
            "-type", "TrueColor", p0)
    _magick(p0, "-resize", "160x", "-depth", "8", p1)
    _magick(p0, p1, "-define", "tiff:predictor=1", "-compress", "zip", out)
    tags = ["-overwrite_original", "-IFD1:SubfileType#=1", "-EXIF:Make=Capture One Test",
            "-EXIF:Model=Z 8", "-XMP-dc:Title=伊豆沼", "-IPTC:CodedCharacterSet=UTF8",
            f"-IPTC:Caption-Abstract={CAPTION}"]
    icc = tw.resolve_srgb_profile("")
    if icc:
        tags.append(f"-ICC_Profile<={icc}")
    tw.exiftool(tags + [str(out)])
    facts = tw.recompress_facts(str(out))
    assert facts.predictor in ("-", "1") and facts.bits == "16 16 16", facts
    shutil.move(str(out), str(path))
    shutil.rmtree(build)
    os.utime(path, ns=(OLD_NS, OLD_NS))
    return path


def _meta(path):
    """What the recompression must keep: EXIF, XMP, IPTC (in UTF-8), the ICC profile's bytes,
    the reduced page's marker."""
    text = exif_stdin(path, "-j", "-G1", "-a", "-EXIF:Make", "-EXIF:Model", "-XMP-dc:Title",
                      "-IPTC:Caption-Abstract", "-IPTC:CodedCharacterSet", "-IFD1:SubfileType#")
    d = json.loads(text)[0]
    d.pop("SourceFile", None)
    d["icc"] = hashlib.md5(exif_stdin(path, "-b", "-ICC_Profile").encode("utf-8", "replace")).hexdigest()
    return d


def _export(tmp_path):
    """<FOLDER>/s 1/_EXPORT/TIFF16/<PHOTO>, a Capture One-like export; and a pristine copy of it."""
    photo = tmp_path / FOLDER / "s 1" / "_EXPORT" / "TIFF16" / PHOTO
    photo.parent.mkdir(parents=True)
    make_c1_like(photo, tmp_path / "build")
    pristine = tmp_path / "pristine.tif"
    shutil.copy2(photo, pristine)
    return photo, pristine


def _assert_same_image(photo, pristine):
    assert rmse_pages(photo, pristine, 0).startswith("0 (0)"), rmse_pages(photo, pristine, 0)
    assert rmse_pages(photo, pristine, 1).startswith("0 (0)"), rmse_pages(photo, pristine, 1)
    assert _meta(photo) == _meta(pristine)


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestRecompressZipE2E:

    def test_mode8_recompresses_in_place_and_keeps_everything(self, tmp_path, lane):
        photo, pristine = _export(tmp_path)
        args = ["--mode", "8", "--input-dir", str(tmp_path / FOLDER), "--staging-dir",
                str(tmp_path / "stg [x] 50%"), "--recompress-zip"]

        r = run_ps(lane, "compress_tiff_zip.py", args, tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert "OK (Adobe Deflate -> ZIP, recompressed: " in r.stdout, r.stdout
        assert "Recompressed: 1 file(s)" in r.stdout, r.stdout
        assert photo.stat().st_size < pristine.stat().st_size
        facts = tw.get_recompress_facts([str(photo)])[str(photo).lower()]
        assert facts.predictor == "2", facts
        _assert_same_image(photo, pristine)
        assert "recompressed with predictor" in exif_stdin(photo, "-s3", "-XMP-xmpMM:HistoryParameters")
        assert photo.stat().st_mtime_ns == OLD_NS, "the export's date must stay (jxl-photo's smart sync)"

        # run twice, fed its own output: the file now has a predictor and is left alone
        before = _sha(photo)
        r = run_ps(lane, "compress_tiff_zip.py", args, tmp_path)
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert f"SKIP (Adobe Deflate) | {PHOTO}" in r.stdout, r.stdout
        assert _sha(photo) == before

    def test_mode9_keeps_the_original_in_old_tiffs(self, tmp_path, lane):
        photo, pristine = _export(tmp_path)

        r = run_ps(lane, "compress_tiff_zip.py",
                   ["--mode", "9", "--input-dir", str(tmp_path / FOLDER), "--recompress-zip"],
                   tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        backup = photo.parent / "OLD_TIFFs" / PHOTO
        assert _sha(backup) == _sha(pristine), "the original must be kept byte for byte"
        assert photo.stat().st_size < pristine.stat().st_size
        _assert_same_image(photo, pristine)
        assert photo.stat().st_mtime_ns == OLD_NS

    def test_a_file_that_would_not_shrink_stays_and_is_remembered(self, tmp_path, lane, monkeypatch):
        photo, pristine = _export(tmp_path)
        memory = tmp_path / "memória [x] 50%" / "no gain.json"
        monkeypatch.setenv("TIFFWORKFLOW_NO_GAIN", str(memory))   # the backend inherits it
        args = ["--mode", "8", "--input-dir", str(tmp_path / FOLDER), "--staging-dir",
                str(tmp_path / "stg"), "--recompress-zip", "--recompress-min-saving", "50"]

        r = run_ps(lane, "compress_tiff_zip.py", args, tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert "SKIP (no gain: " in r.stdout and "- original kept)" in r.stdout, r.stdout
        assert _sha(photo) == _sha(pristine) and photo.stat().st_mtime_ns == OLD_NS
        assert list(json.loads(memory.read_text(encoding="utf-8"))["files"]) == [str(photo).lower()]
        assert os.listdir(tmp_path / "stg") == [], "the dropped output stayed in staging"

        r = run_ps(lane, "compress_tiff_zip.py", args, tmp_path)
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert f"- not tried again) | {PHOTO}" in r.stdout, r.stdout
        assert _sha(photo) == _sha(pristine)

    def test_a_copy_mode_writes_a_smaller_copy(self, tmp_path, lane):
        photo, pristine = _export(tmp_path)

        r = run_ps(lane, "compress_tiff_zip.py",
                   ["--mode", "3", "--input-dir", str(tmp_path / FOLDER), "--recompress-zip"],
                   tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        copy = photo.parent / "ZIP" / PHOTO
        assert copy.stat().st_size < pristine.stat().st_size
        _assert_same_image(copy, pristine)
        assert _sha(photo) == _sha(pristine), "a copy mode leaves the source alone"

    def test_the_dry_run_names_the_files_and_writes_nothing(self, tmp_path, lane):
        photo, pristine = _export(tmp_path)

        r = run_ps(lane, "compress_tiff_zip.py",
                   ["--mode", "8", "--input-dir", str(tmp_path / FOLDER), "--recompress-zip",
                    "--dry-run"], tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert f"DRY (Adobe Deflate -> ZIP, recompress) | {PHOTO}" in r.stdout, r.stdout
        assert _sha(photo) == _sha(pristine)


#: an image the ZIP predictor makes LARGER (noise on 4 levels: 17.6 % smaller without it,
#: measured) and one it makes smaller (a smooth plasma: 7.1 % smaller with it)
NOISE = ["-seed", "5", "xc:", "+noise", "Random", "-posterize", "4"]
SMOOTH = ["-seed", "7", "plasma:fractal"]


def make_uncompressed(path, build, spec):
    """An uncompressed 16-bit RGB TIFF with EXIF and a Japanese XMP title, at `path` (built in
    the plain folder `build` and moved), with the export's old date."""
    build.mkdir(parents=True)
    out = build / "u.tif"
    _magick("-size", "480x320", *spec, "-depth", "16", "-type", "TrueColor", "-compress", "none", out)
    tw.exiftool(["-overwrite_original", "-EXIF:Make=Test Cam", "-XMP-dc:Title=伊豆沼", str(out)])
    shutil.move(str(out), str(path))
    shutil.rmtree(build)
    os.utime(path, ns=(OLD_NS, OLD_NS))
    return path


def _predictor(path):
    return tw.get_recompress_facts([str(path)])[str(path).lower()].predictor


@requires_tools
@pytest.mark.parametrize("lane", LANES)
class TestZipPredictorAutoE2E:
    """--zip-predictor auto on real files: the sample decides, the file is written once, the
    choice is recorded in its History entry and read back by --recompress-zip."""

    def _scan(self, tmp_path, spec):
        photo = tmp_path / FOLDER / "scans" / PHOTO
        photo.parent.mkdir(parents=True)
        make_uncompressed(photo, tmp_path / "build", spec)
        pristine = tmp_path / "pristine.tif"
        shutil.copy2(photo, pristine)
        return photo, pristine

    def test_a_file_the_predictor_makes_larger_is_written_without_it(self, tmp_path, lane):
        photo, pristine = self._scan(tmp_path, NOISE)
        with_p = tmp_path / "with predictor.tif"
        _magick(pristine, "-type", "TrueColor", "-define", "tiff:predictor=2", "-compress", "zip", with_p)
        args = ["--mode", "8", "--input-dir", str(tmp_path / FOLDER), "--staging-dir",
                str(tmp_path / "stg"), "--zip-predictor", "auto"]

        r = run_ps(lane, "compress_tiff_zip.py", args, tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert "OK (Uncompressed -> ZIP, without predictor (sample" in r.stdout, r.stdout
        assert "ZIP predictor chosen per file: 0 with, 1 without" in r.stdout, r.stdout
        assert compression(photo).lower() == "adobe deflate"
        assert _predictor(photo) in ("1", "-")
        assert photo.stat().st_size < with_p.stat().st_size, "smaller than with the predictor"
        assert rmse_pages(photo, pristine, 0).startswith("0 (0)")
        history = exif_stdin(photo, "-s3", "-XMP-xmpMM:HistoryParameters")
        assert history.endswith("ZIP (Deflate) without predictor - zipcheck 1 predictor 1"), history
        assert photo.stat().st_mtime_ns == OLD_NS

        # a later --recompress-zip knows the file is already the smaller: its record
        before = _sha(photo)
        r = run_ps(lane, "compress_tiff_zip.py", args + ["--recompress-zip"], tmp_path)
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert f"SKIP (Adobe Deflate: already the smaller - zipcheck) | {PHOTO}" in r.stdout, r.stdout
        assert _sha(photo) == before

    def test_a_file_the_predictor_makes_smaller_keeps_it(self, tmp_path, lane):
        photo, pristine = self._scan(tmp_path, SMOOTH)

        r = run_ps(lane, "compress_tiff_zip.py",
                   ["--mode", "9", "--input-dir", str(tmp_path / FOLDER), "--zip-predictor", "auto"],
                   tmp_path)

        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert "OK (Uncompressed -> ZIP, with predictor (sample" in r.stdout, r.stdout
        assert _predictor(photo) == "2"
        assert rmse_pages(photo, pristine, 0).startswith("0 (0)")
        assert exif_stdin(photo, "-s3", "-XMP-xmpMM:HistoryParameters").endswith(
            "ZIP (Deflate) - zipcheck 1 predictor 2")
        assert _sha(photo.parent / "OLD_TIFFs" / PHOTO) == _sha(pristine)


@requires_tools
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("mode", [8, 9])
def test_an_ordinary_compression_in_place_keeps_the_date(tmp_path, lane, mode):
    """The kept date is not only --recompress-zip's: every file that takes its original's place
    keeps the original's modification time (owner's decision, 15/11/2026). Until v3.2 a TIFF16
    export compressed in place by mode 8 looked NEW to jxl-photo's smart sync, which then
    re-encoded its master and every copy made from it."""
    build = tmp_path / "build"
    build.mkdir()
    make_tiff(build / PHOTO.replace("%", "pct"))
    root = tmp_path / FOLDER
    build.rename(root)
    photo = root / PHOTO
    (root / PHOTO.replace("%", "pct")).rename(photo)
    os.utime(photo, ns=(OLD_NS, OLD_NS))

    r = run_ps(lane, "compress_tiff_zip.py",
               ["--mode", str(mode), "--input-dir", str(root), "--staging-dir", str(tmp_path / "stg")],
               tmp_path)

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert compression(photo).lower() == "adobe deflate", r.stdout
    assert photo.stat().st_mtime_ns == OLD_NS


@requires_tools
@pytest.mark.parametrize("lane", LANES)
def test_include_folders_takes_only_those_folders(tmp_path, lane):
    """The owner's preset: mode 8 over the year folders, only the TIFF16 folders of the exports.
    An ordinary (uncompressed) TIFF elsewhere in the same tree is not touched."""
    build = tmp_path / "build"
    for rel in ("s 1/_EXPORT/TIFF16", "s 1/_EXPORT/sRGB", "s 1/scans"):
        (build / rel).mkdir(parents=True)
        make_tiff(build / rel / "p.tif")
    os.utime(build / "s 1/_EXPORT/TIFF16/p.tif", ns=(OLD_NS, OLD_NS))
    root = tmp_path / FOLDER
    build.rename(root)
    others = {rel: _sha(root / rel / "p.tif") for rel in ("s 1/_EXPORT/sRGB", "s 1/scans")}

    r = run_ps(lane, "compress_tiff_zip.py",
               ["--mode", "8", "--input-dir", str(root), "--include-folders", "TIFF16",
                "--staging-dir", str(tmp_path / "stg")], tmp_path)

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    target = root / "s 1/_EXPORT/TIFF16/p.tif"
    assert compression(target).lower() == "adobe deflate", r.stdout
    assert target.stat().st_mtime_ns == OLD_NS, "an ordinary compression in place keeps the date too"
    for rel, sha in others.items():
        assert _sha(root / rel / "p.tif") == sha, f"{rel} is not inside a TIFF16 folder"
    assert "Left out 2 file(s) not inside a folder named: TIFF16" in r.stdout, r.stdout
