"""PageWeaver captures full web pages as images or numbered sections."""
import gc
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import psutil
import pyvips
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "screenshots"
SCALE = 2
MAX_CHUNK_HEIGHT = 4000
MAX_CHUNK_HEIGHT_CSS = MAX_CHUNK_HEIGHT // SCALE
MAX_PAGE_HEIGHT_CSS = 150_000
MIN_AVAILABLE_RAM_PERCENT = 15
NAVIGATION_TIMEOUT_MS = 60_000
PAGE_DATA_TIMEOUT_MS = 120_000
FONT_LOAD_TIMEOUT_MS = 120_000
SCREENSHOT_TIMEOUT_MS = 180_000
PRELOAD_STEP_DELAY_MS = 120
JPEG_QUALITY = 92

HEADER_MARKER = "data-pageweaver-hide-after-first-section"
HEADER_HIDE_STYLE = f"[{HEADER_MARKER}] {{ visibility: hidden !important; }}"


class SafetyStop(RuntimeError):
    """Raised when a configured resource guard stops a capture."""


def ask(question, default):
    answer = input(f"{question} [{default}]: ").strip()
    return answer or default


def check_available_memory(stage):
    memory = psutil.virtual_memory()
    available_percent = memory.available / memory.total * 100
    if available_percent < MIN_AVAILABLE_RAM_PERCENT:
        available_gib = memory.available / (1024 ** 3)
        raise SafetyStop(
            f"Available system memory is {available_percent:.1f}% "
            f"({available_gib:.1f} GiB) during {stage}. "
            f"PageWeaver stops below {MIN_AVAILABLE_RAM_PERCENT}%."
        )


def check_page_height(height):
    if height > MAX_PAGE_HEIGHT_CSS:
        raise SafetyStop(
            f"The page reached {height:,} CSS pixels tall, above the safety "
            f"limit of {MAX_PAGE_HEIGHT_CSS:,} CSS pixels. This may be an "
            "infinite-scroll page."
        )


def get_page_height(page):
    return int(page.evaluate("""() => Math.max(
        document.body.scrollHeight,
        document.documentElement.scrollHeight,
        document.body.offsetHeight,
        document.documentElement.offsetHeight,
        document.body.clientHeight,
        document.documentElement.clientHeight
    )"""))


def set_capture_scroll_styles(page):
    page.evaluate("""() => {
        document.documentElement.style.scrollBehavior = 'auto';
        document.documentElement.style.scrollSnapType = 'none';
        if (document.body) {
            document.body.style.scrollBehavior = 'auto';
            document.body.style.scrollSnapType = 'none';
        }
    }""")


