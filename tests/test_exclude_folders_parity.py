"""Round 22: Test-PathExcluded pinned to jxl-photo's _path_excluded_below_root.

The four backends carry near-identical copies of the exclusion matcher. The
expected column below is written from jxl-photo's tests/test_exclude_folders.py
(helper cases); the two repos stay standalone, so nothing is imported from there.
This is a pinning test of the CURRENT semantics -- if a row fails, the code and
jxl-photo disagree and Claude decides (see the round 22 plan)."""

import subprocess
from pathlib import Path

import pytest

from test_ps_helper_parity import _extract_functions
from test_regression_classes import SHELLS, requires_shell

REPO = Path(__file__).resolve().parent.parent

SCRIPTS = [
    "compress_tiff_zip.ps1",
    "copy_exif_to_TIFF_ps5.ps1",
    "copy_exif_to_TIFF_ps7.ps1",
    "generate_thumbnails.ps1",
]

_R = r"C:\Data\Photos"

# (Directory, Root, Names, expected, jxl-photo case)
_ROWS = [
    (r"C:\Data\Photos\_export", _R, ["_export"], True, "matches a folder segment"),
    (r"C:\Data\Photos\_EXPORT", _R, ["_export"], True, "case-insensitive"),
    (r"C:\Data\Photos\My_EXPORT_photos", _R, ["_export"], False, "never a longer name"),
    (_R, _R, ["Photos"], False, "never the root itself"),
    (r"C:\Data\Photos\JobA", _R, ["Data"], False, "never an ancestor"),
    (r"C:\Data\_EXPORT", r"C:\Data\_EXPORT", ["_EXPORT"], False, "root pointed AT the name"),
    (r"C:\Data\_EXPORT\sub", r"C:\Data\_EXPORT", ["_EXPORT"], False, "same, file below it"),
    (r"D:\Other\_EXPORT", _R, ["_EXPORT"], True, "outside the root: fail closed"),
    (r"C:\Data\Photos\_export", _R, [], False, "no names: inert"),
    (r"C:\Data\Photos\JobA\_EXPORT\TIFF16", _R, ["_EXPORT"], True, "nested segment"),
    ("C:/Data/Photos/JobA/_export", _R, ["_EXPORT"], True, "/ separators accepted"),
    ("C:\\Data\\Photos\\JobA\\", "C:\\Data\\Photos\\", ["JobA"], True,
     "trailing separators ignored"),
]


def _ps_quote(value):
    return "'" + value.replace("'", "''") + "'"


def _build_harness(script, tmp_path):
    text = (REPO / script).read_text(encoding="utf-8", errors="replace")
    func = _extract_functions(text)["Test-PathExcluded"]
    lines = [func, ""]
    for directory, root, names, expected, note in _ROWS:
        names_expr = "@(" + ", ".join(_ps_quote(n) for n in names) + ")"
        lines.append("# " + note)
        lines.append("$ok = Test-PathExcluded -Directory " + _ps_quote(directory)
                     + " -Root " + _ps_quote(root) + " -Names " + names_expr)
        lines.append('"$ok"')
    harness = tmp_path / "harness.ps1"
    harness.write_text("\n".join(lines) + "\n", encoding="ascii")
    return harness


@requires_shell
@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("shell", SHELLS)
def test_path_excluded_matches_jxl_photo(shell, script, tmp_path):
    harness = _build_harness(script, tmp_path)
    result = subprocess.run([shell, "-NoProfile", "-File", str(harness)],
                            capture_output=True, text=True, timeout=120)
    out = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    expected = ["True" if row[3] else "False" for row in _ROWS]
    assert out == expected, (
        f"{script} under {shell} diverges from jxl-photo\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}")
