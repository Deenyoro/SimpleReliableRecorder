"""Reusable Tkinter widgets + dark theme for SimpleReliableRecorder.

Signatures are kept stable (COLORS, apply_dark_theme, ScrollFrame, StatusLight,
LevelMeter, DeviceRow, GoldBanner) so ui/app.py is unaffected by visual tweaks.
"""

import math
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# Modern flat dark palette.
COLORS = {
    "bg": "#16181d",
    "panel": "#1e2127",
    "panel2": "#262a32",
    "panel3": "#2f343d",
    "fg": "#e6e8ec",
    # Muted carries load-bearing hint text at 10pt, so it is kept a notch
    # brighter than a decorative gray (~6.3:1 on bg, ~5.7:1 on panel).
    "muted": "#9aa1ad",
    "accent": "#4cc2b0",
    "accent_dim": "#36serial",  # placeholder, overwritten below
    "gold": "#FFC107",
    "gold_bright": "#FFE082",
    "red": "#ef5350",
    "green": "#3ecf7d",
    "blue": "#5ab0f0",
    "border": "#333945",
}
COLORS["accent_dim"] = "#3a8f83"

FONT = "Segoe UI"

# Spacing tokens so paddings stop drifting between panels and dialogs.
PAD_XS, PAD_S, PAD_M, PAD_L = 4, 8, 12, 16

_ui_scale = None


def ui_scale(widget):
    """Pixel scale factor (1.0 at 96 DPI). Canvas-based widgets use fixed
    pixel sizes that don't follow `tk scaling`, so they multiply by this to
    stay proportionate to the text on high-DPI displays."""
    global _ui_scale
    if _ui_scale is None:
        try:
            _ui_scale = max(1.0, widget.winfo_fpixels("1i") / 96.0)
        except Exception:
            return 1.0
    return _ui_scale

# Maps the ttk frame styles we use to their background color, so plain tk
# widgets (Canvas/Label) placed inside them can match exactly.
_STYLE_BG = {
    "Card.TFrame": COLORS["panel2"],
    "Panel.TFrame": COLORS["panel"],
    "TFrame": COLORS["bg"],
}


def _widget_bg(widget):
    """Best-effort background color of a widget (works for tk and ttk frames)."""
    try:
        return widget.cget("bg")          # plain tk widgets expose bg
    except Exception:
        pass
    try:
        style = str(widget.cget("style")) or widget.winfo_class()
        if style in _STYLE_BG:
            return _STYLE_BG[style]
        return ttk.Style().lookup(style, "background") or COLORS["bg"]
    except Exception:
        return COLORS["bg"]


