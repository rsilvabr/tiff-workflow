"""Round 22: the folder exclusion pinned to jxl-photo's _path_excluded_below_root.

The expected column below is written from jxl-photo's tests/test_exclude_folders.py
(helper cases); the two repos stay standalone, so nothing is imported from there.
This is a pinning test of the CURRENT semantics -- if a row fails, the code and
jxl-photo disagree and Claude decides (see the round 22 plan).

The matcher is tiff_workflow_common.path_excluded (one copy for the three backends; the
PowerShell edition had four) and, in the wizard, convert_tiff._is_tool_output_path."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tiff_workflow_common as tw  # noqa: E402

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


@pytest.mark.parametrize("directory,root,names,expected,note", _ROWS, ids=[r[4] for r in _ROWS])
def test_path_excluded_matches_jxl_photo(directory, root, names, expected, note):
    assert tw.path_excluded(directory, root, names) is expected, note
