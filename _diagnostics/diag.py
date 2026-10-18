"""PageWeaver diagnostics.

Measures where time and memory go when capturing a very long page.
It does NOT modify pageweaver.py: test T1 imports the program's own functions
unchanged; the other tests compare alternative ways of taking a section.

Results go to a run_<timestamp> folder next to this file:
  log.txt      - readable timeline
  memory.csv   - browser RAM and free system RAM every 0.5 s
  *.png        - sections captured during the tests
"""
import os
import platform
import sys
import threading
import time
import traceback
from datetime import datetime
from importlib.metadata import version as pkg_version
from pathlib import Path

import psutil
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "https://en.wikipedia.org/wiki/Japanese_conjugation"
WIDTH, SCALE, CHUNK = 1440, 2, 2000
SHOT_TIMEOUT_MS = 200_000          # alternative tests; the program itself uses 180 s
T1_CAPTURE_BUDGET_S = 6 * 60       # stop the exact-replica test after this much capture time
EMERGENCY_FREE_RAM_PCT = 7         # kill the test browser if free RAM falls below this

HERE = Path(__file__).resolve().parent
OUTDIR = HERE / f"run_{datetime.now():%Y%m%d_%H%M%S}"
OUTDIR.mkdir(parents=True, exist_ok=True)
LOG = open(OUTDIR / "log.txt", "w", encoding="utf-8")
T0 = time.time()

HEIGHT_JS = """() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight,
    document.body.offsetHeight, document.documentElement.offsetHeight,
    document.body.clientHeight, document.documentElement.clientHeight)"""

# Records viewport resizes and long rendering frames inside the page.
INSTRUMENT_JS = """() => {
    if (window.__pwdiag) return;
    window.__pwdiag = [];
    const t = () => Math.round(performance.now());
    addEventListener('resize', () => __pwdiag.push(`resize at ${t()}ms -> viewport ${innerWidth}x${innerHeight}`));
    try {
        new PerformanceObserver(list => {
            for (const e of list.getEntries()) {
                if (e.duration < 250) continue;
                const end = e.startTime + e.duration;
                const render = e.renderStart ? Math.round(end - e.renderStart) : 0;
                const scriptMs = Math.round((e.scripts || []).reduce((s, x) => s + x.duration, 0));
                const scripts = (e.scripts || []).filter(s => s.duration > 50)
                    .map(s => `${(s.sourceURL || s.invoker || '?').split('/').pop().slice(0, 50)} ${Math.round(s.duration)}ms`).join('; ');
                __pwdiag.push(`slow frame at ${Math.round(e.startTime)}ms: ${Math.round(e.duration)}ms (rendering ${render}ms, page scripts ${scriptMs}ms)` + (scripts ? ` [${scripts}]` : ''));
            }
        }).observe({type: 'long-animation-frame', buffered: false});
    } catch (err) { __pwdiag.push('long-animation-frame observer unsupported'); }
}"""


def log(msg=""):
    line = f"[{time.time() - T0:7.1f}s] {msg}"
    print(line, flush=True)
    LOG.write(line + "\n")
    LOG.flush()


def descendants():
    try:
        return psutil.Process(os.getpid()).children(recursive=True)
    except Exception:
        return []


def browser_rss_mb():
    total = 0
    for p in descendants():
        try:
            total += p.memory_info().rss
        except Exception:
            pass
    return total / 2**20


def kill_browsers():
    for p in descendants():
        try:
            name = (p.name() or "").lower()
            if "chrom" in name or "headless" in name:
                p.kill()
        except Exception:
            pass


class Monitor:
    def __init__(self):
        self.phase = "start"
        self.peak_rss = 0.0
        self.min_free = 100.0
        self.emergency = False
        self.csv = open(OUTDIR / "memory.csv", "w", encoding="utf-8")
        self.csv.write("t_s,phase,browser_rss_mb,free_ram_pct,free_ram_gb\n")
        threading.Thread(target=self.run, daemon=True).start()

    def reset(self, phase):
        self.phase = phase
        self.peak_rss = 0.0
        self.min_free = 100.0

    def summary(self):
        return f"browser RAM peak {self.peak_rss:,.0f} MB, lowest free system RAM {self.min_free:.0f}%"

    def run(self):
        while True:
            rss = browser_rss_mb()
            vm = psutil.virtual_memory()
            free = vm.available / vm.total * 100
            self.peak_rss = max(self.peak_rss, rss)
            self.min_free = min(self.min_free, free)
            self.csv.write(f"{time.time() - T0:.1f},{self.phase},{rss:.0f},{free:.1f},{vm.available / 2**30:.2f}\n")
            self.csv.flush()
            if free < EMERGENCY_FREE_RAM_PCT and not self.emergency:
                self.emergency = True
                log(f"!!! free system RAM fell to {free:.1f}% - killing the test browser to protect the computer")
                kill_browsers()
            time.sleep(0.5)


