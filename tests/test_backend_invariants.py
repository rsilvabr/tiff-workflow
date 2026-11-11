"""Invariants of the Python backends, without ImageMagick or exiftool.

Until v2.9.1 the backends were PowerShell and many of their rules could only be pinned by
matching the SHAPE of the source ("this line appears three times, once per worker path").
The Python backends are importable, so the same rules are pinned here by RUNNING the code
with the tool calls replaced: which gate runs before which irreversible step, what a failed
gate leaves on disk, what is sent to exiftool and magick. Each test names the audit round
its rule comes from (docs/bugs_fixed.md).

The end-to-end tests with the real tools are in the other files; these are the ones that
fail in a second when a gate is removed or reordered.
"""

import ast
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import compress_tiff_zip as cz  # noqa: E402
import copy_exif_to_tiff as cx  # noqa: E402
import generate_thumbnails as gt  # noqa: E402
import tiff_workflow_common as tw  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BACKEND_FILES = ["compress_tiff_zip.py", "copy_exif_to_tiff.py", "generate_thumbnails.py"]
ALL_FILES = BACKEND_FILES + ["tiff_workflow_common.py"]
RGB = ["rgb/3/8,8,8/"]


# ---------------------------------------------------------------------------
# A stand-in for the tools
# ---------------------------------------------------------------------------

class FakeTools:
    """Replaces every tool call of tiff_workflow_common and records the order of the steps.
    Defaults describe a healthy single-page uncompressed RGB TIFF; a test overrides what it
    wants to fail."""

    def __init__(self, monkeypatch):
        self.events = []
        self.magick_calls = []
        self.exif_calls = []
        self.comp = "Uncompressed"
        self.pages = 1
        self.only_subfile = True
        self.layout = (True, list(RGB))
        self.layout_kept = True
        self.alpha_ok = True
        self.meta = "auto"            # "auto" = what backup_tiff_metadata would decide
        self.magick_rc = 0
        self.magick_writes = True
        # one entry per magick call, in order, for a test that needs the calls to differ:
        # "ok", "fail" (exit 1, nothing written), "nofile" (exit 0, nothing written) or an
        # exception to raise. When it runs out, magick_rc / magick_writes answer.
        self.magick_plan = []
        self.thumb_markers = True
        self.exif_result = (0, "")
        self.subfile_restore = True
        self.history = True
        self.integrity = True
        self.pixels = True
        self.thumb_match = True
        self.thumb_safe = tw.ThumbSafe(True, 1, "")
        self.allowed_seen = []
        m = monkeypatch
        m.setattr(tw, "ifd0_compression", lambda p: (True, self.comp))
        m.setattr(tw, "tiff_page_count", lambda p, t=30: tw.PageCount(True, self.pages, ""))
        m.setattr(tw, "tiff_has_only_subfile_pages", self._only_subfile)
        m.setattr(tw, "tiff_has_thumbnail_page", lambda p, t=30: False)
        m.setattr(tw, "tiff_sample_layout", lambda p: self.layout)
        m.setattr(tw, "tiff_sample_layout_kept", self._layout_kept)
        m.setattr(tw, "associated_alpha_rewritable", lambda p, pages, t=30: self.alpha_ok)
        m.setattr(tw, "backup_tiff_metadata", self._backup)
        m.setattr(tw, "magick_run", self._magick_run)
        m.setattr(tw, "exiftool", self._exiftool)
        m.setattr(tw, "restore_tiff_subfile_types", self._subfile)
        m.setattr(tw, "set_tiff_thumb_markers", lambda s, d: self.thumb_markers)
        m.setattr(tw, "add_tiff_history_entry", self._history)
        m.setattr(tw, "zip_integrity", self._integrity)
        m.setattr(tw, "pixel_identical", self._pixels)
        m.setattr(tw, "thumb_output_matches_source", self._thumb_match)
        m.setattr(tw, "thumbnail_safe_source", lambda p, t=30: self.thumb_safe)
        real_replace = tw.move_file_replacing

        def replacing(src, dst):
            self.events.append("replace")
            return real_replace(src, dst)
        m.setattr(tw, "move_file_replacing", replacing)

    def _only_subfile(self, path, count, allowed=("REDUCEDIMAGE", "REDUCED", "MASK", "PAGE"), timeout=30):
        self.allowed_seen.append(tuple(allowed))
        return self.only_subfile

    def _layout_kept(self, pages, dst, main_page_only=False):
        self.events.append("layout")
        return self.layout_kept

    def _backup(self, src, write_dst):
        if self.meta == "auto":
            needed = tw.same_path(write_dst, src) or "%" in str(src)
            return (True, None) if False else ((True, str(src) + ".meta") if needed else (False, None))
        return self.meta

    def _magick_run(self, args):
        self.events.append("magick")
        self.magick_calls.append([str(a) for a in args])
        if self.magick_plan:
            step = self.magick_plan.pop(0)
            if isinstance(step, BaseException):
                raise step
            rc, writes = {"ok": (0, True), "fail": (1, False), "nofile": (0, False)}[step]
        else:
            rc, writes = self.magick_rc, self.magick_writes
        return self._magick_write(args, rc, writes)

    def _magick_write(self, args, rc, writes):
        if rc == 0 and writes:
            out = str(args[-1]).replace("%%", "%")
            if ":" in out[:8] and not out[1:3] == ":\\":
                out = out.split(":", 1)[1]
            with open(out, "wb") as fh:
                fh.write(b"COMPRESSED")
        return rc, "magick: boom" if rc else ""

    def _exiftool(self, args, merge_stderr=False):
        self.events.append("exif")
        self.exif_calls.append([str(a) for a in args])
        return self.exif_result

    def _subfile(self, src, dst, timeout=30):
        self.events.append("subfile")
        return self.subfile_restore

    def _history(self, path, agent, params):
        self.events.append("history")
        self.history_params = params
        return self.history

    def _integrity(self, path, timeout=30, load_factor=1.0):
        self.events.append("integrity")
        return self.integrity

    def _pixels(self, src, dst, timeout=30, load_factor=1.0):
        self.events.append("pixels")
        return self.pixels

    def _thumb_match(self, src, dst, expected, timeout=30):
        self.events.append("thumb_match")
        self.thumb_expected = expected
        return self.thumb_match


class NoLog:
    path = ""

    def __init__(self):
        self.lines = []

    def __call__(self, msg="", level="INFO"):
        self.lines.append((level, msg))


#: The mode of a job whose final_dst is its own source: mode 2 with the output on the
#: input root writes a file of that folder in place (a temp sibling, the gates, then the
#: rename).
IN_PLACE = ("-Mode", "2")


def compress_run(*argv):
    opt, bound = tw.parse_args(list(argv), cz.PARAMS)
    assert "Mode" in bound, "a compress run has a mode: run_main refuses one without"
    return cz.Run(opt, NoLog(), [])


def job(run, src, write_dst=None, final_dst=None, **extra):
    t = cz.Job(src=str(src), write_dst=str(write_dst or src), final_dst=str(final_dst or src),
               name=Path(src).name, **extra)
    return cz.process_job(run, t, 1.0)


@pytest.fixture
def tools(monkeypatch):
    return FakeTools(monkeypatch)


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "伊豆沼 [FINAL] x" / "photo 50%off.tif"
    p.parent.mkdir()
    p.write_bytes(b"ORIGINAL")
    return p


def leftovers(folder):
    return sorted(n for n in os.listdir(folder) if tw.LEFTOVER_NAME_RE.search(n))


# ---------------------------------------------------------------------------
# compress: a write onto its own source (mode 2 on the input root, mode 5 in ZIP\)
# ---------------------------------------------------------------------------

class TestInPlaceReplace:
    """An in-place write goes over the only copy of the image. It is written to a temp
    sibling, proven (metadata restored, decodes, pixel-identical) and only then renamed onto
    the original -- in every mode that can resolve final_dst == src (rounds 15, 28)."""

    @pytest.mark.parametrize("argv", [["-Mode", "2"], ["-Mode", "5"]], ids=["mode2", "mode5"])
    def test_the_original_is_replaced_only_after_every_gate(self, tools, photo, argv):
        r = job(compress_run(*argv), photo)
        assert r.result.startswith("OK (Uncompressed -> ZIP)"), r.result
        assert photo.read_bytes() == b"COMPRESSED"
        assert leftovers(photo.parent) == []
        ev = tools.events
        assert ev.index("exif") < ev.index("integrity") < ev.index("pixels") < ev.index("replace"), ev
        assert ev.index("history") < ev.index("integrity"), "the gates must check the final bytes"

    @pytest.mark.parametrize("failing,why", [
        ("pixels", "in-place verification failed"),
        ("integrity", "in-place verification failed"),
        ("exif", "metadata restore failed"),
    ])
    def test_a_failed_gate_leaves_the_original_untouched(self, tools, photo, failing, why):
        if failing == "exif":
            tools.exif_result = (0, "Warning: Error opening file - x.mie")
        else:
            setattr(tools, failing, False)
        r = job(compress_run(*IN_PLACE), photo)
        assert r.result == f"ERROR ({why} - original untouched) | {photo.name}"
        assert photo.read_bytes() == b"ORIGINAL"
        assert leftovers(photo.parent) == [], "the dropped temp sibling stayed behind"
        assert "replace" not in tools.events

    def test_a_nonzero_exiftool_exit_is_a_failed_restore_too(self, tools, photo):
        tools.exif_result = (1, "")
        r = job(compress_run(*IN_PLACE), photo)
        assert "metadata restore failed - original untouched" in r.result
        assert photo.read_bytes() == b"ORIGINAL"

    def test_no_writable_tags_is_not_a_failure(self, tools, photo):
        """A tag-less source legitimately reports this; only 'Error opening file' means the
        SOURCE was never read."""
        tools.exif_result = (0, "Warning: No writable tags set from x.tif")
        r = job(compress_run(*IN_PLACE), photo)
        assert r.result.startswith("OK "), r.result

    def test_staged_for_its_own_place_is_verified_like_an_in_place_write(self, tools, photo, tmp_path):
        """Round 28: with -StagingDir the output is not a temp sibling, and the gate keyed on
        the temp sibling alone -- an unverified file was moved over the only copy."""
        stg = tmp_path / "stg"
        stg.mkdir()
        tools.pixels = False
        r = job(compress_run("-Mode", "2"), photo, write_dst=stg / "abcd1234_0123456789ab_photo.tif",
                final_dst=photo)
        assert r.result.startswith("ERROR (in-place verification failed"), r.result
        assert r.staging_name is None, "a refused file must never reach the staging move"
        assert os.listdir(stg) == []

    def test_the_metadata_is_parked_first_and_a_failed_container_refuses_the_write(self, tools, photo):
        """In place the original is gone before the restore runs (round 2); without the
        container the write would strip every tag: refuse it, fail closed."""
        tools.meta = (True, None)
        r = job(compress_run(*IN_PLACE), photo)
        assert r.result == f"ERROR (metadata container failed - write refused) | {photo.name}"
        assert tools.magick_calls == [], "magick must not run without the container"
        assert photo.read_bytes() == b"ORIGINAL"

    def test_the_restore_reads_the_container_not_the_overwritten_source(self, tools, photo):
        job(compress_run(*IN_PLACE), photo)
        copy = next(c for c in tools.exif_calls if "-tagsfromfile" in c)
        assert copy[copy.index("-tagsfromfile") + 1] == str(photo) + ".meta"
        assert "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet" in copy, "round 28: UTF-8 IPTC"
        assert copy.count("-q") == 1, "a second -q hides the unreadable-source warning"


class TestMagickArguments:
    def test_input_literal_output_escaped(self, tools, photo, tmp_path):
        """'%' is expanded in every OUTPUT name; a plain input is opened literally (round 13)."""
        out = tmp_path / "out 100%dpi" / "photo 50%off.tif"
        out.parent.mkdir()
        job(compress_run("-Mode", "1"), photo, write_dst=out, final_dst=out)
        call = tools.magick_calls[0]
        assert call[1] == str(photo), "the input must stay unescaped"
        assert call[-1] == str(out).replace("%", "%%")
        assert call[call.index("-compress") + 1] == "zip"

    def test_the_source_layout_picks_the_type(self, tools, photo):
        """Round 28: magick writes a neutral RGB file as 1-channel gray unless told."""
        job(compress_run(*IN_PLACE), photo)
        call = tools.magick_calls[0]
        assert call[call.index("-type") + 1] == "TrueColor"

    def test_exit_zero_without_a_file_is_an_error(self, tools, photo, tmp_path):
        out = tmp_path / "o.tif"
        tools.magick_writes = False
        r = job(compress_run("-Mode", "1"), photo, write_dst=out, final_dst=out)
        assert r.result.startswith("ERROR (magick reported success but wrote no file)"), r.result

    def test_a_failed_conversion_drops_its_partial_output(self, tools, photo, tmp_path):
        out = tmp_path / "o.tif"
        out.write_bytes(b"PARTIAL")
        tools.magick_rc = 1
        r = job(compress_run("-Mode", "1", "-Overwrite"), photo, write_dst=out, final_dst=out)
        assert r.result.startswith("ERROR (magick) |"), r.result
        assert not out.exists()

    def test_a_changed_layout_drops_the_output(self, tools, photo, tmp_path):
        out = tmp_path / "o.tif"
        tools.layout_kept = False
        r = job(compress_run("-Mode", "1"), photo, write_dst=out, final_dst=out)
        assert r.result.startswith("ERROR (sample layout changed: rgb/3/8,8,8/ - output dropped"), r.result
        assert not out.exists()
        assert photo.read_bytes() == b"ORIGINAL"

    def test_unreadable_layout_and_non_opaque_associated_alpha_are_refused(self, tools, photo):
        tools.layout = (False, [])
        assert job(compress_run(*IN_PLACE), photo).result.startswith("ERROR (sample layout unreadable)")
        tools.layout = (True, ["rgb/4/8,8,8,8/1"])
        tools.alpha_ok = False
        assert "associated alpha that is not opaque" in job(compress_run(*IN_PLACE), photo).result
        assert tools.magick_calls == []


