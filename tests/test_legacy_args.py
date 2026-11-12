"""v3.1 phase 8: tools/legacy_args.py turns a command line of v3.0.0 (-Name value, -Name:value,
-Switch:$false, names without case, an unambiguous prefix) into v3.1's (--kebab-case value,
--x / --no-x).

The translation converts the tests, the golden lines of the wizard, the presets and a user's
.bat, so a line and its translation must bind to the same values, parameter by parameter: for
the 13 golden lines, and for every form the old reader took, for every parameter of the three
backends. Hostile values on purpose (Japanese, [ ], a space, a %, a leading '-')."""

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tiff_workflow_common as tw  # noqa: E402
import legacy_args  # noqa: E402

GOLDEN = ROOT / "tests" / "data" / "wizard_registry_golden.json"
HOSTILE = "D:\\写真 [FINAL] x\\2019 100%"
BACKENDS = sorted(legacy_args.BACKENDS)
_PS1_TO_BACKEND = {"compress_tiff_zip.ps1": "compress", "copy_exif_to_TIFF_ps7.ps1": "copy-exif"}


def _params(backend):
    return legacy_args.backend_module(backend).PARAMS


def _old(argv, params):
    """What v3.0.0 bound `argv` to."""
    return tw._parse_legacy(list(argv), params)


def _new(argv, params):
    """What v3.1 binds `argv` to -- through argparse, never through the old reader."""
    assert not any(tw._LEGACY_TOKEN.match(a) for a in argv), argv
    return tw.parse_args(list(argv), params)


def _accepted(p, raw):
    try:
        tw._convert_param(p, raw)
        return True
    except tw.UsageError:
        return False


def _values(p):
    """Values of every shape the old reader took for `p`, each one it accepts."""
    if p.kind == "switch":
        return [None, "$true", "$false", "0", "1", "True", "FALSE"]
    if p.kind == "bool":
        return ["$true", "$false", "1", "0", "True", "FALSE"]
    if p.kind in ("int", "float"):
        cands = [p.lo, p.hi, p.default, 1, 0, " 7 ", "-1"]
    elif p.choices:
        cands = [c.swapcase() for c in p.choices]
    else:
        cands = [p.default, HOSTILE, "", "-old", "a:b", "x y", "50%", "all", "90"]
    out = []
    for c in cands:
        if c is not None and str(c) not in out and _accepted(p, str(c)):
            out.append(str(c))
    assert out, f"no value accepted for -{p.name}"
    return out


def _names(p, params):
    """The ways v3.0.0 let a user name `p`: exact, other case, the shortest unambiguous prefix
    -- of the name it had then (ThumbSize; thumb_size since v3.1, T7)."""
    old = tw._legacy_name(p.name)
    names = [old, old.lower(), old.upper()]
    for n in range(1, len(old) + 1):
        try:
            if tw._resolve_param(old[:n], params) is p:
                names.append(old[:n])
                break
        except tw.UsageError:
            pass
    return names


def _forms(p, params):
    for name in _names(p, params):
        for v in _values(p):
            if v is None:
                yield [f"-{name}"]
            elif p.kind == "switch":
                yield [f"-{name}:{v}"]
            else:
                yield [f"-{name}", v]
                yield [f"-{name}:{v}"]


# --- the long names ----------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_every_parameter_has_a_clean_long_name(backend):
    params = _params(backend)
    names = [tw.cli_name(p.name) for p in params]
    assert len(set(names)) == len(names)
    for p, n in zip(params, names):
        assert re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)*", p.name), p.name    # T7: snake_case
        assert re.fullmatch(r"--[a-z0-9]+(-[a-z0-9]+)*", n), n
        assert n[2:].replace("-", "_") == p.name


