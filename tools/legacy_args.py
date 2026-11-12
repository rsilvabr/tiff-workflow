"""The command line of v3.0.0 and before in the form of v3.1.

v3.0.0 bound its arguments the way the PowerShell scripts had: `-Name value`, `-Name:value`,
`-Switch`, `-Switch:$false`, names without case, an unambiguous prefix of a name. v3.1 reads
`--kebab-case value` and `--switch` / `--no-switch` only (D3), and its backends refuse the old
form. This turns an old argv into the new one, each argument in its place: for a saved .bat
or scheduled task, for the tests, and to compare v3.0.0 with v3.1 side by side.

    python tools/legacy_args.py compress -Mode 9 -InputDir "D:\\my photos" -SafeMode:$false
    --mode 9 --input-dir "D:\\my photos" --no-safe-mode

The first word names the backend: compress, copy-exif, thumbnails, or its script
(compress_tiff_zip.py, copy_exif_to_tiff.py, generate_thumbnails.py). A value is passed on as
it was written; an argv the old reader refused is refused here too (exit 2).

The old reader itself lives here since v3.1 (parse_legacy): what the translation stands on.
"""
import importlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiff_workflow import common as tw  # noqa: E402

BACKENDS = {"compress": "compress_tiff_zip", "copy-exif": "copy_exif_to_tiff",
            "thumbnails": "generate_thumbnails"}

BOOL_WORDS = {"$true": True, "true": True, "1": True,
              "$false": False, "false": False, "0": False}
PARAM_TOKEN = re.compile(r"-([A-Za-z][A-Za-z0-9]*)(?::(.*))?", re.S)


def backend_module(word: str):
    """The backend module a subcommand name or a script name stands for; None for neither."""
    name = BACKENDS.get(word, Path(word).stem)
    return importlib.import_module("tiff_workflow." + name) if name in BACKENDS.values() else None


# --- the old reader (tiff_workflow_common.parse_args until v3.0.0) --------------------------

def legacy_name(name):
    """The name a parameter had on the command line of v3.0.0 and the .ps1 backends:
    thumb_size -> ThumbSize (every name of then is its words capitalised, no acronym)."""
    return "".join(w.capitalize() for w in name.split("_"))


def resolve_param(word, params):
    """The parameter `word` named then: without case, or an unambiguous prefix of it."""
    low = word.lower()
    for p in params:
        if legacy_name(p.name).lower() == low:
            return p
    hits = [p for p in params if legacy_name(p.name).lower().startswith(low)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise tw.UsageError(f"unknown parameter -{word}")
    raise tw.UsageError(f"-{word} is ambiguous: "
                        + ", ".join("-" + legacy_name(p.name) for p in hits))


def _value(p, raw):
    """What `raw` gave `p` then: a bool took $true/$false (1/0, true/false); every other value
    is checked as v3.1 checks it (tw._convert_param, the one copy)."""
    if p.kind == "bool":
        word = raw.strip().lower()
        if word not in BOOL_WORDS:
            raise tw.UsageError(f"needs $true or $false, got '{raw}'")
        return BOOL_WORDS[word]
    return tw._convert_param(p, raw)


def parse_legacy(argv, params):
    """Bind `argv` the way v3.0.0 and the PowerShell backends bound theirs: `-Name value`,
    `-Name:value`, `-Switch`, `-Switch:$false`; names without case; an unambiguous prefix of a
    name is that name. Returns ({name: value}, {names given on the command line}) with the
    names of v3.1 (thumb_size). Raises UsageError for anything it could not bind -- a value
    without its -Parameter included."""
    values = {p.name: p.default for p in params}
    bound = set()
    i = 0
    while i < len(argv):
        tok = str(argv[i])
        i += 1
        m = PARAM_TOKEN.fullmatch(tok)
        if not m:
            raise tw.UsageError(f"unexpected argument '{tok}' (every value needs its -Parameter)")
        p = resolve_param(m.group(1), params)
        old = "-" + legacy_name(p.name)
        inline = m.group(2)
        if p.kind == "switch":
            if inline is None:
                val = True
            elif inline.strip().lower() in BOOL_WORDS:
                val = BOOL_WORDS[inline.strip().lower()]
            else:
                raise tw.UsageError(f"{old} is a switch: {old} or {old}:$false")
        else:
            if inline is not None:
                raw = inline
            else:
                if i >= len(argv):
                    raise tw.UsageError(f"{old} needs a value")
                raw = str(argv[i])
                if p.kind == "str":
                    m2 = PARAM_TOKEN.fullmatch(raw)
                    if m2 and m2.group(2) is None:
                        try:
                            resolve_param(m2.group(1), params)
                            is_param = True
                        except tw.UsageError:
                            is_param = False
                        if is_param:
                            raise tw.UsageError(f"{old} needs a value")
                i += 1
            try:
                val = _value(p, raw)
            except tw.UsageError as e:
                raise tw.UsageError(f"{old} {e}") from None
        values[p.name] = val
        bound.add(p.name)
    return values, bound


# --- the translation -------------------------------------------------------------------------

def translate(argv, params) -> list:
    """`argv` (the old form) as the new form, argument by argument, in the same order."""
    argv = [str(a) for a in argv]
    parse_legacy(argv, params)              # refuse what v3.0.0 refused, with its message
    out = []
    i = 0
    while i < len(argv):
        m = PARAM_TOKEN.fullmatch(argv[i])
        assert m is not None, argv[i]       # parse_legacy accepted the line: a -Name here
        p = resolve_param(m.group(1), params)
        inline = m.group(2)
        i += 1
        if p.kind == "switch":
            out.append(tw.cli_switch(p.name, True if inline is None
                                     else BOOL_WORDS[inline.strip().lower()]))
            continue
        if inline is None:
            inline = argv[i]
            i += 1
        if p.kind == "bool":
            out.append(tw.cli_switch(p.name, BOOL_WORDS[inline.strip().lower()]))
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
