"""Round 26, wizard side: after a compress run that reports originals stranded in
OLD_TIFFs/, the wizard offers -RestoreStranded (interactive runs only), and menu 5
(Restore OLD_TIFFs) can restore ONLY the stranded ones. Hostile folder names."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import convert_tiff as ct  # noqa: E402

HOSTILE = "伊豆沼_夏 [FINAL] 5% off"
COMPRESS = [sys.executable, "C:\\x\\compress_tiff_zip.py", "-Mode", "8",
            "-InputDir", "G:\\2025"]


def put(p: Path, data=b"II*\0"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_the_end_of_run_line_is_recognised():
    line = ("12:00:00 | WARN | STRANDED: 3 original(s) still in OLD_TIFFs with nothing in "
            "their place -- see the STRANDED lines above")
    assert ct._STRANDED_LEFT_RE.search(line).group(1) == "3"
    # the start-of-run line (restored or not) is not the one that counts
    assert not ct._STRANDED_LEFT_RE.search(
        "STRANDED: 3 original(s) found in OLD_TIFFs with nothing in their place, 3 restored")


def test_run_subprocess_reads_the_count_from_real_output(monkeypatch):
    seen = []
    monkeypatch.setattr(ct, "_offer_restore_stranded",
                        lambda cmd, n, stdin: seen.append((n, stdin)) or None)
    code = ("print('12:00 | WARN | STRANDED: 4 original(s) still in OLD_TIFFs with "
            "nothing in their place -- see the STRANDED lines above')")
    rc = ct.run_subprocess([sys.executable, "-c", code, "compress_tiff_zip.py"],
                           stdin=subprocess.DEVNULL)
    assert rc == 0
    assert seen == [(4, subprocess.DEVNULL)]


def test_restore_flag_is_one_more_argument():
    out = ct._with_restore_stranded(COMPRESS)
    assert out == COMPRESS + ["-RestoreStranded"] and out is not COMPRESS


def test_no_offer_without_stranded_files_or_for_other_backends(monkeypatch):
    called = []
    monkeypatch.setattr(ct, "run_subprocess", lambda *a, **k: called.append(a) or 0)
    assert ct._offer_restore_stranded(COMPRESS, 0, None) is None
    other = [sys.executable, "C:\\x\\copy_exif_to_tiff.py"]
    assert ct._offer_restore_stranded(other, 5, None) is None
    assert ct._offer_restore_stranded(COMPRESS + ["-RestoreStranded"], 5, None) is None
    assert called == []


def test_unattended_and_dry_runs_only_get_a_hint(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(ct, "run_subprocess", lambda *a, **k: called.append(a) or 0)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert ct._offer_restore_stranded(COMPRESS, 2, subprocess.DEVNULL) is None   # a preset
    assert ct._offer_restore_stranded(COMPRESS + ["-DryRun"], 2, None) is None
    out = capsys.readouterr().out
    assert out.count("-RestoreStranded") >= 2
    assert called == []


def test_an_interactive_yes_reruns_with_the_flag(monkeypatch):
    called = []
    monkeypatch.setattr(ct, "run_subprocess", lambda cmd, **k: called.append(cmd) or 0)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    if ct.RICH_AVAILABLE and ct.console:
        monkeypatch.setattr(ct.Confirm, "ask", staticmethod(lambda *a, **k: True))
    else:
        monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert ct._offer_restore_stranded(COMPRESS, 2, None) == 0
    assert called == [COMPRESS + ["-RestoreStranded"]]


def test_stranded_backups_use_the_compress_rule(tmp_path):
    cam = tmp_path / HOSTILE / "01_D810"
    old = cam / "OLD_TIFFs"
    put(old / "a.tif")                                   # stranded
    put(old / "b.tif"); put(cam / "b.tif")               # healthy
    put(old / "c.tiff"); put(cam / "c.tif")              # .tiff written as .tif: healthy
    put(old / "d_20260803_081300.tif"); put(cam / "d.tif")
    # audit round 28 (#16): a date stamp is a collision rename only next to the backup it
    # collided with; otherwise it is the photo's own name and restores as such.
    put(old / "d.tif")                                   # the backup d_... collided with
    put(old / "e_20260803_081300_1.tif"); put(old / "e.tif")  # both stranded, both claim e.tif
    put(old / "f_20260803_081300.tif"); put(cam / "f.tif")    # its OWN name: stranded
    put(old / "OLD_TIFFs" / "z.tif")                     # backup of a backup: ignored
    put(old / "notes.txt")
    res = ct._stranded_backups([old, old / "OLD_TIFFs"])
    assert sorted((f.name, t.name) for f, t in res) == [
        ("a.tif", "a.tif"), ("e.tif", "e.tif"), ("e_20260803_081300_1.tif", "e.tif"),
        ("f_20260803_081300.tif", "f_20260803_081300.tif")]


def test_restore_stranded_only_never_overwrites_and_leaves_ambiguity(tmp_path):
    cam = tmp_path / HOSTILE / "01_D810"
    old = cam / "OLD_TIFFs"
    put(old / "a.tif", b"A")
    put(old / "c.tif", b"C1"); put(old / "c_20260803_081300.tif", b"C2")   # both claim c.tif
    put(old / "b.tif", b"B")
    res = ct._stranded_backups([old])
    put(cam / "b.tif", b"NEW")                           # appeared after the scan
    assert ct._restore_stranded_only(res) is True
    assert (cam / "a.tif").read_bytes() == b"A" and not (old / "a.tif").exists()
    assert (old / "c.tif").exists() and (old / "c_20260803_081300.tif").exists()
    assert not (cam / "c.tif").exists()
    assert (cam / "b.tif").read_bytes() == b"NEW" and (old / "b.tif").exists()
