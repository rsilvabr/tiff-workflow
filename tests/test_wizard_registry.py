"""v2.9.0: the wizard's option REGISTRY (convert_tiff.py, "Option Registry").

T1  golden   -- the argv the command builders produced BEFORE the registry existed, for 13
                workflow dicts (tests/data/wizard_registry_golden.json). Generated once from
                the pre-registry builders of the PowerShell edition; never regenerate it. Each
                case holds the `pwsh -NoProfile -File <script>.ps1 args` line and the Windows
                PowerShell 5.1 `-Command` line: the ARGUMENTS of the first are what today's
                builders must still produce. Both lines are what a preset saved by v2.x holds,
                and since v3.1 neither is replayed (tests/test_powershell_journals.py).
T2  coverage -- every parameter of every backend is in REGISTRY or NOT_OFFERED, and vice versa.
T3  advanced -- the per-run Advanced options screen.
T4  a saved workflow that carries `advanced` rebuilds the same argv.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import convert_tiff

GOLDEN = Path(__file__).parent / "data" / "wizard_registry_golden.json"
TOKEN = "<SCRIPT_DIR>"
# Hostile on purpose: Japanese, [ ], a space, a %. The builders never touch the disk.
HOSTILE = "D:\\写真 [FINAL] x\\2019 100%"
HOSTILE2 = "E:\\second session\\一時"

# (id, builder, workflow, folders, extra_flags)
CASES = [
    ("c_mode0_one_folder_everything", "compress",
     {"mode": 0, "workers": 4, "staging": "D:\\stage dir", "cap_workers": 2, "dry_run": True,
      "safe_mode": True, "skip_lzw": False}, [HOSTILE], None),
    ("c_mode2_two_folders_output_dup_workers_over_max", "compress",
     {"mode": 2, "output_dir": "D:\\out [x]", "duplicate_action": "Numbered", "workers": 99,
      "safe_mode": False, "skip_lzw": True, "exclude_folders": "ZIP;OLD_TIFFs"},
     [HOSTILE, HOSTILE2], None),
    ("c_mode8_delete_overwrite_force_parallel", "compress",
     {"mode": 8, "delete_source": True, "overwrite": True, "force_parallel": True,
      "workers": 8, "cap_workers": 0}, [HOSTILE], None),
    ("c_mode1_force_sequential_workers_zero", "compress",
     {"mode": 1, "workers": 0, "force_sequential": True, "force_parallel": False,
      "cap_workers": 0}, [HOSTILE], None),
    ("c_thumbnail_every_key", "compress",
     {"mode": 1, "generate_thumbnail": True, "thumb_size": 512, "thumb_quality": "90",
      "thumb_format": "png", "thumb_page": "1", "skip_compressed_with_thumb": True},
     [HOSTILE], None),
    ("c_thumbnail_no_thumb_keys", "compress",
     {"mode": 1, "generate_thumbnail": True}, [HOSTILE], None),
    ("c_thumbnail_off_keys_present", "compress",
     {"mode": 1, "generate_thumbnail": False, "thumb_size": 512, "thumb_quality": "90",
      "thumb_format": "png", "thumb_page": "1", "skip_compressed_with_thumb": True},
     [HOSTILE], None),
    ("c_empty_workflow_no_folders", "compress", {}, None, None),
    ("c_odd_values", "compress",
     {"mode": 0, "safe_mode": "yes", "skip_lzw": None, "force_parallel": 1,
      "force_sequential": "yes", "workers": "6", "cap_workers": "3", "dry_run": 0,
      "output_dir": "", "staging": ""}, [HOSTILE], None),
    ("e_every_key_extra_flags", "copy_exif",
     {"workers": 3, "staging": "D:\\stage dir", "output_dir": "D:\\out [x]", "dry_run": True,
      "skip_exif": True, "compress_zip": True, "safe_mode": False, "skip_lzw": True,
      "overwrite": True, "exclude_folders": "ZIP;OLD_TIFFs"}, [HOSTILE],
     ["-AutoFind", "-FolderPattern", "*Fuji*"]),
    ("e_empty_workflow_no_folders", "copy_exif", {}, None, None),
    ("e_two_folders_workers_over_max_safe_true", "copy_exif",
     {"workers": 100, "safe_mode": True, "skip_lzw": False}, [HOSTILE, HOSTILE2], None),
    ("e_odd_values", "copy_exif",
     {"safe_mode": "yes", "skip_lzw": 0, "skip_exif": 0, "workers": "6"}, [HOSTILE], None),
]
def _golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _golden_cmd(cid, shell):
    return [a.replace(TOKEN, str(convert_tiff.SCRIPT_DIR)) for a in _golden()[cid][shell]]


#: The script each golden line named -> the backend that takes the same arguments today.
_PS1_TO_PY = {
    "compress_tiff_zip.ps1": "compress_tiff_zip.py",
    "copy_exif_to_TIFF_ps7.ps1": "copy_exif_to_tiff.py",
}


def _build(builder: str, workflow: dict, folders, extra):
    paths = [Path(f) for f in folders] if folders is not None else None
    if builder == "compress":
        return convert_tiff.build_compress_command(dict(workflow), paths)
    return convert_tiff.build_copy_exif_command(dict(workflow), paths, extra)


# --- T1 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_T1_golden_argv_unchanged(case):
    """The arguments are byte for byte the golden ones; only what runs them changed
    (`pwsh -NoProfile -File <script>.ps1` -> this Python and <script>.py)."""
    cid, builder, wf, folders, extra = case
    golden = _golden_cmd(cid, "pwsh")
    assert golden[:3] == ["pwsh", "-NoProfile", "-File"]
    argv = _build(builder, wf, folders, extra)
    assert argv[0] == sys.executable
    assert argv[1] == str(convert_tiff.SCRIPT_DIR / _PS1_TO_PY[Path(golden[3]).name])
    assert Path(argv[1]).name in convert_tiff.BACKEND_SCRIPTS
    assert argv[2:] == golden[4:]


@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_T1_a_golden_command_line_is_no_longer_replayed(case, shell):
    """What a preset saved by v2.x holds (either shell's form) is not read since v3.1: the
    journal reader knows one form, the one today's builders write."""
    cid, builder, wf, folders, extra = case
    assert convert_tiff._journal_parts(_golden_cmd(cid, shell)) is None
    today = _build(builder, wf, folders, extra)
    assert convert_tiff._rebuild_journaled_command(convert_tiff._journal_parts(today)) == today


def test_T1_golden_file_covers_exactly_the_cases():
    golden = _golden()
    assert set(golden) == {c[0] for c in CASES}
    for cid in golden:
        assert set(golden[cid]) == {"pwsh", "powershell"}


# --- T2 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("backend", ["compress", "copy_exif", "thumbnails"])
def test_T2_every_parameter_is_registered_or_declared_not_offered(backend):
    registered = {o.param for o in convert_tiff.REGISTRY[backend]}
    declined = set(convert_tiff.NOT_OFFERED[backend])
    assert not (registered & declined), f"in both REGISTRY and NOT_OFFERED: {sorted(registered & declined)}"
    for script in convert_tiff.REGISTRY_SCRIPTS[backend]:
        params = set(convert_tiff._backend_param_names(script))
        assert params, f"no parameters read from {script}"
        for p in sorted(params):
            assert p in registered or p in declined, (
                f"-{p} of {script} is in neither REGISTRY['{backend}'] nor NOT_OFFERED['{backend}']")
        for p in sorted(registered | declined):
            assert p in params, f"-{p} is in the registry for {backend} but not in {script}"


def test_T2_registry_entries_are_well_formed():
    kinds = {"input", "value", "notnone", "int_notnone", "workers", "flag", "true_flag",
             "bool", "true_bool", "none"}
    levels = {"asked", "advanced", "overlay", "internal"}
    for backend, opts in convert_tiff.REGISTRY.items():
        names = [o.param for o in opts]
        assert len(names) == len(set(names)), f"{backend}: a parameter is registered twice"
        for o in opts:
            assert o.kind in kinds and o.level in levels, o
            assert o.label and o.help, f"{backend}.{o.param}: label and help are required"
            assert (o.level == "advanced") == o.key.startswith("advanced."), o
            assert (o.level == "advanced") == bool(o.value_spec), o
            if o.level == "overlay":
                assert o.kind == "none", f"{o.param}: overlay-only options are never emitted"


def test_T2_the_options_that_delete_or_overwrite_are_marked_danger():
    danger = {o.param for o in convert_tiff.REGISTRY["compress"] if o.danger}
    assert danger == {"SafeMode", "Overwrite", "DeleteSource"}


# --- T3 / T4: the per-run Advanced options screen --------------------------------------------

#: captured at import: tests/conftest.py replaces the module attribute with a "No" for every
#: other test, because they script the wizard's prompts one by one.
_REAL_ADVANCED = getattr(convert_tiff, "step_advanced_options", None)


def REAL_ADVANCED(cfg, workflow):
    assert _REAL_ADVANCED is not None, "convert_tiff.step_advanced_options does not exist"
    return _REAL_ADVANCED(cfg, workflow)


def _cfg():
    from types import SimpleNamespace
    return SimpleNamespace(config=convert_tiff.ToolConfig(),
                           save_config=lambda: None)


def _script_plain(monkeypatch, answers):
    """Answer the plain-text (no rich) prompts in order; the test fails if they run out."""
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", False)
    asked = []
    it = iter(answers)

    def fake_input(prompt=""):
        asked.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"the screen asked more than scripted: {prompt!r}")
    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _script_rich(monkeypatch, answers):
    """The same for the rich UI path (Confirm.ask / Prompt.ask)."""
    monkeypatch.setattr(convert_tiff, "RICH_AVAILABLE", True)
    monkeypatch.setattr(convert_tiff, "console", type("C", (), {"print": staticmethod(lambda *a, **k: None)})())
    it = iter(answers)

    def nxt(*a, **k):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"the screen asked more than scripted: {a!r}")
    monkeypatch.setattr(convert_tiff.Confirm, "ask", staticmethod(lambda *a, **k: nxt(*a, **k) in ("y", True)))
    monkeypatch.setattr(convert_tiff.Prompt, "ask", staticmethod(nxt))


