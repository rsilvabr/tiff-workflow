"""The entry point of tiff-workflow: `python convert_tiff.py ...` (or `python -m tiff_workflow ...`).

    convert_tiff.py                                    the wizard
    convert_tiff.py --run-preset NAME [--dry-run]      a preset, without the menu
    convert_tiff.py --list-presets | --version | --help
    convert_tiff.py compress|copy-exif|thumbnails ...  a backend (--help lists its options)

A subcommand runs its backend in this process and imports nothing of the wizard: a backend
run does not depend on the wizard's module, its settings or the screen library (Rich).
"""

import importlib
import sys
from typing import Optional, Sequence

#: The backends by subcommand (v3.1): the file of the package each one is. The wizard reads a
#: backend's PARAMS table from that file with `ast`, and keeps a step of a journal as
#: [subcommand, *args] (T8). The Windows program takes the same three names.
SUBCOMMANDS = {"compress": "compress_tiff_zip.py", "copy-exif": "copy_exif_to_tiff.py",
               "thumbnails": "generate_thumbnails.py"}


def run_backend(sub: str, args: Sequence[str]) -> int:
    """`convert_tiff.py <sub> ...`: that backend's main() with `args`, in this process. Its exit
    code is main()'s return value, as when the module runs as `python -m tiff_workflow.<module>`."""
    module = importlib.import_module("." + SUBCOMMANDS[sub][:-len(".py")], __package__)
    return module.main([str(a) for a in args])


def main(argv: Optional[Sequence[str]] = None) -> int:
    """The exit code of `convert_tiff.py` with `argv` (sys.argv[1:] when None): a backend when
    the first word is one of SUBCOMMANDS, the wizard otherwise."""
    argv = sys.argv[1:] if argv is None else [str(a) for a in argv]
    if argv and argv[0] in SUBCOMMANDS:
        return run_backend(argv[0], argv[1:])
    from . import wizard
    return wizard.run(argv)


if __name__ == "__main__":      # python -m tiff_workflow.cli
    sys.exit(main())
