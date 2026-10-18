# PageWeaver

PageWeaver captures a website or a local HTML file as a high-resolution image. It is designed for designers who need a webpage as a visual reference or want to place a long page into Figma.

PageWeaver opens the page in Chromium at the viewport width you choose and captures at **2x scale**. You can choose PNG or JPG output and one of three capture modes: one assembled image, numbered sections, or one assembled image captured with a fresh browser for every section.

## How a capture works

1. PageWeaver opens the URL or local HTML file in Chromium. The viewport width controls the website's responsive layout; for example, 1440 CSS pixels gives a 2880-pixel-wide output image at 2x.
2. It measures the page height, subject to a safety limit of 150,000 CSS pixels.
3. It captures the page in consecutive sections of up to 2,000 CSS pixels tall (4,000 output pixels at 2x). Each section starts exactly where the previous one ended. If the page ends partway through the last section, only the remaining page pixels are captured; no blank padding is added.
4. Depending on the selected mode, the sections are saved separately or joined vertically into one image.

The first section includes the page's top area. When PageWeaver detects fixed or sticky elements at the top, it hides them on later sections so they do not repeat when the sections are stacked. This is a visual heuristic; unusual headers may not be detected.

## The three capture modes

### 1. Full image (`full`)

Use this when you want one image containing the entire page.

- PageWeaver opens the site once and keeps the same browser page for the capture.
- Before capturing, it scrolls through the page to trigger lazy-loaded images and other scroll-triggered content, returns to the top, waits for network activity to settle, and waits for web fonts.
- It takes consecutive sections, each up to 4,000 output pixels tall, and writes them as temporary PNG files on disk.
- It joins those sections vertically with libvips, saves one PNG or JPG in `screenshots`, and removes the temporary files.

This avoids asking Chromium to create one enormous screenshot and keeps the image assembly section-based. The browser still has to load the page and its content, so this mode can still use substantial memory on very long or complex pages. The final image can also exceed Figma's image-size limit; choose Split sections for Figma imports that need pieces no taller than 4,000 pixels.

### 2. Split sections (`split`)

Use this when you want images that can be stacked in Figma or another editor.

- PageWeaver opens the site once and prepares the full page, including scrolling through it to trigger lazy-loaded content.
- It saves each section directly as a numbered PNG or JPG in its own folder inside `screenshots`, for example:

  ```text
  screenshots/example_com_1440px_<timestamp>/
  ├── 1.png
  ├── 2.png
  └── 3.png
  ```

- The folder uses the same page-and-width name that the full-image file would use, without the image extension. Numbering starts at 1 and continues in page order.
- Each image is at most 4,000 pixels tall. The final image contains only the page content remaining at the bottom and may be shorter.
- Sections are consecutive: the next section begins at the first pixel after the previous section, with no intentional gaps or overlap.

This mode writes finished sections as it goes. If a safety limit stops the capture, already completed section files are kept in the output folder.

### 3. Full image with browser restart (`reopen`)

Use this when a very long page causes the browser to run out of memory during a normal full-image capture. This mode still produces **one full-page image**; it does not produce a folder of final sections.

- For each section, PageWeaver launches a fresh Chromium browser and opens the page again.
- It scrolls near the section it needs to capture to trigger content in that region, waits for page activity and web fonts, then captures that section as a temporary PNG.
- It closes that browser and context before moving to the next section, which releases that browser session's resources.
- After all sections are captured, libvips joins the temporary PNGs vertically and writes one final PNG or JPG in `screenshots`. The temporary section files are removed afterward.

This mode is slower because it reloads the website and repeats the page and font waits for every section. It can reduce the memory retained by one long-running browser session, but it cannot guarantee that a page will fit within available memory. Since the page is loaded again for every section, live or changing content can shift between visits and create visible seams. PageWeaver stops if a page becomes too short or a section's dimensions change in a way that could leave gaps in the result.

## Choosing a mode

| Mode | Final output | Browser behavior | Best for |
| --- | --- | --- | --- |
| `full` | One PNG or JPG | One browser session; prepares the whole page | Regular full-page captures |
| `split` | A folder of numbered PNG or JPG sections | One browser session; prepares the whole page | Figma and other workflows that need sections up to 4,000 px tall |
| `reopen` | One PNG or JPG | Starts a new browser session for every section | Very long pages that need a full image and may benefit from releasing browser memory |

## Features

- Capture a website or a local HTML file.
- Choose a viewport width in CSS pixels, such as 1440 for desktop or 390 for mobile.
- Save as PNG or JPG.
- Render at 2x scale. A 1440 CSS-pixel viewport produces an image 2880 pixels wide.
- Save captures in a `screenshots` folder next to the app.

## Requirements

- Windows
- Python 3
- An internet connection for the initial installation of Python packages and Chromium

## Install

1. Install Python 3 if needed. During setup, enable **Add Python to PATH**.
2. Run `install.bat`. It installs Playwright, the libvips binary package, the RAM monitor, and Chromium.
3. Run `PageWeaver.bat`.

## Use it

Run `PageWeaver.bat` without arguments to answer the prompts for the URL or HTML file, viewport width, image format, and save mode. If you leave the URL blank, PageWeaver uses the Rome article on Wikipedia as an example.

You can also run it from a terminal:

```text
python pageweaver.py <url-or-html-file> [width] [png|jpg] [full|split|reopen]
```

Examples:

```text
python pageweaver.py https://example.com 1440 png full
python pageweaver.py https://example.com 1440 png split
python pageweaver.py https://example.com 390 jpg split
python pageweaver.py https://example.com 1440 png reopen
```

The width is the browser viewport width in CSS pixels and affects the website's responsive layout. The mode can also be selected in the interactive prompt as `1` (full), `2` (split), or `3` (reopen). If the mode is omitted from the command, PageWeaver uses `full`.

The same arguments can be passed to `PageWeaver.bat` on Windows.

## Limits and safeguards

- **Maximum page height:** 150,000 CSS pixels, equivalent to up to 300,000 output pixels at 2x. This limit helps stop runaway pages, such as unbounded infinite scroll.
- **Available memory:** capture stops if available system RAM falls below 15%. Completed Split sections remain on disk.
- **Navigation timeout:** 60 seconds for opening a page.
- **Page data wait:** up to 120 seconds for network activity to become idle. If the page remains active, capture continues with the content currently loaded.
- **Web font wait:** up to 120 seconds. If fonts have not loaded by then, capture stops before taking screenshots.
- **Section screenshot timeout:** 180 seconds per section, after the page preparation and font waits.
- **Browser cleanup:** browser contexts and processes are closed after success or failure. In Reopen mode, this happens after every section.

Long captures can take time and use disk space. Dynamic pages, blocked assets, animations, consent dialogs, and unusual layouts can affect the result. A full-page image may be too large for Figma even if capture succeeds; use Split sections when you need separate pieces.

## License

PageWeaver is licensed under the MIT License. See `LICENSE`.
