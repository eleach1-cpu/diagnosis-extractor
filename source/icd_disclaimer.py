"""The consent gate shown before the tool can be used.

The window is modal and Accept stays disabled until the box is ticked, so the program cannot be
used without an explicit acceptance. Acceptance is recorded in a plain-text file in the folder
the tool is installed in, and is asked for again whenever the version changes.
"""

import os
import sys
import datetime
import textwrap

# tkinter is imported lazily inside the window functions ONLY. The command-line program needs
# acceptance_path() / accepted_version() to check whether the terms were agreed to, and it must
# be able to do that without dragging in a GUI toolkit.

ACCEPT_NAME = "icd_tool_accepted.txt"

TITLE = "Before you use this tool - please read"

# The terms as (kind, text) blocks. Each block is ONE long line with no hard line breaks in it:
# the window wraps it to whatever width the reader has chosen, and the saved copy is wrapped by
# textwrap. Hard-wrapping the source is what produced stranded words like "...speaking with a /
# doctor." in a narrow window and a half-empty page in a wide one.
DISCLAIMER_BLOCKS = [
    ("body", "This program reads documents and lists the diagnosis codes it finds."
             " That is all it does."),

    ("head", "It is not medical advice."),
    ("body", "Nothing it produces is a diagnosis, a medical opinion, or a substitute for"
             " speaking with a doctor."),

    ("head", "It is not legal advice."),
    ("body", "Nothing it produces is a legal opinion or a substitute for speaking with an"
             " attorney or an accredited representative. A diagnosis code is not a claim, a"
             " rating, or a decision."),

    ("head", "It will miss things."),
    ("body", "No tool of this kind can pull every detail out of every document. Scanned pages,"
             " handwriting, unusual layouts, faint print, tables, and photographed paper records"
             " can all defeat it. Codes may be missed, misread, or tied to the wrong date. The"
             " dates it shows are approximate. A diagnosis written in plain words with no code"
             " beside it may not be picked up at all."),

    ("head", "Check everything against the original records."),
    ("body", "Treat the report as a starting point for your own review, never as the final word."
             " Before you rely on any item, especially for a medical or legal decision, open the"
             " source document and confirm it with your own eyes."),

    ("body", "You use this tool at your own risk. It is provided as-is, with no warranty of any"
             " kind."),
]


def disclaimer_text(width=78):
    """The terms as plain text, wrapped for a file. Used for the saved acceptance record."""
    out = []
    for kind, text in DISCLAIMER_BLOCKS:
        if kind == "head" and out:
            out.append("")
        out.append("\n".join(textwrap.wrap(text, width=width)))
        out.append("")
    return "\n".join(out).strip() + "\n"


DISCLAIMER = disclaimer_text()


def install_dir():
    """The folder the program lives in - beside the .exe, or beside the source when run from
    Python."""
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _candidate_dirs():
    yield install_dir()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield os.path.join(local, "ICD_Tool")


def _writable(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, ".icd_write_test")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def acceptance_path():
    """Where the acceptance record lives. An existing record wins wherever it is; otherwise the
    install folder, falling back to LOCALAPPDATA when the install folder is read-only (as it is
    under Program Files). None if nowhere can be written."""
    for folder in _candidate_dirs():
        path = os.path.join(folder, ACCEPT_NAME)
        if os.path.exists(path):
            return path
    for folder in _candidate_dirs():
        if _writable(folder):
            return os.path.join(folder, ACCEPT_NAME)
    return None


