"""Pytest configuration and fixtures for tiff-workflow tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


import pytest
from pathlib import Path
import tempfile
import os


@pytest.fixture(autouse=True)
def _isolated_diagnose_cache(tmp_path, monkeypatch):
    """Diagnose writes its verdict cache next to the user's config; no test may touch
    the real one. Tests that set USERPROFILE to tmp_path see the same file."""
    import convert_tiff
    monkeypatch.setattr(convert_tiff, "_diagnose_cache_path",
                        lambda: tmp_path / ".convert_tiff_diagnose_cache.json")


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)