def test_long_names_and_the_names_of_v3_0_0():
    assert tw.cli_name("safe_mode") == "--safe-mode"
    assert tw.cli_name("input_dir_file") == "--input-dir-file"
    assert tw.cli_name("skip_if_tiff_has_exif") == "--skip-if-tiff-has-exif"
    assert tw.cli_name("size") == "--size"
    assert tw.cli_switch("safe_mode", False) == "--no-safe-mode"
    assert tw.cli_value("exclude_folders", "-old") == ["--exclude-folders=-old"]
    assert tw.cli_value("workers", 4) == ["--workers", "4"]
    assert tw._legacy_name("skip_lzw_as_compressed") == "SkipLzwAsCompressed"
    assert tw._legacy_name("icc_policy") == "IccPolicy"
    assert tw._legacy_name("size") == "Size"


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_name_of_v3_0_0_is_known(backend):
    """The names the old reader matches are the names of v3.0.0's tables, every one (they are
    in the golden lines and in users' .bat files); a name of then that does not come back
    from its v3.1 name could not be translated."""
    v300 = {
        "compress": "Mode InputDir OutputDir ZipSuffix ZipSubfolderName ExportMarker "
                    "ExportZipSubfolder ExportTiffSubfolder Workers CapWorkers DryRun SafeMode "
                    "SkipLzwAsCompressed Overwrite DeleteSource ForceParallel ForceSequential "
                    "StagingDir DuplicateAction MagickTimeout ExcludeFolders GenerateThumbnail "
                    "ThumbSize ThumbQuality ThumbFormat ThumbPage SkipCompressedWithThumb "
                    "SrgbProfile WorkerMemoryFraction InputDirFile RestoreStranded",
        "copy-exif": "InputDir Workers DryRun SkipIfTiffHasExif SkipLzwAsCompressed SafeMode "
                     "IccPolicy CompressZip OutputDir StagingDir Overwrite AutoFind "
                     "FolderPattern MagickTimeout FailOnWarn ExcludeFolders "
                     "WorkerMemoryFraction InputDirFile",
        "thumbnails": "InputDir Size OutputDir Remove DryRun Recursive Workers Page Quality "
                      "Format SrgbProfile ExcludeFolders",
    }[backend].split()
    assert [tw._legacy_name(p.name) for p in _params(backend)] == v300


# --- the translation binds the same --------------------------------------------------------

def _golden_cases():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for cid, lines in golden.items():
        line = lines["pwsh"]
        yield pytest.param(_PS1_TO_BACKEND[line[3].split("\\")[-1]], line[4:], id=cid)


@pytest.mark.parametrize("backend, old", list(_golden_cases()))
def test_every_golden_line_binds_the_same_after_translation(backend, old):
    params = _params(backend)
    assert _new(legacy_args.translate(old, params), params) == _old(old, params)


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_form_of_every_parameter_binds_the_same(backend):
    params = _params(backend)
    wrong, n = [], 0
    for p in params:
        for old in _forms(p, params):
            n += 1
            new = legacy_args.translate(old, params)
            if _new(new, params) != _old(old, params):
                wrong.append((old, new))
    assert not wrong, f"{len(wrong)} of {n} lines bind differently: {wrong[:5]}"
    assert n > 20 * len(params)


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_parameter_in_one_line_binds_the_same(backend):
    params = _params(backend)
    old = []
    for p in params:
        if p.name == "input_dir_file":            # one of -InputDir / -InputDirFile is enough
            continue
        v = HOSTILE if p.kind == "str" and _accepted(p, HOSTILE) else _values(p)[-1]
        name = tw._legacy_name(p.name)
        old += [f"-{name}:{v}"] if p.kind == "switch" else [f"-{name}", v]
    new = legacy_args.translate(old, params)
    assert _new(new, params) == _old(old, params)
    assert _old(old, params)[1] == {p.name for p in params} - {"input_dir_file"}


def test_the_last_of_a_repeated_parameter_wins_in_both():
    params = _params("compress")
    old = ["-Mode", "1", "-mode:2", "-SafeMode:$false", "-Saf", "1", "-DryRun", "-DryRun:0"]
    values, bound = _new(legacy_args.translate(old, params), params)
    assert (values, bound) == _old(old, params)
    assert values["mode"] == 2 and values["safe_mode"] is True and values["dry_run"] is False


