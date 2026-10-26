"""The window's status line after a run shows the entry count, not a bare "Done." (2026-10-26).

The report has said "clinical record entry line(s)" since v3.0, but the window still looked for
"diagnosis line(s)", so every finished run reported only "Done.". Builds the real window with
the consent gate and mainloop stubbed, runs a synthetic record through Extract, reads the status.
"""

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "source"))
import icd_disclaimer as D  # noqa: E402
import icd_gui as G  # noqa: E402


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


def find(root, cls, text=None):
    return next((w for w in walk(root) if w.winfo_class() == cls
                 and (text is None or str(w.cget("text")) == text)), None)


class _InlineThread:
    """Runs the worker on the calling thread; with mainloop stubbed, a real thread's root.after()
    calls fail ("main thread is not in main loop")."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._run = lambda: target(*args, **(kwargs or {}))

    def start(self):
        self._run()


class StatusLine(unittest.TestCase):
    def test_status_shows_entry_count(self):
        tsv = os.path.join(os.environ.get("ICD_APP") or os.path.join(REPO, "app"), "icd10_data.tsv")
        if not os.path.isfile(tsv):
            self.skipTest("icd10_data.tsv not in app/ (set ICD_APP)")
        d = tempfile.mkdtemp()
        shutil.copy2(tsv, d)
        keep_argv, keep_thread, keep_gate = sys.argv[0], G.threading.Thread, D.require_acceptance
        real_tk, real_loop = tk.Tk, tk.Tk.mainloop
        sys.argv[0] = os.path.join(d, "icd_extract_gui.exe")
        G.threading.Thread = _InlineThread
        D.require_acceptance = lambda parent, version: (True, None)
        roots = []
        tk.Tk.mainloop = lambda self: None
        tk.Tk = lambda *a, **k: roots.append(real_tk(*a, **k)) or roots[-1]
        try:
            try:
                G.launch()
            except tk.TclError:
                self.skipTest("no display")
            finally:
                tk.Tk, tk.Tk.mainloop = real_tk, real_loop
            win = roots[-1]
            rec = os.path.join(d, "record.txt")
            with open(rec, "w", encoding="utf-8") as f:
                f.write("Visit Diagnoses\nM20.41 Other hammer toe(s) (acquired), right foot\n"
                        "I10 Essential (primary) hypertension\n")
            entry = find(win, "TEntry")
            entry.delete(0, "end")
            entry.insert(0, rec)
            find(win, "TButton", "Extract").invoke()
            copy = find(win, "TButton", "Copy ICD-10 codes")
            end = time.time() + 30
            while not copy.winfo_ismapped() and time.time() < end:
                win.update()
                time.sleep(0.05)
            labels = [str(w.cget("text")) for w in walk(win) if w.winfo_class() == "TLabel"]
            status = next((s for s in labels if "clinical record entry line(s)" in s), "")
            self.assertTrue(status.startswith("2 clinical record entry line(s)"), labels)
            self.assertIn("2 ICD-10 code(s).", status)
            win.destroy()
        finally:
            sys.argv[0], G.threading.Thread, D.require_acceptance = keep_argv, keep_thread, keep_gate
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
