"""Runs PageWeaver in all three modes on one page and logs the output with timings."""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROGRAM = HERE.parent / "pageweaver.py"
URL = sys.argv[1] if len(sys.argv) > 1 else "https://en.wikipedia.org/wiki/Japanese_conjugation"
LOG = open(HERE / f"modes_{datetime.now():%Y%m%d_%H%M%S}.txt", "w", encoding="utf-8")
T0 = time.time()


def out(line):
    text = f"[{time.time() - T0:7.1f}s] {line}"
    print(text, flush=True)
    LOG.write(text + "\n")
    LOG.flush()


out(f"URL: {URL}")
for mode in ("split", "full", "reopen"):
    out(f"=== mode {mode} ===")
    started = time.time()
    process = subprocess.Popen(
        [sys.executable, "-u", str(PROGRAM), URL, "1440", "png", mode],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", cwd=str(PROGRAM.parent),
    )
    for line in process.stdout:
        out("  " + line.rstrip())
    process.wait()
    out(f"=== mode {mode}: exit code {process.returncode}, {time.time() - started:.0f}s ===")
out("All modes finished.")
