# Folder Analyzer

A local Windows folder analyzer with a browser interface. Scan a folder,
drill into its subfolders, find large files, inspect file types, and export the
file inventory as CSV. No account, cloud service, or frontend build is required.

## Features

- Interactive donut chart and bar view with folder drill-down.
- Sortable folder explorer, largest-file search, and extension filters.
- File-type totals and a full file inventory exported as CSV.
- Background scanning with progress, cancellation, and partial results.
- Details for unreadable entries and skipped links or junctions.
- Confirmed removal of indexed subfolders to the Windows Recycle Bin, followed
  by an automatic rescan.
- Local HTTP server and browser interface with no external assets or services.

## Download the Windows app

**[Download FolderAnalyzer.exe](https://github.com/Gehrman-Sparrow42/folder-analyzer/releases/latest/download/FolderAnalyzer.exe)**

Or open the [latest release](https://github.com/Gehrman-Sparrow42/folder-analyzer/releases/latest)
for release notes and the `SHA256SUMS.txt` checksum file.

1. Download `FolderAnalyzer.exe` to any convenient folder.
2. Double-click it. The app opens in your default browser at
   <http://127.0.0.1:8520>.
3. Keep its console window open while using the app. Press Ctrl+C in that window
   to stop it. Closing only the browser does not stop the app.

The portable executable bundles Python, the browser assets, and the folder
chooser. **No Python installation, package installation, or administrator access
is required.** The release targets Windows 10/11 on x64 processors. It is
unsigned; Windows may display an unknown-publisher or SmartScreen notice. This
release has been tested locally, but not on a separate clean Windows machine.
The one-file executable extracts its runtime to a temporary folder at startup.

To use another port, run `FolderAnalyzer.exe --port 8521`. The `--no-browser`
option is also supported.

## Requirements for running from source

- Windows 10 or 11 for the full experience, including Recycle Bin operations.
- Python 3.13 (the version used for development and verification).
- A modern browser.
- Tkinter, included with the standard Windows Python installer, for the optional
  native folder chooser. Paths can also be entered manually.

The application uses only the Python standard library. No `pip install` or
frontend build is needed. Node.js is optional and used only to check JavaScript
syntax during development. Scanning may work on other platforms, but the native
Recycle Bin implementation is Windows-only and other platforms have not been
verified.

## Quick start

Clone the repository and run the server:

```powershell
git clone https://github.com/Gehrman-Sparrow42/folder-analyzer.git
cd folder-analyzer
python server.py
```

The app opens <http://127.0.0.1:8520> automatically. Keep the console open while
using it; press Ctrl+C in that console to stop the server. If `python` is not
available on PATH, try `py -3.13 server.py`.

Choose a different port or suppress automatic browser opening:

```powershell
python server.py --port 8521 --no-browser
```

Only one instance can listen on a given port. If the port is occupied, use the
existing instance or select a different port.

For the original `C:\Tools` workspace installation, the existing launcher is
`C:\Tools\LAUNCHERS\Launch_Folder_Analyzer.bat`. It runs this project using the
shared interpreter at `C:\Tools\.venv\Scripts\python.exe`. That machine-specific
launcher is outside this repository; cloned copies use the commands above.

## Use

1. Paste a folder path or use **Browse** to choose a folder on this PC.
2. Start a scan. Progress shows discovered files, folders, and bytes. Cancel is
   available for long scans.
3. Explore folder sizes using the interactive donut chart and proportional bars,
   view the largest files, or select a file type to filter the inventory.
   Directory rows and folder chart entries open that directory in the analyzer.
   The donut shows the largest immediate items and groups the rest as **Other**;
   its shares include every indexed child, regardless of table pagination.
4. Download the CSV inventory for the current scan.
5. To remove a subfolder, use its **Move to Recycle Bin** action and confirm the
   full path. The app rescans the original root afterward to refresh all totals.

Folder removal is available after a scan completes or is cancelled. It moves the
selected folder and its contents to the Windows Recycle Bin; there is no permanent
deletion fallback. The scanned root itself cannot be removed from the app. If a
drive or folder cannot use the Recycle Bin, the app reports the failure. Restore
items using the Windows Recycle Bin if needed.

Scans measure **logical file size**, rather than allocated space on disk. Sparse,
compressed, and hard-linked files can therefore produce different totals from
Windows disk allocation figures. Hard links are counted at each discovered path.
The root folder is not included in its subfolder count. Hidden files are included
when readable. Links, junctions, and other reparse points are skipped so scans do
not leave the chosen tree or loop. Unreadable or disappearing entries are reported
in **Skipped items**; the scan continues where possible. Files can change while a
scan runs, so a scan is an inventory over time, not a filesystem snapshot.

Cancellation leaves a partial inventory available for inspection and export.
Starting a new scan replaces the previous inventory. Results are held in a
temporary SQLite index for the running process and are not retained as history.
For very large trees, the index needs free space in the Windows temporary folder.

The server listens only on loopback, validates the request host and origin, and
offers folder removal only through the scoped Recycle Bin action above. It does
not offer file editing or file-content download endpoints.
Do not expose it through a proxy or public tunnel.

## Development and verification

Run these commands from the repository root:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q scanner.py server.py tests
node --check web/app.js
```

The tests cover totals, filtering, export, cancellation, chart aggregation,
scoped folder-removal validation, replacement detection, and HTTP request
guards. Automated removal tests mock the native operation; a real Windows
Recycle Bin operation was separately checked using disposable test folders.
The symlink test skips if the current Windows session cannot create symlinks.

| File | Purpose |
| --- | --- |
| `scanner.py` | Filesystem traversal, temporary SQLite index, queries, CSV export, and Windows recycling |
| `server.py` | Loopback HTTP API, static files, folder picker, and startup |
| `web/` | HTML, CSS, and JavaScript interface |
| `tests/test_backend.py` | Standard-library behavioral tests |

The browser interface and API are served by the same process. Check
`/api/health` for a running server. Closing the browser does not stop the server.

## Build the Windows executable

Build on Windows x64 using Python 3.13:

```powershell
python -m pip install -r requirements-build.txt
.\build_windows.ps1
```

Use the intended Python environment on PATH when running the build script.
It creates `dist/FolderAnalyzer.exe` and `dist/SHA256SUMS.txt`. PyInstaller is a
build dependency only; it is not needed to run from source or use the executable.
The web assets and Tkinter runtime are included in the executable. Generated
build files and binaries are ignored by Git and distributed as release assets.