UIS = [pytest.param(_script_plain, id="plain"), pytest.param(_script_rich, id="rich")]


@pytest.mark.parametrize("ui", UIS)
def test_T3_answering_no_stores_nothing(monkeypatch, ui):
    wf = {"origin": "free_compress", "mode": 1, "workers": 4}
    before = json.dumps(wf, sort_keys=True)
    ui(monkeypatch, ["n"])
    assert REAL_ADVANCED(_cfg(), wf) is True
    assert json.dumps(wf, sort_keys=True) == before and "advanced" not in wf


@pytest.mark.parametrize("ui", UIS)
def test_T3_an_edited_value_equal_to_the_default_is_still_emitted(monkeypatch, ui):
    # 1 = MagickTimeout of compress; the script's own default is 30
    assert convert_tiff._backend_param_defaults("compress_tiff_zip.py")["MagickTimeout"] == 30
    wf = {"origin": "free_compress", "mode": 1}
    ui(monkeypatch, ["y", "1", "30", "0"])
    REAL_ADVANCED(_cfg(), wf)
    assert wf["advanced"] == {"compress": {"MagickTimeout": 30}}
    argv = convert_tiff.build_compress_command(wf, [Path(HOSTILE)])
    assert argv[argv.index("-MagickTimeout") + 1] == "30"