def accepted_version(path):
    """The version recorded in an acceptance file, or None."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def record_acceptance(path, version):
    """Write the acceptance record. Returns True on success."""
    if not path:
        return False
    try:
        user = os.environ.get("USERNAME") or "unknown"
        machine = os.environ.get("COMPUTERNAME") or "unknown"
        with open(path, "w", encoding="utf-8") as f:
            f.write("ICD-10 Diagnosis Extractor - terms accepted\n")
            f.write(f"Version:   {version}\n")
            f.write(f"Accepted:  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write(f"User:      {user}\n")
            f.write(f"Machine:   {machine}\n")
            f.write("\nThe person named above ticked the box and clicked Accept on the"
                    " following terms:\n\n")
            f.write("-" * 78 + "\n")
            f.write(DISCLAIMER)
            f.write("-" * 78 + "\n")
        return True
    except OSError:
        return False


def _build_window(parent, version, read_only):
    """The disclaimer window itself, dressed like the main window: navy header, one bordered
    reading panel, an actions strip along the bottom. read_only gives the plain 'view it again'
    variant with a single Close button and no checkbox.

    Returns (window, actions frame) - the caller fills the actions frame, because the gate and
    the read-only view need different things in it.
    """
    import tkinter as tk
    from tkinter import ttk
    import icd_theme                    # imported here, not at module level: the command-line
    # build imports this module only to read the acceptance record and must not pull in tkinter.

    win = tk.Toplevel(parent)
    win.title(TITLE if not read_only else "Terms of use")
    win.geometry("800x680")
    win.minsize(620, 540)
    fonts = icd_theme.init(win)

    win.columnconfigure(0, weight=1)
    win.rowconfigure(1, weight=1)
    icd_theme.header(win, TITLE if not read_only else "Terms of use",
                     f"Diagnosis Extractor, version {version}").grid(row=0, column=0, sticky="ew")

    outer = ttk.Frame(win, padding=(18, 14, 18, 16))
    outer.grid(row=1, column=0, sticky="nsew")
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(1, weight=1)

    ttk.Label(outer, style="Hint.TLabel",
              text=("Please read all of it. The tool will not run until you have accepted."
                    if not read_only else
                    "These are the terms in force for this version.")
              ).grid(row=0, column=0, sticky="w", pady=(0, 8))

    # A proportional font, not the Text widget's default fixed-width one - this is prose meant to
    # be read, by people who are not looking at a terminal all day.
    card, body = icd_theme.scrolled_text(outer, "prose", padx=22, pady=16, height=14)
    card.grid(row=1, column=0, sticky="nsew")
    body.tag_configure("head", font=fonts["prose_head"], foreground=icd_theme.NAVY,
                       spacing1=16, spacing3=4)
    body.tag_configure("body", font=fonts["prose"], foreground=icd_theme.INK,
                       spacing1=2, spacing3=10, lmargin1=2, lmargin2=2)
    for kind, text in DISCLAIMER_BLOCKS:
        body.insert("end", text + "\n", kind)
    body.configure(state="disabled")

    actions = ttk.Frame(outer)
    actions.grid(row=2, column=0, sticky="ew", pady=(14, 0))
    actions.columnconfigure(0, weight=1)
    return win, actions


def _present(win, parent):
    """Put the window on screen, in front, with the focus, and make it modal.

    transient() is applied ONLY when the parent is actually on screen. A transient of a hidden
    window gets no taskbar button and does not raise to the front - which at startup, with the
    main window still withdrawn, means the program appears to launch and die.
    """
    import tkinter as tk
    from icd_extract import debug
    debug(f"_present: parent viewable={parent.winfo_viewable()}")
    if parent.winfo_viewable():
        win.transient(parent)
    win.update_idletasks()
    debug("_present: deiconify")
    win.deiconify()
    win.lift()
    debug(f"_present: after deiconify, win viewable={win.winfo_viewable()} "
          f"ismapped={win.winfo_ismapped()}")
    # Raise above whatever else is on screen, then drop the flag straight away - done inline
    # rather than on a timer, so nothing is left pending to fire after the window is destroyed.
    win.attributes("-topmost", True)
    win.update()
    win.attributes("-topmost", False)
    win.focus_force()
    debug("_present: focus forced")
    try:
        win.grab_set()
    except tk.TclError:
        pass                                # not modal, but still usable - better than no window
    debug("_present: done")


def show_terms(parent, version):
    """Read-only view, for the Help menu."""
    from tkinter import ttk
    win, actions = _build_window(parent, version, read_only=True)
    ttk.Button(actions, text="Close", style="Secondary.TButton",
               command=win.destroy).grid(row=0, column=0, sticky="e")
    _present(win, parent)
    parent.wait_window(win)


def ask_acceptance(parent, version):
    """Show the gate. Returns True only if the box was ticked and Accept clicked."""
    import tkinter as tk
    from tkinter import ttk
    import icd_theme
    result = {"ok": False}
    win, actions = _build_window(parent, version, read_only=False)

    agreed = tk.BooleanVar(value=False)

    def toggle(*_):
        accept_btn.configure(state="normal" if agreed.get() else "disabled")

    # Tick box on its own row under the text, buttons on a row below it. Keeping them in one row
    # put them at opposite ends of a maximised window, a long way apart for no reason. The tinted
    # strip marks the tick box as the thing standing between the reader and the program.
    checkrow = tk.Frame(actions, background=icd_theme.ACCENT_SOFT, bd=0, highlightthickness=1,
                        highlightbackground=icd_theme.ACCENT, highlightcolor=icd_theme.ACCENT)
    checkrow.grid(row=0, column=0, sticky="ew", pady=(0, 14))
    ttk.Checkbutton(checkrow, text="I have read and understand the above.",
                    variable=agreed, command=toggle, padding=6,
                    style="Consent.TCheckbutton").pack(side="left", padx=12, pady=8)

    row = ttk.Frame(actions)
    row.grid(row=1, column=0, sticky="ew")

    def decline():
        result["ok"] = False
        win.destroy()

    def accept():
        if not agreed.get():                 # belt and braces - the button is disabled anyway
            return
        result["ok"] = True
        win.destroy()

    accept_btn = ttk.Button(row, text="Accept", command=accept, state="disabled",
                            style="Primary.TButton")
    accept_btn.pack(side="right")
    ttk.Button(row, text="Decline and exit", command=decline,
               style="Danger.TButton").pack(side="right", padx=(0, 10))

    # Closing the window with the X is a decline, never an acceptance.
    win.protocol("WM_DELETE_WINDOW", decline)
    _present(win, parent)
    parent.wait_window(win)
    return result["ok"]


def require_acceptance(parent, version):
    """The whole gate: skip if this version was already accepted, otherwise ask and record.

    Returns (allowed, note). note is a line worth showing the user - it is set when the
    acceptance could not be stored, so they know they will be asked again next time.
    """
    from icd_extract import debug
    path = acceptance_path()
    debug(f"require_acceptance: path={path} recorded={accepted_version(path)} want={version}")
    if accepted_version(path) == version:
        debug("require_acceptance: already accepted")
        return True, None
    debug("require_acceptance: showing gate")
    if not ask_acceptance(parent, version):
        debug("require_acceptance: declined")
        return False, None
    debug("require_acceptance: accepted")
    if record_acceptance(path, version):
        return True, f"Terms accepted - recorded in {path}"
    return True, ("Terms accepted, but the acceptance could not be saved "
                  "(no writable folder), so you will be asked again next time.")
