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
