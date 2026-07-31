"""One place for how both windows look: palette, fonts, ttk styles, and a few widget helpers.

Kept apart from the windows themselves so the main window and the consent gate cannot drift into
looking like two different programs. Everything here is local - stock Windows fonts and flat
colours, no images, no downloads, nothing that a PyInstaller one-file build has to be told about.

The ttk theme is switched to 'clam' on purpose. The native 'vista' theme draws its buttons and
entries with the system's own bitmaps and quietly ignores background/foreground/padding, so no
amount of styling shows up. 'clam' honours all of it.
"""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --- palette ---------------------------------------------------------------------------------
# Deep navy for the header, slate for text, warm-cool white for surfaces, one teal accent.
NAVY = "#132A3E"          # header band
NAVY_TEXT = "#FFFFFF"
NAVY_SUB = "#C4D6E4"      # secondary text on the band
INK = "#16232F"           # primary text
SLATE = "#3F5163"         # secondary text
MUTED = "#5B6A79"         # hints, descriptions - dark enough to read on white at 9pt
CANVAS = "#F3F5F7"        # window background
SURFACE = "#FFFFFF"       # cards, entries, log
BORDER = "#CFD8E0"        # hairlines between areas
BORDER_STRONG = "#9FAEBC"  # outlines that have to read as a control edge
ACCENT = "#0F7C8A"        # the single accent - primary action, progress, focus
ACCENT_DK = "#0A5E69"     # pressed
ACCENT_LT = "#12909F"     # hover
ACCENT_SOFT = "#E4F1F3"
DANGER = "#A32F26"
DANGER_LT = "#F7ECEB"
GOOD = "#1C6B4E"
DISABLED_BG = "#E3E8ED"
DISABLED_FG = "#77848F"   # still clearly "off", but the word is legible
SELECT_BG = "#CDE5E9"

STATUS_COLOURS = {"idle": BORDER_STRONG, "busy": ACCENT, "good": GOOD, "bad": DANGER}
STATUS_STYLES = {"idle": "Status.TLabel", "busy": "StatusBusy.TLabel",
                 "good": "StatusGood.TLabel", "bad": "StatusBad.TLabel"}

_fonts = None


def _family(root, names, fallback="TkDefaultFont"):
    """First of `names` actually installed, else whatever Tk is already using."""
    try:
        have = {f.lower() for f in tkfont.families(root)}
    except tk.TclError:
        have = set()
    for name in names:
        if name.lower() in have:
            return name
    return tkfont.nametofont(fallback).actual("family")


def fonts(root=None):
    """The font set, built once. Sizes are points, so they follow the display scaling."""
    global _fonts
    if _fonts is None:
        ui = _family(root, ("Segoe UI", "Tahoma", "Verdana", "Arial"))
        mono = _family(root, ("Cascadia Mono", "Consolas", "Lucida Console", "Courier New"),
                       "TkFixedFont")
        _fonts = {
            "title": (ui, 15, "bold"),
            "subtitle": (ui, 9),
            "section": (ui, 10, "bold"),
            "body": (ui, 10),
            "small": (ui, 9),
            "button": (ui, 10),
            "primary": (ui, 10, "bold"),
            "prose": (ui, 11),
            "prose_head": (ui, 11, "bold"),
            "mono": (mono, 10),
            "mono_head": (mono, 10, "bold"),
        }
    return _fonts


