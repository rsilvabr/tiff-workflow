"""audit round 17: the wizard reads the backends' own param() defaults.

The wizard used to carry literal copies of the .ps1 defaults (Safe mode, LZW,
thumbnails, duplicate action, ZIP/_EXPORT folder names) and, in two places, passed a
setting only when the answer differed from the copy -- so a user editing a default in
the .ps1 had it silently discarded (the jxl-photo #467/#528 class).

These tests pin: (a) the fallback table agrees with the scripts; (b) an edit to a
script's param() default changes what the wizard offers/passes; (c) the builders pass
the wizard's decision explicitly even when it equals the shipped default; (d) a real
run proves the answer now overrides an edited backend default.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import convert_tiff as ct


@pytest.fixture
def scripts_copy(tmp_path, monkeypatch):
    """A copy of the four backends whose param defaults a test can edit; the wizard
    reads its settings from there."""
    for name in ("compress_tiff_zip.ps1", "copy_exif_to_TIFF_ps5.ps1",
                 "copy_exif_to_TIFF_ps7.ps1", "generate_thumbnails.ps1"):
        shutil.copy2(ct.SCRIPT_DIR / name, tmp_path / name)
    monkeypatch.setattr(ct, "SCRIPT_DIR", tmp_path)
    # getattr: against the pre-fix code (the proof run) the cache does not exist yet;
    # this keeps the proof reaching the tests' own assertions.
    cache_clear = getattr(ct, "_ps_param_defaults", None)
    if cache_clear is not None:
        cache_clear.cache_clear()
    yield tmp_path
    cache_clear = getattr(ct, "_ps_param_defaults", None)
    if cache_clear is not None:
        cache_clear.cache_clear()


def _edit_default(path, param, new_literal):
    """Replace the default of $<param> in the script's param() block with
    `new_literal` (the PowerShell literal, quotes included)."""
    text = path.read_text(encoding="ascii")
    pattern = re.compile(
        r"^(\s*(?:\[[^\]]*\]\s*)*\$" + re.escape(param) + r"\s*=\s*)"
        r"(\"(?:[^\"]*)\"|'(?:[^']*)'|\$true|\$false|-?\d+(?:\.\d+)?)",
        re.MULTILINE)
    new_text, n = pattern.subn(lambda m: m.group(1) + new_literal, text)
    assert n == 1, f"{param}: expected exactly 1 substitution in {path.name}, got {n}"
    path.write_text(new_text, encoding="ascii")


def test_fallbacks_match_the_scripts():
    """T1: every _BACKEND_PARAM_FALLBACKS value equals the script's real default."""
    for (script, param), fallback in ct._BACKEND_PARAM_FALLBACKS.items():
        parsed = ct._ps_param_defaults(script)
        assert param in parsed, f"{script}: param() default for {param} was not parsed"
        assert parsed[param] == fallback, (
            f"{script} -{param}: fallback {fallback!r} disagrees with the script's "
            f"{parsed[param]!r}")


def test_backend_default_follows_an_edit(scripts_copy):
    """T2: an edited default is what the wizard reads; a wrong-typed edit falls back."""
    _edit_default(scripts_copy / "compress_tiff_zip.ps1", "ThumbSize", "512")
    _edit_default(scripts_copy / "compress_tiff_zip.ps1", "ZipSubfolderName", '"ZIPPED"')
    assert ct._backend_default("compress_tiff_zip.ps1", "ThumbSize") == 512
    assert ct._backend_default("compress_tiff_zip.ps1", "ZipSubfolderName") == "ZIPPED"

    _edit_default(scripts_copy / "compress_tiff_zip.ps1", "ThumbSize", '"abc"')
    ct._ps_param_defaults.cache_clear()  # re-read the edited file
    assert ct._backend_default("compress_tiff_zip.ps1", "ThumbSize") == 256


def test_builders_pass_safe_mode_and_lzw_explicitly(tmp_path):
    """T3: the wizard's decision reaches the command line even when it equals the
    shipped default -- omitting it let an edited backend default win."""
    workflow = {"safe_mode": True, "skip_lzw": False, "cap_workers": 0}
    cmd = ct.build_compress_command(workflow, folders=[tmp_path], ps_name="pwsh")
    assert "-SafeMode:$true" in cmd
    assert "-SkipLzwAsCompressed:$false" in cmd
    assert "-CapWorkers" in cmd
    assert cmd[cmd.index("-CapWorkers") + 1] == "0"

    copy_cmd = ct.build_copy_exif_command(
        {"safe_mode": True, "skip_lzw": False}, folders=[tmp_path], ps_name="pwsh")
    assert "-SafeMode:$true" in copy_cmd
    assert not any(a.startswith("-SkipLzwAsCompressed") for a in copy_cmd), (
        "the Copy EXIF backend's -SkipLzwAsCompressed is a [switch]: only 'yes' is passed"
    )

    bare = ct.build_compress_command({}, folders=[tmp_path], ps_name="pwsh")
    assert not any(a.startswith("-SafeMode") for a in bare)
    bare_copy = ct.build_copy_exif_command({}, folders=[tmp_path], ps_name="pwsh")
    assert not any(a.startswith("-SafeMode") for a in bare_copy)


def test_tool_output_names_follow_the_backend(scripts_copy):
    """T4: an edited -ZipSubfolderName is skipped by discovery too."""
    _edit_default(scripts_copy / "compress_tiff_zip.ps1", "ZipSubfolderName", '"ZIPPED"')
    names = ct._tool_output_names()
    assert "zipped" in names