def apply_dark_theme(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    c = COLORS
    root.configure(bg=c["bg"])

    style.configure(".", background=c["bg"], foreground=c["fg"],
                    fieldbackground=c["panel2"], bordercolor=c["border"],
                    font=(FONT, 11))
    style.configure("TFrame", background=c["bg"])
    style.configure("Panel.TFrame", background=c["panel"])
    style.configure("Card.TFrame", background=c["panel2"])

    style.configure("TLabel", background=c["bg"], foreground=c["fg"],
                    font=(FONT, 11))
    style.configure("Panel.TLabel", background=c["panel"], foreground=c["fg"],
                    font=(FONT, 11))
    style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"],
                    font=(FONT, 10))
    style.configure("Header.TLabel", background=c["bg"], foreground=c["accent"],
                    font=(FONT, 11, "bold"))
    style.configure("Title.TLabel", background=c["bg"], foreground=c["fg"],
                    font=("Segoe UI Semibold", 18, "bold"))

    # Buttons.
    style.configure("TButton", background=c["panel3"], foreground=c["fg"],
                    borderwidth=0, focuscolor=c["accent"], padding=(12, 7),
                    font=(FONT, 10))
    style.map("TButton",
              background=[("active", c["accent_dim"]), ("disabled", "#23262d")],
              foreground=[("active", "#06120f"), ("disabled", "#5b606a")])
    style.configure("Accent.TButton", background=c["accent"], foreground="#06120f",
                    font=(FONT, 10, "bold"), padding=(14, 8), borderwidth=0)
    style.map("Accent.TButton",
              background=[("active", "#63d6c6"), ("disabled", "#2c4f49")])

    # Inputs.
    style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"],
                    focuscolor=c["bg"])
    style.map("TCheckbutton",
              background=[("active", c["bg"])],
              foreground=[("active", c["fg"])])
    style.configure("TRadiobutton", background=c["bg"], foreground=c["fg"],
                    focuscolor=c["bg"], padding=(0, 3))
    style.map("TRadiobutton", background=[("active", c["bg"])])
    style.configure("TCombobox", fieldbackground=c["panel2"], background=c["panel3"],
                    foreground=c["fg"], arrowcolor=c["fg"], bordercolor=c["border"],
                    padding=(6, 5))
    style.configure("TCombobox", font=(FONT, 11))
    style.map("TCombobox", fieldbackground=[("readonly", c["panel2"])],
              foreground=[("readonly", c["fg"])])
    style.configure("TEntry", fieldbackground=c["panel2"], foreground=c["fg"],
                    bordercolor=c["border"], padding=(6, 4), insertcolor=c["fg"])
    style.configure("TSpinbox", fieldbackground=c["panel2"], foreground=c["fg"],
                    arrowcolor=c["fg"], bordercolor=c["border"], padding=(4, 3))

    # Sliders (gain faders).
    style.configure("Horizontal.TScale", background=c["panel"],
                    troughcolor=c["panel3"], bordercolor=c["panel"])

    # Scrollbars.
    style.configure("Vertical.TScrollbar", background=c["panel3"],
                    troughcolor=c["bg"], bordercolor=c["bg"], arrowcolor=c["muted"])
    style.map("Vertical.TScrollbar", background=[("active", c["accent_dim"])])

    # Section frames. Background matches the window so a section's TFrame
    # interior doesn't show as a darker rectangle floating inside it.
    style.configure("TLabelframe", background=c["bg"], foreground=c["accent"],
                    bordercolor=c["border"], relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=c["bg"], foreground=c["accent"],
                    font=(FONT, 10, "bold"))

    # Labels that sit on Card.TFrame rows (device rows, library rows).
    style.configure("Card.TLabel", background=c["panel2"], foreground=c["fg"],
                    font=(FONT, 11))

    # Busy bar for background combine/convert/transcribe jobs.
    style.configure("Busy.Horizontal.TProgressbar",
                    background=c["accent"], troughcolor=c["panel3"],
                    bordercolor=c["panel"], lightcolor=c["accent"],
                    darkcolor=c["accent"])

    # Settings notebook.
    style.configure("TNotebook", background=c["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=c["panel"], foreground=c["muted"],
                    padding=(14, 7), font=(FONT, 10))
    style.map("TNotebook.Tab",
              background=[("selected", c["panel3"])],
              foreground=[("selected", c["fg"])])
    return style


class Tooltip:
    """Delayed, theme-matched hover tooltip for any widget."""

    def __init__(self, widget, text, delay=600):
        self.widget, self.text, self.delay = widget, text, delay
        self._tip = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text):
        self.text = text

    def _schedule(self, _e=None):
        self._cancel()
        try:
            self._job = self.widget.after(self.delay, self._show)
        except Exception:
            pass

    def _show(self):
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self._tip = tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            tk.Label(tw, text=self.text, bg=COLORS["panel3"], fg=COLORS["fg"],
                     font=(FONT, 9), justify="left", wraplength=320,
                     padx=8, pady=5, bd=1, relief="solid").pack()
        except Exception:
            self._tip = None

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class ScrollFrame(ttk.Frame):
    """A vertically scrollable container. Put content into `.body`.

    Wheel events are routed globally to the ScrollFrame under the pointer
    instead of per-instance <Enter>/<Leave> bind_all juggling: with NESTED
    ScrollFrames (the recordings list inside the scrollable right column) the
    old scheme unbound the outer frame's wheel the moment the pointer left the
    inner one, leaving the column wheel-dead until re-entered. The router also
    respects widgets with native wheel scrolling (Text, Listbox): scrolling
    the live log must not drag the whole column along.
    """

    _router_installed_for = set()  # toplevel ids with the wheel router bound

    def __init__(self, parent):
        super().__init__(parent, style="TFrame")
        self.canvas = tk.Canvas(self, bg=COLORS["bg"], highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, style="TFrame")
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.body.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfig(self._win, width=e.width))
        top = self.winfo_toplevel()
        if id(top) not in ScrollFrame._router_installed_for:
            ScrollFrame._router_installed_for.add(id(top))
            # bind (not bind_all): per-toplevel, so it dies with the window and
            # never outlives its widgets. "all" bindtag processing means every
            # widget in this window funnels unhandled wheel events here.
            top.bind_class(str(top), "<MouseWheel>", ScrollFrame._route_wheel)
            top.bind("<Destroy>", lambda e, t=top: (
                ScrollFrame._router_installed_for.discard(id(t))
                if e.widget is t else None), add="+")
            top.bind("<MouseWheel>", ScrollFrame._route_wheel)

    @staticmethod
    def _route_wheel(event):
        """Scroll the innermost scrollABLE ScrollFrame under the pointer.

        Walking up from the pointer widget: a Text/Listbox handles its own
        wheel (class binding already ran), so stop there; a ScrollFrame whose
        content fits is skipped so the wheel falls through to an outer one.
        """
        try:
            w = event.widget.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return
        while w is not None:
            if isinstance(w, (tk.Text, tk.Listbox)):
                return  # native scrolling area - leave the event to it
            if isinstance(w, ScrollFrame):
                try:
                    lo, hi = w.canvas.yview()
                    if hi - lo < 1.0:  # content taller than the viewport
                        w.canvas.yview_scroll(int(-event.delta / 120), "units")
                        return "break"
                except tk.TclError:
                    return
            w = getattr(w, "master", None)


