"""The PyInstaller first-launch splash (Windows one-file build).

Kept in its own tiny module so both the GUI and the lightweight watchdog
entry point can close it without importing the whole UI."""

import logging

log = logging.getLogger("srr.splash")


def close_splash():
    """Close the splash if one is up. A no-op when running from source or
    when the splash was suppressed (e.g. for the --watchdog child)."""
    try:
        import pyi_splash  # only exists inside a PyInstaller build
    except ImportError:
        return
    try:
        if pyi_splash.is_alive():
            pyi_splash.close()
    except Exception:  # never let the splash block startup
        log.debug("closing the splash screen failed", exc_info=True)
