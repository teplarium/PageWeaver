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
TILE_SETTLE_MS = 100
JPEG_QUALITY = 92
MAX_JPEG_DIMENSION = 65_500

# Each section is captured from window-sized screenshots taken while scrolling
# the page in its normal browser window. Asking Chromium for a "full page"
# screenshot per section instead made it re-render the whole page for every
# section, which hung on very long pages.
PIN_STICKY_AT_TOP_JS = """() => {
    // Sticky elements move while the page scrolls. Pin each one where it is
    // drawn when the page is at the top, so it appears once, in that place.
    const marker = "data-pageweaver-sticky";
    window.scrollTo(0, 0);
    const roots = [document];
    let pinned = 0;
    for (let rootIndex = 0; rootIndex < roots.length; rootIndex++) {
        for (const element of roots[rootIndex].querySelectorAll("*")) {
            if (element.shadowRoot)
                roots.push(element.shadowRoot);
            if (element.hasAttribute(marker))
                continue;
            if (getComputedStyle(element).position !== "sticky")
                continue;
            const stuck = element.getBoundingClientRect();
            element.setAttribute(marker, "");
            element.style.setProperty("position", "relative", "important");
            element.style.setProperty("inset", "auto", "important");
            const normal = element.getBoundingClientRect();
            element.style.setProperty("top", `${stuck.top - normal.top}px`, "important");
            element.style.setProperty("left", `${stuck.left - normal.left}px`, "important");
            pinned++;
        }
    }
    return pinned;
}"""
PREPARE_TILE_JS = """(hideFixed) => {
    // Fixed elements belong to the first screen of the page only; hide them
    // once the page is scrolled. Elements that turn sticky or fixed while
    // scrolling are handled here as well.
    const fixedMarker = "data-pageweaver-fixed";
    const stickyMarker = "data-pageweaver-sticky";
    const hide = (element) => {
        element.style.setProperty("visibility", "hidden", "important");
        element.style.setProperty("opacity", "0", "important");
    };
    const roots = [document];
    for (let rootIndex = 0; rootIndex < roots.length; rootIndex++) {
        for (const element of roots[rootIndex].querySelectorAll("*")) {
            if (element.shadowRoot)
                roots.push(element.shadowRoot);
            if (element.hasAttribute(stickyMarker)) {
                if (element.style.getPropertyValue("position") !== "relative")
                    element.style.setProperty("position", "relative", "important");
                continue;
            }
            if (element.hasAttribute(fixedMarker)) {
                if (hideFixed)
                    hide(element);
                continue;
            }
            const position = getComputedStyle(element).position;
            if (position === "sticky") {
                element.setAttribute(stickyMarker, "");
                element.style.setProperty("position", "relative", "important");
                element.style.setProperty("inset", "auto", "important");
            } else if (position === "fixed") {
                element.setAttribute(fixedMarker, "");
                if (hideFixed)
                    hide(element);
            }
        }
    }
}"""
SCROLL_TO_JS = """(position) => {
    window.scrollTo(0, position);
    return {
        scrollY: window.scrollY,
        height: window.innerHeight,
        scale: window.visualViewport ? window.visualViewport.scale : 1,
    };
}"""
WAIT_FOR_FRAME_JS = """() => new Promise((resolve) => {
    const timer = setTimeout(resolve, 1000);
    requestAnimationFrame(() => requestAnimationFrame(() => {
        clearTimeout(timer);
        resolve();
    }));
})"""


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


def pin_sticky_elements(page):
    """Fix sticky elements where they appear at the top of the page."""
    return int(page.evaluate(PIN_STICKY_AT_TOP_JS))


def save_image(image, output_path, fmt):
    if fmt == "jpeg":
        if image.hasalpha():
            image = image.flatten(background=[255, 255, 255])
        image.write_to_file(str(output_path), Q=JPEG_QUALITY)
    else:
        image.write_to_file(str(output_path))


