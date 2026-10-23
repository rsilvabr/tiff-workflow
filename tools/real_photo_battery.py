#!/usr/bin/env python3
"""Real-photo battery: the backends end to end on the owner's real photos.

The pytest suite is synthetic; multi-page layouts, ICC profiles, non-ASCII and
'%' names, metadata round-trips and the destructive in-place paths only show up
on real files. This battery runs every backend as a SUBPROCESS with stdin closed
(exactly like an unattended run), on COPIES of real photos, and checks the files
on disk afterwards.

    python tools/real_photo_battery.py --fixtures <folder>
    (or set TW_FIXTURES=<folder> and omit the flag)

Options: --scratch <dir>, --keep, --shells powershell,pwsh.

Fixtures (never modified -- everything is copied to a scratch folder first):
    <fixtures>/TIFF16/*.tif    >= 2 single-image 16-bit RGB TIFFs with ICC + EXIF
                               (the first two sorted by name are used)
    <fixtures>/raw_scan/*.tif  a 3-page film scan [image, reduced preview, IR MASK]
    <fixtures>/tiff_scan/*.tif a 2-page scan [image, reduced preview]
    <fixtures>/fuji_s3s5/<session>/   Fuji S3/S5 sessions: TIF + JPG and/or RAF
    <fixtures>/TIFF_tagless/S5Pro/    S5 Pro TIFFs without a single tag + their JPGs

A missing optional folder (raw_scan, tiff_scan, fuji_s3s5, TIFF_tagless) skips its section
and the report says so. On the owner's machine the fixtures live in E:\\TESTE.

The scratch folder (default <fixtures drive>\\tw_battery_<YYMMDD_HHMMSS>) is
deleted at the end unless --keep, or a check failed -- then it is kept and its
path printed. Every backend run is logged under <scratch>\\_logs\\<shell>__<tag>.log.
The report is written to AI_tools/<YYMMDD>_battery_report.md and printed.
Exit code 0 when every check passed, 1 otherwise. Needs magick and exiftool.
"""
import argparse
import builtins
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import convert_tiff  # noqa: E402  (the repo root must be on sys.path first)

try:  # folder names with Japanese characters must not kill the console print
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

results = []          # (section, name, ok, detail)
skips = []            # text lines for the report
SCRATCH = None
SHELL_DIR = None
LOGS = None
CURRENT_SHELL = None
_section = ""


def section(name):
    global _section
    _section = name
    print(f"\n== {name}", flush=True)


def check(name, ok, detail=""):
    results.append((_section, name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""),
          flush=True)


def run(tag, script, args, timeout=1800):
    """One backend run as an unattended subprocess (stdin closed).

    Under powershell the command goes through _wrap_ps5_command, so that
    -SafeMode:$false binds (PS5 -File would bind the string '$false')."""
    cmd = ([CURRENT_SHELL, "-NoProfile", "-File", str(REPO / script)]
           + [str(a) for a in args])
    if CURRENT_SHELL == "powershell":
        cmd = convert_tiff._wrap_ps5_command(cmd)
    t = time.time()
    rc = None
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(SHELL_DIR))
        rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        def _part(v):
            if v is None:
                return ""
            return v if isinstance(v, str) else v.decode("utf-8", "replace")
        out = _part(e.stdout) + _part(e.stderr) + f"\n[TIMEOUT after {timeout}s]"
    (LOGS / f"{CURRENT_SHELL}__{tag}.log").write_text(
        f"$ {' '.join(cmd)}\nrc={rc} ({time.time() - t:.0f}s)\n\n{out}",
        encoding="utf-8")
    print(f"   [{tag}] rc={rc} {time.time() - t:.0f}s", flush=True)
    bad = [m for m in ("ParserError", "CategoryInfo", "Exception calling") if m in out]
    check(f"{tag}: no PowerShell error record", not bad,
          f"found {bad}" if bad else "")
    return rc, out


# -- helpers ----------------------------------------------------------------

def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def cp(src, dst):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def _exe(args, timeout=600):
    return subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout)


