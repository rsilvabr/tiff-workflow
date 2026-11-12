"""How the tests start a backend script.

Until v2.9.1 the backends were PowerShell scripts and every end-to-end test ran under two
shells: Windows PowerShell 5.1 (one file at a time) and PowerShell 7 (`ForEach-Object
-Parallel`). The two paths were separate code, and divergence between them was the most
recurring bug class of the project. The Python backends have ONE implementation of the
per-file work; what is left of that axis is whether it runs on the caller's thread or on a
thread pool. So every end-to-end test still runs twice, once per LANE:

    parallel    the default (--workers as the script has it)
    sequential  compress: --force-sequential; copy_exif and generate_thumbnails: --workers 1
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

COMPRESS = "compress_tiff_zip.py"
COPY_EXIF = "copy_exif_to_tiff.py"
THUMBNAILS = "generate_thumbnails.py"
COMMON = "common.py"
BACKENDS = [COMPRESS, COPY_EXIF, THUMBNAILS]
#: since v3.1 a backend is a module of the package `tiff_workflow` and runs through the
#: entry point: `python convert_tiff.py <subcommand> ...`
SUBCOMMANDS = {COMPRESS: "compress", COPY_EXIF: "copy-exif", THUMBNAILS: "thumbnails"}
PACKAGE = "tiff_workflow"


def install_copy(dest):
    """A copy of the installation (convert_tiff.py and the package) in `dest`, for a test that
    edits a script or puts a settings file "next to the scripts". Returns `dest`."""
    import shutil
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "convert_tiff.py", dest / "convert_tiff.py")
    shutil.copytree(REPO / PACKAGE, dest / PACKAGE, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def wizard_uses_install(monkeypatch, ct, install):
    """Point the imported wizard `ct` at the installation `install` (an install_copy): the
    backends' tables it reads and the entry point its command lines run."""
    install = Path(install)
    monkeypatch.setattr(ct, "SCRIPT_DIR", install)
    monkeypatch.setattr(ct, "PACKAGE_DIR", install / PACKAGE)
    monkeypatch.setattr(ct, "ENTRY", install / "convert_tiff.py")
    ct._backend_tables.cache_clear()
    return install

LANES = ["parallel", "sequential"]


def lane_args(lane, script, args):
    """`args` plus what makes `script` run in `lane` (nothing when the test already chose)."""
    args = [str(a) for a in args]
    if lane != "sequential":
        return args
    if Path(str(script)).name == COMPRESS:
        if "--force-sequential" in args or "--force-parallel" in args:
            return args
        return args + ["--force-sequential"]
    if "--workers" in args or any(a.startswith("--workers=") for a in args):
        return args
    return args + ["--workers", "1"]


def backend_cmd(lane, script, args):
    """The command line of one backend run: this Python, the entry point of the installation
    the script belongs to (the repo for a bare name; for a full path, the folder that holds
    its package, or the script's own folder -- an install_copy), the subcommand, the
    arguments."""
    path = Path(str(script))
    install = REPO
    if path.is_absolute():
        install = path.parent.parent if path.parent.name == PACKAGE else path.parent
    return ([sys.executable, str(install / "convert_tiff.py"), SUBCOMMANDS[path.name]]
            + lane_args(lane, script, args))


def run_backend(lane, script, args, cwd, timeout=300, env=None):
    """Run a backend as an unattended subprocess (stdin closed) and return the
    CompletedProcess. Output is decoded as UTF-8: that is what a backend writes to a pipe."""
    return subprocess.run(
        backend_cmd(lane, script, args), cwd=str(cwd), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env)
