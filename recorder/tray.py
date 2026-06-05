"""System tray icon for SimpleReliableRecorder.

Shows a small icon in the notification area: a neutral dot when idle and a bright
red dot while recording, so you can tell at a glance that capture is live even
when the window is minimized or hidden. Right-click exposes Show, Start/Stop, and
Quit.

Runs the pystray icon on its own thread. All callbacks are marshalled back onto
the Tk thread by the App via root.after, so this module never touches Tk.
"""

import threading

from .logging_setup import get_logger

log = get_logger("gui")

try:
    import pystray
    from PIL import Image, ImageDraw
    _AVAILABLE = True
except Exception as e:  # pragma: no cover - optional dependency
    _AVAILABLE = False
    _IMPORT_ERR = e


def available():
    return _AVAILABLE


def _make_image(recording):
    """Draw a 64x64 tray icon. Recording is unmistakable: a solid bright-red
    filled disc. Idle is a hollow gold ring with a muted center, so the two
    states read clearly even at 16x16 in the notification area."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if recording:
        # Solid red disc with a white center dot - obvious "REC" indicator.
        d.ellipse([4, 4, 60, 60], fill=(229, 32, 32, 255),
                  outline=(255, 255, 255, 255), width=3)
        d.ellipse([24, 24, 40, 40], fill=(255, 255, 255, 255))
    else:
        # Idle: gold ring, dark center.
        d.ellipse([6, 6, 58, 58], fill=(40, 44, 52, 255),
                  outline=(255, 193, 7, 255), width=5)
        d.ellipse([26, 26, 38, 38], fill=(120, 124, 132, 255))
    return img


class TrayIcon:
    """Wraps a pystray.Icon. Safe no-op if pystray/Pillow are unavailable."""

    def __init__(self, on_show=None, on_toggle_record=None, on_quit=None,
                 is_recording=None):
        self.on_show = on_show
        self.on_toggle_record = on_toggle_record
        self.on_quit = on_quit
        # callable returning the current recording bool (for the menu label)
        self.is_recording = is_recording or (lambda: False)
        self._icon = None
        self._thread = None
        self._recording = False

    def start(self):
        if not _AVAILABLE:
            log.warning("Tray icon unavailable (pystray/Pillow not installed): %s",
                        _IMPORT_ERR)
            return False

        def _menu():
            return pystray.Menu(
                pystray.MenuItem("Show SimpleReliableRecorder",
                                 lambda: self._fire(self.on_show), default=True),
                pystray.MenuItem(
                    lambda item: "Stop recording" if self.is_recording()
                    else "Start recording",
                    lambda: self._fire(self.on_toggle_record)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda: self._fire(self.on_quit)),
            )

        try:
            self._icon = pystray.Icon(
                "SimpleReliableRecorder",
                icon=_make_image(False),
                title="SimpleReliableRecorder",
                menu=_menu())
            self._thread = threading.Thread(target=self._icon.run,
                                            name="tray", daemon=True)
            self._thread.start()
            log.info("Tray icon started.")
            return True
        except Exception as e:
            log.exception("Failed to start tray icon: %s", e)
            self._icon = None
            return False

    def _fire(self, cb):
        if cb:
            try:
                cb()
            except Exception:
                log.exception("Tray callback failed")

    def set_recording(self, recording):
        recording = bool(recording)
        self._recording = recording
        if self._icon is None:
            return
        # Update the title first (cheap, always works), then the icon image,
        # then the menu - each guarded separately so one failure does not block
        # the others. pystray repaints when .icon is reassigned.
        try:
            self._icon.title = ("SimpleReliableRecorder - RECORDING"
                                if recording else "SimpleReliableRecorder")
        except Exception:
            log.debug("Tray title update failed", exc_info=True)
        try:
            self._icon.icon = _make_image(recording)
        except Exception:
            log.debug("Tray icon image update failed", exc_info=True)
        try:
            self._icon.update_menu()
        except Exception:
            log.debug("Tray menu update failed", exc_info=True)

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
        log.debug("Tray icon stopped.")