@pytest.mark.parametrize("ui", UIS)
def test_T3_an_option_not_edited_is_not_passed(monkeypatch, ui):
    wf = {"origin": "free_compress", "mode": 1, "workers": 4}
    # opens the screen, looks, leaves: nothing was edited
    ui(monkeypatch, ["y", "0"])
    REAL_ADVANCED(_cfg(), wf)
    assert "advanced" not in wf
    argv = convert_tiff.build_compress_command(wf, [Path(HOSTILE)])
    for p in ("-MagickTimeout", "-WorkerMemoryFraction", "-SrgbProfile", "-FailOnWarn"):
        assert p not in argv


@pytest.mark.parametrize("ui", UIS)
def test_T3_an_invalid_value_is_not_stored(monkeypatch, ui):
    wf = {"origin": "free_compress", "mode": 1}
    # MagickTimeout: text, zero, negative, a float; WorkerMemoryFraction: 2.5 (above 2), -0.1, text
    ui(monkeypatch, ["y", "1", "abc", "1", "0", "1", "-5", "1", "2.5",
                     "3", "2.5", "3", "-0.1", "3", "lots", "9", "0"])
    REAL_ADVANCED(_cfg(), wf)
    assert "advanced" not in wf


@pytest.mark.parametrize("ui", UIS)
def test_T3_float_and_path_options(monkeypatch, ui):
    wf = {"origin": "compress", "mode": 9}
    ui(monkeypatch, ["y", "3", "0.5", "2", "D:\\profiles\\写真 [x].icc", "0"])
    REAL_ADVANCED(_cfg(), wf)
    assert wf["advanced"] == {"compress": {"WorkerMemoryFraction": 0.5,
                                           "SrgbProfile": "D:\\profiles\\写真 [x].icc"}}
    argv = convert_tiff.build_compress_command(wf, [Path(HOSTILE)])
    assert argv[argv.index("-WorkerMemoryFraction") + 1] == "0.5"
    assert argv[argv.index("-SrgbProfile") + 1] == "D:\\profiles\\写真 [x].icc"


