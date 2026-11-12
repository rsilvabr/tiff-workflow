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
    """The ways v3.0.0 let a user name `p`: exact, other case, the shortest unambiguous prefix."""
    names = [p.name, p.name.lower(), p.name.upper()]
    for n in range(1, len(p.name) + 1):
        try:
            if tw._resolve_param(p.name[:n], params) is p:
                names.append(p.name[:n])
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
        assert re.fullmatch(r"--[a-z0-9]+(-[a-z0-9]+)*", n), n
        assert n[2:].replace("-", "") == p.name.lower()


def test_long_names_split_at_each_word():
    assert tw.cli_name("SafeMode") == "--safe-mode"
    assert tw.cli_name("InputDirFile") == "--input-dir-file"
    assert tw.cli_name("SkipIfTiffHasExif") == "--skip-if-tiff-has-exif"
    assert tw.cli_name("IccPolicy") == "--icc-policy"
    assert tw.cli_name("Size") == "--size"
    assert tw.cli_name("ICCPolicy") == "--icc-policy"        # an acronym stays one word
    assert tw.cli_switch("SafeMode", False) == "--no-safe-mode"
    assert tw.cli_value("ExcludeFolders", "-old") == ["--exclude-folders=-old"]
    assert tw.cli_value("Workers", 4) == ["--workers", "4"]


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
        if p.name == "InputDirFile":            # one of -InputDir / -InputDirFile is enough
            continue
        v = HOSTILE if p.kind == "str" and _accepted(p, HOSTILE) else _values(p)[-1]
        old += [f"-{p.name}:{v}"] if p.kind == "switch" else [f"-{p.name}", v]
    new = legacy_args.translate(old, params)
    assert _new(new, params) == _old(old, params)
    assert _old(old, params)[1] == {p.name for p in params} - {"InputDirFile"}


def test_the_last_of_a_repeated_parameter_wins_in_both():
    params = _params("compress")
    old = ["-Mode", "1", "-mode:2", "-SafeMode:$false", "-Saf", "1", "-DryRun", "-DryRun:0"]
    values, bound = _new(legacy_args.translate(old, params), params)
    assert (values, bound) == _old(old, params)
    assert values["Mode"] == 2 and values["SafeMode"] is True and values["DryRun"] is False


@pytest.mark.parametrize("old", [
    ["9"],                          # a value without its -Parameter
    ["-Mode"],                      # a parameter without its value
    ["-Nonexistent", "1"],
    ["-Inp", "x"],                  # ambiguous: -InputDir, -InputDirFile
    ["-DryRun:maybe"],
    ["-SafeMode", "maybe"],
    ["-Mode", "x"],
    ["-Workers", "65"],             # out of its range
    ["-DuplicateAction", "Rename"],
    ["-InputDir", "-DryRun"],       # a parameter where its value should be
    ["--mode", "9"],                # already the new form: not a line of v3.0.0
])
def test_what_v3_0_0_refused_is_refused(old):
    with pytest.raises(tw.UsageError):
        legacy_args.translate(old, _params("compress"))


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
