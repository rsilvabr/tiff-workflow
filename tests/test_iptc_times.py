"""Audit round 32: the IPTC times keep the zone the photo had -- none stays none.

exiftool writes every IPTC time through IptcTime, which completes a time without a zone with the
zone of the COMPUTER running it (WriteIPTC.pl, "use local system timezone by default"). So the
metadata copy of every compress since v2.x -- and copy-exif's self-copy, and the wizard's padded
conversion -- turned a Capture One export's 15:02:12 into 15:02:12+09:00 in Japan and -03:00 in
Brazil, wherever the photo was taken. ImageMagick carries the IPTC block byte for byte; the copy
now leaves the five IPTC time tags out when the written file holds them exactly as the source.

A time without a zone cannot be written through exiftool's tags (it adds the zone): the fixture's
IPTC block is written RAW, with exiftool's `IPTC` block tag. Real files, hostile names; the MIE
container path (a '%' in the name) and the direct one.
"""

import shutil
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backends import LANES  # noqa: E402
from test_diagnose_preset import _cfg, _depth, _padded  # noqa: E402
from test_exiftool_stdin import exif_stdin  # noqa: E402
from test_regression_classes import make_tiff, requires_tools, run_ps  # noqa: E402
from tiff_workflow import common as tw  # noqa: E402
from tiff_workflow import copy_exif_to_tiff as cx  # noqa: E402
from tiff_workflow import wizard as ct  # noqa: E402

FOLDER = "写真 [x] 50% 伊豆沼"
CAPTION = "伊豆沼の夏 [FINAL] 100%"


def _record(rec, ds, data):
    return bytes([0x1C, rec, ds]) + struct.pack(">H", len(data)) + data


def iptc_block(time):
    """An IPTC block as Capture One writes it: UTF-8, the dates, the two times (`time`: b"150212"
    without a zone, b"123718+0900" with one), a Japanese caption."""
    return b"".join([
        _record(1, 90, b"\x1b%G"),                  # CodedCharacterSet: UTF-8
        _record(2, 0, b"\x00\x04"),                 # ApplicationRecordVersion
        _record(2, 55, b"20240317"),                # DateCreated
        _record(2, 60, time),                       # TimeCreated
        _record(2, 62, b"20240317"),                # DigitalCreationDate
        _record(2, 63, time),                       # DigitalCreationTime
        _record(2, 120, CAPTION.encode("utf-8")),   # Caption-Abstract
    ])


def with_raw_iptc(path, time, build):
    """`path`, an existing TIFF, given the raw IPTC block -- under a plain name in `build` (the
    fixture's exiftool takes no Japanese path on its command line), then moved back."""
    build.mkdir(parents=True, exist_ok=True)
    plain = build / "f.tif"
    shutil.move(str(path), str(plain))
    block = build / "iptc.bin"
    block.write_bytes(iptc_block(time))
    tw.exiftool(["-overwrite_original", f"-IPTC<={block}", str(plain)])
    shutil.move(str(plain), str(path))
    return path


def iptc(path):
    """[TimeCreated, DigitalCreationTime, Caption-Abstract] as exiftool reads them."""
    return exif_stdin(path, "-s3", "-IPTC:TimeCreated", "-IPTC:DigitalCreationTime",
                      "-IPTC:Caption-Abstract").splitlines()


def scan(folder, name, time, build):
    """An uncompressed TIFF at folder/name with the raw IPTC block."""
    folder.mkdir(parents=True, exist_ok=True)
    plain = build.parent / f"{build.name}_src.tif"
    make_tiff(plain)
    dst = folder / name
    shutil.move(str(plain), str(dst))
    return with_raw_iptc(dst, time, build)


ZONELESS = ["15:02:12", "15:02:12", CAPTION]
ZONED = ["12:37:18+09:00", "12:37:18+09:00", CAPTION]


@requires_tools
def test_the_fixture_holds_what_capture_one_writes(tmp_path):
    p = scan(tmp_path / FOLDER, "a.tif", b"150212", tmp_path / "b")
    assert iptc(p) == ZONELESS
    assert tw.iptc_times(str(p)) == (("DigitalCreationTime", "15:02:12"), ("TimeCreated", "15:02:12"))
    assert tw.tiff_facts(str(p)).iptc_times == tw.iptc_times(str(p))


