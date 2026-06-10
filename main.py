"""SimpleReliableRecorder - entry point.

Routes between two roles of the same executable:
  * default                -> launches the GUI.
  * --watchdog A B C D E   -> runs the independent watcher process (no window),
                              where A=session_dir B=gui_pid C=stale_seconds
                              D=sound E=messagebox.

Keeping both roles in one file means the whole app ships as a single EXE.
"""

import multiprocessing
import sys


def _safe_setup_logging(tag):
    """setup_logging that can never kill a console=False exe.

    A read-only APPDATA or an AV lock on the log folder must degrade to
    'no file logs', not to a silent instant exit with zero feedback.
    """
    try:
        from recorder.logging_setup import setup_logging
        return setup_logging(tag=tag), None
    except Exception:
        import logging
        import traceback
        err = traceback.format_exc()
        log = logging.getLogger("srr")
        log.addHandler(logging.NullHandler())
        try:
            log.warning("setup_logging failed; continuing without file logs:\n%s",
                        err)
        except Exception:
            pass
        return log, err


def main():
    multiprocessing.freeze_support()
    argv = sys.argv[1:]

    if argv and argv[0] == "--watchdog":
        # Watcher role: minimal, no GUI. Argument validation lives in
        # watchdog_main itself; pass everything through unchanged.
        _safe_setup_logging("watchdog")
        from recorder.watchdog import watchdog_main
        watchdog_main(argv[1:])
        return

    # GUI role.
    log, logging_err = _safe_setup_logging("gui")
    try:
        from ui.app import run
        run()
    except Exception:
        log.exception("Fatal error in GUI")
        # Last-resort visible error so failures are never silent.
        try:
            import tkinter.messagebox as mb
            import traceback
            detail = traceback.format_exc()
            if logging_err:
                detail = ("(logging also failed to initialize:\n"
                          + logging_err + ")\n\n" + detail)
            mb.showerror("SimpleReliableRecorder crashed", detail)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
