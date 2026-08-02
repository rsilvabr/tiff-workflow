"""
Tests for the manifest system in convert_tiff.py.

Mirrors the jxl-photo manifest tests (test_audit_priority2.py,
test_throughput_and_manifest_encoding.py, test_audit_delete_gates.py):
encoding/BOM, Excel float modes, traversal guards, source overlaps,
output collisions, the mode-8 delete gate, and per-entry execution.
"""

import csv
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import convert_tiff
from convert_tiff import (
    ToolConfig,
    _is_manifest_header_row,
    build_manifest_entry_cmd,
    execute_manifest_workflow,
    generate_manifest,
    get_latest_manifest,
    load_manifest_entries,
    manifest_output_collisions,
    manifest_source_overlaps,
    run_manifest_workflow,
    run_repeat_last,
)


# --- Helpers --------------------------------------------------------


def _cfg(**overrides):
    """Config stand-in with a no-op save (never touches the real JSON)."""
    cfg = SimpleNamespace(
        config=ToolConfig(ps_name="pwsh", ps_major=7, **overrides),
        save_config=lambda: None,
    )
    return cfg


def _write_manifest(path: Path, rows, header=True, encoding="utf-8-sig"):
    with open(path, "w", newline="", encoding=encoding) as f:
        writer = csv.writer(f)
        if header:
            writer.writerow(["Source", "Destination", "Mode"])
        writer.writerows(rows)
    return path


def _workflow(**overrides):
    wf = {
        "origin": "free_compress",
        "dest": "zip",
        "workers": 8,
        "staging": "",
        "dry_run": False,
    }
    wf.update(overrides)
    return wf


@pytest.fixture
def no_subprocess(monkeypatch):
    """run_subprocess must never fire in these tests unless a test mocks it."""
    calls = []

    def fake(cmd, timeout=None):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(convert_tiff, "run_subprocess", fake)
    return calls


def _confirm(monkeypatch, value):
    """Confirm.ask exists only when rich is importable; the plain-text path
    uses input(). Patching Confirm unconditionally breaks the suite without rich."""
    if convert_tiff.RICH_AVAILABLE:
        monkeypatch.setattr(convert_tiff.Confirm, "ask",
                            staticmethod(lambda *a, **k: value))
    else:
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y" if value else "n")


def _prompt(monkeypatch, value):
    """Prompt.ask exists only when rich is importable (see _confirm)."""
    if convert_tiff.RICH_AVAILABLE:
        monkeypatch.setattr(convert_tiff.Prompt, "ask",
                            staticmethod(lambda *a, **k: value))
    else:
        monkeypatch.setattr("builtins.input", lambda *a, **k: str(value))


# --- Encoding / parsing (tests 1-8) ----------------------------------


class TestEncoding:
    def test_manifest_written_with_bom_for_excel(self, tmp_path, monkeypatch):
        """Paths with non-ASCII chars must survive an Excel round-trip."""
        monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", tmp_path)
        src = tmp_path / "240419_山羊公園"
        src.mkdir()
        (src / "foto.tif").write_bytes(b"x")

        path = Path(generate_manifest(tmp_path, 0))

        raw = path.read_bytes()
        assert raw[:3] == b"\xef\xbb\xbf", "manifest must start with a UTF-8 BOM"

    def test_bom_stripped_on_read(self, tmp_path):
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv", [(str(src), str(src), 0)])

        entries = load_manifest_entries(str(path))

        assert entries is not None
        assert entries[0][0] == str(src), "BOM must not leak into the first cell"

    def test_ansi_manifest_is_refused_not_guessed(self, tmp_path):
        """Excel re-saving in the system ANSI codepage writes bytes that are
        not valid UTF-8. Guessing an encoding could yield a plausible-looking
        path pointing somewhere else -- refuse instead."""
        path = tmp_path / "m_ansi.csv"
        path.write_bytes("Source,Destination,Mode\r\nF:\\fotos\\ação,F:\\fotos\\ação,0\r\n".encode("cp1252"))

        assert load_manifest_entries(str(path)) is None

    def test_ascii_manifest_works_without_bom(self, tmp_path):
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv",
                               [(str(src), str(src), 0), (str(src), "", 1)],
                               encoding="ascii")

        entries = load_manifest_entries(str(path))

        assert entries is not None
        assert len(entries) == 2