def init(root):
    """Apply the whole look to a window's interpreter. Safe to call more than once."""
    f = fonts(root)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(background=CANVAS)

    # --- containers and text ------------------------------------------------------------------
    style.configure("TFrame", background=CANVAS)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("TLabel", background=CANVAS, foreground=INK, font=f["body"])
    style.configure("Hint.TLabel", background=CANVAS, foreground=MUTED, font=f["small"])
    style.configure("Card.TLabel", background=SURFACE, foreground=INK, font=f["body"])
    style.configure("CardSection.TLabel", background=SURFACE, foreground=INK, font=f["section"])
    style.configure("CardHint.TLabel", background=SURFACE, foreground=MUTED, font=f["small"])
    style.configure("Section.TLabel", background=CANVAS, foreground=INK, font=f["section"])
    style.configure("Status.TLabel", background=CANVAS, foreground=SLATE, font=f["body"])
    style.configure("StatusBusy.TLabel", background=CANVAS, foreground=ACCENT, font=f["body"])
    style.configure("StatusGood.TLabel", background=CANVAS, foreground=GOOD, font=f["body"])
    style.configure("StatusBad.TLabel", background=CANVAS, foreground=DANGER, font=f["body"])
    style.configure("TSeparator", background=BORDER)

    # --- buttons ------------------------------------------------------------------------------
    # Four weights, so the eye can tell them apart without reading them: primary (filled teal),
    # secondary (outlined), quiet (light outline), and destructive (outlined red).
    #
    # clam paints a button's 1px frame from -lightcolor / -darkcolor, NOT from -bordercolor (that
    # one only shows at borderwidth 2+). Setting the frame colours to the fill is what makes an
    # outlined button vanish into the background, so every state below sets fill and edge apart.
    def button(name, fg, edge, fill, edge_hover, fill_hover, fill_press,
               fg_disabled=DISABLED_FG, fill_disabled=DISABLED_BG, edge_disabled="#B4C0CB",
               button_font=None, pad=None, focus=ACCENT):
        # relief="solid" matters: with the flat relief clam skips the frame entirely, which is
        # what made the outlined buttons read as bare text floating on the card.
        style.configure(name, font=button_font or f["button"], padding=pad or (14, 7),
                        relief="solid", borderwidth=1, anchor="center", focuscolor=focus,
                        background=fill, foreground=fg,
                        bordercolor=edge, lightcolor=edge, darkcolor=edge)
        style.map(name,
                  background=[("disabled", fill_disabled), ("pressed", fill_press),
                              ("active", fill_hover)],
                  foreground=[("disabled", fg_disabled)],
                  bordercolor=[("disabled", edge_disabled), ("active", edge_hover)],
                  lightcolor=[("disabled", edge_disabled), ("active", edge_hover)],
                  darkcolor=[("disabled", edge_disabled), ("active", edge_hover)])

    button("TButton", INK, BORDER_STRONG, SURFACE, ACCENT, "#F0F4F8", "#E2E8EE")

    # Primary is the dominant action (Extract); Accent is the same colour at button weight, for
    # the follow-up action that matters once a run has finished (Copy ICD-10 codes).
    button("Primary.TButton", "#FFFFFF", ACCENT, ACCENT, ACCENT_LT, ACCENT_LT, ACCENT_DK,
           button_font=f["primary"], pad=(24, 10), focus="#FFFFFF")
    button("Accent.TButton", "#FFFFFF", ACCENT, ACCENT, ACCENT_LT, ACCENT_LT, ACCENT_DK,
           pad=(16, 8), focus="#FFFFFF")

    # Outlined, dark-ink text: a secondary button still has to look like a button you can press.
    button("Secondary.TButton", INK, BORDER_STRONG, SURFACE, ACCENT, "#F0F4F8", "#E2E8EE")

    # Quiet is the least of the four, but it keeps a visible edge so it is not a floating word.
    button("Quiet.TButton", SLATE, "#C2CDD8", CANVAS, BORDER_STRONG, "#E9EEF2", "#DFE5EB",
           edge_disabled="#D5DDE4", fill_disabled=CANVAS)

    button("Danger.TButton", DANGER, "#C79A94", SURFACE, DANGER, DANGER_LT, "#EFD9D6")

    # --- entry, checkbutton, progress, scrollbar ----------------------------------------------
    style.configure("App.TEntry", padding=(10, 8), relief="flat", borderwidth=1,
                    fieldbackground=SURFACE, foreground=INK, bordercolor=BORDER_STRONG,
                    lightcolor=BORDER_STRONG, darkcolor=BORDER_STRONG, insertcolor=INK,
                    selectbackground=SELECT_BG, selectforeground=INK)
    style.map("App.TEntry",
              bordercolor=[("focus", ACCENT), ("hover", "#A9B6C2")],
              lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
              fieldbackground=[("disabled", DISABLED_BG)])

    for name, bg in (("TCheckbutton", CANVAS), ("Card.TCheckbutton", SURFACE),
                     ("Consent.TCheckbutton", ACCENT_SOFT)):
        # indicatorsize/indicatormargin and the two border colours are clam's own names for the
        # tick box; without them it draws a cramped grey box that ignores the accent entirely.
        style.configure(name, background=bg, foreground=INK, font=f["body"], padding=(0, 3),
                        focuscolor=ACCENT, indicatorbackground=SURFACE, indicatorforeground=ACCENT,
                        indicatorsize=13, indicatormargin=(1, 1, 7, 1),
                        upperbordercolor=BORDER_STRONG, lowerbordercolor=BORDER_STRONG)
        style.map(name,
                  background=[("active", bg)],
                  foreground=[("disabled", DISABLED_FG)],
                  indicatorbackground=[("disabled", DISABLED_BG), ("selected", ACCENT),
                                       ("!selected", SURFACE)],
                  indicatorforeground=[("selected", "#FFFFFF")],
                  upperbordercolor=[("disabled", BORDER), ("selected", ACCENT),
                                    ("active", ACCENT)],
                  lowerbordercolor=[("disabled", BORDER), ("selected", ACCENT),
                                    ("active", ACCENT)])

    style.configure("App.Horizontal.TProgressbar", troughcolor="#E4E9ED", background=ACCENT,
                    bordercolor="#E4E9ED", lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)

    style.configure("App.Vertical.TScrollbar", troughcolor=SURFACE, background="#AEBCC8",
                    bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE,
                    arrowcolor=SLATE, arrowsize=12, relief="flat", borderwidth=0)
    style.map("App.Vertical.TScrollbar", background=[("pressed", SLATE), ("active", "#AEBBC7")])
    return f


