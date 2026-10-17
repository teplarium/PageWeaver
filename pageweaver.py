"""PageWeaver captures full web pages as one image or as vertical sections.
The viewport width is configurable; the page height is detected automatically.

Run without arguments to enter the URL, width, format, and save mode.
Examples:
    python pageweaver.py https://example.com 1440 png full
    python pageweaver.py file:///D:/site/index.html 390 jpg split

Images are saved in the screenshots folder next to this script.
"""
import sys, re, time
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "screenshots"
SCALE = 2
MAX_CHUNK_HEIGHT = 4000  # output image height; at SCALE=2 this is 2000 CSS pixels


def ask(q, default):
    a = input(f"{q} [{default}]: ").strip()
    return a or default


def main():
    args = sys.argv[1:]
    if args:
        url = args[0]
        width = int(args[1]) if len(args) > 1 else 1440
        fmt = (args[2] if len(args) > 2 else "png").lower()
        mode = (args[3] if len(args) > 3 else "full").lower()
    else:
        url = ask("Website URL or path to an HTML file", "https://en.wikipedia.org/wiki/Rome")
        width = int(ask("Viewport width in CSS pixels (1440 desktop, 390 phone)", "1440"))
        fmt = ask("Image format: png or jpg", "png").lower()
        mode = ask("Save mode: 1 — single image, 2 — split into sections up to 4000 px", "1").lower()

    if mode in ("1", "full", "single"):
        split = False
    elif mode in ("2", "split", "chunks"):
        split = True
    else:
        raise ValueError("Save mode must be 1/full or 2/split")

    fmt = "jpeg" if fmt in ("jpg", "jpeg") else "png"
    p = Path(url)
    if p.exists():  # Local file
        url = p.resolve().as_uri()
    elif not re.match(r"^[a-z]+://", url):
        url = "https://" + url

    mobile = width < 768
    OUT.mkdir(exist_ok=True)
    from urllib.parse import unquote
    name = re.sub(r"[^\w]+", "_", unquote(re.sub(r"^[a-z]+://", "", url)).replace("index.html", "")).strip("_")[-60:]
    file = OUT / f"{name}_{width}px_{datetime.now():%Y%m%d_%H%M%S}.{'jpg' if fmt == 'jpeg' else 'png'}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport={"width": width, "height": 900 if not mobile else 844},
            device_scale_factor=SCALE,    # High-resolution output
            is_mobile=mobile, has_touch=mobile,
        )
        print("Opening", url)
        page.goto(url, wait_until="networkidle", timeout=60000)

        # Scroll the page to load lazy images and trigger reveal animations
        page.evaluate("""async () => {
            document.documentElement.style.scrollBehavior = 'auto';
            document.querySelectorAll('img[loading=lazy]').forEach(i => i.loading = 'eager');
            for (let y = 0; y < document.documentElement.scrollHeight; y += 300) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 120));
            }
        }""")
        time.sleep(2)  # Let counters and animations finish
        page.evaluate("window.scrollTo(0, 0)")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(1)

        h = page.evaluate("""() => Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight,
            document.body.offsetHeight,
            document.documentElement.offsetHeight,
            document.body.clientHeight,
            document.documentElement.clientHeight
        )""")
        if split:
            folder = file.with_suffix("")
            folder.mkdir(exist_ok=True)
            css_chunk_height = MAX_CHUNK_HEIGHT // SCALE
            count = (h + css_chunk_height - 1) // css_chunk_height
            for index in range(count):
                y = index * css_chunk_height
                chunk_height = min(css_chunk_height, h - y)
                page.screenshot(
                    path=str(folder / f"{index + 1}.{'jpg' if fmt == 'jpeg' else 'png'}"),
                    type=fmt,
                    full_page=True,
                    clip={"x": 0, "y": y, "width": width, "height": chunk_height},
                    scale="device",
                    **({"quality": 92} if fmt == "jpeg" else {}),
                )
        else:
            opts = {"path": str(file), "full_page": True, "type": fmt, "scale": "device"}
            if fmt == "jpeg":
                opts["quality"] = 92
            page.screenshot(**opts)
        browser.close()

    if split:
        print(f"Done: {folder}  ({width * SCALE} x {h * SCALE} px total, {count} sections, up to {MAX_CHUNK_HEIGHT} px each)")
    else:
        print(f"Done: {file}  ({width} x {h} CSS px, image is 2x)")


if __name__ == "__main__":
    main()
