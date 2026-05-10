# Camera Auto Sync

A Windows Service that automatically syncs photos from a Sony ZV-E10 camera to Google Drive whenever the camera is connected via USB. No terminal window, starts on boot, runs silently in the background.

A lightweight system tray icon (`tray.py`) runs in your desktop session and provides pause/resume control and toast notifications.

## How it works

1. The service listens for USB volume-arrival events using Windows WMI (`Win32_VolumeChangeEvent`).
2. When a drive mounts, it checks for a `DCIM` folder — the camera fingerprint.
3. It checks `state.json` — if the tray has paused sync, the event is ignored.
4. If not paused, it runs the sync pipeline:
   - **Scan** all `.ARW` and `.JPG`/`.JPEG` files on the camera.
   - **Upload** any files not yet in the local SQLite index to Google Drive via rclone, organised into `YYYY/MM/RAW/` and `YYYY/MM/JPG/` folders under `DRIVE_BASE_PATH`. Two upload modes are available — see [Upload modes](#upload-modes).
   - **Retry** failed files up to 3 times (5 s between attempts). Persistent failures are recorded in `failed_uploads` and retried on the next camera connection.
   - **Delete** from Drive any files that are in the index but no longer on the camera — only if all uploads succeeded.
   - **Notify** via a Windows toast with a summary (e.g. "5 uploaded, 0 deleted").
5. If the whole pipeline fails (rclone missing, Drive unreachable, etc.) it retries with exponential backoff — starting at 1 minute, doubling each attempt, capping at 30 minutes.

Files are never moved or deleted from the camera. The service only copies.

---

## Prerequisites

- **Windows 10/11**, 64-bit
- **Python 3.11+** (managed via `uv` or installed system-wide)
- **rclone** configured with a Google Drive remote — set `RCLONE_PATH` and `RCLONE_CONF` in `.env`
- **Administrator privileges** to register and control a Windows Service
- *(Optional)* **BurntToast** PowerShell module for richer toast notifications:
  ```powershell
  Install-Module BurntToast -Scope CurrentUser
  ```

---

## Setup

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
| `RCLONE_REMOTE` | Yes | — | rclone remote name (e.g. `gdrive`) |
| `DRIVE_BASE_PATH` | Yes | — | Base folder on Drive (e.g. `Photos/Camera`) |
| `RCLONE_PATH` | Yes | — | Full path to `rclone.exe` |
| `RCLONE_CONF` | Yes | — | Full path to `rclone.conf` |
| `DCIM_PATH_OVERRIDE` | No | *(auto-detect)* | Hard-code DCIM path if camera always mounts to the same letter |
| `DB_PATH` | No | `C:\ProgramData\CameraSync\sync.db` | SQLite database path |
| `LOG_PATH` | No | `C:\ProgramData\CameraSync\sync.log` | Rotating log file path |
| `STATE_PATH` | No | `C:\ProgramData\CameraSync\state.json` | Tray pause-state file |
| `NOTIFY_PATH` | No | `C:\ProgramData\CameraSync\notify.json` | Notification queue file |
| `UPLOAD_MODE` | No | `individual` | Upload strategy: `individual` or `batch` — see [Upload modes](#upload-modes) |
| `BATCH_SIZE` | No | `10` | Files per rclone call in `batch` mode |
| `UPLOAD_WORKERS` | No | `4` | Parallel upload threads in `individual` mode |

`C:\ProgramData\CameraSync\` is created automatically on first run.

### 4. Set the service environment variables

The service runs as SYSTEM in Session 0 and needs `PYTHONHOME` and `PYTHONPATH` pointing at your Python installation and venv. Run this once from an **elevated** PowerShell, substituting your actual paths:

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
2. Right-click -> New -> Shortcut.
3. Location: `.venv\Scripts\pythonw.exe` with argument `"C:\Users\<you>\camera_auto_sync\tray.py"`
   — or point to a `.bat` wrapper:
   ```bat
   @echo off
   "C:\Users\<you>\camera_auto_sync\.venv\Scripts\pythonw.exe" "C:\Users\<you>\camera_auto_sync\tray.py"
   ```
4. Name it **Camera Sync Tray** and click Finish.

The tray icon appears in the system tray. Right-click it to pause/resume sync or quit the tray process.

---

## Upload modes

Two upload strategies are available, switched via `UPLOAD_MODE` in `.env`.

### `individual` (default)

One `rclone copy` subprocess per file, parallelised across `UPLOAD_WORKERS` threads (default 4). Each file is retried independently up to 3 times before being recorded in `failed_uploads`.

Good for small card sessions or when you want maximum parallelism.

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

**Retry behaviour in batch mode:** if a batch fails all 3 attempts, the service automatically falls back to uploading each file in that batch individually. This ensures one bad file can't cause the rest of the batch to be marked as failed.

**Progress logging** after each batch:
```
  [############------------------] 12/48 (25%) | 290 file(s) | 6.9 GB | avg 14.2 MB/s
```

---

## Notifications

The service tries to deliver toasts in this order:

1. **BurntToast** -- PowerShell `New-BurntToastNotification` (richest, works from Session 0 via the Windows notification infrastructure). Requires `Install-Module BurntToast`.
2. **winotify** -- Python toast notification (Windows 10+).
3. **Queue file** -- writes `notify.json`; the tray icon polls every 5 seconds and displays the toast in the user session. This is the guaranteed fallback.

If neither BurntToast nor winotify is available (or the service can't reach the desktop), you will still get the notification as long as the tray is running.

---

## Verifying a sync

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
2026-05-10 14:07:45  INFO  [OK]      DSC00001.ARW (24.0 MB) uploaded
...
2026-05-10 14:07:46  INFO    [#-----------------------------] 1/30 (3%) | 25 file(s) | 600.0 MB | avg 46.1 MB/s
2026-05-10 14:07:46  INFO  --- Batch 2/30  [25 file(s)  D:\DCIM\100MSDCF -> gdrive:Photography/Prime/2026/05/JPG] ---
...
2026-05-10 14:12:10  INFO    [##############################] 30/30 (100%) | 675 file(s) | 15.8 GB | avg 52.3 MB/s
2026-05-10 14:12:10  INFO  Phase: Delete
2026-05-10 14:12:10  INFO  0 file(s) to delete from Drive
2026-05-10 14:12:10  INFO  Sync finished -- uploaded=675  deleted=0  success=True
```

---

## File organisation on Google Drive

Files are split by type within the same date tree:

```
Photos/Camera/
└── 2026/
    └── 05/
        ├── RAW/
        │   └── IMG_1234.ARW
        └── JPG/
            └── IMG_1234.JPG
```

Any extension that is not `.JPG` or `.JPEG` is placed in the `RAW/` subfolder.

---

## Service control

```powershell
net start CameraSyncService      # start
net stop  CameraSyncService      # stop
sc query  CameraSyncService      # status
Get-Content "C:\ProgramData\CameraSync\sync.log" -Wait   # live log

# Elevated -- re-register after code changes:
.venv\Scripts\python.exe service.py install
.venv\Scripts\python.exe service.py remove
```

---

## Uninstalling

```powershell
net stop CameraSyncService
.venv\Scripts\python.exe service.py remove
```

The `.env`, log, database, and state files are not removed -- delete `C:\ProgramData\CameraSync\` manually if desired. Also remove the Startup folder shortcut for the tray.

---

## Troubleshooting

### Toast notifications not appearing

If neither BurntToast nor winotify delivers the notification, check that `tray.py` is running -- it polls `notify.json` every 5 seconds and shows the toast in your session.

To install BurntToast:
```powershell
Install-Module BurntToast -Scope CurrentUser
```

### rclone not found

The SYSTEM account PATH is minimal. Always set `RCLONE_PATH` in `.env` to the full path to `rclone.exe`, e.g.:
```dotenv
RCLONE_PATH=C:\Users\moham\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_...\rclone.exe
```

If rclone is missing at runtime the pipeline will raise `FileNotFoundError` and the backoff loop will retry every 1 -> 2 -> 4 -> ... -> 30 minutes until the service is stopped or rclone becomes reachable.

### Upload failures

Files that fail all retries are recorded in the `failed_uploads` SQLite table and retried automatically on the next camera connection.

In **batch mode**, if a batch exhausts all retries it falls back to uploading each file in that batch individually, so a single problematic file cannot cause healthy files to be marked as failed.

To inspect failed uploads:
```powershell
.venv\Scripts\python.exe -c "import sqlite3; [print(r) for r in sqlite3.connect(r'C:\ProgramData\CameraSync\sync.db').execute('SELECT filename, fail_count, last_attempt FROM failed_uploads')]"
```

### WMI errors in the log

```powershell
sc query winmgmt   # must be RUNNING
net start winmgmt
```

### "Access is denied" when registering the service

Run the elevated PowerShell as Administrator (right-click -> Run as administrator).

### SQLite "database is locked"

Close any SQLite browser left open on `sync.db`.

---

## Database schema

```sql
CREATE TABLE uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL,
    date_taken    TEXT,
    drive_path    TEXT    NOT NULL UNIQUE,   -- e.g. "gdrive:Photos/Camera/2026/05/RAW/IMG_1234.ARW"
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

Only files whose `drive_path` is in `uploads` are candidates for deletion from Drive. Nothing else on your Drive is touched.