@requires_tools
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("mode", ["8", "9"])
def test_compress_keeps_the_iptc_times_as_they_were(tmp_path, lane, mode):
    """Mode 8 stages and lands; mode 9 keeps the original in OLD_TIFFs. '0014_14 (2) 50%.tif' has a
    '%': its metadata goes through the MIE container, where exiftool already added the zone --
    the times must come from the file magick wrote, not from there."""
    folder = tmp_path / FOLDER
    pct = scan(folder, "0014_14 (2) 50%.tif", b"150212", tmp_path / "b1")
    plain = scan(folder, "plain [x].tif", b"150212", tmp_path / "b2")
    zoned = scan(folder, "zoned [z].tif", b"123718+0900", tmp_path / "b3")
    args = ["--mode", mode, "--input-dir", str(folder)]
    if mode == "8":
        args += ["--staging-dir", str(tmp_path / "stg [s]")]

    r = run_ps(lane, "compress_tiff_zip.py", args, tmp_path)

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    for p in (pct, plain, zoned):
        assert exif_stdin(p, "-s3", "-IFD0:Compression").lower() == "adobe deflate", r.stdout
    assert iptc(pct) == ZONELESS, "a time without a zone was given the computer's"
    assert iptc(plain) == ZONELESS
    assert iptc(zoned) == ZONED, "the camera's zone must stay"


@requires_tools
def test_copy_exif_leaves_the_tiffs_own_iptc_times_alone(tmp_path):
    """copy-exif copies the TIFF's own IPTC onto itself (the charset, audit round 28) before the
    camera's tags: that self-copy rewrote its IPTC times with the computer's zone. A JPEG without
    IPTC (a camera's) leaves them as they were."""
    tiff = scan(tmp_path / FOLDER, "DSCF0001 50%.tif", b"150212", tmp_path / "b")
    jpeg = tmp_path / "cam.jpg"
    tw.magick_run(["-size", "32x32", "xc:gray", str(jpeg)])
    tw.exiftool(["-overwrite_original", "-EXIF:Make=FUJIFILM", str(jpeg)])

    tw.exiftool(["-q", "-overwrite_original"] + cx._jpeg_copy_args("JPEG", str(jpeg), "", str(tiff)))

    assert iptc(tiff) == ZONELESS
    assert exif_stdin(tiff, "-s3", "-EXIF:Make") == "FUJIFILM", "the camera's tags are copied"


@requires_tools
@pytest.mark.parametrize("lane", LANES)
def test_copy_exif_compress_zip_keeps_the_iptc_times(tmp_path, lane):
    """copy-exif --compress-zip: the self-copy, then magick writes the ZIP and finish_written_tiff
    copies the metadata back -- both used to give the times the computer's zone."""
    work = tmp_path / FOLDER / "S5pro [x]"
    tiff = scan(work, "DSCF0001 50%.tif", b"150212", tmp_path / "b")
    jpeg = tmp_path / "cam.jpg"
    tw.magick_run(["-size", "64x64", "gradient:", str(jpeg)])
    tw.exiftool(["-overwrite_original", "-EXIF:Make=FUJIFILM", "-EXIF:Model=FinePix S5Pro", str(jpeg)])
    shutil.move(str(jpeg), str(work / "DSCF0001 50%.jpg"))

    r = run_ps(lane, "copy_exif_to_tiff.py", ["--input-dir", str(work), "--compress-zip",
                                              "--workers", "1"], tmp_path)

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert exif_stdin(tiff, "-s3", "-IFD0:Compression").lower() == "adobe deflate", r.stdout
    assert iptc(tiff) == ZONELESS


@requires_tools
def test_the_padded_conversion_keeps_the_iptc_times_as_they_were(tmp_path):
    """The wizard's padded conversion (Diagnose) writes an 8-bit copy with magick and copies the
    metadata onto it: the same exiftool copy, the same zone."""
    folder = tmp_path / FOLDER
    folder.mkdir()
    build = tmp_path / "pb"
    plain = _padded(tmp_path / "padded_src.tif")
    src = folder / "padded 50%.tif"
    shutil.move(str(plain), str(src))
    with_raw_iptc(src, b"150212", build)

    ct._compress_padded_files([src], tmp_path / "staging", workers=1, cfg=_cfg())

    assert _depth(src) == "8", "not converted"
    assert iptc(src) == ZONELESS