class StatusLight(tk.Frame):
    """A small colored dot with a label, used for per-subsystem state.

    A Frame with a real Label (auto-sizes, follows DPI) instead of a
    fixed-width canvas that clipped longer states like 'Screen: REC 1023MB'.
    `text` is the bare subsystem name ("Audio"); set_state appends the state.
    """

    def __init__(self, parent, text, **kw):
        super().__init__(parent, bg=COLORS["bg"], **kw)
        s = ui_scale(parent)
        d = int(14 * s)
        self.dot = tk.Canvas(self, width=d, height=d, bg=COLORS["bg"],
                             highlightthickness=0)
        pad = max(2, int(2 * s))
        self._oval = self.dot.create_oval(pad, pad, d - pad, d - pad,
                                          fill=COLORS["muted"], outline="")
        self.dot.pack(side="left", padx=(0, 5))
        self.lbl = tk.Label(self, text=text, bg=COLORS["bg"],
                            fg=COLORS["fg"], font=(FONT, 10))
        self.lbl.pack(side="left")
        self._base = text

    def set_state(self, color, suffix=""):
        self.dot.itemconfig(self._oval, fill=color)
        self.lbl.config(text=self._base + suffix)


class LevelMeter(tk.Canvas):
    """An OBS-style horizontal peak meter with a decaying peak-hold tick."""

    def __init__(self, parent, width=200, height=16):
        super().__init__(parent, width=width, height=height, highlightthickness=1,
                         highlightbackground=COLORS["border"], bg="#0b0d10")
        # NOTE: do NOT use self._w / self._h - those are Tk Canvas internals
        # (the widget's Tcl path). Use distinct names.
        self._mw = width
        self._mh = height
        self._peak_hold = 0.0
        self._draw(0.0)

    @staticmethod
    def _to_frac(peak):
        """Map a linear peak (0..1) to an OBS-style dBFS display fraction.

        -60 dBFS -> 0.0, 0 dBFS -> 1.0. This makes normal speech (which is quiet
        in linear terms) clearly visible instead of a tiny sliver.
        """
        if peak <= 1e-6:
            return 0.0
        db = 20.0 * math.log10(min(1.0, peak))
        return max(0.0, min(1.0, (db + 60.0) / 60.0))

    def set_level(self, peak):
        frac = self._to_frac(peak)
        self._peak_hold = max(frac, self._peak_hold * 0.90)
        self._draw(frac)

    def _draw(self, frac):
        self.delete("all")
        w, h = self._mw, self._mh
        # Subtle segment background.
        filled = int(min(1.0, frac) * w)
        seg = 4
        for x in range(0, w, seg):
            frac = x / w
            lit = x < filled
            if frac < 0.6:
                col = "#2fbf63" if lit else "#15301f"
            elif frac < 0.85:
                col = "#e6c200" if lit else "#332d0a"
            else:
                col = "#ef4040" if lit else "#331414"
            self.create_rectangle(x + 1, 2, x + seg - 1, h - 2, fill=col, outline="")
        ph = int(min(1.0, self._peak_hold) * w)
        if ph > 1:
            self.create_rectangle(ph - 1, 1, ph + 1, h - 1, fill="#ffffff", outline="")


