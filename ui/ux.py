"""Plain-language UI helpers with no Tk dependency.

Everything here is pure so it can be unit-tested on any CI runner (no display
needed): friendly names and labels, settings display maps, hotkey capture
translation, size/duration formatting, friendly error text and window
geometry sanity checks.
"""

import os
import re

# --------------------------------------------------------------------------- #
# Recording names
# --------------------------------------------------------------------------- #
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_STAMP_RE = re.compile(
    r"^SRR_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$")


def friendly_recording_name(name):
    """'SRR_2026-09-23_05-49-48' -> 'Recording 23 Sep 2026, 05:49'.

    Only the display changes; the folder on disk keeps its name. Names the
    user chose (anything not matching the automatic stamp) are shown as-is.
    """
    m = _STAMP_RE.match(name or "")
    if not m:
        return name or "Recording"
    y, mo, d, hh, mm, _ss = m.groups()
    try:
        month = _MONTHS[int(mo) - 1]
    except (ValueError, IndexError):
        return name
    return f"Recording {int(d)} {month} {y}, {hh}:{mm}"


def friendly_created(created):
    """'2026-09-22 16:40:51' -> '22 Sep 2026, 16:40' (blank stays blank)."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", created or "")
    if not m:
        return created or ""
    y, mo, d, hh, mm = m.groups()
    try:
        month = _MONTHS[int(mo) - 1]
    except (ValueError, IndexError):
        return created
    return f"{int(d)} {month} {y}, {hh}:{mm}"


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #
KIND_WORD = {"input": "Mic", "loopback": "Sound from"}


def device_label(dev):
    """Dropdown text for a device: plain words, no host-API noise."""
    prefix = KIND_WORD.get(dev.get("kind"), "Device")
    return f"{prefix}: {dev.get('name', '?')}"


def device_labels(devices):
    """[(label, device)] with unique labels (a duplicate name gets its
    host API appended, then a number, so the map back to devices is exact)."""
    out, seen = [], set()
    for d in devices:
        label = device_label(d)
        if label in seen:
            label = f"{label} ({d.get('hostapi', '')})".replace(" ()", "")
        n = 2
        base = label
        while label in seen:
            label = f"{base} #{n}"
            n += 1
        seen.add(label)
        out.append((label, d))
    return out


def next_unused_device(devices, used_ids, prefer_kind=None):
    """First device (optionally of prefer_kind) whose id is not in used_ids,
    else None. Used by '+ Add device' so it never silently adds a duplicate."""
    pool = list(devices)
    if prefer_kind:
        pool = ([d for d in pool if d.get("kind") == prefer_kind]
                + [d for d in pool if d.get("kind") != prefer_kind])
    for d in pool:
        if d.get("id") not in used_ids:
            return d
    return None


def source_display(label):
    """Internal source label 'Name [input]' -> "microphone 'Name'" for
    alerts; unknown shapes pass through unchanged."""
    m = re.match(r"^(.*) \[(input|loopback)\]$", label or "")
    if not m:
        return label
    name, kind = m.groups()
    word = "microphone" if kind == "input" else "system sound from"
    return f"{word} '{name}'"


def humanize_subsystem_error(label, reason):
    """Gold-banner text for a subsystem error, without bracketed internals."""
    if label in ("screen", "Screen"):
        who = "Screen recording"
    elif label in ("audio", "Audio"):
        who = "Audio recording"
    else:
        who = source_display(label)
        who = who[:1].upper() + who[1:]
    return f"{who}: {reason}"


# --------------------------------------------------------------------------- #
# Settings display maps (labels in the UI, the same tokens in config.json)
# --------------------------------------------------------------------------- #
CHOICES = {
    "audio_subtype": [
        ("PCM_16", "16-bit (most compatible)"),
        ("FLOAT", "32-bit float (never clips)"),
    ],
    "screen_encoder": [
        ("auto", "Automatic (best available)"),
        ("nvenc", "NVIDIA graphics (NVENC)"),
        ("qsv", "Intel graphics (Quick Sync)"),
        ("amf", "AMD graphics (AMF)"),
        ("videotoolbox", "Apple (VideoToolbox)"),
        ("cpu", "Processor only (slowest)"),
    ],
    "screen_container": [
        ("mkv", "MKV (crash-safe)"),
        ("mp4", "MP4 (plays everywhere)"),
    ],
    "screen_codec": [
        ("h264", "H.264 (most compatible)"),
        ("hevc", "H.265 / HEVC (smaller files)"),
    ],
    "screen_quality": [
        ("high", "High"),
        ("balanced", "Balanced"),
        ("small", "Small files"),
    ],
    "screen_reliability": [
        ("hybrid", "Maximum - clean MP4 at stop (recommended)"),
        ("fragmented", "Crash-safe fragmented MP4"),
        ("standard", "Standard MP4 (lost if the app crashes)"),
    ],
    "on_stop_action": [
        ("ask", "Ask me"),
        ("combine", "Always make one video"),
        ("separate", "Keep separate files"),
    ],
    "ptt_mode": [
        ("ptt", "Push to talk (hold to speak)"),
        ("ptm", "Push to mute (hold to mute)"),
        ("toggle", "Toggle (press to switch)"),
    ],
}


def choice_label(key, value):
    """Display label for a config value; unknown values show as themselves
    so a hand-edited config never renders blank."""
    for v, lbl in CHOICES.get(key, []):
        if v == value:
            return lbl
    return str(value)


def choice_value(key, label):
    """Config value for a display label (inverse of choice_label)."""
    for v, lbl in CHOICES.get(key, []):
        if lbl == label:
            return v
    return label


# --------------------------------------------------------------------------- #
# Hotkey capture: Tk key event -> `keyboard` library hotkey string
# --------------------------------------------------------------------------- #
_MODIFIER_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Meta_L", "Meta_R", "Super_L", "Super_R", "Win_L", "Win_R",
    "Caps_Lock", "ISO_Level3_Shift",
}
_KEYSYM_NAMES = {
    "space": "space", "Return": "enter", "KP_Enter": "enter",
    "Tab": "tab", "BackSpace": "backspace", "Delete": "delete",
    "Insert": "insert", "Home": "home", "End": "end", "Prior": "page up",
    "Next": "page down", "Up": "up", "Down": "down", "Left": "left",
    "Right": "right", "Pause": "pause", "Scroll_Lock": "scroll lock",
    "Num_Lock": "num lock", "Print": "print screen", "Menu": "menu",
    "grave": "`", "minus": "-", "equal": "=", "bracketleft": "[",
    "bracketright": "]", "backslash": "\\", "semicolon": ";",
    "apostrophe": "'", "comma": ",", "period": ".", "slash": "/",
}
# Tk event.state bits (same on Windows and X11 for these three).
STATE_SHIFT, STATE_CONTROL = 0x0001, 0x0004
STATE_ALT_WIN, STATE_ALT_X11 = 0x20000, 0x0008


def is_modifier_keysym(keysym):
    return keysym in _MODIFIER_KEYSYMS


def hotkey_from_event(keysym, state, windows=True):
    """Translate a Tk <KeyPress> (keysym + state bits) to the string the
    `keyboard` library understands, e.g. 'ctrl+space', 'f8'. Returns None for
    a bare modifier press (keep waiting for the real key)."""
    if not keysym or is_modifier_keysym(keysym):
        return None
    if re.fullmatch(r"F\d{1,2}", keysym):
        key = keysym.lower()
    elif keysym in _KEYSYM_NAMES:
        key = _KEYSYM_NAMES[keysym]
    elif keysym.startswith("KP_"):
        key = "num " + keysym[3:].lower()
    elif len(keysym) == 1:
        key = keysym.lower()
    else:
        key = keysym.lower()
    mods = []
    if state & STATE_CONTROL:
        mods.append("ctrl")
    alt_bit = STATE_ALT_WIN if windows else STATE_ALT_X11
    if state & alt_bit:
        mods.append("alt")
    if state & STATE_SHIFT:
        mods.append("shift")
    return "+".join(mods + [key])


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def plural(n, word, many=None):
    return f"{n} {word if n == 1 else (many or word + 's')}"


def fmt_bytes(n):
    """Compact human size: 512 B, 12 KB, 41 MB, 1.3 GB."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"
    return ""


