"""v3.1.1, the I/O round (AI_tools/261114_Claude_report_io-optimizations.md): what a file
costs in writes.

exiftool rewrites the WHOLE file for every command that writes to it. The writes after magick
(the metadata copy, the page markers, the xmpMM:History entry) were three commands until
v3.1.1-rc1: a 215 MB ZIP of a 45 MP scan was written four times, 861 MB on the staging disk.
They are one command now, and the file they leave is the same, byte for byte.

Real ImageMagick and exiftool. The fixtures are built under a plain name and moved into a
hostile folder (Japanese, [ ], a space; a '%' in the variant that goes through the metadata
container).
"""

import datetime as _dt
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tiff_workflow import common as tw  # noqa: E402
from test_exiftool_stdin import exif_stdin  # noqa: E402
from test_regression_classes import make_multipage_tiff, requires_tools  # noqa: E402

FOLDERS = {"direct": "190815_Film_伊豆沼 [FINAL] x", "container": "190815_Film_伊豆沼 [FINAL] 50% x"}
FROZEN = _dt.datetime(2026, 11, 14, 12, 0, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=9)))
EARLIER = ["{Action=saved,When=2025-01-18T10:00:00+09:00,SoftwareAgent=Capture One 16}",
           "{Action=derived,When=2025-01-19T11:00:00+09:00,SoftwareAgent=Lightroom Classic 14}"]


