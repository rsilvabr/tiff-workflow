r"""Move the files of every OLD_PADDED or OLD_TIFFs folder to a backup drive, keeping the tree.

The two backup folders this toolkit writes: OLD_PADDED (originals of the padded 16-bit ->
8-bit conversion) and OLD_TIFFs (originals kept by compress_tiff_zip modes 0/9).

    python move_old_backups.py --folder OLD_PADDED        # dry run: lists what would move
    python move_old_backups.py --folder OLD_PADDED --go   # copy, verify (SHA-256), delete the source
    python move_old_backups.py --folder OLD_TIFFs --roots 2010-2026 --go

G:\\2019\\Shoot\\NORITSU\\OLD_PADDED\\a.tif -> H:\\OLD_PADDED_BKP\\2019\\Shoot\\NORITSU\\OLD_PADDED\\a.tif
(--dest defaults to DEFAULT_DESTS[--folder]). Called move_old_padded.py until round 25.

A source is deleted only after its copy on the backup drive reads back byte-identical.
An existing destination with different content is never overwritten: the file is skipped
and reported. Long paths, Japanese names and brackets are handled (\\\\?\\ paths).

A backup whose ORIGINAL PLACE IS EMPTY is never moved (audit round 25): it is not a spare
copy but the only one. compress_tiff_zip modes 0/9 move every original of a folder to
OLD_TIFFs before writing its compressed copy; a run killed in between left 365 originals
there with nothing in their place, and on 2026-10-17 this script moved them to H:\ as if
they were leftovers. "Its place" is the parent folder of OLD_*: the same name, the other
TIFF extension (.tif/.tiff), and the base name of a renamed backup (name_v2.tif from the
padded conversion, name_20260803_081300.tif from compress). Put an OLD_TIFFs one back with
`compress_tiff_zip.py -Mode 9 -InputDir <folder> -RestoreStranded` (it moves them back, then
compresses the folder as mode 9 does; add -DryRun to see first) or with the wizard's Restore
OLD_TIFFs; an OLD_PADDED one by hand -- move it back into the folder above OLD_PADDED
(neither knows OLD_PADDED).

Audit round 28: a name counts as RENAMED only in the folder whose writer makes that rename
(_vN in OLD_PADDED, the date stamp in OLD_TIFFs) and only when the backup it collided with,
<base>.tif/.tiff, is in the same folder -- the only case in which either writer renames.
Otherwise the suffix is part of the photo's own name: OLD_TIFFs\portrait_v2.tif, or
IMG_20190815_143000.tif from a phone, with nothing in its place but an unrelated
portrait.tif / IMG.tif next to it, was moved off the disk as a spare copy.
"""
import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_SOURCE = "G:\\"
DEFAULT_ROOTS = [f"{y}" for y in range(2015, 2024)]   # folders below the source drive
# Backup folder name -> default destination on the backup drive
DEFAULT_DESTS = {
    "OLD_PADDED": "H:\\OLD_PADDED_BKP",
    "OLD_TIFFs": "H:\\OLD_TIFFS_BCK",
}
TIFF_EXTS = (".tif", ".tiff")
# Renamed backups, per folder: name_v2 (padded conversion, OLD_PADDED), name_20260803_081300[_1]
# (compress modes 0/9, OLD_TIFFs). Keys are upper-case folder names.
_RENAMED = {
    "OLD_PADDED": re.compile(r"^(?P<base>.+)_v\d+$"),
    "OLD_TIFFS": re.compile(r"^(?P<base>.+)_\d{8}_\d{6}(?:_\d+)?$"),
}


def _renamed_base(backup: Path):
    """The base name a renamed backup was made from, or None (see the module docstring:
    the folder's own rename, and only next to the backup it collided with)."""
    rx = _RENAMED.get(backup.parent.name.upper())
    m = rx.match(backup.stem) if rx else None
    if not m:
        return None
    base = m["base"]
    if any(os.path.exists(_long(backup.parent / f"{base}{e}")) for e in TIFF_EXTS):
        return base
    return None