MON = Monitor()


def launch(pw, js=True):
    browser = pw.chromium.launch()
    context = browser.new_context(
        viewport={"width": WIDTH, "height": 900},
        device_scale_factor=SCALE,
        java_script_enabled=js,
    )
    page = context.new_page()
    page.set_default_timeout(30_000)
    page.set_default_navigation_timeout(60_000)
    page.on("crash", lambda *_: log("!!! the page's renderer process CRASHED"))
    return browser, context, page


def close(browser):
    try:
        browser.close()
    except Exception:
        pass
    kill_browsers()
    MON.emergency = False


def open_simple(page):
    t = time.time()
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    log(f"  opened (domcontentloaded) in {time.time() - t:.1f}s")
    t = time.time()
    try:
        page.wait_for_load_state("networkidle", timeout=120_000)
        log(f"  network idle after {time.time() - t:.1f}s")
    except PWTimeout:
        log("  network did NOT become idle within 120s")
    t = time.time()
    try:
        page.wait_for_function("document.fonts.status === 'loaded'", timeout=120_000)
        log(f"  fonts ready after {time.time() - t:.1f}s")
    except PWTimeout:
        log("  fonts NOT ready within 120s")
    height = int(page.evaluate(HEIGHT_JS))
    elements = page.evaluate("document.querySelectorAll('*').length")
    log(f"  page height {height:,} CSS px (= {height * SCALE:,} px at 2x), {elements:,} elements")
    return height


def page_events(page):
    """Read and clear the in-page event log without hanging if the page is stuck."""
    try:
        handle = page.wait_for_function(
            "() => JSON.stringify((window.__pwdiag || []).splice(0))", timeout=15_000)
        import json
        return json.loads(handle.json_value())
    except Exception as error:
        return [f"(could not read page events: {type(error).__name__})"]


def timed_shot(page, label, **options):
    MON.reset(label)
    t = time.time()
    try:
        page.screenshot(**options)
        result = "OK"
    except PWTimeout:
        result = "TIMEOUT"
    except Exception as error:
        result = f"ERROR {type(error).__name__}: {str(error).splitlines()[0][:200]}"
    elapsed = time.time() - t
    log(f"  {label}: {result} after {elapsed:.1f}s; {MON.summary()}")
    for event in page_events(page)[:25]:
        log(f"      page: {event}")
    return result, elapsed


def pageweaver_style_shot(page, offset, height, name, timeout=SHOT_TIMEOUT_MS):
    section = min(CHUNK, height - offset)
    return timed_shot(
        page, f"PageWeaver-style section at {offset:,}",
        path=str(OUTDIR / name), type="png", scale="device", full_page=True,
        clip={"x": 0, "y": offset, "width": WIDTH, "height": section}, timeout=timeout,
    )


# --------------------------------------------------------------------------- tests

