"""The command line of v3.0.0 and before in the form of v3.1.

v3.0.0 bound its arguments the way the PowerShell scripts had: `-Name value`, `-Name:value`,
`-Switch`, `-Switch:$false`, names without case, an unambiguous prefix of a name. v3.1 reads
`--kebab-case value` and `--switch` / `--no-switch` only (D3). This turns an old argv into the
new one, each argument in its place: for a saved .bat or scheduled task, for the tests, and to
compare v3.0.0 with v3.1 side by side.

    python tools/legacy_args.py compress -Mode 9 -InputDir "D:\\my photos" -SafeMode:$false
    --mode 9 --input-dir "D:\\my photos" --no-safe-mode

The first word names the backend: compress, copy-exif, thumbnails, or its script
(compress_tiff_zip.py, copy_exif_to_tiff.py, generate_thumbnails.py). A value is passed on as
it was written; an argv the old reader refused is refused here too (exit 2)."""
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tiff_workflow_common as tw  # noqa: E402

BACKENDS = {"compress": "compress_tiff_zip", "copy-exif": "copy_exif_to_tiff",
            "thumbnails": "generate_thumbnails"}


def backend_module(word: str):
    """The backend module a subcommand name or a script name stands for; None for neither."""
    name = BACKENDS.get(word, Path(word).stem)
    return __import__(name) if name in BACKENDS.values() else None


def translate(argv, params) -> list:
    """`argv` (the old form) as the new form, argument by argument, in the same order."""
    argv = [str(a) for a in argv]
    tw._parse_legacy(argv, params)          # refuse what v3.0.0 refused, with its message
    out = []
    i = 0
    while i < len(argv):
        m = tw._PARAM_TOKEN.fullmatch(argv[i])
        p = tw._resolve_param(m.group(1), params)
        inline = m.group(2)
        i += 1
        if p.kind == "switch":
            out.append(tw.cli_switch(p.name, True if inline is None
                                     else tw._BOOL_WORDS[inline.strip().lower()]))
            continue
        if inline is None:
            inline = argv[i]
            i += 1
        if p.kind == "bool":
            out.append(tw.cli_switch(p.name, tw._BOOL_WORDS[inline.strip().lower()]))
        else:
            out += tw.cli_value(p.name, inline)
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    module = backend_module(argv[0]) if argv else None
    if module is None:
        print(__doc__)
        return 2
    try:
        new = translate(argv[1:], module.PARAMS)
    except tw.UsageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(subprocess.list2cmdline(new) if sys.platform == "win32" else shlex.join(new))
    return 0


if __name__ == "__main__":
    tw.setup_console()
    sys.exit(main())