# --- widget helpers ---------------------------------------------------------------------------
# Plain tk containers, not ttk ones: a hairline border is exactly what highlightthickness gives,
# and it is the same colour on every Windows build. ttk frame borders are theme-dependent.

def card(parent, **kw):
    """A white panel with a hairline border - the unit the window is built out of."""
    return tk.Frame(parent, background=SURFACE, highlightthickness=1,
                    highlightbackground=BORDER, highlightcolor=BORDER, bd=0, **kw)


def band(parent, colour=NAVY, **kw):
    return tk.Frame(parent, background=colour, bd=0, highlightthickness=0, **kw)


def rule(parent, colour=BORDER, height=1):
    return tk.Frame(parent, background=colour, height=height, bd=0, highlightthickness=0)


def header(parent, title, subtitle):
    """The navy application header, with the accent rule under it. Returns the whole block."""
    f = fonts(parent)
    wrap = band(parent)
    tk.Label(wrap, text=title, background=NAVY, foreground=NAVY_TEXT, font=f["title"],
             anchor="w").pack(fill="x", padx=18, pady=(12, 0))
    tk.Label(wrap, text=subtitle, background=NAVY, foreground=NAVY_SUB, font=f["subtitle"],
             anchor="w", justify="left").pack(fill="x", padx=18, pady=(2, 12))
    rule(wrap, ACCENT, 3).pack(fill="x", side="bottom")
    return wrap


def scrolled_text(parent, font_key="mono", padx=14, pady=11, **kw):
    """A Text with a styled scrollbar, sunk into a bordered card. Returns (card, text)."""
    f = fonts(parent)
    holder = card(parent)
    text = tk.Text(holder, background=SURFACE, foreground=INK, font=f[font_key],
                   relief="flat", borderwidth=0, highlightthickness=0, wrap="word",
                   padx=padx, pady=pady, spacing1=1, spacing3=3,
                   selectbackground=SELECT_BG, selectforeground=INK, insertbackground=ACCENT,
                   inactiveselectbackground=SELECT_BG, **kw)
    scroll = ttk.Scrollbar(holder, orient="vertical", style="App.Vertical.TScrollbar",
                           command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns", padx=(0, 2), pady=2)
    holder.rowconfigure(0, weight=1)
    holder.columnconfigure(0, weight=1)
    return holder, text
