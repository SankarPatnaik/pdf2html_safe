# pdf2html_safe

Convert PDFs into semantic HTML with **document inspection, structural classification, strategy routing, and safe watermark handling**.

## What changed

The conversion flow now runs a document-understanding pass before rendering:

1. **Inspect (`pdf_inspector`)**: Collect page-level structure signals (text density, headings, bullets, table-like alignment, image density, repeated headers/footers).
2. **Classify (`pdf_classifier`)**: Categorize document layout (e.g. `legal_judgment`, `table_heavy`, `bullet_heavy`, `mixed_text_image`, `scanned_or_image_heavy`, `watermark_background_pdf`).
3. **Analyze and reconstruct (`pdf_to_html_safe`)**: Preserve reading order, reconstruct paragraphs/lists/tables/images, and avoid destructive filtering.
4. **Watermark filtering (`pdf_to_html_safe`)**: Remove only repeated center-overlay image layers when confidence is high.
5. **Render by approved template (`template_renderer`)**: Reuse the style shell from the approved HTML template file.

This keeps the existing implementation but strengthens routing and structure-preservation behavior.

## Key guarantees

- Content-preserving extraction (no paraphrasing/summarization).
- Pre-conversion structural inspection and category detection.
- Strategy routing influenced by detected document category and watermark status.
- Safer signature-stamp removal (cluster-based, avoids dropping isolated legal text).
- Output HTML shell/CSS aligned to approved template.

## Requirements

- Python 3.9+
- [PyMuPDF](https://pymupdf.readthedocs.io/) (`fitz`) — required
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — optional (for prettified HTML)

## Usage

```bash
python pdf_to_html_safe.py <input.pdf> <output.html>
```

### Options

- `--keep-all-images` — keep all images (disables watermark image skipping effect).
- `--page-scale <float>` — PDF pt to CSS px scaling factor (default `1.3333`).
- `--template-html <path>` — approved HTML template to copy style/shell from.

Example:

```bash
python pdf_to_html_safe.py \
  SAYAJI_HANMAT_BANKAR_vs_STATE_OF_MAHARASHTRA\ \(2\).pdf \
  output/sayaji.html \
  --template-html "SAYAJI_HANMAT_BANKAR_semantic_no_watermark (1).html"
```

## Testing

```bash
python -m unittest -v
```

Tests include:
- bullet/list heuristics,
- table detection precision checks,
- watermark text filtering,
- signature overlay filtering safeguards,
- classifier behavior,
- template style extraction.