class TestSafeModeAndSkips:
    def test_safe_mode_passes_the_restricted_list(self, tools, photo):
        """MASK and PAGE are real pages: with the helper's default list a scanner RGB+IR
        file was accepted and rewritten (round 9)."""
        tools.pages = 3
        tools.only_subfile = False
        r = job(compress_run("-Mode", "1"), photo, final_dst=photo.with_name("o.tif"))
        assert r.result == f"MULTI (3 pages - skipped) | {photo.name}"
        assert r.multi == str(photo)
        assert tools.allowed_seen == [("REDUCEDIMAGE", "REDUCED")]
        assert tools.magick_calls == []

    def test_already_compressed_is_skipped_on_the_main_image(self, tools, photo):
        tools.comp = "Adobe Deflate"
        assert job(compress_run(*IN_PLACE), photo).result == f"SKIP (Adobe Deflate) | {photo.name}"
        tools.comp = "LZW"
        assert job(compress_run(*IN_PLACE), photo).result.startswith("OK (LZW -> ZIP)")
        assert job(compress_run(*IN_PLACE, "-SkipLzwAsCompressed:$true"), photo).result.startswith("SKIP (")

    def test_a_refused_thumbnail_is_reported_by_a_dry_run_too(self, tools, photo):
        tools.thumb_safe = tw.ThumbSafe(False, 3, "would drop 2 extra page(s)")
        r = job(compress_run(*IN_PLACE, "-GenerateThumbnail", "-DryRun"), photo)
        assert r.result.startswith("ERROR (-GenerateThumbnail refused)"), r.result

    def test_dry_run_writes_nothing(self, tools, photo):
        r = job(compress_run(*IN_PLACE, "-DryRun"), photo)
        assert r.result == f"DRY (Uncompressed -> ZIP) | {photo.name}"
        assert tools.magick_calls == [] and tools.exif_calls == []


class TestGatesPerMode:
    def test_mode8_needs_pixels_and_metadata_before_it_may_delete(self, tools, photo, tmp_path):
        """Round 12: a decode check cannot see a missing page. Round 28: nor a lost EXIF."""
        stg = tmp_path / "stg"
        stg.mkdir()
        run = compress_run("-Mode", "8", "-DeleteSource")
        ok = job(run, photo, write_dst=stg / "a.tif")
        assert ok.can_delete is True and "[SOURCE DELETED]" in ok.result
        assert not ok.integrity_failed
        tools.pixels = False
        bad = job(run, photo, write_dst=stg / "b.tif")
        assert bad.integrity_failed is True and bad.can_delete is False
        tools.pixels = True
        tools.exif_result = (0, "Error opening file")
        noexif = job(run, photo, write_dst=stg / "c.tif")
        assert noexif.integrity_failed and noexif.integrity_reason == "metadata restore failed"
        assert noexif.can_delete is False

    def test_mode8_without_delete_source_never_asks_for_a_delete(self, tools, photo, tmp_path):
        r = job(compress_run("-Mode", "8"), photo, write_dst=tmp_path / "a.tif")
        assert r.can_delete is False and "[SOURCE DELETED]" not in r.result

    def test_mode8_thumbnail_output_is_checked_on_page_zero(self, tools, photo, tmp_path):
        """Round 15: whole-file identity refused EVERY -GenerateThumbnail file in mode 8."""
        r = job(compress_run("-Mode", "8", "-GenerateThumbnail"), photo, write_dst=tmp_path / "a.tif")
        assert "thumb_match" in tools.events and "pixels" not in tools.events
        assert tools.thumb_expected == 2
        assert not r.integrity_failed
        assert tools.history_params.endswith("+ thumbnail page")

    @pytest.mark.parametrize("mode", ["0", "9"])
    def test_modes_0_and_9_decode_check_the_copy_before_the_original_is_archived(
            self, tools, photo, mode):
        """Round 26: a copy that does not decode must never cause the archive."""
        tools.integrity = False
        sibling = str(photo) + ".abcd1234_0123456789ab.tif"
        r = job(compress_run("-Mode", mode), photo, write_dst=sibling, keep_dir=str(photo.parent / "OLD_TIFFs"))
        assert r.integrity_failed is True
        assert photo.read_bytes() == b"ORIGINAL", "the worker never touches the original in modes 0/9"

    @pytest.mark.parametrize("mode", [str(m) for m in range(1, 8)])
    def test_modes_1_to_7_refuse_a_page_count_shrink(self, tools, photo, tmp_path, monkeypatch, mode):
        """Round 16: these modes have no pixel gate, and a dropped page reported OK."""
        counts = {str(photo): 2}
        monkeypatch.setattr(tw, "tiff_page_count",
                            lambda p, t=30: tw.PageCount(True, counts.get(str(p), 1), ""))
        monkeypatch.setattr(tw, "tiff_has_only_subfile_pages", lambda *a, **k: True)
        out = tmp_path / "o.tif"
        r = job(compress_run("-Mode", mode), photo, write_dst=out, final_dst=out)
        assert r.result == f"ERROR (page count shrink) | {photo.name} | 2 -> 1 pages"
        assert not out.exists()

    def test_a_degraded_outcome_is_folded_into_the_result_line(self, tools, photo, tmp_path):
        """One line per file, counted: a failed marker restore was once a bare WARN on one
        path and a clean OK on the other (round 9)."""
        out = tmp_path / "o.tif"
        tools.subfile_restore = False
        tools.history = False
        r = job(compress_run("-Mode", "1"), photo, write_dst=out, final_dst=out)
        assert r.result == (f"WARN (exiftool failed, ZIP ok) [subfiletype restore failed]"
                               f" [history not written] | {photo.name}")
        assert r.staging_name == "o.tif"

    def test_history_names_the_mode(self, tools, photo, tmp_path):
        job(compress_run("-Mode", "9"), photo, write_dst=str(photo) + ".abcd1234_0123456789ab.tif")
        assert tools.history_params == "compress_tiff_zip mode 9: ZIP (Deflate)"
        job(compress_run(*IN_PLACE), photo)
        assert tools.history_params == "compress_tiff_zip mode 2: ZIP (Deflate)"


class TestThumbnailWrite:
    """-GenerateThumbnail is three magick calls -- the main image, the thumbnail, the two
    together -- in _write_with_thumbnail, which hands process_job a ThumbWrite. What
    process_job does with each of its fields: the [no thumb] fallback and the page count the
    gate then expects, the marker warning, and what a failure leaves behind (nothing: not
    the partial output, not the metadata container, not the two temp files)."""

    @pytest.fixture
    def tmp(self, tmp_path, monkeypatch):
        """The folder of _write_with_thumbnail's two temp files."""
        folder = tmp_path / "tmp 一時 [x]"
        folder.mkdir()
        counter = iter(range(100))
        monkeypatch.setattr(tw, "temp_file_path",
                            lambda suffix: str(folder / f"tw_{next(counter):016x}{suffix}"))
        return folder

    def in_place(self, tools, photo, *plan):
        """An in-place run (the output replaces the photo); the metadata container that
        FakeTools names exists, so a test can see whether it was removed."""
        tools.magick_plan = list(plan)
        self.container = Path(str(photo) + ".meta")
        self.container.write_bytes(b"MIE")
        return job(compress_run(*IN_PLACE, "-GenerateThumbnail"), photo)

    def nothing_left(self, photo, tmp):
        return (leftovers(photo.parent) == [] and not self.container.exists()
                and os.listdir(tmp) == [])

    def test_the_thumbnail_is_written_and_two_pages_are_expected(self, tools, photo, tmp):
        r = self.in_place(tools, photo)
        assert r.result == f"OK (Uncompressed -> ZIP) | {photo.name}"
        assert len(tools.magick_calls) == 3 and tools.thumb_expected == 2
        assert tools.history_params == "compress_tiff_zip mode 2: ZIP (Deflate) + thumbnail page"
        assert photo.read_bytes() == b"COMPRESSED" and self.nothing_left(photo, tmp)

    @pytest.mark.parametrize("plan", [("ok", "fail"), ("ok", "nofile"), ("ok", "ok", "fail")],
                             ids=["thumbnail-fails", "thumbnail-not-written", "joining-fails"])
    def test_a_failed_thumbnail_falls_back_to_the_main_image_alone(self, tools, photo, tmp, plan):
        """The fallback writes ONE page: the gate must expect one, or it refuses a good file
        (and with two expected it would accept a file that lost its thumbnail)."""
        r = self.in_place(tools, photo, *plan)
        assert r.result == f"OK (Uncompressed -> ZIP) [no thumb] | {photo.name}"
        assert tools.thumb_expected == 1
        assert tools.history_params == "compress_tiff_zip mode 2: ZIP (Deflate)", \
            "the history must not claim a thumbnail page"
        assert photo.read_bytes() == b"COMPRESSED" and self.nothing_left(photo, tmp)

    def test_a_failed_page_marker_is_a_warning_on_the_result_line(self, tools, photo, tmp):
        tools.thumb_markers = False
        r = self.in_place(tools, photo)
        assert r.result == f"WARN (exiftool failed, ZIP ok) [thumb marker failed] | {photo.name}"
        assert photo.read_bytes() == b"COMPRESSED" and self.nothing_left(photo, tmp)

    def test_a_failed_main_image_leaves_nothing(self, tools, photo, tmp):
        r = self.in_place(tools, photo, "fail")
        assert r.result == f"ERROR (magick compress) | {photo.name} | magick: boom"
        assert r.final_dst is None
        assert photo.read_bytes() == b"ORIGINAL" and self.nothing_left(photo, tmp)
        assert "replace" not in tools.events and "exif" not in tools.events

    def test_an_os_error_drops_the_partial_output(self, tools, photo, tmp, monkeypatch):
        """A full disk during the fallback copy: the half-written temp sibling must go, or
        the next run finds a leftover -- and in a copy mode a file that says "exists"."""
        def full_disk(src, dst):
            with open(dst, "wb") as fh:
                fh.write(b"HALF")
            raise OSError("no space left on device")
        monkeypatch.setattr(cz.shutil, "copyfile", full_disk)
        r = self.in_place(tools, photo, "ok", "fail")
        assert r.result == f"ERROR (magick compress) | {photo.name} | no space left on device"
        assert r.final_dst is None
        assert photo.read_bytes() == b"ORIGINAL" and self.nothing_left(photo, tmp)

    def test_an_os_error_drops_the_partial_output_of_a_copy_mode(self, tools, photo, tmp, tmp_path,
                                                                 monkeypatch):
        def full_disk(src, dst):
            with open(dst, "wb") as fh:
                fh.write(b"HALF")
            raise OSError("no space left on device")
        monkeypatch.setattr(cz.shutil, "copyfile", full_disk)
        tools.magick_plan = ["ok", "fail"]
        out = tmp_path / "out [x]" / photo.name
        out.parent.mkdir()
        r = job(compress_run("-Mode", "1", "-GenerateThumbnail"), photo, write_dst=out, final_dst=out)
        assert r.result.startswith("ERROR (magick compress)")
        assert not out.exists(), "the half-written output would be a SKIP (exists) on the next run"
        assert os.listdir(tmp) == []

    def test_success_without_a_file_is_an_error_that_names_the_destination(self, tools, photo, tmp):
        r = self.in_place(tools, photo, "ok", "ok", "nofile")
        assert r.result.startswith(f"ERROR (magick reported success but wrote no file) | {photo.name} | expected: ")
        assert r.final_dst == str(photo)
        assert photo.read_bytes() == b"ORIGINAL" and self.nothing_left(photo, tmp)
        assert "replace" not in tools.events


