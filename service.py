"""
Camera Auto Sync -- Windows Service
Listens for USB mounts via WMI, detects a camera by its DCIM folder,
and syncs photos to Google Drive through rclone.
"""

# pythonservice.exe loads bare Python without venv activation, so the venv
# site-packages won't be on sys.path. Insert them here before any third-party
# imports so pywin32, wmi, exifread, etc. are all found.
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
for _sp in (
    _os.path.join(_here, ".venv", "Lib", "site-packages"),
    _os.path.join(_here, ".venv", "Lib", "site-packages", "win32"),
    _os.path.join(_here, ".venv", "Lib", "site-packages", "win32", "lib"),
    _os.path.join(_here, ".venv", "Lib", "site-packages", "Pythonwin"),
):
    if _os.path.isdir(_sp) and _sp not in _sys.path:
        _sys.path.insert(0, _sp)
del _os, _sys, _here, _sp

import json
import os
import sys
import time
import sqlite3
import subprocess
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import exifread
import pythoncom
import servicemanager
import win32service
import win32serviceutil
import wmi
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

RCLONE_REMOTE      = os.getenv("RCLONE_REMOTE", "gdrive")
DRIVE_BASE_PATH    = os.getenv("DRIVE_BASE_PATH", "Photos/Camera")
DCIM_PATH_OVERRIDE = os.getenv("DCIM_PATH_OVERRIDE", "")
DB_PATH            = os.getenv("DB_PATH",      r"C:\ProgramData\CameraSync\sync.db")
LOG_PATH           = os.getenv("LOG_PATH",     r"C:\ProgramData\CameraSync\sync.log")
STATE_PATH         = os.getenv("STATE_PATH",   r"C:\ProgramData\CameraSync\state.json")
NOTIFY_PATH        = os.getenv("NOTIFY_PATH",  r"C:\ProgramData\CameraSync\notify.json")
RCLONE_EXE         = os.getenv("RCLONE_PATH",  "rclone")
RCLONE_CONF        = os.getenv("RCLONE_CONF",  "")

UPLOAD_MAX_RETRIES    = 3
UPLOAD_RETRY_DELAY    = 5     # seconds between per-file retry attempts
UPLOAD_WORKERS        = int(os.getenv("UPLOAD_WORKERS", "4"))
UPLOAD_MODE           = os.getenv("UPLOAD_MODE", "individual")  # "individual" | "batch"
BATCH_SIZE            = int(os.getenv("BATCH_SIZE", "10"))
PIPELINE_BACKOFF_INIT = 60    # seconds -- first retry delay after a pipeline exception
PIPELINE_BACKOFF_MAX  = 1800  # seconds -- cap at 30 minutes

log = logging.getLogger("CameraSync")


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    """Return an ASCII progress bar string: '[####------] 4/10 (40%)'."""
    filled = round(width * done / total) if total else 0
    bar    = "#" * filled + "-" * (width - filled)
    pct    = round(100 * done / total) if total else 0
    return f"[{bar}] {done}/{total} ({pct}%)"


