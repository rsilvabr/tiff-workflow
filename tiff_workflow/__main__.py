"""`python -m tiff_workflow ...`: the same as `python convert_tiff.py ...` (tiff_workflow/cli.py)."""

import sys

from .cli import main

sys.exit(main())
