# pdf2html_safe

Convert PDFs into readable, semantic HTML while **safely handling repeated watermark/background images**.

This project is designed for legal, administrative, and scanned-style documents where repeated center-page seals/logos can hurt readability in extracted HTML.

## What this tool does

- Extracts text from each PDF page and preserves reading order as much as possible.
- Builds semantic HTML blocks (headings, paragraphs, tables, blockquotes, signature-like sections, and figure/image wrappers).
- Detects repeated image assets that look like center-page watermarks.
- Removes only those probable watermark images by default.
- Leaves all text content untouched.
- Writes a single standalone HTML file with embedded styling.

## How it works (high-level)

1. Open the PDF using **PyMuPDF**.
2. Scan image blocks across pages and compute image hashes.
3. Mark an image as a *watermark candidate* only when it is repeated across pages and matches positional/size heuristics.
4. Extract text lines/spans and reconstruct logical content rows.
5. Convert the page content into semantic HTML sections.
6. Embed a conversion summary in an HTML comment and save the output file.

If no watermark candidate is found, watermark-removal logic is skipped automatically.

## Requirements

- Python 3.9+
- [PyMuPDF](https://pymupdf.readthedocs.io/) (`fitz`) — required
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — optional (used for prettier/normalized HTML output)

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install pymupdf beautifulsoup4
```

If you only want required dependencies:

```bash
pip install pymupdf
```

## Usage

```bash
python pdf_to_html_safe.py <input.pdf> <output.html>
```

### Options

- `--keep-all-images`  
  Keep every image, even if watermark-like images are detected.
- `--page-scale <float>`  
  Scale factor from PDF points to CSS pixels. Default is `1.3333` (96/72).

### Examples

Default behavior (safe watermark skipping enabled):

```bash
python pdf_to_html_safe.py sample.pdf output/sample.html
```

Keep all images (disable watermark filtering effect):

```bash
python pdf_to_html_safe.py sample.pdf output/sample_keep_images.html --keep-all-images
```

Custom page scale:

```bash
python pdf_to_html_safe.py sample.pdf output/sample_scaled.html --page-scale 1.2
```

## Console output you should expect

After conversion, the script prints one of these statuses:

- `Watermark detected on pages [...]; removal logic applied safely during HTML conversion.`
- `Watermark detection completed, but --keep-all-images was used, so all images were retained.`
- `No watermark detected; watermark-removal logic was skipped.`

And then:

- `Created: <output path>`

## Notes and limitations

- The script does not modify your source PDF.
- Watermark detection is heuristic-based; unusual layouts may require `--keep-all-images`.
- Best results come from PDFs with an accessible text layer.
- For purely image-only PDFs, OCR is not included in this repository.