@pytest.mark.parametrize("ui", UIS)
def test_T3_dash_takes_an_edit_back(monkeypatch, ui):
    wf = {"origin": "free_compress", "mode": 1}
    ui(monkeypatch, ["y", "1", "60", "1", "-", "0"])
    REAL_ADVANCED(_cfg(), wf)
    assert "advanced" not in wf


@pytest.mark.parametrize("ui", UIS)
def test_T3_combined_workflow_lists_both_backends_and_keeps_them_apart(monkeypatch, ui):
    wf = {"origin": "both", "mode": 9, "folders": [HOSTILE]}
    # 1-3 compress (MagickTimeout, SrgbProfile, WorkerMemoryFraction), 4-6 copy_exif
    # (MagickTimeout, WorkerMemoryFraction, FailOnWarn)
    ui(monkeypatch, ["y", "4", "77", "6", "yes", "3", "0.25", "0"])
    REAL_ADVANCED(_cfg(), wf)
    assert wf["advanced"] == {"copy_exif": {"MagickTimeout": 77, "FailOnWarn": True},
                              "compress": {"WorkerMemoryFraction": 0.25}}
    exif = convert_tiff.build_copy_exif_command(wf, [Path(HOSTILE)])
    comp = convert_tiff.build_compress_command(wf, [Path(HOSTILE)])
    assert exif[exif.index("-MagickTimeout") + 1] == "77" and "-FailOnWarn" in exif
    assert "-WorkerMemoryFraction" not in exif
    assert comp[comp.index("-WorkerMemoryFraction") + 1] == "0.25"
    assert "-MagickTimeout" not in comp and "-FailOnWarn" not in comp