class TestParsing:
    def test_header_deleted_first_entry_kept(self, tmp_path):
        """Only a real header row is skipped; a manifest whose header line
        was deleted must not silently lose entry #1."""
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv",
                               [(str(src), str(src), 0), (str(src), str(src), 1)],
                               header=False)

        entries = load_manifest_entries(str(path))

        assert entries is not None
        assert len(entries) == 2

    def test_header_row_detection(self):
        assert _is_manifest_header_row(["Source", "Destination", "Mode"])
        assert _is_manifest_header_row(["source", "", "mode"])
        assert not _is_manifest_header_row(["F:\\photos\\source", "", "0"])
        assert not _is_manifest_header_row([])

    def test_comments_skipped_and_empty_dest_falls_back_to_source(self, tmp_path):
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv", [
            ("# skipped row", "", ""),
            (str(src), "", 3),
        ])

        entries = load_manifest_entries(str(path))

        assert entries == [(str(src), str(src), 3)]

    def test_empty_manifest_refused(self, tmp_path):
        path = _write_manifest(tmp_path / "m.csv", [("# only a comment", "", "")])

        assert load_manifest_entries(str(path)) is None

    def test_excel_float_mode_accepted(self, tmp_path):
        """Excel formats integers as '7.0' -- that must still parse as 7."""
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv", [(str(src), str(src), "7.0")])

        entries = load_manifest_entries(str(path))

        assert entries == [(str(src), str(src), 7)]

    @pytest.mark.parametrize("bad", ["7.5", "abc", "10", "-1"])
    def test_invalid_mode_refuses_whole_manifest(self, tmp_path, bad):
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv", [(str(src), str(src), bad)])

        assert load_manifest_entries(str(path)) is None

    def test_traversal_entry_refuses_the_whole_manifest(self, tmp_path):
        """A '..' entry must refuse the manifest, not be skipped: a dropped
        folder looks exactly like one that compressed cleanly."""
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv", [
            (str(src), str(src), 0),
            (str(tmp_path / ".." / "evil"), str(src), 0),
        ])

        assert load_manifest_entries(str(path)) is None

    def test_row_without_mode_cell_keeps_none(self, tmp_path):
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv", [(str(src), str(src), "")])

        entries = load_manifest_entries(str(path))

        assert entries == [(str(src), str(src), None)]

    def test_semicolon_in_path_refused(self, tmp_path):
        """';' is legal in folder names, but the backend splits -InputDir on ';'
        -- one entry would arrive as two roots, and mode 8 would delete sources
        in a folder that was never validated."""
        src = tmp_path / "a;b"
        path = _write_manifest(tmp_path / "m.csv", [(str(src), str(src), 0)])

        assert load_manifest_entries(str(path)) is None

    def test_semicolon_delimited_manifest_refused(self, tmp_path):
        """Excel in pt-BR/de-DE locales saves CSV delimited by ';'. Parsed with
        ',' every line collapses into one cell -- detect and refuse with a clear
        message instead of treating the whole line as a path."""
        path = tmp_path / "m_semicolon.csv"
        path.write_text("Source;Destination;Mode\r\nF:\\fotos;F:\\fotos;0\r\n",
                        encoding="utf-8-sig")

        assert load_manifest_entries(str(path)) is None

    def test_comment_row_with_invalid_mode_cell_ignored(self, tmp_path):
        """Comment rows are ignored whole: an invalid Mode cell on a line that
        would be skipped anyway must not refuse the manifest."""
        src = tmp_path / "a"
        path = _write_manifest(tmp_path / "m.csv", [
            ("# lembrete", "revisar", "abc"),
            (str(src), str(src), 0),
        ])

        entries = load_manifest_entries(str(path))

        assert entries == [(str(src), str(src), 0)]


# --- Guards (tests 9-13) ----------------------------------------------


class TestSourceOverlaps:
    def test_nested_sources_detected(self, tmp_path):
        entries = [(str(tmp_path), str(tmp_path), 3),
                   (str(tmp_path / "sub"), str(tmp_path / "sub"), 3)]
        assert manifest_source_overlaps(entries)

    def test_duplicate_sources_detected(self, tmp_path):
        entries = [(str(tmp_path), str(tmp_path), 0)] * 2
        assert manifest_source_overlaps(entries)

    def test_sibling_sources_do_not_overlap(self, tmp_path):
        entries = [(str(tmp_path / "a"), str(tmp_path / "a"), 0),
                   (str(tmp_path / "ab"), str(tmp_path / "ab"), 0)]
        assert not manifest_source_overlaps(entries)