def test_thumbnail_no_configure_passes_no_values(tmp_path):
    """T5a: with no thumb_* keys the builder passes none of the three -- the backend
    runs on its own (possibly edited) defaults."""
    workflow = {"generate_thumbnail": True}
    cmd = ct.build_compress_command(workflow, folders=[tmp_path], ps_name="pwsh")
    assert "-GenerateThumbnail" in cmd
    for flag in ("-ThumbSize", "-ThumbQuality", "-ThumbFormat"):
        assert flag not in cmd


def test_run_free_compress_no_configure_adds_no_thumb_keys(tmp_path, monkeypatch):
    """T5b: answering "configure thumbnails? No" leaves the workflow without thumb_*
    keys (it used to write 256/"85"/jpg, overriding the backend's defaults)."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    captured = {}

    def fake_step_confirm(workflow, cfg):
        captured.update(workflow)
        return False

    monkeypatch.setattr(ct, "step_confirm", fake_step_confirm)
    answers = iter([
        "0",            # mode
        str(tmp_path),  # input folder
        "n",            # generate manifest?
        "",             # workers
        "",             # staging
        "n",            # dry-run
        "",             # safe mode (default)
        "",             # skip LZW (default)
        "",             # cap workers (default)
        "",             # exclude folders
        "",             # force parallel?
        "y",            # generate thumbnails?
        "n",            # configure thumbnail settings?
        "n",            # skip compressed with thumb?
    ])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    cfg = SimpleNamespace(config=ct.ToolConfig(), save_config=lambda: None)
    cfg.config.ps_name = "pwsh"

    assert ct.run_free_compress(cfg) is False  # cancelled at the (stubbed) summary
    assert captured.get("generate_thumbnail") is True
    assert "thumb_size" not in captured
    assert "thumb_quality" not in captured
    assert "thumb_format" not in captured


def test_ps5_wrap_keeps_true_unquoted():
    """T6: the PS5 -Command wrapper must keep `:$true` unquoted, or powershell.exe
    binds the string '$true'; the unwrap must read it back intact."""
    wrapped = ct._wrap_ps5_command([
        "powershell", "-NoProfile", "-File", "C:\\s\\compress_tiff_zip.ps1",
        "-InputDir", "C:\\x", "-SafeMode:$true"])
    invocation = wrapped[-1]
    assert "-SafeMode:$true" in invocation
    assert "'-SafeMode:$true'" not in invocation
    parts = ct._unwrap_ps5_invocation(invocation)
    assert parts is not None
    assert "-SafeMode:$true" in parts


def test_edited_safe_mode_default_is_overridden_by_the_answer(tmp_path, scripts_copy):
    """T7 (the real proof of the #467 class here): with `$SafeMode = $false` edited in
    the backend, the wizard's "Safe mode? Yes" answer must still skip the multi-page
    TIFF. Pre-fix the wizard omitted -SafeMode and the backend processed the file."""
    if not shutil.which("pwsh"):
        pytest.skip("pwsh not on PATH")
    if not shutil.which("magick") or not shutil.which("exiftool"):
        pytest.skip("ImageMagick (magick) and exiftool required")
    from test_regression_classes import make_multipage_tiff

    _edit_default(scripts_copy / "compress_tiff_zip.ps1", "SafeMode", "$false")

    in_dir = tmp_path / "in"
    in_dir.mkdir()
    # Two full-size untagged pages: a file SafeMode (ON) must classify as multi-page.
    # A main + REDUCEDIMAGE thumbnail pair would be compressed under SafeMode too, so
    # it could not distinguish the answer from the edited default.
    two_page = make_multipage_tiff(in_dir / "two_pages.tif", [None, None])

    cmd = ct.build_compress_command(
        {"mode": 3, "safe_mode": True, "workers": 1},
        folders=[in_dir], ps_name="pwsh")
    result = subprocess.run(cmd, cwd=str(in_dir), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=300)

    assert not (in_dir / "ZIP" / "two_pages.tif").exists(), (
        "the wizard answered Safe mode=Yes but the backend's edited $SafeMode=$false "
        "processed the multi-page TIFF anyway\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    assert two_page.exists(), "the skipped source vanished"
    # The disk check above must not pass because the backend failed before writing:
    # it ran cleanly and classified the file as multi-page.
    assert result.returncode == 0, f"the backend did not run cleanly:\n{result.stdout}"
    assert "MULTI" in result.stdout, f"the file was not classified multi-page:\n{result.stdout}"


def test_text_safe_mode_unknown_answer_keeps_default(monkeypatch):
    """T8 (R17-1): in the text branch an answer that is neither yes nor no must keep
    the backend's Safe mode default. "s"/"sim" is what the owner types (the wizard is
    used in Portuguese); pre-fix every unrecognised answer fell into the False branch
    and silently disabled the gate, so the backend processed multi-page TIFFs."""
    monkeypatch.setattr(ct, "RICH_AVAILABLE", False)
    # workers, staging, dry-run, safe mode, skip LZW, cap, exclude, force parallel
    answers = iter(["4", "", "n", "s", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    # ConfigManager without disk: the probe's shape (no config file is read or written)
    cfg = ct.ConfigManager.__new__(ct.ConfigManager)
    cfg.config = ct.ToolConfig()
    cfg.save_config = lambda: None

    workflow = {"origin": "free_compress", "dest": "zip"}
    assert ct.step_basic_params(cfg, workflow) is True
    assert workflow["safe_mode"] is True, (
        "'s' is neither yes nor no -- it must keep the default (True), not "
        "silently disable Safe mode")
