"""audit round 28 (Fable bug sweep, 2026-11-11) -- the Python side.

#1/#2  magick collapses an all-neutral RGB page to one gray channel (and writes a neutral
       RGB+IR scan with half its samples); the padded conversion and the Delete OLD_TIFFs
       gate compared pixels through magick's own decode and could not see it.
#3     UTF-8 IPTC text (伊豆沼) came back '???' after a trip through the MIE container.
#5     padded 16 -> 8: the original left its place before the converted file arrived.
#10/16 renamed-backup rules (move_old_backups.py, the wizard's stranded scan).
#11    the Delete OLD_TIFFs gate read EXIF only (XMP/IPTC loss purged the last copy).
#13-15, #21: overlay key case, AutoFind ';', relative preset folders, -SrgbProfile auto,
       manifest folder names, the unattended refusal, Diagnose preset details.

Real files from the owner's fixtures (E:\\TESTE, copied, never modified) where the bug only
shows on real data; every path is hostile (Japanese, [ ], spaces, '%' where it matters).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import convert_tiff as ct  # noqa: E402
import move_old_backups as mop  # noqa: E402
from test_presets import _cfg as _preset_cfg, _compress_entry, calls  # noqa: E402,F401
from test_diagnose_preset import (  # noqa: E402,F401
    _cfg as _diag_cfg, _entry as _diag_entry, _padded, _settings, _tree, home)

FIXTURES = Path(os.environ.get("TW_FIXTURES") or r"E:\TESTE\TIFF_scans_varied")
NEUTRAL_RGB = FIXTURES / "rgb16_photoshop_noicc__000015990010.tif"
NEUTRAL_RGBI = FIXTURES / "rgbi16_vuescan_srgb__0019.tif"
HOSTILE = Path("190815_Film_伊豆沼_夏 [FINAL]") / "NORITSU"

requires_tools = pytest.mark.skipif(
    shutil.which("magick") is None or shutil.which("exiftool") is None,
    reason="ImageMagick + exiftool required")
requires_fixtures = pytest.mark.skipif(
    not (NEUTRAL_RGB.is_file() and NEUTRAL_RGBI.is_file()),
    reason="owner's real-photo fixtures not available")


@pytest.fixture(autouse=True)
def _no_overlay(monkeypatch):
    monkeypatch.setenv("TIFFWORKFLOW_SETTINGS", "")


def _magick(*args):
    subprocess.run(["magick", *map(str, args)], check=True, capture_output=True, timeout=300)


def _exif_write(path, *tags):
    r = ct._run_exiftool(["-q", "-q", "-overwrite_original", *tags, str(path)], 120)
    assert r.returncode == 0


def _tags(path, *names):
    r = ct._run_exiftool(["-j", "-G1", *names, str(path)], 120, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)[0]


def _ifd0_layout(path):
    """(SamplesPerPixel, PhotometricInterpretation) of IFD0, read here (not with the code
    under test) so the pre-fix run fails on the bug, not on a missing helper."""
    t = _tags(path, "-n", "-IFD0:SamplesPerPixel", "-IFD0:PhotometricInterpretation")
    return t.get("IFD0:SamplesPerPixel", 1), t.get("IFD0:PhotometricInterpretation")


def _hostile_file(tmp_path: Path, name: str, build) -> Path:
    """Build with an ASCII name (magick globs '[ ]' in outputs), then move it under the
    hostile folder."""
    tmp_path.joinpath("_b").mkdir(exist_ok=True)
    plain = tmp_path / "_b" / "build.tif"
    build(plain)
    dst = tmp_path / HOSTILE / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(plain), str(dst))
    return dst


def _padded_crop(src: Path):
    """A padded (8-bit data in 16-bit) crop of a REAL neutral scan, kept RGB(+IR)."""
    def build(out):
        _magick(src, "-crop", "640x400+900+900", "+repage", "-depth", "8", "-depth", "16",
                "-type", "TrueColor", "-compress", "none", out)
    return build


# --- #1/#2: the padded conversion keeps the sample layout -----------------------------


@requires_tools
@requires_fixtures
@pytest.mark.parametrize("src, spp", [(NEUTRAL_RGB, 3), (NEUTRAL_RGBI, 4)],
                         ids=["neutral_rgb", "neutral_rgb_ir"])
def test_padded_conversion_keeps_rgb_of_a_neutral_real_scan(tmp_path, src, spp):
    f = _hostile_file(tmp_path, "0014_14 (2).tif", _padded_crop(src))
    assert _ifd0_layout(f) == (spp, 2)
    staging = tmp_path / "staging"
    staging.mkdir()
    res = ct._process_single_padded(f, staging)
    status, tmp8 = res[2], res[8]
    assert status == "ok", res
    assert _ifd0_layout(tmp8) == (spp, 2), "the 8-bit file lost its RGB samples"


def test_layout_gate_refuses_a_collapsed_page():
    rgb = [{"cls": "rgb", "spp": 3, "alpha": (), "bits": "16 16 16"}]
    gray = [{"cls": "gray", "spp": 1, "alpha": (), "bits": "16"}]
    assert ct._sample_layout_diff(rgb, gray)
    assert ct._sample_layout_diff(rgb, None)
    assert ct._sample_layout_diff(rgb, rgb + gray)          # a page more or less
    assert ct._sample_layout_diff(rgb, [dict(rgb[0], bits="8 8 8")])
    assert not ct._sample_layout_diff(rgb, [dict(rgb[0], bits="8 8 8")], compare_bits=False)
    assert ct._magick_keep_type_args(rgb) == ["-type", "TrueColor"]
    assert ct._magick_keep_type_args(rgb + gray) == []      # -type is global: mixed gets none
    assoc = [{"cls": "rgb", "spp": 4, "alpha": ("1",), "bits": "16 16 16 16"}]
    assert ct._magick_keep_type_args(assoc) == ["-type", "TrueColorAlpha",
                                                "-define", "tiff:alpha=associated"]


# --- #1 + #11: the Delete OLD_TIFFs gate --------------------------------------------


@requires_tools
@requires_fixtures
def test_purge_gate_sees_a_gray_collapse_and_lost_xmp_iptc(tmp_path):
    def build_old(out):
        _magick(NEUTRAL_RGB, "-crop", "640x400+900+900", "+repage", "-type", "TrueColor",
                "-compress", "none", out)
    old = _hostile_file(tmp_path, "OLD_TIFFs/0014_14 (2).tif", build_old)
    _exif_write(old, "-IPTC:CodedCharacterSet=UTF8", "-IPTC:Keywords=伊豆沼",
                "-IPTC:Caption-Abstract=伊豆沼の夏", "-XMP-dc:Subject=伊豆沼", "-EXIF:Make=FUJIFILM")
    new = tmp_path / HOSTILE / "0014_14 (2).tif"

    def rewrite(type_args):
        plain = tmp_path / "_b" / "rw.tif"
        plain.unlink(missing_ok=True)
        _magick(old, "-compress", "zip", *type_args, plain)
        ct._run_exiftool(["-q", "-overwrite_original", "-tagsfromfile", str(old), "-all:all",
                          "-IPTC:CodedCharacterSet<IPTC:CodedCharacterSet", "-unsafe",
                          str(plain)], 120)
        shutil.move(str(plain), str(new))

    rewrite(["-type", "TrueColor"])                       # a correct copy passes
    ok, detail = ct._compare_tiff_metadata(old, new)
    assert ok, detail

    _exif_write(new, "-XMP:all=", "-IPTC:all=")          # keywords and captions lost
    ok, detail = ct._compare_tiff_metadata(old, new)
    assert not ok and "XMP_IPTC" in detail, detail

    rewrite(["-type", "TrueColor"])
    _exif_write(new, "-IPTC:Keywords=???")                # the '???' of finding #3
    assert not ct._compare_tiff_metadata(old, new)[0]

    rewrite([])                                           # magick's gray collapse
    assert _ifd0_layout(new) == (1, 1), "precondition: magick collapsed it"
    ok, detail = ct._compare_tiff_metadata(old, new)
    assert not ok and "SAMPLE_LAYOUT" in detail, detail


# --- #3: UTF-8 IPTC through the '%' container ---------------------------------------


@requires_tools
@requires_fixtures
def test_padded_conversion_keeps_japanese_iptc_through_the_mie_container(tmp_path):
    f = _hostile_file(tmp_path, "50%off (2).tif", _padded_crop(NEUTRAL_RGB))
    _exif_write(f, "-IPTC:CodedCharacterSet=UTF8", "-IPTC:Keywords=伊豆沼",
                "-IPTC:Caption-Abstract=伊豆沼の夏", "-XMP-dc:Subject=伊豆沼")
    staging = tmp_path / "staging"
    staging.mkdir()
    res = ct._process_single_padded(f, staging)
    assert res[2] == "ok" and res[6], res
    t = _tags(res[8], "-IPTC:Keywords", "-IPTC:Caption-Abstract", "-XMP-dc:Subject")
    assert t.get("IPTC:Keywords") == "伊豆沼", t
    assert t.get("IPTC:Caption-Abstract") == "伊豆沼の夏", t


# --- #5: the padded swap never leaves the place empty -------------------------------


class _Kill(BaseException):
    """Stands for the process dying: no `except Exception` catches it."""


def _fake_padded_result(staging_holder):
    def fake(tiff_path, staging):
        tmp8 = staging / f"tmp8_{tiff_path.name.replace('[', '(').replace(']', ')')}"
        tmp8.write_bytes(b"z" * 100)
        return (tiff_path.name, tiff_path.parent, "ok", 2000, 100, 95.0, True, None, tmp8)
    return fake


@pytest.mark.parametrize("kill_on", ["into_place", "backup"])
def test_padded_swap_killed_midway_leaves_a_photo_in_place(tmp_path, monkeypatch, kill_on):
    photo = tmp_path / HOSTILE / "0014_14 (2).tif"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"x" * 2000)
    monkeypatch.setattr(ct, "_process_single_padded", _fake_padded_result(None))
    monkeypatch.setattr(ct, "_memory_budget", lambda *a, **k: (None, {}))
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)

    real_replace, real_move, real_link = os.replace, shutil.move, os.link

    def is_final(dst):
        return os.path.normcase(os.path.abspath(str(dst))) == os.path.normcase(str(photo))

    def is_backup(dst):
        return "OLD_PADDED" in str(dst)

    def guard(real):
        def wrapped(src, dst, *a, **k):
            if kill_on == "into_place" and is_final(dst):
                raise _Kill()
            if kill_on == "backup" and is_backup(dst):
                raise _Kill()
            return real(src, dst, *a, **k)
        return wrapped

    monkeypatch.setattr(os, "replace", guard(real_replace))
    monkeypatch.setattr(shutil, "move", guard(real_move))
    monkeypatch.setattr(os, "link", guard(real_link))
    cfg = SimpleNamespace(config=ct.ToolConfig())
    with pytest.raises(_Kill):
        ct._compress_padded_files([photo], tmp_path / "temp", workers=1, cfg=cfg)
    assert photo.exists(), "the photo's place is EMPTY after a kill"
    assert photo.read_bytes() in (b"x" * 2000, b"z" * 100)


def test_padded_swap_keeps_backup_and_replaces_atomically(tmp_path, monkeypatch):
    photo = tmp_path / HOSTILE / "0014_14 (2).tif"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"x" * 2000)
    monkeypatch.setattr(ct, "_process_single_padded", _fake_padded_result(None))
    monkeypatch.setattr(ct, "_memory_budget", lambda *a, **k: (None, {}))
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ct.ToolConfig())
    done = ct._compress_padded_files([photo], tmp_path / "temp", workers=1, cfg=cfg)
    assert done["converted"] == 1 and done.get("failed", []) == []
    assert photo.read_bytes() == b"z" * 100
    assert (photo.parent / "OLD_PADDED" / photo.name).read_bytes() == b"x" * 2000
    leftovers = [p.name for p in photo.parent.iterdir() if p.is_file() and p != photo]
    assert leftovers == [], leftovers


def test_padded_swap_on_a_locked_original_leaves_no_copy_in_old_padded(tmp_path, monkeypatch):
    """P4: shutil.move fell back to copy + delete on a locked file and left a full copy in
    OLD_PADDED/ while reporting the backup as failed."""
    photo = tmp_path / HOSTILE / "0014_14 (2).tif"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"x" * 2000)
    monkeypatch.setattr(ct, "_process_single_padded", _fake_padded_result(None))
    monkeypatch.setattr(ct, "_memory_budget", lambda *a, **k: (None, {}))
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ct.ToolConfig())
    with open(photo, "rb"):                 # open without delete sharing (Windows)
        done = ct._compress_padded_files([photo], tmp_path / "temp", workers=1, cfg=cfg)
    assert photo.read_bytes() == b"x" * 2000
    if sys.platform == "win32":
        assert done["converted"] == 0, done
        copies = list((photo.parent / "OLD_PADDED").glob("*")) if (photo.parent / "OLD_PADDED").exists() else []
        assert copies == [], copies
    assert not [p for p in photo.parent.iterdir() if p.is_file() and p != photo]


@pytest.mark.skipif(sys.platform != "win32", reason="the read-only attribute blocks a rename on Windows only")
def test_padded_swap_replaces_a_read_only_original(tmp_path, monkeypatch):
    """Review of round 28: os.replace refuses a READ-ONLY destination, and the hard link in
    OLD_PADDED/ shares the attribute, so it could not be unlinked either -- the conversion
    failed (v2.9.0 converted the file) and left a "backup" of a swap that never happened."""
    import stat
    photo = tmp_path / HOSTILE / "0014_14 (2).tif"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"x" * 2000)
    os.chmod(photo, stat.S_IREAD)
    monkeypatch.setattr(ct, "_process_single_padded", _fake_padded_result(None))
    monkeypatch.setattr(ct, "_memory_budget", lambda *a, **k: (None, {}))
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ct.ToolConfig())
    backup = photo.parent / "OLD_PADDED" / photo.name
    try:
        done = ct._compress_padded_files([photo], tmp_path / "temp", workers=1, cfg=cfg)
        assert done["converted"] == 1 and done.get("failed", []) == [], done
        assert photo.read_bytes() == b"z" * 100
        assert os.access(photo, os.W_OK), "the converted file must not be read-only"
        assert backup.read_bytes() == b"x" * 2000
        assert not os.access(backup, os.W_OK), "the original keeps its read-only mark"
        assert [p.name for p in photo.parent.iterdir() if p.is_file()] == [photo.name]
    finally:
        for p in (photo, backup):
            if p.exists():
                os.chmod(p, stat.S_IWRITE)


def test_padded_swap_that_fails_on_a_read_only_original_leaves_it_as_it_was(tmp_path, monkeypatch):
    """The swap still fails when the file is open elsewhere: no link stays in OLD_PADDED/ and
    the original is read-only again."""
    import stat
    photo = tmp_path / HOSTILE / "0014_14 (2).tif"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"x" * 2000)
    os.chmod(photo, stat.S_IREAD)
    monkeypatch.setattr(ct, "_process_single_padded", _fake_padded_result(None))
    monkeypatch.setattr(ct, "_memory_budget", lambda *a, **k: (None, {}))
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    cfg = SimpleNamespace(config=ct.ToolConfig())
    try:
        with open(photo, "rb"):                 # open without delete sharing (Windows)
            done = ct._compress_padded_files([photo], tmp_path / "temp", workers=1, cfg=cfg)
        assert photo.read_bytes() == b"x" * 2000
        if sys.platform == "win32":
            assert done["converted"] == 0 and len(done["failed"]) == 1, done
            assert not os.access(photo, os.W_OK), "the original lost its read-only mark"
            old = photo.parent / "OLD_PADDED"
            assert not old.exists() or list(old.glob("*")) == []
        assert [p.name for p in photo.parent.iterdir() if p.is_file()] == [photo.name]
    finally:
        for p in photo.parent.rglob("*"):
            if p.is_file():
                os.chmod(p, stat.S_IWRITE)


# --- #10 / #16: renamed-backup rules -------------------------------------------------


def _put(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_wizard_stranded_scan_keeps_a_stamp_that_is_the_photos_own_name(tmp_path):
    cam = tmp_path / HOSTILE
    old = cam / "OLD_TIFFs"
    own = _put(old / "Scan_20190815_143000.tif")          # its own name, nothing in place
    phone = _put(old / "IMG_20190815_143105.tif")         # an unrelated IMG.tif is there
    _put(cam / "IMG.tif")
    real_copy = _put(old / "d_20260803_081300.tif")       # a real collision copy of d.tif
    _put(old / "d.tif")
    _put(cam / "d.tif")
    got = {b.name: t.name for b, t in ct._stranded_backups([old])}
    assert got.get(own.name) == own.name, got
    assert got.get(phone.name) == phone.name, got
    assert real_copy.name not in got and "d.tif" not in got, got


def test_move_old_backups_keeps_a_v2_in_old_tiffs(tmp_path):
    cam = tmp_path / HOSTILE
    v2 = _put(cam / "OLD_TIFFs" / "portrait_v2.tif")
    _put(cam / "portrait.tif")
    assert mop.place_is_empty(v2)
    padded_v2 = _put(cam / "OLD_PADDED" / "p_v2.tif")
    _put(cam / "OLD_PADDED" / "p.tif")
    _put(cam / "p.tif")
    assert not mop.place_is_empty(padded_v2)


# --- #13: overlay keys the PowerShell readers refuse -----------------------------------


@pytest.mark.parametrize("body", [
    {"compress_tiff_zip": {"CapWorkers": 2, "capworkers": 3}},
    {"compress_tiff_zip": {"": 1}},
    {"compress_tiff_zip": {"CapWorkers": 2}, "COMPRESS_TIFF_ZIP": {}},
])
def test_overlay_refuses_case_duplicate_or_empty_keys(tmp_path, body):
    f = tmp_path / ".tiff_workflow_settings.json"
    f.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError):
        ct._settings_overlay_section(f, "compress_tiff_zip")


def test_overlay_exact_duplicate_still_reads(tmp_path):
    f = tmp_path / ".tiff_workflow_settings.json"
    f.write_text('{"compress_tiff_zip": {"CapWorkers": 2, "CapWorkers": 3}}', encoding="utf-8")
    assert ct._settings_overlay_section(f, "compress_tiff_zip") == {"CapWorkers": 3}


# --- #14: AutoFind and ';' ------------------------------------------------------------


def test_autofind_skips_a_folder_with_a_semicolon(tmp_path):
    root = tmp_path / "伊豆沼 [FINAL]"
    _put(root / "2019;S5pro" / "fuji.tif")
    _put(root / "2019" / "scan_not_fuji.tif")
    _put(root / "2020_S5pro" / "ok.tif")
    found = ct.find_folders_by_pattern(root, ["S5pro"])
    assert [p.name for p in found] == ["2020_S5pro"], found


# --- #15: a relative folder is stored absolute ----------------------------------------


def test_step_folder_returns_an_absolute_path(tmp_path, monkeypatch):
    (tmp_path / "伊豆沼 [FINAL]").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "伊豆沼 [FINAL]")
    cfg = SimpleNamespace(config=ct.ToolConfig())
    p = ct.step_folder(cfg)
    assert p is not None and p.is_absolute(), p
    assert p == tmp_path / "伊豆沼 [FINAL]"


# --- #21: wizard details ------------------------------------------------------------


def test_srgb_profile_auto_for_this_run_is_passed():
    wf = {"mode": 1, "workers": 1, "advanced": {"compress": {"SrgbProfile": ""}}}
    argv = ct._emit_all("compress", wf, folders=[Path("C:/x")])
    i = argv.index("-SrgbProfile")
    assert argv[i + 1] == "", argv
    # not edited: not passed
    argv = ct._emit_all("compress", {"mode": 1, "workers": 1}, folders=[Path("C:/x")])
    assert "-SrgbProfile" not in argv


def test_manifest_collision_reads_the_backend_folder_names(monkeypatch):
    names = {"ZipSuffix": "_ARC", "ZipSubfolderName": "ARC"}
    real = ct._backend_default
    monkeypatch.setattr(ct, "_backend_default",
                        lambda s, p: names[p] if p in names else real(s, p))
    root = Path("C:/lib")
    assert ct._resolve_output_folder(root / "Shoot_TIFF" / "a.tif", 4, root, "") == root / "Shoot_ARC"
    assert ct._resolve_output_folder(root / "TIFF" / "a.tif", 4, root, "") == root / "ARC"
    assert ct._resolve_output_folder(root / "s" / "x" / "a.tif", 5, root, "") == root / "s" / "ARC"


@pytest.mark.parametrize("spelling", ["-deletesource", "-DeleteS", "-DeleteSource:$true", "-De"])
def test_unattended_refusal_reads_any_delete_source_spelling(tmp_path, calls, spelling):
    entry = _compress_entry(tmp_path, mode=8, delete_source=True)
    entry["commands"] = [[spelling if a == "-DeleteSource" else a for a in c]
                         for c in entry["commands"]]
    assert ct._run_journal(_preset_cfg(), entry, "Preset: delete", unattended=True) == 1
    assert calls == []


def test_unattended_refuses_mode_8_without_the_switch(tmp_path, calls):
    entry = _compress_entry(tmp_path, mode=8, delete_source=False)
    assert ct._run_journal(_preset_cfg(), entry, "Preset: m8", unattended=True) == 1
    assert calls == []
    entry = _compress_entry(tmp_path, mode=9, delete_source=False)
    assert ct._run_journal(_preset_cfg(), entry, "Preset: m9", unattended=True) == 0
    entry = _compress_entry(tmp_path, mode=8, delete_source=True)
    entry["commands"] = [[("-DeleteSource:$false" if a == "-DeleteSource" else a) for a in c]
                         for c in entry["commands"]]
    entry["commands"] = [[("9" if a == "8" else a) for a in c] for c in entry["commands"]]
    assert ct._run_journal(_preset_cfg(), entry, "Preset: off", unattended=True) == 0


def test_diagnose_status_ignores_the_word_padded_in_a_path():
    detail = r"magick identify failed: unable to open G:\padded_scans\x.tif"
    assert ct._diagnose_status(False, detail) == "error"
    assert ct._diagnose_status(False, "padded 8-bit (round-trip RMSE=0)") == "padded"
    assert ct._diagnose_status(False, "8-bit (not 16-bit)") == "other"


requires_cli = pytest.mark.skipif(shutil.which("magick") is None or shutil.which("exiftool") is None,
                                  reason="ImageMagick + exiftool required")


@requires_cli
def test_diagnose_preset_convert_padded_string_false_never_converts(home):
    t = _tree(home)
    _padded(t["padded"])
    before = t["padded"].read_bytes()
    entry = _diag_entry({**_settings(home), "convert_padded": "false"})
    assert ct._run_journal(_diag_cfg(), entry, "Preset: padded", unattended=True) == 0
    assert t["padded"].read_bytes() == before
    assert not any(home.rglob("OLD_PADDED"))


@requires_cli
def test_diagnose_preset_failed_conversion_exits_1_and_names_the_file(home, monkeypatch):
    t = _tree(home)
    _padded(t["padded"])
    before = t["padded"].read_bytes()

    def failing(tiff_path, staging):
        return (tiff_path.name, tiff_path.parent, "integrity_error", None, None, None,
                False, None, staging / "nothing.tif")
    monkeypatch.setattr(ct, "_process_single_padded", failing)
    entry = _diag_entry({**_settings(home), "convert_padded": True})
    assert ct._run_journal(_diag_cfg(), entry, "Preset: padded", unattended=True) == 1
    assert t["padded"].read_bytes() == before
    report = sorted((home / "Logs" / "convert_tiff").glob("diagnose_*.log"))[-1]
    text = report.read_text(encoding="utf-8")
    assert "Conversion FAILED" in text and str(t["padded"]) in text, text


@requires_cli
def test_diagnose_preset_with_an_unusable_temp_dir_does_not_crash(home, monkeypatch):
    t = _tree(home)
    _padded(t["padded"])
    blocker = home / "not_a_dir"
    blocker.write_text("x")
    entry = _diag_entry({**_settings(home), "convert_padded": True,
                         "temp_dir": str(blocker / "tmp")})
    assert ct._run_journal(_diag_cfg(), entry, "Preset: padded", unattended=True) == 0
