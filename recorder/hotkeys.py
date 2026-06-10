"""Global push-to-talk / push-to-mute hotkey support.

Uses the `keyboard` library to register a system-wide hotkey that mutes or
unmutes a target audio device, even when the app is not focused. Three modes:

  * "ptt"    push to talk  - held = unmuted, released = muted
  * "ptm"    push to mute  - held = muted,   released = unmuted
  * "toggle" each press flips the mute state

All state changes are delivered through a callback the App provides; this module
never touches Tk or the audio engine directly. On Windows the `keyboard` library
needs no special privileges for normal key capture.
"""

import threading

from .logging_setup import get_logger

log = get_logger("gui")

try:
    import keyboard as _kb
    _AVAILABLE = True
except Exception as e:  # pragma: no cover - optional dependency
    _AVAILABLE = False
    _IMPORT_ERR = e


def available():
    return _AVAILABLE


class HotkeyManager:
    """Registers one push-to-talk/mute hotkey. Reconfigurable at runtime."""

    def __init__(self, on_mute_change):
        # on_mute_change(target: str, muted: bool) -> None
        self.on_mute_change = on_mute_change
        self._hooks = []        # everything registered with the keyboard lib
        self._hotkey = ""
        self._mode = "ptt"
        self._target = ""
        self._held = False
        self._toggle_state = False
        # True while a mute we asserted is in effect on the target, so teardown
        # can release it instead of leaving the device silently muted forever.
        self._asserted_mute = False
        self._lock = threading.Lock()

    def configure(self, enabled, hotkey, mode, target, initial_state=None):
        """Apply (or clear) the hotkey binding. Safe to call repeatedly.

        initial_state (optional): for mode "toggle", seeds the starting mute
        state so the first press is never a no-op when the device is already
        muted. Accepts a bool or a zero-arg callable returning one; the default
        None keeps the previous behavior (assume unmuted).
        """
        self.clear()
        if not enabled or not hotkey or not _AVAILABLE:
            if enabled and not _AVAILABLE:
                log.warning("Hotkeys unavailable (keyboard lib not installed): %s",
                            _IMPORT_ERR)
            return False
        # Validate before touching any state: an unparseable string must leave
        # us cleanly unbound with nothing emitted.
        try:
            steps = _kb.parse_hotkey(hotkey)
        except Exception:
            log.warning("Invalid hotkey '%s'; hotkey disabled", hotkey)
            return False
        is_combo = len(steps) > 1 or len(steps[0]) > 1
        self._hotkey = hotkey
        self._mode = mode
        self._target = target or ""
        self._held = False
        self._toggle_state = False
        if mode == "toggle" and initial_state is not None:
            try:
                seed = initial_state() if callable(initial_state) else initial_state
                self._toggle_state = bool(seed)
            except Exception:
                log.exception("hotkey initial_state callback failed")
        try:
            if mode == "toggle":
                # Fire once per physical press.
                self._hooks.append(_kb.add_hotkey(hotkey, self._on_toggle,
                                                  suppress=False,
                                                  trigger_on_release=False))
            elif is_combo:
                # hook_key only accepts a single key, so combos like
                # "ctrl+space" get both edges via paired add_hotkey calls.
                self._hooks.append(_kb.add_hotkey(
                    hotkey, lambda: self._on_hold_edge(True), suppress=False))
                self._hooks.append(_kb.add_hotkey(
                    hotkey, lambda: self._on_hold_edge(False), suppress=False,
                    trigger_on_release=True))
            else:
                # Need both edges for press-and-hold behavior, so hook the key
                # directly rather than add_hotkey (which only signals press).
                self._hooks.append(_kb.hook_key(hotkey, self._on_edge,
                                                suppress=False))
            # Emit the initial resting state so the device starts correct.
            if mode == "ptt":
                self._emit(True)    # resting muted until held
            elif mode == "ptm":
                self._emit(False)   # resting unmuted until held
            log.info("Hotkey bound: %s mode=%s target=%s", hotkey, mode,
                     target or "(all mics)")
            return True
        except Exception as e:
            log.exception("Failed to bind hotkey '%s': %s", hotkey, e)
            self._unhook_all()
            return False

    def _on_toggle(self):
        with self._lock:
            self._toggle_state = not self._toggle_state
            muted = self._toggle_state
        self._emit(muted)

    def _on_edge(self, event):
        # event.event_type is 'down' or 'up'
        down = getattr(event, "event_type", None) == "down"
        self._on_hold_edge(down)

    def _on_hold_edge(self, down):
        with self._lock:
            if down == self._held:
                return  # ignore key-repeat / duplicate edges
            self._held = down
        if self._mode == "ptt":
            self._emit(not down)     # held -> unmuted
        else:  # ptm
            self._emit(down)         # held -> muted

    def _emit(self, muted):
        self._asserted_mute = bool(muted)
        if self.on_mute_change:
            try:
                self.on_mute_change(self._target, bool(muted))
            except Exception:
                log.exception("hotkey mute callback failed")

    def _unhook_all(self):
        hooks, self._hooks = self._hooks, []
        for hook in hooks:
            try:
                _kb.unhook(hook)
            except Exception:
                try:
                    _kb.remove_hotkey(hook)
                except Exception:
                    pass

    def clear(self):
        self._unhook_all()
        # Release any mute we put in place (e.g. ptt's resting state) so
        # disabling the hotkey can never leave the device recording silence.
        if self._asserted_mute:
            self._emit(False)

    def stop(self):
        self.clear()