def _fmt_speed(bps: float) -> str:
    """Format bytes-per-second as a human-readable speed: '4.2 MB/s'."""
    for unit in ("B", "KB", "MB", "GB"):
        if bps < 1024:
            return f"{bps:.1f} {unit}/s"
        bps /= 1024
    return f"{bps:.1f} TB/s"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class CameraFile:
    filename:   str   # bare filename, e.g. "IMG_1234.ARW"
    full_path:  str   # absolute local path on the mounted camera drive
    size_bytes: int
    date_taken: str   # "YYYY:MM:DD HH:MM:SS" -- EXIF or mtime fallback
    drive_path: str   # full rclone path, e.g. "gdrive:Photos/Camera/2026/05/RAW/IMG_1234.ARW"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class _PrettyFormatter(logging.Formatter):
    _LABELS = {
        logging.DEBUG:    "DBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARN",
        logging.ERROR:    "ERR!",
        logging.CRITICAL: "CRIT",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        label = self._LABELS.get(record.levelno, record.levelname[:4])
        msg   = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{ts}  {label}  {msg}"


def setup_logging() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(_PrettyFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------
def open_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.isolation_level = None  # autocommit
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            filename      TEXT    NOT NULL,
            size_bytes    INTEGER NOT NULL,
            date_taken    TEXT,
            drive_path    TEXT    NOT NULL,
            uploaded_at   TEXT    NOT NULL,
            camera_serial TEXT
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_drive_path ON uploads(drive_path)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed_uploads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            filename      TEXT    NOT NULL,
            full_path     TEXT    NOT NULL,
            size_bytes    INTEGER NOT NULL,
            date_taken    TEXT,
            drive_path    TEXT    NOT NULL,
            camera_serial TEXT,
            fail_count    INTEGER NOT NULL DEFAULT 1,
            last_attempt  TEXT    NOT NULL
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_failed_drive_path"
        " ON failed_uploads(drive_path)"
    )


# ---------------------------------------------------------------------------
# EXIF / date helpers
# ---------------------------------------------------------------------------
def _get_date_taken(path: str) -> str:
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        val = tags.get("EXIF DateTimeOriginal")
        if val:
            return str(val)
    except Exception:
        pass
    mtime = os.path.getmtime(path)
    return datetime.fromtimestamp(mtime).strftime("%Y:%m:%d %H:%M:%S")


def _parse_year_month(date_str: str) -> tuple[str, str]:
    try:
        parts = date_str.split(" ")[0].split(":")
        return parts[0], parts[1]
    except Exception:
        now = datetime.now()
        return str(now.year), f"{now.month:02d}"


def _file_type_folder(filename: str) -> str:
    """Return 'JPG' for .jpg/.jpeg files, 'RAW' for every other extension."""
    return "JPG" if os.path.splitext(filename)[1].upper() in (".JPG", ".JPEG") else "RAW"


# ---------------------------------------------------------------------------
# Camera detection helpers
# ---------------------------------------------------------------------------
def _has_dcim(drive_letter: str) -> bool:
    return os.path.isdir(os.path.join(drive_letter + "\\", "DCIM"))


def _get_dcim_root(drive_letter: str) -> str:
    if DCIM_PATH_OVERRIDE:
        return DCIM_PATH_OVERRIDE
    return os.path.join(drive_letter + "\\", "DCIM")


# ---------------------------------------------------------------------------
# Pause state
# ---------------------------------------------------------------------------
def is_sync_paused() -> bool:
    """Return True if the tray icon has toggled sync to paused."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return bool(json.load(f).get("paused", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# rclone command builder
# ---------------------------------------------------------------------------
def _rclone_cmd(*args: str) -> list[str]:
    cmd = [RCLONE_EXE]
    if RCLONE_CONF:
        cmd += ["--config", RCLONE_CONF]
    return cmd + list(args)


# ---------------------------------------------------------------------------
# Sync pipeline -- step 1: scan
# ---------------------------------------------------------------------------
def scan_camera_files(dcim_root: str) -> dict[str, CameraFile]:
    """
    Recursively find all .ARW, .JPG, and .JPEG files under dcim_root.
    Returns a dict keyed by drive_path.

    Drive path format:
        gdrive:Photos/Camera/YYYY/MM/RAW/IMG_1234.ARW
        gdrive:Photos/Camera/YYYY/MM/JPG/IMG_1234.JPG
    """
    results: dict[str, CameraFile] = {}
    for root, _, files in os.walk(dcim_root):
        for fname in files:
            if not fname.upper().endswith((".ARW", ".JPG", ".JPEG")):
                continue
            full = os.path.join(root, fname)
            try:
                size = os.path.getsize(full)
            except OSError:
                log.warning("Cannot stat %s -- skipping", full)
                continue
            date_str    = _get_date_taken(full)
            yyyy, mm    = _parse_year_month(date_str)
            type_folder = _file_type_folder(fname)
            dest_dir    = f"{DRIVE_BASE_PATH}/{yyyy}/{mm}/{type_folder}"
            drive_path  = f"{RCLONE_REMOTE}:{dest_dir}/{fname}"
            results[drive_path] = CameraFile(
                filename=fname,
                full_path=full,
                size_bytes=size,
                date_taken=date_str,
                drive_path=drive_path,
            )
    n_raw = sum(1 for cf in results.values() if _file_type_folder(cf.filename) == "RAW")
    n_jpg = len(results) - n_raw
    log.info(
        "Scan complete: %d file(s) in %s  [%d RAW, %d JPG]",
        len(results), dcim_root, n_raw, n_jpg,
    )
    return results


# ---------------------------------------------------------------------------
# Sync pipeline -- step 2: upload
# ---------------------------------------------------------------------------
def _is_size_stable(path: str) -> bool:
    """Return True if the file size hasn't changed over 2 seconds (not still writing)."""
    try:
        s1 = os.path.getsize(path)
        time.sleep(2)
        s2 = os.path.getsize(path)
        return s1 == s2
    except OSError:
        return False


def _rclone_copy(cf: CameraFile) -> bool:
    """
    Upload one file to its destination folder on Drive.
    Raises FileNotFoundError if the rclone binary is missing (systemic failure --
    the caller's backoff loop will retry the whole pipeline).
    Returns False for per-file errors (non-zero exit, timeout).
    """
    dest_dir = cf.drive_path.rsplit("/", 1)[0]
    cmd = _rclone_cmd("copy", cf.full_path, dest_dir, "--no-traverse", "--progress")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise  # rclone binary missing -- bubble up to trigger pipeline backoff
    except subprocess.TimeoutExpired:
        log.error("[TIMEOUT] rclone did not finish within 300s for %s", cf.filename)
        return False
    if result.returncode != 0:
        stderr = result.stderr.strip()
        log.error("[FAILED]  rclone exited %d for %s: %s",
                  result.returncode, cf.filename, stderr)
        return False
    return True


def _insert_upload(conn: sqlite3.Connection, cf: CameraFile, camera_serial: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO uploads
           (filename, size_bytes, date_taken, drive_path, uploaded_at, camera_serial)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cf.filename, cf.size_bytes, cf.date_taken, cf.drive_path,
         datetime.now(timezone.utc).isoformat(), camera_serial),
    )


