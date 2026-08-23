"""A small window over the ICD-10 extractor. Pick a file or folder, tick the options, Extract.

Runs the scan on a background thread so the window stays responsive during the slow OCR of
scanned pages; progress lines stream into the log as it goes, and Stop ends a long run early.
"""

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog

import icd_extract
import icd_disclaimer
import icd_theme

SETTINGS_NAME = "icd_tool_settings.txt"


def _report_path(target):
    """Where to write the report: beside the target if we can, else beside the program."""
    folder = target if os.path.isdir(target) else os.path.dirname(target)
    for cand in (folder, os.path.dirname(os.path.abspath(sys.argv[0])), os.getcwd()):
        try:
            test = os.path.join(cand, ".icd_write_test")
            with open(test, "w") as f:
                f.write("")
            os.remove(test)
            return os.path.join(cand, "icd10_report.txt")
        except Exception:
            continue
    return os.path.join(os.getcwd(), "icd10_report.txt")


def _settings_path():
    """Beside the acceptance record, so both land in the same (writable) place."""
    accept = icd_disclaimer.acceptance_path()
    if not accept:
        return None
    return os.path.join(os.path.dirname(accept), SETTINGS_NAME)


def _load_last_target():
    path = _settings_path()
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if line.startswith("last_target="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _save_last_target(target):
    path = _settings_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"last_target={target}\n")
    except OSError:
        pass