def fmt_duration(seconds):
    """3:12, 1:02:03 (blank for unknown/negative)."""
    try:
        s = round(float(seconds))
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def contents_text(n_audio, n_video):
    """'2 tracks + screen', '1 track', 'screen only'."""
    parts = []
    if n_audio:
        parts.append(plural(n_audio, "track"))
    if n_video:
        parts.append("screen")
    if not parts:
        return "empty"
    if parts == ["screen"]:
        return "screen only"
    return " + ".join(parts)


def take_summary(n_audio, n_video, seconds, total_bytes):
    """One line for the success strip after STOP."""
    bits = [contents_text(n_audio, n_video)]
    dur = fmt_duration(seconds) if seconds else ""
    if dur:
        bits.append(dur)
    size = fmt_bytes(total_bytes) if total_bytes else ""
    if size:
        bits.append(size)
    return "  ·  ".join(bits)


def total_size(paths_):
    total = 0
    for p in paths_ or []:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def middle_ellipsize(text, max_px, measure):
    """Shorten `text` in the middle until measure(text) <= max_px so both the
    drive and the distinctive tail of a path survive."""
    if max_px <= 0 or measure(text) <= max_px:
        return text
    lo, hi = 0, len(text)
    best = "…"
    while lo <= hi:
        keep = (lo + hi) // 2
        head = keep - keep // 2
        tail = keep // 2
        cand = text[:head] + "…" + (text[-tail:] if tail else "")
        if measure(cand) <= max_px:
            best = cand
            lo = keep + 1
        else:
            hi = keep - 1
    return best