def test_exact_replica(pw):
    """T1: the program's own functions, unchanged, in split mode."""
    log("T1. Exact PageWeaver run (split mode, program code unchanged), capped at "
        f"{T1_CAPTURE_BUDGET_S // 60} min of capturing")
    sys.path.insert(0, str(HERE.parent))
    try:
        import pageweaver as pwv
    except Exception as error:
        log(f"  could not import pageweaver.py: {error}")
        return

    class Budget(Exception):
        pass

    marks = {"start": None, "last": None, "stage": None}
    original_check = pwv.check_available_memory

    def section_done_line():
        if marks["last"] is not None:
            log(f"    {marks['stage']}: {time.time() - marks['last']:.1f}s; {MON.summary()}")

    def watched_check(stage):
        if stage.startswith("capturing section"):
            now = time.time()
            section_done_line()
            if marks["start"] is None:
                marks["start"] = now
            elif now - marks["start"] > T1_CAPTURE_BUDGET_S:
                marks["last"] = None
                raise Budget(stage)
            marks["last"], marks["stage"] = now, stage
            MON.reset(f"T1 {stage}")
        return original_check(stage)

    pwv.check_available_memory = watched_check

    browser, context, page = launch(pw)
    try:
        page.on("console", lambda *_: None)
        t = time.time()
        MON.reset("T1 open")
        page.goto(URL, wait_until="domcontentloaded", timeout=pwv.NAVIGATION_TIMEOUT_MS)
        log(f"  opened in {time.time() - t:.1f}s")
        t = time.time()
        MON.reset("T1 prepare (scroll pass)")
        height = pwv.prepare_page(page)
        log(f"  prepare_page (scroll through whole page + network wait) took {time.time() - t:.1f}s; "
            f"height {height:,} CSS px; {MON.summary()}")
        t = time.time()
        pwv.wait_for_page_fonts(page)
        log(f"  fonts wait {time.time() - t:.1f}s")
        page.evaluate(INSTRUMENT_JS)

        folder = OUTDIR / "T1_sections"
        folder.mkdir()
        t = time.time()
        try:
            paths, total = pwv.capture_sections(page, WIDTH, height, folder, True, "png")
            section_done_line()
            log(f"  T1 finished ALL {len(paths)} sections in {time.time() - t:.1f}s")
        except Budget as stop:
            done = len(list(folder.glob('*.png')))
            log(f"  T1 stopped by the diagnostic time budget before '{stop}'; "
                f"{done} sections done in {time.time() - t:.1f}s")
        except PWTimeout as error:
            section_done_line()
            done = len(list(folder.glob('*.png')))
            log(f"  T1 TIMEOUT after {done} finished sections, {time.time() - t:.1f}s: "
                f"{str(error).splitlines()[0][:200]}")
        except Exception as error:
            section_done_line()
            done = len(list(folder.glob('*.png')))
            log(f"  T1 ERROR after {done} sections: {type(error).__name__}: {str(error)[:300]}")
        log(f"  {MON.summary()}")
        for event in page_events(page)[:40]:
            log(f"      page: {event}")
    finally:
        pwv.check_available_memory = original_check
        close(browser)


def test_pageweaver_style(pw, js):
    label = "T2" if js else "T3"
    log(f"{label}. PageWeaver-style section screenshots (full_page=True + clip), "
        f"no scroll pass, JavaScript {'ON' if js else 'OFF'}")
    browser, context, page = launch(pw, js=js)
    try:
        MON.reset(f"{label} open")
        height = open_simple(page)
        page.evaluate(INSTRUMENT_JS) if js else None
        result, elapsed = pageweaver_style_shot(page, 0, height, f"{label}_offset_0.png")
        if result == "OK" and height > 62_000:
            pageweaver_style_shot(page, 60_000, height, f"{label}_offset_60000.png")
    except Exception as error:
        log(f"  {label} ERROR: {type(error).__name__}: {str(error)[:300]}")
    finally:
        close(browser)


def test_viewport_style(pw):
    log("T4. Alternative: viewport made 2000 px tall, page scrolled, normal (not full-page) screenshot")
    browser, context, page = launch(pw)
    try:
        MON.reset("T4 open")
        height = open_simple(page)
        page.evaluate(INSTRUMENT_JS)
        page.set_viewport_size({"width": WIDTH, "height": CHUNK})
        for offset in (0, 60_000, max(0, height - CHUNK)):
            page.evaluate("(y) => window.scrollTo(0, y)", offset)
            page.wait_for_timeout(300)
            real = page.evaluate("Math.round(window.scrollY)")
            timed_shot(page, f"viewport-style section at {offset:,} (scrollY={real:,})",
                       path=str(OUTDIR / f"T4_offset_{offset}.png"), type="png",
                       scale="device", timeout=SHOT_TIMEOUT_MS)
    except Exception as error:
        log(f"  T4 ERROR: {type(error).__name__}: {str(error)[:300]}")
    finally:
        close(browser)


def main():
    vm = psutil.virtual_memory()
    log("PageWeaver diagnostics")
    log(f"URL: {URL}")
    log(f"OS: {platform.platform()}; Python {platform.python_version()}; "
        f"CPU threads: {psutil.cpu_count()}; RAM: {vm.total / 2**30:.1f} GB total, "
        f"{vm.available / 2**30:.1f} GB free ({vm.available / vm.total * 100:.0f}%)")
    for pkg in ("playwright", "psutil", "pyvips"):
        try:
            log(f"{pkg} {pkg_version(pkg)}")
        except Exception:
            log(f"{pkg} not installed")
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        log(f"Chromium {b.version}")
        b.close()
        for test in (lambda: test_exact_replica(pw),
                     lambda: test_pageweaver_style(pw, js=True),
                     lambda: test_pageweaver_style(pw, js=False),
                     lambda: test_viewport_style(pw)):
            log()
            try:
                test()
            except Exception:
                log("  unexpected error:\n" + traceback.format_exc())
    log()
    log(f"Finished. Results folder: {OUTDIR}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