def _insert_failed_upload(
    conn: sqlite3.Connection, cf: CameraFile, camera_serial: str
) -> None:
    conn.execute(
        """INSERT INTO failed_uploads
               (filename, full_path, size_bytes, date_taken, drive_path,
                camera_serial, fail_count, last_attempt)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(drive_path) DO UPDATE SET
               fail_count   = fail_count + 1,
               last_attempt = excluded.last_attempt""",
        (cf.filename, cf.full_path, cf.size_bytes, cf.date_taken, cf.drive_path,
         camera_serial, datetime.now(timezone.utc).isoformat()),
    )


def _upload_one(
    cf: CameraFile, db_conn: sqlite3.Connection, camera_serial: str
) -> bool:
    """
    Upload a single file with up to UPLOAD_MAX_RETRIES attempts.
    On success: records in uploads, removes from failed_uploads if present.
    On exhausted retries: records in failed_uploads, returns False.
    """
    size_str = _fmt_size(cf.size_bytes)
    dest_dir = cf.drive_path.rsplit("/", 1)[0].split(":", 1)[-1]  # strip remote prefix
    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        log.info(
            "[%d/%d] Uploading %s (%s) -> %s",
            attempt, UPLOAD_MAX_RETRIES, cf.filename, size_str, dest_dir,
        )
        if _rclone_copy(cf):
            _insert_upload(db_conn, cf, camera_serial)
            db_conn.execute(
                "DELETE FROM failed_uploads WHERE drive_path = ?", (cf.drive_path,)
            )
            log.info("[OK]      %s (%s) uploaded successfully", cf.filename, size_str)
            return True
        if attempt < UPLOAD_MAX_RETRIES:
            log.warning(
                "[RETRY]   Attempt %d/%d failed for %s -- waiting %ds",
                attempt, UPLOAD_MAX_RETRIES, cf.filename, UPLOAD_RETRY_DELAY,
            )
            time.sleep(UPLOAD_RETRY_DELAY)
        else:
            log.error(
                "[GIVE UP] All %d attempts failed for %s -- added to failed_uploads",
                UPLOAD_MAX_RETRIES, cf.filename,
            )
    _insert_failed_upload(db_conn, cf, camera_serial)
    return False