def _resource(name):
    """A bundled data file - works both from source and from the PyInstaller one-file exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _read_only_text(widget):
    """Keep a Text widget selectable and copyable but not editable. state='disabled' would block
    selection too, which stops people lifting a code straight out of the log."""
    def block(event):
        if event.state & 0x4:                       # Ctrl held - allow copy / select-all
            return None
        if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                            "Prior", "Next", "Shift_L", "Shift_R"):
            return None
        return "break"

    def travel(forward):
        """Tab must leave the log. The Text class binding types a tab character and stops the
        traversal, which strands keyboard users inside a widget they cannot even edit."""
        def go(event):
            nxt = event.widget.tk_focusNext() if forward else event.widget.tk_focusPrev()
            if nxt:
                nxt.focus_set()
            return "break"
        return go

    widget.bind("<Key>", block)
    widget.bind("<Tab>", travel(True))
    widget.bind("<Shift-Tab>", travel(False))
    widget.bind("<ISO_Left_Tab>", travel(False))        # X11 name for Shift+Tab


ERROR_LOG_NAME = "icd_tool_error.log"


def _write_crash_log(text):
    """A --windowed exe has no console, so an unhandled error would vanish without trace. Put it
    in a file beside the program and tell the user where it went."""
    for folder in (icd_disclaimer.install_dir(),
                   os.path.join(os.environ.get("LOCALAPPDATA", ""), "ICD_Tool"),
                   os.getcwd()):
        if not folder:
            continue
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, ERROR_LOG_NAME)
            with open(path, "a", encoding="utf-8") as f:
                import datetime
                f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                        f"version {icd_extract.VERSION} =====\n")
                f.write(text)
            return path
        except OSError:
            continue
    return None


def launch():
    """Run the window, and make sure any failure is visible rather than a silent exit."""
    try:
        _launch()
    except Exception:
        import traceback
        text = traceback.format_exc()
        path = _write_crash_log(text)
        last = text.strip().splitlines()[-1] if text.strip() else "Unknown error"
        try:
            from tkinter import messagebox
            tmp = tk.Tk()
            tmp.withdraw()
            messagebox.showerror(
                "ICD-10 Extractor - error",
                f"The program hit an error and had to stop.\n\n{last}\n\n"
                + (f"Details written to:\n{path}" if path else "The details could not be saved."))
            tmp.destroy()
        except Exception:
            pass
        raise


def _launch():
    icd_extract.debug(f"_launch: start, frozen={getattr(sys, 'frozen', False)} argv0={sys.argv[0]}")
    root = tk.Tk()
    icd_extract.debug("_launch: Tk created")
    root.title("Diagnosis Extractor")
    # Roomy by default, but never taller than the screen it opens on - a 768-high laptop would
    # otherwise get a window with its result buttons under the taskbar.
    root.geometry(f"{min(920, int(root.winfo_screenwidth() * 0.9))}"
                  f"x{min(780, int(root.winfo_screenheight() * 0.88))}")
    root.minsize(700, 600)          # replaced below by the layout's own measured minimum
    fonts = icd_theme.init(root)
    try:
        root.iconbitmap(_resource("appicon.ico"))
    except Exception:
        try:
            root.iconphoto(True, tk.PhotoImage(file=_resource("appicon.png")))
        except Exception:
            pass

    # --- consent gate: nothing else is built until this passes -------------------------------
    icd_extract.debug("_launch: withdraw + gate")
    root.withdraw()
    allowed, accept_note = icd_disclaimer.require_acceptance(root, icd_extract.VERSION)
    icd_extract.debug(f"_launch: gate returned allowed={allowed}")
    if not allowed:
        root.destroy()
        return
    root.deiconify()
    icd_extract.debug("_launch: main window shown")

    target_var = tk.StringVar(value=_load_last_target())
    unique_var = tk.BooleanVar(value=False)
    noocr_var = tk.BooleanVar(value=False)
    state = {"out": None, "report": None, "stop": None}

    def pick_file():
        p = filedialog.askopenfilename(
            title="Choose a document",
            filetypes=[("Documents", "*.pdf *.docx *.txt *.htm *.html *.xml"), ("All files", "*.*")])
        if p:
            target_var.set(p)

    def pick_folder():
        p = filedialog.askdirectory(title="Choose a folder")
        if p:
            target_var.set(p)

    # --- menu --------------------------------------------------------------------------------
    menubar = tk.Menu(root)
    filemenu = tk.Menu(menubar, tearoff=0)
    filemenu.add_command(label="Choose file...", command=lambda: pick_file())
    filemenu.add_command(label="Choose folder...", command=lambda: pick_folder())
    filemenu.add_separator()
    filemenu.add_command(label="Exit", command=root.destroy)
    menubar.add_cascade(label="File", menu=filemenu)

    helpmenu = tk.Menu(menubar, tearoff=0)
    helpmenu.add_command(label="View terms of use",
                         command=lambda: icd_disclaimer.show_terms(root, icd_extract.VERSION))
    helpmenu.add_command(label="About", command=lambda: show_about())
    menubar.add_cascade(label="Help", menu=helpmenu)
    root.configure(menu=menubar)

    # --- header ------------------------------------------------------------------------------
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)
    icd_theme.header(
        root, "Diagnosis Extractor",
        "Reads your documents and lists the diagnosis codes in them. Runs entirely on this "
        "computer - nothing is sent anywhere.").grid(row=0, column=0, sticky="ew")

    # One body frame owning all the padding, laid out in a single column: source, options,
    # actions, status, activity, results. Every row keeps its natural height except the log,
    # which takes whatever is left over.
    body = ttk.Frame(root, padding=(16, 12, 16, 12))
    body.grid(row=1, column=0, sticky="nsew")
    body.columnconfigure(0, weight=1)
    body.rowconfigure(6, weight=1)

    # --- source ------------------------------------------------------------------------------
    src = icd_theme.card(body)
    src.grid(row=0, column=0, sticky="ew")
    src.columnconfigure(0, weight=1)
    ttk.Label(src, text="Document or folder to scan",
              style="CardSection.TLabel").grid(row=0, column=0, columnspan=3, sticky="w",
                                               padx=16, pady=(10, 1))
    ttk.Label(src, text="One PDF, Word, text, HTML or XML file - or a folder, to read every "
                        "document inside it.",
              style="CardHint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w",
                                            padx=16, pady=(0, 7))
    entry = ttk.Entry(src, textvariable=target_var, style="App.TEntry", font=fonts["body"])
    entry.grid(row=2, column=0, sticky="ew", padx=(16, 8), pady=(0, 12))
    ttk.Button(src, text="File...", style="Secondary.TButton",
               command=pick_file).grid(row=2, column=1, pady=(0, 12))
    ttk.Button(src, text="Folder...", style="Secondary.TButton",
               command=pick_folder).grid(row=2, column=2, padx=(8, 16), pady=(0, 12))

    # --- options -----------------------------------------------------------------------------
    opts = icd_theme.card(body)
    opts.grid(row=1, column=0, sticky="ew", pady=(10, 0))
    opts.columnconfigure(0, weight=1)
    ttk.Label(opts, text="Options",
              style="CardSection.TLabel").grid(row=0, column=0, sticky="w", padx=16, pady=(10, 4))
    ttk.Checkbutton(opts, text="Collapse duplicate diagnoses", variable=unique_var,
                    style="Card.TCheckbutton").grid(row=1, column=0, sticky="w", padx=14)
    ttk.Label(opts, text="List each diagnosis once instead of repeating every mention of it.",
              style="CardHint.TLabel").grid(row=2, column=0, sticky="w", padx=(37, 16),
                                            pady=(0, 6))
    ttk.Checkbutton(opts, text="Skip scanned pages", variable=noocr_var,
                    style="Card.TCheckbutton").grid(row=3, column=0, sticky="w", padx=14)
    ttk.Label(opts, text="Much faster, but scanned and photographed pages are left unread, so "
                         "codes on them are missed.",
              style="CardHint.TLabel").grid(row=4, column=0, sticky="w", padx=(37, 16),
                                            pady=(0, 11))

    # --- actions -----------------------------------------------------------------------------
    runrow = ttk.Frame(body)
    runrow.grid(row=2, column=0, sticky="ew", pady=(12, 0))
    extract_btn = ttk.Button(runrow, text="Extract", style="Primary.TButton")
    extract_btn.pack(side="left")
    stop_btn = ttk.Button(runrow, text="Stop", state="disabled", style="Danger.TButton")
    stop_btn.pack(side="left", padx=(10, 0))
    clear_btn = ttk.Button(runrow, text="Clear", style="Quiet.TButton")
    clear_btn.pack(side="left", padx=(4, 0))
    ttk.Label(runrow, text="Enter also starts a run", style="Hint.TLabel").pack(side="right",
                                                                                pady=(0, 2))

    # --- status + progress -------------------------------------------------------------------
    statusrow = ttk.Frame(body)
    statusrow.grid(row=3, column=0, sticky="ew", pady=(12, 0))
    dot = tk.Frame(statusrow, width=9, height=9, background=icd_theme.STATUS_COLOURS["idle"],
                   bd=0, highlightthickness=0)
    dot.pack(side="left", pady=(3, 0))
    dot.pack_propagate(False)
    status = ttk.Label(statusrow, text="Choose a file or folder, then Extract.",
                       style="Status.TLabel")
    status.pack(side="left", padx=(9, 0))

    bar = ttk.Progressbar(body, mode="determinate", value=0,
                          style="App.Horizontal.TProgressbar")
    bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))

    # --- activity log ------------------------------------------------------------------------
    loghead = ttk.Frame(body)
    loghead.grid(row=5, column=0, sticky="ew", pady=(12, 4))
    ttk.Label(loghead, text="Activity", style="Section.TLabel").pack(side="left")
    ttk.Label(loghead, text="select any line to copy it", style="Hint.TLabel").pack(side="right")

    logcard, log = icd_theme.scrolled_text(body, "mono", height=4, width=52)
    logcard.grid(row=6, column=0, sticky="nsew")
    log.tag_configure("head", foreground=icd_theme.NAVY, font=fonts["mono_head"])
    log.tag_configure("note", foreground=icd_theme.MUTED)
    log.tag_configure("error", foreground=icd_theme.DANGER)
    log.tag_configure("good", foreground=icd_theme.GOOD)
    _read_only_text(log)

    # --- results (hidden until there is a report) ---------------------------------------------
    footer = ttk.Frame(body)
    footer.grid(row=7, column=0, sticky="ew", pady=(12, 0))
    icd_theme.rule(footer).pack(fill="x", pady=(0, 10))
    resultrow = ttk.Frame(footer)
    resultrow.pack(fill="x")
    copy_btn = ttk.Button(resultrow, text="Copy ICD-10 codes", style="Accent.TButton")
    open_report_btn = ttk.Button(resultrow, text="Open report", style="Secondary.TButton")
    open_folder_btn = ttk.Button(resultrow, text="Open folder", style="Secondary.TButton")
    handoff_btn = ttk.Button(resultrow, text="Create Claim File Handoff",
                             style="Secondary.TButton")

    def log_line(msg, tag=None):
        log.insert("end", msg + "\n", tag or ())
        log.see("end")

    def set_status(msg, kind="idle"):
        """One place for the status line, so its wording and its colour cannot disagree."""
        status.configure(text=msg, style=icd_theme.STATUS_STYLES[kind])
        dot.configure(background=icd_theme.STATUS_COLOURS[kind])

    def progress(msg):                    # called from the worker thread
        root.after(0, _progress_ui, msg)

    def _progress_ui(msg):
        log_line(msg)
        set_status(msg.strip(), "busy")

    def show_about():
        accept_path = icd_disclaimer.acceptance_path()
        recorded = icd_disclaimer.accepted_version(accept_path)
        log_line("")
        log_line(f"Diagnosis Extractor, version {icd_extract.VERSION}", "head")
        log_line(f"Program folder: {icd_disclaimer.install_dir()}", "note")
        log_line(f"Terms accepted: version {recorded or '(none recorded)'}", "note")
        if accept_path:
            log_line(f"Acceptance record: {accept_path}", "note")
        log_line("Codes validated against the CDC ICD-10-CM code set in icd10_data.tsv.", "note")

    def hide_results():
        copy_btn.pack_forget()
        open_report_btn.pack_forget()
        open_folder_btn.pack_forget()
        handoff_btn.pack_forget()
        footer.grid_remove()

    def show_results():
        footer.grid()
        copy_btn.pack(side="left")
        open_report_btn.pack(side="left", padx=(8, 0))
        open_folder_btn.pack(side="left", padx=(8, 0))
        handoff_btn.pack(side="left", padx=(8, 0))

    def set_running(running):
        extract_btn.configure(state="disabled" if running else "normal")
        stop_btn.configure(state="normal" if running else "disabled")
        clear_btn.configure(state="disabled" if running else "normal")
        if running:
            bar.configure(mode="indeterminate")
            bar.start(12)
        else:
            bar.stop()
            # An idle indeterminate bar parks a block at the left, which reads as a stalled run.
            # Empty determinate trough instead: the row keeps its height, so nothing jumps.
            bar.configure(mode="determinate", value=0)
            if stop_btn.instate(["focus"]):   # Stop is about to go dead - do not strand the focus
                extract_btn.focus_set()

    def open_path(p):
        try:
            os.startfile(p)               # Windows
        except Exception as e:
            log_line(f"Could not open: {e}")

    def make_handoff():
        """Write the claim-organization document beside the report, and open it.

        Organizes what the finished run already found - never rescans. The button only shows
        after a completed run, but the guard stays: Clear empties the report while the button
        object still exists, and a stale LAST_RUN from an earlier target must never be
        presented as this one's results.
        """
        if not state["report"] or not state["out"] or not icd_extract.LAST_RUN:
            set_status("No completed extraction to organize - run an extraction first.", "bad")
            return
        try:
            text = icd_extract.claim_handoff_text()
        except ValueError as e:
            set_status(str(e), "bad")
            return
        path = os.path.splitext(state["out"])[0] + "_claim_handoff.txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            set_status(f"Could not write the handoff document: {e}", "bad")
            return
        log_line("")
        log_line("Claim file handoff saved to:", "head")
        log_line(f"   {path}", "note")
        set_status("Claim file handoff created - not medical or legal advice; confirm every "
                   "item against the source record.", "good")
        open_path(path)

    def copy_codes():
        codes = icd_extract.codes_from_report(state["report"] or "")
        if not codes:
            set_status("No ICD-10 codes in this report to copy.", "bad")
            return
        root.clipboard_clear()
        root.clipboard_append("\n".join(codes))
        set_status(f"{len(codes)} ICD-10 code(s) copied - paste into {icd_extract.CODES_URL}",
                   "good")

    def worker(target, no_ocr, unique, stop_event):
        out = _report_path(target)
        state["out"] = out
        try:
            report = icd_extract.run_extraction(target, out, no_ocr=no_ocr, unique=unique,
                                                progress=progress,
                                                should_stop=stop_event.is_set)
        except FileNotFoundError:
            root.after(0, done, None, "icd10_data.tsv is missing - keep it next to this program.")
            return
        except ValueError as e:
            root.after(0, done, None, str(e))
            return
        except Exception as e:
            root.after(0, done, None, f"Something went wrong: {e}")
            return
        root.after(0, done, report, None)

    def done(report, error):
        set_running(False)
        if error:
            set_status(error, "bad")
            log_line("ERROR: " + error, "error")
            return
        state["report"] = report
        tail = [ln for ln in report.splitlines() if "diagnosis line(s)" in ln]
        codes = icd_extract.codes_from_report(report)
        summary = tail[-1] if tail else "Done."
        if codes:
            summary += f"  {len(codes)} ICD-10 code(s)."
        set_status(summary, "good")
        log_line("")
        log_line("---- REPORT ----", "head")
        log_line(report)
        log_line("")
        log_line(f"Saved to: {state['out']}", "note")
        show_results()

    def start():
        target = target_var.get().strip().strip('"')
        if not target or not os.path.exists(target):
            set_status("Pick a real file or folder first.", "bad")
            entry.focus_set()
            return
        _save_last_target(target)
        log.delete("1.0", "end")
        hide_results()
        state["report"] = None
        state["stop"] = threading.Event()
        set_status("Working... (scanned pages take a few seconds each)", "busy")
        set_running(True)
        threading.Thread(target=worker,
                         args=(target, noocr_var.get(), unique_var.get(), state["stop"]),
                         daemon=True).start()

    def stop():
        if state["stop"]:
            state["stop"].set()
        stop_btn.configure(state="disabled")
        set_status("Stopping after the current page...", "busy")

    def clear():
        target_var.set("")
        log.delete("1.0", "end")
        unique_var.set(False)
        noocr_var.set(False)
        state["out"] = None
        state["report"] = None
        hide_results()
        set_status("Choose a file or folder, then Extract.")
        entry.focus_set()

    extract_btn.configure(command=start)
    stop_btn.configure(command=stop)
    clear_btn.configure(command=clear)
    copy_btn.configure(command=copy_codes)
    handoff_btn.configure(command=make_handoff)
    open_report_btn.configure(command=lambda: open_path(state["out"]))
    open_folder_btn.configure(command=lambda: open_path(os.path.dirname(state["out"])))
    root.bind("<Return>", lambda e: start() if extract_btn["state"] != "disabled" else None)

    # The floor for the window comes from the layout itself, measured with the result row showing
    # (its tallest state), so nothing can be clipped however small the user drags it.
    show_results()
    root.update_idletasks()
    root.minsize(max(700, min(root.winfo_reqwidth(), 900)),
                 min(root.winfo_reqheight(), int(root.winfo_screenheight() * 0.85)))
    icd_extract.debug(f"_launch: minsize {root.winfo_reqwidth()}x{root.winfo_reqheight()}")
    hide_results()
    entry.focus_set()

    if accept_note:
        log_line(accept_note, "note")
    log_line("Reminder: this is not medical or legal advice, and no tool catches everything - "
             "check every result against the original record.  (Help > View terms of use)",
             "note")
    log_line("")

    root.mainloop()


if __name__ == "__main__":
    launch()