def wait_for_page_data(page):
    print("Waiting up to 120 seconds for page data...")
    try:
        page.wait_for_load_state("networkidle", timeout=PAGE_DATA_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        print(
            "Page data did not become idle before the timeout; "
            "continuing with the content currently loaded."
        )


def wait_for_page_fonts(page):
    print("Waiting up to 120 seconds for web fonts...")
    try:
        page.wait_for_function(
            "document.fonts.status === 'loaded'",
            timeout=FONT_LOAD_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise SafetyStop(
            "Web fonts did not finish loading within 120 seconds; "
            "image capture was not started."
        ) from error
    print("Web fonts are ready.")


def prepare_page(page):
    set_capture_scroll_styles(page)
    viewport_height = int(page.evaluate("window.innerHeight"))
    scroll_step = max(300, min(800, int(viewport_height * 0.75)))
    height = get_page_height(page)
    check_page_height(height)

    # Scroll the full page once to trigger lazy-loaded images and reveals.
    y = 0
    while y < height:
        check_available_memory("preparing the page")
        page.evaluate("(position) => window.scrollTo(0, position)", y)
        page.wait_for_timeout(PRELOAD_STEP_DELAY_MS)
        height = get_page_height(page)
        check_page_height(height)
        y += scroll_step

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    wait_for_page_data(page)
    height = get_page_height(page)
    check_page_height(height)
    return height


def prepare_page_region(page, offset):
    """Load content near one section without preloading the entire page."""
    set_capture_scroll_styles(page)
    viewport_height = int(page.evaluate("window.innerHeight"))
    scroll_step = max(300, min(800, int(viewport_height * 0.75)))
    target_end = offset + MAX_CHUNK_HEIGHT_CSS
    height = get_page_height(page)
    check_page_height(height)
    y = max(0, offset - viewport_height)
    stable_bottom_steps = 0

    while True:
        check_available_memory("preparing a reopened page section")
        height = get_page_height(page)
        check_page_height(height)
        max_scroll = max(0, height - viewport_height)
        page.evaluate(
            "(position) => window.scrollTo(0, position)",
            min(y, max_scroll),
        )
        page.wait_for_timeout(PRELOAD_STEP_DELAY_MS)
        updated_height = get_page_height(page)
        check_page_height(updated_height)
        actual_scroll = int(round(page.evaluate("window.scrollY")))

        if actual_scroll + viewport_height >= target_end:
            break

        if actual_scroll + viewport_height >= updated_height:
            if updated_height <= height:
                stable_bottom_steps += 1
            else:
                stable_bottom_steps = 0
            if stable_bottom_steps >= 2:
                break
            y = actual_scroll + scroll_step
            page.wait_for_timeout(300)
            continue

        stable_bottom_steps = 0
        y = actual_scroll + scroll_step

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    wait_for_page_data(page)
    height = get_page_height(page)
    check_page_height(height)
    return height


def mark_persistent_top_headers(page):
    return int(page.evaluate("""() => {
        const marker = "data-pageweaver-hide-after-first-section";
        const roots = [document];
        let marked = 0;
        const topLimit = Math.min(180, window.innerHeight * 0.25);
        const maxHeaderHeight = Math.min(360, window.innerHeight * 0.45);

        for (let rootIndex = 0; rootIndex < roots.length; rootIndex++) {
            const root = roots[rootIndex];
            for (const element of root.querySelectorAll("*")) {
                if (element.shadowRoot)
                    roots.push(element.shadowRoot);

                const style = getComputedStyle(element);
                if (style.position !== "fixed" && style.position !== "sticky")
                    continue;

                const rect = element.getBoundingClientRect();
                const semanticHeader = element.matches("header, [role='banner']");
                if (rect.top < -5 || rect.top > topLimit || rect.bottom <= 0)
                    continue;
                if (rect.height < 24 || rect.height > maxHeaderHeight)
                    continue;
                if (!semanticHeader && rect.width < window.innerWidth * 0.4)
                    continue;

                let ancestor = element.parentElement;
                let parentAlreadyMarked = false;
                while (ancestor) {
                    if (ancestor.hasAttribute(marker)) {
                        parentAlreadyMarked = true;
                        break;
                    }
                    ancestor = ancestor.parentElement;
                }
                if (parentAlreadyMarked)
                    continue;

                element.setAttribute(marker, "");
                marked++;
            }
        }
        return marked;
    }"""))


def capture_sections(page, width, starting_height, target_folder, split, fmt):
    total_height = starting_height
    offset = 0
    index = 0
    header_count = 0
    paths = []
    extension = "jpg" if split and fmt == "jpeg" else "png"
    screenshot_type = fmt if split else "png"

    while offset < total_height:
        check_available_memory(f"capturing section {index + 1}")
        current_height = get_page_height(page)
        check_page_height(current_height)
        total_height = max(total_height, current_height)
        if offset >= total_height:
            break

        section_height = min(MAX_CHUNK_HEIGHT_CSS, total_height - offset)
        output_path = target_folder / f"{index + 1}.{extension}"
        options = {
            "path": str(output_path),
            "type": screenshot_type,
            "scale": "device",
            "full_page": True,
            "clip": {
                "x": 0,
                "y": offset,
                "width": width,
                "height": section_height,
            },
            "timeout": SCREENSHOT_TIMEOUT_MS,
        }
        if screenshot_type == "jpeg":
            options["quality"] = JPEG_QUALITY
        if index > 0 and header_count:
            options["style"] = HEADER_HIDE_STYLE

        print(
            f"Capturing section {index + 1} at vertical offset "
            f"{offset:,} CSS px...",
            end=" ",
            flush=True,
        )
        try:
            page.screenshot(**options)
        except Exception:
            output_path.unlink(missing_ok=True)
            print("failed")
            raise
        print("done")

        captured = pyvips.Image.new_from_file(str(output_path), access="sequential")
        captured_height = captured.height
        del captured
        gc.collect()
        if captured_height <= 0 or captured_height % SCALE:
            output_path.unlink(missing_ok=True)
            raise SafetyStop(
                "A captured section had an unexpected height; capture stopped "
                "to avoid gaps between sections."
            )

        paths.append(output_path)
        index += 1
        offset += captured_height // SCALE

        if index == 1:
            try:
                header_count = mark_persistent_top_headers(page)
                if header_count:
                    print(
                        f"Detected {header_count} fixed or sticky top "
                        "element(s); they will be hidden after the first section."
                    )
            except Exception:
                header_count = 0

        current_height = get_page_height(page)
        check_page_height(current_height)
        total_height = max(total_height, current_height)

    return paths, total_height


def capture_full_with_reentry(playwright, url, width, target_folder):
    mobile = width < 768
    paths = []
    offset = 0
    total_height = 0
    index = 0

    while index == 0 or offset < total_height:
        check_available_memory(f"starting section {index + 1}")
        print(f"Reopening the website for section {index + 1}...")
        browser = playwright.chromium.launch()
        context = None
        try:
            context = browser.new_context(
                viewport={"width": width, "height": 844 if mobile else 900},
                device_scale_factor=SCALE,
                is_mobile=mobile,
                has_touch=mobile,
            )
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )

            page_height = prepare_page_region(page, offset)
            wait_for_page_fonts(page)
            page_height = get_page_height(page)
            check_page_height(page_height)
            if offset >= page_height:
                if index == 0:
                    raise SafetyStop("The page has no capturable content.")
                raise SafetyStop(
                    "The page became shorter after reopening, before the next "
                    f"section at {offset:,} CSS pixels. Capture stopped to "
                    "avoid gaps in the final image."
                )

            if index and page_height != total_height:
                print(
                    "Warning: page height changed between visits "
                    f"({total_height:,} to {page_height:,} CSS px); "
                    "sections may not align perfectly."
                )
            total_height = page_height
            section_height = min(MAX_CHUNK_HEIGHT_CSS, total_height - offset)
            output_path = target_folder / f"{index + 1}.png"
            options = {
                "path": str(output_path),
                "type": "png",
                "scale": "device",
                "full_page": True,
                "clip": {
                    "x": 0,
                    "y": offset,
                    "width": width,
                    "height": section_height,
                },
                "timeout": SCREENSHOT_TIMEOUT_MS,
            }
            try:
                header_count = mark_persistent_top_headers(page)
            except Exception:
                header_count = 0
            if index > 0 and header_count:
                options["style"] = HEADER_HIDE_STYLE

            print(
                f"Capturing section {index + 1} at vertical offset "
                f"{offset:,} CSS px...",
                end=" ",
                flush=True,
            )
            try:
                page.screenshot(**options)
            except Exception:
                output_path.unlink(missing_ok=True)
                print("failed")
                raise
            print("done")

            captured = pyvips.Image.new_from_file(str(output_path), access="sequential")
            captured_height = captured.height
            del captured
            gc.collect()
            if captured_height != section_height * SCALE:
                output_path.unlink(missing_ok=True)
                raise SafetyStop(
                    "The page dimensions changed while capturing a section; "
                    "capture stopped to avoid gaps in the final image."
                )

            paths.append(output_path)
            offset += captured_height // SCALE
            index += 1
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass

    return paths, total_height


def assemble_full_image(chunk_paths, output_path, fmt):
    check_available_memory("assembling the full image")
    image = None
    try:
        for chunk_path in chunk_paths:
            check_available_memory("assembling the full image")
            chunk = pyvips.Image.new_from_file(str(chunk_path), access="sequential")
            if image is None:
                image = chunk
            else:
                if chunk.width != image.width:
                    raise RuntimeError(
                        "Captured sections have different widths; "
                        "the full image cannot be assembled safely."
                    )
                image = image.join(chunk, "vertical", expand=True)
            del chunk
            gc.collect()

        if image is None:
            raise RuntimeError("No sections were captured.")

        width, height = image.width, image.height
        if fmt == "jpeg":
            image.write_to_file(str(output_path), Q=JPEG_QUALITY)
        else:
            image.write_to_file(str(output_path))
        return width, height
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        image = None
        gc.collect()


def make_output_name(url, width, extension):
    safe_name = re.sub(
        r"[^\w]+",
        "_",
        unquote(re.sub(r"^[a-z]+://", "", url)).replace("index.html", ""),
    ).strip("_")[-60:] or "page"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return safe_name, OUT / f"{safe_name}_{width}px_{timestamp}.{extension}"


def main():
    args = sys.argv[1:]
    if args:
        url = args[0]
        width = int(args[1]) if len(args) > 1 else 1440
        fmt = (args[2] if len(args) > 2 else "png").lower()
        mode = (args[3] if len(args) > 3 else "full").lower()
    else:
        url = ask(
            "Website URL or path to an HTML file",
            "https://en.wikipedia.org/wiki/Rome",
        )
        width = int(
            ask("Viewport width in CSS pixels (1440 desktop, 390 phone)", "1440")
        )
        fmt = ask("Image format: png or jpg", "png").lower()
        mode = ask(
            "Save mode: 1 - single image, 2 - split, "
            "3 - full image with browser restart per section",
            "1",
        ).lower()

    if mode in ("1", "full", "single"):
        split = False
        reopen = False
    elif mode in ("2", "split", "chunks"):
        split = True
        reopen = False
    elif mode in ("3", "reopen", "restart", "full-restart"):
        split = False
        reopen = True
    else:
        raise ValueError("Save mode must be 1/full, 2/split, or 3/reopen")

    if width <= 0:
        raise ValueError("Viewport width must be a positive number.")
    if fmt not in ("png", "jpg", "jpeg"):
        raise ValueError("Image format must be png or jpg.")
    fmt = "jpeg" if fmt in ("jpg", "jpeg") else "png"

    local_path = Path(url)
    if local_path.exists():
        url = local_path.resolve().as_uri()
    elif not re.match(r"^[a-z]+://", url):
        url = "https://" + url

    extension = "jpg" if fmt == "jpeg" else "png"
    safe_name, output_file = make_output_name(url, width, extension)
    output_folder = output_file.with_suffix("") if split else None
    mobile = width < 768
    OUT.mkdir(parents=True, exist_ok=True)

    try:
        check_available_memory("starting capture")
        with sync_playwright() as playwright:
            if reopen:
                with tempfile.TemporaryDirectory(
                    prefix=f"{safe_name}_chunks_",
                    dir=OUT,
                ) as temp_folder:
                    chunk_paths, _ = capture_full_with_reentry(
                        playwright,
                        url,
                        width,
                        Path(temp_folder),
                    )
                    output_width, output_height = assemble_full_image(
                        chunk_paths,
                        output_file,
                        fmt,
                    )
                print(
                    f"Done: {output_file} "
                    f"({output_width} x {output_height} px; "
                    "browser restarted for each section)"
                )
            else:
                browser = playwright.chromium.launch()
                context = None
                try:
                    context = browser.new_context(
                        viewport={
                            "width": width,
                            "height": 844 if mobile else 900,
                        },
                        device_scale_factor=SCALE,
                        is_mobile=mobile,
                        has_touch=mobile,
                    )
                    page = context.new_page()
                    page.set_default_timeout(30_000)
                    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
                    print("Opening", url)
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    page_height = prepare_page(page)
                    wait_for_page_fonts(page)

                    if split:
                        output_folder.mkdir(parents=True, exist_ok=False)
                        chunk_paths, page_height = capture_sections(
                            page,
                            width,
                            page_height,
                            output_folder,
                            True,
                            fmt,
                        )
                        if not chunk_paths:
                            raise SafetyStop("No sections were captured.")
                        print(
                            f"Done: {output_folder} "
                            f"({width * SCALE} x {page_height * SCALE} px total, "
                            f"{len(chunk_paths)} sections; maximum "
                            f"{MAX_CHUNK_HEIGHT} px per section)"
                        )
                    else:
                        with tempfile.TemporaryDirectory(
                            prefix=f"{safe_name}_chunks_",
                            dir=OUT,
                        ) as temp_folder:
                            chunk_paths, _ = capture_sections(
                                page,
                                width,
                                page_height,
                                Path(temp_folder),
                                False,
                                fmt,
                            )
                            output_width, output_height = assemble_full_image(
                                chunk_paths,
                                output_file,
                                fmt,
                            )
                        print(
                            f"Done: {output_file} "
                            f"({output_width} x {output_height} px)"
                        )
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass
                    try:
                        browser.close()
                    except Exception:
                        pass

    except SafetyStop as error:
        print(f"\nStopped safely: {error}")
        if split and output_folder is not None and output_folder.exists():
            completed = sorted(
                path for path in output_folder.iterdir() if path.is_file()
            )
            if completed:
                print(
                    f"{len(completed)} completed section(s) remain in: "
                    f"{output_folder}"
                )
            else:
                output_folder.rmdir()
    except PlaywrightTimeoutError as error:
        print(f"\nTimed out: {error}")
        if split and output_folder is not None and output_folder.exists():
            completed = sorted(
                path for path in output_folder.iterdir() if path.is_file()
            )
            if completed:
                print(
                    f"{len(completed)} completed section(s) remain in: "
                    f"{output_folder}"
                )


if __name__ == "__main__":
    main()