def small(src, dst):
    """Fast working copy: page 0, 1024x1024, uncompressed, with src's EXIF + ICC."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = _exe(["magick", f"{src}[0]", "-crop", "1024x1024+0+0", "+repage",
              "-compress", "none", dst])
    if r.returncode != 0:
        raise RuntimeError(f"magick small({src}) failed: {r.stderr[:200]!r}")
    r = _exe(["exiftool", "-q", "-q", "-overwrite_original",
              "-tagsfromfile", str(src), "-all:all", dst])
    if r.returncode != 0:
        raise RuntimeError(f"exiftool small({src}) failed: {r.stderr[:200]!r}")
    return dst


def _all_uncompressed(p):
    r = _exe(["exiftool", "-a", "-s", "-s", "-s", "-n", "-Compression", p])
    vals = [ln.strip() for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    return bool(vals) and all(v == "1" for v in vals)


def uncompressed(src, dst):
    """Full-size working copy (every page), uncompressed, with src's EXIF + ICC.

    An already-uncompressed fixture is copied byte for byte: an ImageMagick rewrite
    turns every SubfileType into 2 (page), and the scanner layout [0 image,
    1 preview, 4 IR mask] is exactly what C6-C8 exist to exercise (round 22 review)."""
    dst = Path(dst)
    if _all_uncompressed(src):
        return cp(src, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = _exe(["magick", str(src), "-compress", "none", dst])
    if r.returncode != 0:
        raise RuntimeError(f"magick uncompressed({src}) failed: {r.stderr[:200]!r}")
    r = _exe(["exiftool", "-q", "-q", "-overwrite_original",
              "-tagsfromfile", str(src), "-all:all", dst])
    if r.returncode != 0:
        raise RuntimeError(f"exiftool uncompressed({src}) failed: {r.stderr[:200]!r}")
    return dst


def pixels_equal(a, b, page=0):
    """AE of one page pair; True only for exit 0/1 and an AE of exactly 0."""
    try:
        r = subprocess.run(["magick", "compare", "-metric", "AE",
                            f"{a}[{page}]", f"{b}[{page}]", "null:"],
                           capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False
    if r.returncode not in (0, 1):
        return False
    text = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
    try:
        return float(text.split()[0]) == 0.0
    except (ValueError, IndexError):
        return False


def meta(p):
    """The tags the compress path must preserve, plus the ICC profile's md5."""
    r = _exe(["exiftool", "-j", "-n", "-G0", "-EXIF:Make", "-EXIF:Model",
              "-EXIF:DateTimeOriginal", "-EXIF:ExposureTime", "-EXIF:FNumber",
              "-EXIF:ISO", "-XMP:CreatorTool", "-ICC_Profile:ProfileDescription", p])
    try:
        d = json.loads(r.stdout.decode("utf-8", "replace"))[0]
        d.pop("SourceFile", None)
    except (ValueError, IndexError, KeyError):
        # The nonce keeps two failed reads from comparing equal (a false PASS).
        d = {"meta_error": r.stdout[:200].decode("utf-8", "replace"),
             "nonce": os.urandom(8).hex()}
    d["ICC_md5"] = hashlib.md5(_exe(["exiftool", "-b", "-ICC_Profile", p]).stdout).hexdigest()
    return d


def compression(p):
    return _exe(["exiftool", "-s", "-s", "-s", "-IFD0:Compression", p]).stdout.decode(
        "utf-8", "replace").strip()


def pages(p):
    r = _exe(["magick", "identify", "-format", "%p\n", p])
    return len([ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()])


def subfiletype_by_ifd(p):
    """{IFD index: value} for the pages that CARRY the tag -- exiftool prints no
    line at all when a page has no SubfileType (the main image usually has none)."""
    r = _exe(["exiftool", "-a", "-G1", "-s", "-s", "-n", "-SubfileType", p])
    by_ifd = {}
    for ln in r.stdout.decode("utf-8", "replace").splitlines():
        m = re.match(r"\[IFD(\d+)\]\s+SubfileType:?\s+(\d+)\s*$", ln.strip())
        if m:
            by_ifd[int(m.group(1))] = int(m.group(2))
    return by_ifd


def exif_tag(path, tag):
    """One tag via a UTF-8 -@ argfile so non-ASCII and '%' paths stay intact."""
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(["-charset", "filename=utf8", "-s", "-s", "-s", tag,
                            str(path)]) + "\n")
        argfile = fh.name
    try:
        r = subprocess.run(["exiftool", "-@", argfile], capture_output=True, timeout=120)
    finally:
        os.unlink(argfile)
    return r.stdout.decode("utf-8", "replace").strip()


def find_pair(tif):
    r"""Find-JpegPair's search order: JPEG exact -> JPEG variant (_1.._99 stripped)
    -> RAF exact -> RAF variant -> JPEG heuristic (_\d{3,4} stripped)."""
    base = tif.stem
    variant = re.sub(r"_\d{1,2}$", "", base)
    heur = re.sub(r"_\d{3,4}$", "", base)
    search_dirs = [d for d in (
        tif.parent, tif.parent / "JPEG", tif.parent / "JPG",
        tif.parent.parent, tif.parent.parent / "JPEG", tif.parent.parent / "JPG")
        if d.is_dir()]
    steps = [(base, "JPEG")]
    if variant and variant != base:
        steps.append((variant, "JPEG"))
    steps.append((base, "RAF"))
    if variant and variant != base:
        steps.append((variant, "RAF"))
    if heur and heur != base:
        steps.append((heur, "JPEG"))
    for b, kind in steps:
        for d in search_dirs:
            if kind == "JPEG":
                for ext in (".jpg", ".jpeg"):
                    p = d / (b + ext)
                    if p.is_file():
                        return p
            else:
                for f in d.iterdir():
                    if (f.is_file() and f.stem.lower() == b.lower()
                            and f.suffix.lower() == ".raf"):
                        return f
    return None


def _make_long_path(base, target=240):
    """Nest folders under base until str(<deepest>/p.tif) is exactly target chars."""
    base.mkdir(parents=True, exist_ok=True)
    dirstring_len = target - len(str(base)) - len("p.tif") - 2
    assert dirstring_len > 41, f"scratch path too long for a {target}-char fixture"
    n_full, rem = divmod(dirstring_len, 41)
    parts = ["d" * 40] * n_full
    if rem == 0:
        parts[-1] = "d" * 41
    else:
        parts.append("d" * rem)
    p = base
    for name in parts:
        p = p / name
    p.mkdir(parents=True, exist_ok=True)
    f = p / "p.tif"
    assert len(str(f)) == target, f"built path has {len(str(f))} chars, not {target}"
    return p


