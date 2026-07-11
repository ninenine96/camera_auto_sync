<div align="center">

# 📷 Camera Auto Sync

**Plug in your camera. Walk away. Done.**

A Windows Service that automatically syncs photos from a Sony ZV-E10 to Google Drive the moment the camera is connected via USB — no terminal, no clicks, no fuss.

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![Google Drive](https://img.shields.io/badge/Cloud-Google%20Drive-4285F4?style=for-the-badge&logo=googledrive&logoColor=white)](https://drive.google.com/)
[![rclone](https://img.shields.io/badge/Powered%20by-rclone-blue?style=for-the-badge&logo=rclone&logoColor=white)](https://rclone.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

</div>

---

## ✨ Features

| | |
|---|---|
| 🔌 **USB Auto-detect** | WMI event listener fires the moment your camera mounts |
| ☁️ **Google Drive sync** | Uploads via rclone into a tidy `YYYY/MM/RAW` & `YYYY/MM/JPG` tree |
| 🖼️ **Google Photos (optional)** | Route JPEGs to Google Photos month albums while RAW stays on Drive |
| ⚡ **Two upload modes** | `individual` (parallel workers) or `batch` (rclone `--files-from`) |
| 🔄 **Smart retry** | Files that fail retry up to 3× — persistent failures are queued for the next connection |
| 🗑️ **Mirror deletes** | Removes Drive copies of files deleted from the camera — only after a clean upload |
| 🛡️ **Read-only on camera** | Never moves or deletes anything from your SD card |
| 🔔 **Toast notifications** | BurntToast → winotify → tray fallback, in that order |
| 🕹️ **System tray icon** | Pause / resume sync from the taskbar without touching the service |
| 💾 **SQLite index** | Every upload is tracked; Drive is only touched for files in the index |
| 🔁 **Auto-start on boot** | Registered as a Windows Service with Automatic startup |

---

## 🔁 How it works

```
Camera plugged in via USB
        │
        ▼
  WMI detects volume mount
        │
        ▼
  DCIM folder found? ──No──► Ignore (not a camera)
        │ Yes
        ▼
  Sync paused? ──Yes──► Ignore (tray has paused sync)
        │ No
        ▼
  ┌─────────────────────────────────────┐
  │  1. Scan  — find .ARW / .JPG files  │
  │  2. Upload — skip files in index    │
  │  3. Retry  — up to 3× per file      │
  │  4. Delete — only if upload OK      │
  │  5. Notify — toast with summary     │
  └─────────────────────────────────────┘
        │
        ▼
  Failure? ──► Exponential backoff (1m → 2m → … → 30m)
```

Files are **never moved or deleted from the camera**. The service only copies.

---

## 🧰 Prerequisites

- **Windows 10/11**, 64-bit
- **Python 3.11+** (managed via `uv` or installed system-wide)
- **rclone** configured with a Google Drive remote — set `RCLONE_PATH` and `RCLONE_CONF` in `.env`
- **Administrator privileges** to register and control a Windows Service
- *(Optional)* **BurntToast** PowerShell module for richer toast notifications:
  ```powershell
  Install-Module BurntToast -Scope CurrentUser
  ```

---

## 🚀 Setup

### 1. Install Python dependencies

```powershell
cd camera_auto_sync
& "C:\Users\<you>\.local\bin\uv.exe" venv .venv --python 3.11
& "C:\Users\<you>\.local\bin\uv.exe" pip install -r requirements.txt --python .venv\Scripts\python.exe
.venv\Scripts\python.exe .venv\Scripts\pywin32_postinstall.py -install
```

Then copy the required DLLs so the service host (`pythonservice.exe`) can initialise Python correctly. Replace `<uvpython>` with your uv Python path (shown in `pyvenv.cfg`):

```powershell
$base = "C:\Users\<you>\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none"
Copy-Item "$base\python311.dll" ".venv\python311.dll"
Copy-Item ".venv\Lib\site-packages\win32\servicemanager.pyd" "$base\DLLs\servicemanager.pyd"
```

### 2. Configure rclone

```powershell
rclone config   # create a remote named 'gdrive'
rclone lsd gdrive:
```

### 3. Create `.env`

```powershell
copy .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `RCLONE_REMOTE` | ✅ | — | rclone remote name (e.g. `gdrive`) |
| `DRIVE_BASE_PATH` | ✅ | — | Base folder on Drive (e.g. `Photos/Camera`) |
| `RCLONE_PATH` | ✅ | — | Full path to `rclone.exe` |
| `RCLONE_CONF` | ✅ | — | Full path to `rclone.conf` |
| `GPHOTOS_REMOTE` | ➖ | *(off)* | rclone Google Photos remote name. When set, JPEGs go to Photos month albums instead of Drive |
| `GPHOTOS_ALBUM_PREFIX` | ➖ | `Camera` | Album name prefix — a JPEG from May 2026 lands in album `Camera 2026-05` |
| `DCIM_PATH_OVERRIDE` | ➖ | *(auto-detect)* | Hard-code DCIM path if camera always mounts to the same letter |
| `DB_PATH` | ➖ | `C:\ProgramData\CameraSync\sync.db` | SQLite database path |
| `LOG_PATH` | ➖ | `C:\ProgramData\CameraSync\sync.log` | Rotating log file path |
| `STATE_PATH` | ➖ | `C:\ProgramData\CameraSync\state.json` | Tray pause-state file |
| `NOTIFY_PATH` | ➖ | `C:\ProgramData\CameraSync\notify.json` | Notification queue file |
| `PROGRESS_PATH` | ➖ | `C:\ProgramData\CameraSync\progress.json` | Live sync progress published for the tray icon |
| `UPLOAD_MODE` | ➖ | `individual` | Upload strategy: `individual` or `batch` — see [Upload modes](#-upload-modes) |
| `BATCH_SIZE` | ➖ | `10` | Files per rclone call in `batch` mode |
| `UPLOAD_WORKERS` | ➖ | `4` | Parallel upload threads in `individual` mode |
| `LOG_LEVEL` | ➖ | `INFO` | Set `DEBUG` to log every uploaded file individually (`[OK]` lines) |

`C:\ProgramData\CameraSync\` is created automatically on first run.

### 4. Set the service environment variables

The service runs as SYSTEM in Session 0 and needs `PYTHONHOME` and `PYTHONPATH` pointing at your Python installation and venv. Run this once from an **elevated** PowerShell:

```powershell
$base = "C:\Users\<you>\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none"
$venv = "C:\Users\<you>\camera_auto_sync\.venv\Lib\site-packages"
reg add "HKLM\SYSTEM\CurrentControlSet\Services\CameraSyncService" /v Environment /t REG_MULTI_SZ /d "PYTHONHOME=$base`0PYTHONPATH=$venv;$venv\win32;$venv\win32\lib;$venv\Pythonwin" /f
```

### 5. Register and start the service

From an **elevated** PowerShell:

```powershell
.venv\Scripts\python.exe service.py install
net start CameraSyncService
sc query CameraSyncService
```

The service is configured for **Automatic** startup — it starts on every boot.

### 6. Set up the tray icon

The tray icon runs in your user session, not as part of the service. Start it manually or add it to your Startup folder so it launches on login.

**Start manually:**
```powershell
.venv\Scripts\pythonw.exe tray.py
```

**Autostart on login — Startup folder shortcut:**

1. Press `Win + R`, type `shell:startup`, press Enter.
2. Right-click → New → Shortcut.
3. Location: `.venv\Scripts\pythonw.exe` with argument `"C:\Users\<you>\camera_auto_sync\tray.py"`
   — or point to a `.bat` wrapper:
   ```bat
   @echo off
   "C:\Users\<you>\camera_auto_sync\.venv\Scripts\pythonw.exe" "C:\Users\<you>\camera_auto_sync\tray.py"
   ```
4. Name it **Camera Sync Tray** and click Finish.

The icon shows sync state at a glance:

| Icon | Meaning |
|---|---|
| Teal camera | Idle — waiting for a camera |
| Grey camera filling up with blue | Sync in progress — fill level = % uploaded, like a battery indicator |
| Grey camera, yellow pause bars | Paused |
| Red camera, white `!` | Sync failing — service is retrying with backoff |

Hover the icon (or open the menu — the first line is a live status) for details while syncing, e.g.:

```
Syncing 100/261 RAW, 90/243 JPG — 46%, 42.3 MB/s, ETA 4m 20s
```

When idle, the tooltip shows the last sync result. The service publishes progress to `progress.json` after every batch (`batch` mode) or file (`individual` mode); the tray polls it once a second. Right-click the icon to pause/resume sync, open the log, or quit.

When Google Photos routing is enabled, JPEG batches upload **before** RAW batches so shareable photos land in Photos first.

Resuming from pause (and service startup) checks for an already-attached camera and syncs it immediately — no need to replug.

---

## ⚡ Upload modes

Two upload strategies are available, switched via `UPLOAD_MODE` in `.env`.

### `individual` (default)

One `rclone copy` subprocess per file, parallelised across `UPLOAD_WORKERS` threads (default 4). Each file is retried independently up to 3 times before being recorded in `failed_uploads`.

Best for small card sessions or when you want maximum parallelism.

```dotenv
UPLOAD_MODE=individual
UPLOAD_WORKERS=4
```

### `batch`

Files are grouped by destination folder (same year/month/type) and uploaded in chunks of `BATCH_SIZE` using a single `rclone copy --files-from` call per chunk. Significantly reduces subprocess and API overhead for large sessions.

```dotenv
UPLOAD_MODE=batch
BATCH_SIZE=25
```

> **Retry fallback:** if a batch fails all 3 attempts, the service automatically falls back to uploading each file in that batch individually — one bad file can't mark a healthy batch as failed.

**Progress logging** after each batch:
```
  [############------------------] 12/48 (25%) | 290 file(s) | 6.9 GB | avg 14.2 MB/s
```

---

## 🔔 Notifications

The service tries to deliver toasts in this order:

| Priority | Method | Notes |
|---|---|---|
| 1 | **BurntToast** | PowerShell `New-BurntToastNotification` — richest, works from Session 0 |
| 2 | **winotify** | Python toast (Windows 10+) |
| 3 | **Queue file** | Writes `notify.json`; tray polls every 5 s and shows the toast in your session |

As long as the tray is running you'll always get the notification.

---

## ✅ Verifying a sync

1. Connect your camera via USB.
2. Tail the log:

```powershell
Get-Content "C:\ProgramData\CameraSync\sync.log" -Wait
```

Example output (batch mode):
```
2026-05-10 14:07:22  INFO  WMI listener ready -- watching for USB camera events
2026-05-10 14:07:31  INFO  ============================================================
2026-05-10 14:07:31  INFO  Sync started -- drive D:  serial 4A7B2C1D
2026-05-10 14:07:31  INFO  DCIM root: D:\DCIM\100MSDCF
2026-05-10 14:07:32  INFO  Scan complete: 675 file(s) in D:\DCIM\100MSDCF  [338 RAW, 337 JPG]
2026-05-10 14:07:32  INFO  Phase: Upload  [mode=batch]
2026-05-10 14:07:32  INFO  Batch upload starting -- 30 batch(es) | 675 file(s) | 5 destination folder(s)
2026-05-10 14:07:32  INFO    [------------------------------] 0/30 (0%)
2026-05-10 14:07:32  INFO  --- Batch 1/30  [25 file(s)  D:\DCIM\100MSDCF -> gdrive:Photography/Prime/2026/05/RAW] ---
2026-05-10 14:07:46  INFO    [#-----------------------------] 1/30 (3%) | 25 file(s) | 600.0 MB | avg 46.1 MB/s
...
2026-05-10 14:12:10  INFO    [##############################] 30/30 (100%) | 675 file(s) | 15.8 GB | avg 52.3 MB/s
2026-05-10 14:12:10  INFO  Phase: Delete
2026-05-10 14:12:10  INFO  0 file(s) to delete from Drive
2026-05-10 14:12:10  INFO  ============================================================
2026-05-10 14:12:10  INFO  Sync complete  [4m 39s]
2026-05-10 14:12:10  INFO    Google Photos   ->   337 JPEGs      4.1 GB   OK
2026-05-10 14:12:10  INFO    Google Drive    ->   338 RAW       11.7 GB   OK
2026-05-10 14:12:10  INFO    Already synced  ->     0 files
2026-05-10 14:12:10  INFO    Deleted         ->     0
2026-05-10 14:12:10  INFO    Failed          ->     0
2026-05-10 14:12:10  INFO    Uploaded        ->  15.8 GB (avg 52.3 MB/s)
2026-05-10 14:12:10  INFO  ============================================================
```

Per-file `[OK]` lines are logged at `DEBUG` — the summary block replaces them. Set `LOG_LEVEL=DEBUG` in `.env` to get them back.

---

## 🗂️ File organisation on Google Drive

```
Photos/Camera/
└── 2026/
    └── 05/
        ├── RAW/
        │   └── DSC00001.ARW
        └── JPG/
            └── DSC00001.JPG
```

Any extension that isn't `.JPG` / `.JPEG` lands in the `RAW/` folder.

---

## 🖼️ Sending JPEGs to Google Photos

By default both RAW and JPEG go to Drive. To route JPEGs to **Google Photos**
instead (RAW still goes to Drive), add a Google Photos remote and point the
service at it.

### 1. Create the remote

```powershell
rclone config   # add a new remote, storage type "Google Photos", e.g. named 'gphotos'
rclone lsd gphotos:album
```

### 2. Enable it in `.env`

```dotenv
GPHOTOS_REMOTE=gphotos
GPHOTOS_ALBUM_PREFIX=Camera
```

JPEGs are now uploaded into a month album per capture date:

```
Google Photos
└── Albums
    ├── Camera 2026-05   ← DSC00001.JPG, DSC00002.JPG, …
    └── Camera 2026-06   ← …
```

RAW files continue to land on Drive under `Photos/Camera/YYYY/MM/RAW/`.

> **Heads-up — deletes don't mirror to Photos.** The Google Photos API cannot
> delete media, so a JPEG removed from the camera stays in Photos. Its index
> record is kept on purpose so the file is never re-uploaded (which would create
> a duplicate). Mirror-delete still works normally for RAW files on Drive.

---

## 🛠️ Service control

```powershell
net start CameraSyncService      # start
net stop  CameraSyncService      # stop
sc query  CameraSyncService      # status
Get-Content "C:\ProgramData\CameraSync\sync.log" -Wait   # live log

# Elevated — re-register after code changes:
.venv\Scripts\python.exe service.py install
.venv\Scripts\python.exe service.py remove
```

---

## 🗑️ Uninstalling

```powershell
net stop CameraSyncService
.venv\Scripts\python.exe service.py remove
```

The `.env`, log, database, and state files are **not** removed — delete `C:\ProgramData\CameraSync\` manually if desired. Also remove the Startup folder shortcut for the tray.

---

## 🔍 Troubleshooting

<details>
<summary><strong>Toast notifications not appearing</strong></summary>

Check that `tray.py` is running — it polls `notify.json` every 5 seconds and shows the toast in your session.

To install BurntToast:
```powershell
Install-Module BurntToast -Scope CurrentUser
```
</details>

<details>
<summary><strong>rclone not found</strong></summary>

The SYSTEM account PATH is minimal. Always set `RCLONE_PATH` in `.env` to the full path to `rclone.exe`:
```dotenv
RCLONE_PATH=C:\Users\<you>\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_...\rclone.exe
```

If rclone is missing at runtime the pipeline raises `FileNotFoundError` and the backoff loop retries every 1 → 2 → 4 → … → 30 minutes until the service is stopped or rclone becomes reachable.
</details>

<details>
<summary><strong>Upload failures</strong></summary>

Files that fail all retries are recorded in the `failed_uploads` SQLite table and retried automatically on the next camera connection.

In **batch mode**, if a batch exhausts all retries it falls back to per-file uploads so a single bad file can't mark healthy files as failed.

Inspect failed uploads:
```powershell
.venv\Scripts\python.exe -c "import sqlite3; [print(r) for r in sqlite3.connect(r'C:\ProgramData\CameraSync\sync.db').execute('SELECT filename, fail_count, last_attempt FROM failed_uploads')]"
```
</details>

<details>
<summary><strong>WMI errors in the log</strong></summary>

```powershell
sc query winmgmt   # must be RUNNING
net start winmgmt
```
</details>

<details>
<summary><strong>"Access is denied" when registering the service</strong></summary>

Run the PowerShell as Administrator (right-click → Run as administrator).
</details>

<details>
<summary><strong>SQLite "database is locked"</strong></summary>

Close any SQLite browser left open on `sync.db`.
</details>

---

## 🗃️ Database schema

```sql
CREATE TABLE uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL,
    date_taken    TEXT,
    drive_path    TEXT    NOT NULL UNIQUE,   -- e.g. "gdrive:Photos/Camera/2026/05/RAW/DSC00001.ARW"
    uploaded_at   TEXT    NOT NULL,          -- UTC ISO-8601
    camera_serial TEXT
);

CREATE TABLE failed_uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT    NOT NULL,
    full_path     TEXT    NOT NULL,          -- local path on camera at time of failure
    size_bytes    INTEGER NOT NULL,
    date_taken    TEXT,
    drive_path    TEXT    NOT NULL UNIQUE,
    camera_serial TEXT,
    fail_count    INTEGER NOT NULL DEFAULT 1,
    last_attempt  TEXT    NOT NULL           -- UTC ISO-8601
);
```

Only files whose `drive_path` is in `uploads` are candidates for deletion from Drive. Nothing else on your Drive is ever touched.
