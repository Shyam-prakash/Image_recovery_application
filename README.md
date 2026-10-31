# Image Recovery Application

[![CI](https://github.com/Shyam-prakash/Image_recovery_application/actions/workflows/ci.yml/badge.svg)](https://github.com/Shyam-prakash/Image_recovery_application/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20WSL%20%7C%20Linux-lightgrey)

**Recover JPEG photos that open with grey areas, colour casts or scrambled bands.**

Photos recovered from damaged SD cards, failing phones or undelete tools often look like this:
the top is fine, then the picture turns grey, green or pink, or the rest of it is shifted
sideways. Usually most of the picture data is still in the file. A few corrupted bytes just
confuse every normal viewer from that point on.

`photo-restore` decodes the damaged data itself, finds every damage point, puts the blocks that
follow back in the right place with the right colours, and fills the few small gaps that are
really lost. It works on whole folders or ZIPs of photos, has a simple browser UI, and
everything runs **locally on your computer**: nothing is uploaded.

| Damage | What the tool does |
|---|---|
| Grey area from some point down (viewer stopped decoding) | Decodes past the damage and restores the rest |
| Wrong colour cast from some point down | Detects the jump and corrects the colour level |
| Content shifted sideways, wrapping between rows | Finds the shift and moves the blocks back |
| End of the file overwritten or wiped (zeros) | Crops the image just above the lost part (or fills it with grey) |
| Undamaged photo | Detected and copied byte-for-byte, untouched |

Your original files are never modified. Each output comes with a debug image showing exactly
which pixels were filled in.

---

## Contents

- [Requirements](#requirements)
- [Setup on Windows](#setup-on-windows)
- [Setup on WSL (Ubuntu on Windows)](#setup-on-wsl-ubuntu-on-windows)
- [Setup on Linux](#setup-on-linux)
- [Using the app](#using-the-app)
- [Command line](#command-line)
- [Where files go](#where-files-go)
- [Reading the results](#reading-the-results)
- [How it works](#how-it-works)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## Requirements

| | |
|---|---|
| Operating system | Windows 10/11 (natively or in WSL 2), or Linux. macOS should work but is untested. |
| Python | 3.10, 3.11 or 3.12 (3.12 recommended) |
| Memory | about 1.6 GB free per parallel worker for 13-megapixel photos (less for smaller photos); the number of workers is chosen automatically |
| Disk | about 3× the size of the photos you process (copies, outputs, report) |
| Photos | baseline JPEGs, which covers nearly every camera and phone photo (progressive JPEGs are reported as unsupported) |

Pick **one** of the setups below. Native Windows is simplest if you don't already use WSL.

---

## Setup on Windows

All commands below are for **PowerShell** (Start menu → "PowerShell").

### 1. Install Python and Git

Install them from the websites or with `winget`:

```powershell
winget install --id Python.Python.3.12 -e
```

```powershell
winget install --id Git.Git -e
```

If you use the python.org installer instead, tick **"Add python.exe to PATH"**. Close and
reopen PowerShell afterwards, then check:

```powershell
py -3.12 --version
```

### 2. Download the code

```powershell
cd $HOME
```

```powershell
git clone https://github.com/Shyam-prakash/Image_recovery_application.git
```

```powershell
cd Image_recovery_application
```

(No Git? On the GitHub page use **Code → Download ZIP**, extract it, and `cd` into the folder.)

### 3. Create a virtual environment and install

```powershell
py -3.12 -m venv .venv
```

```powershell
.\.venv\Scripts\Activate.ps1
```

```powershell
python -m pip install --upgrade pip
```

```powershell
pip install -e ".[ui]"
```

If `Activate.ps1` is blocked with "running scripts is disabled", allow local scripts for your
user once, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 4. Start the app

```powershell
photo-restore ui
```

Then open **http://localhost:8501** in your browser. Press `Ctrl+C` in PowerShell to stop it.

**Next time:** open PowerShell, `cd $HOME\Image_recovery_application`, run
`.\.venv\Scripts\Activate.ps1`, then `photo-restore ui`.

**Shortcut:** `.\run.ps1 -Ui` does steps 3 and 4 in one go: it creates the environment on the
first run and starts the UI.

Your photos, results and logs are kept in `C:\Users\<you>\photo-restore`.

---

## Setup on WSL (Ubuntu on Windows)

Use this if you already work in WSL, or prefer Linux tools. Your Windows browser and Explorer
work with it directly.

### 1. Install WSL (once)

In PowerShell **as Administrator**:

```powershell
wsl --install -d Ubuntu
```

Restart when asked, open **Ubuntu** from the Start menu and create a user name and password.

### 2. Install Python, venv and Git inside Ubuntu

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
```

Ubuntu 22.04 ships Python 3.10 and 24.04 ships 3.12; both are fine. Check with `python3 --version`.

### 3. Download the code into your Linux home folder

Keep the code and photos in the Linux file system (`~/...`), not under `/mnt/c/...`, which is
much slower from WSL.

```bash
cd ~ && git clone https://github.com/Shyam-prakash/Image_recovery_application.git
```

```bash
cd ~/Image_recovery_application
```

### 4. Create a virtual environment and install

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install --upgrade pip && pip install -e ".[ui]"
```

### 5. Start the app

```bash
photo-restore ui
```

Open **http://localhost:8501** in your **Windows** browser (WSL forwards it automatically).
Press `Ctrl+C` to stop.

**Next time:** `cd ~/Image_recovery_application && source .venv/bin/activate && photo-restore ui`,
or just `./run.sh --ui`.

In the app you can paste Windows paths such as `C:\Users\you\Downloads\photos.zip`; they are
converted automatically. Your photos, results and logs are kept in `~/photo-restore`, which
Windows Explorer can open as `\\wsl.localhost\Ubuntu\home\<you>\photo-restore`.

**Memory tip:** WSL gets only part of your RAM by default (half on recent versions). If large
batches run out of memory, give it more by creating `C:\Users\<you>\.wslconfig` with the two
lines below (use about three quarters of your RAM), then run `wsl --shutdown` in PowerShell:

```ini
[wsl2]
memory=6GB
```

---

## Setup on Linux

```bash
git clone https://github.com/Shyam-prakash/Image_recovery_application.git && cd Image_recovery_application
```

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[ui]"
```

```bash
photo-restore ui
```

---

## Using the app

The browser UI has four tabs:

1. **Import ZIPs**: paste the paths of your ZIP files, or of folders containing ZIPs, one per
   line. They are copied into the project folder and extracted (your originals are left alone).
   Tick *Also restore and build the report afterwards* to do everything in one click.
   For loose photos (no ZIP), copy them into `photo-restore\input\extracted\<any folder name>\`.
2. **Restore**: restores every photo that has no result yet. Options:
   - *Redo everything*: process all photos again;
   - *Keep full size*: grey instead of cropping an unrecoverable bottom part;
   - the number of parallel workers;
   - only some files, by name pattern.

   *Redo only the flagged photos* re-runs the ones marked for review. The job runs in the
   background with a progress bar, so you can close the browser tab and come back later.
   Buttons open the restored-photos folder and the HTML report.
3. **Results**: status counts and a thumbnail grid you can filter by status or name, showing
   the result, the filled areas, or the original. **Details** opens a before/after comparison
   with a download button.
4. **Single photo**: re-run one photo (or upload a new JPEG) with adjustable thresholds, and
   see the result straight away.

A static HTML report (`output/report/index.html`) is also written, if you prefer to browse
results without the app.

---

## Command line

Activate the virtual environment first (`.\.venv\Scripts\Activate.ps1` on Windows,
`source .venv/bin/activate` on WSL/Linux).

| Command | What it does |
|---|---|
| `photo-restore ui` | Start the browser UI (`--port 8502` for another port) |
| `photo-restore ingest --zip PATH [--zip PATH ...] [--zip-dir FOLDER]` | Copy ZIPs into the project folder and extract them |
| `photo-restore restore [--force] [--jobs N] [--only PATTERN] [--keep-full-size]` | Restore all imported photos; finished ones are skipped unless `--force` |
| `photo-restore report` | Build `report.csv`, `report.json` and `index.html` |
| `photo-restore all --zip PATH ...` | `ingest` + `restore` + `report` |
| `photo-restore inspect FILE --debug` | Restore one file verbosely, with a segment table and a full-size debug image |

Examples (Windows PowerShell; on WSL use the same paths, or `/mnt/c/...`):

```powershell
photo-restore all --zip "C:\Users\me\Downloads\photos-001.zip" --zip "C:\Users\me\Downloads\photos-002.zip"
```

```powershell
photo-restore restore --force --only "IMG_*"
```

The whole flow is also available as one script: `.\run.ps1 "C:\path\a.zip"` (Windows) or
`./run.sh "C:\path\a.zip"` (WSL/Linux).

Global options: `--root FOLDER` uses another project folder (or set the environment
variable `PHOTO_RESTORE_ROOT`), and `-v` gives verbose logging.

Advanced tuning: `--damage-threshold` (default 25), `--resync-accept` (8) and
`--trim-threshold` (18). The first two are minimums; each photo raises them according to how
detailed it is.

---

## Where files go

Everything lives in one project folder: `C:\Users\<you>\photo-restore` on Windows,
`~/photo-restore` on WSL/Linux. It is separate from the code.

```
photo-restore/
  input/zips/                 copies of your ZIP files
  input/extracted/<zip>/...   extracted originals (made read-only)
  output/restored/<zip>/...   restored photos: same folders and file names as the originals
  output/restored/_other/     non-JPEG files, copied as they are
  output/report/              index.html, report.csv, report.json, debug/ images, thumbnails
  logs/                       one log file per run
```

Restored photos are saved as high-quality JPEGs (quality 95, no chroma subsampling) with the
original EXIF data (date, camera, orientation) preserved.

---

## Reading the results

| Status | Meaning |
|---|---|
| `clean` | No damage found; the original was copied unchanged |
| `repaired` | Restored at full size; small areas were filled in |
| `repaired_cropped` | The end of the file was destroyed; the photo was cropped just above it |
| `needs_review` | Restored, but with low confidence. **Look at these first.** |
| `unsupported` | Not a baseline JPEG (e.g. progressive); copied unchanged |
| `failed` | An error occurred; the message says why |
| `other` | Not a JPEG; copied to `_other/` |

In the debug images and the app's "filled areas" view, **magenta** marks pixels that were
invented (interpolated) and **dark red** marks the unrecoverable part that was cropped. If a
`needs_review` photo still has a wrong band, cropping or retouching it in any photo editor is
usually the quickest fix.

`report.csv` lists one row per photo: status, original and output size, number of damage
points, the segments found, % filled, rows cropped, a confidence score (0–1) and processing time.

---

## How it works

1. **Tolerant decoder**: a JPEG decoder (Python + numba) that never stops at errors and keeps,
   for every 8×8 / 16×16 block group (MCU), its position in the bit stream, its brightness/colour
   level (DC) and its detail pixels.
2. **Clean check**: if the data ends exactly where it should with no decoding errors, nothing
   was lost or gained. The photo is either clean (copied unchanged) or has only a colour shift.
3. **Damage detection**: compares every block's top row with the row above it. A sudden
   mismatch, a decoding error or an impossible brightness marks a damage point. Thresholds adapt
   to how detailed each photo is.
4. **Resynchronisation**: after each damage point, tries every plausible start position and
   sideways shift. The one whose blocks fit the rows above best (with the colour level
   re-estimated) wins, but only if it keeps fitting over a longer stretch and moves forward
   through the image.
5. **Wrap-seam test**: a wrong sideways shift leaves a vertical seam down the whole picture;
   removing that seam fixes the shift.
6. **Lost data**: areas overwritten with unrelated data or zero bytes are detected and never
   drawn; at the end of a file they are cropped.
7. **Clean-up and fill**: garbage blocks at the edges of each repaired stretch are removed, and
   the small holes are filled by vertical interpolation (OpenCV inpainting only as a last
   resort). No AI or generative fill is used.

---

## Limitations

- Only baseline (sequential) JPEGs; progressive JPEGs are copied unchanged.
- Data that is really gone (overwritten or wiped) cannot be brought back: it is cropped or filled
  with grey, never invented.
- Very detailed, texture-like pictures (sketch filters, foliage) and photos with many damage
  points can come out with some misplaced bands; these are marked `needs_review`.
- Large photos take 2–30 seconds each, depending on size and damage.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `python` opens the Microsoft Store, or "Python was not found" | Install Python (see setup), then use `py -3.12` instead of `python` to create the venv |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then activate again |
| `photo-restore` is not recognised | Activate the virtual environment first (see setup) |
| http://localhost:8501 does not open from Windows when running in WSL | Make sure `photo-restore ui` is still running; try http://127.0.0.1:8501; restart WSL with `wsl --shutdown` |
| "Port 8501 is already in use" | `photo-restore ui --port 8502` |
| The very first photo is a little slower | The decoder is compiled on first use (a few seconds) and cached; later runs start immediately |
| WSL closes or freezes during a big batch | It ran out of memory: lower the workers (`--jobs 2`) or raise WSL's memory (see the WSL memory tip) |
| A photo is `unsupported` | It is progressive or otherwise non-baseline; the original is kept unchanged |

---

## Development

```bash
pip install -e ".[dev]"
```

```bash
python -m pytest
```

```bash
ruff check photo_restore tests
```

- The tests generate their own JPEGs (clean and synthetically damaged), so no personal photos
  are needed. `tests/test_samples.py` can additionally check real damaged photos of your own:
  put them in a folder with an `expected.json` (format in the test's docstring) and point
  `PHOTO_RESTORE_SAMPLES` at it. Without that folder those tests are skipped. Keep such photos
  out of the repository.
- GitHub Actions runs the linter and tests on Ubuntu and Windows (Python 3.10 and 3.12) for
  every push and pull request.
- Code layout:

  | Module | Role |
  |---|---|
  | `photo_restore/jpegscan.py` | Header parsing, tolerant Huffman decoder (numba), rendering, tail/wipe detection |
  | `photo_restore/restorer.py` | Damage detection, resync, wrap-seam test, garbage removal, trimming |
  | `photo_restore/pipeline.py` | One photo end to end: classify, restore, fill, save, debug image, result record |
  | `photo_restore/batch.py` | Parallel, resumable batch processing |
  | `photo_restore/report.py` | CSV/JSON/HTML report |
  | `photo_restore/ingest.py` | Safe ZIP copy and extraction |
  | `photo_restore/ui_app.py`, `jobs.py` | Streamlit UI and its background jobs |
  | `photo_restore/sysutil.py` | Windows / WSL / Linux differences |
  | `photo_restore/cli.py` | `photo-restore` command |

Contributions are welcome. Please open an issue first for larger changes, run the tests and
linter before sending a pull request, and **never attach or commit personal photos**. Describe
the damage instead, or share a synthetic example.

---

## License

[MIT](LICENSE) © 2026 Shyam-prakash
