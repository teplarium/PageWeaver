# PageWeaver

PageWeaver turns a website into a high-resolution image of its full scrollable page. It is made for designers who need a quick visual reference or want to bring a webpage into Figma without taking and stitching screenshots by hand.

PageWeaver opens the page in Chromium at a viewport width you choose, then captures it at 2x scale. You can save the result as one tall image or as numbered vertical sections.

## What it can do

- Capture a website or a local HTML file.
- Choose a viewport width in CSS pixels, such as 1440 for desktop or 390 for mobile.
- Save as PNG or JPG.
- Export the whole page as one image, or split it into numbered images up to 4000 output pixels tall each.
- Save captures in a `screenshots` folder next to the app.

In split mode, each section continues where the previous one ends. The last section contains only the page content that remains, so it can be shorter than 4000 pixels.

## Requirements

- Windows
- Python 3
- An internet connection for the initial installation of Playwright and Chromium

## Install

1. Install Python 3 if needed. During setup, enable **Add Python to PATH**.
2. Run `install.bat` and wait for it to finish installing the dependencies and Chromium.
3. Run `PageWeaver.bat`.

## Use it

Run `PageWeaver.bat` without arguments to answer the prompts for the URL or HTML file, viewport width, image format, and save mode. If you leave the URL blank, PageWeaver uses the Rome article on Wikipedia as an example.

You can also run it from a terminal:

```text
python pageweaver.py <url-or-html-file> [width] [png|jpg] [full|split]
```

Examples:

```text
python pageweaver.py https://example.com 1440 png full
python pageweaver.py https://example.com 1440 png split
python pageweaver.py https://example.com 390 jpg split
```

The width is the browser viewport width in CSS pixels. Images are captured at 2x scale, so a 1440-pixel viewport produces an image that is 2880 pixels wide. The selected width also affects the website's responsive layout.

- `full` saves one image of the entire page.
- `split` saves a folder of numbered images, such as `1.png`, `2.png`, and `3.png`.
- If the save mode is omitted, PageWeaver saves one full-page image.

The same arguments can be passed to `PageWeaver.bat` on Windows.

## Very long pages

A single full-page image can require a lot of memory. Screenshot capture is allowed up to 90 seconds, but extremely tall pages may still fail if the browser or computer cannot process the image. For long pages, try split mode.

## License

PageWeaver is licensed under the MIT License. See [LICENSE](LICENSE).