@pytest.mark.parametrize("ui", UIS)
def test_T3_copy_exif_workflow_never_offers_compress_options(monkeypatch, ui):
    wf = {"origin": "copy_exif", "folders": [HOSTILE]}
    # rows are copy_exif's: 1 MagickTimeout, 2 WorkerMemoryFraction, 3 FailOnWarn; 4 does not exist
    ui(monkeypatch, ["y", "4", "3", "no", "1", "45", "0"])
    REAL_ADVANCED(_cfg(), wf)
    assert wf["advanced"] == {"copy_exif": {"FailOnWarn": False, "MagickTimeout": 45}}
    argv = convert_tiff.build_copy_exif_command(wf, [Path(HOSTILE)])
    assert "-FailOnWarn" not in argv            # off is the switch's default
    assert argv[argv.index("-MagickTimeout") + 1] == "45"


def test_T3_a_workflow_without_a_registry_backend_is_not_asked(monkeypatch):
    _script_plain(monkeypatch, [])             # any prompt would raise
    wf = {"origin": "thumbnails"}
    assert REAL_ADVANCED(_cfg(), wf) is True and wf == {"origin": "thumbnails"}


def test_T3_the_screen_is_called_after_each_step_basic_params():
    import re
    src = (Path(convert_tiff.__file__)).read_text(encoding="utf-8")
    calls = re.findall(r"^( +)step_basic_params\(cfg, workflow\)\n\1step_advanced_options\(cfg, workflow\)\n",
                       src, flags=re.M)
    assert len(calls) == 3 == src.count("step_basic_params(cfg, workflow)")


def test_T3_a_hand_edited_value_that_fails_its_spec_is_dropped_not_passed():
    wf = {"origin": "free_compress", "mode": 1,
          "advanced": {"compress": {"MagickTimeout": "abc", "WorkerMemoryFraction": 7,
                                    "SrgbProfile": 5}}}
    argv = convert_tiff.build_compress_command(wf, [Path(HOSTILE)])
    for p in ("-MagickTimeout", "-WorkerMemoryFraction", "-SrgbProfile"):
        assert p not in argv
    for malformed in (["x"], "x", {"compress": ["x"]}, {"compress": None}):
        argv = convert_tiff.build_compress_command({"mode": 1, "advanced": malformed}, [Path(HOSTILE)])
        assert "-MagickTimeout" not in argv


def test_T4_a_saved_workflow_with_advanced_rebuilds_the_same_argv(tmp_path, monkeypatch):
    wf = {"origin": "both", "mode": 9, "workers": 4, "dry_run": False, "safe_mode": True,
          "advanced": {"compress": {"MagickTimeout": 30, "WorkerMemoryFraction": 0.5},
                       "copy_exif": {"FailOnWarn": True, "MagickTimeout": 90}}}
    folders = [Path(HOSTILE)]
    expect_c = convert_tiff.build_compress_command(wf, folders)
    expect_e = convert_tiff.build_copy_exif_command(wf, folders)
    # the JSON a preset / the journal stores
    stored = json.loads(json.dumps(wf, ensure_ascii=False))
    assert convert_tiff.build_compress_command(stored, folders) == expect_c
    assert convert_tiff.build_copy_exif_command(stored, folders) == expect_e
    # the manifest's per-entry command carries it too
    entry = convert_tiff.build_manifest_entry_cmd(HOSTILE, "", 1, wf)
    joined = " ".join(entry)
    assert "-MagickTimeout" in joined and "-WorkerMemoryFraction" in joined
    # and the config file's last_manifest_workflow, written and read back by a real manager
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = convert_tiff.ConfigManager()
    convert_tiff._save_last_run(cfg, "manifest", "x", None, manifest_workflow=wf)
    back = convert_tiff.ConfigManager().config.last_manifest_workflow
    assert back["advanced"] == wf["advanced"]
    assert convert_tiff.build_compress_command(dict(back, mode=9), folders) == expect_c


if __name__ == "__main__":
    sys.exit(__doc__)