def upload_new_files(
    camera_files: dict[str, CameraFile],
    db_conn: sqlite3.Connection,
    camera_serial: str,
) -> tuple[bool, int]:
    """
    Upload files not yet in the uploads index, then retry any in failed_uploads
    that are still present on the camera. Returns (all_ok, n_uploaded).
    Uses a thread pool (UPLOAD_WORKERS) for parallel uploads; each worker
    opens its own SQLite connection (WAL mode allows concurrent writers).
    Deletions are skipped for this session if all_ok is False.
    """
    existing     = {row[0] for row in db_conn.execute("SELECT drive_path FROM uploads")}
    failed_paths = {row[0] for row in db_conn.execute("SELECT drive_path FROM failed_uploads")}

    new_files = [
        cf for dp, cf in camera_files.items()
        if dp not in existing and dp not in failed_paths
    ]
    to_retry = [cf for dp, cf in camera_files.items() if dp in failed_paths]

    n_synced = len(camera_files) - len(new_files) - len(to_retry)
    log.info(
        "Upload plan: %d already synced | %d new | %d retry  [%d workers]",
        n_synced, len(new_files), len(to_retry), UPLOAD_WORKERS,
    )

    queue    = new_files + to_retry
    total    = len(queue)
    uploaded = 0
    all_ok   = True

    # Shared cancellation flag: set when pause is detected so queued workers
    # exit immediately without touching the network or the DB.
    pause_flag = threading.Event()

    def _worker(idx: int, cf: CameraFile) -> int:
        """Return 1=uploaded, 0=skipped, -1=failed."""
        if pause_flag.is_set() or is_sync_paused():
            pause_flag.set()
            return 0
        log.info("--- File %d/%d  [%s] ---", idx, total, cf.filename)
        if not _is_size_stable(cf.full_path):
            log.warning("[SKIP]    %s is still being written -- skipping", cf.filename)
            return 0
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.isolation_level = None
        try:
            return 1 if _upload_one(cf, conn, camera_serial) else -1
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="Upload") as pool:
        futures = {pool.submit(_worker, i, cf): cf for i, cf in enumerate(queue, 1)}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                log.error("Upload worker raised unexpectedly: %s", exc)
                all_ok = False
                continue
            if pause_flag.is_set():
                # Drain remaining without acting on results
                continue
            if result == 1:
                uploaded += 1
            elif result == -1:
                all_ok = False
            # result == 0: skipped, no action

    if pause_flag.is_set():
        log.info("[PAUSED]  Upload stopped -- %d/%d file(s) uploaded before pause", uploaded, total)
        return False, uploaded

    return all_ok, uploaded


# ---------------------------------------------------------------------------
# Sync pipeline -- step 2 (alt): batch upload
# ---------------------------------------------------------------------------
def _is_batch_stable(files: list) -> list:
    """
    Stability-check a whole batch in a single 2-second window instead of
    sleeping 2 s per file. Returns only the files whose size didn't change.
    """
    sizes_before: dict[str, int | None] = {}
    for cf in files:
        try:
            sizes_before[cf.drive_path] = os.path.getsize(cf.full_path)
        except OSError:
            sizes_before[cf.drive_path] = None
    time.sleep(2)
    stable = []
    for cf in files:
        try:
            size_after = os.path.getsize(cf.full_path)
        except OSError:
            log.warning("[SKIP]    %s not accessible -- skipping", cf.filename)
            continue
        if sizes_before.get(cf.drive_path) == size_after:
            stable.append(cf)
        else:
            log.warning("[SKIP]    %s is still being written -- skipping", cf.filename)
    return stable


