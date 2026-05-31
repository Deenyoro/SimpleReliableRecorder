"""Alerting primitives: taskbar flashing, alert sounds, and an OS message box.

The animated gold banner inside the window lives in ui/widgets.py + ui/app.py;
this module provides the low-level Win32/audio bits used by both the GUI and the
separate watchdog process.
"""

import sys
import threading

from .logging_setup import get_logger

log = get_logger("watchdog")

_IS_WIN = sys.platform == "win32"


def beep(loop=False):
    """Play an attention sound (non-blocking)."""
    def _play():
        try:
            if _IS_WIN:
                import winsound
                flags = winsound.SND_ALIAS | winsound.SND_ASYNC
                if loop:
                    flags |= winsound.SND_LOOP
                winsound.PlaySound("SystemExclamation", flags)
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
    else:
        log.error("ALERT: %s — %s", title, text)