def capture_region(page, width, offset, height, output_path, fmt):
    """Capture one section by scrolling the page in its normal browser window.

    The section is made from window-sized screenshots stitched together, so the
    window keeps its real size (blocks sized to the window keep their height)
    and Chromium only renders what is in the window.
    Returns the captured height in CSS pixels.
    """
    end = offset + height
    tiles = []
    y = offset

    while y < end:
        view = page.evaluate(SCROLL_TO_JS, y)
        if abs(view["scale"] - 1) > 0.001:
            raise SafetyStop(
                "The page is zoomed out in the browser window (it has no layout "
                f"for a {width}-pixel-wide screen), so it cannot be captured in "
                "sections at this width. Try a desktop width such as 1440."
            )
        scroll_y = view["scrollY"]
        top = y - scroll_y
        if top < 0 or top >= view["height"]:
            raise SafetyStop(
                f"The page could not be scrolled to {y:,} CSS pixels. It may "
                "scroll inside an inner area instead of the browser window, or "
                "it became shorter during capture."
            )

        page.evaluate(PREPARE_TILE_JS, scroll_y > 0)
        page.evaluate(WAIT_FOR_FRAME_JS)
        page.wait_for_timeout(TILE_SETTLE_MS)

        tile_height = min(view["height"] - top, end - y)
        data = page.screenshot(
            type="png",
            scale="device",
            clip={"x": 0, "y": top, "width": width, "height": tile_height},
            timeout=SCREENSHOT_TIMEOUT_MS,
        )
        tile = pyvips.Image.new_from_buffer(data, "")
        if tile.height != round(tile_height * SCALE):
            raise SafetyStop(
                "A screenshot had an unexpected height; capture stopped to avoid "
                "gaps between sections."
            )
        tiles.append(tile)
        y += tile_height

    image = tiles[0]
    for tile in tiles[1:]:
        image = image.join(tile, "vertical")
    if image.width != round(width * SCALE) or image.height != round(height * SCALE):
        raise SafetyStop(
            "A captured section had unexpected dimensions; capture stopped to "
            "avoid gaps between sections."
        )
    save_image(image, output_path, fmt)
    del image, tiles
    gc.collect()
    return height


def capture_sections(page, width, starting_height, target_folder, split, fmt):
    total_height = starting_height
    offset = 0
    index = 0
    paths = []
    extension = "jpg" if split and fmt == "jpeg" else "png"
    section_format = fmt if split else "png"
    pin_sticky_elements(page)

    while offset < total_height:
        check_available_memory(f"capturing section {index + 1}")
        current_height = get_page_height(page)
        check_page_height(current_height)
        total_height = max(total_height, current_height)
        if offset >= total_height:
            break

        section_height = min(MAX_CHUNK_HEIGHT_CSS, total_height - offset)
        output_path = target_folder / f"{index + 1}.{extension}"
        print(
            f"Capturing section {index + 1} at vertical offset "
            f"{offset:,} CSS px...",
            end=" ",
            flush=True,
        )
        try:
            captured_height = capture_region(
                page, width, offset, section_height, output_path, section_format
            )
        except Exception:
            output_path.unlink(missing_ok=True)
            print("failed")
            raise
        print("done")

        paths.append(output_path)
        index += 1
        offset += captured_height

        current_height = get_page_height(page)
        check_page_height(current_height)
        total_height = max(total_height, current_height)

    return paths, total_height


def check_jpeg_height(height_css, fmt):
    """A single JPG image cannot be taller than about 65,500 pixels."""
    if fmt == "jpeg" and height_css * SCALE > MAX_JPEG_DIMENSION:
        raise SafetyStop(
            f"The full page would be {height_css * SCALE:,} pixels tall, but a "
            f"JPG image can be at most {MAX_JPEG_DIMENSION:,} pixels tall. "
            "Choose PNG, or the Split sections mode for JPG."
        )


def capture_full_with_reentry(playwright, url, width, target_folder, fmt):
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
            pin_sticky_elements(page)
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
            if index == 0:
                check_jpeg_height(total_height, fmt)
            section_height = min(MAX_CHUNK_HEIGHT_CSS, total_height - offset)
            output_path = target_folder / f"{index + 1}.png"

            print(
                f"Capturing section {index + 1} at vertical offset "
                f"{offset:,} CSS px...",
                end=" ",
                flush=True,
            )
            try:
                captured_height = capture_region(
                    page, width, offset, section_height, output_path, "png"
                )
            except Exception:
                output_path.unlink(missing_ok=True)
                print("failed")
                raise
            print("done")

            paths.append(output_path)
            offset += captured_height
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
        if fmt == "jpeg" and height > MAX_JPEG_DIMENSION:
            raise SafetyStop(
                f"The full page is {height:,} pixels tall, but a JPG image can "
                f"be at most {MAX_JPEG_DIMENSION:,} pixels tall. Choose PNG, or "
                "the Split sections mode for JPG."
            )
        save_image(image, output_path, fmt)
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
                        fmt,
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
                    if not split:
                        check_jpeg_height(page_height, fmt)

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