def _rclone_copy_batch(files: list, dest_dir: str) -> bool:
    """
    Upload a batch of files that share the same local source directory to a
    single Drive destination in one rclone invocation using --files-from.
    All files must reside in the same parent directory.
    Returns True on rclone exit 0, False on any error.
    Raises FileNotFoundError if the rclone binary is missing (triggers backoff).
    """
    import tempfile

    source_dir = os.path.dirname(files[0].full_path)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("\n".join(cf.filename for cf in files) + "\n")
            tmp_path = tmp.name
    except OSError as exc:
        log.error("[BATCH]   Could not write temp file-list: %s", exc)
        return False

    try:
        timeout = max(300, 120 * len(files))
        cmd = _rclone_cmd(
            "copy", source_dir, dest_dir,
            "--files-from", tmp_path,
            "--no-traverse", "--progress",
        )
        log.info(
            "[BATCH]   rclone copy %d file(s) -> %s",
            len(files), dest_dir,
        )
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise
        except subprocess.TimeoutExpired:
            log.error(
                "[BATCH]   rclone did not finish within %ds for -> %s", timeout, dest_dir
            )
            return False
        if result.returncode != 0:
            log.error(
                "[BATCH]   rclone exited %d for -> %s: %s",
                result.returncode, dest_dir, result.stderr.strip(),
            )
            return False
        return True
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def upload_new_files_batch(
    camera_files: dict[str, CameraFile],
    db_conn: sqlite3.Connection,
    camera_serial: str,
) -> tuple[bool, int]:
    """
    Batch upload variant of upload_new_files(). Groups files by
    (local source directory, Drive destination directory) and uploads each
    group in chunks of BATCH_SIZE with a single rclone call per chunk.
    Returns (all_ok, n_uploaded).

    Controlled by env vars:
        UPLOAD_MODE=batch   -- selects this path in run_sync_pipeline()
        BATCH_SIZE=<n>      -- files per rclone invocation (default 10)
    """
    from collections import defaultdict

    existing     = {row[0] for row in db_conn.execute("SELECT drive_path FROM uploads")}
    failed_paths = {row[0] for row in db_conn.execute("SELECT drive_path FROM failed_uploads")}

    new_files = [
        cf for dp, cf in camera_files.items()
        if dp not in existing and dp not in failed_paths
    ]
    to_retry = [cf for dp, cf in camera_files.items() if dp in failed_paths]

    n_synced = len(camera_files) - len(new_files) - len(to_retry)
    log.info(
        "Upload plan (batch): %d already synced | %d new | %d retry  [batch_size=%d]",
        n_synced, len(new_files), len(to_retry), BATCH_SIZE,
    )

    queue = new_files + to_retry
    total = len(queue)
    if not total:
        return True, 0

    # Group by (local source dir, Drive dest dir) so each rclone call copies
    # from exactly one folder to exactly one folder via --files-from.
    groups: dict[tuple[str, str], list[CameraFile]] = defaultdict(list)
    for cf in queue:
        key = (os.path.dirname(cf.full_path), cf.drive_path.rsplit("/", 1)[0])
        groups[key].append(cf)

    # Split each group into BATCH_SIZE chunks
    batches: list[tuple[str, str, list[CameraFile]]] = []
    for (source_dir, dest_dir), files in groups.items():
        for i in range(0, len(files), BATCH_SIZE):
            batches.append((source_dir, dest_dir, files[i : i + BATCH_SIZE]))

    n_batches = len(batches)
    log.info(
        "Batch upload starting -- %d batch(es) | %d file(s) | %d destination folder(s)",
        n_batches, total, len(groups),
    )
    log.info("  %s", _progress_bar(0, n_batches))

    uploaded       = 0
    bytes_uploaded = 0
    all_ok         = True
    pause_flag     = threading.Event()
    start_time     = time.time()

    for batch_idx, (source_dir, dest_dir, batch_files) in enumerate(batches, 1):
        if pause_flag.is_set() or is_sync_paused():
            pause_flag.set()
            break

        log.info(
            "--- Batch %d/%d  [%d file(s)  %s -> %s] ---",
            batch_idx, n_batches, len(batch_files), source_dir, dest_dir,
        )

        stable = _is_batch_stable(batch_files)
        if not stable:
            log.warning("[BATCH]   All files in this batch still writing -- skipping")
            continue

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.isolation_level = None
        try:
            batch_ok = False
            for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
                if _rclone_copy_batch(stable, dest_dir):
                    for cf in stable:
                        _insert_upload(conn, cf, camera_serial)
                        conn.execute(
                            "DELETE FROM failed_uploads WHERE drive_path = ?",
                            (cf.drive_path,),
                        )
                        log.info(
                            "[OK]      %s (%s) uploaded", cf.filename, _fmt_size(cf.size_bytes)
                        )
                        uploaded       += 1
                        bytes_uploaded += cf.size_bytes
                    batch_ok = True
                    break
                if attempt < UPLOAD_MAX_RETRIES:
                    log.warning(
                        "[RETRY]   Batch attempt %d/%d failed for %d file(s) -> %s -- waiting %ds",
                        attempt, UPLOAD_MAX_RETRIES, len(stable), dest_dir, UPLOAD_RETRY_DELAY,
                    )
                    time.sleep(UPLOAD_RETRY_DELAY)

            if not batch_ok:
                log.warning(
                    "[FALLBACK] Batch failed after %d attempts -> retrying %d file(s) individually",
                    UPLOAD_MAX_RETRIES, len(stable),
                )
                for cf in stable:
                    if not _upload_one(cf, conn, camera_serial):
                        all_ok = False
        finally:
            conn.close()

        elapsed = time.time() - start_time
        speed   = bytes_uploaded / elapsed if elapsed > 0 else 0
        log.info(
            "  %s | %d file(s) | %s | avg %s",
            _progress_bar(batch_idx, n_batches), uploaded, _fmt_size(bytes_uploaded), _fmt_speed(speed),
        )

    if pause_flag.is_set():
        log.info(
            "[PAUSED]  Upload stopped -- %d/%d file(s) uploaded before pause",
            uploaded, total,
        )
        return False, uploaded

    return all_ok, uploaded


