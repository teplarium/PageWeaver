# PageWeaver

PageWeaver captures a full web page as one PNG or JPG image, or splits it into vertical sections for tools such as Figma.

## Windows setup

1. Install Python 3 if it is not already installed. Enable **Add Python to PATH** during setup.
2. Run **install.bat**. It installs Playwright and the Chromium browser.
3. Run **PageWeaver.bat** and answer the prompts.

## Usage

When started without arguments, PageWeaver asks for the website URL (or local HTML file), viewport width, image format, and save mode.

- **Single image** saves the entire page as one image.
- **Split sections** creates a folder containing numbered images such as 1.png, 2.png, and 3.png. Each full section is up to 4000 output pixels tall. The final section contains only the remaining page content.

The width is entered in CSS pixels. Images are captured at 2x scale, so a 1440-pixel viewport produces an image 2880 pixels wide. The viewport keeps its normal height while the script captures sections of the full page.

Images are saved in a screenshots folder next to the app. Split mode creates a subfolder named after the corresponding full-image filename.

## Command-line examples

    python pageweaver.py https://example.com 1440 png full
    python pageweaver.py https://example.com 1440 png split
    python pageweaver.py https://example.com 390 jpg split

Arguments are: website URL or HTML file path, viewport width, image format (png or jpg), and save mode (full or split). If the save mode is omitted, PageWeaver saves one image.

## License

PageWeaver is licensed under the MIT License. See LICENSE.