def _wizard(fn, folder, tag):
    """Call a wizard function in-process with the prompts answered, log its output."""
    cfg = SimpleNamespace(config=convert_tiff.ToolConfig())
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if "[y/N]" in prompt or "[Y/n]" in prompt or "Delete" in prompt:
            return "y"
        return datetime.now().strftime("%H:%M")

    old = (convert_tiff.step_folder, convert_tiff.RICH_AVAILABLE, builtins.input)
    buf = io.StringIO()
    try:
        convert_tiff.step_folder = lambda cfg, *a, **k: folder
        convert_tiff.RICH_AVAILABLE = False
        builtins.input = fake_input
        with redirect_stdout(buf):
            result = fn(cfg)
    finally:
        (convert_tiff.step_folder, convert_tiff.RICH_AVAILABLE,
         builtins.input) = old
    (LOGS / f"{CURRENT_SHELL}__{tag}.log").write_text(
        buf.getvalue() + "\nPROMPTS:\n" + "\n".join(prompts) + "\n", encoding="utf-8")
    return result


# -- checks -----------------------------------------------------------------

def c1(A1, A2):
    section(f"C1: mode 0 in place, full size [{CURRENT_SHELL}]")
    m0 = SHELL_DIR / "m0"
    files = [uncompressed(A1, m0 / A1.name), uncompressed(A2, m0 / A2.name)]
    before = {f.name: md5(f) for f in files}
    before_meta = {f.name: meta(f) for f in files}

    rc, _ = run("c1_run1", "compress_tiff_zip.ps1",
                ["-Mode", "0", "-InputDir", str(m0), "-Workers", "2"])
    check("C1: first run rc == 0", rc == 0, f"rc={rc}")
    for f in files:
        comp = compression(f)
        check(f"C1: {f.name}: Deflate in place", comp in ("Deflate", "Adobe Deflate"), comp)
        old = m0 / "OLD_TIFFs" / f.name
        check(f"C1: {f.name}: OLD_TIFFs backup exists", old.is_file())
        if old.is_file():
            check(f"C1: {f.name}: backup md5 == pre-run md5", md5(old) == before[f.name])
            check(f"C1: {f.name}: backup pixels == compressed pixels", pixels_equal(old, f))
        after_meta = meta(f)
        check(f"C1: {f.name}: metadata unchanged", after_meta == before_meta[f.name],
              "" if after_meta == before_meta[f.name]
              else repr((before_meta[f.name], after_meta))[:300])

    old_list1 = sorted(p.name for p in (m0 / "OLD_TIFFs").glob("*"))
    md5_1 = {f.name: md5(f) for f in files}
    rc, _ = run("c1_run2", "compress_tiff_zip.ps1",
                ["-Mode", "0", "-InputDir", str(m0), "-Workers", "2"])
    check("C1: second run rc == 0", rc == 0, f"rc={rc}")
    check("C1: second run: output md5 unchanged",
          {f.name: md5(f) for f in files} == md5_1)
    check("C1: second run: OLD_TIFFs list unchanged",
          sorted(p.name for p in (m0 / "OLD_TIFFs").glob("*")) == old_list1)


def c2(A1, A2):
    section(f"C2: mode 1 subfolder [{CURRENT_SHELL}]")
    m1 = SHELL_DIR / "m1"
    files = [small(A1, m1 / A1.name), small(A2, m1 / A2.name)]
    src_md5 = {f.name: md5(f) for f in files}
    src_meta = {f.name: meta(f) for f in files}

    rc, _ = run("c2_run1", "compress_tiff_zip.ps1",
                ["-Mode", "1", "-InputDir", str(m1), "-Workers", "2"])
    check("C2: first run rc == 0", rc == 0, f"rc={rc}")
    for f in files:
        out = m1 / "ZIP" / f.name
        check(f"C2: {f.name}: ZIP output exists", out.is_file())
        if out.is_file():
            comp = compression(out)
            check(f"C2: {f.name}: output is Deflate", comp in ("Deflate", "Adobe Deflate"), comp)
            check(f"C2: {f.name}: pixels equal", pixels_equal(f, out))
            out_meta = meta(out)
            check(f"C2: {f.name}: metadata equal", out_meta == src_meta[f.name],
                  "" if out_meta == src_meta[f.name] else repr((src_meta[f.name], out_meta))[:300])
        check(f"C2: {f.name}: source md5 unchanged", md5(f) == src_md5[f.name])

    def _out_state():
        return {f.name: (md5(m1 / "ZIP" / f.name), (m1 / "ZIP" / f.name).stat().st_mtime_ns)
                for f in files if (m1 / "ZIP" / f.name).is_file()}

    state = _out_state()
    rc, _ = run("c2_run2", "compress_tiff_zip.ps1",
                ["-Mode", "1", "-InputDir", str(m1), "-Workers", "2"])
    check("C2: second run rc == 0", rc == 0, f"rc={rc}")
    check("C2: second run: outputs md5/mtime unchanged", _out_state() == state)