# ---------------------------------------------------------------------------
# Sync pipeline -- step 3: delete
# ---------------------------------------------------------------------------
def _rclone_deletefile(drive_path: str) -> bool:
    cmd = _rclone_cmd("deletefile", drive_path, "--drive-use-trash=false")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise
    if result.returncode != 0:
        log.error("rclone deletefile failed for %s: %s", drive_path, result.stderr.strip())
        return False
    return True


def delete_removed_files(
    camera_files: dict[str, CameraFile],
    db_conn: sqlite3.Connection,
) -> int:
    """
    Delete from Drive files that are in the uploads index but absent from the
    current camera scan. Records are kept on rclone failure so deletion is
    retried next session.
    """
    current_paths = set(camera_files.keys())
    in_db = [row[0] for row in db_conn.execute("SELECT drive_path FROM uploads")]
    to_delete = [dp for dp in in_db if dp not in current_paths]
    log.info("%d file(s) to delete from Drive", len(to_delete))

    deleted = 0
    for drive_path in to_delete:
        if drive_path in current_paths:
            log.warning("[SKIP]    Deletion skipped -- file reappeared in scan: %s", drive_path)
            continue
        fname = drive_path.rsplit("/", 1)[-1]
        log.info("[DELETE]  %s (removed from camera)", fname)
        if _rclone_deletefile(drive_path):
            db_conn.execute("DELETE FROM uploads WHERE drive_path = ?", (drive_path,))
            deleted += 1
            log.info("[OK]      Deleted from Drive: %s", drive_path)
        else:
            log.error("[FAILED]  Could not delete %s -- will retry next session", drive_path)
    return deleted


# ---------------------------------------------------------------------------
# Sync pipeline -- step 4: notify
# ---------------------------------------------------------------------------
def _try_burntoast(title: str, message: str) -> bool:
    """Send a toast via PowerShell BurntToast module (if installed)."""
    # Escape single quotes for PowerShell string literals
    t = title.replace("'", "''")
    m = message.replace("'", "''")
    ps = (
        f"Import-Module BurntToast -ErrorAction Stop; "
        f"New-BurntToastNotification -Text '{t}', '{m}' -AppId 'CameraSync'"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _try_winotify(title: str, message: str) -> bool:
    """Send a toast via winotify (works from any session on Windows 10+)."""
    try:
        from winotify import Notification, audio  # type: ignore
        toast = Notification(
            app_id="Camera Sync",
            title=title,
            msg=message,
            duration="short",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        return True
    except Exception:
        return False


def _queue_notification(title: str, message: str) -> None:
    """
    Write notification to a JSON file for the tray icon process to pick up.
    Used as a guaranteed fallback when direct toast methods are unavailable.
    """
    try:
        os.makedirs(os.path.dirname(NOTIFY_PATH), exist_ok=True)
        tmp = NOTIFY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"title": title, "message": message,
                 "ts": datetime.now(timezone.utc).isoformat()}, f
            )
        os.replace(tmp, NOTIFY_PATH)
    except Exception as exc:
        log.warning("Failed to queue notification: %s", exc)


