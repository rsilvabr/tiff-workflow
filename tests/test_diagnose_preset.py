"""
Diagnose as a preset (workflow 7 journalled, report only).

Workflow 7 used to be a Python-only workflow outside the journal, so it could not be
repeated or saved as a preset. It now records its settings (`diagnose`) and a repeat
or preset re-scans the saved roots and REPORTS: it never runs the 16 -> 8-bit
conversion, attended or not. Two new questions narrow the search -- a folder path
the file must sit under (`_EXPORT/TIFF16`) and file-name patterns to skip
(`*NXSTUDIO*`) -- and a cache keyed by size + mtime spares the files already checked.

The scan tests are pure Python; the run tests need ImageMagick (real padded and real
16-bit TIFFs, real round-trip check) and the CLI test needs the whole wizard.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import convert_tiff as ct

REPO = Path(__file__).resolve().parent.parent

requires_magick = pytest.mark.skipif(shutil.which("magick") is None,
                                     reason="ImageMagick (magick) required")
requires_cli = pytest.mark.skipif(
    shutil.which("magick") is None or shutil.which("exiftool") is None
    or (shutil.which("pwsh") is None and shutil.which("powershell") is None),
    reason="ImageMagick + exiftool + PowerShell required for the CLI run")


# --- Helpers --------------------------------------------------------


def _touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _padded(path: Path) -> Path:
    """8-bit data stored as 16-bit -- what an 8-bit JPEG exported as TIFF16 gives."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp8 = path.with_name("_m8_" + path.stem + ".tif")
    subprocess.run(["magick", "-size", "48x48", "gradient:red-blue", "-depth", "8", str(tmp8)],
                   capture_output=True, check=True, timeout=120)
    subprocess.run(["magick", str(tmp8), "-depth", "16", str(path)],
                   capture_output=True, check=True, timeout=120)
    tmp8.unlink()
    return path


def _real16(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["magick", "-size", "48x48", "plasma:fractal", "-depth", "16", str(path)],
                   capture_output=True, check=True, timeout=120)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _settings(root, scope="_EXPORT/TIFF16", skip="*NXSTUDIO*"):
    return {"roots": [str(root)], "scope": scope, "skip_names": skip, "workers": 2,
            "temp_dir": None, "compress_tmp": "none"}


def _entry(settings):
    return {"kind": "diagnose", "label": ct._diagnose_label(settings), "commands": None,
            "time": "2026-10-17 12:00:00", "manifest_path": None, "manifest_workflow": None,
            "manifest_default_mode": None, "autofind": None, "diagnose": settings}