class _Frozen(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is None else FROZEN.astimezone(tz)


def _write_tags(path, assignments):
    out = tw.exiftool(["-q", "-q", "-overwrite_original"] + assignments + [str(path)], check=False)
    assert "rror" not in out, out


def _history(path):
    rows = json.loads(exif_stdin(path, "-j", "-struct", "-XMP-xmpMM:History") or "[{}]")
    return rows[0].get("History") or []


def _source(tmp_path, variant):
    """A scanner-like 2-page TIFF (image + reduced preview) with EXIF, two earlier History
    entries and a UTF-8 IPTC keyword, in the hostile folder; the name has a '%' in the
    container variant (exiftool cannot take it as -tagsfromfile source)."""
    plain = tmp_path / "plain"
    plain.mkdir()
    built = make_multipage_tiff(plain / "scan.tif", [None, 1])
    _write_tags(built, ["-EXIF:Make=FUJIFILM", "-EXIF:Artist=写真家",
                        "-IPTC:CodedCharacterSet=UTF8", "-IPTC:Keywords=伊豆沼"]
                + [f"-XMP-xmpMM:History+={e}" for e in EARLIER])
    folder = tmp_path / FOLDERS[variant]
    folder.mkdir()
    src = folder / ("0014_14 (2) 50%off.tif" if variant == "container" else "0014_14 (2).tif")
    built.rename(src)
    return src


@requires_tools
@pytest.mark.parametrize("variant", ["direct", "container"])
def test_the_writes_after_magick_rewrite_the_file_once(tmp_path, monkeypatch, variant):
    src = _source(tmp_path, variant)
    dst0 = src.parent / "out [zip].tif"
    tw.magick_run(["-quiet", str(src), "-compress", "zip", tw.magick_path_spec(str(dst0))])
    new, ref = src.parent / "new.tif", src.parent / "ref.tif"
    shutil.copy2(dst0, new)
    shutil.copy2(dst0, ref)
    monkeypatch.setattr(tw, "datetime", _Frozen)
    facts = tw.tiff_facts(str(src))
    markers = tw.subfile_marker_args(str(src), facts.subfile_types)
    assert markers == ["-IFD0:SubfileType=", "-IFD1:SubfileType#=1"], "the fixture itself"
    tag_source = tw.backup_tiff_metadata(str(src), str(new)) or str(src)
    assert (tag_source != str(src)) == (variant == "container"), "the route under test"

    real = tw.exiftool_commands
    sent = []

    def recording(commands):
        sent.extend(commands)
        return real(commands)
    monkeypatch.setattr(tw, "exiftool_commands", recording)
    done = tw.finish_written_tiff(str(new), tag_source, markers, "io test")
    rewrites = [c for c in sent if "-overwrite_original" in c]
    assert len(rewrites) == 1, (
        f"the metadata, the markers and the History in ONE command, one rewrite of the "
        f"file: {len(rewrites)} rewrites")
    assert done == tw.Finished(True, True, True)

    hist = _history(new)
    assert [h.get("SoftwareAgent") for h in hist[:-1]] == ["Capture One 16", "Lightroom Classic 14"], \
        f"the earlier entries once each, in their order (jxl-photo #531 doubled them): {hist}"
    assert hist[-1].get("Parameters") == "io test" and hist[-1].get("Action") == "converted"
    marks = [m.strip() for m in exif_stdin(new, "-a", "-G1", "-s", "-s", "-n",
                                           "-SubfileType").splitlines()]
    assert marks == ["[IFD1] SubfileType: 1"], \
        "magick's PAGE stamp gone from the image, the preview marked as one"
    assert exif_stdin(new, "-s3", "-IPTC:Keywords") == "伊豆沼"
    assert exif_stdin(new, "-s3", "-EXIF:Artist") == "写真家"

    # The bytes are the ones the three commands of v3.1.1-rc1 left, each run in an exiftool
    # of its own (as v3.0.0 ran them).
    monkeypatch.setattr(tw, "exiftool_commands", real)
    when = FROZEN.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    entry = (f"{{Action=converted,When={when},SoftwareAgent={tw.history_agent()},"
             f"Parameters=io test}}")
    for cmd in (tw._restore_metadata_args(tag_source, str(ref)),
                ["-q", "-q", "-overwrite_original"] + markers + [str(ref)],
                ["-q", "-overwrite_original", f"-XMP-xmpMM:History+={entry}", str(ref)]):
        assert real([cmd])[0].status == 0
    tw.remove_quietly(None if tag_source == str(src) else tag_source)
    assert new.read_bytes() == ref.read_bytes(), "the same file as the three rewrites left"


class _NoLog:
    path = ""

    def __call__(self, msg="", level="INFO"):
        pass


@requires_tools
def test_a_thumbnail_output_compresses_the_main_image_once(tmp_path, monkeypatch):
    """--generate-thumbnail: the thumbnail first, then ONE magick reads page 0 of the source and
    the thumbnail and writes the ZIP, and the page markers go into the one exiftool rewrite.
    Until v3.1.1-rc1 page 0 was compressed into a temp TIFF, read back and compressed again
    with the thumbnail (two deflates of the main image, 26 s against 13 for a 45 MP export),
    and the markers were a read and a rewrite of their own."""
    from tiff_workflow import compress_tiff_zip as cz
    src = _source(tmp_path, "direct")
    out = src.parent / "ZIP [x]" / src.name
    out.parent.mkdir()
    opt, _bound = tw.parse_args(["--mode", "1", "--generate-thumbnail", "--thumb-size", "16"],
                                cz.PARAMS)
    run = cz.Run(opt, _NoLog(), [])
    run.srgb_profile = tw.resolve_srgb_profile("")
    calls, writes = [], []
    real_magick, real_exif, real_cmds = tw.magick_run, tw.exiftool, tw.exiftool_commands

    def magick(args):
        calls.append([str(a) for a in args])
        return real_magick(args)

    def exif(args, *a, **k):
        if "-overwrite_original" in [str(x) for x in args]:
            writes.append(args)
        return real_exif(args, *a, **k)

    def cmds(commands):
        writes.extend(c for c in commands if "-overwrite_original" in c)
        return real_cmds(commands)
    monkeypatch.setattr(tw, "magick_run", magick)
    monkeypatch.setattr(tw, "exiftool", exif)
    monkeypatch.setattr(tw, "exiftool_commands", cmds)
    r = cz.process_job(run, cz.Job(src=str(src), write_dst=str(out), final_dst=str(out),
                                   name=src.name), 1.0)
    assert r.result == f"OK (Uncompressed -> ZIP) [thumb replaced] | {src.name}", r.result
    deflates = [c for c in calls if "-compress" in c]
    assert len(deflates) == 1, f"the main image compressed once, not {len(deflates)} times"
    assert deflates[0][1] == f"{tw.magick_path_spec(str(src))}[0]", "read from the source"
    assert len(writes) == 1, f"one exiftool rewrite of the output, not {len(writes)}"
    assert tw.thumb_output_matches_source(str(src), str(out), 2), "page 0 = the source's"
    marks = [m.strip() for m in exif_stdin(out, "-a", "-G1", "-s", "-s", "-n",
                                           "-SubfileType").splitlines()]
    assert marks == ["[IFD1] SubfileType: 1"], "the new thumbnail marked, the image unmarked"
    hist = _history(out)
    assert len(hist) == 3 and hist[-1].get("Parameters") == \
        "compress_tiff_zip mode 1: ZIP (Deflate) + thumbnail page"
    assert exif_stdin(out, "-s3", "-IPTC:Keywords") == "伊豆沼"

class TestPageClassesWithPing:
    """v3.1.1: the page classifiers read each page's marker and size from its directory
    (`identify -ping`), as tiff_page_count does since v3.1. The full identify decoded every
    page -- 0.3 s for a 45 MP Capture One export, multi-page by its thumbnail -- for the same
    answers (95 real TIFFs read both ways, AI_tools/261114_Claude_report_io-optimizations.md)."""

    def test_what_is_sent_to_magick(self, monkeypatch):
        sent = []

        def fake(args, timeout=30):
            sent.append(([str(a) for a in args], timeout))
            geometry = "|%w|%h" in str(args[3])
            return tw.MagickResult(False, 0, ["|64|48", "REDUCEDIMAGE|16|12"] if geometry
                                   else ["", "REDUCEDIMAGE"])
        monkeypatch.setattr(tw, "magick_probe", fake)
        p = r"E:\伊豆沼 [x]\50%off.tif"
        assert tw.tiff_has_only_subfile_pages(p, 2, tw.THUMBNAIL_PAGE_TYPES, 45) is True
        assert tw.tiff_has_thumbnail_page(p, 45) is True
        assert sent == [(["identify", "-ping", "-format", "%[tiff:subfiletype]|%w|%h\n", p], 45),
                        (["identify", "-ping", "-format", "%[tiff:subfiletype]\\n", p], 45)]

    @requires_tools
    @pytest.mark.parametrize("subfiletypes, thumbs_only, has_thumb", [
        ([None, 1], True, True),            # image + reduced preview
        ([None, 1, 4], False, True),        # scanner: image, preview, IR mask
        ([None, None, None], False, False), # three full pages, no markers
    ], ids=["preview", "scanner-ir", "three-unmarked"])
    def test_the_layouts_the_gates_meet(self, tmp_path, subfiletypes, thumbs_only, has_thumb):
        plain = tmp_path / "plain"
        plain.mkdir()
        built = make_multipage_tiff(plain / "scan.tif", subfiletypes)
        folder = tmp_path / FOLDERS["container"]
        folder.mkdir()
        photo = folder / "0014_14 (2) 50%off.tif"
        built.rename(photo)
        n = len(subfiletypes)
        assert tw.tiff_has_only_subfile_pages(str(photo), n, tw.THUMBNAIL_PAGE_TYPES) is thumbs_only
        assert tw.tiff_has_thumbnail_page(str(photo)) is has_thumb

    @requires_tools
    def test_damaged_pixels_do_not_make_a_scan_multi_page(self, tmp_path):
        """Damaged pixel data on page 0, directories intact: the pages are still an image and
        its preview. The full identify failed on the pixels and safe mode skipped the file as
        MULTI (2 pages) -- a wrong reason; the conversion now meets the damage and says so."""
        plain = tmp_path / "plain"
        plain.mkdir()
        built = make_multipage_tiff(plain / "scan.tif", [None, 1], sizes=["600x400", "60x40"])
        zipped = plain / "zip.tif"
        import subprocess
        subprocess.run(["magick", str(built), "-compress", "zip", str(zipped)],
                       capture_output=True, check=True, timeout=120)
        _write_tags(zipped, ["-IFD0:SubfileType=", "-IFD1:SubfileType#=1"])
        data = bytearray(zipped.read_bytes())
        for i in range(len(data) // 3, len(data) // 3 + 20000):
            data[i] = 0x55
        zipped.write_bytes(bytes(data))
        full = subprocess.run(["magick", "identify", "-format", "%[tiff:subfiletype]|%w|%h\n",
                               str(zipped)], capture_output=True, timeout=120)
        assert full.returncode != 0, "the fixture: the full identify fails on the pixels"
        folder = tmp_path / FOLDERS["direct"]
        folder.mkdir()
        photo = folder / "0014_14 (2).tif"
        zipped.rename(photo)
        assert tw.tiff_has_only_subfile_pages(str(photo), 2, tw.THUMBNAIL_PAGE_TYPES) is True
        assert tw.tiff_has_thumbnail_page(str(photo)) is True
        assert not tw.zip_integrity(str(photo)), "the damage is the integrity gate's to see"
