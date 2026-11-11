"""Pytest configuration and fixtures for tiff-workflow tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


import pytest
from pathlib import Path
import tempfile
import os

# convert_tiff applies its own overlay section when it is imported (test modules import it at
# collection, before any fixture runs): switch the overlay off for that too.
os.environ["TIFFWORKFLOW_SETTINGS"] = ""


@pytest.fixture(autouse=True)
def _isolated_diagnose_cache(tmp_path, monkeypatch):
    """Diagnose writes its verdict cache next to the user's config; no test may touch
    the real one. Tests that set USERPROFILE to tmp_path see the same file."""
    import convert_tiff
    monkeypatch.setattr(convert_tiff, "_diagnose_cache_path",
                        lambda: tmp_path / ".convert_tiff_diagnose_cache.json")


@pytest.fixture(autouse=True)
def _no_release_check(monkeypatch):
    """The menu's new-release check asks GitHub; no test may touch the network. Tests of the
    check itself delete this variable and monkeypatch convert_tiff._latest_release_tag."""
    monkeypatch.setenv("TIFFWORKFLOW_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def _no_settings_overlay(monkeypatch):
    """The settings overlay (.tiff_workflow_settings.json) is read by convert_tiff and by every
    backend a test starts: set-but-empty turns it OFF, so the owner's real file
    never reaches a test. A test that wants an overlay sets TIFFWORKFLOW_SETTINGS itself."""
    monkeypatch.setenv("TIFFWORKFLOW_SETTINGS", "")


@pytest.fixture(autouse=True)
def _no_advanced_screen(monkeypatch):
    """Tests script the wizard's prompts one by one; the per-run Advanced options question
    would eat an answer. It answers "No" (nothing stored) here; tests/test_wizard_registry.py
    keeps the real function in REAL_ADVANCED."""
    import convert_tiff
    monkeypatch.setattr(convert_tiff, "step_advanced_options", lambda cfg, wf: True)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)