def c3(A1, A2):
    section(f"C3: mode 2 flat name collision [{CURRENT_SHELL}]")
    m2 = SHELL_DIR / "m2"
    a = small(A1, m2 / "in" / "a" / "x.tif")
    b = small(A2, m2 / "in" / "b" / "x.tif")
    rc, _ = run("c3", "compress_tiff_zip.ps1",
                ["-Mode", "2", "-InputDir", str(m2 / "in"),
                 "-OutputDir", str(m2 / "out"), "-Workers", "2"])
    check("C3: rc == 0", rc == 0, f"rc={rc}")
    out = m2 / "out"
    names = sorted(p.name for p in out.iterdir() if p.is_file()) if out.is_dir() else []
    check("C3: out holds exactly x.tif and x_v2.tif", names == ["x.tif", "x_v2.tif"], str(names))
    o1, o2 = out / "x.tif", out / "x_v2.tif"
    if o1.is_file() and o2.is_file():
        srcs = {"a": a, "b": b}
        mapping = {o.name: sorted(k for k, s in srcs.items() if pixels_equal(s, o))
                   for o in (o1, o2)}
        ok = (len(mapping["x.tif"]) == 1 and len(mapping["x_v2.tif"]) == 1
              and mapping["x.tif"] != mapping["x_v2.tif"])
        check("C3: each output matches one source, outputs map to different sources",
              ok, str(mapping))


def c4(A1, A2):
    section(f"C4: mode 8 with delete [{CURRENT_SHELL}]")
    m8 = SHELL_DIR / "m8"
    ref8 = SHELL_DIR / "ref8"
    pairs = []
    for src in (A1, A2):
        ref = small(src, ref8 / src.name)
        pairs.append((ref, cp(ref, m8 / src.name)))
    rc, _ = run("c4", "compress_tiff_zip.ps1",
                ["-Mode", "8", "-InputDir", str(m8), "-DeleteSource",
                 "-StagingDir", str(SHELL_DIR / "stage8"), "-Workers", "2"])
    check("C4: rc == 0", rc == 0, f"rc={rc}")
    for ref, f in pairs:
        comp = compression(f)
        check(f"C4: {f.name}: Deflate in place", comp in ("Deflate", "Adobe Deflate"), comp)
        check(f"C4: {f.name}: no OLD_TIFFs", not (m8 / "OLD_TIFFs").exists())
        check(f"C4: {f.name}: pixels == reference", pixels_equal(ref, f))
        out_meta = meta(f)
        check(f"C4: {f.name}: metadata == reference", out_meta == meta(ref),
              "" if out_meta == meta(ref) else repr(out_meta)[:300])


def c5(A1):
    # Without -StagingDir, mode 8 picks a default staging folder by itself (no prompt)
    # and replaces each original in place after the pixel check -- the documented
    # contract. The round 22 plan assumed a Read-Host that blocks; there is none.
    section(f"C5: mode 8 without -StagingDir on a closed stdin [{CURRENT_SHELL}]")
    m8b = SHELL_DIR / "m8b"
    ref = small(A1, SHELL_DIR / "ref8b" / A1.name)
    f = cp(ref, m8b / A1.name)
    rc, _ = run("c5", "compress_tiff_zip.ps1",
                ["-Mode", "8", "-InputDir", str(m8b), "-DeleteSource", "-Workers", "1"],
                timeout=300)
    check("C5: run did not hang (timeout not hit)", rc is not None,
          "" if rc is not None else "timeout hit")
    check("C5: rc == 0", rc == 0, f"rc={rc}")
    check("C5: file still exists", f.is_file())
    if f.is_file():
        comp = compression(f)
        check("C5: Deflate in place", comp in ("Deflate", "Adobe Deflate"), comp)
        check("C5: pixels == reference", pixels_equal(ref, f))
        check("C5: metadata == reference", meta(f) == meta(ref))
    left = [p.name for p in m8b.iterdir() if p.is_dir() and p.name.lower().startswith("_cs8_")]
    check("C5: default staging folder removed", not left, ", ".join(left))


def _check_layout(tag, fixture, copy):
    """The working copy must keep the fixture's page layout, or the multi-page checks
    test something other than the real scanner file."""
    a, b = (pages(fixture), subfiletype_by_ifd(fixture)), (pages(copy), subfiletype_by_ifd(copy))
    check(f"{tag}: working copy keeps the fixture's page layout", a == b, f"{b} vs {a}")


def c6(raw_scan):
    section(f"C6: multi-page, SafeMode on [{CURRENT_SHELL}]")
    mp = SHELL_DIR / "mp"
    f = uncompressed(raw_scan, mp / raw_scan.name)
    _check_layout("C6", raw_scan, f)
    before = md5(f)
    rc, out = run("c6", "compress_tiff_zip.ps1",
                  ["-Mode", "0", "-InputDir", str(mp), "-Workers", "1"])
    check("C6: rc == 0", rc == 0, f"rc={rc}")
    check("C6: multi-page md5 unchanged", md5(f) == before)
    check("C6: no OLD_TIFFs backup was created", not (mp / "OLD_TIFFs" / f.name).exists())
    check("C6: output mentions MULTI", "MULTI" in out)