def _long(p: Path) -> str:
    s = str(p.resolve()) if not str(p).startswith("\\\\?\\") else str(p)
    return s if s.startswith("\\\\?\\") else "\\\\?\\" + s


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(_long(path), "rb") as f:
        for chunk in iter(lambda: f.read(8 * 2**20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s" if s < 60 else (f"{s // 60}m" if s < 3600 else f"{s // 3600}h{(s % 3600) // 60:02d}m")


def counterparts(backup: Path):
    """Where the file this backup was made from lives (or lived): the folder above OLD_*."""
    parent = backup.parent.parent
    ext = backup.suffix.lower()
    if ext not in TIFF_EXTS:
        return [parent / backup.name]
    stems = [backup.stem]
    base = _renamed_base(backup)
    if base:
        stems.append(base)
    return [parent / f"{s}{e}" for s in stems for e in TIFF_EXTS]


def place_is_empty(backup: Path) -> bool:
    return not any(os.path.exists(_long(c)) for c in counterparts(backup))


def find(source: Path, roots, folder: str):
    jobs = []
    for r in roots:
        root = source / r
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if Path(dirpath).name.upper() != folder.upper():
                continue
            for name in filenames:
                jobs.append(Path(dirpath) / name)
    return sorted(jobs)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--roots", default=";".join(DEFAULT_ROOTS),
                    help="';'-separated folders below --source to scan")
    ap.add_argument("--folder", required=True, choices=sorted(DEFAULT_DESTS),
                    help="which backup folders to move")
    ap.add_argument("--dest", default=None,
                    help="backup root (default: DEFAULT_DESTS for --folder)")
    ap.add_argument("--go", action="store_true", help="really move (default: dry run)")
    a = ap.parse_args(argv)
    source = Path(a.source)
    dest = Path(a.dest if a.dest else DEFAULT_DESTS[a.folder])
    roots = []
    for r in (x for x in a.roots.split(";") if x):
        if "-" in r and all(part.isdigit() for part in r.split("-", 1)):
            lo, hi = map(int, r.split("-", 1))
            roots += [str(y) for y in range(lo, hi + 1)]
        else:
            roots.append(r)
    folder = a.folder

    print(f"Searching {folder} folders under {source} ({', '.join(roots)}) ...", flush=True)
    found = find(source, roots, folder)
    only_copies = [j for j in found if place_is_empty(j)]
    kept = set(only_copies)
    # Renamed copies first: each is re-checked right before its move, and its base backup
    # (moved in the same run) is what makes it a renamed copy.
    jobs = sorted((j for j in found if j not in kept),
                  key=lambda j: (_renamed_base(j) is None, str(j)))
    total_bytes = sum(os.path.getsize(_long(j)) for j in jobs)
    print(f"Found {len(found)} file(s) in {len({j.parent for j in found})} {folder} folder(s); "
          f"{len(jobs)} to move, {total_bytes / 2**30:.1f} GB", flush=True)
    if only_copies:
        print(f"\nKEPT: {len(only_copies)} backup(s) have NOTHING in their original place -- "
              f"each is the only copy of that image and is never moved:")
        for j in only_copies[:40]:
            print(f"  {j}")
        if len(only_copies) > 40:
            print(f"  ... and {len(only_copies) - 40} more")
        if folder.upper() == "OLD_PADDED":
            # -RestoreStranded and the wizard's Restore only know OLD_TIFFs (audit round 28).
            print("Put them back first: move each one by hand into the folder above OLD_PADDED.\n")
        else:
            print("Put them back first (compress_tiff_zip.py -Mode 9 -InputDir <folder> "
                  "-RestoreStranded, the wizard's Restore OLD_TIFFs, or by hand).\n")
    if not a.go:
        for j in jobs[:15]:
            print(f"  {j}  ->  {dest / j.relative_to(source)}")
        if len(jobs) > 15:
            print(f"  ... and {len(jobs) - 15} more")
        print("Dry run: nothing moved. Add --go to move.")
        return 1 if only_copies else 0

    moved = skipped = failed = 0
    done_bytes = 0
    t0 = time.time()
    for i, src in enumerate(jobs, 1):
        rel = src.relative_to(source)
        dst = dest / rel
        size = os.path.getsize(_long(src))
        try:
            # Checked again right before the move: the original may have gone meanwhile.
            if place_is_empty(src):
                skipped += 1
                status = "KEPT: nothing in its original place any more -- the only copy"
            else:
                os.makedirs(_long(dst.parent), exist_ok=True)
                if os.path.exists(_long(dst)):
                    if _sha256(dst) == _sha256(src):
                        os.remove(_long(src))
                        moved += 1
                        status = "already on the backup (identical) -- source removed"
                    else:
                        skipped += 1
                        status = "SKIPPED: a DIFFERENT file with this name is already on the backup"
                else:
                    tmp = dst.with_name(dst.name + ".copying")
                    with open(_long(src), "rb") as fi, open(_long(tmp), "wb") as fo:
                        for chunk in iter(lambda: fi.read(8 * 2**20), b""):
                            fo.write(chunk)
                    st = os.stat(_long(src))
                    os.utime(_long(tmp), ns=(st.st_atime_ns, st.st_mtime_ns))
                    if _sha256(tmp) != _sha256(src):
                        os.remove(_long(tmp))
                        failed += 1
                        status = "FAILED: the copy did not verify -- source kept"
                    else:
                        os.replace(_long(tmp), _long(dst))
                        os.remove(_long(src))
                        moved += 1
                        status = "moved"
        except OSError as e:
            failed += 1
            status = f"FAILED: {e} -- source kept"
        done_bytes += size
        spent = time.time() - t0
        left = spent / done_bytes * (total_bytes - done_bytes) if done_bytes else 0
        print(f"  [{i}/{len(jobs)}] {status}  {rel}", flush=True)
        if i % 25 == 0 and i < len(jobs):
            print(f"  {i}/{len(jobs)} -- {done_bytes / 2**30:.1f}/{total_bytes / 2**30:.1f} GB in "
                  f"{_fmt(spent)} -- about {_fmt(left)} left", flush=True)

    # Remove the backup folders that are now empty (only those).
    removed_dirs = 0
    for d in sorted({j.parent for j in jobs}, key=lambda p: len(str(p)), reverse=True):
        try:
            if not os.listdir(_long(d)):
                os.rmdir(_long(d))
                removed_dirs += 1
        except OSError:
            pass
    print(f"\nDone: {moved} moved, {skipped} skipped, {failed} failed, "
          f"{len(only_copies)} kept (only copy) | "
          f"{removed_dirs} empty {folder} folder(s) removed | {_fmt(time.time() - t0)}")
    return 1 if (skipped or failed or only_copies) else 0


if __name__ == "__main__":
    sys.exit(main())
