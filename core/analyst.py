"""The agent's computer: the model writes Python, runs it in the owner's files folder, reads the output and iterates.
Owner only.

- Every file the owner sends (any type) is kept in data/files/<user>/ (last 15), so follow-ups work on the same files.
- run_python runs code in a separate process inside that folder: pandas, numpy, matplotlib, python-docx, python-pptx,
  openpyxl, PyMuPDF, Pillow; a 90-second limit and no access to the agent's secrets (empty environment).
- Charts are sent as images; any file the code creates or changes in the folder is sent as a document.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

from core import context

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "files")
TIMEOUT = int(os.getenv("ANALYSIS_TIMEOUT", "90"))
KEEP = 15

PRELUDE = r'''
import os, sys, warnings
warnings.filterwarnings("ignore")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
pd.set_option("display.width", 160); pd.set_option("display.max_columns", 40); pd.set_option("display.max_rows", 80)
plt.rcParams.update({"figure.figsize": (9, 5), "figure.dpi": 130, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.alpha": .3, "font.size": 10})
_CHARTS = os.environ["CHART_DIR"]
_n = [0]
def show(title=None):
    """Save the current figure as a chart for the user."""
    _n[0] += 1
    if title: plt.title(title)
    plt.tight_layout()
    plt.savefig(os.path.join(_CHARTS, f"chart_{_n[0]:02d}.png")); plt.close()
plt.show = lambda *a, **k: show()
'''

EPILOGUE = r'''
for _f in plt.get_fignums():
    plt.figure(_f); show()
'''


def user_dir(uid: str) -> str:
    d = os.path.join(ROOT, uid.replace(":", "_"))
    os.makedirs(d, exist_ok=True)
    return d


def keep_file(uid: str, src: str, name: str) -> str:
    """Copy an uploaded file into the user's analysis folder; returns its file name there."""
    d = user_dir(uid)
    safe = "".join(c for c in name if c.isalnum() or c in "._- ").strip() or f"file_{int(time.time())}"
    shutil.copy(src, os.path.join(d, safe))
    files = sorted(glob.glob(os.path.join(d, "*")), key=os.path.getmtime)
    for old in files[:-KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return safe


def list_files(uid: str) -> str:
    fs = sorted(glob.glob(os.path.join(user_dir(uid), "*")), key=os.path.getmtime, reverse=True)
    return "\n".join(f"{os.path.basename(f)} ({os.path.getsize(f) // 1024} KB)" for f in fs) or "No files yet."


def run_python(code: str) -> str:
    c = context.get()
    if not c.is_owner:
        return "Data analysis is private to the bot owner."
    work = user_dir(c.uid)
    charts = tempfile.mkdtemp(prefix="charts_")
    script = os.path.join(charts, "analysis.py")
    with open(script, "w") as f:
        f.write(PRELUDE + "\n" + code + "\n" + EPILOGUE)
    before = {f: os.path.getmtime(f) for f in glob.glob(os.path.join(work, "*"))}
    env = {"PATH": os.environ.get("PATH", ""), "HOME": work, "CHART_DIR": charts, "MPLBACKEND": "Agg",
           "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run([sys.executable, "-I", script], cwd=work, env=env, capture_output=True, text=True, timeout=TIMEOUT)
        out = (r.stdout or "") + (("\nERROR:\n" + r.stderr[-3000:]) if r.returncode else "")
    except subprocess.TimeoutExpired:
        out = f"Stopped: the code ran longer than {TIMEOUT} seconds. Use a smaller sample or simpler steps."
    pngs = sorted(glob.glob(os.path.join(charts, "*.png")))
    for p in pngs[:8]:
        c.outbox.append({"kind": "photo", "path": p})
    made = [f for f in glob.glob(os.path.join(work, "*"))
            if os.path.isfile(f) and (f not in before or os.path.getmtime(f) > before[f])]
    for f in made[:6]:     # files the code created or changed (Word, PowerPoint, Excel, PDF, CSV, images, zip...)
        if os.path.getsize(f) > 45 * 1024 * 1024:
            continue
        tmp = os.path.join(tempfile.mkdtemp(prefix="out_"), os.path.basename(f))
        shutil.copy(f, tmp)
        c.outbox.append({"kind": "document", "path": tmp, "caption": os.path.basename(f)})
    note = (f"\n[{len(pngs)} chart(s) will be sent to the user]" if pngs else "") + \
           (f"\n[files sent to the user: {', '.join(os.path.basename(f) for f in made[:6])}]" if made else "")
    out = out.strip() or "(no output: print() the results you need to see)"
    return (out[:9000] + ("\n...(output cut)" if len(out) > 9000 else "")) + note