class ToggleSwitch(tk.Frame):
    """An iPhone-style on/off sliding toggle bound to a tk.BooleanVar.

    Implemented as a Frame holding a fixed-size Canvas (the switch) plus a real
    ttk.Label (the text). The Label auto-sizes to its content, so the caption can
    NEVER be clipped regardless of length, font, or DPI scaling.
    """

    LABEL_FONT = (FONT, 11)

    def __init__(self, parent, variable, text="", command=None,
                 bg=None, width=52, height=28):
        if bg is None:
            # Inherit the parent's real background so the switch blends into
            # cards / panels / the window automatically (no color mismatch).
            bg = _widget_bg(parent)
        super().__init__(parent, bg=bg)
        self.var = variable
        self.command = command
        s = ui_scale(parent)
        width, height = int(width * s), int(height * s)
        self._w_sw = width
        self._h_sw = height

        self.canvas = tk.Canvas(self, width=width, height=height,
                                highlightthickness=0, bg=bg, cursor="hand2",
                                takefocus=1)
        self.canvas.pack(side="left")
        self.canvas.bind("<Button-1>", self._on_click)
        # Keyboard operability + a visible focus ring (canvas widgets are
        # otherwise invisible to Tab navigation).
        self.canvas.bind("<space>", self._on_click)
        self.canvas.bind("<Return>", self._on_click)
        self.canvas.bind("<FocusIn>", lambda e: self.canvas.configure(
            highlightthickness=2, highlightbackground=COLORS["accent"]))
        self.canvas.bind("<FocusOut>", lambda e: self.canvas.configure(
            highlightthickness=0))

        if text:
            # Determine a label style whose background matches our parent so the
            # text blends in (panels use a different bg than the window).
            style_name = "Panel.TLabel" if bg == COLORS["panel"] else "TLabel"
            self.label = tk.Label(self, text=text, bg=bg, fg=COLORS["fg"],
                                  font=self.LABEL_FONT, cursor="hand2")
            self.label.pack(side="left", padx=(10, 0))
            self.label.bind("<Button-1>", self._on_click)

        try:
            self._trace = self.var.trace_add("write", lambda *a: self._draw())
        except Exception:
            self._trace = None
        # Remove the trace when the widget dies, otherwise the stale callback
        # raises TclError and aborts every later trace on the same variable.
        self.bind("<Destroy>", self._on_destroy)
        self._draw()

    def _on_destroy(self, event):
        # <Destroy> fires once per descendant; only act on our own.
        if event.widget is not self:
            return
        if self._trace is not None:
            trace, self._trace = self._trace, None
            try:
                self.var.trace_remove("write", trace)
            except Exception:
                pass

    def _on_click(self, _e=None):
        self.var.set(not bool(self.var.get()))
        if self.command:
            self.command()
        return "break"

    def _draw(self):
        try:
            if not self.winfo_exists():
                return
            c = self.canvas
            c.delete("all")
            on = bool(self.var.get())
            w, h = self._w_sw, self._h_sw
            pad = 3
            r = (h - 2 * pad) / 2
            track = COLORS["accent"] if on else "#3a3f48"
            c.create_oval(1, pad, 1 + (h - 2 * pad), h - pad, fill=track, outline="")
            c.create_oval(w - (h - 2 * pad) - 1, pad, w - 1, h - pad,
                          fill=track, outline="")
            c.create_rectangle(1 + r, pad, w - 1 - r, h - pad, fill=track, outline="")
            kx = (w - 1 - r) if on else (1 + r)
            c.create_oval(kx - r, pad, kx + r, h - pad, fill="#ffffff", outline="")
        except tk.TclError:
            pass