@pytest.mark.parametrize("old", [
    ["9"], ["stray"],               # a value without its -Parameter
    ["-Mode"],                      # a parameter without its value
    ["-Nonexistent", "1"], ["-NoSuchThing"],
    ["-Inp", "x"], ["-D"],          # ambiguous: -InputDir, -InputDirFile / -DryRun, -DeleteSource...
    ["-DryRun:maybe"],
    ["-SafeMode", "maybe"],
    ["-Mode", "x"],
    ["-Workers", "0"], ["-Workers", "65"],          # out of its range
    ["-Workers", "many"], ["-WorkerMemoryFraction", "nan"],
    ["-DuplicateAction", "Rename"],
    ["-InputDir", "-DryRun"], ["-OutputDir", "-DryRun"],    # a parameter where its value should be
    ["--mode", "9"],                # already the new form: not a line of v3.0.0
])
def test_what_v3_0_0_refused_is_refused(old):
    with pytest.raises(tw.UsageError):
        legacy_args.translate(old, _params("compress"))


def test_a_refusal_names_the_parameter_the_way_the_line_did():
    for old, said in ((["-Workers", "65"], "-Workers must be from 1 to 64, got 65"),
                      (["-ThumbSize", "x"], "-ThumbSize needs a whole number, got 'x'"),
                      (["-DryRun:maybe"], "-DryRun is a switch: -DryRun or -DryRun:$false"),
                      (["-Inp", "x"], "-Inp is ambiguous: -InputDir, -InputDirFile")):
        with pytest.raises(tw.UsageError) as e:
            legacy_args.translate(old, _params("compress"))
        assert str(e.value) == said


# --- the old reader itself: what the translation stands on -----------------------------------

def test_the_old_reader_binds_the_way_powershell_did():
    """What a preset or .bat of v2.x / v3.0.0 meant (until v3.0.0 this was tw.parse_args)."""
    opt, bound = _old(["-mode", "9", "-InputDir", r"E:\a b", "-SafeMode:$false", "-DryRun",
                       "-Overwrite:$false", "-workers", "3", "-SkipLzwAsCompressed:$true",
                       "-SrgbProfile", ""], _params("compress"))
    assert opt["mode"] == 9 and opt["input_dir"] == r"E:\a b" and opt["workers"] == 3
    assert opt["safe_mode"] is False and opt["dry_run"] is True and opt["overwrite"] is False
    assert opt["skip_lzw_as_compressed"] is True and opt["srgb_profile"] == ""
    assert bound == {"mode", "input_dir", "safe_mode", "dry_run", "overwrite", "workers",
                     "skip_lzw_as_compressed", "srgb_profile"}
    assert opt["zip_subfolder_name"] == "ZIP" and opt["delete_source"] is False


def test_the_old_reader_takes_prefixes_and_values():
    opt, _ = _old(["-DeleteS", "-Mo", "8", "-Mode:8", "-SafeMode", "$true", "-DuplicateAction",
                   "skip", "-OutputDir", "-weird name"], _params("compress"))
    assert opt["delete_source"] is True and opt["mode"] == 8
    assert opt["duplicate_action"] == "Skip", "a choice is matched without case"
    assert opt["output_dir"] == "-weird name"


# --- the command-line tool -----------------------------------------------------------------

def _cmdline(argv):
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


def test_the_tool_prints_the_new_line(capsys):
    assert legacy_args.main(["compress_tiff_zip.py", "-mo", "9", "-InputDir", HOSTILE,
                             "-ExcludeFolders", "-old", "-Saf:$false", "-DryRun"]) == 0
    assert capsys.readouterr().out.strip() == _cmdline(
        ["--mode", "9", "--input-dir", HOSTILE, "--exclude-folders=-old", "--no-safe-mode",
         "--dry-run"])


def test_the_example_of_the_tool_is_what_it_prints(capsys):
    lines = legacy_args.__doc__.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.strip().startswith("python tools/legacy_args.py"))
    argv = shlex.split(lines[i].strip())[2:]
    assert legacy_args.main(argv) == 0
    assert capsys.readouterr().out.strip() == lines[i + 1].strip()


def test_the_tool_refuses_with_exit_2(capsys):
    assert legacy_args.main(["copy-exif", "9"]) == 2
    assert capsys.readouterr().err.startswith("ERROR: unexpected argument '9'")
    assert legacy_args.main(["nonexistent", "-Mode", "9"]) == 2
    assert legacy_args.main([]) == 2
    assert "legacy_args.py compress" in capsys.readouterr().out