class TestKeepOriginal:
    """Modes 0/9, round 26: the original leaves its place only after the copy is next to
    it, and comes back when the swap fails."""

    def _setup(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        src = d / "p.tif"
        src.write_bytes(b"ORIGINAL")
        written = d / "p.tif.abcd1234_0123456789ab.tif"
        written.write_bytes(b"COMPRESSED")
        st = os.stat(src)
        return d, src, written, st

    def test_lands_the_copy_then_archives(self, tmp_path):
        d, src, written, st = self._setup(tmp_path)
        run = compress_run("-Mode", "9")
        err = cz.complete_keep_original(run, str(written), str(src), str(src), str(d / "OLD_TIFFs"),
                                        st.st_size, st.st_mtime_ns)
        assert err is None
        assert src.read_bytes() == b"COMPRESSED"
        assert (d / "OLD_TIFFs" / "p.tif").read_bytes() == b"ORIGINAL"
        assert leftovers(d) == [] and run.moved_originals == []

    def test_an_original_edited_during_the_run_is_not_archived(self, tmp_path):
        d, src, written, st = self._setup(tmp_path)
        src.write_bytes(b"EDITED MEANWHILE")
        err = cz.complete_keep_original(compress_run("-Mode", "9"), str(written), str(src), str(src),
                                        str(d / "OLD_TIFFs"), st.st_size, st.st_mtime_ns)
        assert err and "changed or vanished" in err
        assert src.read_bytes() == b"EDITED MEANWHILE"
        assert not (d / "OLD_TIFFs").exists()

    def test_a_failed_swap_puts_the_original_back(self, tmp_path, monkeypatch):
        d, src, written, st = self._setup(tmp_path)
        real = tw.move_file_replacing
        calls = []

        def failing(a, b):
            calls.append((a, b))
            if len(calls) == 2:       # 1: written -> temp sibling; 2: temp sibling -> final
                raise OSError("locked")
            return real(a, b)
        monkeypatch.setattr(tw, "move_file_replacing", failing)
        run = compress_run("-Mode", "9")
        err = cz.complete_keep_original(run, str(written), str(src), str(src), str(d / "OLD_TIFFs"),
                                        st.st_size, st.st_mtime_ns)
        assert err and err.endswith("original restored"), err
        assert src.read_bytes() == b"ORIGINAL"
        assert not (d / "OLD_TIFFs" / "p.tif").exists()
        assert run.moved_originals == []

    def test_an_existing_backup_is_never_overwritten(self, tmp_path):
        d, src, written, st = self._setup(tmp_path)
        (d / "OLD_TIFFs").mkdir()
        (d / "OLD_TIFFs" / "p.tif").write_bytes(b"EARLIER BACKUP")
        err = cz.complete_keep_original(compress_run("-Mode", "9"), str(written), str(src), str(src),
                                        str(d / "OLD_TIFFs"), st.st_size, st.st_mtime_ns)
        assert err is None
        names = sorted(os.listdir(d / "OLD_TIFFs"))
        assert len(names) == 2 and (d / "OLD_TIFFs" / "p.tif").read_bytes() == b"EARLIER BACKUP"

    def test_interrupt_puts_back_an_original_whose_copy_never_landed(self, tmp_path, monkeypatch):
        """Round 16: Ctrl+C between the archive and the swap. A final file that does not
        decode must not veto the restore."""
        d, src, written, st = self._setup(tmp_path)
        (d / "OLD_TIFFs").mkdir()
        backup = d / "OLD_TIFFs" / "p.tif"
        os.rename(src, backup)
        run = compress_run("-Mode", "9")
        run.moved_originals.append({"backup": str(backup), "original": str(src), "final_dst": str(src)})
        run.temp_files.add(str(written))
        src.write_bytes(b"PARTIAL OUTPUT")
        monkeypatch.setattr(tw, "zip_integrity", lambda *a, **k: False)
        cz.cleanup_after_stop(run)
        assert src.read_bytes() == b"ORIGINAL"
        assert not backup.exists() and not written.exists()

    def test_interrupt_keeps_an_archived_original_whose_copy_is_good(self, tmp_path, monkeypatch):
        d, src, written, st = self._setup(tmp_path)
        (d / "OLD_TIFFs").mkdir()
        backup = d / "OLD_TIFFs" / "p.tif"
        os.rename(src, backup)
        src.write_bytes(b"GOOD COPY")
        run = compress_run("-Mode", "9")
        run.moved_originals.append({"backup": str(backup), "original": str(src), "final_dst": str(src)})
        monkeypatch.setattr(tw, "zip_integrity", lambda *a, **k: True)
        cz.cleanup_after_stop(run)
        assert src.read_bytes() == b"GOOD COPY" and backup.read_bytes() == b"ORIGINAL"


# ---------------------------------------------------------------------------
# copy_exif
# ---------------------------------------------------------------------------

def exif_run(*argv):
    opt, _bound = tw.parse_args(list(argv), cx.PARAMS)
    return cx.Run(opt, NoLog())


def pair(tiff, jpeg, **extra):
    p = {"tiff": str(tiff), "tif_name": Path(tiff).name, "tif_base": Path(tiff).stem,
         "dest_name": Path(tiff).name, "jpeg": str(jpeg) if jpeg else None,
         "used_base": Path(tiff).stem, "kind": "exact", "source": "JPEG"}
    p.update(extra)
    return cx.Pair(**p)


class TestCopyExif:
    def test_the_jpeg_copy_excludes_the_geometry_and_never_passes_unsafe(self, tools, photo):
        """Round 16: -unsafe stamped the JPEG's size and orientation onto a cropped TIFF.
        Round 28: the TIFF's own IPTC is rewritten first, the charset copied by name."""
        jpg = photo.with_suffix(".jpg")
        r = cx.process_pair(exif_run(), pair(photo, jpg), str(photo.parent), str(photo.parent), 1.0)
        assert r.line == f"OK | {photo.name} <= {jpg.name}"
        call = tools.exif_calls[0]
        assert "-unsafe" not in call
        for tag in ("--ImageWidth", "--ImageHeight", "--Orientation", "--ThumbnailImage"):
            assert tag in call
        assert call[call.index("-tagsfromfile"):call.index("-tagsfromfile") + 3] == ["-tagsfromfile", "@", "-IPTC:All"]
        assert "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet" in call
        assert call.count("-q") == 1 and "-P" in call

    def test_a_raf_never_gives_its_focal_plane_resolution_or_icc(self, tools, photo):
        raf = photo.with_suffix(".RAF")
        r = cx.process_pair(exif_run("-IccPolicy", "always"), pair(photo, raf, source="RAF"),
                            str(photo.parent), str(photo.parent), 1.0)
        assert r.line.endswith("[EXIF from RAW]")
        call = next(c for c in tools.exif_calls if "-EXIF:All" in c)
        assert "--FocalPlaneXResolution" in call and "-ICC_Profile" not in call

    def test_the_tag_list_of_the_jpeg_copy(self):
        """The same rules, on the list itself: the TIFF's own IPTC first, then the camera's
        tags, the charset by name after -IPTC:All, the geometry and the embedded images
        excluded by name, the file last."""
        args = cx._jpeg_copy_args("JPEG", "C:\\t\\tw_0123.mie", "-ICC_Profile", "E:\\伊豆沼 [x]\\a 50%.tif")
        assert args[:5] == ["-tagsfromfile", "@", "-IPTC:All", "-tagsfromfile", "C:\\t\\tw_0123.mie"]
        assert args[5:8] == ["-EXIF:All", "-XMP:All", "-IPTC:All"]
        assert args[8] == "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet"
        assert args[-2:] == ["-ICC_Profile", "E:\\伊豆沼 [x]\\a 50%.tif"]
        assert "-unsafe" not in args and not [a for a in args if a.startswith("--FocalPlane")]
        geometry = {"--ImageWidth", "--ImageHeight", "--ImageLength", "--ExifImageWidth",
                    "--ExifImageHeight", "--PixelXDimension", "--PixelYDimension", "--Orientation",
                    "--ThumbnailImage", "--PreviewImage", "--JpgFromRaw"}
        assert {a for a in args if a.startswith("--")} == geometry
        raf = cx._jpeg_copy_args("RAF", "x.RAF", "", "a.tif")
        assert raf[-4:] == ["--FocalPlaneXResolution", "--FocalPlaneYResolution",
                            "--FocalPlaneResolutionUnit", "a.tif"]
        assert "-ICC_Profile" not in raf and geometry < {a for a in raf if a.startswith("--")}

    def test_an_unreadable_tiff_is_never_overwritten(self, tools, photo):
        """-SkipIfTiffHasExif: the probe's exit code decides, fail closed."""
        tools.exif_result = (1, "")
        r = cx.process_pair(exif_run("-SkipIfTiffHasExif"), pair(photo, photo.with_suffix(".jpg")),
                            str(photo.parent), str(photo.parent), 1.0)
        assert r.line.startswith("ERROR (exiftool EXIF check)")
        assert len(tools.exif_calls) == 1

    def test_safe_mode_passes_the_restricted_list(self, tools, photo):
        tools.pages = 3
        tools.only_subfile = False
        r = cx.process_pair(exif_run("-CompressZip"), pair(photo, photo.with_suffix(".jpg")),
                            str(photo.parent), str(photo.parent), 1.0)
        assert r.line.startswith("MULTI (3 IFDs") and r.multi == str(photo)
        assert tools.allowed_seen == [("REDUCEDIMAGE", "REDUCED")]
        assert tools.exif_calls == [], "a multi-page TIFF must not be written to at all"

    @pytest.mark.parametrize("failing,text", [
        ("pixels", "ERROR (ZIP pixel verification failed - original untouched)"),
        ("layout_kept", "ERROR (sample layout changed: rgb/3/8,8,8/ - original untouched)"),
        ("subfile_restore", "ERROR (subfiletype restore failed - original untouched)"),
    ])
    def test_a_failed_gate_never_registers_the_staged_file(self, tools, photo, failing, text):
        """Round 16: the staged ZIP is moved ON TOP of the TIFF; a file registered before
        its gates was moved over the EXIF-bearing original."""
        setattr(tools, failing, False)
        r = cx.process_pair(exif_run("-CompressZip"), pair(photo, photo.with_suffix(".jpg")),
                            str(photo.parent), str(photo.parent), 1.0)
        assert r.line.startswith(text), r.line
        assert r.staged is None
        assert sorted(os.listdir(photo.parent)) == [photo.name], "the staged file stayed behind"

    def test_a_failed_metadata_restore_never_registers_the_staged_file(self, tools, photo, monkeypatch):
        calls = []

        def exiftool(args, merge_stderr=False):
            calls.append(list(args))
            if "-all:all" in args:
                return 0, "Warning: Error opening file"
            return 0, ""
        monkeypatch.setattr(tw, "exiftool", exiftool)
        r = cx.process_pair(exif_run("-CompressZip"), pair(photo, photo.with_suffix(".jpg")),
                            str(photo.parent), str(photo.parent), 1.0)
        assert r.line.startswith("ERROR (exiftool metadata restore failed - original untouched)")
        assert r.staged is None
        assert sorted(os.listdir(photo.parent)) == [photo.name]

    def test_success_registers_the_staged_file_under_this_runs_id(self, tools, photo):
        run = exif_run("-CompressZip")
        r = cx.process_pair(run, pair(photo, photo.with_suffix(".jpg")),
                            str(photo.parent), str(photo.parent), 1.0)
        assert r.line.startswith("OK+ZIP |"), r.line
        assert r.staged.startswith(run.run_id + "_") and tw.LEFTOVER_NAME_RE.search(r.staged)
        assert photo.read_bytes() == b"ORIGINAL", "the worker stages; the move comes after"
        ev = tools.events
        assert ev.index("layout") < ev.index("pixels") < ev.index("subfile")

    def _to_outputdir(self, photo, tmp_path, *argv):
        out = tmp_path / "out 伊豆沼 [x] 100%"
        r = cx.process_pair(exif_run("-OutputDir", str(out), *argv), pair(photo, photo.with_suffix(".jpg")),
                            str(out), str(out), 1.0)
        return out, r

    @pytest.mark.parametrize("failing,text", [
        ("exif_copy", "ERROR (exiftool EXIF) |"),
        ("jpeg_container", "ERROR (metadata container failed - EXIF copy refused) |"),
        ("compression", "ERROR (exiftool check) |"),
        ("layout", "ERROR (sample layout unreadable) |"),
        ("alpha_ok", "ERROR (associated alpha that is not opaque: ImageMagick would change it - refused) |"),
        ("magick_rc", "ERROR (magick ZIP) |"),
        ("magick_writes", "ERROR (magick ZIP) |"),
        ("layout_kept", "ERROR (sample layout changed: rgb/3/8,8,8/ - original untouched) |"),
        ("pixels", "ERROR (ZIP pixel verification failed - original untouched) |"),
        ("tiff_container", "ERROR (exiftool metadata restore failed - original untouched) |"),
        ("restore", "ERROR (exiftool metadata restore failed - original untouched) |"),
        ("subfile_restore", "ERROR (subfiletype restore failed - original untouched) |"),
    ])
    def test_with_outputdir_a_failure_leaves_neither_the_copy_nor_a_staged_file(
            self, tools, photo, tmp_path, monkeypatch, failing, text):
        """With -OutputDir the TIFF is copied there first and the ZIP staged next to the
        copy. Whatever fails after the copy, both go: a copy left behind is skipped as
        "exists in OutputDir" by the next run, without EXIF and uncompressed."""
        if failing == "exif_copy":
            tools.exif_result = (0, "Warning: Error opening file - x.jpg")
        elif failing == "jpeg_container":
            tools.meta = (True, None)
        elif failing == "compression":
            monkeypatch.setattr(tw, "ifd0_compression", lambda p: (False, ""))
        elif failing == "layout":
            tools.layout = (False, [])
        elif failing == "magick_rc":
            tools.magick_rc = 1
        elif failing == "tiff_container":
            answers = [(False, None), (True, None)]      # the JPEG's, then the TIFF's
            monkeypatch.setattr(tw, "backup_tiff_metadata", lambda s, d: answers.pop(0))
        elif failing == "restore":
            monkeypatch.setattr(tw, "exiftool", lambda args, merge_stderr=False: (
                (0, "Warning: Error opening file") if "-all:all" in args else (0, "")))
        else:
            setattr(tools, failing, False)
        out, r = self._to_outputdir(photo, tmp_path, "-CompressZip")
        assert r.line.startswith(text), r.line
        if failing == "magick_writes":
            assert "magick reported success but wrote no file" in r.line
        assert r.staged is None
        assert os.listdir(out) == [], "the copy or the staged file stayed in -OutputDir"
        assert photo.read_bytes() == b"ORIGINAL"
        assert sorted(os.listdir(photo.parent)) == [photo.name]

    def test_with_outputdir_success_leaves_only_the_staged_zip(self, tools, photo, tmp_path):
        """The copy has served (EXIF written to it, the ZIP made from it): the staged ZIP
        takes its name in the move that follows."""
        out, r = self._to_outputdir(photo, tmp_path, "-CompressZip")
        assert r.line.startswith("OK+ZIP |"), r.line
        assert os.listdir(out) == [r.staged]
        assert photo.read_bytes() == b"ORIGINAL"
        ev = tools.events
        assert ev.index("magick") < ev.index("layout") < ev.index("pixels") < ev.index("subfile")
        restore = next(c for c in tools.exif_calls if "-all:all" in c)
        assert ev.index("pixels") < [i for i, e in enumerate(ev) if e == "exif"][-1] < ev.index("subfile"), \
            "the metadata restore runs between the pixel gate and the subfiletype restore"
        assert restore[-1] == str(out / r.staged)

    def test_with_outputdir_and_no_zip_the_copy_carries_the_exif(self, tools, photo, tmp_path):
        out, r = self._to_outputdir(photo, tmp_path)
        assert r.line == f"OK | {photo.name} <= {photo.with_suffix('.jpg').name} -> {out}"
        assert os.listdir(out) == [photo.name]
        assert tools.exif_calls[0][-1] == str(out / photo.name), "never written to the original"

    def test_a_copy_to_outputdir_that_fails_leaves_no_partial_file(self, tools, photo, tmp_path, monkeypatch):
        out = tmp_path / "out"

        def broken_copy(src, dst):
            Path(dst).write_bytes(b"PART")
            raise OSError("disk full")
        monkeypatch.setattr(cx.shutil, "copy2", broken_copy)
        r = cx.process_pair(exif_run("-OutputDir", str(out)), pair(photo, photo.with_suffix(".jpg")),
                            str(out), str(out), 1.0)
        assert r.line.startswith("ERROR (copy to OutputDir failed)")
        assert not (out / photo.name).exists()

    def test_heuristic_match_is_a_warning(self, tools, photo):
        r = cx.process_pair(exif_run(), pair(photo, photo.parent / "photo.jpg", used_base="photo",
                                             kind="heuristic"),
                            str(photo.parent), str(photo.parent), 1.0)
        assert r.line.startswith("WARN (heuristic JPEG match) |")

    def test_find_jpeg_pair_order(self, tmp_path):
        """Round 20: JPEG exact -> JPEG variant -> RAF exact -> RAF variant -> heuristic."""
        d = tmp_path / "S5pro [1]"
        (d / "JPEG").mkdir(parents=True)
        for name in ("DSC_0001.tif", "DSC_0001.RAF", "DSC.jpg", "A_1.tif", "B_2.tif", "B.raf",
                     "C_0007.tif", "C.jpeg", "D.tif"):
            (d / name).write_bytes(b"x")
        (d / "JPEG" / "A.JPG").write_bytes(b"x")
        run = exif_run()
        files = {f.name: f for f in tw.list_files(str(d), False)}
        index = {}
        for f in tw.list_files(str(d), True):
            if f.ext.lower() in (".jpg", ".jpeg"):
                index[f.dir.lower() + "|" + f.stem.lower()] = f.path

        def found(name):
            p = cx.find_jpeg_pair(run, files[name], index)
            return (os.path.basename(p.path), p.kind, p.source) if p else None
        assert found("DSC_0001.tif") == ("DSC_0001.RAF", "exact", "RAF")
        assert found("A_1.tif") == ("A.JPG", "variant", "JPEG")
        assert found("B_2.tif") == ("B.raf", "variant", "RAF")
        assert found("C_0007.tif") == ("C.jpeg", "heuristic", "JPEG")
        assert found("D.tif") is None


# ---------------------------------------------------------------------------
# The three staging loops: what they say when the move does not land the file
# ---------------------------------------------------------------------------

def failing_move(kind):
    """A move_file_into_place that fails in one of the three ways a staging loop tells
    apart: it reports an error; it reports success and nothing is at the destination; it
    reports success and the destination has another size than the staged file."""
    def move(src, dst, run_id):
        if kind == "error":
            return "move failed: boom"
        if kind == "size":
            Path(dst).write_bytes(b"SHORT")
        return None
    return move


class TestCopyExifOutputNames:
    """copy_exif -OutputDir: every session writes into ONE folder, and the cameras restart
    their numbering (two sessions hold DSCF0001.tif). The second claimant of a name gets
    _v2: it is never skipped as "exists in OutputDir", and with -Overwrite it never lands on
    the first one's output (_dest_name)."""

    NAME = "DSCF0001 50%.tif"

    def sessions(self, tmp_path, count=2):
        made = []
        for i in range(1, count + 1):
            s = tmp_path / f"0{i} S5pro 伊豆沼 [{i}]"
            s.mkdir()
            (s / self.NAME).write_bytes(f"TIFF {i}".encode())
            (s / self.NAME).with_suffix(".jpg").write_bytes(b"JPEG")
            made.append(s)
        return made

    def run_all(self, tmp_path, folders, *argv):
        out = tmp_path / "out 伊豆沼 [x] 100%"
        run = exif_run("-OutputDir", str(out), "-Workers", "1", *argv)
        for f in folders:
            cx.process_folder(run, str(f))
        return run, out

    def renames(self, run):
        return [m for level, m in run.log.lines if "RENAME" in m]

    @pytest.mark.parametrize("extra", [[], ["-Overwrite"]], ids=["plain", "overwrite"])
    def test_the_second_session_gets_v2(self, tools, tmp_path, extra):
        run, out = self.run_all(tmp_path, self.sessions(tmp_path), *extra)
        assert sorted(os.listdir(out)) == ["DSCF0001 50%.tif", "DSCF0001 50%_v2.tif"]
        assert (out / "DSCF0001 50%.tif").read_bytes() == b"TIFF 1"
        assert (out / "DSCF0001 50%_v2.tif").read_bytes() == b"TIFF 2"
        assert self.renames(run) == [
            "  RENAME (name already taken in OutputDir) | DSCF0001 50%.tif -> DSCF0001 50%_v2.tif"]
        assert ("WARN", self.renames(run)[0]) in run.log.lines
        assert (run.ok, run.err, run.skip) == (2, 0, 0), run.log.lines

    def test_a_third_session_gets_v3_and_a_name_on_disk_is_never_taken(self, tools, tmp_path):
        folders = self.sessions(tmp_path, 3)
        out = tmp_path / "out 伊豆沼 [x] 100%"
        out.mkdir()
        (out / "DSCF0001 50%_v3.tif").write_bytes(b"SOMEONE ELSE'S")
        run, out = self.run_all(tmp_path, folders)
        assert sorted(os.listdir(out)) == ["DSCF0001 50%.tif", "DSCF0001 50%_v2.tif",
                                           "DSCF0001 50%_v3.tif", "DSCF0001 50%_v4.tif"]
        assert (out / "DSCF0001 50%_v3.tif").read_bytes() == b"SOMEONE ELSE'S"
        assert (out / "DSCF0001 50%_v4.tif").read_bytes() == b"TIFF 3"
        assert [r.split(" -> ")[1] for r in self.renames(run)] == ["DSCF0001 50%_v2.tif", "DSCF0001 50%_v4.tif"]

    def test_a_dry_run_gives_each_session_its_own_name(self, tools, tmp_path):
        """Nothing is written in a dry run, so only the claims of this run keep the third
        session off the name the second one was given."""
        run, out = self.run_all(tmp_path, self.sessions(tmp_path, 3), "-DryRun")
        assert [r.split(" -> ")[1] for r in self.renames(run)] == ["DSCF0001 50%_v2.tif", "DSCF0001 50%_v3.tif"]
        assert not out.exists()
        assert len([m for _lv, m in run.log.lines if "] DRY (" in m]) == 3, run.log.lines

    def test_the_same_session_twice_keeps_its_own_name(self, tools, tmp_path):
        """The claim is by source path: a folder listed twice is not its own rival."""
        s1 = self.sessions(tmp_path, 1)[0]
        run, out = self.run_all(tmp_path, [s1, s1])
        assert os.listdir(out) == ["DSCF0001 50%.tif"] and self.renames(run) == []
        assert [m for _lv, m in run.log.lines if "SKIP (exists in OutputDir)" in m]

    def test_without_outputdir_nothing_is_renamed(self, tools, tmp_path):
        folders = self.sessions(tmp_path)
        run = exif_run("-Workers", "1")
        for f in folders:
            cx.process_folder(run, str(f))
        assert self.renames(run) == [] and run.claimed_dest == {}
        assert all(sorted(os.listdir(f)) == ["DSCF0001 50%.jpg", "DSCF0001 50%.tif"] for f in folders)

    def test_the_zip_path_lands_under_the_new_name_too(self, tools, tmp_path):
        run, out = self.run_all(tmp_path, self.sessions(tmp_path), "-CompressZip")
        assert sorted(os.listdir(out)) == ["DSCF0001 50%.tif", "DSCF0001 50%_v2.tif"]
        assert (run.ok, run.err) == (2, 0), run.log.lines
        assert len(self.renames(run)) == 1


def error_lines(run):
    return [msg for level, msg in run.log.lines if level == "ERROR"]


class TestStagedMoveFailures:
    """A staged file that did not land is ONE counted error line (exit code 1), and the
    run goes on. The loops: modes 1-8, copy_exif -CompressZip."""

    NAME = "photo 50%off.tif"

    @pytest.mark.parametrize("kind,text", [
        ("missing", f"ERROR (move failed) | {NAME}"),
        ("size", f"ERROR (size mismatch after move) | {NAME}"),
        ("error", f"ERROR (move exception) | {NAME}: move failed: boom"),
    ])
    def test_modes(self, tools, photo, tmp_path, monkeypatch, kind, text):
        monkeypatch.setattr(tw, "move_file_into_place", failing_move(kind))
        run = compress_run("-Mode", "1", "-InputDir", str(photo.parent),
                           "-StagingDir", str(tmp_path / "stg"), "-ForceSequential")
        assert cz.run_modes(run, {"Mode", "InputDir", "StagingDir", "ForceSequential"}) == 1
        assert error_lines(run) == [text]
        assert (run.err, run.ok) == (1, 1)
        assert not [m for _lv, m in run.log.lines if "Moved" in m]
        assert photo.read_bytes() == b"ORIGINAL"

    @pytest.mark.parametrize("kind,text", [
        ("missing", f"ERROR (move failed) | {NAME}"),
        ("size", f"ERROR (move failed) | {NAME}"),
        ("error", f"ERROR (move failed) | {NAME}: move failed: boom"),
    ])
    def test_copy_exif(self, tools, photo, tmp_path, monkeypatch, kind, text):
        photo.with_suffix(".jpg").write_bytes(b"JPEG")
        monkeypatch.setattr(tw, "move_file_into_place", failing_move(kind))
        run = exif_run("-CompressZip", "-OutputDir", str(tmp_path / "out [x]"), "-Workers", "1")
        cx.process_folder(run, str(photo.parent))
        assert error_lines(run) == [text]
        assert (run.err, run.ok) == (1, 1)
        assert not [m for _lv, m in run.log.lines if "Moved" in m]
        assert photo.read_bytes() == b"ORIGINAL"

    def test_modes_land_the_staged_file(self, tools, photo, tmp_path):
        stg = tmp_path / "stg"
        run = compress_run("-Mode", "1", "-InputDir", str(photo.parent), "-StagingDir", str(stg),
                           "-ForceSequential")
        assert cz.run_modes(run, {"Mode", "InputDir", "StagingDir", "ForceSequential"}) == 0
        out = photo.parent / "ZIP"
        assert (out / self.NAME).read_bytes() == b"COMPRESSED" and os.listdir(stg) == []
        assert ("INFO", f"  -> Moved 1 file(s) -> {out}") in run.log.lines
        assert (run.err, run.ok) == (0, 1) and photo.read_bytes() == b"ORIGINAL"

    def test_copy_exif_lands_the_staged_file_and_keeps_the_date(self, tools, photo, tmp_path):
        """In place the staged ZIP takes the TIFF's name, and the TIFF's write time: the
        EXIF-only path keeps it too."""
        photo.with_suffix(".jpg").write_bytes(b"JPEG")
        old = 1_200_000_000_000_000_000
        os.utime(photo, ns=(old, old))
        stg = tmp_path / "stg [x]"
        run = exif_run("-CompressZip", "-StagingDir", str(stg), "-Workers", "1")
        cx.process_folder(run, str(photo.parent))
        assert photo.read_bytes() == b"COMPRESSED" and os.listdir(stg) == []
        assert os.stat(photo).st_mtime_ns == old
        assert ("INFO", f"  -> Moved 1 file(s) -> {photo.parent}") in run.log.lines
        assert (run.err, run.ok) == (0, 1)


def modes(*argv):
    """run_modes on the command line `argv` (one file at a time): (run, exit code)."""
    opt, bound = tw.parse_args(list(argv) + ["-ForceSequential"], cz.PARAMS)
    run = cz.Run(opt, NoLog(), [])
    return run, cz.run_modes(run, bound)


def said(run, text):
    return [m for _lv, m in run.log.lines if text in m]


class TestPlan:
    """What run_modes decides before any worker starts (_plan_jobs): which file gets which
    destination, which one is refused or skipped -- and that a refused file is never queued."""

    @pytest.fixture
    def flat(self, tmp_path):
        """Two source folders that hold the same file name, and a flat output folder."""
        root = tmp_path / "伊豆沼 [FINAL] x"
        for sub in ("a", "b"):
            (root / sub).mkdir(parents=True)
            (root / sub / "x 50%.tif").write_bytes(sub.upper().encode())
        return root, tmp_path / "out [x]"

    def test_mode2_numbered_gives_the_second_claimant_v2(self, tools, flat):
        root, out = flat
        run, rc = modes("-Mode", "2", "-InputDir", str(root), "-OutputDir", str(out))
        assert rc == 0 and sorted(os.listdir(out)) == ["x 50%.tif", "x 50%_v2.tif"]
        assert len(tools.magick_calls) == 2 and (run.ok, run.counter, run.total) == (2, 2, 2)

    def test_mode2_overwrite_takes_the_earlier_claimant_out_of_the_plan(self, tools, flat):
        """Last claimant wins and the dropped one is still reported. The plan is REBOUND
        there: whoever reads the job list afterwards must read the new one, or both files
        are written to the same name."""
        root, out = flat
        run, rc = modes("-Mode", "2", "-InputDir", str(root), "-OutputDir", str(out),
                        "-DuplicateAction", "Overwrite")
        assert rc == 0 and os.listdir(out) == ["x 50%.tif"]
        assert [c[1] for c in tools.magick_calls] == [str(root / "b" / "x 50%.tif")], \
            "only the last claimant is compressed"
        assert len(said(run, "SKIP (superseded by a later duplicate in flat output) | x 50%.tif")) == 1
        assert len(said(run, "OVERWRITE (duplicate filename in flat output) | x 50%.tif")) == 1
        assert (run.ok, run.skip, run.counter, run.total) == (1, 1, 2, 2)

    def test_mode2_skip_leaves_the_later_claimant_out(self, tools, flat):
        root, out = flat
        run, rc = modes("-Mode", "2", "-InputDir", str(root), "-OutputDir", str(out),
                        "-DuplicateAction", "Skip")
        assert rc == 0 and os.listdir(out) == ["x 50%.tif"]
        assert [c[1] for c in tools.magick_calls] == [str(root / "a" / "x 50%.tif")]
        assert len(said(run, "SKIP (duplicate filename in flat output) | x 50%.tif")) == 1
        assert (run.ok, run.skip, run.counter, run.total) == (1, 1, 2, 2)

    @pytest.mark.parametrize("mode", ["0", "9"])
    def test_modes_0_and_9_refuse_a_multi_page_file_while_planning(self, tools, photo, mode):
        """SafeMode: the file is reported and NOT queued -- queued, the worker would
        compress it and the original would go to OLD_TIFFs."""
        tools.pages, tools.only_subfile = 2, False
        run, rc = modes("-Mode", mode, "-InputDir", str(photo.parent))
        assert said(run, f"MULTI (2 pages - skipped, original untouched) | {photo.name}")
        assert tools.magick_calls == [] and run.multi_paths == [str(photo)] and run.multi == 1
        assert os.listdir(photo.parent) == [photo.name] and photo.read_bytes() == b"ORIGINAL"

    @pytest.mark.parametrize("mode", ["0", "9"])
    def test_modes_0_and_9_write_a_temp_sibling_never_the_file_itself(self, tools, photo, mode):
        """The worker's output path for a .tif source would BE the original."""
        run, rc = modes("-Mode", mode, "-InputDir", str(photo.parent))
        assert rc == 0, run.log.lines
        written = tools.magick_calls[0][-1].replace("%%", "%")
        assert written != str(photo) and os.path.dirname(written) == str(photo.parent)
        assert tw.LEFTOVER_NAME_RE.search(os.path.basename(written)), written
        assert photo.read_bytes() == b"COMPRESSED"
        assert (photo.parent / "OLD_TIFFs" / photo.name).read_bytes() == b"ORIGINAL"
        assert leftovers(photo.parent) == [] and run.temp_files

    def test_mode9_with_staging_writes_there_and_leaves_it_empty(self, tools, photo, tmp_path):
        stg = tmp_path / "stg [x]"
        run, rc = modes("-Mode", "9", "-InputDir", str(photo.parent), "-StagingDir", str(stg))
        assert rc == 0, run.log.lines
        assert os.path.dirname(tools.magick_calls[0][-1].replace("%%", "%")) == str(stg)
        assert os.listdir(stg) == [] and photo.read_bytes() == b"COMPRESSED"
        assert (photo.parent / "OLD_TIFFs" / photo.name).read_bytes() == b"ORIGINAL"

    @pytest.mark.parametrize("mode", ["1", "8", "9"])
    def test_a_dry_run_plans_without_the_staging_folder(self, tools, photo, tmp_path, mode):
        stg = tmp_path / "stg [x]"
        run, rc = modes("-Mode", mode, "-InputDir", str(photo.parent), "-StagingDir", str(stg), "-DryRun")
        assert rc == 0 and run.dry == 1 and said(run, f"DRY (Uncompressed -> ZIP) | {photo.name}")
        assert not stg.exists(), "a dry run created the staging folder"
        assert os.listdir(photo.parent) == [photo.name] and tools.magick_calls == []
        assert not run.temp_files

    @pytest.mark.parametrize("mode", ["0", "9"])
    def test_a_dry_run_of_modes_0_and_9_has_nothing_to_clean_or_archive(self, tools, photo, mode):
        run, rc = modes("-Mode", mode, "-InputDir", str(photo.parent), "-DryRun")
        assert rc == 0 and run.dry == 1 and not run.temp_files and not run.moved_originals
        assert os.listdir(photo.parent) == [photo.name] and photo.read_bytes() == b"ORIGINAL"

    def test_a_failed_archive_is_a_counted_error(self, tools, photo, monkeypatch):
        """Modes 0/9: complete_keep_original said no -- the run must end with exit code 1."""
        monkeypatch.setattr(cz, "complete_keep_original", lambda *a, **k: "boom")
        run, rc = modes("-Mode", "9", "-InputDir", str(photo.parent))
        assert rc == 1 and run.err == 1
        assert error_lines(run) == [f"ERROR (keep original) | {photo.name} | boom"]

    def test_an_output_over_its_own_source_is_refused_in_the_copy_modes(self, tools, tmp_path):
        """Modes 1/3/4/6/7 never mean "in place": a folder name that makes the output resolve
        onto the source (-ZipSuffix _TIFF in mode 4) is an error, not a silent replace."""
        d = tmp_path / "伊豆沼 [x]" / "shots_TIFF"
        d.mkdir(parents=True)
        (d / "p.tif").write_bytes(b"ORIGINAL")
        run, rc = modes("-Mode", "4", "-InputDir", str(d.parent), "-ZipSuffix", "_TIFF")
        assert rc == 1 and tools.magick_calls == [] and (d / "p.tif").read_bytes() == b"ORIGINAL"
        assert said(run, "ERROR (mode 4 would write over its own source - refused;")

    def test_mode5_never_climbs_above_the_input_root(self, tmp_path):
        """A TIFF directly in the root has its grandparent OUTSIDE the chosen tree."""
        root = str(tmp_path / "data")
        opt, _b = tw.parse_args(["-Mode", "5"], cz.PARAMS)

        def out(directory):
            f = SimpleNamespace(dir=directory, stem="p", name="p.tif", path=directory + "\\p.tif")
            return cz.resolve_output(f, 5, root, opt)
        assert out(root) == root + "\\ZIP\\p.tif"
        assert out(root + "\\job") == root + "\\ZIP\\p.tif"
        assert out(root + "\\job\\TIFF") == root + "\\job\\ZIP\\p.tif"
        # a prefix is not a parent: C:\data also prefixes C:\database
        assert out(root + "base\\x") == root + "\\ZIP\\p.tif"


class TestMode8DefaultStaging:
    """Mode 8 without -StagingDir stages in a folder of its own (_cs8_<id>): inside the input
    folder when there is one, in the temp folder when there are several. On an interactive
    console it asks first; an unattended run (the wizard, a preset, a pipe) is never asked."""

    @pytest.fixture
    def console(self, monkeypatch):
        """An interactive console whose answers are the list this returns."""
        answers = []
        asked = []

        def fake_input(prompt=""):
            asked.append(prompt)
            if not answers:
                raise EOFError
            return answers.pop(0)
        monkeypatch.setattr(cz, "_is_interactive", lambda: True)
        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(tw, "say", lambda *a, **k: None)
        return SimpleNamespace(answers=answers, asked=asked)

    def staging_folders(self, folder):
        return [n for n in os.listdir(folder) if n.startswith("_cs8_")]

    def test_unattended_it_is_never_asked(self, tools, photo, monkeypatch):
        def no_input(prompt=""):
            raise AssertionError("an unattended run was asked a question")
        monkeypatch.setattr(cz, "_is_interactive", lambda: False)
        monkeypatch.setattr("builtins.input", no_input)
        run, rc = modes("-Mode", "8", "-InputDir", str(photo.parent))
        assert rc == 0, run.log.lines
        staged = tools.magick_calls[0][-1].replace("%%", "%")
        assert os.path.dirname(os.path.dirname(staged)) == str(photo.parent)
        assert os.path.basename(os.path.dirname(staged)).startswith("_cs8_")
        assert photo.read_bytes() == b"COMPRESSED"
        assert os.listdir(photo.parent) == [photo.name], "the staging folder of the run stayed"
        assert said(run, "Default temporary staging will be used: ")

    @pytest.mark.parametrize("answer", ["", "y", "Y", "yes", "s", " "])
    def test_yes_or_enter_uses_the_default_folder(self, tools, photo, console, answer):
        console.answers.append(answer)
        run, rc = modes("-Mode", "8", "-InputDir", str(photo.parent))
        assert rc == 0 and console.asked == ["Use default temporary staging? [Y/n]: "]
        assert photo.read_bytes() == b"COMPRESSED" and os.listdir(photo.parent) == [photo.name]

    @pytest.mark.parametrize("answer", ["n", "N", "no", " nao", "Não"])
    def test_no_stops_the_run_before_anything_is_made(self, tools, photo, console, answer):
        console.answers.append(answer)
        run, rc = modes("-Mode", "8", "-InputDir", str(photo.parent))
        assert rc == 1
        assert error_lines(run) == ["Mode 8 aborted: no staging directory selected."]
        assert tools.magick_calls == [] and photo.read_bytes() == b"ORIGINAL"
        assert os.listdir(photo.parent) == [photo.name], "a refused run created its staging folder"
        assert not run.cleanup_dirs and not run.created_default_staging

    def test_end_of_input_counts_as_enter(self, tools, photo, console):
        """As it is today: Ctrl+Z at the question goes on with the default folder."""
        run, rc = modes("-Mode", "8", "-InputDir", str(photo.parent))
        assert rc == 0 and len(console.asked) == 1 and photo.read_bytes() == b"COMPRESSED"

    def test_with_staging_given_or_in_a_dry_run_there_is_no_question(self, tools, photo, console, tmp_path):
        run, rc = modes("-Mode", "8", "-InputDir", str(photo.parent), "-StagingDir", str(tmp_path / "stg"))
        assert rc == 0 and console.asked == []
        photo.write_bytes(b"ORIGINAL")
        run, rc = modes("-Mode", "8", "-InputDir", str(photo.parent), "-DryRun")
        assert rc == 0 and console.asked == [] and os.listdir(photo.parent) == [photo.name]
        assert not said(run, "Default temporary staging")

    def test_several_roots_stage_in_the_temp_folder(self, tools, photo, tmp_path, monkeypatch):
        other = tmp_path / "second [x] 一時"
        other.mkdir()
        (other / "q.tif").write_bytes(b"ORIGINAL")
        temp = tmp_path / "temp 50%"
        temp.mkdir()
        monkeypatch.setattr(cz.tempfile, "gettempdir", lambda: str(temp))
        monkeypatch.setattr(cz, "_is_interactive", lambda: False)
        run, rc = modes("-Mode", "8", "-InputDir", f"{photo.parent};{other}")
        assert rc == 0, run.log.lines
        for call in tools.magick_calls:
            assert os.path.dirname(os.path.dirname(call[-1].replace("%%", "%"))) == str(temp)
        assert os.listdir(temp) == [] and self.staging_folders(photo.parent) == []
        assert photo.read_bytes() == b"COMPRESSED" and (other / "q.tif").read_bytes() == b"COMPRESSED"


class TestGroupSteps:
    """What run_modes does with a group once its workers are done: move the staged files,
    archive the originals (modes 0/9), delete the sources (mode 8) -- each step on its own."""

    def _outcome(self, **given):
        fields = dict(staging_map={}, error_srcs=set(), integrity_failed_dst=set(), results=[])
        fields.update(given)
        return cz._GroupOutcome(**fields)

    def _tiff_and_tif(self, tmp_path):
        d = tmp_path / "伊豆沼 [FINAL] x"
        d.mkdir()
        src, dst = d / "p 50%.tiff", d / "p 50%.tif"
        src.write_bytes(b"ORIGINAL")
        dst.write_bytes(b"COMPRESSED")
        job = cz.Job(src=str(src), write_dst=str(tmp_path / "stg" / "x.tif"), final_dst=str(dst),
                     name=src.name)
        return src, dst, job, cz.JobResult("OK (Uncompressed -> ZIP) [SOURCE DELETED] | p 50%.tiff",
                                           src.name, str(src), can_delete=True)

    def test_mode8_checks_the_file_where_it_landed_before_it_deletes_the_source(
            self, tmp_path, monkeypatch):
        """Round 12: the pixel gate ran on the STAGED file. The .tiff source goes only after
        the .tif that replaces it decodes at its destination."""
        src, dst, job, result = self._tiff_and_tif(tmp_path)
        run = compress_run("-Mode", "8", "-DeleteSource")
        checked, verdict = [], [False]

        def integrity(path, timeout=30, load_factor=1.0):
            checked.append(path)
            assert src.exists(), "the source must still be there while its replacement is checked"
            return verdict[0]
        monkeypatch.setattr(tw, "zip_integrity", integrity)
        cz._delete_sources(run, self._outcome(results=[(job, result)]))
        assert src.read_bytes() == b"ORIGINAL" and checked == [str(dst)]
        assert error_lines(run) == [f"ERROR (final ZIP failed integrity - source preserved) | {src.name}"]
        assert run.err == 1
        verdict[0] = True
        os.chmod(src, stat.S_IREAD)                      # a read-only source is deleted too
        cz._delete_sources(run, self._outcome(results=[(job, result)]))
        assert not src.exists() and dst.read_bytes() == b"COMPRESSED"
        assert ("INFO", "  -> Deleted 1 source file(s) after successful move") in run.log.lines
        assert run.err == 1, "no new error"

    def test_mode8_keeps_the_source_when_nothing_landed_or_nothing_asked(self, tmp_path, monkeypatch):
        src, dst, job, result = self._tiff_and_tif(tmp_path)
        checked = []
        monkeypatch.setattr(tw, "zip_integrity", lambda p, t=30, load_factor=1.0: checked.append(p) or True)
        run = compress_run("-Mode", "8", "-DeleteSource")
        os.remove(dst)
        cz._delete_sources(run, self._outcome(results=[(job, result)]))
        assert error_lines(run) == [f"ERROR (final destination missing, source preserved) | {src.name}"]
        assert src.exists() and checked == []
        dst.write_bytes(b"COMPRESSED")
        refused = cz.JobResult("ERROR (x) | p", src.name, str(src), can_delete=False)
        cz._delete_sources(run, self._outcome(results=[(job, refused)]))
        dry = compress_run("-Mode", "8", "-DeleteSource", "-DryRun")
        cz._delete_sources(dry, self._outcome(results=[(job, result)]))
        other_mode = compress_run("-Mode", "1", "-DeleteSource")
        cz._delete_sources(other_mode, self._outcome(results=[(job, result)]))
        in_place = cz.Job(src=str(dst), write_dst=job.write_dst, final_dst=str(dst), name=dst.name)
        replaced = cz.JobResult("OK (Uncompressed -> ZIP) | p 50%.tif", dst.name, str(dst), can_delete=True)
        cz._delete_sources(run, self._outcome(results=[(in_place, replaced)]))
        assert src.exists() and dst.exists() and checked == [], "nothing may be deleted in any of these"
        assert run.err == 1 and dry.err == 0 and other_mode.err == 0

    def test_a_staged_file_that_failed_its_gate_is_dropped_never_moved(self, tmp_path, monkeypatch):
        """Round 12: a bad ZIP must not replace the original. The staged file goes; the good
        one of the same group lands."""
        stg, lib = tmp_path / "stg", tmp_path / "lib [x] 50%"
        stg.mkdir()
        lib.mkdir()
        jobs, staging_map = [], {}
        for name in ("a.tif", "b.tif", "c.tif"):
            (lib / name).write_bytes(b"ORIGINAL " + name.encode())
            staged = "abcd1234_0123456789ab_" + name
            (stg / staged).write_bytes(b"COMPRESSED " + name.encode())
            staging_map[str(lib / name).lower()] = staged
            # c.tif is a mode 0/9 job in this group: it lands through _archive_originals
            jobs.append(cz.Job(src=str(lib / name), write_dst=str(stg / staged), final_dst=str(lib / name),
                               name=name, keep_dir=str(lib / "OLD_TIFFs") if name == "c.tif" else None))
        moves = []
        real = tw.move_file_into_place

        def spy(src, dst, run_id):
            moves.append(os.path.basename(dst))
            return real(src, dst, run_id)
        monkeypatch.setattr(tw, "move_file_into_place", spy)
        run = compress_run("-Mode", "8", "-StagingDir", str(stg))
        outcome = self._outcome(staging_map=staging_map, integrity_failed_dst={str(lib / "b.tif").lower()})
        cz._move_staged(run, jobs, str(lib), outcome)
        assert (lib / "a.tif").read_bytes() == b"COMPRESSED a.tif"
        assert (lib / "b.tif").read_bytes() == b"ORIGINAL b.tif"
        assert (lib / "c.tif").read_bytes() == b"ORIGINAL c.tif"
        assert moves == ["a.tif"]
        assert os.listdir(stg) == ["abcd1234_0123456789ab_c.tif"], "the failed file is dropped, c.tif waits"
        assert ("INFO", f"  -> Moved 1 file(s) -> {lib}") in run.log.lines and run.err == 0
        (stg / "abcd1234_0123456789ab_a.tif").write_bytes(b"SECOND")
        dry = compress_run("-Mode", "8", "-StagingDir", str(stg), "-DryRun")
        cz._move_staged(dry, jobs[:1], str(lib), self._outcome(staging_map=staging_map))
        assert (lib / "a.tif").read_bytes() == b"COMPRESSED a.tif" and moves == ["a.tif"], \
            "a dry run moves nothing"

    @pytest.mark.parametrize("failing,reason", [("pixels", "ZIP integrity check failed"),
                                                ("exif", "metadata restore failed")])
    def test_mode8_a_failed_gate_is_an_error_line_and_its_staged_file_never_lands(
            self, tools, photo, tmp_path, failing, reason):
        """Rounds 12 and 28, the whole chain: the worker says the gate failed, the line that
        is counted is an ERROR, the staged file is dropped and the source is kept."""
        stg = tmp_path / "stg"
        stg.mkdir()
        if failing == "exif":
            tools.exif_result = (0, "Warning: Error opening file - x.mie")
        else:
            tools.pixels = False
        run = compress_run("-Mode", "8", "-DeleteSource", "-StagingDir", str(stg), "-ForceSequential")
        run.total = 1
        staged = stg / "abcd1234_0123456789ab_photo.tif"
        job = cz.Job(src=str(photo), write_dst=str(staged), final_dst=str(photo), name=photo.name,
                     comp="Uncompressed")
        outcome = cz._run_group(run, [job], 1, {})
        assert error_lines(run) == [f"[1/1] ERROR ({reason} - source preserved) | {photo.name}"]
        key = str(photo).lower()
        assert outcome.integrity_failed_dst == {key} and outcome.staging_map == {key: staged.name}
        assert staged.exists()
        cz._move_staged(run, [job], str(photo.parent), outcome)
        cz._delete_sources(run, outcome)
        assert photo.read_bytes() == b"ORIGINAL" and os.listdir(stg) == []
        assert (run.err, run.ok) == (1, 0)

    def test_a_worker_that_raised_never_gets_its_original_archived(self, tools, photo, monkeypatch):
        """Modes 0/9: a worker that fails AFTER it wrote the copy is that file's ERROR line;
        the copy is dropped and the original stays where it is (round 26)."""
        def boom(path, agent, params):
            raise RuntimeError("history exploded")
        monkeypatch.setattr(tw, "add_tiff_history_entry", boom)
        run = compress_run("-Mode", "9", "-ForceSequential")
        run.total = 1
        st = os.stat(photo)
        sibling = str(photo) + ".abcd1234_0123456789ab.tif"
        job = cz.Job(src=str(photo), write_dst=sibling, final_dst=str(photo), name=photo.name,
                     comp="Uncompressed", safe_checked=True, keep_dir=str(photo.parent / "OLD_TIFFs"),
                     src_length=st.st_size, src_mtime_ns=st.st_mtime_ns)
        outcome = cz._run_group(run, [job], 1, {})
        assert error_lines(run) == [
            f"[1/1] ERROR (unexpected) | {photo.name} | RuntimeError: history exploded"]
        assert outcome.error_srcs == {str(photo).lower()} and run.err == 1
        assert os.path.exists(sibling), "the worker left its copy behind"
        cz._archive_originals(run, [job], outcome)
        assert photo.read_bytes() == b"ORIGINAL" and not os.path.exists(sibling)
        assert not (photo.parent / "OLD_TIFFs").exists()
        assert run.err == 1 and not [m for _lv, m in run.log.lines if "archived" in m]

    @pytest.mark.parametrize("why", ["error", "integrity"])
    def test_modes_0_9_never_archive_an_original_whose_copy_failed(self, tmp_path, why):
        """Round 26: a copy that failed never causes the archive; its leftover is dropped."""
        d = tmp_path / "写真 [x]"
        d.mkdir()
        src = d / "p.tif"
        src.write_bytes(b"ORIGINAL")
        written = d / "p.tif.abcd1234_0123456789ab.tif"
        written.write_bytes(b"BAD COPY")
        st = os.stat(src)
        job = cz.Job(src=str(src), write_dst=str(written), final_dst=str(src), name=src.name,
                     keep_dir=str(d / "OLD_TIFFs"), src_length=st.st_size, src_mtime_ns=st.st_mtime_ns)
        run = compress_run("-Mode", "9")
        failed = (self._outcome(error_srcs={str(src).lower()}) if why == "error"
                  else self._outcome(integrity_failed_dst={str(src).lower()}))
        cz._archive_originals(run, [job], failed)
        assert src.read_bytes() == b"ORIGINAL" and not written.exists()
        assert not (d / "OLD_TIFFs").exists() and run.err == 0
        written.write_bytes(b"COMPRESSED")
        cz._archive_originals(run, [job], self._outcome())
        assert src.read_bytes() == b"COMPRESSED"
        assert (d / "OLD_TIFFs" / "p.tif").read_bytes() == b"ORIGINAL"
        assert ("INFO", "  -> 1 original(s) archived in OLD_TIFFs after their compressed copy landed") \
            in run.log.lines


# ---------------------------------------------------------------------------
# tiff_workflow_common: the gates fail closed
# ---------------------------------------------------------------------------

def probe(timed_out=False, rc=0, lines=()):
    return lambda args, timeout=30: tw.MagickResult(timed_out, rc, list(lines))


class TestGatesFailClosed:
    """A gate that authorises destroying data: a timeout, a non-zero exit or output that
    cannot be parsed is a failure, never a pass."""

    def test_page_count(self, monkeypatch):
        monkeypatch.setattr(tw, "magick_probe", probe(timed_out=True, rc=-1))
        assert tw.tiff_page_count("x").error == "timeout"
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["", " "]))
        assert tw.tiff_page_count("x").error == "failed"
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["???"]))
        pc = tw.tiff_page_count("x")
        assert not pc.ok and pc.error == "parse:???"
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["3", "3", "3"]))
        assert tw.tiff_page_count("x").count == 3

    def test_zip_integrity(self, monkeypatch, tmp_path):
        f = tmp_path / "a.tif"
        f.write_bytes(b"x")
        for bad in (probe(timed_out=True, rc=-1), probe(rc=1), probe(rc=-1)):
            monkeypatch.setattr(tw, "magick_probe", bad)
            assert tw.zip_integrity(str(f)) is False
        monkeypatch.setattr(tw, "magick_probe", probe(rc=0))
        assert tw.zip_integrity(str(f)) is True

    @pytest.mark.parametrize("compare", [
        tw.MagickResult(True, -1, []), tw.MagickResult(False, 2, ["0"]), tw.MagickResult(False, -1, ["0"]),
        tw.MagickResult(False, 0, []), tw.MagickResult(False, 0, ["nan?"]), tw.MagickResult(False, 1, ["0.25"]),
    ])
    def test_pixel_identical(self, monkeypatch, tmp_path, compare):
        a, b = tmp_path / "a.tif", tmp_path / "b 50%.tif"
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        seen = []

        def fake(args, timeout=30):
            seen.append(list(args))
            if args[0] == "identify":
                return tw.MagickResult(False, 0, ["1"])
            return compare
        monkeypatch.setattr(tw, "magick_probe", fake)
        assert tw.pixel_identical(str(a), str(b)) is False
        cmp_call = next(c for c in seen if c[0] == "compare")
        assert cmp_call[4] == str(b).replace("%", "%%") + "[0]", "a '[n]' spec needs '%' escaped"

    def test_pixel_identical_needs_the_same_page_count_and_every_page(self, monkeypatch, tmp_path):
        a, b = tmp_path / "a.tif", tmp_path / "b.tif"
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        counts = {str(a): 2, str(b): 1}
        compared = []

        def fake(args, timeout=30):
            if args[0] == "identify":
                return tw.MagickResult(False, 0, [str(counts[args[-1]])])
            compared.append(args[3])
            return tw.MagickResult(False, 0, ["0"])
        monkeypatch.setattr(tw, "magick_probe", fake)
        assert tw.pixel_identical(str(a), str(b)) is False, "a page-short copy is not identical"
        counts[str(b)] = 2
        assert tw.pixel_identical(str(a), str(b)) is True
        assert compared == [f"{a}[0]", f"{a}[1]"]

    def test_extra_pages(self, monkeypatch):
        monkeypatch.setattr(tw, "magick_probe", probe(rc=1, lines=["|10|10", "REDUCEDIMAGE|2|2"]))
        assert tw.tiff_has_only_subfile_pages("x", 2) is False
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["|10|10"]))
        assert tw.tiff_has_only_subfile_pages("x", 2) is False, "fewer lines than pages"
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["|10|10", "MASK|10|10"]))
        assert tw.tiff_has_only_subfile_pages("x", 2, ("REDUCEDIMAGE", "REDUCED")) is False
        assert tw.tiff_has_only_subfile_pages("x", 2) is True, "the helper's default list allows MASK"
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["|10|10", "PAGE|4|4"]))
        assert tw.tiff_has_only_subfile_pages("x", 2, ("REDUCEDIMAGE", "REDUCED")) is True, \
            "a strictly smaller page is a thumbnail whatever magick stamped on it"
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["|10|10", "|10|10"]))
        assert tw.tiff_has_only_subfile_pages("x", 2, ("REDUCEDIMAGE", "REDUCED")) is False

    def test_sample_layout(self, monkeypatch):
        def says(rc, text):
            monkeypatch.setattr(tw, "exiftool", lambda args, merge_stderr=False: (rc, text))
        says(1, "[IFD0] PhotometricInterpretation: 2\n")
        assert tw.tiff_sample_layout("x") == (False, [])
        says(0, "[IFD0] SamplesPerPixel: 3\n")
        assert tw.tiff_sample_layout("x") == (False, []), "no photometric = unknown layout"
        says(0, "[IFD1] PhotometricInterpretation: 2\n")
        assert tw.tiff_sample_layout("x") == (False, []), "no IFD0"
        says(0, "[IFD0] PhotometricInterpretation: 2\n[IFD2] PhotometricInterpretation: 2\n")
        assert tw.tiff_sample_layout("x") == (False, []), "a gap in the IFD numbering"
        says(0, "[IFD0]  SamplesPerPixel: 4\n[IFD0]  PhotometricInterpretation: 2\n"
                "[IFD0]  ExtraSamples: 0\n[IFD0]  BitsPerSample: 16 16 16 16\n"
                "[IFD1]  PhotometricInterpretation: 1\n")
        assert tw.tiff_sample_layout("x") == (True, ["rgb/4/16,16,16,16/", "gray/1/1/"])

    def test_keep_type_args(self):
        assert tw.magick_keep_type_args(["rgb/3/8,8,8/"]) == ["-type", "TrueColor"]
        assert tw.magick_keep_type_args(["rgb/4/8,8,8,8/"]) == ["-type", "TrueColor"], "an IR channel"
        assert tw.magick_keep_type_args(["rgb/4/8,8,8,8/2"]) == ["-type", "TrueColorAlpha"]
        assert tw.magick_keep_type_args(["rgb/4/8,8,8,8/1"]) == [
            "-type", "TrueColorAlpha", "-define", "tiff:alpha=associated"]
        assert tw.magick_keep_type_args(["rgb/3/8,8,8/", "gray/1/8/"]) == []
        assert tw.magick_keep_type_args(["rgb/3/8,8,8/", "rgb/4/8,8,8,8/2"]) == []
        assert tw.magick_keep_type_args([]) == []

    def test_layout_kept(self, monkeypatch):
        monkeypatch.setattr(tw, "tiff_sample_layout", lambda p: (False, []))
        assert tw.tiff_sample_layout_kept(RGB, "x") is False
        monkeypatch.setattr(tw, "tiff_sample_layout", lambda p: (True, ["gray/1/8/"]))
        assert tw.tiff_sample_layout_kept(RGB, "x") is False
        monkeypatch.setattr(tw, "tiff_sample_layout", lambda p: (True, RGB + ["rgb/3/8,8,8/"]))
        assert tw.tiff_sample_layout_kept(RGB, "x") is False, "one page more is not the same file"
        assert tw.tiff_sample_layout_kept(RGB, "x", main_page_only=True) is True
        assert tw.tiff_sample_layout_kept([], "x") is False

    def test_associated_alpha(self, monkeypatch):
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["False"]))
        assert tw.associated_alpha_rewritable("x", ["rgb/4/8,8,8,8/1"]) is False
        assert tw.associated_alpha_rewritable("x", ["rgb/4/8,8,8,8/2"]) is True, "unassociated: no probe"
        monkeypatch.setattr(tw, "magick_probe", probe(timed_out=True, rc=-1))
        assert tw.associated_alpha_rewritable("x", ["rgb/4/8,8,8,8/1"]) is False
        monkeypatch.setattr(tw, "magick_probe", probe(lines=["True", "True"]))
        assert tw.associated_alpha_rewritable("x", ["rgb/4/8,8,8,8/1"]) is True

    def test_metadata_container(self, monkeypatch, tmp_path):
        calls = []
        made = {}

        def exiftool(args, merge_stderr=False):
            calls.append(list(args))
            if made.get("write"):
                Path(args[args.index("-o") + 1]).write_bytes(b"MIE")
            return made.get("rc", 0), ""
        monkeypatch.setattr(tw, "exiftool", exiftool)
        assert tw.backup_tiff_metadata("a.tif", "b.tif") == (False, None)
        assert calls == [], "no container needed: nothing runs"
        needed, path = tw.backup_tiff_metadata("a.tif", "A.TIF")
        assert needed and path is None, "exit 0 without a container is a failure"
        made["write"] = True
        needed, path = tw.backup_tiff_metadata("50%off.tif", "other.tif")
        assert needed and path and "%" not in path and os.path.exists(path)
        os.remove(path)
        assert "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet" in calls[-1]
        made["rc"] = 1
        needed, path = tw.backup_tiff_metadata("a.tif", "a.tif")
        assert needed and path is None, "a failed dump leaves no partial container"
        assert not os.path.exists(calls[-1][calls[-1].index("-o") + 1])
        monkeypatch.setattr(tw.tempfile, "gettempdir", lambda: str(tmp_path / "100% temp"))
        assert tw.backup_tiff_metadata("a.tif", "a.tif") == (True, None), "'%' in the temp folder"

    def test_subfile_restore(self, monkeypatch):
        monkeypatch.setattr(tw, "tiff_page_count", lambda p, t=30: tw.PageCount(False, 0, "timeout"))
        assert tw.restore_tiff_subfile_types("a", "b") is False
        monkeypatch.setattr(tw, "tiff_page_count", lambda p, t=30: tw.PageCount(True, 3, ""))
        calls = []

        def exiftool(args, merge_stderr=False):
            calls.append(list(args))
            if "-SubfileType" in args:
                return 0, "[IFD0]  SubfileType: 0\n[IFD2]  SubfileType: 4\n"
            return 0, ""
        monkeypatch.setattr(tw, "exiftool", exiftool)
        assert tw.restore_tiff_subfile_types("a", "b") is True
        write = calls[-1]
        assert "-IFD0:SubfileType#=0" in write, "an explicit 0 is kept, not deleted (round 11)"
        assert "-IFD1:SubfileType=" in write, "a page without the tag gets magick's stamp cleared"
        assert "-IFD2:SubfileType#=4" in write