class SegmentedControl(tk.Frame):
    """A clearly-visible vertical option selector bound to a StringVar.

    Replaces tiny ttk radio buttons: each option is a full-width clickable row,
    the selected one is highlighted in the accent color.
    """

    def __init__(self, parent, variable, options, command=None, bg=None):
        bg = bg or COLORS["panel"]
        super().__init__(parent, bg=bg, takefocus=1)
        self.var = variable
        self.command = command
        self._values = [v for v, _ in options]
        self._rows = {}  # value -> label widget
        for value, text in options:
            row = tk.Label(self, text="   " + text, anchor="w", justify="left",
                           font=(FONT, 11), padx=12, pady=9, cursor="hand2",
                           bd=0)
            row.pack(fill="x", pady=2)
            row.bind("<Button-1>", lambda e, v=value: self._select(v))
            self._rows[value] = row
        # Keyboard operability: Tab to focus, Up/Down to change selection.
        self.bind("<Up>", lambda e: self._move(-1))
        self.bind("<Down>", lambda e: self._move(+1))
        self.bind("<FocusIn>", lambda e: self.configure(
            highlightthickness=2, highlightbackground=COLORS["accent"]))
        self.bind("<FocusOut>", lambda e: self.configure(highlightthickness=0))
        self._refresh()
        try:
            self._trace = self.var.trace_add("write", lambda *a: self._refresh())
        except Exception:
            self._trace = None
        # Remove the trace when the widget dies, otherwise the stale callback
        # raises TclError and aborts every later trace on the same variable.
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, event):
        # <Destroy> fires once per descendant; only act on our own.
        if event.widget is not self:
            return
        if self._trace is not None:
            trace, self._trace = self._trace, None
            try:
                self.var.trace_remove("write", trace)
            except Exception:
                pass

    def _select(self, value):
        self.var.set(value)
        if self.command:
            self.command()

    def _move(self, step):
        try:
            i = self._values.index(self.var.get())
        except ValueError:
            i = 0
        self._select(self._values[max(0, min(len(self._values) - 1, i + step))])
        return "break"

    def _refresh(self):
        try:
            if not self.winfo_exists():
                return
            cur = self.var.get()
            for value, row in self._rows.items():
                if value == cur:
                    row.configure(bg=COLORS["accent"], fg="#06120f",
                                  font=(FONT, 11, "bold"))
                else:
                    row.configure(bg=COLORS["panel3"], fg=COLORS["fg"],
                                  font=(FONT, 11))
        except tk.TclError:
            pass


class DeviceRow(ttk.Frame):
    """A selectable capture device with a gain fader and a live level meter."""

    def __init__(self, parent, devices, on_remove, on_change=None, preset=None,
                 gain=1.0, muted=False):
        super().__init__(parent, style="Card.TFrame", padding=10)
        self.devices = devices
        self.on_change = on_change
        self.muted = bool(muted)
        self._map = {}
        values = []
        for d in devices:
            icon = "MIC  " if d["kind"] == "input" else "SPK  "
            tag = "  [loopback]" if d["kind"] == "loopback" else ""
            label = f'{icon}{d["name"]}  ({d["hostapi"]}){tag}'
            self._map[label] = d
            values.append(label)

        # Row 0: device chooser + mute + remove
        self.var = tk.StringVar()
        self.combo = ttk.Combobox(self, textvariable=self.var, values=values,
                                  state="readonly", width=46)
        self.combo.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        self.mute_btn = tk.Button(self, text="Mute", width=7, relief="flat",
                                  font=(FONT, 10, "bold"), cursor="hand2",
                                  command=self._toggle_mute)
        self.mute_btn.grid(row=0, column=2, sticky="e", padx=(0, 6))
        Tooltip(self.mute_btn, "Silences this device in the recording - "
                               "its meter drops to zero while muted.")
        self.remove_btn = ttk.Button(self, text="Remove", width=8,
                                     command=lambda: on_remove(self))
        self.remove_btn.grid(row=0, column=3, sticky="e")

        # Row 1: gain fader + percent + meter. Range 0..800% so a quiet mic can
        # actually be boosted. Mousewheel nudges it for easy fine control.
        self.GAIN_MAX = 800
        ttk.Label(self, text="Volume", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(10, 0))
        self.gain_var = tk.DoubleVar(value=float(gain) * 100.0)
        self.scale = ttk.Scale(self, from_=0, to=self.GAIN_MAX, variable=self.gain_var,
                               command=self._on_gain, length=240,
                               style="Horizontal.TScale")
        self.scale.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(10, 0))
        self.scale.bind("<MouseWheel>", self._on_wheel)
        Tooltip(self.scale, "Drag or scroll to boost a quiet mic - "
                            "100% is normal, up to 800% for very quiet ones.")
        self.pct_lbl = ttk.Label(self, text=f"{int(float(gain) * 100)}%",
                                 style="Card.TLabel", width=6)
        self.pct_lbl.grid(row=1, column=2, sticky="w", pady=(10, 0))
        self.meter = LevelMeter(self)
        self.meter.grid(row=1, column=3, sticky="e", pady=(10, 0))

        self.columnconfigure(1, weight=1)

        matched = False
        if preset:
            for key in ("id", "namekind", "name"):
                for label, d in self._map.items():
                    if key == "id" and preset.get("id") and d.get("id") == preset.get("id"):
                        self.var.set(label); matched = True; break
                    if key == "namekind" and d["name"] == preset.get("name") \
                            and d["kind"] == preset.get("kind"):
                        self.var.set(label); matched = True; break
                    if key == "name" and d["name"] == preset.get("name"):
                        self.var.set(label); matched = True; break
                if matched:
                    break
        if not matched and values:
            if preset and preset.get("kind"):
                for label, d in self._map.items():
                    if d["kind"] == preset.get("kind"):
                        self.var.set(label); matched = True; break
            if not matched:
                self.var.set(values[0])

        self._refresh_mute_btn()

    def _refresh_mute_btn(self):
        if self.muted:
            self.mute_btn.config(text="Muted", bg=COLORS["red"], fg="#0b0b0b",
                                 activebackground="#ff7b72")
        else:
            self.mute_btn.config(text="Mute", bg=COLORS["panel3"],
                                 fg=COLORS["fg"], activebackground=COLORS["accent_dim"])

    def _toggle_mute(self):
        self.set_muted(not self.muted, notify=True)

    def set_muted(self, muted, notify=False):
        self.muted = bool(muted)
        self._refresh_mute_btn()
        if notify and self.on_change:
            self.on_change("mute", self)

    def is_muted(self):
        return self.muted

    def _on_wheel(self, event):
        step = 10 if event.delta > 0 else -10
        new = min(self.GAIN_MAX, max(0, self.gain_var.get() + step))
        self.gain_var.set(new)
        self._on_gain(new)
        return "break"

    def _on_gain(self, _value):
        g = self.get_gain()
        self.pct_lbl.config(text=f"{int(g * 100)}%")
        if self.on_change:
            self.on_change("gain", self)

    def get_gain(self):
        return float(self.gain_var.get()) / 100.0

    def get_selection(self):
        return self._map.get(self.var.get())

    def current_source_label(self):
        d = self.get_selection()
        if not d:
            return None
        return f'{d["name"]} [{d["kind"]}]'

    def set_level(self, peak):
        self.meter.set_level(0.0 if self.muted else peak)


