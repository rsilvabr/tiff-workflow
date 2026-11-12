#!/usr/bin/env python3
"""tiff-workflow -- TIFF to ZIP/Deflate, EXIF from the Fuji S3/S5 Pro JPEG, sRGB thumbnails.

    python convert_tiff.py                                     the wizard
    python convert_tiff.py compress|copy-exif|thumbnails ...   a backend (--help lists its options)

This file only starts the toolkit: everything is in the package tiff_workflow/ next to it
(the wizard is tiff_workflow/wizard.py, the entry point tiff_workflow/cli.py).
"""

import os
import sys

# The package next to this file, also when it is started from another folder (a preset in
# the Task Scheduler) or by the Windows program.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tiff_workflow.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