def c7(raw_scan):
    section(f"C7: multi-page, SafeMode off, mode 1 [{CURRENT_SHELL}]")
    mp = SHELL_DIR / "mp"
    src = mp / raw_scan.name
    before = md5(src)
    n_pages = pages(src)
    last = n_pages - 1
    rc, _ = run("c7", "compress_tiff_zip.ps1",
                ["-Mode", "1", "-InputDir", str(mp), "-SafeMode:$false", "-Workers", "1"])
    check("C7: rc == 0", rc == 0, f"rc={rc}")
    out = mp / "ZIP" / src.name
    check("C7: ZIP output exists", out.is_file())
    if out.is_file():
        check("C7: page count equal", pages(out) == n_pages,
              f"{pages(out)} vs {n_pages}")
        st_src, st_out = subfiletype_by_ifd(src), subfiletype_by_ifd(out)
        check("C7: subfiletypes equal", st_out == st_src, f"{st_out} vs {st_src}")
        check("C7: page 0 pixels equal", pixels_equal(src, out, 0))
        check(f"C7: last page ({last}) pixels equal", pixels_equal(src, out, last))
    check("C7: source md5 unchanged", md5(src) == before)


def c8(tiff_scan):
    section(f"C8: 2-page scan with preview, SafeMode on, mode 1 [{CURRENT_SHELL}]")
    sc = SHELL_DIR / "sc"
    src = uncompressed(tiff_scan, sc / tiff_scan.name)
    _check_layout("C8", tiff_scan, src)
    n_pages = pages(src)
    rc, _ = run("c8", "compress_tiff_zip.ps1",
                ["-Mode", "1", "-InputDir", str(sc), "-Workers", "1"])
    check("C8: rc == 0", rc == 0, f"rc={rc}")
    out = sc / "ZIP" / src.name
    check("C8: output exists", out.is_file())
    if out.is_file():
        check("C8: page count equal", pages(out) == n_pages, f"{pages(out)} vs {n_pages}")
        st_src, st_out = subfiletype_by_ifd(src), subfiletype_by_ifd(out)
        check("C8: subfiletypes equal", st_out == st_src, f"{st_out} vs {st_src}")
        check("C8: page 0 pixels equal", pixels_equal(src, out, 0))


def c9(A1):
    section(f"C9: '%' in the name [{CURRENT_SHELL}]")
    pct = SHELL_DIR / "pct"
    safe = small(A1, pct / "tmpsmall.tif")
    f = safe.rename(pct / "50%off.tif")
    rc, _ = run("c9", "compress_tiff_zip.ps1",
                ["-Mode", "0", "-InputDir", str(pct), "-Workers", "1"])
    check("C9: rc == 0", rc == 0, f"rc={rc}")
    comp = compression(f)
    check("C9: file is Deflate", comp in ("Deflate", "Adobe Deflate"), comp)
    old = pct / "OLD_TIFFs" / "50%off.tif"
    check("C9: metadata(new) == metadata(OLD_TIFFs backup)",
          old.is_file() and meta(f) == meta(old),
          "" if old.is_file() and meta(f) == meta(old) else repr(meta(f))[:300])


def c10(A1):
    section(f"C10: path near 260 chars [{CURRENT_SHELL}]")
    deep = _make_long_path(SHELL_DIR / "lp")
    f = small(A1, deep / "p.tif")
    assert len(str(f)) == 240
    before = md5(f)
    rc, _ = run("c10", "compress_tiff_zip.ps1",
                ["-Mode", "0", "-InputDir", str(deep), "-Workers", "1"])
    comp = compression(f)
    ok_a = (rc == 0 and comp in ("Deflate", "Adobe Deflate")
            and pixels_equal(deep / "OLD_TIFFs" / "p.tif", f))
    ok_b = rc != 0 and md5(f) == before
    which = ("A: compressed with a pixel-equal backup" if ok_a else
             "B: refused, source untouched" if ok_b else "neither")
    check("C10: either compressed with a verified backup, or refused with source intact",
          ok_a or ok_b, f"rc={rc}, {which}")


def c11(A1, A2):
    section(f"C11: -ExcludeFolders [{CURRENT_SHELL}]")
    ex = SHELL_DIR / "ex"
    small(A1, ex / "a.tif")
    small(A2, ex / "_EXPORT" / "b.tif")
    small(A1, ex / "My_EXPORT_photos" / "c.tif")
    rc, _ = run("c11a", "compress_tiff_zip.ps1",
                ["-Mode", "3", "-InputDir", str(ex), "-ExcludeFolders", "_EXPORT",
                 "-Workers", "2"])
    check("C11: rc == 0", rc == 0, f"rc={rc}")
    check("C11: ex\\ZIP\\a.tif exists", (ex / "ZIP" / "a.tif").is_file())
    check("C11: My_EXPORT_photos processed",
          (ex / "My_EXPORT_photos" / "ZIP" / "c.tif").is_file())
    check("C11: _EXPORT skipped", not (ex / "_EXPORT" / "ZIP" / "b.tif").exists())
    rc, _ = run("c11b", "compress_tiff_zip.ps1",
                ["-Mode", "3", "-InputDir", str(ex / "_EXPORT"),
                 "-ExcludeFolders", "_EXPORT", "-Workers", "1"])
    check("C11: pointing AT the name rc == 0", rc == 0, f"rc={rc}")
    check("C11: pointing AT the name processes it",
          (ex / "_EXPORT" / "ZIP" / "b.tif").is_file())


