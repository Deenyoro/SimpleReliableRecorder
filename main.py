"""SimpleReliableRecorder — entry point.

Routes between two roles of the same executable:
  * default            -> launches the GUI.
  * --watchdog A B C D -> runs the independent watcher process (no window),
                          where A=session_dir B=gui_pid C=stale_seconds D=sound.

Keeping both roles in one file means the whole app ships as a single EXE.
"""

import sys


def main():
    argv = sys.argv[1:]

    if argv and argv[0] == "--watchdog":
        # Watcher role: minimal, no GUI.
        from recorder.logging_setup import setup_logging
        from recorder.watchdog import watchdog_main
        setup_logging(tag="watchdog")
        watchdog_main(argv[1:])
        return

    # GUI role.
    from recorder.logging_setup import setup_logging
    log = setup_logging(tag="gui")
    try:
        from ui.app import run
        run()
    except Exception:
        log.exception("Fatal error in GUI")
        # Last-resort visible error so failures are never silent.
        try:
            import tkinter.messagebox as mb
            import traceback
            mb.showerror("SimpleReliableRecorder crashed", traceback.format_exc())
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
