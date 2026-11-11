# Installing tiff-workflow (the Python version)

> Rather not install Python and the tools? A Windows program packages
> everything: see [tiff-workflow-exe](https://github.com/rsilvabr/tiff-workflow-exe).

### 1. External Tools (Download Executables, NOT Source Code)

| Tool | Download URL | What to Download | Extract to |
|------|-------------|------------------|------------|
| **ImageMagick 7** | https://imagemagick.org/script/download.php | Installer `.exe` (Q16-HDRI x64) | Default location |
| **exiftool** | https://exiftool.org | `exiftool-XX.XX_64.zip` **(Windows .zip, NOT .tar.gz)** | `C:\tools\exiftool\` or your choice |

#### Common Download Mistakes

| Wrong Download | Why It Fails | Correct Download |
|---------------|--------------|------------------|
| `exiftool-XX.XX.tar.gz` | Perl source code, needs Perl installed | `exiftool-XX.XX_64.zip` (Windows executable) |

#### exiftool Setup — Renaming IS Required Here

> **Unlike jxl-photo, this toolkit requires the plain name `exiftool.exe`.** Every script calls `exiftool` directly — a file still named `exiftool(-k).exe` is not found, and the dependency check reports exiftool as missing.

The Windows download comes as `exiftool(-k).exe`. Rename it:

```powershell
# Option A: Rename the file
Rename-Item "C:\tools\exiftool\exiftool(-k).exe" "exiftool.exe"

# Option B: Duplicate and rename (keeps the original)
Copy-Item "C:\tools\exiftool\exiftool(-k).exe" "C:\tools\exiftool\exiftool.exe"
```

### 2. Add to PATH (in a PowerShell terminal)

**Replace the example paths below with YOUR actual installation paths:**

```powershell
# EDIT THESE PATHS to match where YOU installed/extracted the tools:
$myPaths = @(
    "C:\tools\exiftool",                              # where exiftool.exe is (RENAMED!)
    "C:\Program Files\ImageMagick-7.1.1-Q16-HDRI"     # where magick.exe is
)

# Add to user PATH
$p = [Environment]::GetEnvironmentVariable("PATH", "User")
[Environment]::SetEnvironmentVariable("PATH", ($myPaths -join ";") + ";$p", "User")

# RESTART your PowerShell/terminal after this!
```

> **Important:** Both `magick.exe` and `exiftool.exe` must be on your PATH for the scripts and the wizard to find them. They only search the system PATH — they do not look in other directories.

### 3. Python (+ Rich for the wizard)

Python 3.9 or newer runs everything. The three backends use only what Python ships with; the guided wizard (`convert_tiff.py`) also wants the `rich` package for its menus (without it, it falls back to plain text).

```powershell
pip install rich
```

PowerShell itself is no longer needed: any terminal that starts `python` will do.

> Install `rich` in the same Python version you'll use to run the wizard: `python -m pip install rich`.

### 4. Verify Installation

**Restart the terminal**, then run:

```powershell
# Each should return a version number
python --version    # Python 3.9 or newer
magick --version    # ImageMagick 7.x.x
exiftool -ver       # 13.xx

# Test full environment
cd "C:\Users\YourName\Documents\GitHub\tiff-workflow"  # adjust path
python convert_tiff.py
```

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| `exiftool` not recognized / wizard reports exiftool missing | Its folder is not on PATH, or the file is still named `exiftool(-k).exe` | Add the folder to PATH (step 2 above) and reopen the terminal; **rename** to `exiftool.exe` — unlike jxl-photo, this toolkit does not detect the `(-k)` name |
| `magick` not recognized | ImageMagick folder not on PATH | Add its folder to PATH and reopen the terminal |
| `exiftool` returns nothing | Downloaded `.tar.gz` (Perl source) | Download the `.zip` with `_64` suffix |
| `ModuleNotFoundError: No module named 'rich'` | `rich` installed in a different Python version | Run `python -m pip install rich` |
| PATH still not working | Terminal not restarted | Close and reopen the terminal completely |
| `No module named 'tiff_workflow_common'` | A backend was copied somewhere on its own | Keep `tiff_workflow_common.py` in the same folder as the three backends |
| "ERROR (exiftool check) — cannot detect compression" | exiftool can't read the file | `exiftool -s -s -s -Compression photo.tif` — should output: Deflate, LZW, or nothing (uncompressed) |
| "ERROR (magick) — compression failed" | magick can't read the file | `magick identify -verbose photo.tif` — should display image info without errors |
