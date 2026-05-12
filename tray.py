"""
Camera Auto Sync — System Tray Icon
Runs in the user's desktop session (not as part of the service).

Menu:
  Pause sync / Resume sync  — toggles state.json; service reads it before each run
  Quit                      — exits this process only, not the service

Also polls notify.json written by the service and surfaces toasts via win10toast.

Autostart: place a shortcut to this script (or a .bat that runs it) in
    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
"""

import json
import os
import subprocess
import sys
import threading
import time
import winreg

# ---------------------------------------------------------------------------
# Add venv site-packages so this script runs without activating the venv
# ---------------------------------------------------------------------------
_here = os.path.dirname(os.path.abspath(__file__))
for _sp in (
    os.path.join(_here, ".venv", "Lib", "site-packages"),
    os.path.join(_here, ".venv", "Lib", "site-packages", "win32"),
    os.path.join(_here, ".venv", "Lib", "site-packages", "win32", "lib"),
    os.path.join(_here, ".venv", "Lib", "site-packages", "Pythonwin"),
):
    if os.path.isdir(_sp) and _sp not in sys.path:
        sys.path.insert(0, _sp)
del _here, _sp

import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Register app ID so Windows shows banner notifications
# ---------------------------------------------------------------------------
def _register_app_id() -> None:
    """
    Write HKCU\\Software\\Classes\\AppUserModelId\\Camera Sync so Windows
    knows the app and allows it to show banner toast notifications.
    Without this entry Windows silently discards the toast popup.
    """
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\AppUserModelId\Camera Sync",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Camera Sync")
        winreg.CloseKey(key)
    except Exception:
        pass  # non-fatal — notifications fall back to Action Center only


# ---------------------------------------------------------------------------
# Paths — must match service.py / .env defaults
# ---------------------------------------------------------------------------
_DATA_DIR   = r"C:\ProgramData\CameraSync"
STATE_PATH  = os.path.join(_DATA_DIR, "state.json")
NOTIFY_PATH = os.path.join(_DATA_DIR, "notify.json")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def _read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"paused": False}


def _write_state(state: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def _is_paused() -> bool:
    return bool(_read_state().get("paused", False))


# ---------------------------------------------------------------------------
# Icon drawing
# ---------------------------------------------------------------------------
def _make_icon(paused: bool = False) -> Image.Image:
    """
    Draw a 64×64 camera icon.
    Teal (#009688) when active, grey (#646464) when paused.
    Two yellow pause bars are overlaid on the lens when paused.
    """
    size   = 64
    img    = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw   = ImageDraw.Draw(img)
    colour = (100, 100, 100, 255) if paused else (0, 150, 136, 255)

    # Camera body
    draw.rounded_rectangle([6, 18, 58, 54], radius=7, fill=colour)
    # Viewfinder bump
    draw.rectangle([22, 11, 38, 20], fill=colour)
    # Lens outer ring
    draw.ellipse([17, 24, 47, 48], fill=(220, 220, 220, 255))
    # Lens inner (dark)
    draw.ellipse([23, 30, 41, 42], fill=(40, 40, 40, 255))

    if paused:
        # Yellow pause bars over the lens
        draw.rectangle([25, 30, 30, 42], fill=(255, 200, 0, 255))
        draw.rectangle([34, 30, 39, 42], fill=(255, 200, 0, 255))

    return img


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------
_TRAY_LOG  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tray.log")
# PowerShell AUMID — always registered on every Windows 10/11 system
_PS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_PS_TOAST = """\
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null
$xml=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02')
$n=$xml.GetElementsByTagName('text')
$n.Item(0).AppendChild($xml.CreateTextNode('{T}'))|Out-Null
$n.Item(1).AppendChild($xml.CreateTextNode('{M}'))|Out-Null
$toast=[Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{A}').Show($toast)
"""


def _log_tray(msg: str) -> None:
    try:
        with open(_TRAY_LOG, "a", encoding="utf-8") as f:
            f.write(f"{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass


def _show_toast(title: str, msg: str) -> None:
    """Send a toast via PowerShell WinRT using the PowerShell AUMID (always registered)."""
    t = title.replace("'", "''")
    m = msg.replace("'", "''")
    script = _PS_TOAST.replace("{T}", t).replace("{M}", m).replace("{A}", _PS_APP_ID)
    try:
        r = subprocess.run(
            ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            _log_tray(f"Toast OK: {title}")
        else:
            _log_tray(f"Toast PS error ({r.returncode}): {r.stderr.strip()[:200]}")
    except Exception as exc:
        _log_tray(f"Toast exception: {exc}")


def _notify(title: str, msg: str) -> None:
    """Fire toast from a daemon thread — keeps it off pystray's message thread."""
    threading.Thread(
        target=_show_toast, args=(title, msg), daemon=True, name="ToastThread"
    ).start()


# ---------------------------------------------------------------------------
# Notification poller
# ---------------------------------------------------------------------------
def _notification_poller(stop_event: threading.Event) -> None:
    """Poll NOTIFY_PATH every 5 s and surface any pending toast."""
    while not stop_event.is_set():
        try:
            if os.path.exists(NOTIFY_PATH):
                with open(NOTIFY_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                os.remove(NOTIFY_PATH)
                _show_toast(
                    data.get("title", "Camera Sync"),
                    data.get("message", ""),
                )
        except Exception:
            pass
        stop_event.wait(timeout=5)


def _toggle_pause(icon: pystray.Icon, _item) -> None:
    try:
        state = _read_state()
        state["paused"] = not state.get("paused", False)
        _write_state(state)
        paused = state["paused"]
        icon.icon  = _make_icon(paused)
        icon.title = "Camera Sync (paused)" if paused else "Camera Sync"
        icon.update_menu()
        if paused:
            _notify("Camera Sync — Paused", "Plug-in events will be ignored until you resume.")
        else:
            _notify("Camera Sync — Resumed", "Sync will run on the next camera connection.")
    except Exception as exc:
        _log_tray(f"_toggle_pause error: {exc}")


def _quit(icon: pystray.Icon, _item) -> None:
    icon.stop()


def _pause_label(_item) -> str:
    return "Resume sync" if _is_paused() else "Pause sync"


def main() -> None:
    _register_app_id()
    stop_event = threading.Event()
    poller = threading.Thread(
        target=_notification_poller,
        args=(stop_event,),
        daemon=True,
        name="NotifyPoller",
    )
    poller.start()

    paused = _is_paused()
    icon = pystray.Icon(
        name="CameraSync",
        icon=_make_icon(paused),
        title="Camera Sync (paused)" if paused else "Camera Sync",
        menu=pystray.Menu(
            pystray.MenuItem(_pause_label, _toggle_pause, default=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit tray", _quit),
        ),
    )

    try:
        icon.run()
    finally:
        stop_event.set()
        poller.join(timeout=10)


if __name__ == "__main__":
    main()
