"""Pytest configuration and fixtures for tiff-workflow tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


import pytest
from pathlib import Path
import tempfile
import os


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def sample_tiff_path(temp_dir):
    """Create a minimal valid TIFF for testing."""
    return None  # Would need actual TIFF file