def c12(A1):
    section(f"C12: thumbnail add / remove [{CURRENT_SHELL}]")
    th = SHELL_DIR / "th"
    src = small(A1, th / A1.name)
    rc, _ = run("c12a", "compress_tiff_zip.ps1",
                ["-Mode", "1", "-InputDir", str(th), "-GenerateThumbnail", "-Workers", "1"])
    check("C12: add rc == 0", rc == 0, f"rc={rc}")
    out = th / "ZIP" / src.name
    if out.is_file():
        check("C12: output has 2 pages", pages(out) == 2, f"{pages(out)} pages")
        by_ifd = subfiletype_by_ifd(out)
        check("C12: page 1 subfiletype is 1", by_ifd.get(1) == 1, str(by_ifd))
        check("C12: page 0 pixels equal after add", pixels_equal(src, out, 0))
    else:
        check("C12: output exists after add", False, str(out))

    # generate_thumbnails -Remove deletes standalone <name>_thumb.* FILES; it never edits
    # a TIFF, so the embedded thumbnail page above must survive it untouched.
    embedded_md5 = md5(out) if out.is_file() else None
    rc, _ = run("c12b", "generate_thumbnails.ps1",
                ["-InputDir", str(th / "ZIP"), "-Remove"])
    check("C12: -Remove over embedded thumbnails rc == 0", rc == 0, f"rc={rc}")
    if embedded_md5:
        check("C12: -Remove leaves the TIFF with an embedded thumbnail untouched",
              out.is_file() and md5(out) == embedded_md5)

    th2 = SHELL_DIR / "th2"
    src2 = small(A1, th2 / A1.name)
    src2_md5 = md5(src2)
    rc, _ = run("c12c", "generate_thumbnails.ps1", ["-InputDir", str(th2)])
    check("C12: standalone add rc == 0", rc == 0, f"rc={rc}")
    thumbs = sorted(p.name for p in th2.glob(f"{src2.stem}_thumb*"))
    check("C12: a standalone thumbnail file was written", bool(thumbs), str(thumbs))
    rc, _ = run("c12d", "generate_thumbnails.ps1", ["-InputDir", str(th2), "-Remove"])
    check("C12: standalone remove rc == 0", rc == 0, f"rc={rc}")
    left = sorted(p.name for p in th2.glob(f"{src2.stem}_thumb*"))
    check("C12: -Remove deleted the standalone thumbnail", not left, str(left))
    check("C12: source TIFF untouched by add + remove", md5(src2) == src2_md5)


def c13(A1, A2):
    section(f"C13: Restore and Purge (wizard, in-process) [{CURRENT_SHELL}]")
    m0 = SHELL_DIR / "m0"
    if not m0.is_dir():
        check("C13: C1 tree available", False, "m0 missing")
        return

    pg = SHELL_DIR / "pg"
    shutil.copytree(m0, pg)
    parent_md5 = {p.name: md5(p) for p in pg.iterdir() if p.is_file()}
    ok = _wizard(convert_tiff.run_purge_old_tiffs, pg, "c13_purge")
    check("C13: purge returns True", ok is True, f"{ok!r}")
    left = list((pg / "OLD_TIFFs").glob("*.tif")) if (pg / "OLD_TIFFs").is_dir() else []
    check("C13: pg\\OLD_TIFFs holds no .tif", not left,
          ", ".join(p.name for p in left))
    check("C13: parent files unchanged (md5)",
          {p.name: md5(p) for p in pg.iterdir() if p.is_file()} == parent_md5)

    pgm = SHELL_DIR / "pgm"
    shutil.copytree(m0, pgm)
    victim = pgm / "OLD_TIFFs" / A1.name
    uncompressed(A2, victim)
    _wizard(convert_tiff.run_purge_old_tiffs, pgm, "c13_purge_mismatch")
    check("C13: mismatching backup is still there after the purge", victim.is_file())

    rs = SHELL_DIR / "rs"
    shutil.copytree(m0, rs)
    pre = {p.name: md5(p) for p in (rs / "OLD_TIFFs").iterdir() if p.is_file()}
    ok = _wizard(convert_tiff.run_undo_old_tiffs, rs, "c13_undo")
    check("C13: undo returns True", ok is True, f"{ok!r}")
    restored = {n: md5(rs / n) for n in pre if (rs / n).is_file()}
    check("C13: every parent has the pre-C1 md5 again",
          bool(pre) and restored == pre,
          "" if restored == pre else str({n: (pre[n], restored.get(n)) for n in pre}))