class TestOutputCollisions:
    def test_mode2_same_stem_same_dest_collides(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        fa = a / "foto.tif"
        fb = b / "foto.tif"
        fa.write_bytes(b"x")
        fb.write_bytes(b"y")
        dest = str(tmp_path / "out")
        entries = [(str(a), dest, 2), (str(b), dest, 2)]

        collisions = manifest_output_collisions(entries)

        assert collisions, "two entries flattening foto.tif into the same Destination must collide"
        assert {collisions[0][0], collisions[0][1]} == {fa, fb}

    def test_mode2_distinct_stems_do_not_collide(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "foto1.tif").write_bytes(b"x")
        (b / "foto2.tif").write_bytes(b"y")
        dest = str(tmp_path / "out")
        entries = [(str(a), dest, 2), (str(b), dest, 2)]

        assert not manifest_output_collisions(entries)

    def test_mode2_recursive_flattening_seen(self, tmp_path):
        """Mode 2 is recursive-FLAT: A/deep/foto.tif + B/foto.tif -> same output."""
        a = tmp_path / "a"
        deep = a / "deep"
        deep.mkdir(parents=True)
        b = tmp_path / "b"
        b.mkdir()
        (deep / "foto.tif").write_bytes(b"x")
        (b / "foto.tif").write_bytes(b"y")
        dest = str(tmp_path / "out")
        entries = [(str(a), dest, 2), (str(b), dest, 2)]

        assert manifest_output_collisions(entries)

    def test_mode4_rename_targets_do_not_collide(self, tmp_path):
        """Negative cases for mode 4 (_TIFF -> _ZIP rename). A positive
        cross-entry mode-4 collision cannot exist on a case-insensitive
        filesystem: colliding outputs require two source folders renaming to
        the SAME _ZIP under the same grandparent, which is already refused as
        a source overlap."""
        pa = tmp_path / "s1" / "photo_TIFF"
        pb = tmp_path / "s2" / "photo_TIFF"
        pa.mkdir(parents=True)
        pb.mkdir(parents=True)
        (pa / "foto.tif").write_bytes(b"x")
        (pb / "foto.tif").write_bytes(b"y")
        entries = [(str(tmp_path / "s1"), "", 4), (str(tmp_path / "s2"), "", 4)]

        assert not manifest_output_collisions(entries), \
            "different grandparents -> different _ZIP folders, no collision"

        same_grandpa_a = tmp_path / "g" / "a_TIFF"
        same_grandpa_b = tmp_path / "g" / "a_TIFF2"
        same_grandpa_a.mkdir(parents=True)
        same_grandpa_b.mkdir(parents=True)
        (same_grandpa_a / "foto.tif").write_bytes(b"x")
        (same_grandpa_b / "foto.tif").write_bytes(b"y")
        # a_TIFF -> a_ZIP ; a_TIFF2 -> a_ZIP2 -- still distinct
        entries = [(str(tmp_path / "g"), "", 4)]
        assert not manifest_output_collisions(entries)

    def test_per_source_modes_are_not_scanned(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        (a / "foto.tif").write_bytes(b"x")
        for mode in (0, 1, 3, 6, 7, 8, 9):
            assert not manifest_output_collisions([(str(a), str(a), mode)]), \
                f"mode {mode} writes inside its own tree and must be skipped"


class TestExecuteGuards:
    def test_mode8_delete_requires_confirmation(self, tmp_path, monkeypatch, no_subprocess):
        src = tmp_path / "a"
        src.mkdir()
        entries = [(str(src), str(src), 8)]

        _confirm(monkeypatch, False)
        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is False
        assert no_subprocess == [], "gate declined -> nothing may run"

        _confirm(monkeypatch, True)
        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is True
        assert len(no_subprocess) == 1
        assert "-DeleteSource" in no_subprocess[0]

    def test_delete_source_only_for_mode8_entries(self, tmp_path, monkeypatch, no_subprocess):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        entries = [(str(a), str(a), 0), (str(b), str(b), 8)]
        _confirm(monkeypatch, True)

        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is True
        assert "-DeleteSource" not in no_subprocess[0]
        assert "-DeleteSource" in no_subprocess[1]

    def test_missing_source_refuses_the_run(self, tmp_path, no_subprocess):
        entries = [(str(tmp_path / "ghost"), str(tmp_path / "ghost"), 0)]

        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is False
        assert no_subprocess == []

    def test_destination_ignored_warning_for_non_mode2(self, tmp_path, capsys, no_subprocess):
        src = tmp_path / "a"
        src.mkdir()
        entries = [(str(src), str(tmp_path / "elsewhere"), 0)]

        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is True
        out = capsys.readouterr().out
        assert "Destination column" in out

    def test_overlap_declined_refuses_the_run(self, tmp_path, monkeypatch, no_subprocess):
        entries = [(str(tmp_path), str(tmp_path), 3),
                   (str(tmp_path / "sub"), str(tmp_path / "sub"), 3)]
        _confirm(monkeypatch, False)

        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is False
        assert no_subprocess == []

    def test_collision_aborts_before_running(self, tmp_path, no_subprocess):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "foto.tif").write_bytes(b"x")
        (b / "foto.tif").write_bytes(b"y")
        dest = str(tmp_path / "out")
        entries = [(str(a), dest, 2), (str(b), dest, 2)]

        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is False
        assert no_subprocess == []


# --- Execution (tests 14-16) ------------------------------------------


class TestExecution:
    def test_one_command_per_entry_with_row_mode(self, tmp_path, no_subprocess):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        dest = str(tmp_path / "out")
        entries = [(str(a), str(a), 0), (str(b), dest, 2)]

        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is True
        assert len(no_subprocess) == 2

        cmd0, cmd2 = no_subprocess
        assert cmd0[cmd0.index("-Mode") + 1] == "0"
        assert cmd0[cmd0.index("-InputDir") + 1] == str(a)
        assert "-OutputDir" not in cmd0
        assert cmd2[cmd2.index("-Mode") + 1] == "2"
        assert cmd2[cmd2.index("-OutputDir") + 1] == dest

    def test_failure_does_not_stop_remaining_entries(self, tmp_path, monkeypatch, capsys):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        entries = [(str(a), str(a), 0), (str(b), str(b), 0)]
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return 1 if len(calls) == 1 else 0

        monkeypatch.setattr(convert_tiff, "run_subprocess", fake_run)

        assert execute_manifest_workflow(_cfg(), entries, _workflow()) is False
        assert len(calls) == 2, "a failed entry must not stop the rest"
        out = capsys.readouterr().out
        assert "1 ok" in out and "1 with failures" in out
        assert str(a) in out, "failed entry must be listed in the summary"

    def test_dry_run_skips_mode8_gate_and_delete(self, tmp_path, monkeypatch, no_subprocess):
        src = tmp_path / "a"
        src.mkdir()
        entries = [(str(src), str(src), 8)]

        def boom(*a, **k):
            raise AssertionError("mode-8 gate must not fire on a dry run")

        if convert_tiff.RICH_AVAILABLE:
            monkeypatch.setattr(convert_tiff.Confirm, "ask", staticmethod(boom))
        else:
            monkeypatch.setattr("builtins.input", boom)

        assert execute_manifest_workflow(_cfg(), entries, _workflow(dry_run=True)) is True
        assert "-DryRun" in no_subprocess[0]
        assert "-DeleteSource" not in no_subprocess[0]

    def test_default_mode_applied_to_rows_without_mode(self, tmp_path, monkeypatch, no_subprocess):
        """Rows with an empty Mode cell inherit the run's default mode."""
        src = tmp_path / "a"
        src.mkdir()
        manifest = _write_manifest(tmp_path / "manifest_1.csv", [(str(src), str(src), "")])

        monkeypatch.setattr(convert_tiff, "pick_manifest", lambda: str(manifest))
        monkeypatch.setattr(convert_tiff, "step_basic_params", lambda cfg, wf: True)
        monkeypatch.setattr(convert_tiff, "confirm_manifest_entries", lambda p, e: True)
        _prompt(monkeypatch, "3")

        assert run_manifest_workflow(_cfg()) is True
        assert no_subprocess[0][no_subprocess[0].index("-Mode") + 1] == "3"


# --- Repeat (tests 17-18) ----------------------------------------------


class TestRepeat:
    def test_manifest_path_persisted_after_run(self, tmp_path, monkeypatch, no_subprocess):
        src = tmp_path / "a"
        src.mkdir()
        manifest = _write_manifest(tmp_path / "manifest_1.csv", [(str(src), str(src), 0)])

        monkeypatch.setattr(convert_tiff, "pick_manifest", lambda: str(manifest))
        monkeypatch.setattr(convert_tiff, "step_basic_params", lambda cfg, wf: True)
        monkeypatch.setattr(convert_tiff, "confirm_manifest_entries", lambda p, e: True)

        cfg = _cfg()
        assert run_manifest_workflow(cfg) is True
        assert cfg.config.last_manifest_path == str(manifest)
        assert cfg.config.last_run_kind == "manifest"

    def test_repeat_rereads_csv_through_loader(self, tmp_path, monkeypatch, no_subprocess):
        src = tmp_path / "a"
        src.mkdir()
        manifest = _write_manifest(tmp_path / "manifest_1.csv", [(str(src), str(src), 0)])

        cfg = _cfg()
        cfg.config.last_run_kind = "manifest"
        cfg.config.last_run_label = "Manifest: manifest_1.csv (1 entries)"
        cfg.config.last_manifest_path = str(manifest)

        loader_calls = []
        real_loader = convert_tiff.load_manifest_entries

        def tracking_loader(path):
            loader_calls.append(path)
            return real_loader(path)

        monkeypatch.setattr(convert_tiff, "load_manifest_entries", tracking_loader)
        _confirm(monkeypatch, True)

        assert run_repeat_last(cfg) is True
        assert loader_calls == [str(manifest)], "repeat must re-read the CSV via the loader"
        assert len(no_subprocess) == 1

    def test_repeat_refused_when_manifest_deleted(self, tmp_path):
        cfg = _cfg()
        cfg.config.last_run_kind = "manifest"
        cfg.config.last_manifest_path = str(tmp_path / "gone.csv")

        assert run_repeat_last(cfg) is False


# --- Generation (tests 19-20) ------------------------------------------


class TestGeneration:
    def test_per_folder_entries_for_nonrecursive_modes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", tmp_path)
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "x.tif").write_bytes(b"x")
        (b / "y.tif").write_bytes(b"y")
        (tmp_path / "empty").mkdir()

        path = Path(generate_manifest(tmp_path, 0))

        assert path is not None
        entries = load_manifest_entries(str(path))
        sources = {s for s, _, m in entries}
        assert sources == {str(a), str(b)}, "one entry per folder containing TIFFs"
        assert all(m == 0 for _, _, m in entries)

    def test_single_root_entry_for_recursive_modes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "x.tif").write_bytes(b"x")

        path = Path(generate_manifest(tmp_path, 3))

        entries = load_manifest_entries(str(path))
        assert entries == [(str(tmp_path), str(tmp_path), 3)], \
            "recursive modes get one entry for the root (per-folder entries would overlap)"

    def test_mode2_prefills_flat_destination(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", tmp_path)
        (tmp_path / "x.tif").write_bytes(b"x")

        path = Path(generate_manifest(tmp_path, 2))

        entries = load_manifest_entries(str(path))
        assert entries == [(str(tmp_path), str(tmp_path / "ZIP_flat"), 2)]

    def test_no_tiffs_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", tmp_path)

        assert generate_manifest(tmp_path, 0) is None

    def test_generation_excludes_backup_and_output_folders(self, tmp_path, monkeypatch):
        """OLD_TIFFs holds the archived originals -- a manifest entry pointing
        there would recompress the backups (and mode 8 would delete them)."""
        monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", tmp_path)
        good = tmp_path / "photos"
        good.mkdir()
        (good / "x.tif").write_bytes(b"x")
        for name in ("OLD_TIFFs", "old_padded", "ZIP", "converted_zip"):
            d = tmp_path / name
            d.mkdir()
            (d / "y.tif").write_bytes(b"y")

        path = Path(generate_manifest(tmp_path, 0))

        entries = load_manifest_entries(str(path))
        sources = {s for s, _, _ in entries}
        assert sources == {str(good)}, f"backup/output folders must not become entries: {sources}"

    def test_get_latest_manifest(self, tmp_path, monkeypatch):
        import os
        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()
        older = manifest_dir / "manifest_20200101_000000.csv"
        older.write_text("x")
        latest = manifest_dir / "manifest_20210101_000000.csv"
        latest.write_text("x")
        os.utime(older, (1000000000, 1000000000))
        os.utime(latest, (1600000000, 1600000000))
        monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", tmp_path)

        assert get_latest_manifest() == str(latest)


# --- Command builder ---------------------------------------------------


class TestBuildEntryCmd:
    def test_mode8_gets_delete_source(self, tmp_path):
        cmd = build_manifest_entry_cmd(str(tmp_path), str(tmp_path), 8, _workflow(), "pwsh")
        assert "-DeleteSource" in cmd

    def test_non_mode8_never_gets_delete_source(self, tmp_path):
        cmd = build_manifest_entry_cmd(str(tmp_path), str(tmp_path), 0, _workflow(), "pwsh")
        assert "-DeleteSource" not in cmd

    def test_mode2_output_dir_honors_destination(self, tmp_path):
        dest = str(tmp_path / "out")
        cmd = build_manifest_entry_cmd(str(tmp_path), dest, 2, _workflow(), "pwsh")
        assert cmd[cmd.index("-OutputDir") + 1] == dest

    def test_destination_ignored_outside_mode2(self, tmp_path):
        cmd = build_manifest_entry_cmd(str(tmp_path), str(tmp_path / "out"), 1, _workflow(), "pwsh")
        assert "-OutputDir" not in cmd
