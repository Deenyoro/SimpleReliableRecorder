"""Alerting primitives: taskbar flashing, alert sounds, and an OS message box.

The animated gold banner inside the window lives in ui/widgets.py + ui/app.py;
this module provides the low-level Win32/audio bits used by both the GUI and the
separate watchdog process.
"""

import subprocess
import sys
import threading
import time

from .logging_setup import get_logger

log = get_logger("watchdog")

_IS_WIN = sys.platform == "win32"


def beep(loop=False):
    """Play an attention sound (non-blocking)."""
    def _play():
        try:
            if _IS_WIN:
                import winsound
                try:
                    # SND_NODEFAULT makes PlaySound fail (instead of silently
                    # doing nothing) when the sound scheme is "No Sounds", so
                    # we can fall back to an audible tone.
                    flags = (winsound.SND_ALIAS | winsound.SND_ASYNC
                             | winsound.SND_NODEFAULT)
                    if loop:
                        flags |= winsound.SND_LOOP
                    winsound.PlaySound("SystemExclamation", flags)
                except RuntimeError:
                    for _ in range(3):
                        winsound.Beep(880, 250)
                        time.sleep(0.1)
            else:
                print("\a", end="", flush=True)
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()


def stop_beep():
    try:
        if _IS_WIN:
            import winsound
            winsound.PlaySound(None, 0)
    except Exception:
        pass


def flash_taskbar(hwnd, keep_flashing=True):
    """Flash the taskbar button until the window gains focus (Win32 FlashWindowEx)."""
    if not _IS_WIN or not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        FLASHW_ALL = 0x00000003
        FLASHW_TIMERNOFG = 0x0000000C  # flash until window comes to foreground
        flags = FLASHW_ALL | (FLASHW_TIMERNOFG if keep_flashing else 0)
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), wintypes.HWND(hwnd),
                          flags, 0 if keep_flashing else 5, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception as e:
        log.debug("flash_taskbar failed: %s", e)


def stop_flash_taskbar(hwnd):
    if not _IS_WIN or not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]
        FLASHW_STOP = 0
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), wintypes.HWND(hwnd),
                          FLASHW_STOP, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def message_box(title, text):
    """Blocking topmost OS message box (used by the separate watchdog process)."""
    if _IS_WIN:
        try:
            import ctypes
            MB_OK = 0x0
            MB_ICONERROR = 0x10
            MB_TOPMOST = 0x40000
            MB_SETFOREGROUND = 0x10000
            ctypes.windll.user32.MessageBoxW(
                0, str(text), str(title),
                MB_OK | MB_ICONERROR | MB_TOPMOST | MB_SETFOREGROUND)
        except Exception as e:
            log.error("message_box failed: %s", e)
        return
    try:
        if sys.platform == "darwin":
            esc_text = str(text).replace("\\", "\\\\").replace('"', '\\"')
            esc_title = str(title).replace("\\", "\\\\").replace('"', '\\"')
            script = (f'display dialog "{esc_text}" with title "{esc_title}" '
                      'buttons {"OK"} with icon caution')
            subprocess.call(["osascript", "-e", script])
            return
        # Linux: best-effort dialog, then a desktop notification.
        try:
            subprocess.call(["zenity", "--warning", "--title", str(title),
                             "--text", str(text)])
            return
        except FileNotFoundError:
            pass
        try:
            subprocess.call(["notify-send", "-u", "critical", str(title),
                             str(text)])
            return
        except FileNotFoundError:
            pass
    except Exception as e:
        log.debug("message_box fallback failed: %s", e)
    log.error("ALERT: %s - %s", title, text)