def c14(fixtures):
    section(f"C14: Copy EXIF on real Fuji sessions [{CURRENT_SHELL}]")
    fuji = fixtures / "fuji_s3s5"
    if not fuji.is_dir():
        print("(no fuji_s3s5 fixture: C14 skipped)")
        skips.append("C14 skipped: no fuji_s3s5 folder under the fixtures")
        return
    sessions = sorted(d for d in fuji.iterdir() if d.is_dir())
    fj = SHELL_DIR / "fj"
    fj_ref = SHELL_DIR / "fj_ref"
    for s in sessions:
        shutil.copytree(s, fj / s.name)
    tifs = sorted(p for p in fj.rglob("*")
                  if p.is_file() and p.suffix.lower() in (".tif", ".tiff"))
    for tif in tifs:
        cp(tif, fj_ref / tif.relative_to(fj))

    backend = ("copy_exif_to_TIFF_ps5.ps1" if CURRENT_SHELL == "powershell"
               else "copy_exif_to_TIFF_ps7.ps1")
    input_arg = ";".join(str(fj / s.name) for s in sessions)
    rc, _ = run("c14_run1", backend, ["-InputDir", input_arg, "-Workers", "2"])
    check("C14: first run rc == 0", rc == 0, f"rc={rc}")
    for tif in tifs:
        rel = tif.relative_to(fj)
        pair = find_pair(tif)
        if pair is None:
            check(f"C14: {rel}: has a paired source", False, "no pair found")
            continue
        for tag in ("-EXIF:Make", "-EXIF:Model", "-EXIF:DateTimeOriginal"):
            a, b = exif_tag(tif, tag), exif_tag(pair, tag)
            check(f"C14: {rel}: {tag} == its source", a == b and a != "",
                  ascii((a, b)))
        check(f"C14: {rel}: page 0 pixels unchanged", pixels_equal(fj_ref / rel, tif, 0))

    after1 = {t.relative_to(fj): md5(t) for t in tifs}
    rc, _ = run("c14_run2", backend,
                ["-InputDir", input_arg, "-SkipIfTiffHasExif", "-Workers", "2"])
    check("C14: second run (-SkipIfTiffHasExif) rc == 0", rc == 0, f"rc={rc}")
    check("C14: second run: every TIF md5 unchanged",
          {t.relative_to(fj): md5(t) for t in tifs} == after1)

    fj2 = SHELL_DIR / "fj2"
    for s in sessions:
        shutil.copytree(s, fj2 / s.name)
    list_file = SHELL_DIR / "fj2_dirs.txt"
    list_file.write_text("".join(str(fj2 / s.name) + "\n" for s in sessions),
                         encoding="utf-8", newline="\n")
    rc, _ = run("c14_run3", backend, ["-InputDirFile", str(list_file), "-Workers", "2"])
    check("C14: third run (-InputDirFile) rc == 0", rc == 0, f"rc={rc}")
    for tif in sorted(p for p in fj2.rglob("*")
                      if p.is_file() and p.suffix.lower() in (".tif", ".tiff")):
        rel = tif.relative_to(fj2)
        pair = find_pair(tif)
        if pair is None:
            check(f"C14: {rel} (-InputDirFile): has a paired source", False, "no pair found")
            continue
        make_t, make_s = exif_tag(tif, "-EXIF:Make"), exif_tag(pair, "-EXIF:Make")
        model_t, model_s = exif_tag(tif, "-EXIF:Model"), exif_tag(pair, "-EXIF:Model")
        check(f"C14: {rel} (-InputDirFile): Make/Model match the source",
              make_t == make_s and model_t == model_s and make_t != "",
              ascii((make_t, make_s, model_t, model_s)))

    # Round 23: -SkipIfTiffHasExif on FRESH exports must still write them -- it used to
    # count the IFD0 tags every TIFF has as EXIF and skip every file (the second run
    # above passes either way).
    fj3 = SHELL_DIR / "fj3"
    for s in sessions:
        shutil.copytree(s, fj3 / s.name)
    rc, _ = run("c14_run4", backend, ["-InputDir", ";".join(str(fj3 / s.name) for s in sessions),
                                      "-SkipIfTiffHasExif", "-Workers", "2"])
    check("C14: fresh exports with -SkipIfTiffHasExif: rc == 0", rc == 0, f"rc={rc}")
    for tif in sorted(p for p in fj3.rglob("*")
                      if p.is_file() and p.suffix.lower() in (".tif", ".tiff")):
        rel = tif.relative_to(fj3)
        pair = find_pair(tif)
        if pair is None:
            continue
        a, b = exif_tag(tif, "-EXIF:DateTimeOriginal"), exif_tag(pair, "-EXIF:DateTimeOriginal")
        check(f"C14: {rel} (fresh, -SkipIfTiffHasExif): EXIF written, not skipped",
              a == b and a != "", ascii((a, b)))

    # S5 Pro TIFFs that lost EVERY tag (no Software, no Make, no EXIF; image + the
    # 160x120 camera thumbnail page), as found in a real library: EXIF from the
    # JPEG, both pages pixel-identical.
    tagless = fixtures / "TIFF_tagless" / "S5Pro"
    if not tagless.is_dir():
        print("(no TIFF_tagless/S5Pro fixture: tagless check skipped)")
        return
    ft = SHELL_DIR / "ft" / "S5Pro"
    shutil.copytree(tagless, ft)
    rc, _ = run("c14_tagless", backend, ["-InputDir", str(ft), "-SkipIfTiffHasExif",
                                         "-Workers", "2"])
    check("C14: tagless S5 Pro TIFFs: rc == 0", rc == 0, f"rc={rc}")
    for tif in sorted(p for p in ft.iterdir() if p.suffix.lower() in (".tif", ".tiff")):
        pair = find_pair(tif)
        a = exif_tag(tif, "-EXIF:DateTimeOriginal")
        b = exif_tag(pair, "-EXIF:DateTimeOriginal") if pair else ""
        same = all(pixels_equal(tagless / tif.name, tif, pg) for pg in (0, 1))
        check(f"C14: tagless {tif.name}: EXIF from its JPEG, both pages unchanged",
              a == b and a != "" and same, ascii((a, b, same)))


