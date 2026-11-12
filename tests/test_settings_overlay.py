"""v2.9.0: the settings overlay (.tiff_workflow_settings.json).

T5  the wizard's reader and the backends' reader agree on ~70 (spec, JSON value) cases
T6  no safety/per-run name is in any spec table
T7  real end to end under a hostile folder: a value from the file is what a run really does; a
    -Param on the command line beats it; a SafeMode entry is refused
T8  a corrupt file, a root array, a wrong-case name, an unknown section, a BOM
T9  discovery order: TIFFWORKFLOW_SETTINGS (blank = off), next to the scripts, the user profile
T10 _backend_default and _tool_output_names follow the overlay; a wrong type falls back
T11 the Script settings menu (main menu 6)

Nothing here reads or writes the owner's real settings file: every run points
TIFFWORKFLOW_SETTINGS (or USERPROFILE and a copy of the script) into tmp_path.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import convert_tiff  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ["compress_tiff_zip.py", "copy_exif_to_tiff.py", "generate_thumbnails.py"]
import tiff_workflow_common as twc  # noqa: E402
from backends import LANES, backend_cmd  # noqa: E402
HOSTILE = "写真 [FINAL] x"
ENV = "TIFFWORKFLOW_SETTINGS"
need_tools = pytest.mark.skipif(shutil.which("magick") is None or shutil.which("exiftool") is None,
                                reason="ImageMagick and exiftool required")

# Never in any spec table: chosen per run, or what deletes / overwrites / skips safety checks.
FORBIDDEN = ["mode", "input_dir", "input_dir_file", "output_dir", "staging_dir", "workers", "dry_run",
             "safe_mode", "skip_lzw_as_compressed", "overwrite", "delete_source", "duplicate_action",
             "force_parallel", "force_sequential", "restore_stranded", "generate_thumbnail",
             "skip_compressed_with_thumb", "thumb_page", "page", "remove", "recursive",
             "skip_if_tiff_has_exif", "compress_zip", "auto_find", "folder_pattern", "fail_on_warn",
             "icc_policy", "exclude_folders"]


# --- helpers ---------------------------------------------------------------------------------

def _env(settings=None, **extra):
    """A child environment: TIFFWORKFLOW_SETTINGS = settings (None = removed), plus extras."""
    env = dict(os.environ)
    env.pop(ENV, None)
    if settings is not None:
        env[ENV] = str(settings)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _write(path: Path, data, raw: bytes = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if raw is not None else json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return path


def _run(lane, script, args, cwd, env, script_dir=REPO):
    cmd = backend_cmd(lane, script_dir / script, [*[str(a) for a in args]])
    r = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, timeout=600)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _tiff(path: Path, pages=1):
    import numpy as np
    import tifffile
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    with tifffile.TiffWriter(str(path)) as tw:
        for _ in range(pages):
            tw.write(rng.integers(0, 255, size=(48, 64, 3), dtype=np.uint8), photometric="rgb")


# --- T5: the two readers agree -----------------------------------------------------------------

# (spec, JSON text, accepted)
CASES = [
    ("int:1:", "5", True), ("int:1:", "0", False), ("int:1:", "-3", False),
    ("int:1:", "true", False), ("int:1:", "1.5", False), ("int:1:", "5.0", False),
    ("int:1:", '"5"', False), ("int:1:", "null", False), ("int:1:", "2147483647", True),
    ("int:1:", "2147483648", False), ("int:1:", "99999999999999999999", False),
    ("int:0:64", "64", True), ("int:0:64", "65", False), ("int:0:64", "0", True),
    ("float:0:1", "0.8", True), ("float:0:1", "1", True), ("float:0:1", "0", True),
    ("float:0:1", "1.5", False), ("float:0:1", "-0.1", False), ("float:0:1", "true", False),
    ("float:0:1", '"0.5"', False), ("float:0:1", "null", False),
    # the real WorkerMemoryFraction spec: the owner runs 1.1 (jxl-photo allows up to 2)
    ("float:0:2", "1.1", True), ("float:0:2", "2", True), ("float:0:2", "2.5", False),
    ("intstr:1:100", "85", True), ("intstr:1:100", "100", True), ("intstr:1:100", "1", True),
    ("intstr:1:100", "101", False), ("intstr:1:100", "0", False), ("intstr:1:100", '"85"', False),
    ("intstr:1:100", "85.0", False), ("intstr:1:100", "true", False),
    ("bool", "true", True), ("bool", "false", True), ("bool", "1", False),
    ("bool", '"true"', False), ("bool", "null", False),
    ("name", '"ZIP"', True), ("name", '" ZIP "', True), ("name", '"写真 [x]"', True),
    ("name", '""', False), ("name", '"   "', False), ("name", '"."', False),
    ("name", '".."', False), ("name", '"a/b"', False), ("name", '"a\\\\b"', False),
    ("name", '"a:b"', False), ("name", '"a*b"', False), ("name", '"a?b"', False),
    ("name", '"a\\"b"', False), ("name", '"a<b"', False), ("name", '"a>b"', False),
    ("name", '"a|b"', False), ("name", "5", False), ("name", "null", False),
    ("name", "true", False),
    ("path?", "null", True), ("path?", '"C:\\\\x\\\\a.icc"', True), ("path?", '" C:\\\\x "', True),
    ("path?", '""', False), ("path?", '"   "', False), ("path?", "5", False),
    ("path?", "true", False), ("path?", "[]", False),
]


def _py_verdict(spec, text):
    ok, checked = convert_tiff._overlay_value(spec, json.loads(text))
    if not ok:
        return "no"
    return f"yes:{type(checked).__name__}:{checked}"


def test_T5_python_reader_verdicts_are_the_documented_ones():
    for spec, text, accepted in CASES:
        assert _py_verdict(spec, text).startswith("yes") == accepted, (spec, text)


def test_T5_the_backends_reader_agrees_with_the_wizard(tmp_path, monkeypatch):
    """Same verdict, same value, same type: the wizard shows a value and the backend runs
    with it."""
    bad = []
    for i, (spec, text, accepted) in enumerate(CASES):
        ov = tmp_path / f"ov_{i}.json"
        ov.write_text('{"s": {"X": ' + text + '}}', encoding="utf-8")
        monkeypatch.setenv(ENV, str(ov))
        got = twc.read_settings_overlay("s", {"X": spec}, "")
        if "X" in got["values"]:
            v = got["values"]["X"]
            backend = f"yes:{type(v).__name__}:{v}"
        else:
            backend = "no"
            assert got["warnings"], (spec, text)
        if backend != _py_verdict(spec, text) or backend.startswith("yes") != accepted:
            bad.append(f"{spec} {text}: wizard {_py_verdict(spec, text)!r}, backend {backend!r}")
    assert not bad, "\n".join(bad)


# --- T6: what a spec table may contain ------------------------------------------------------------

def _table(script):
    return convert_tiff._backend_overlay_specs(script)


@pytest.mark.parametrize("script", SCRIPTS)
def test_T6_no_safety_or_per_run_name_is_in_a_spec_table(script):
    table = _table(script)
    assert table, f"{script}: no OVERLAY_SPECS table"
    leaked = sorted(set(table) & set(FORBIDDEN))
    assert not leaked, f"{script} lets the overlay change {leaked}"
    # every name is a real parameter of the script, and only those the plan lists
    assert set(table) <= set(convert_tiff._backend_param_names(script))
    # ...and the table the wizard reads from the file is the one the script runs with
    module = __import__(script[:-3])
    assert table == module.OVERLAY_SPECS


def test_T6_the_tables_are_exactly_the_planned_ones():
    assert _table("compress_tiff_zip.py") == {
        "zip_suffix": "name", "zip_subfolder_name": "name", "export_marker": "name",
        "export_zip_subfolder": "name", "export_tiff_subfolder": "name", "cap_workers": "int:0:64",
        "magick_timeout": "int:1:", "thumb_size": "int:1:", "thumb_quality": "intstr:1:100",
        "thumb_format": "name", "srgb_profile": "path?", "worker_memory_fraction": "float:0:2"}
    assert _table("generate_thumbnails.py") == {
        "size": "int:1:", "quality": "intstr:1:100", "format": "name", "srgb_profile": "path?"}
    assert _table("copy_exif_to_tiff.py") == {"magick_timeout": "int:1:",
                                              "worker_memory_fraction": "float:0:2"}


def test_T6_a_name_of_v3_0_0_in_the_file_changes_nothing(tmp_path, monkeypatch):
    """v3.1 (T7): the file takes the names the code has (zip_subfolder_name). One written
    before, ZipSubfolderName, is an unknown name like any other: a warning, and the script's
    value stays -- never a guess at what it meant."""
    import compress_tiff_zip as cz
    ov = _write(tmp_path / HOSTILE / "s.json", {"compress_tiff_zip": {
        "ZipSubfolderName": "OLD", "WorkerMemoryFraction": 1.1, "cap_workers": 3}})
    monkeypatch.setenv(ENV, str(ov))
    values, bound = twc.parse_args(["--mode", "1"], cz.PARAMS)
    result = twc.apply_settings_overlay("compress_tiff_zip", cz.OVERLAY_SPECS, values, bound,
                                        str(REPO))
    assert result["applied"] == ["cap_workers"] and values["cap_workers"] == 3
    assert values["zip_subfolder_name"] == "ZIP" and values["worker_memory_fraction"] == \
        {p.name: p.default for p in cz.PARAMS}["worker_memory_fraction"]
    assert sorted(w.split(" ")[0] for w in result["warnings"]) == ["WorkerMemoryFraction",
                                                                   "ZipSubfolderName"]
    assert all("is not a setting the overlay can change" in w for w in result["warnings"])


@pytest.mark.parametrize("script,section,key,value", [
    ("compress_tiff_zip.py", "compress_tiff_zip", "cap_workers", 3),
    ("copy_exif_to_tiff.py", "copy_exif", "magick_timeout", 77),
    ("generate_thumbnails.py", "generate_thumbnails", "size", 128),
])
def test_T6_the_overlay_is_applied_before_anything_reads_a_parameter(
        script, section, key, value, tmp_path, monkeypatch):
    """A parameter read before the overlay is applied keeps the script's own value while
    the rest of the run uses the file's. The run is watched: every read of the options
    must come after the one apply_settings_overlay, and the log names the overlay right
    after its first line (v3.1: was a text match on the source)."""
    import importlib
    module = importlib.import_module(script[:-3])
    applied, early, late = [], [], []

    class Watched(dict):
        def __getitem__(self, name):
            (late if applied else early).append(name)
            return super().__getitem__(name)

        def get(self, name, default=None):
            (late if applied else early).append(name)
            return super().get(name, default)

    real_parse, real_apply = twc.parse_args, twc.apply_settings_overlay

    def parse_args(argv, params):
        opt, bound = real_parse(argv, params)
        return Watched(opt), bound

    def apply(sec, specs, values, bound, root):
        applied.append(sec)
        return real_apply(sec, specs, values, bound, root)
    monkeypatch.setattr(twc, "parse_args", parse_args)
    monkeypatch.setattr(twc, "apply_settings_overlay", apply)
    root = tmp_path / HOSTILE
    src = root / "in 50%"
    src.mkdir(parents=True)
    ov = _write(root / "settings.json", {section: {key: value}})
    monkeypatch.setenv(ENV, str(ov))
    monkeypatch.chdir(root)
    argv = (["--mode", "1"] if section == "compress_tiff_zip" else []) + ["--input-dir", str(src), "--dry-run"]
    module.main(argv)
    assert applied == [section]
    assert not early, f"{script}: read before the overlay was applied: {early}"
    assert len(late) >= 3, f"{script}: the run read no option: the watch proves nothing"
    logs = list((root / "Logs").rglob("*.log"))
    assert len(logs) == 1, logs
    lines = logs[0].read_text(encoding="utf-8").splitlines()
    assert "| tiff-workflow " in lines[0], lines[:3]
    assert f"Script settings overlay: {ov}: 1 value(s): {key}" in lines[1], lines[:3]


# --- T7 / T8: real runs ---------------------------------------------------------------------------

@need_tools
class TestEndToEnd:
    """compress_tiff_zip.py mode 1 (output into a subfolder of each input folder) in a hostile
    folder; the overlay file sits in the same folder and is named through TIFFWORKFLOW_SETTINGS."""

    @staticmethod
    def _layout(tmp_path):
        root = tmp_path / HOSTILE
        src = root / "scans 01"
        _tiff(src / "DSCF0001 (2).tif")
        return root, src

    @staticmethod
    def _mode1(lane, src, root, env, *extra):
        return _run(lane, "compress_tiff_zip.py",
                    ["--mode", "1", "--input-dir", src, "--workers", "1", *extra], root, env)

    @pytest.mark.parametrize("lane", LANES)
    def test_T7_a_value_from_the_file_is_what_the_run_does(self, tmp_path, lane):
        root, src = self._layout(tmp_path)
        ov = _write(root / "settings.json", {"compress_tiff_zip": {"zip_subfolder_name": "FROM_FILE"}})
        rc, out = self._mode1(lane, src, root, _env(ov))
        assert rc == 0, out[-3000:]
        assert (src / "FROM_FILE" / "DSCF0001 (2).tif").is_file(), out[-3000:]
        assert not (src / "ZIP").exists()
        assert f"Script settings overlay: {ov}: 1 value(s): zip_subfolder_name" in out
        logs = list((root / "Logs" / "compress_tiff_zip").glob("*.log"))
        assert logs, "no log written"
        text = logs[0].read_text(encoding="utf-8")
        assert text.index("tiff-workflow ") < text.index("Script settings overlay: ")
        assert "zip_subfolder_name" in text

    @pytest.mark.parametrize("lane", LANES)
    def test_T7_without_the_file_the_script_default_applies(self, tmp_path, lane):
        root, src = self._layout(tmp_path)
        rc, out = self._mode1(lane, src, root, _env(tmp_path / "does not exist.json"))
        assert rc == 0, out[-3000:]
        assert (src / "ZIP" / "DSCF0001 (2).tif").is_file()
        assert "settings overlay" not in out.lower()

    @pytest.mark.parametrize("lane", LANES)
    def test_T7_a_param_on_the_command_line_beats_the_file(self, tmp_path, lane):
        root, src = self._layout(tmp_path)
        ov = _write(root / "settings.json", {"compress_tiff_zip": {"zip_subfolder_name": "FROM_FILE"}})
        rc, out = self._mode1(lane, src, root, _env(ov), "--zip-subfolder-name", "FROM_CMDLINE")
        assert rc == 0, out[-3000:]
        assert (src / "FROM_CMDLINE" / "DSCF0001 (2).tif").is_file()
        assert not (src / "FROM_FILE").exists()
        assert "Script settings overlay:" not in out, "a bound param is not 'applied'"

    @pytest.mark.parametrize("lane", LANES)
    def test_T7_a_safemode_entry_is_refused_and_multipage_handling_is_unchanged(self, tmp_path, lane):
        root, src = self._layout(tmp_path)
        _tiff(src / "multi.tif", pages=2)
        results = {}
        for label, data in (("none", None),
                            ("safemode_false", {"compress_tiff_zip": {"safe_mode": False,
                                                                      "skip_lzw_as_compressed": True,
                                                                      "overwrite": True,
                                                                      "delete_source": True}})):
            work = tmp_path / label
            shutil.copytree(src, work / "scans 01")
            ov = _write(root / f"{label}.json", data) if data else tmp_path / "absent.json"
            rc, out = self._mode1(lane, work / "scans 01", work, _env(ov))
            assert rc == 0, out[-3000:]
            results[label] = (sorted(p.name for p in (work / "scans 01" / "ZIP").glob("*")),
                              sorted(p.name for p in (work / "scans 01").glob("*.tif")), out)
        assert results["none"][:2] == results["safemode_false"][:2]
        assert "multi.tif" not in results["none"][0], "SafeMode (default on) must skip the multi-page file"
        assert results["safemode_false"][1] == ["DSCF0001 (2).tif", "multi.tif"], "DeleteSource must not apply"
        for name in ("safe_mode", "skip_lzw_as_compressed", "overwrite", "delete_source"):
            assert f"WARNING: settings overlay: {name} is not a setting the overlay can change in compress_tiff_zip" \
                in results["safemode_false"][2]

    @pytest.mark.parametrize("lane", LANES)
    @pytest.mark.parametrize("label,raw,warns", [
        ("corrupt_json", b'{"compress_tiff_zip": {"zip_subfolder_name": ', True),
        ("root_array", b'[{"compress_tiff_zip": {"zip_subfolder_name": "ARR"}}]', True),
        ("root_scalar", b'5', True),
        ("section_not_object", b'{"compress_tiff_zip": [1, 2]}', True),
        ("wrong_case_name", '{"compress_tiff_zip": {"zipsubfoldername": "LOWER"}}'.encode(), True),
        ("wrong_case_section", '{"Compress_Tiff_Zip": {"zip_subfolder_name": "UPPER"}}'.encode(), False),
        ("unknown_section", b'{"nonsense": {"zip_subfolder_name": "OTHER"}}', False),
        ("invalid_value", b'{"compress_tiff_zip": {"zip_subfolder_name": "a/b"}}', True),
        ("empty_file", b'', True),
        ("empty_object", b'{}', False),
    ])
    def test_T8_a_bad_file_changes_nothing_and_says_so(self, tmp_path, lane, label, raw, warns):
        root, src = self._layout(tmp_path)
        ov = _write(root / "settings.json", None, raw=raw)
        rc, out = self._mode1(lane, src, root, _env(ov))
        assert rc == 0, out[-3000:]
        assert (src / "ZIP" / "DSCF0001 (2).tif").is_file(), out[-3000:]   # the script's own default
        assert sorted(p.name for p in src.iterdir()) == ["DSCF0001 (2).tif", "ZIP"]
        assert ("WARNING: settings overlay:" in out) == warns, out[-2500:]
        assert "Script settings overlay:" not in out

    @pytest.mark.parametrize("lane", LANES)
    def test_T8_a_utf8_bom_is_read(self, tmp_path, lane):
        root, src = self._layout(tmp_path)
        data = {"compress_tiff_zip": {"zip_subfolder_name": "写真 BOM"}}
        ov = _write(root / "settings.json", None,
                    raw=b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode("utf-8"))
        rc, out = self._mode1(lane, src, root, _env(ov))
        assert rc == 0, out[-3000:]
        assert (src / "写真 BOM" / "DSCF0001 (2).tif").is_file(), out[-3000:]
        assert "WARNING: settings overlay" not in out

    @pytest.mark.parametrize("lane", LANES)
    def test_T8_the_exit_code_of_a_failing_run_is_unchanged_by_a_bad_file(self, tmp_path, lane):
        """A missing input folder exits 1 with or without an overlay problem: the overlay never
        turns an error into a pass nor a pass into an error."""
        root, _src = self._layout(tmp_path)
        ov = _write(root / "settings.json", None, raw=b"{ not json")
        rc_bad, out_bad = _run(lane, "compress_tiff_zip.py", ["--mode", "1", "--input-dir", root / "missing"],
                               root, _env(ov))
        rc_none, _ = _run(lane, "compress_tiff_zip.py", ["--mode", "1", "--input-dir", root / "missing"],
                          root, _env(tmp_path / "absent.json"))
        assert rc_bad == rc_none
        assert "WARNING: settings overlay" in out_bad

    @pytest.mark.parametrize("lane", LANES)
    @pytest.mark.parametrize("script,section,key,value", [
        ("generate_thumbnails.py", "generate_thumbnails", "size", 128),
        ("copy_exif_to_tiff.py", "copy_exif", "magick_timeout", 77),
    ])
    def test_T7_every_script_applies_its_own_section(self, tmp_path, lane, script, section, key, value):
        root, src = self._layout(tmp_path)
        ov = _write(root / "settings.json", {section: {key: value, "safe_mode": False}})
        rc, out = _run(lane, script, ["--input-dir", src, "--dry-run"], root, _env(ov))
        assert rc == 0, out[-3000:]
        assert f"Script settings overlay: {ov}: 1 value(s): {key}" in out
        assert "safe_mode is not a setting the overlay can change" in out
        # the other section's script does not see this one's entries
        other = {"generate_thumbnails": "compress_tiff_zip"}.get(section, "generate_thumbnails")
        ov2 = _write(root / "other.json", {other: {"size": 9}})
        rc, out = _run(lane, script, ["--input-dir", src, "--dry-run"], root, _env(ov2))
        assert rc == 0 and "settings overlay" not in out.lower()


# --- T9: where the file is looked for -----------------------------------------------------------

def test_T9_python_discovery_order(tmp_path, monkeypatch):
    portable_dir, home = tmp_path / "scripts [x]", tmp_path / "home 写真"
    portable_dir.mkdir(), home.mkdir()
    monkeypatch.setattr(convert_tiff, "SCRIPT_DIR", portable_dir)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    name = convert_tiff._SETTINGS_OVERLAY_FILE
    assert name == ".tiff_workflow_settings.json" and convert_tiff._SETTINGS_OVERLAY_ENV == ENV
    monkeypatch.delenv(ENV, raising=False)
    assert convert_tiff._settings_overlay_path() == home / name          # nothing anywhere: the profile
    (home / name).write_text("{}", encoding="utf-8")
    assert convert_tiff._settings_overlay_path() == home / name
    (portable_dir / name).write_text("{}", encoding="utf-8")
    assert convert_tiff._settings_overlay_path() == portable_dir / name  # next to the scripts wins
    monkeypatch.setenv(ENV, str(tmp_path / "explicit.json"))
    assert convert_tiff._settings_overlay_path() == tmp_path / "explicit.json"   # the variable wins
    monkeypatch.setenv(ENV, "")
    assert convert_tiff._settings_overlay_path() is None                 # set but blank: OFF
    monkeypatch.setenv(ENV, "   ")
    assert convert_tiff._settings_overlay_path() is None
    assert convert_tiff._overlay_values("compress_tiff_zip.py") == {}   # and nothing is read


@pytest.mark.parametrize("lane", LANES)
def test_T9_backend_discovery_order(tmp_path, lane):
    scripts = tmp_path / "portable [x]"
    home = tmp_path / "home 写真"
    empty = tmp_path / "empty input"
    for d in (scripts, home, empty):
        d.mkdir()
    for name in ("generate_thumbnails.py", "tiff_workflow_common.py"):   # a portable copy
        shutil.copy(REPO / name, scripts / name)
    name = ".tiff_workflow_settings.json"
    explicit = _write(tmp_path / "explicit.json", {"generate_thumbnails": {"size": 111}})

    def run(env):
        return _run(lane, "generate_thumbnails.py", ["--input-dir", empty, "--dry-run"], tmp_path, env,
                    script_dir=scripts)

    base = dict(USERPROFILE=home, HOME=home)
    # 1. only the profile has a file
    _write(home / name, {"generate_thumbnails": {"size": 222}})
    rc, out = run(_env(None, **base))
    assert rc == 0 and f"Script settings overlay: {home / name}: 1 value(s): size" in out, out[-1500:]
    # 2. a file next to the scripts beats the profile
    _write(scripts / name, {"generate_thumbnails": {"size": 333}})
    rc, out = run(_env(None, **base))
    assert rc == 0 and f"Script settings overlay: {scripts / name}: 1 value(s): size" in out, out[-1500:]
    # 3. the variable beats both
    rc, out = run(_env(explicit, **base))
    assert rc == 0 and f"Script settings overlay: {explicit}: 1 value(s): size" in out, out[-1500:]
    # 4. set but blank: no overlay at all, whatever files exist
    for blank in ("", "   "):
        rc, out = run(_env(blank, **base))
        assert rc == 0 and "settings overlay" not in out.lower(), out[-1500:]


# --- T10: the wizard follows the overlay ---------------------------------------------------------

def _use_overlay(monkeypatch, path):
    monkeypatch.setenv(ENV, str(path))
    monkeypatch.setenv("USERPROFILE", str(path.parent))


def test_T10_backend_default_and_tool_output_names_follow_the_overlay(tmp_path, monkeypatch):
    ov = _write(tmp_path / HOSTILE / "s.json", {
        "compress_tiff_zip": {"zip_subfolder_name": "ZIPPED", "export_marker": "_OUT", "cap_workers": 3,
                              "thumb_quality": 90, "thumb_size": 128},
        "generate_thumbnails": {"size": 64, "format": "png"}})
    before = convert_tiff._tool_output_names()
    assert "zipped" not in before
    _use_overlay(monkeypatch, ov)
    bd = convert_tiff._backend_default
    assert bd("compress_tiff_zip.py", "zip_subfolder_name") == "ZIPPED"
    assert bd("compress_tiff_zip.py", "cap_workers") == 3
    assert bd("compress_tiff_zip.py", "thumb_quality") == "90"           # intstr: a string, like the script's
    assert bd("compress_tiff_zip.py", "thumb_size") == 128
    assert bd("generate_thumbnails.py", "size") == 64 and bd("generate_thumbnails.py", "format") == "png"
    names = convert_tiff._tool_output_names()
    assert "zipped" in names and "_out" in names
    # discovery's module constant is refreshed on request
    convert_tiff._refresh_tool_output_parts()
    try:
        assert "zipped" in convert_tiff._TOOL_OUTPUT_PARTS
    finally:
        monkeypatch.setenv(ENV, "")
        convert_tiff._refresh_tool_output_parts()
    assert convert_tiff._TOOL_OUTPUT_PARTS == before
    # a name the script does not list in its table, or a bad value, changes nothing
    _write(ov, {"compress_tiff_zip": {"zip_subfolder_name": "a/b", "thumb_page": 5, "safe_mode": False,
                                      "cap_workers": 99}})
    _use_overlay(monkeypatch, ov)
    assert bd("compress_tiff_zip.py", "zip_subfolder_name") == "ZIP"
    assert bd("compress_tiff_zip.py", "cap_workers") == 0
    assert bd("compress_tiff_zip.py", "safe_mode") is True


def test_T10_a_value_of_the_wrong_type_falls_back(tmp_path, monkeypatch):
    _use_overlay(monkeypatch, tmp_path / "s.json")
    monkeypatch.setattr(convert_tiff, "_overlay_values",
                        lambda script: {"cap_workers": "three", "zip_subfolder_name": 7, "thumb_quality": 90})
    bd = convert_tiff._backend_default
    assert bd("compress_tiff_zip.py", "cap_workers") == 0
    assert bd("compress_tiff_zip.py", "zip_subfolder_name") == "ZIP"
    assert bd("compress_tiff_zip.py", "thumb_quality") == "85"


def test_T10_an_unreadable_overlay_changes_nothing(tmp_path, monkeypatch):
    for raw in (b"{ nope", b"[1]", b'{"compress_tiff_zip": 3}', b"\xff\xfe\x00"):
        ov = _write(tmp_path / "s.json", None, raw=raw)
        _use_overlay(monkeypatch, ov)
        assert convert_tiff._backend_default("compress_tiff_zip.py", "zip_subfolder_name") == "ZIP"
        assert convert_tiff._overlay_values("compress_tiff_zip.py") == {}
        _path, values, warnings = convert_tiff._overlay_scan("compress_tiff_zip.py")
        assert values == {} and len(warnings) == 1 and "not read" in warnings[0]


def test_T10_the_convert_tiff_section_is_applied_to_this_module(tmp_path, monkeypatch):
    ns = {"WORKER_MEMORY_FRACTION": 0.8, "WORKER_MEMORY_LIMIT": "both", "DIAGNOSE_CACHE": True,
          "CONVERT_TIMEOUT_S": 60, "COMPARE_TIMEOUT_S": 120, "TIMEOUT_S_PER_MB": 0.25, "MODE": 1}
    ov = _write(tmp_path / "s.json", {"convert_tiff": {
        "WORKER_MEMORY_FRACTION": 0.5, "WORKER_MEMORY_LIMIT": "physical", "DIAGNOSE_CACHE": False,
        "CONVERT_TIMEOUT_S": 90, "COMPARE_TIMEOUT_S": 0, "TIMEOUT_S_PER_MB": 0.001, "MODE": 5,
        "TOOLKIT_VERSION": "9"}})
    monkeypatch.setenv(ENV, str(ov))
    result = convert_tiff._apply_settings_overlay(ns)
    assert ns == {"WORKER_MEMORY_FRACTION": 0.5, "WORKER_MEMORY_LIMIT": "physical",
                  "DIAGNOSE_CACHE": False, "CONVERT_TIMEOUT_S": 90, "COMPARE_TIMEOUT_S": 120,
                  "TIMEOUT_S_PER_MB": 0.25, "MODE": 1}, "COMPARE (0), TIMEOUT_PER_MB (0.001) and the unknown names are refused"
    assert sorted(result["applied"]) == ["CONVERT_TIMEOUT_S", "DIAGNOSE_CACHE", "WORKER_MEMORY_FRACTION",
                                         "WORKER_MEMORY_LIMIT"]
    assert len(result["warnings"]) == 4
    # the live module still has its own values (conftest turned the overlay off for the import)
    assert convert_tiff.TOOLKIT_VERSION != "9"


# --- T11: the Script settings menu -----------------------------------------------------------------

def _menu(monkeypatch, answers):
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    it = iter(answers)
    said = []

    def fake_input(prompt=""):
        said.append(("ask", prompt))
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"the menu asked more than scripted: {prompt!r}")
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("builtins.print", lambda *a, **k: said.append(("say", " ".join(str(x) for x in a))))
    return said


def _row(section, name):
    script = {s: sc for s, _l, sc in convert_tiff._OVERLAY_MENU}[section]
    return [r[0] for r in convert_tiff._overlay_menu_rows(section, script)].index(name) + 1


def _section_no(section):
    return [s for s, _l, _sc in convert_tiff._OVERLAY_MENU].index(section) + 1


def _go(monkeypatch, tmp_path, answers, content=None, raw=None):
    ov = tmp_path / HOSTILE / "settings.json"
    if content is not None or raw is not None:
        _write(ov, content, raw=raw)
    _use_overlay(monkeypatch, ov)
    said = _menu(monkeypatch, answers)
    convert_tiff.script_settings_menu(SimpleNamespace())
    return ov, said


def _folder_files(ov):
    return sorted(p.name for p in ov.parent.iterdir())


def test_T11_creates_the_file_and_keeps_the_discovery_in_step(tmp_path, monkeypatch):
    ov, said = _go(monkeypatch, tmp_path, [
        str(_section_no("compress_tiff_zip")), str(_row("compress_tiff_zip", "zip_subfolder_name")),
        "ZIPPED", "0", "0"])
    try:
        assert json.loads(ov.read_text(encoding="utf-8")) == {"compress_tiff_zip": {"zip_subfolder_name": "ZIPPED"}}
        assert _folder_files(ov) == ["settings.json"], "a temp file was left behind"
        assert "zipped" in convert_tiff._TOOL_OUTPUT_PARTS
        # and a script really reads what the menu wrote (same grammar, same file)
        assert convert_tiff._overlay_values("compress_tiff_zip.py") == {"zip_subfolder_name": "ZIPPED"}
    finally:
        monkeypatch.setenv(ENV, "")
        convert_tiff._refresh_tool_output_parts()


def test_T11_keeps_unknown_sections_keys_and_the_order(tmp_path, monkeypatch):
    start = {"other_tool": {"a": [1, 2], "b": "写真"}, "compress_tiff_zip": {"FutureSetting": 7, "cap_workers": 2},
             "copy_exif": {"magick_timeout": 45}}
    ov, _ = _go(monkeypatch, tmp_path, [
        str(_section_no("compress_tiff_zip")), str(_row("compress_tiff_zip", "thumb_quality")), "85",
        str(_row("compress_tiff_zip", "srgb_profile")), "none", "0", "0"], content=start)
    got = json.loads(ov.read_text(encoding="utf-8"))
    assert got["other_tool"] == start["other_tool"] and got["copy_exif"] == start["copy_exif"]
    assert got["compress_tiff_zip"] == {"FutureSetting": 7, "cap_workers": 2, "thumb_quality": 85,
                                        "srgb_profile": None}, "an intstr is stored as a JSON int; empty path is null"
    assert list(got) == ["other_tool", "compress_tiff_zip", "copy_exif"]
    assert "写真" in ov.read_text(encoding="utf-8"), "ensure_ascii=False"
    assert _folder_files(ov) == ["settings.json"]


def test_T11_a_name_of_v3_0_0_is_said_and_goes_with_a_change(tmp_path, monkeypatch):
    """T7: a file written for v3.0.0 holds MagickTimeout; v3.1 takes magick_timeout and the
    scripts ignore the old name. The screen says so (it showed `file: -` and nothing else),
    and setting the value here writes the new name and drops the old one; '-' drops it too.
    A name nobody knows stays, and is said."""
    start = {"compress_tiff_zip": {"MagickTimeout": 180, "FutureSetting": 7, "cap_workers": 2}}
    row = str(_row("compress_tiff_zip", "magick_timeout"))
    ov, said = _go(monkeypatch, tmp_path, [str(_section_no("compress_tiff_zip")), row, "120", "0",
                                           "0"], content=start)
    text = "\n".join(t for kind, t in said if kind == "say")
    assert "MagickTimeout (written magick_timeout since v3.1" in text and "FutureSetting" in text
    assert "cap_workers (written" not in text
    assert json.loads(ov.read_text(encoding="utf-8")) == {
        "compress_tiff_zip": {"FutureSetting": 7, "cap_workers": 2, "magick_timeout": 120}}
    ov, _ = _go(monkeypatch, tmp_path, [str(_section_no("compress_tiff_zip")), row, "-", "0", "0"],
                content={"compress_tiff_zip": {"MagickTimeout": 180}, "keep": {"x": 1}})
    assert json.loads(ov.read_text(encoding="utf-8")) == {"keep": {"x": 1}}


def test_T11_dash_removes_a_key_and_an_empty_section_goes_with_it(tmp_path, monkeypatch):
    ov, _ = _go(monkeypatch, tmp_path, [
        str(_section_no("compress_tiff_zip")), str(_row("compress_tiff_zip", "cap_workers")), "-", "0", "0"],
        content={"compress_tiff_zip": {"cap_workers": 2}, "keep": {"x": 1}})
    assert json.loads(ov.read_text(encoding="utf-8")) == {"keep": {"x": 1}}
    # removing a key that is not there writes nothing
    before = ov.read_bytes()
    ov, _ = _go(monkeypatch, tmp_path, [
        str(_section_no("compress_tiff_zip")), str(_row("compress_tiff_zip", "cap_workers")), "-", "0", "0"])
    assert ov.read_bytes() == before


@pytest.mark.parametrize("raw", [b"{ nope", b"[1, 2]", b'"text"', b"\xff\xfe\x00\x01", b"",
                                 # v3.1 phase 6 (W-O6): what the scripts refuse, the menu refuses
                                 b'{"compress_tiff_zip": {"thumb_size": 64, "THUMB_SIZE": 128}}',
                                 b'{"compress_tiff_zip": {"": 1}}'])
def test_T11_a_file_that_cannot_be_read_is_refused_and_left_byte_for_byte(tmp_path, monkeypatch, raw):
    ov, said = _go(monkeypatch, tmp_path, [], raw=raw)        # no answer scripted: it must not ask
    assert ov.read_bytes() == raw
    assert _folder_files(ov) == ["settings.json"]
    assert any("cannot be read" in t for k, t in said if k == "say")


def test_T11_a_section_that_is_not_an_object_is_refused_and_left_alone(tmp_path, monkeypatch):
    raw = b'{"compress_tiff_zip": [1, 2], "other": {}}'
    ov, said = _go(monkeypatch, tmp_path, [str(_section_no("compress_tiff_zip")), "0"], raw=raw)
    assert ov.read_bytes() == raw
    assert any("is not an object" in t for k, t in said if k == "say")


def test_T11_an_invalid_value_writes_nothing(tmp_path, monkeypatch):
    raw = json.dumps({"compress_tiff_zip": {"cap_workers": 2}}).encode()
    sec = str(_section_no("compress_tiff_zip"))
    for row, text in (("thumb_quality", "101"), ("thumb_quality", "abc"), ("zip_subfolder_name", "a/b"),
                      ("zip_subfolder_name", ".."), ("cap_workers", "65"), ("cap_workers", "1.5"),
                      ("worker_memory_fraction", "2.5"), ("magick_timeout", "0")):
        ov, said = _go(monkeypatch, tmp_path, [sec, str(_row("compress_tiff_zip", row)), text, "0", "0"], raw=raw)
        assert ov.read_bytes() == raw, (row, text)
        assert any("Not changed" in t for k, t in said if k == "say"), (row, text)
    assert _folder_files(ov) == ["settings.json"]


def test_T11_the_write_is_atomic_a_failed_replace_leaves_the_old_file(tmp_path, monkeypatch):
    raw = json.dumps({"compress_tiff_zip": {"cap_workers": 2}}).encode()
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(convert_tiff.os, "replace", boom)
    ov, said = _go(monkeypatch, tmp_path, [
        str(_section_no("compress_tiff_zip")), str(_row("compress_tiff_zip", "cap_workers")), "5",
        "0", "0"], raw=raw)
    monkeypatch.setattr(convert_tiff.os, "replace", real_replace)
    assert ov.read_bytes() == raw
    assert _folder_files(ov) == ["settings.json"], "the temp file must be removed"
    assert any("Could not write" in t for k, t in said if k == "say")


def test_T11_the_temp_file_lives_in_the_files_own_folder(tmp_path, monkeypatch):
    seen = []
    real = convert_tiff.tempfile.mkstemp

    def spy(*a, **k):
        seen.append(k)
        return real(*a, **k)
    monkeypatch.setattr(convert_tiff.tempfile, "mkstemp", spy)
    ov, _ = _go(monkeypatch, tmp_path, [
        str(_section_no("generate_thumbnails")), str(_row("generate_thumbnails", "size")), "128", "0", "0"])
    assert seen and seen[0]["dir"] == str(ov.parent) and seen[0]["prefix"] == ".tiff_workflow_settings_"
    assert json.loads(ov.read_text(encoding="utf-8")) == {"generate_thumbnails": {"size": 128}}


def test_T11_blank_variable_means_off_and_nothing_is_asked(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "")
    said = _menu(monkeypatch, [])
    convert_tiff.script_settings_menu(SimpleNamespace())
    assert any("OFF" in t for k, t in said if k == "say") and not any(k == "ask" for k, _ in said)


def test_T11_the_convert_tiff_section_says_when_it_applies(tmp_path, monkeypatch):
    ov, said = _go(monkeypatch, tmp_path, [
        str(_section_no("convert_tiff")), str(_row("convert_tiff", "DIAGNOSE_CACHE")), "no", "0", "0"])
    assert json.loads(ov.read_text(encoding="utf-8")) == {"convert_tiff": {"DIAGNOSE_CACHE": False}}
    assert any("next time the wizard starts" in t for k, t in said if k == "say")


def test_T11_every_listed_setting_has_its_script_value_and_a_spec(tmp_path, monkeypatch):
    for section, _label, script in convert_tiff._OVERLAY_MENU:
        rows = convert_tiff._overlay_menu_rows(section, script)
        assert rows, section
        for name, spec, script_value, _help in rows:
            assert script_value is not None, (section, name)
            assert convert_tiff._overlay_value(spec, object())[0] is False
