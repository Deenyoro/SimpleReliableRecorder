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
    """Draw a 64x64 tray icon: a ring with a center dot (red while recording)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ring = (255, 193, 7, 255)          # gold ring (brand color)
    dot = (239, 83, 80, 255) if recording else (120, 124, 132, 255)
    d.ellipse([6, 6, 58, 58], outline=ring, width=5)
    d.ellipse([20, 20, 44, 44], fill=dot)
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
        if recording == self._recording:
            return
        self._recording = recording
        if self._icon is not None:
            try:
                self._icon.icon = _make_image(recording)
                self._icon.title = ("SimpleReliableRecorder - RECORDING"
                                    if recording else "SimpleReliableRecorder")
                self._icon.update_menu()
            except Exception:
                log.debug("Tray update failed", exc_info=True)

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
        log.debug("Tray icon stopped.")