class GoldBanner(tk.Frame):
    """The impossible-to-miss bottom alert bar. Flashes gold when active."""

    def __init__(self, parent, on_ack, on_restart):
        super().__init__(parent, bg=COLORS["gold"], height=66)
        self.on_ack = on_ack
        self.on_restart = on_restart
        self._flash_on = False
        self._flashing = False
        self._flash_after = None

        self.label = tk.Label(self, text="", bg=COLORS["gold"], fg="#1a1a1a",
                              font=(FONT, 13, "bold"), anchor="w", justify="left")
        self.label.pack(side="left", padx=18, pady=10, fill="x", expand=True)

        self.restart_btn = tk.Button(self, text="Restart recording",
                                     command=self._restart, bg="#1a1a1a",
                                     fg=COLORS["gold_bright"], relief="flat",
                                     font=(FONT, 10, "bold"), padx=14, pady=7,
                                     activebackground="#000000",
                                     activeforeground=COLORS["gold_bright"],
                                     cursor="hand2")
        self.restart_btn.pack(side="right", padx=(6, 18), pady=12)

        self.ack_btn = tk.Button(self, text="Dismiss", command=self._ack,
                                bg="#1a1a1a", fg=COLORS["gold_bright"], relief="flat",
                                font=(FONT, 10, "bold"), padx=14, pady=7,
                                activebackground="#000000",
                                activeforeground=COLORS["gold_bright"],
                                cursor="hand2")
        self.ack_btn.pack(side="right", pady=12)

    def _ack(self):
        self.stop()
        if self.on_ack:
            self.on_ack()

    def _restart(self):
        self.stop()
        if self.on_restart:
            self.on_restart()

    def show(self, message):
        self._cancel_flash()
        self.label.config(text="   RECORDING PROBLEM:   " + message)
        if not self.winfo_manager():
            self.pack(side="bottom", fill="x")
        if not self._flashing:
            self._flashing = True
        self._flash()

    def _cancel_flash(self):
        if self._flash_after is not None:
            after_id, self._flash_after = self._flash_after, None
            try:
                self.after_cancel(after_id)
            except Exception:
                pass

    def _flash(self):
        if not self._flashing:
            return
        self._flash_on = not self._flash_on
        col = COLORS["gold_bright"] if self._flash_on else COLORS["gold"]
        self.config(bg=col)
        self.label.config(bg=col)
        self._flash_after = self.after(450, self._flash)

    def stop(self):
        self._flashing = False
        self._cancel_flash()
        try:
            self.pack_forget()
        except Exception:
            pass