# ---------------------------------------------------------------------------
# tiff_workflow_common: paths, moves, the command line
# ---------------------------------------------------------------------------

class TestPathsAndMoves:
    def test_exclusion_is_relative_to_the_root(self):
        """Round 14: an absolute match excluded the whole run when a folder ABOVE the root
        (or the root itself) had the name."""
        root = r"E:\Temp\OLD_TIFFs\2019"
        assert tw.path_excluded(root, root, ["OLD_TIFFs", "Temp", "2019"]) is False
        assert tw.path_excluded(root + r"\a", root, ["OLD_TIFFs", "Temp", "2019"]) is False
        assert tw.path_excluded(root + r"\a\old_tiffs\b", root, ["OLD_TIFFs"]) is True
        assert tw.path_excluded(root + r"\My_EXPORT_photos", root, ["_EXPORT"]) is False
        assert tw.path_excluded("E:/Temp/OLD_TIFFs/2019/x/ZIP", "E:/Temp/OLD_TIFFs/2019/", ["zip"]) is True
        assert tw.path_excluded(r"E:\Tempest\zip", r"E:\Temp", ["zip"]) is True, "not under the root: skip"
        assert tw.path_excluded(root + r"\a", root, []) is False

    def test_exclude_folders_takes_names_not_paths(self):
        assert tw.parse_exclude_folders(" _EXPORT ; temp;; ") == ["_EXPORT", "temp"]
        for bad in (r"a\b", "a/b"):
            with pytest.raises(tw.UsageError):
                tw.parse_exclude_folders(bad)

    def test_generated_names_are_short_and_recognised(self):
        """Round 13: two full GUIDs per staged name blew past 260 characters."""
        rid = tw.new_run_id()
        assert len(rid) == 8
        staged = tw.staged_name(rid, "photo.tif")
        sibling = tw.new_temp_sibling_path(r"E:\x\photo.tif", rid)
        assert len(staged) == len("photo.tif") + 22
        assert len(sibling) - len(r"E:\x\photo.tif") == cz.TEMP_SIBLING_CHARS
        assert tw.LEFTOVER_NAME_RE.search(staged) and tw.LEFTOVER_NAME_RE.search(os.path.basename(sibling))
        for photo in ("Scan_20190815_143000.tif", "abcd1234_photo.tif", "photo.tif", "a.b.tif"):
            assert not tw.LEFTOVER_NAME_RE.search(photo), photo

    def test_cleanup_removes_only_this_runs_files(self, tmp_path):
        mine, theirs = tw.new_run_id(), tw.new_run_id()
        for name in (tw.staged_name(mine, "a.tif"), tw.staged_name(theirs, "a.tif"), "a.tif"):
            (tmp_path / name).write_bytes(b"x")
        tw.cleanup_run_files([str(tmp_path), str(tmp_path / "missing")], mine)
        left = sorted(os.listdir(tmp_path))
        assert len(left) == 2 and not any(n.startswith(mine) for n in left)

    def test_replace_takes_a_read_only_destination(self, tmp_path):
        """Round 28 (review): MoveFileEx refuses a read-only destination."""
        src, dst = tmp_path / "new.tif", tmp_path / "photo.tif"
        src.write_bytes(b"NEW")
        dst.write_bytes(b"OLD")
        os.chmod(dst, stat.S_IREAD)
        tw.move_file_replacing(str(src), str(dst))
        assert dst.read_bytes() == b"NEW" and not src.exists()
        assert os.stat(dst).st_mode & stat.S_IWRITE, "the new file is not read-only"

    def test_a_refused_replace_leaves_the_destination_as_it_was(self, tmp_path, monkeypatch):
        src, dst = tmp_path / "new.tif", tmp_path / "photo.tif"
        src.write_bytes(b"NEW")
        dst.write_bytes(b"OLD")
        os.chmod(dst, stat.S_IREAD)

        def refuse(a, b):
            raise PermissionError("in use")
        monkeypatch.setattr(tw.os, "replace", refuse)
        with pytest.raises(OSError):
            tw.move_file_replacing(str(src), str(dst))
        assert dst.read_bytes() == b"OLD"
        assert not os.stat(dst).st_mode & stat.S_IWRITE, "the read-only attribute was not put back"
        os.chmod(dst, stat.S_IWRITE)

    def test_a_move_from_another_volume_copies_next_to_the_destination_first(self, tmp_path, monkeypatch):
        """Round 26: never a copy straight onto the photo's name."""
        src, dst = tmp_path / "stg" / "s.tif", tmp_path / "lib" / "photo.tif"
        src.parent.mkdir()
        dst.parent.mkdir()
        src.write_bytes(b"NEW")
        dst.write_bytes(b"OLD")
        monkeypatch.setattr(tw, "_same_volume", lambda a, b: False)
        seen = []
        real = tw.move_file_replacing

        def spy(a, b):
            seen.append((os.path.dirname(a), b, Path(b).read_bytes()))
            return real(a, b)
        monkeypatch.setattr(tw, "move_file_replacing", spy)
        assert tw.move_file_into_place(str(src), str(dst), "abcd1234") is None
        assert dst.read_bytes() == b"NEW" and not src.exists()
        assert seen == [(str(dst.parent), str(dst), b"OLD")], \
            "the destination must be whole until the one rename from its own folder"
        assert os.listdir(dst.parent) == ["photo.tif"]

    def test_a_short_copy_never_reaches_the_destination(self, tmp_path, monkeypatch):
        src, dst = tmp_path / "s.tif", tmp_path / "photo.tif"
        src.write_bytes(b"NEW DATA")
        dst.write_bytes(b"OLD")
        monkeypatch.setattr(tw, "_same_volume", lambda a, b: False)
        monkeypatch.setattr(tw.shutil, "copy2", lambda a, b: Path(b).write_bytes(b"NE"))
        assert tw.move_file_into_place(str(src), str(dst), "abcd1234") == "size mismatch after copy"
        assert dst.read_bytes() == b"OLD" and src.exists()
        assert sorted(os.listdir(tmp_path)) == ["photo.tif", "s.tif"]

    def _staged(self, tmp_path):
        stg, lib = tmp_path / "stg 伊豆沼 [x]", tmp_path / "lib 50%"
        stg.mkdir()
        lib.mkdir()
        staged = stg / "abcd1234_0123456789ab_photo.tif"
        staged.write_bytes(b"NEW DATA")
        return staged, lib / "photo.tif"

    @pytest.mark.parametrize("same_volume", [True, False])
    def test_land_staged_file(self, tmp_path, monkeypatch, same_volume):
        """The one function every staging loop lands a file with: through
        move_file_into_place, then the destination must exist and have the staged size."""
        monkeypatch.setattr(tw, "_same_volume", lambda a, b: same_volume)
        staged, dest = self._staged(tmp_path)
        dest.write_bytes(b"OLD")
        assert tw.land_staged_file(str(staged), str(dest), "abcd1234") is None
        assert dest.read_bytes() == b"NEW DATA" and not staged.exists()
        assert os.listdir(dest.parent) == ["photo.tif"]

    def test_land_staged_file_reports_what_the_move_left(self, tmp_path, monkeypatch):
        staged, dest = self._staged(tmp_path)
        calls = []

        def says(result, leave=None):
            def move(src, dst, run_id):
                calls.append((src, dst, run_id))
                if leave is not None:
                    Path(dst).write_bytes(leave)
                return result
            monkeypatch.setattr(tw, "move_file_into_place", move)
        says(None)
        assert tw.land_staged_file(str(staged), str(dest), "abcd1234") == ("missing", "")
        assert calls == [(str(staged), str(dest), "abcd1234")]
        says(None, leave=b"NEW DAT")
        assert tw.land_staged_file(str(staged), str(dest), "abcd1234") == ("size", ""), \
            "one byte short is not the staged file"
        says("copy failed: disk full")
        assert tw.land_staged_file(str(staged), str(dest), "abcd1234") == ("error", "copy failed: disk full")
        assert staged.read_bytes() == b"NEW DATA", "a failed move keeps the staged file"

    def test_land_staged_file_without_a_staged_file_or_a_folder(self, tmp_path, monkeypatch):
        staged, dest = self._staged(tmp_path)
        kind, detail = tw.land_staged_file(str(staged) + ".gone", str(dest), "abcd1234")
        assert kind == "error" and detail and not dest.exists()
        deeper = dest.parent / "new folder" / "photo.tif"
        kind, detail = tw.land_staged_file(str(staged), str(deeper), "abcd1234")
        assert kind == "error" and staged.exists(), "the destination folder is the caller's to make"
        assert not deeper.parent.exists()

    def test_listing_skips_hidden_and_links_and_sorts(self, tmp_path):
        root = tmp_path / "写真 [x]"
        (root / "b").mkdir(parents=True)
        (root / "a").mkdir()
        for rel in ("b/2.tif", "a/1.TIF", "top.tif", "a/hidden.tif"):
            (root / rel).write_bytes(b"x")
        if os.name == "nt":
            subprocess.run(["attrib", "+h", str(root / "a" / "hidden.tif")], check=True)
            (root / "hid").mkdir()
            (root / "hid" / "h.tif").write_bytes(b"x")
            subprocess.run(["attrib", "+h", str(root / "hid")], check=True)
        names = [f.name for f in tw.list_files(str(root), True)]
        assert names == ["1.TIF", "2.tif", "top.tif"] if os.name == "nt" else "top.tif" in names
        assert [f.name for f in tw.list_files(str(root), False)] == ["top.tif"]

    @pytest.mark.skipif(os.name != "nt", reason="Windows' 260-character limit")
    def test_listing_sees_past_260_characters(self, tmp_path):
        """A folder past the limit used to be an "unreadable folder": its files were never
        listed, so nothing said they were skipped."""
        deep = tmp_path / "写真 [x]"
        while len(str(deep)) < 270:
            deep = deep / ("d" * 40)
        os.makedirs("\\\\?\\" + str(deep))
        with open("\\\\?\\" + str(deep / "far.tif"), "wb") as fh:
            fh.write(b"x")
        (tmp_path / "写真 [x]" / "near.tif").write_bytes(b"x")
        try:
            unreadable = []
            found = tw.list_files(str(tmp_path / "写真 [x]"), True, unreadable)
            assert sorted(f.name for f in found) == ["far.tif", "near.tif"] and unreadable == []
            far = next(f for f in found if f.name == "far.tif")
            assert far.path == str(deep / "far.tif") and not far.path.startswith("\\\\")
            assert str(deep) in tw.list_dirs(str(tmp_path / "写真 [x]"), True)
        finally:
            # pytest's own cleanup cannot remove a tree this deep
            import shutil
            shutil.rmtree("\\\\?\\" + str(tmp_path / "写真 [x]"), ignore_errors=True)

    def test_stranded_originals(self, tmp_path):
        """Round 25/28: a backup whose place is empty; a stamp is part of the name unless
        the base is in the backup folder too."""
        d = tmp_path / "写真 [x]"
        old = d / "OLD_TIFFs"
        old.mkdir(parents=True)
        for name in ("gone.tif", "here.tif", "astiff.tiff", "Scan_20190815_143000.tif",
                     "dup.tif", "dup_20260101_101010.tif", "notes.txt"):
            (old / name).write_bytes(b"x")
        (d / "here.tif").write_bytes(b"x")
        (d / "astiff.tif").write_bytes(b"x")
        (d / "dup.tif").write_bytes(b"x")
        got = cz.find_stranded_originals(str(d), True, cz.TOOL_OUTPUT_NAMES, [])
        assert [(os.path.basename(s["backup"]), os.path.basename(s["target"])) for s in got] == [
            ("gone.tif", "gone.tif"), ("Scan_20190815_143000.tif", "Scan_20190815_143000.tif")]
        assert cz.find_stranded_originals(str(tmp_path), False, cz.TOOL_OUTPUT_NAMES, []) == [], \
            "mode 0 only looks at its own depth"


