"""How the tests start a backend script.

Until v2.9.1 the backends were PowerShell scripts and every end-to-end test ran under two
shells: Windows PowerShell 5.1 (one file at a time) and PowerShell 7 (`ForEach-Object
-Parallel`). The two paths were separate code, and divergence between them was the most
recurring bug class of the project. The Python backends have ONE implementation of the
per-file work; what is left of that axis is whether it runs on the caller's thread or on a
thread pool. So every end-to-end test still runs twice, once per LANE:

    parallel    the default (-Workers as the script has it)
    sequential  compress: -ForceSequential; copy_exif and generate_thumbnails: -Workers 1
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

COMPRESS = "compress_tiff_zip.py"
COPY_EXIF = "copy_exif_to_tiff.py"
THUMBNAILS = "generate_thumbnails.py"
COMMON = "tiff_workflow_common.py"
BACKENDS = [COMPRESS, COPY_EXIF, THUMBNAILS]

LANES = ["parallel", "sequential"]


def lane_args(lane, script, args):
    """`args` plus what makes `script` run in `lane` (nothing when the test already chose)."""
    args = [str(a) for a in args]
    if lane != "sequential":
        return args
    lowered = [a.lower() for a in args]
    if Path(str(script)).name == COMPRESS:
        if "-forcesequential" in lowered or "-forceparallel" in lowered:
            return args
        return args + ["-ForceSequential"]
    if "-workers" in lowered:
        return args
    return args + ["-Workers", "1"]


def backend_cmd(lane, script, args):
    """The command line of one backend run: this Python, the script (a name in the repo, or
    a full path), the arguments."""
    path = Path(str(script))
    if not path.is_absolute():
        path = REPO / path
    return [sys.executable, str(path)] + lane_args(lane, script, args)


def run_backend(lane, script, args, cwd, timeout=300, env=None):
    """Run a backend as an unattended subprocess (stdin closed) and return the
    CompletedProcess. Output is decoded as UTF-8: that is what a backend writes to a pipe."""
    return subprocess.run(
        backend_cmd(lane, script, args), cwd=str(cwd), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env)
