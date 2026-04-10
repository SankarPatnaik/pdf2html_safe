#!/usr/bin/env python3
"""Convert a PDF to semantic HTML while safely skipping repeated watermark images.

Why this approach?
- It does NOT modify the source PDF.
- It preserves the text layer exactly as extracted.
- It only omits image blocks that look like repeated watermark/background assets.
- It groups extracted lines into semantic web-friendly blocks such as headings,
  paragraphs, figures, and signature sections.
- If no watermark is detected, watermark-removal logic is not triggered.

Usage:
    python pdf_to_html_safe.py input.pdf output.html

Optional:
    python pdf_to_html_safe.py input.pdf output.html --page-scale 1.3333 --keep-all-images
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import fitz  # PyMuPDF

try:  # Optional but helpful for prettier HTML output.
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - graceful fallback
    BeautifulSoup = None


PDF_TO_CSS_SCALE = 96.0 / 72.0  # 1 PDF pt = 1.3333 CSS px at 96 dpi


@dataclass
class TextLine:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    font_name: str
    font_size: float
    page_width: float
    page_height: float

    @property
    def left(self) -> float:
        return self.x0

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def is_centered(self) -> bool:
        return abs(self.center_x - (self.page_width / 2.0)) <= self.page_width * 0.14

    @property
    def is_bold(self) -> bool:
        return "bold" in self.font_name.lower()

    @property
    def uppercase_ratio(self) -> float:
        letters = [ch for ch in self.text if ch.isalpha()]
        if not letters:
            return 0.0
        return sum(1 for ch in letters if ch.isupper()) / len(letters)


@dataclass
class ImageBlock:
    top: float
    html_fragment: str


@dataclass
class WatermarkDetection:
    found: bool
    pages_with_candidates: List[int]
    repeated_image_count: int
    candidate_instances: int


def normalize_text(s: str) -> str:
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def bbox_area(bbox: Tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def bbox_center(bbox: Tuple[float, float, float, float]) -> Tuple[float, float]:
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def collect_image_occurrences(doc: fitz.Document) -> Counter:
    counts: Counter = Counter()
    for page in doc:
        data = page.get_text("dict")
        for block in data["blocks"]:
            if block["type"] == 1 and block.get("image"):
                digest = hashlib.sha1(block["image"]).hexdigest()
                counts[digest] += 1
    return counts


def is_probable_watermark(
    page_rect: fitz.Rect,
    block: dict,
    image_counts: Counter,
    min_repeat_pages: int,
) -> bool:
    img = block.get("image")
    if not img:
        return False

    digest = hashlib.sha1(img).hexdigest()
    repeated = image_counts[digest] >= min_repeat_pages
    if not repeated:
        return False

    bbox = tuple(block["bbox"])
    page_bbox = (0.0, 0.0, page_rect.width, page_rect.height)
    area_ratio = bbox_area(bbox) / max(bbox_area(page_bbox), 1.0)
    img_cx, img_cy = bbox_center(bbox)
    page_cx, page_cy = (page_rect.width / 2.0, page_rect.height / 2.0)
    center_dx = abs(img_cx - page_cx) / max(page_rect.width, 1.0)
    center_dy = abs(img_cy - page_cy) / max(page_rect.height, 1.0)

    # Heuristics tuned to catch repeated center-page background seals/logos.
    return 0.03 <= area_ratio <= 0.65 and center_dx <= 0.18 and center_dy <= 0.18


# ---------------------------------------------------------------------------
# Requested new function #1: detect watermark before triggering removal logic.
# ---------------------------------------------------------------------------
def pdf_has_watermark(doc: fitz.Document, min_repeat_pages: int = 2) -> WatermarkDetection:
    image_counts = collect_image_occurrences(doc)
    repeated_image_count = sum(1 for _, count in image_counts.items() if count >= min_repeat_pages)
    pages_with_candidates: List[int] = []
    candidate_instances = 0

    for page_number, page in enumerate(doc, start=1):
        page_dict = page.get_text("dict")
        page_has_candidate = False
        for block in page_dict["blocks"]:
            if block["type"] != 1 or not block.get("image"):
                continue
            if is_probable_watermark(page.rect, block, image_counts, min_repeat_pages=min_repeat_pages):
                page_has_candidate = True
                candidate_instances += 1
        if page_has_candidate:
            pages_with_candidates.append(page_number)

    return WatermarkDetection(
        found=bool(pages_with_candidates),
        pages_with_candidates=pages_with_candidates,
        repeated_image_count=repeated_image_count,
        candidate_instances=candidate_instances,
    )


def merge_row_segments(segments: List[TextLine]) -> TextLine:
    ordered = sorted(segments, key=lambda item: (round(item.x0, 2), round(item.x1, 2)))
    merged_text = merge_line_text([segment.text for segment in ordered])
    first = ordered[0]
    return TextLine(
        text=merged_text,
        x0=min(segment.x0 for segment in ordered),
        y0=min(segment.y0 for segment in ordered),
        x1=max(segment.x1 for segment in ordered),
        y1=max(segment.y1 for segment in ordered),
        font_name=first.font_name,
        font_size=sum(segment.font_size for segment in ordered) / max(len(ordered), 1),
        page_width=first.page_width,
        page_height=first.page_height,
    )


def merge_physical_lines_to_logical_rows(lines: List[TextLine]) -> List[TextLine]:
    if not lines:
        return []

    ordered = sorted(lines, key=lambda item: (round(item.y0, 1), round(item.x0, 1)))
    merged_rows: List[TextLine] = []
    current_row: List[TextLine] = [ordered[0]]

    for line in ordered[1:]:
        anchor = current_row[0]
        same_row = abs(line.y0 - anchor.y0) <= 1.5 and abs(line.y1 - anchor.y1) <= 1.5
        if same_row:
            current_row.append(line)
        else:
            merged_rows.append(merge_row_segments(current_row))
            current_row = [line]

    if current_row:
        merged_rows.append(merge_row_segments(current_row))

    return merged_rows


def extract_text_lines(page: fitz.Page) -> List[TextLine]:
    page_dict = page.get_text("dict")
    physical_lines: List[TextLine] = []

    for block in page_dict["blocks"]:
        if block["type"] != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            raw_text = "".join(span.get("text", "") for span in spans)
            text = normalize_text(raw_text)
            if not text:
                continue
            first_span = spans[0]
            x0, y0, x1, y1 = line["bbox"]
            physical_lines.append(
                TextLine(
                    text=text,
                    x0=float(x0),
                    y0=float(y0),
                    x1=float(x1),
                    y1=float(y1),
                    font_name=str(first_span.get("font", "")),
                    font_size=float(first_span.get("size", 12.0)),
                    page_width=float(page.rect.width),
                    page_height=float(page.rect.height),
                )
            )

    logical_rows = merge_physical_lines_to_logical_rows(physical_lines)
    logical_rows.sort(key=lambda item: (round(item.y0, 1), round(item.x0, 1)))
    return logical_rows


def merge_line_text(parts: List[str]) -> str:
    if not parts:
        return ""

    out = parts[0]
    for current in parts[1:]:
        if out.endswith("-") and current and current[0].isalnum():
            out = out[:-1] + current
        elif out.endswith(("/", "(", "[")):
            out += current
        elif current.startswith((".", ",", ";", ":", ")", "]", "?", "!")):
            out += current
        else:
            out += " " + current
    return normalize_text(out)


def get_numbered_prefix(text: str) -> tuple[int | None, str]:
    match = re.match(r"^(\d+)\.\s+(.*)$", text)
    if not match:
        return None, text
    return int(match.group(1)), match.group(2).strip()


def is_numbered_paragraph_start(text: str) -> bool:
    number, remainder = get_numbered_prefix(text)
    return number is not None and bool(remainder)



@dataclass
class TextBlock:
    lines: List[TextLine]

    @property
    def text(self) -> str:
        return merge_line_text([line.text for line in self.lines])

    @property
    def x0(self) -> float:
        return min(line.x0 for line in self.lines)

    @property
    def y0(self) -> float:
        return min(line.y0 for line in self.lines)

    @property
    def x1(self) -> float:
        return max(line.x1 for line in self.lines)

    @property
    def y1(self) -> float:
        return max(line.y1 for line in self.lines)

    @property
    def avg_font_size(self) -> float:
        return sum(line.font_size for line in self.lines) / max(len(self.lines), 1)

    @property
    def is_centered(self) -> bool:
        centered_count = sum(1 for line in self.lines if line.is_centered)
        return centered_count >= max(1, int(len(self.lines) * 0.6))

    @property
    def indent(self) -> float:
        return min(line.left for line in self.lines)

    @property
    def uppercase_ratio(self) -> float:
        text = self.text
        letters = [ch for ch in text if ch.isalpha()]
        if not letters:
            return 0.0
        return sum(1 for ch in letters if ch.isupper()) / len(letters)


def group_lines_into_blocks(lines: List[TextLine]) -> List[TextBlock]:
    if not lines:
        return []

    blocks: List[TextBlock] = []
    current: List[TextLine] = [lines[0]]

    for prev, line in zip(lines, lines[1:]):
        vertical_gap = line.y0 - prev.y1
        indent_delta = abs(line.x0 - prev.x0)
        font_delta = abs(line.font_size - prev.font_size)
        current_is_numbered = is_numbered_paragraph_start(line.text)
        prev_is_numbered = is_numbered_paragraph_start(prev.text)
        prev_is_short_byline = len(prev.text) <= 40 and (prev.text.endswith('J.') or prev.text.endswith(',J.') or prev.text.endswith('J.,'))
        likely_new_block = (
            current_is_numbered
            or vertical_gap > max(prev.font_size * 1.15, 10.0)
            or font_delta > 1.8
            or (indent_delta > max(prev.font_size * 6.0, 72.0) and prev_is_numbered)
            or prev.text.endswith(":")
            or prev_is_short_byline
        )

        if likely_new_block:
            blocks.append(TextBlock(lines=current))
            current = [line]
        else:
            current.append(line)

    if current:
        blocks.append(TextBlock(lines=current))
    return blocks


def classify_block_tag(block: TextBlock, page_index: int, total_pages: int, base_indent: float) -> str:
    text = block.text
    text_len = len(text)

    if is_numbered_paragraph_start(text):
        return "li"
    if page_index == total_pages and (".J." in text or text.startswith("New Delhi")):
        return "address"
    if block.is_centered and block.uppercase_ratio >= 0.72 and text_len <= 110:
        return "h1" if page_index == 1 else "h2"
    if block.is_centered and text_len <= 80 and block.avg_font_size >= 12:
        return "h2"
    if (text.startswith("Exception") or text.startswith("Provided") or text.startswith("Provided that")) and text_len >= 30:
        return "blockquote"
    if block.indent >= (base_indent + 32) and text_len >= 45:
        return "blockquote"
    if text_len <= 18 and re.fullmatch(r"\d+[.]?", text):
        return "p"
    return "p"


def block_to_html(block: TextBlock, page_index: int, total_pages: int, base_indent: float) -> tuple[str, str, int | None]:
    tag = classify_block_tag(block, page_index, total_pages, base_indent)
    text = block.text

    if tag == "li":
        start_number, item_text = get_numbered_prefix(text)
        return tag, f'<li>{html.escape(item_text)}</li>', start_number
    if tag == "address":
        return tag, f'<address class="signature-block">{html.escape(text)}</address>', None
    if tag == "blockquote":
        return tag, f'<blockquote>{html.escape(text)}</blockquote>', None
    if tag == "h1":
        return tag, f'<h1>{html.escape(text)}</h1>', None
    if tag == "h2":
        return tag, f'<h2>{html.escape(text)}</h2>', None
    return tag, f'<p>{html.escape(text)}</p>', None


def render_page_parts(page_parts: List[Tuple[float, str, str, int | None]]) -> List[str]:
    rendered: List[str] = []
    active_list_number: int | None = None
    list_items: List[str] = []

    def flush_list() -> None:
        nonlocal active_list_number, list_items
        if list_items:
            rendered.append(f'<ol start="{active_list_number}" class="section-list">' + ''.join(list_items) + '</ol>')
            list_items = []
            active_list_number = None

    for _, part_type, fragment, start_number in page_parts:
        if part_type == 'li':
            if active_list_number is None:
                active_list_number = start_number or 1
            elif start_number is not None and start_number != (active_list_number + len(list_items)):
                flush_list()
                active_list_number = start_number
            list_items.append(fragment)
        else:
            flush_list()
            rendered.append(fragment)

    flush_list()
    return rendered


def collect_semantic_images(
    page: fitz.Page,
    image_counts: Counter,
    keep_all_images: bool,
    watermark_removal_enabled: bool,
) -> List[ImageBlock]:
    page_dict = page.get_text("dict")
    images: List[ImageBlock] = []

    for block in page_dict["blocks"]:
        if block["type"] != 1 or not block.get("image"):
            continue

        if watermark_removal_enabled and not keep_all_images:
            if is_probable_watermark(page.rect, block, image_counts, min_repeat_pages=2):
                continue

        x0, y0, x1, y1 = block["bbox"]
        width = max(1.0, x1 - x0)
        height = max(1.0, y1 - y0)
        img_b64 = base64.b64encode(block["image"]).decode("ascii")
        ext = block.get("ext", "png")
        images.append(
            ImageBlock(
                top=float(y0),
                html_fragment=(
                    '<figure class="image-block">'
                    f'<img alt="embedded-image" width="{int(width)}" height="{int(height)}" '
                    f'src="data:image/{ext};base64,{img_b64}" />'
                    '</figure>'
                ),
            )
        )
    return images


def build_semantic_page_html(
    page: fitz.Page,
    page_index: int,
    total_pages: int,
    image_counts: Counter,
    keep_all_images: bool,
    watermark_removal_enabled: bool,
) -> Tuple[str, dict]:
    lines = extract_text_lines(page)
    blocks = group_lines_into_blocks(lines)
    images = collect_semantic_images(page, image_counts, keep_all_images, watermark_removal_enabled)

    base_indent = sorted(block.indent for block in blocks)[len(blocks) // 2] if blocks else 0.0
    page_parts: List[Tuple[float, str, str, int | None]] = []
    for block in blocks:
        part_type, fragment, start_number = block_to_html(block, page_index, total_pages, base_indent)
        page_parts.append((block.y0, part_type, fragment, start_number))
    page_parts.extend((img.top, "figure", img.html_fragment, None) for img in images)
    page_parts.sort(key=lambda item: item[0])

    skipped_watermarks = 0
    if watermark_removal_enabled and not keep_all_images:
        raw_page_dict = page.get_text("dict")
        for block in raw_page_dict["blocks"]:
            if block["type"] == 1 and block.get("image") and is_probable_watermark(page.rect, block, image_counts, min_repeat_pages=2):
                skipped_watermarks += 1

    page_html = [
        '<section class="page-wrap">',
        f'  <div class="page-label">Page {page_index}</div>',
        '  <article class="page semantic-page">',
    ]
    for fragment in render_page_parts(page_parts):
        page_html.append(f"    {fragment}")
    page_html.extend([
        '  </article>',
        '</section>',
    ])

    debug_info = {
        "page": page_index,
        "text_lines": len(lines),
        "semantic_blocks": len(blocks),
        "embedded_images": len(images),
        "skipped_watermarks": skipped_watermarks,
    }
    return "\n".join(page_html), debug_info


# ---------------------------------------------------------------------------
# Requested new function #2: beautify final HTML and keep semantic structure.
# ---------------------------------------------------------------------------
def beautify_html_output(raw_html: str) -> str:
    raw_html = raw_html.strip() + "\n"

    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw_html, "html.parser")
        return soup.prettify()

    # Lightweight fallback if bs4 is unavailable.
    raw_html = re.sub(r">\s+<", ">\n<", raw_html)
    raw_html = re.sub(r"\n{3,}", "\n\n", raw_html)
    return raw_html


def emit_html(doc: fitz.Document, keep_all_images: bool, page_scale: float) -> str:
    image_counts = collect_image_occurrences(doc)
    watermark_detection = pdf_has_watermark(doc)
    watermark_removal_enabled = watermark_detection.found and not keep_all_images

    pages_html: List[str] = []
    debug_summary: List[dict] = []

    for page_index, page in enumerate(doc, start=1):
        page_html, page_debug = build_semantic_page_html(
            page=page,
            page_index=page_index,
            total_pages=len(doc),
            image_counts=image_counts,
            keep_all_images=keep_all_images,
            watermark_removal_enabled=watermark_removal_enabled,
        )
        pages_html.append(page_html)
        debug_summary.append(page_debug)

    title = html.escape(doc.metadata.get("title") or "PDF to HTML")
    summary_json = html.escape(
        json.dumps(
            {
                "watermark_detected": watermark_detection.found,
                "watermark_pages": watermark_detection.pages_with_candidates,
                "repeated_image_count": watermark_detection.repeated_image_count,
                "candidate_instances": watermark_detection.candidate_instances,
                "page_summary": debug_summary,
                "page_scale": page_scale,
            },
            indent=2,
        )
    )

    note = (
        "Watermark detected. Repeated watermark-style image layers were skipped while keeping the text layer intact."
        if watermark_removal_enabled
        else "No watermark detected. Watermark-removal logic was skipped and the PDF was converted directly to semantic HTML."
    )

    raw_html = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f3f4f6;
      --paper: #ffffff;
      --ink: #111827;
      --muted: #6b7280;
      --accent: #1d4ed8;
      --border: rgba(17, 24, 39, 0.08);
      --shadow: 0 12px 32px rgba(15, 23, 42, 0.10);
      --max-width: 920px;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      background: linear-gradient(180deg, #eef2ff 0%, #f8fafc 120px, #f3f4f6 100%);
      color: var(--ink);
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif;
      line-height: 1.65;
      padding: 24px 16px 48px;
    }}
    .doc-shell {{
      max-width: var(--max-width);
      margin: 0 auto;
    }}
    .doc-meta {{
      margin: 0 auto 20px;
      padding: 16px 18px;
      color: var(--muted);
      font-size: 14px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid var(--border);
      border-radius: 16px;
      backdrop-filter: blur(10px);
    }}
    .page-wrap {{
      max-width: var(--max-width);
      margin: 0 auto 24px;
    }}
    .page-label {{
      margin: 0 0 8px 4px;
      color: var(--muted);
      font-size: 13px;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}
    .page {{
      background: var(--paper);
      border: 1px solid var(--border);
      border-radius: 22px;
      box-shadow: var(--shadow);
      padding: 36px 44px;
    }}
    .semantic-page h1,
    .semantic-page h2,
    .semantic-page p,
    .semantic-page blockquote,
    .semantic-page address,
    .semantic-page figure,
    .semantic-page ol {{
      margin: 0 0 14px;
    }}
    .semantic-page h1 {{
      font-size: 1.35rem;
      line-height: 1.35;
      text-align: center;
      color: #111827;
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }}
    .semantic-page h2 {{
      font-size: 1.05rem;
      line-height: 1.4;
      text-align: center;
      color: #111827;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}
    .semantic-page p {{
      font-size: 1rem;
      color: #111827;
      text-align: justify;
      text-indent: 0;
    }}
    .semantic-page .section-list {{
      padding-left: 1.55rem;
    }}
    .semantic-page .section-list li {{
      padding-left: 0.2rem;
      margin-bottom: 0.45rem;
      text-align: justify;
    }}
    .semantic-page blockquote {{
      margin-left: 26px;
      padding-left: 16px;
      border-left: 3px solid rgba(29, 78, 216, 0.18);
      color: #1f2937;
      background: rgba(37, 99, 235, 0.03);
      border-radius: 0 10px 10px 0;
      padding-top: 10px;
      padding-bottom: 10px;
    }}
    .semantic-page .signature-block {{
      white-space: pre-line;
      font-style: normal;
      color: #111827;
      margin-top: 22px;
    }}
    .semantic-page .image-block {{
      margin: 18px 0;
      text-align: center;
    }}
    .semantic-page .image-block img {{
      max-width: 100%;
      height: auto;
      border-radius: 12px;
      border: 1px solid rgba(17, 24, 39, 0.08);
    }}
    @media (max-width: 720px) {{
      .page {{ padding: 24px 20px; border-radius: 16px; }}
      .semantic-page h1 {{ font-size: 1.15rem; }}
      .semantic-page h2 {{ font-size: 1rem; }}
      .semantic-page p {{ font-size: 0.98rem; }}
      .semantic-page blockquote {{ margin-left: 12px; padding-left: 12px; }}
    }}
  </style>
</head>
<body>
  <main class=\"doc-shell\">
    <div class=\"doc-meta\">{html.escape(note)}</div>
    {''.join(pages_html)}
  </main>
  <!-- conversion-summary
  {summary_json}
  -->
</body>
</html>
"""
    return beautify_html_output(raw_html)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PDF to semantic HTML while safely skipping repeated watermark images.")
    parser.add_argument("input_pdf", help="Path to source PDF")
    parser.add_argument("output_html", help="Path to output HTML")
    parser.add_argument("--keep-all-images", action="store_true", help="Embed all images, even if they look like repeated watermarks")
    parser.add_argument("--page-scale", type=float, default=PDF_TO_CSS_SCALE, help="Scale factor from PDF points to CSS pixels")
    args = parser.parse_args()

    input_pdf = Path(args.input_pdf)
    output_html = Path(args.output_html)
    output_html.parent.mkdir(parents=True, exist_ok=True)

    with fitz.open(input_pdf) as doc:
        detection = pdf_has_watermark(doc)
        html_text = emit_html(doc, keep_all_images=args.keep_all_images, page_scale=args.page_scale)

    output_html.write_text(html_text, encoding="utf-8")

    if detection.found and not args.keep_all_images:
        print(
            "Watermark detected on pages "
            f"{detection.pages_with_candidates}; removal logic applied safely during HTML conversion."
        )
    elif args.keep_all_images:
        print("Watermark detection completed, but --keep-all-images was used, so all images were retained.")
    else:
        print("No watermark detected; watermark-removal logic was skipped.")

    print(f"Created: {output_html}")


if __name__ == "__main__":
    main()