def _cfg():
    return SimpleNamespace(config=ct.ToolConfig(ps_name="pwsh", ps_major=7),
                           save_config=lambda: None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Config, cache and the report's Logs\\ all under tmp_path; no prompt may run."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)

    def no_prompt(*a, **k):
        raise AssertionError("an unattended Diagnose preset prompted")
    monkeypatch.setattr("builtins.input", no_prompt)
    return tmp_path


def _tree(root: Path):
    """The owner's layout: shoot folder with TIFFs beside _EXPORT and TIFF16 inside."""
    shoot = root / "2025" / "250118_Shoot [FINAL]"
    return {
        "padded": shoot / "_EXPORT" / "TIFF16" / "DSC_0001.tif",
        "real": shoot / "_EXPORT" / "TIFF16" / "DSC_0002.tif",
        "nx": shoot / "_EXPORT" / "TIFF16" / "DSC_0003_NXstudio.TIF",
        "beside": shoot / "DSC_0004.tif",
        "other_export": shoot / "_EXPORT" / "AdobeRGB" / "DSC_0005.tif",
        "backup": shoot / "_EXPORT" / "TIFF16" / "OLD_PADDED" / "DSC_0006.tif",
    }


# --- Discovery ------------------------------------------------------


class TestTargets:
    def test_scope_keeps_only_files_under_export_tiff16(self, tmp_path):
        t = _tree(tmp_path)
        for p in t.values():
            _touch(p)
        found = ct._diagnose_targets([tmp_path], "_EXPORT/TIFF16", "")
        assert set(found) == {t["padded"], t["real"], t["nx"]}, \
            "only TIFF16 files, never the backup folder, the shoot folder or another export"

    def test_scope_accepts_either_slash_and_any_case(self, tmp_path):
        t = _tree(tmp_path)
        for p in t.values():
            _touch(p)
        a = ct._diagnose_targets([tmp_path], "_export\\tiff16", "")
        b = ct._diagnose_targets([tmp_path], "_EXPORT/TIFF16", "")
        assert a == b and a

    def test_skip_names_is_case_insensitive(self, tmp_path):
        t = _tree(tmp_path)
        for p in t.values():
            _touch(p)
        found = ct._diagnose_targets([tmp_path], "_EXPORT/TIFF16", "*NXSTUDIO*")
        assert t["nx"] not in found, "DSC_0003_NXstudio.TIF must match *NXSTUDIO*"
        assert set(found) == {t["padded"], t["real"]}

    def test_several_skip_patterns(self, tmp_path):
        t = _tree(tmp_path)
        for p in t.values():
            _touch(p)
        found = ct._diagnose_targets([tmp_path], "_EXPORT/TIFF16", "*nxstudio*; DSC_0001.*")
        assert found == [t["real"]]

    def test_root_already_inside_the_scope(self, tmp_path):
        t = _tree(tmp_path)
        for p in t.values():
            _touch(p)
        root = t["padded"].parent  # ...\_EXPORT\TIFF16 itself
        found = ct._diagnose_targets([root], "_EXPORT/TIFF16", "")
        assert set(found) == {t["padded"], t["real"], t["nx"]}

    def test_no_scope_keeps_the_old_exclusions(self, tmp_path):
        """Without a scope the search is what workflow 7 always did: the export tree
        and the toolkit's own folders are skipped below the root."""
        t = _tree(tmp_path)
        for p in t.values():
            _touch(p)
        found = ct._diagnose_targets([tmp_path], "", "")
        assert found == [t["beside"]]

    def test_several_roots(self, tmp_path):
        a = _touch(tmp_path / "2024" / "x" / "_EXPORT" / "TIFF16" / "a.tif")
        b = _touch(tmp_path / "2025" / "y" / "_EXPORT" / "TIFF16" / "b.tif")
        _touch(tmp_path / "2026" / "z" / "_EXPORT" / "TIFF16" / "c.tif")
        found = ct._diagnose_targets([tmp_path / "2024", tmp_path / "2025"], "_EXPORT/TIFF16")
        assert found == [a, b]


# --- Journal --------------------------------------------------------


class TestJournal:
    def test_workflow_7_journals_its_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
        t = _tree(tmp_path)
        for p in t.values():
            _touch(p)
        monkeypatch.setattr(ct, "_memory_capped_pool_workers", lambda req, *a, **k: req)
        monkeypatch.setattr(ct, "_is_real_16bit", lambda *a, **k: (True, 0.5, "real 16-bit"))
        answers = iter([str(tmp_path), "_EXPORT/TIFF16", "*NXSTUDIO*", "3", "", "1"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        cfg = _cfg()

        assert ct.run_diagnose_tiffs(cfg) is True

        snap = ct._journal_snapshot(cfg)
        assert snap["kind"] == "diagnose"
        assert snap["commands"] is None
        assert snap["diagnose"] == {"roots": [str(tmp_path.resolve())],
                                    "scope": "_EXPORT/TIFF16", "skip_names": "*NXSTUDIO*",
                                    "workers": 3, "temp_dir": None, "compress_tmp": "none"}
        assert "[report only]" in snap["label"]
        assert ct._journal_runnable(snap)

    def test_a_later_workflow_clears_the_diagnose_settings(self):
        cfg = _cfg()
        ct._save_last_run(cfg, "diagnose", "d", None, diagnose=_settings("C:\\x"))
        ct._save_last_run(cfg, "compress", "c", [["pwsh", "-File", "x.ps1"]])
        assert cfg.config.last_diagnose is None

    def test_a_diagnose_preset_survives_the_config_sanitiser(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".convert_tiff_config.json").write_text(json.dumps({"presets": {
            "padded": _entry(_settings(tmp_path)),
            "broken": {**_entry(_settings(tmp_path)), "diagnose": {"roots": []}},
        }}), encoding="utf-8")
        mgr = ct.ConfigManager()
        assert list(mgr.config.presets) == ["padded"]


# --- Running the journal --------------------------------------------


@requires_magick
class TestRun:
    def test_unattended_reports_padded_and_touches_nothing(self, home, capsys):
        t = _tree(home)
        _padded(t["padded"])
        _real16(t["real"])
        _padded(t["nx"])
        _padded(t["beside"])
        before = {k: _sha(p) for k, p in t.items() if p.exists()}

        rc = ct._run_journal(_cfg(), _entry(_settings(home)), "Preset: padded",
                             unattended=True)

        assert rc == 0
        assert {k: _sha(p) for k, p in t.items() if p.exists()} == before, \
            "a Diagnose preset must never change a file"
        assert not any(home.rglob("OLD_PADDED")), "the 8-bit conversion ran"
        reports = list((home / "Logs" / "convert_tiff").glob("diagnose_*.log"))
        assert len(reports) == 1
        text = reports[0].read_text(encoding="utf-8")
        assert str(t["padded"]) in text
        assert "Padded 8-bit: 1" in text and "Real 16-bit: 1" in text
        assert str(t["nx"]) not in text, "NXSTUDIO files are skipped"
        assert str(t["beside"]) not in text, "outside the scope"

    def test_second_run_checks_only_new_or_changed_files(self, home, monkeypatch):
        t = _tree(home)
        _padded(t["padded"])
        _real16(t["real"])
        entry = _entry(_settings(home))
        assert ct._run_journal(_cfg(), entry, "Preset: padded", unattended=True) == 0
        cache = json.loads((home / ".convert_tiff_diagnose_cache.json").read_text(encoding="utf-8"))
        assert {v["status"] for v in cache.values()} == {"padded", "real"}

        checked = []
        real_check = ct._is_real_16bit

        def spy(path, *a, **k):
            checked.append(Path(path).name)
            return real_check(path, *a, **k)
        monkeypatch.setattr(ct, "_is_real_16bit", spy)

        assert ct._run_journal(_cfg(), entry, "Preset: padded", unattended=True) == 0
        assert checked == [], "unchanged files were checked again"

        _padded(t["real"])  # the file is replaced: new size/mtime, now padded
        st = t["real"].stat()
        os.utime(t["real"], ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        assert ct._run_journal(_cfg(), entry, "Preset: padded", unattended=True) == 0
        assert checked == ["DSC_0002.tif"]
        last = sorted((home / "Logs" / "convert_tiff").glob("diagnose_*.log"))[-1]
        assert "Padded 8-bit: 2" in last.read_text(encoding="utf-8"), \
            "the cached padded file must still be reported"

    def test_errors_are_reported_never_cached(self, home):
        t = _tree(home)
        _touch(t["padded"], b"not a tiff")
        rc = ct._run_journal(_cfg(), _entry(_settings(home)), "Preset: padded", unattended=True)
        assert rc == 1, "a file that could not be checked must show in the exit code"
        cache_file = home / ".convert_tiff_diagnose_cache.json"
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
        assert cache == {}

    def test_dry_run_checks_nothing(self, home, monkeypatch):
        t = _tree(home)
        _padded(t["padded"])
        monkeypatch.setattr(ct, "_is_real_16bit",
                            lambda *a, **k: pytest.fail("a dry run checked a file"))
        rc = ct._run_journal(_cfg(), _entry(_settings(home)), "Preset: padded",
                             unattended=True, dry_run=True)
        assert rc == 0
        assert not (home / ".convert_tiff_diagnose_cache.json").exists()

    def test_missing_root_refuses(self, home):
        rc = ct._run_journal(_cfg(), _entry(_settings(home / "gone")), "Preset: padded",
                             unattended=True)
        assert rc == 1

    def test_attended_run_asks_once_and_never_offers_the_conversion(self, home, monkeypatch):
        t = _tree(home)
        _padded(t["padded"])
        asked = []

        def answer(prompt=""):
            asked.append(prompt)
            return "y"
        monkeypatch.setattr("builtins.input", answer)
        rc = ct._run_journal(_cfg(), _entry(_settings(home)), "Preset: padded")
        assert rc == 0
        assert asked == ["Run it now? [y/N]: "], f"unexpected prompts: {asked}"
        assert not any(home.rglob("OLD_PADDED"))


# --- CLI ------------------------------------------------------------


@requires_cli
def test_run_preset_cli_end_to_end(tmp_path):
    t = _tree(tmp_path)
    _padded(t["padded"])
    _real16(t["real"])
    (tmp_path / ".convert_tiff_config.json").write_text(
        json.dumps({"presets": {"TIFF16 PADDED": _entry(_settings(tmp_path))}}),
        encoding="utf-8")
    env = os.environ.copy()
    env["USERPROFILE"] = str(tmp_path)
    env["HOME"] = str(tmp_path)

    def run(*args):
        return subprocess.run([sys.executable, str(REPO / "convert_tiff.py"), *args],
                              cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)

    listing = run("--list-presets")
    assert "TIFF16 PADDED" in listing.stdout and "[report only]" in listing.stdout

    r = run("--run-preset", "TIFF16 PADDED")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Padded 8-bit: 1" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


# --- v2.6.1: the cache serves the conversion, survives an interruption, and the
# --- conversion checks pixels itself ---------------------------------------


def _depth(path: Path) -> str:
    r = subprocess.run(["magick", "identify", "-format", "%z", f"{path}[0]"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout.strip()


@requires_cli
class TestCacheServesTheConversion:
    def test_menu_converts_what_the_preset_found_without_rechecking(self, home, monkeypatch):
        t = _tree(home)
        _padded(t["padded"])
        _real16(t["real"])
        assert ct._run_journal(_cfg(), _entry(_settings(home)), "Preset: padded",
                               unattended=True) == 0

        monkeypatch.setattr(ct, "_is_real_16bit",
                            lambda *a, **k: pytest.fail("the menu re-checked a cached file"))
        # folder, scope, skip names, workers, temp dir, compress-temp choice, convert?
        answers = iter([str(home), "_EXPORT/TIFF16", "*NXSTUDIO*", "2", "", "1", "y"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        assert ct.run_diagnose_tiffs(_cfg()) is True

        assert _depth(t["padded"]) == "8", "the padded file was not converted"
        assert (t["padded"].parent / "OLD_PADDED" / t["padded"].name).exists()
        assert _depth(t["real"]) == "16", "a real 16-bit file was converted"


@requires_magick
class TestCacheSurvivesAnInterruption:
    def test_interrupted_run_keeps_what_it_checked(self, home, monkeypatch):
        t = _tree(home)
        for k in ("padded", "real", "nx"):
            _padded(t[k])
        settings = {**_settings(home, skip=""), "workers": 1}
        real_check = ct._is_real_16bit
        seen = []

        def stop_on_third(path, *a, **k):
            seen.append(path)
            if len(seen) == 3:
                raise KeyboardInterrupt
            return real_check(path, *a, **k)
        monkeypatch.setattr(ct, "_is_real_16bit", stop_on_third)

        with pytest.raises(KeyboardInterrupt):
            ct._run_journal(_cfg(), _entry(settings), "Preset: padded", unattended=True)

        cache = json.loads((home / ".convert_tiff_diagnose_cache.json").read_text(encoding="utf-8"))
        assert len(cache) == 2, "the two files checked before Ctrl+C were lost"

    def test_cache_is_written_while_the_run_goes(self, home, monkeypatch):
        t = _tree(home)
        for k in ("padded", "real", "nx"):
            _padded(t[k])
        monkeypatch.setattr(ct, "_CACHE_SAVE_EVERY", 1)
        sizes = []
        real_save = ct._save_diagnose_cache
        monkeypatch.setattr(ct, "_save_diagnose_cache",
                            lambda cache: (sizes.append(len(cache)), real_save(cache)))
        settings = {**_settings(home, skip=""), "workers": 1}
        assert ct._run_journal(_cfg(), _entry(settings), "Preset: padded", unattended=True) == 0
        assert sizes == [1, 2, 3], f"the cache was not saved file by file: {sizes}"


@requires_cli
class TestConversionChecksPixels:
    def test_a_real_16bit_file_is_never_converted(self, tmp_path):
        """The verdict may come from the cache; the conversion must not trust it."""
        real = _real16(tmp_path / "real.tif")
        before = _sha(real)
        ct._compress_padded_files([real], tmp_path / "staging", workers=1, cfg=_cfg())
        assert _sha(real) == before, "a real 16-bit file was replaced by its 8-bit rounding"
        assert not (tmp_path / "OLD_PADDED").exists()

    def test_a_padded_file_is_still_converted(self, tmp_path):
        padded = _padded(tmp_path / "padded.tif")
        ct._compress_padded_files([padded], tmp_path / "staging", workers=1, cfg=_cfg())
        assert _depth(padded) == "8"
        assert (tmp_path / "OLD_PADDED" / "padded.tif").exists()


# --- v2.6.2: no alias copies piling up in %TEMP% ------------------------------


@requires_magick
class TestBracketAliasesAreReleased:
    """A '[FINAL]' folder makes every magick input go through a bracket-free alias in
    %TEMP%. Across volumes the alias is a full COPY, and v2.6.1 kept every one (~3 per
    file) until the run ended: 434 GB on the owner's C: during one Diagnose preset."""

    def _setup(self, tmp_path, monkeypatch):
        alias_dir = tmp_path / "alias"
        alias_dir.mkdir()
        monkeypatch.setattr(ct, "_magick_alias_dir", alias_dir)
        made = []

        def copy_not_link(src, dst):  # behave like G: -> C:, where linking fails
            made.append(dst)
            raise OSError("cross-volume")
        monkeypatch.setattr(ct.os, "link", copy_not_link)
        return alias_dir, made

    def test_diagnose_check_leaves_no_alias(self, tmp_path, monkeypatch):
        alias_dir, made = self._setup(tmp_path, monkeypatch)
        a = _padded(tmp_path / "250118_Shoot [FINAL]" / "_EXPORT" / "TIFF16" / "a.tif")
        b = _real16(tmp_path / "250118_Shoot [FINAL]" / "_EXPORT" / "TIFF16" / "b.tif")

        assert ct._is_real_16bit(a, tmp_path)[2].startswith("padded")
        assert ct._is_real_16bit(b, tmp_path)[0] is True
        assert list(alias_dir.iterdir()) == [], "alias copies were left in %TEMP%"
        assert len(made) == 2, f"one alias per file, not one per magick call: {len(made)}"

    def test_padded_conversion_leaves_no_alias(self, tmp_path, monkeypatch):
        alias_dir, _ = self._setup(tmp_path, monkeypatch)
        src = _padded(tmp_path / "Shoot [FINAL]" / "p.tif")
        staging = tmp_path / "stg"
        staging.mkdir()
        status = ct._process_single_padded(src, staging)[2]
        assert status == "ok", status
        assert list(alias_dir.iterdir()) == []


# --- progress line names the folder, not only the file -----------------------


class TestDisplayPath:
    def test_starts_at_the_root_folder(self, tmp_path):
        root = tmp_path / "2025"
        f = root / "Shoot [FINAL]" / "_EXPORT" / "TIFF16" / "DSC_0001.tif"
        assert ct._display_path(f, [root]) == os.sep.join(
            ["2025", "Shoot [FINAL]", "_EXPORT", "TIFF16", "DSC_0001.tif"])

    def test_picks_the_root_the_file_is_under(self, tmp_path):
        a, b = tmp_path / "2024", tmp_path / "2025"
        f = b / "S" / "x.tif"
        assert ct._display_path(f, [a, b]).startswith("2025" + os.sep)

    def test_long_path_keeps_session_and_file(self, tmp_path):
        root = tmp_path / "2025"
        f = root / ("S" * 60) / "a" / ("b" * 40) / "_EXPORT" / "TIFF16" / "DSC_0001.tif"
        shown = ct._display_path(f, [root])
        assert shown.startswith("2025" + os.sep + "S" * 60)
        assert shown.endswith(os.sep.join(["_EXPORT", "TIFF16", "DSC_0001.tif"]))
        assert "..." in shown

    def test_outside_every_root_shows_the_full_path(self, tmp_path):
        f = tmp_path / "elsewhere" / "x.tif"
        assert ct._display_path(f, [tmp_path / "2025"]) == str(f)


@requires_magick
def test_same_named_files_are_told_apart_on_screen(home, capsys):
    a = _padded(home / "2025" / "S1 [FINAL]" / "_EXPORT" / "TIFF16" / "DSC_0001.tif")
    b = _padded(home / "2025" / "S2 [FINAL]" / "_EXPORT" / "TIFF16" / "DSC_0001.tif")
    settings = _settings(home / "2025", skip="")
    assert ct._run_journal(_cfg(), _entry(settings), "Preset: padded", unattended=True) == 0
    out = capsys.readouterr().out
    for f in (a, b):
        assert ct._display_path(f, [home / "2025"]) in out, f"no folder shown for {f}"


# --- v2.7: the padded conversion records itself in xmpMM:History --------------


def _history(path: Path) -> list:
    r = subprocess.run(["exiftool", "-j", "-struct", "-XMP-xmpMM:History", str(path)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    hist = json.loads(r.stdout)[0].get("History") or []
    return hist if isinstance(hist, list) else [hist]


@requires_cli
class TestPaddedHistory:
    def test_one_entry_appended_earlier_ones_kept(self, tmp_path):
        src = _padded(tmp_path / "photo.tif")
        subprocess.run(["exiftool", "-q", "-overwrite_original",
                        "-XMP-xmpMM:History+={Action=saved,When=2025-01-18T10:00:00+09:00,"
                        "SoftwareAgent=Capture One 16.5}", "-EXIF:Artist=Owner", str(src)],
                       capture_output=True, check=True, timeout=120)
        assert len(_history(src)) == 1

        ct._compress_padded_files([src], tmp_path / "staging", workers=1, cfg=_cfg())

        assert _depth(src) == "8", "not converted"
        hist = _history(src)
        assert len(hist) == 2, f"History must gain exactly ONE entry (jxl-photo #531): {hist}"
        assert hist[0]["SoftwareAgent"] == "Capture One 16.5"
        new = hist[1]
        assert new["Action"] == "converted"
        assert new["SoftwareAgent"] == (f"{ct.TOOLKIT_NAME} {ct.TOOLKIT_VERSION} "
                                        f"({ct.TOOLKIT_URL})")
        assert "16-bit padded -> 8-bit" in new["Parameters"]
        assert new.get("When")

    def test_a_history_failure_keeps_the_original(self, tmp_path, monkeypatch):
        src = _padded(tmp_path / "photo.tif")
        before = _sha(src)
        monkeypatch.setattr(ct, "_append_padded_history", lambda p: False)
        ct._compress_padded_files([src], tmp_path / "staging", workers=1, cfg=_cfg())
        assert _sha(src) == before
        assert not (tmp_path / "OLD_PADDED").exists()


def test_toolkit_version_matches_the_newest_changelog_section():
    text = (REPO / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    import re
    newest = re.search(r"^## v(\S+)", text, re.M).group(1)
    assert ct.TOOLKIT_VERSION == newest, (ct.TOOLKIT_VERSION, newest)


# --- no silent phases: discovery, the size read and the checks report progress --


def test_fmt_duration():
    assert [ct._fmt_duration(s) for s in (5, 125, 3600 + 5 * 60)] == ["5s", "2m", "1h05m"]


def test_discovery_says_what_it_does(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    _touch(tmp_path / "2019" / "a" / "x.tif")
    ct._diagnose_targets([tmp_path / "2019"])
    out = capsys.readouterr().out
    assert "Searching for TIFFs under" in out
    assert "Found 1 TIFF(s)" in out


def test_size_read_reports_its_batches(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    monkeypatch.setattr(ct, "_run_captured",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="[]", stderr=""))
    ct._max_image_pixels([tmp_path / f"{i}.tif" for i in range(900)])
    out = capsys.readouterr().out
    assert "Reading the image size of 900 file(s)" in out
    assert "sizes read: 400/900" in out and "sizes read: 800/900" in out


def test_checks_report_time_left(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    monkeypatch.setattr(ct, "_is_real_16bit", lambda *a, **k: (True, 0.5, "real 16-bit"))
    files = [tmp_path / f"{i}.tif" for i in range(60)]
    ct._diagnose_pool(files, 1, None, "none")
    out = capsys.readouterr().out
    assert "25/60 checked in" in out and "50/60 checked in" in out
    assert "left" in out
