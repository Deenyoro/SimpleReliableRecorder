"""Immense, rotating logging for SimpleReliableRecorder.

- A combined rotating log (app.log) captures everything.
- Per-subsystem rotating logs (audio.log, screen.log, watchdog.log) capture
  just that subsystem for easy triage.
- An optional in-app handler streams records to the GUI log panel.

Use get_logger("audio") / get_logger("screen") / get_logger("watchdog") etc.
All loggers are children of the "srr" root so they share the combined handler.
"""

import logging
import logging.handlers
import os
import sys

from . import paths

ROOT_NAME = "srr"
_CONFIGURED = False

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def get_logger(name=None):
    """Return a namespaced logger, e.g. get_logger('audio') -> 'srr.audio'."""
    if name:
        return logging.getLogger(f"{ROOT_NAME}.{name}")
    return logging.getLogger(ROOT_NAME)


def _add_rotating_file(logger, filepath, level=logging.DEBUG):
    handler = logging.handlers.RotatingFileHandler(
        filepath, maxBytes=8 * 1024 * 1024, backupCount=8, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    logger.addHandler(handler)
    return handler


def setup_logging(level=logging.DEBUG, tag="gui"):
    """Configure the 'srr' logger tree. Safe to call once per process.

    tag distinguishes processes (gui vs watchdog) in filenames so a spawned
    watchdog never collides with the GUI's own log handles.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return get_logger()

    ldir = paths.logs_dir()
    root = logging.getLogger(ROOT_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    # Combined log — everything.
    _add_rotating_file(root, os.path.join(ldir, f"app.{tag}.log"))

    # Console (visible when run from a terminal / source).
    try:
        ch = logging.StreamHandler(stream=sys.stderr)
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        root.addHandler(ch)
    except Exception:
        pass

    # Per-subsystem logs (these also propagate up to the combined log).
    for sub in ("audio", "screen", "watchdog"):
        sublog = get_logger(sub)
        sublog.setLevel(logging.DEBUG)
        _add_rotating_file(sublog, os.path.join(ldir, f"{sub}.{tag}.log"))

    _CONFIGURED = True
    root.info("=" * 70)
    root.info("Logging initialized (tag=%s). Logs dir: %s", tag, ldir)
    root.info("Python %s | frozen=%s | exe_dir=%s", sys.version.split()[0],
              paths.is_frozen(), paths.exe_dir())
    return root


class CallbackLogHandler(logging.Handler):
    """Forward log records to a callback (the GUI installs this for its panel)."""

    def __init__(self, callback, level=logging.INFO):
        super().__init__(level=level)
        self.callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                                             "%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)
            self.callback(msg, record.levelno)
        except Exception:
            pass


def install_inapp_handler(callback, level=logging.INFO):
    """Attach a CallbackLogHandler to the root srr logger. Returns the handler."""
    handler = CallbackLogHandler(callback, level=level)
    logging.getLogger(ROOT_NAME).addHandler(handler)
    return handler