# --------------------------------------------------------------------------- #
# Friendly errors
# --------------------------------------------------------------------------- #
_ERROR_RULES = [
    (re.compile(r"no space left|disk full|not enough space|errno 28",
                re.IGNORECASE),
     "The disk is full.",
     "Free up some space or choose another folder in Settings > Saving."),
    (re.compile(r"permission denied|access is denied|errno 13|winerror 5\b",
                re.IGNORECASE),
     "Windows blocked access to a file or folder.",
     ("Choose a folder you own in Settings > Saving (for example Videos), "
      "or close any program that has the file open.")),
    (re.compile(r"ffmpeg not found|ffmpeg.*(no such file|cannot find)|"
                r"cannot find the file specified.*ffmpeg", re.IGNORECASE),
     "The video tool that ships with the app (ffmpeg) is missing.",
     "Reinstall Simple Reliable Recorder. Audio-only recording still works."),
    (re.compile(r"0x8889000a|device.?in.?use|exclusive mode|"
                r"device or resource busy", re.IGNORECASE),
     "Another program is using this device exclusively.",
     ("Close apps that may own the device (calls, DAWs), or turn off "
      "'Allow applications to take exclusive control' in the Windows "
      "Sound settings for it.")),
    (re.compile(r"0x88890004|device.?invalidated|no such device|"
                r"invalid device", re.IGNORECASE),
     "The audio device is not available.",
     "Check it is plugged in, then press Refresh next to the devices."),
]


def friendly_error(text):
    """(headline, advice) for a raw error string, or (None, None)."""
    t = str(text or "")
    for rx, headline, advice in _ERROR_RULES:
        if rx.search(t):
            return headline, advice
    return None, None


# --------------------------------------------------------------------------- #
# Window geometry persistence
# --------------------------------------------------------------------------- #
_GEOM_RE = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")


def sane_geometry(geom, screen_w, screen_h, min_w=400, min_h=300):
    """Validate a saved 'WxH+X+Y' so it fits the current screen; returns a
    (possibly clamped) geometry string, or None when unusable (e.g. the saved
    monitor is gone). Never restores a window off-screen."""
    m = _GEOM_RE.match((geom or "").strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    x, y = int(m.group(3)), int(m.group(4))
    if w < min_w or h < min_h:
        return None
    w, h = min(w, screen_w), min(h, screen_h)
    # At least 120 px of the title bar must remain on the screen.
    if x > screen_w - 120 or x + w < 120 or y < -8 or y > screen_h - 60:
        x = max(0, (screen_w - w) // 2)
        y = max(0, (screen_h - h) // 3)
    x = max(-8, min(x, screen_w - 120))
    y = max(0, min(y, screen_h - 60))
    return f"{w}x{h}+{x}+{y}"
