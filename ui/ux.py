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


def eta_text(elapsed, fraction):
    """'about 3 min left' from how long a job has run and how far it got;
    '' until there is enough to go on (the first seconds jump around)."""
    if fraction <= 0.03 or fraction >= 1.0 or elapsed < 3:
        return ""
    left = elapsed * (1.0 - fraction) / fraction
    if left < 45:
        return "less than a minute left"
    mins = round(left / 60.0)
    if mins < 60:
        return f"about {max(1, mins)} min left"
    return f"about {left / 3600.0:.1f} h left"


def total_size(paths_):
    total = 0
    for p in paths_ or []:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


# Recordings list: which optional columns fit. Name always shows and gets
# the leftover width; the others are added in order of usefulness while they
# still fit, so a narrow (snapped) window drops Contents first instead of
# squeezing Name to a few letters or pushing Size off the edge.
LIBRARY_COLUMN_ORDER = ("name", "created", "length", "contents", "size")
LIBRARY_COLUMN_PRIORITY = ("created", "length", "size", "contents")


def library_columns(avail, widths, name_min):
    """Return the columns to display (in display order) for a list that is
    `avail` px wide, given each optional column's preferred width."""
    shown = {"name"}
    used = name_min
    for col in LIBRARY_COLUMN_PRIORITY:
        w = widths.get(col, 0)
        if used + w <= avail:
            shown.add(col)
            used += w
    return [c for c in LIBRARY_COLUMN_ORDER if c in shown]


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


def end_ellipsize(text, max_px, measure):
    """Cut `text` at the end with '…' so measure(text) <= max_px (list
    names: the start of a name is the part people recognise)."""
    if max_px <= 0 or measure(text) <= max_px:
        return text
    lo, hi, best = 0, len(text), "…"
    while lo <= hi:
        keep = (lo + hi) // 2
        cand = text[:keep].rstrip() + "…"
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
_GEOM_RE = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")


def parse_geometry(geom):
    """'WxH+X+Y' (Tk writes '+-1920+0' for a monitor left of the primary)
    -> (w, h, x, y), or None."""
    m = _GEOM_RE.match((geom or "").strip().replace("+-", "-"))
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def min_window_size(scale, work_w, work_h):
    """Smallest main-window size (client area, px) at a UI `scale`.

    Never wider than a Windows Snap half of the monitor's work area (minus
    the resize frame), so the recorder can sit next to Teams/Zoom at 100%,
    125% and 150% on a 1920 screen; never taller than the work area. Below
    the design size the panes wrap, ellipsize and scroll instead."""
    frame = int(16 * scale)
    w = min(int(820 * scale), work_w // 2 - frame, work_w - 40)
    h = min(int(560 * scale), work_h - 40)
    return max(320, w), max(240, h)


def sane_geometry(geom, screen_w, screen_h, min_w=400, min_h=300,
                  bounds=None):
    """Validate a saved 'WxH+X+Y' so it fits on screen; returns a (possibly
    clamped) geometry string, or None when unusable.

    `bounds` = (left, top, right, bottom) of the area the window must fit
    in - on Windows the work area of the monitor the window was saved on
    (which may have negative coordinates), or of the primary monitor when
    that monitor is gone. Defaults to the whole (screen_w x screen_h)
    screen. The window is shrunk to fit and moved fully inside."""
    g = parse_geometry(geom)
    if g is None:
        return None
    w, h, x, y = g
    if w < min_w or h < min_h:
        return None
    left, top, right, bottom = bounds or (0, 0, screen_w, screen_h)
    aw, ah = max(1, right - left), max(1, bottom - top)
    w, h = min(w, aw), min(h, ah)
    # Mostly outside the area (e.g. saved on a monitor that is gone):
    # centre it instead of just nudging a sliver into view.
    if x >= right - 120 or x + w <= left + 120 or y >= bottom - 60 \
            or y + h <= top + 60:
        x = left + (aw - w) // 2
        y = top + (ah - h) // 3
    # Fully inside. A window snapped to the left edge legitimately sits a
    # few px left of it (Windows 10/11 frames have an invisible border).
    x = max(left - 8, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return f"{w}x{h}+{x}+{y}"  # Tk reads '+-1920' as x = -1920