def send_notification(upload_ok: bool, uploaded: int, deleted: int) -> None:
    if upload_ok:
        title   = "Camera Sync -- Done"
        message = f"Sync complete: {uploaded} uploaded, {deleted} deleted"
    else:
        title   = "Camera Sync -- Error"
        message = "Some uploads failed -- check the log for details"

    # Try live delivery first; fall back to queue file for the tray to display.
    if not _try_burntoast(title, message):
        if not _try_winotify(title, message):
            log.info("Direct toast unavailable -- queuing notification for tray")
            _queue_notification(title, message)


# ---------------------------------------------------------------------------
# Top-level pipeline orchestrator
# ---------------------------------------------------------------------------
def run_sync_pipeline(
    drive_letter: str,
    camera_serial: str,
    db_conn: sqlite3.Connection,
) -> None:
    """
    Full sync pass for a single camera connection.
    Raises on systemic errors (rclone missing, scan I/O failure) so the
    WMI listener's backoff loop can retry the whole pipeline.
    """
    log.info("=" * 60)
    log.info("Sync started -- drive %s  serial %s", drive_letter, camera_serial)
    dcim_root = _get_dcim_root(drive_letter)
    log.info("DCIM root: %s", dcim_root)

    camera_files = scan_camera_files(dcim_root)  # OSError propagates -> backoff

    log.info("-" * 40)
    log.info("Phase: Upload  [mode=%s]", UPLOAD_MODE)
    if UPLOAD_MODE == "batch":
        upload_ok, uploaded_count = upload_new_files_batch(camera_files, db_conn, camera_serial)
    else:
        upload_ok, uploaded_count = upload_new_files(camera_files, db_conn, camera_serial)

    deleted_count = 0
    log.info("-" * 40)
    log.info("Phase: Delete")
    if upload_ok:
        deleted_count = delete_removed_files(camera_files, db_conn)
    else:
        log.warning("Some uploads failed -- deletions skipped this session")

    send_notification(upload_ok, uploaded_count, deleted_count)
    log.info("-" * 40)
    log.info(
        "Sync finished -- uploaded=%d  deleted=%d  success=%s",
        uploaded_count, deleted_count, upload_ok,
    )
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# WMI event helper
# ---------------------------------------------------------------------------
def _resolve_drive_letter(event) -> tuple[str, str] | None:
    drive_name = getattr(event, "DriveName", None)
    if not drive_name:
        return None
    letter = drive_name.rstrip("\\")  # "E:\\" -> "E:"
    try:
        c = wmi.WMI()
        disks = c.Win32_LogicalDisk(DeviceID=letter)
        serial = disks[0].VolumeSerialNumber if disks else "UNKNOWN"
    except Exception:
        serial = "UNKNOWN"
    return letter, serial