def run_checks_for_shell(shell, fixtures, A1, A2, raw_scan, tiff_scan):
    global CURRENT_SHELL, SHELL_DIR, LOGS
    CURRENT_SHELL = shell
    SHELL_DIR = SCRATCH / shell
    SHELL_DIR.mkdir(parents=True, exist_ok=True)
    LOGS = SCRATCH / "_logs"
    LOGS.mkdir(parents=True, exist_ok=True)

    c1(A1, A2)
    c2(A1, A2)
    c3(A1, A2)
    c4(A1, A2)
    c5(A1)
    if raw_scan is not None:
        c6(raw_scan)
        c7(raw_scan)
    else:
        print("\n(no raw_scan fixture: C6/C7 skipped)")
        skips.append(f"C6/C7 skipped [{shell}]: no raw_scan folder under the fixtures")
    if tiff_scan is not None:
        c8(tiff_scan)
    else:
        print("\n(no tiff_scan fixture: C8 skipped)")
        skips.append(f"C8 skipped [{shell}]: no tiff_scan folder under the fixtures")
    c9(A1)
    c10(A1)
    c11(A1, A2)
    c12(A1)
    c13(A1, A2)
    c14(fixtures)


def pick_tiff16(fixtures):
    tiffs = sorted((fixtures / "TIFF16").glob("*.tif"), key=lambda p: p.name)
    if len(tiffs) < 2:
        print(f"need at least 2 TIFFs in {fixtures / 'TIFF16'}")
        sys.exit(2)
    return tiffs[0], tiffs[1]


def first_tif(folder):
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*.tif"), key=lambda p: p.name)
    return files[0] if files else None


def main():
    global SCRATCH
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixtures", type=Path,
                    default=(Path(os.environ["TW_FIXTURES"])
                             if os.environ.get("TW_FIXTURES") else None),
                    help="fixture folder (default: the TW_FIXTURES variable)")
    ap.add_argument("--scratch", type=Path, default=None,
                    help="scratch folder (default: <fixtures drive>\\tw_battery_<date>)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder")
    default_shells = [s for s in ("powershell", "pwsh") if shutil.which(s)]
    ap.add_argument("--shells", default=",".join(default_shells),
                    help="comma-separated shells (default: powershell and pwsh on PATH)")
    args = ap.parse_args()
    if args.fixtures is None or not args.fixtures.is_dir():
        ap.error("pass --fixtures <folder> or set TW_FIXTURES "
                 "(see the module docstring for the layout)")

    shells = [s.strip() for s in args.shells.split(",") if s.strip()]
    if not shells:
        ap.error("no shells selected (--shells)")
    for sh in shells:
        if sh not in ("powershell", "pwsh") or shutil.which(sh) is None:
            ap.error(f"{sh} is not available on PATH")
    for tool in ("magick", "exiftool"):
        if shutil.which(tool) is None:
            print(f"{tool} is not on PATH")
            sys.exit(2)

    A1, A2 = pick_tiff16(args.fixtures)
    raw_scan = first_tif(args.fixtures / "raw_scan")
    tiff_scan = first_tif(args.fixtures / "tiff_scan")

    SCRATCH = args.scratch or (Path(args.fixtures.anchor)
                               / f"tw_battery_{time.strftime('%y%m%d_%H%M%S')}")
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)

    t0 = time.time()
    try:
        for shell in shells:
            try:
                run_checks_for_shell(shell, args.fixtures, A1, A2, raw_scan, tiff_scan)
            except Exception as e:
                check(f"{shell}: section completed without crashing", False,
                      f"{type(e).__name__}: {e}")
    finally:
        n_ok = sum(1 for *_, ok, _ in results if ok)
        failed = [r for r in results if not r[2]]
        lines = [
            f"# Real-photo battery -- {time.strftime('%Y-%m-%d %H:%M')}", "",
            f"**{n_ok}/{len(results)} passed** in {time.time() - t0:.0f}s, "
            f"fixtures `{args.fixtures}`, shells {', '.join(shells)}", ""]
        if skips:
            lines += [f"Skipped: {s}" for s in skips] + [""]
        cur = None
        for sec, name, ok, d in results:
            if sec != cur:
                lines += ["", f"## {sec}", "", "| | Check | Detail |", "|---|---|---|"]
                cur = sec
            lines.append(f"| {'PASS' if ok else 'FAIL'} | {name} | {d.replace('|', '/')} |")
        report = REPO / "AI_tools" / f"{time.strftime('%y%m%d')}_battery_report.md"
        report.parent.mkdir(exist_ok=True)
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n{n_ok}/{len(results)} passed -- report: {report}")
        if failed:
            print(f"{len(failed)} FAIL(s):")
            for sec, name, ok, d in failed:
                print(f"  FAIL {name} - {d}")
        if not args.keep and not failed:
            shutil.rmtree(SCRATCH, ignore_errors=True)
        elif not args.keep:
            print(f"Scratch folder KEPT for inspection (a check failed): {SCRATCH}")
    sys.exit(0 if results and all(ok for *_, ok, _ in results) else 1)


if __name__ == "__main__":
    main()