class TestCommandLine:
    """The command line the .ps1 backends had: a preset saved by v2.x must mean the same."""

    def test_binding(self):
        opt, bound = tw.parse_args(["-mode", "9", "-InputDir", r"E:\a b", "-SafeMode:$false",
                                    "-DryRun", "-Overwrite:$false", "-workers", "3",
                                    "-SkipLzwAsCompressed:$true", "-SrgbProfile", ""], cz.PARAMS)
        assert opt["Mode"] == 9 and opt["InputDir"] == r"E:\a b" and opt["Workers"] == 3
        assert opt["SafeMode"] is False and opt["DryRun"] is True and opt["Overwrite"] is False
        assert opt["SkipLzwAsCompressed"] is True and opt["SrgbProfile"] == ""
        assert bound == {"Mode", "InputDir", "SafeMode", "DryRun", "Overwrite", "Workers",
                         "SkipLzwAsCompressed", "SrgbProfile"}
        assert opt["ZipSubfolderName"] == "ZIP" and opt["DeleteSource"] is False

    def test_prefixes_and_values(self):
        opt, _ = tw.parse_args(["-DeleteS", "-Mo", "8", "-Mode:8", "-SafeMode", "$true",
                                "-DuplicateAction", "skip", "-OutputDir", "-weird name"], cz.PARAMS)
        assert opt["DeleteSource"] is True and opt["Mode"] == 8
        assert opt["DuplicateAction"] == "Skip", "a choice is matched without case"
        assert opt["OutputDir"] == "-weird name"

    @pytest.mark.parametrize("argv", [
        ["-D"], ["-NoSuchThing"], ["stray"], ["-Workers", "0"], ["-Workers", "65"],
        ["-Workers", "many"], ["-Mode"], ["-SafeMode", "maybe"], ["-DryRun:perhaps"],
        ["-DuplicateAction", "Rename"], ["-OutputDir", "-DryRun"], ["-WorkerMemoryFraction", "nan"],
    ])
    def test_what_cannot_be_bound_is_refused(self, argv):
        with pytest.raises(tw.UsageError):
            tw.parse_args(argv, cz.PARAMS)

    def test_thumbnail_patterns(self):
        for bad in (["-Page", "first"], ["-Quality", "0"], ["-Quality", "101"]):
            with pytest.raises(tw.UsageError):
                tw.parse_args(bad, gt.PARAMS)
        opt, _ = tw.parse_args(["-Page", "all", "-Quality", "100"], gt.PARAMS)
        assert opt["Page"] == "all" and opt["Quality"] == "100"

    def test_a_bad_command_line_exits_1_before_anything_is_touched(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        for module in (cz, cx, gt):
            assert module.main(["-NoSuchThing"]) == 1
        assert "ERROR: unknown parameter -NoSuchThing" in capsys.readouterr().out
        assert os.listdir(tmp_path) == [], "not even a log folder"

    def test_exiftool_gets_its_arguments_on_stdin(self, monkeypatch):
        """Round 27: its command line -- an argfile path included -- is read in the ANSI
        code page."""
        seen = {}

        def run_captured(cmd, timeout=None, input=None):
            seen["cmd"], seen["input"] = cmd, input
            return 0, "ok\r\n".encode(), b"warn"
        monkeypatch.setattr(tw, "run_captured", run_captured)
        rc, out = tw.exiftool(["-s", "G:\\伊豆沼 [FINAL]\\a 50%.tif"], merge_stderr=True)
        assert (rc, out) == (0, "ok\nwarn")
        assert seen["cmd"][1:] == ["-@", "-"]
        assert seen["input"].decode("utf-8") == "-charset\nfilename=utf8\n-s\nG:\\伊豆沼 [FINAL]\\a 50%.tif\n"

    def test_thumbnail_goes_through_the_profile_before_strip(self):
        """Round 9: -colorspace alone reinterprets a ProPhoto file's numbers as sRGB."""
        args = tw.thumbnail_magick_args(r"C:\p.icc", 256)
        assert args.index("-profile") < args.index("-strip") < args.index("-thumbnail")
        assert args[args.index("-thumbnail") + 1] == "256x256>"
        assert "-profile" not in tw.thumbnail_magick_args(None, 64)

    def test_thumbnail_frames_need_digits(self, tmp_path):
        """Round 16: name_thumb-old.jpg is not a frame of -Page all."""
        for n in ("p [1]_thumb-0.jpg", "p [1]_thumb-12.JPG", "p [1]_thumb-old.jpg", "p [1]_thumb-.jpg",
                  "q_thumb-0.jpg"):
            (tmp_path / n).write_bytes(b"x")
        dest = str(tmp_path / "p [1]_thumb.jpg")
        assert sorted(gt.frame_files(dest)) == ["p [1]_thumb-0.jpg", "p [1]_thumb-12.JPG"]
        assert gt.thumb_exists(dest, True) and not gt.thumb_exists(dest, False)
        for name in ("a_thumb", "a_thumb-0", "a_thumb_v2", "a_thumb_v2-3"):
            assert gt.THUMB_NAME_RE.search(name)
        assert not gt.THUMB_NAME_RE.search("a_thumbnail")


# ---------------------------------------------------------------------------
# generate_thumbnails
# ---------------------------------------------------------------------------

class TestThumbnails:
    def test_remove_keeps_a_tiff_larger_than_size(self, monkeypatch):
        """Round 28: -Remove finds thumbnails by NAME, and a 1200x800 hitchhiker_thumb.tif
        was a photo. This script never writes a thumbnail larger than -Size."""
        seen = []

        def says(*lines):
            def fake(args, timeout=30):
                seen.append([str(a) for a in args])
                return tw.MagickResult(False, 0, list(lines))
            monkeypatch.setattr(tw, "magick_probe", fake)
        path = "E:\\伊豆沼 [FINAL] 50%\\hitchhiker_thumb.tif"
        says("1200 800")
        assert gt._own_thumbnail(path, 256) == (False, "1200x800 is larger than -Size 256")
        assert seen[-1][0] == "identify", "a probe (with a timeout), never a conversion"
        assert seen[-1][-1] == path.replace("%", "%%") + "[0]", "a '[n]' spec needs '%' escaped"
        says("256 171")
        assert gt._own_thumbnail(path, 256) == (True, "")
        says("171 257")
        assert gt._own_thumbnail(path, 256) == (False, "171x257 is larger than -Size 256"), \
            "the longer side decides"

    @pytest.mark.parametrize("result", [
        tw.MagickResult(True, -1, []), tw.MagickResult(False, 1, ["64 64"]),
        tw.MagickResult(False, -1, []), tw.MagickResult(False, 0, []),
        tw.MagickResult(False, 0, ["64"]), tw.MagickResult(False, 0, ["64 x"]),
    ])
    def test_remove_keeps_a_tiff_it_cannot_measure(self, monkeypatch, result):
        """Fail closed: a timeout, a non-zero exit or output that is not two numbers."""
        monkeypatch.setattr(tw, "magick_probe", lambda args, timeout=30: result)
        assert gt._own_thumbnail("a_thumb.TIFF", 4096) == (False, "size unreadable")

    def test_remove_does_not_probe_the_other_formats(self, monkeypatch):
        def never(args, timeout=30):
            raise AssertionError("only a .tif thumbnail can be mistaken for a photo")
        monkeypatch.setattr(tw, "magick_probe", never)
        for name in ("a_thumb.jpg", "a_thumb.PNG", "p [1]_thumb-0.jpeg"):
            assert gt._own_thumbnail(name, 32) == (True, "")

    def test_a_taken_thumbnail_name_is_numbered(self):
        """Round 16: with -OutputDir + -Recursive the second p_thumb.jpg was reported
        "SKIP (exists)" and never made."""
        files = [tw.FoundFile(f"E:\\r\\{d}\\p.tif", "p.tif", f"E:\\r\\{d}", 1, 1) for d in "abc"]
        log = NoLog()
        pairs = gt._plan(log, files, "E:\\out [x]", "jpg")
        assert pairs == [("E:\\r\\a\\p.tif", "E:\\out [x]\\p_thumb.jpg"),
                         ("E:\\r\\b\\p.tif", "E:\\out [x]\\p_thumb_v2.jpg"),
                         ("E:\\r\\c\\p.tif", "E:\\out [x]\\p_thumb_v3.jpg")]
        assert log.lines == [("WARN", "RENAME (thumbnail name already taken) | p.tif -> p_thumb_v2.jpg"),
                             ("WARN", "RENAME (thumbnail name already taken) | p.tif -> p_thumb_v3.jpg")]
        beside = gt._plan(NoLog(), files, "", "png")
        assert [d for _s, d in beside] == [f"E:\\r\\{d}\\p_thumb.png" for d in "abc"]
        relative = gt._plan(NoLog(), files[:1], "thumbs", "tif")
        assert relative == [("E:\\r\\a\\p.tif", "E:\\r\\a\\thumbs\\p_thumb.tif")]

    def test_what_a_thumbnail_sends_to_magick(self, tmp_path, monkeypatch):
        """Round 13: '%' is expanded in the output name and in an input with a '[n]' suffix;
        a plain input (-Page all) is opened literally."""
        calls = []

        def magick_run(args):
            calls.append([str(a) for a in args])
            Path(str(args[-1]).replace("%%", "%")).write_bytes(b"T")
            return 0, ""
        monkeypatch.setattr(tw, "magick_run", magick_run)
        src = str(tmp_path / "伊豆沼 [x] 50%off.tif")
        dest = str(tmp_path / "out 100%" / "伊豆沼 [x] 50%off_thumb.jpg")
        cfg = gt.ThumbSettings(64, "85", "jpg", "0", False, None)
        assert gt.make_thumbnail(src, dest, cfg) == \
            "OK | 伊豆沼 [x] 50%off.tif -> 伊豆沼 [x] 50%off_thumb.jpg (1 bytes)"
        call = calls[0]
        assert call[0] == src.replace("%", "%%") + "[0]"
        assert call[-1] == dest.replace("%", "%%")
        assert call[call.index("-quality") + 1] == "85" and "-interlace" in call
        assert call[call.index("-thumbnail") + 1] == "64x64>"
        assert gt.make_thumbnail(src, dest, cfg) == "SKIP (exists) | 伊豆沼 [x] 50%off.tif"
        every_page = cfg._replace(page="all", format="tif")
        tif_dest = dest[:-4] + ".tif"
        gt.make_thumbnail(src, tif_dest, every_page)
        assert calls[-1][0] == src, "a plain input must stay unescaped"
        assert "-quality" not in calls[-1] and calls[-1][-3:-1] == ["-compress", "zip"]
        assert len(calls) == 2
        dry = gt.make_thumbnail(src, dest[:-4] + "_other.jpg", cfg._replace(dry_run=True))
        assert dry == "DRY-RUN | 伊豆沼 [x] 50%off.tif -> 伊豆沼 [x] 50%off_thumb_other.jpg"
        assert len(calls) == 2, "a dry run starts nothing"


# ---------------------------------------------------------------------------
# Static: what the source of the backends must never contain
# ---------------------------------------------------------------------------

def _tree(name):
    return ast.parse((REPO / name).read_text(encoding="utf-8"))


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            yield node, owner, name


class TestSourceRules:
    @pytest.mark.parametrize("name", BACKEND_FILES)
    def test_only_the_common_module_starts_a_process(self, name):
        """Every tool call goes through run_captured (no reader threads, round 21) and
        exiftool() (stdin argfile, round 27)."""
        tree = _tree(name)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert "subprocess" not in imported
        assert not [1 for _n, owner, _f in _calls(tree) if owner == "subprocess"]

    def test_one_place_runs_subprocess(self):
        tree = _tree("tiff_workflow_common.py")
        sites = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for _node, owner, name in _calls(fn):
                if owner == "subprocess" and name in ("run", "Popen", "call", "check_output"):
                    sites.append(fn.name)
        assert sites == ["run_captured"], sites
        src = (REPO / "tiff_workflow_common.py").read_text(encoding="utf-8")
        assert "capture_output" not in src.replace("capture_output=True makes", "")

    @pytest.mark.parametrize("name", ALL_FILES)
    def test_probes_have_a_timeout_and_conversions_do_not_probe(self, name):
        """A bare identify/compare has no timeout: a corrupted file hung the whole run."""
        for node, _owner, fname in _calls(_tree(name)):
            if fname != "magick_run" or not node.args:
                continue
            first = node.args[0]
            literals = [e.value for e in ast.walk(first) if isinstance(e, ast.Constant)
                        and isinstance(e.value, str)]
            assert "identify" not in literals and "compare" not in literals, \
                f"{name}:{node.lineno}: use magick_probe for identify/compare"

    @pytest.mark.parametrize("name", ALL_FILES)
    def test_the_swap_is_one_rename(self, name):
        """os.replace only inside move_file_replacing (which handles a read-only
        destination); never shutil.move, which copies across volumes onto the destination."""
        tree = _tree(name)
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node, owner, fname in _calls(fn):
                assert not (owner == "shutil" and fname == "move"), f"{name}:{node.lineno}"
                if owner == "os" and fname == "replace":
                    assert fn.name == "move_file_replacing", f"{name}:{node.lineno} in {fn.name}"

    @pytest.mark.parametrize("name", ALL_FILES)
    def test_compression_is_read_from_ifd0(self, name):
        """Plain -Compression prints one line per IFD: a compressed thumbnail page made an
        uncompressed main image "already compressed" (round 8)."""
        for node in ast.walk(_tree(name)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.lstrip("-").lower() == "compression":
                    pytest.fail(f"{name}:{node.lineno}: read -IFD0:Compression")

    @pytest.mark.parametrize("name", BACKEND_FILES)
    def test_the_exit_code_is_mains_return_value(self, name):
        """The wizard gates step 2 on the exit code: an error count that does not reach
        sys.exit reads as success (rounds 5-6)."""
        tree = _tree(name)
        last = tree.body[-1]
        assert isinstance(last, ast.If) and ast.unparse(last.test) == "__name__ == '__main__'"
        assert ast.unparse(last.body[0]) == "sys.exit(main())"
        exits = [n.lineno for n, owner, f in _calls(tree) if owner == "sys" and f == "exit"]
        assert len(exits) == 1, f"{name}: sys.exit outside the entry point at lines {exits}"
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        for ret in [n for n in ast.walk(main) if isinstance(n, ast.Return)]:
            assert ret.value is not None, f"{name}:{ret.lineno}: a bare return exits 0"

    @pytest.mark.parametrize("name", BACKEND_FILES)
    def test_run_functions_never_return_nothing(self, name):
        tree = _tree(name)
        for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name in ("run", "run_main", "run_modes", "remove_thumbnails")]:
            for ret in [n for n in ast.walk(fn) if isinstance(n, ast.Return)]:
                assert ret.value is not None, f"{name}:{ret.lineno}: bare return in {fn.name}"
            assert isinstance(fn.body[-1], ast.Return), f"{name}: {fn.name} can fall off its end"

    def test_the_overlay_reader_is_the_wizards(self):
        """One grammar: the wizard plans with the values the backend will run with."""
        import inspect
        import convert_tiff as ct
        for fn in ("_overlay_object_pairs", "_overlay_bound", "_overlay_value"):
            assert inspect.getsource(getattr(ct, fn)) == inspect.getsource(getattr(tw, fn)), fn
        assert ct._SETTINGS_OVERLAY_FILE == tw.SETTINGS_OVERLAY_FILE
        assert ct._SETTINGS_OVERLAY_ENV == tw.SETTINGS_OVERLAY_ENV