# ---------------------------------------------------------------------------
# WMI listener thread
# ---------------------------------------------------------------------------
class WMIListenerThread(threading.Thread):
    """
    Daemon thread that watches for USB volume-arrival WMI events and triggers
    the sync pipeline. Handles per-camera-event pipeline-level backoff and
    pause-state checks.
    """

    def __init__(
        self,
        stop_event: threading.Event,
        sync_lock:  threading.Lock,
        db_conn:    sqlite3.Connection,
    ) -> None:
        super().__init__(daemon=True, name="WMIListener")
        self._stop_event = stop_event
        self._sync_lock  = sync_lock
        self._db_conn    = db_conn

    def run(self) -> None:
        pythoncom.CoInitialize()
        try:
            self._listen()
        finally:
            pythoncom.CoUninitialize()

    def _listen(self) -> None:
        """Outer loop: (re)creates the WMI watcher, backing off on init failures."""
        while not self._stop_event.is_set():
            try:
                c = wmi.WMI()
                watcher = c.Win32_VolumeChangeEvent.watch_for(EventType=2)
                log.info("WMI listener ready -- watching for USB camera events")
                self._event_loop(watcher)
            except Exception as exc:
                log.error("WMI setup error: %s -- retrying in 30 s", exc)
                self._stop_event.wait(timeout=30)

    def _event_loop(self, watcher) -> None:
        """Inner loop: process events until an error forces a watcher rebuild."""
        while not self._stop_event.is_set():
            try:
                event = watcher(timeout_ms=5000)
            except wmi.x_wmi_timed_out:
                continue
            except Exception as exc:
                log.error("WMI watcher error: %s -- rebuilding watcher", exc)
                return

            result = _resolve_drive_letter(event)
            if result is None:
                continue
            drive_letter, camera_serial = result

            log.info("USB volume arrival -- drive %s  serial %s", drive_letter, camera_serial)

            if not _has_dcim(drive_letter):
                log.info("Drive %s has no DCIM folder -- not a camera, ignoring", drive_letter)
                continue

            log.info("Camera detected on %s (DCIM present)", drive_letter)

            if is_sync_paused():
                log.info(
                    "Sync is PAUSED -- camera event on %s ignored (resume via tray icon)",
                    drive_letter,
                )
                continue

            log.info("Sync is active -- starting pipeline for %s", drive_letter)

            if not self._sync_lock.acquire(blocking=False):
                log.warning(
                    "Sync already in progress -- ignoring arrival event for %s",
                    drive_letter,
                )
                continue
            try:
                self._run_with_backoff(drive_letter, camera_serial)
            finally:
                self._sync_lock.release()

    def _run_with_backoff(self, drive_letter: str, camera_serial: str) -> None:
        """
        Run the sync pipeline for one camera event, retrying with exponential
        backoff on unhandled exceptions (rclone missing, Drive unreachable, etc.).
        Backoff resets implicitly on the next successful run or new plug-in event.
        """
        backoff  = PIPELINE_BACKOFF_INIT
        attempt  = 0
        while not self._stop_event.is_set():
            attempt += 1
            if attempt > 1:
                log.info(
                    "[BACKOFF] Pipeline attempt %d for drive %s",
                    attempt, drive_letter,
                )
            try:
                run_sync_pipeline(drive_letter, camera_serial, self._db_conn)
                if attempt > 1:
                    log.info("[BACKOFF] Pipeline succeeded on attempt %d", attempt)
                return  # success -- exit the retry loop
            except Exception as exc:
                next_backoff = min(backoff * 2, PIPELINE_BACKOFF_MAX)
                log.error(
                    "[BACKOFF] Pipeline attempt %d failed: %s",
                    attempt, exc,
                )
                log.error(
                    "[BACKOFF] Retrying in %ds (next cap %ds, max %ds)",
                    backoff, next_backoff, PIPELINE_BACKOFF_MAX,
                )
                if self._stop_event.wait(timeout=backoff):
                    log.info("[BACKOFF] Service stopping -- abandoning retry for %s", drive_letter)
                    return  # service is stopping
                backoff = next_backoff


# ---------------------------------------------------------------------------
# Windows Service
# ---------------------------------------------------------------------------
class CameraSyncService(win32serviceutil.ServiceFramework):
    """
    Windows Service entry point registered as 'CameraSyncService'.

    Service control:
        python service.py install   -- register (sets startup=auto)
        net start CameraSyncService -- start
        net stop  CameraSyncService -- stop
        python service.py remove    -- unregister
    """

    _svc_name_         = "CameraSyncService"
    _svc_display_name_ = "Camera Auto Sync Service"
    _svc_description_  = "Syncs Sony ZV-E10 photos to Google Drive when connected via USB."

    def __init__(self, args):
        super().__init__(args)
        self._stop_event = threading.Event()
        self._sync_lock  = threading.Lock()
        self._wmi_thread: WMIListenerThread | None = None
        self._db_conn:    sqlite3.Connection | None = None

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        log.info("Stop requested -- signalling WMI thread")
        self._stop_event.set()
        if self._wmi_thread is not None:
            self._wmi_thread.join(timeout=10)
        if self._db_conn is not None:
            self._db_conn.close()
        log.info("Service stopped")

    def SvcDoRun(self) -> None:
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        setup_logging()
        log.info("CameraSync service starting")

        self._db_conn = open_db()
        init_db(self._db_conn)

        self._wmi_thread = WMIListenerThread(
            self._stop_event, self._sync_lock, self._db_conn
        )
        self._wmi_thread.start()

        self._stop_event.wait()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "install":
        sys.argv = [sys.argv[0], "--startup=auto", "install"]
    win32serviceutil.HandleCommandLine(CameraSyncService)
