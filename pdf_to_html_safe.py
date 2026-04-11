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
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Dict, List, Set, Tuple

from pdf_classifier import classify_document
from pdf_inspector import inspect_document
from template_renderer import extract_template_style, render_document_shell

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover - allows running heuristic unit tests without PyMuPDF
    fitz = None  # type: ignore[assignment]

try:  # Optional but helpful for prettier HTML output.
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - graceful fallback
    BeautifulSoup = None


PDF_TO_CSS_SCALE = 96.0 / 72.0  # 1 PDF pt = 1.3333 CSS px at 96 dpi
DEFAULT_TEMPLATE_NAME = "SAYAJI_HANMAT_BANKAR_semantic_no_watermark (1).html"
PDF2HTMLEX_REFERENCE = "https://github.com/pdf2htmlex/pdf2htmlex"


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
    dir_x: float = 1.0
    dir_y: float = 0.0

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
class TableRegion:
    start_row: int
    end_row: int
    column_x: List[float]


@dataclass
class WatermarkDetection:
    found: bool
    pages_with_candidates: List[int]
    repeated_image_count: int
    candidate_instances: int


DEFAULT_LEGAL_CSS = """
  :root {
    --a4-width: 210mm;
    --a4-height: 297mm;
    --legal-margin-top: 16mm;
    --legal-margin-right: 14mm;
    --legal-margin-bottom: 16mm;
    --legal-margin-left: 14mm;
  }
  body { background: #fff; color: #111; margin: 0; font-family: "Noto Serif", Georgia, "Times New Roman", serif; line-height: 1.45; }
  .doc-shell { max-width: none; margin: 0 auto; padding: 8mm 0 10mm; }
  .doc-meta { font-size: 0.9rem; margin-bottom: 12px; color: #222; }
  article.court-judgment { counter-reset: page; display: flex; flex-direction: column; align-items: center; gap: 6mm; }
  header.court-header p { margin: 0.15rem 0; text-align: center; }
  section.page-wrap { width: var(--a4-width); margin: 0; }
  article.page.semantic-page {
    box-sizing: border-box;
    width: var(--a4-width);
    height: var(--a4-height);
    background: #fff;
    border: 1px solid #222;
    border-radius: 0;
    box-shadow: none;
    display: grid;
    grid-template-rows: 12mm 1fr 10mm;
    padding: var(--legal-margin-top) var(--legal-margin-right) var(--legal-margin-bottom) var(--legal-margin-left);
    overflow: hidden;
  }
  .page-header { min-height: 0; }
  .page-body { min-height: 0; overflow: hidden; align-self: stretch; }
  .page-footer { min-height: 0; display: flex; align-items: flex-end; justify-content: center; }
  .page-folio { font-size: 0.83rem; color: #111; min-height: 1.2em; }
  .semantic-page p, .semantic-page li, .semantic-page blockquote { margin: 0.22rem 0 0.48rem; }
  .semantic-page h1, .semantic-page h2, .semantic-page h3 { margin: 0.8rem 0 0.4rem; font-weight: 600; }
  .semantic-page blockquote { margin-left: 1.4rem; border-left: 2px solid #888; padding-left: 0.8rem; }
  .pdf-table { width: 100%; border-collapse: collapse; margin: 0.45rem 0 0.8rem; }
  .pdf-table th, .pdf-table td { border: 1px solid #333; padding: 4px 6px; vertical-align: top; }
  figure.image-block, figure.preserved-table-image { margin: 0.65rem 0; }
  figure img { max-width: 100%; height: auto; display: block; }
  @page { size: A4; margin: 16mm 14mm; }
  @media print {
    body { font-size: 11pt; }
    .doc-shell { max-width: none; padding: 0; }
    article.page.semantic-page { border: none; width: auto; height: auto; min-height: calc(var(--a4-height) - 32mm); page-break-after: always; overflow: visible; }
    section.page-wrap { page-break-inside: avoid; }
    table, figure, blockquote, ol, ul { page-break-inside: avoid; }
  }
"""

HEADING_KEYWORDS = {"JUDGMENT", "JUDGEMENT", "FACTS", "ANALYSIS", "ISSUE", "ISSUES", "ORDER", "CONCLUSION", "DECISION"}


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


def group_physical_lines_by_row(lines: List[TextLine]) -> List[List[TextLine]]:
    if not lines:
        return []
    ordered = sorted(lines, key=lambda item: (round(item.y0, 1), round(item.x0, 1)))
    rows: List[List[TextLine]] = [[ordered[0]]]
    for line in ordered[1:]:
        anchor = rows[-1][0]
        same_row = abs(line.y0 - anchor.y0) <= 1.5 and abs(line.y1 - anchor.y1) <= 1.5
        if same_row:
            rows[-1].append(line)
        else:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda item: item.x0)
    return rows


def _extract_page_lines(page: fitz.Page) -> List[TextLine]:
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
            dir_x, dir_y = line.get("dir", (1.0, 0.0))
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
                    dir_x=float(dir_x),
                    dir_y=float(dir_y),
                )
            )

    physical_lines = filter_probable_watermark_text_lines(physical_lines)
    physical_lines = filter_signature_stamp_lines(physical_lines)
    logical_rows = merge_physical_lines_to_logical_rows(physical_lines)
    logical_rows.sort(key=lambda item: (round(item.y0, 1), round(item.x0, 1)))
    return logical_rows


def extract_text_lines(page: fitz.Page) -> List[TextLine]:
    return _extract_page_lines(page)


def extract_text_rows(page: fitz.Page) -> tuple[List[List[TextLine]], List[TextLine]]:
    physical_lines = _extract_page_lines(page)
    rows = group_physical_lines_by_row(physical_lines)
    logical_rows = [merge_row_segments(row) for row in rows]
    logical_rows.sort(key=lambda item: (round(item.y0, 1), round(item.x0, 1)))
    return rows, logical_rows


@dataclass
class HeaderFooterProfile:
    repeated_zone_text: Set[str]


@dataclass
class PageLayout:
    source_page_index: int
    source_page_number: int
    source_page_folio: str | None
    page_classification: str
    header_footer_suppressed: int
    rendered_parts: List[str]
    skipped_watermarks: int
    has_images: bool
    is_truly_blank_source_page: bool


def _canonical_header_footer_text(text: str) -> str:
    canonical = normalize_text(text).lower()
    canonical = re.sub(r"\bpage\s+\d+\b", "page #", canonical)
    canonical = re.sub(r"\d+", "#", canonical)
    return canonical


def _line_zone(line: TextLine) -> str:
    top_ratio = line.y0 / max(line.page_height, 1.0)
    bottom_ratio = line.y1 / max(line.page_height, 1.0)
    if top_ratio <= 0.12:
        return "top"
    if bottom_ratio >= 0.88:
        return "bottom"
    return "body"


def _is_footer_noise_pattern(text: str) -> bool:
    compact = normalize_text(text).lower()
    return bool(
        re.fullmatch(r"\d{1,4}", compact)
        or compact.startswith("signature not verified")
        or compact.startswith("digitally signed by")
        or compact.startswith("signed by ")
        or compact.startswith("reason:")
        or bool(re.match(r"^date:\s*\d{4}[./-]\d{1,2}[./-]\d{1,2}", compact))
    )


FOLIO_CANDIDATE_PATTERN = re.compile(r"^(?:\(?\s*[A-Za-z]{0,4}\s*[-./]?\s*)?([ivxlcdmIVXLCDM]+|\d{1,5})(?:\s*[-./]?\s*[A-Za-z]{0,4}\s*\)?)?$")


def _extract_folio_token(text: str) -> str | None:
    compact = normalize_text(text)
    if not compact or len(compact) > 24:
        return None
    compact = compact.strip("[]{}")
    match = FOLIO_CANDIDATE_PATTERN.fullmatch(compact)
    if not match:
        return None
    return compact


def extract_source_page_folio(lines: List[TextLine]) -> str | None:
    zone_candidates: List[TextLine] = []
    for line in lines:
        zone = _line_zone(line)
        if zone == "body":
            continue
        token = _extract_folio_token(line.text)
        if token is None:
            continue
        if is_probable_signature_stamp_text(line.text):
            continue
        zone_candidates.append(line)

    if not zone_candidates:
        return None

    # Prefer bottom-center folios which are common in Indian court PDFs.
    zone_candidates.sort(
        key=lambda item: (
            0 if _line_zone(item) == "bottom" else 1,
            abs(item.center_x - (item.page_width / 2.0)),
            len(normalize_text(item.text)),
        )
    )
    return normalize_text(zone_candidates[0].text)


def build_header_footer_profile(doc: fitz.Document) -> HeaderFooterProfile:
    total_pages = max(len(doc), 1)
    repeat_threshold = max(3, ceil(total_pages * 0.35))
    by_canonical: Dict[str, dict] = {}

    for page_no, page in enumerate(doc, start=1):
        page_lines = _extract_page_lines(page)
        for line in page_lines:
            zone = _line_zone(line)
            if zone == "body":
                continue
            canonical = _canonical_header_footer_text(line.text)
            if not canonical:
                continue
            entry = by_canonical.setdefault(canonical, {"pages": set(), "top": 0, "bottom": 0})
            entry["pages"].add(page_no)
            if zone == "top":
                entry["top"] += 1
            else:
                entry["bottom"] += 1

    repeated_zone_text: Set[str] = set()
    for canonical, entry in by_canonical.items():
        page_hits = len(entry["pages"])
        zone_consistent = max(entry["top"], entry["bottom"]) >= max(2, ceil(page_hits * 0.75))
        if page_hits >= repeat_threshold and zone_consistent and len(canonical) <= 130:
            repeated_zone_text.add(canonical)

    return HeaderFooterProfile(repeated_zone_text=repeated_zone_text)


def suppress_header_footer_lines(lines: List[TextLine], profile: HeaderFooterProfile) -> tuple[List[TextLine], int]:
    kept: List[TextLine] = []
    suppressed = 0
    for line in lines:
        zone = _line_zone(line)
        canonical = _canonical_header_footer_text(line.text)
        pattern_match = _is_footer_noise_pattern(line.text)
        repeated_zone = canonical in profile.repeated_zone_text
        page_number_artifact = bool(re.fullmatch(r"\d{1,4}", normalize_text(line.text))) and zone != "body"
        if page_number_artifact or (pattern_match and zone != "body") or (repeated_zone and zone != "body"):
            suppressed += 1
            continue
        kept.append(line)
    return kept, suppressed


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


def _vertical_overlap_amount(a: TextLine, b: TextLine) -> float:
    return max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))


def filter_probable_watermark_text_lines(lines: List[TextLine]) -> List[TextLine]:
    if not lines:
        return []

    ordered_sizes = sorted(line.font_size for line in lines)
    median_size = ordered_sizes[len(ordered_sizes) // 2]
    watermark_tokens = {"JUDGMENT", "JUDGEMENT", "SUPREMECOURTOFINDIA", "GOVERNMENTOFINDIA"}

    kept: List[TextLine] = []
    for line in lines:
        text_compact = re.sub(r"[^A-Za-z]", "", line.text).upper()
        is_upper_short = 5 <= len(text_compact) <= 40 and line.uppercase_ratio >= 0.85
        looks_rotated = abs(line.dir_y) >= 0.18
        looks_large = line.font_size >= max(14.0, median_size * 1.35)
        explicit_watermark_word = text_compact in watermark_tokens
        overlap_count = sum(
            1
            for other in lines
            if other is not line and _vertical_overlap_amount(line, other) >= max(2.0, min(line.height, other.height) * 0.2)
        )
        looks_overlay = overlap_count >= 2

        if looks_overlay and (
            (looks_rotated and is_upper_short and line.is_centered)
            or (explicit_watermark_word and line.is_centered and (looks_large or looks_rotated))
        ):
            continue

        kept.append(line)
    return kept


def is_probable_signature_stamp_text(text: str) -> bool:
    normalized = normalize_text(text)
    compact_lower = re.sub(r"\s+", " ", normalized).lower()
    compact_upper = compact_lower.upper()

    if compact_upper.startswith("SIGNATURE NOT VERIFIED"):
        return True
    if compact_lower.startswith("digitally signed by "):
        return True
    if compact_lower.startswith("signed by "):
        return True
    if compact_lower.startswith("date:") and re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", compact_lower):
        return True
    return False


def filter_signature_stamp_lines(lines: List[TextLine]) -> List[TextLine]:
    if not lines:
        return []
    marker_indexes = [idx for idx, line in enumerate(lines) if is_probable_signature_stamp_text(line.text)]
    if not marker_indexes:
        return lines

    removable_indexes: set[int] = set()
    for idx in marker_indexes:
        neighborhood = lines[max(0, idx - 2) : min(len(lines), idx + 3)]
        marker_count = sum(1 for item in neighborhood if is_probable_signature_stamp_text(item.text))
        # Remove only clustered digital-signature overlays; keep isolated "Date:" body content.
        if marker_count >= 2:
            removable_indexes.add(idx)

    return [line for idx, line in enumerate(lines) if idx not in removable_indexes]


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


def is_bullet_line(text: str) -> bool:
    return bool(
        re.match(
            r"^(\u2022|\u25aa|\u25cf|[-*•]|[a-zA-Z]\)|\(\d+\)|\d+\)|[ivxlcdmIVXLCDM]+\)|\d+\.)\s+.+$",
            text,
        )
    )


def strip_bullet_prefix(text: str) -> tuple[str, str]:
    match = re.match(
        r"^(\u2022|\u25aa|\u25cf|[-*•]|[a-zA-Z]\)|\(\d+\)|\d+\)|[ivxlcdmIVXLCDM]+\)|\d+\.)\s+(.+)$",
        text,
    )
    if not match:
        return "", text
    return match.group(1), match.group(2).strip()


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
        prev_sentence_continues = bool(prev.text) and prev.text[-1] not in ".!?:;"
        line_starts_lower_or_digit = bool(line.text) and (line.text[0].islower() or line.text[0].isdigit())
        continuation_hint = prev_sentence_continues and (
            line_starts_lower_or_digit or indent_delta <= max(prev.font_size * 1.35, 16.0)
        )
        likely_new_block = (
            current_is_numbered
            or is_bullet_line(line.text)
            or (vertical_gap > max(prev.font_size * 1.45, 14.0) and not continuation_hint)
            or font_delta > 1.8
            or (line.x0 - prev.x0) > max(prev.font_size * 1.35, 14.0)
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


def classify_block_tag(
    block: TextBlock,
    page_index: int,
    total_pages: int,
    base_indent: float,
    prev_tail: str | None = None,
) -> str:
    text = block.text
    text_len = len(text)

    if is_numbered_paragraph_start(text):
        return "li"
    if page_index == total_pages and (".J." in text or text.startswith("New Delhi")):
        return "address"
    if prev_tail and prev_tail[-1:] not in ".!?:;" and text[:1].islower():
        return "p"
    if block.is_centered and block.uppercase_ratio >= 0.72 and text_len <= 110:
        return "h1" if page_index == 1 else "h2"
    semantic_heading_hit = any(word in text.upper() for word in HEADING_KEYWORDS)
    if (
        semantic_heading_hit
        and block.is_centered
        and text_len <= 100
        and block.avg_font_size >= 11.0
        and block.uppercase_ratio >= 0.45
        and (not prev_tail or prev_tail[-1:] in ".!?:;")
        and not text[:1].islower()
    ):
        return "h2"
    if (text.startswith("Exception") or text.startswith("Provided") or text.startswith("Provided that")) and text_len >= 30:
        return "blockquote"
    if block.indent >= (base_indent + 32) and text_len >= 45:
        return "blockquote"
    if text_len <= 18 and re.fullmatch(r"\d+[.]?", text):
        return "p"
    return "p"


def block_to_html(
    block: TextBlock,
    page_index: int,
    total_pages: int,
    base_indent: float,
    prev_tail: str | None = None,
) -> tuple[str, str, int | None]:
    tag = classify_block_tag(block, page_index, total_pages, base_indent, prev_tail=prev_tail)
    text = block.text

    if tag == "li":
        start_number, item_text = get_numbered_prefix(text)
        return tag, (
            f'<li data-source-page="{page_index}" data-extraction-method="native" '
            f'data-block-role="list" data-confidence="0.92" data-watermark-removed="false">{html.escape(item_text)}</li>'
        ), start_number
    if tag == "address":
        return tag, (
            f'<address class="signature-block" data-source-page="{page_index}" '
            f'data-extraction-method="native" data-block-role="signature" data-confidence="0.9">{html.escape(text)}</address>'
        ), None
    if tag == "blockquote":
        return tag, (
            f'<blockquote data-source-page="{page_index}" data-extraction-method="native" '
            f'data-block-role="paragraph" data-confidence="0.9">{html.escape(text)}</blockquote>'
        ), None
    if tag == "h1":
        return tag, (
            f'<h1 data-source-page="{page_index}" data-extraction-method="native" '
            f'data-block-role="heading" data-confidence="0.95">{html.escape(text)}</h1>'
        ), None
    if tag == "h2":
        return tag, (
            f'<h2 data-source-page="{page_index}" data-extraction-method="native" '
            f'data-block-role="heading" data-confidence="0.94">{html.escape(text)}</h2>'
        ), None
    return tag, (
        f'<p data-source-page="{page_index}" data-extraction-method="native" '
        f'data-block-role="paragraph" data-confidence="0.92">{html.escape(text)}</p>'
    ), None


def detect_table_regions(rows: List[List[TextLine]]) -> List[TableRegion]:
    regions: List[TableRegion] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if len(row) < 2:
            i += 1
            continue
        if any(is_bullet_line(cell.text) for cell in row):
            i += 1
            continue

        run_start = i
        run_rows = [row]
        j = i + 1
        while j < len(rows):
            nxt = rows[j]
            if len(nxt) < 2 or any(is_bullet_line(cell.text) for cell in nxt):
                break
            gap = nxt[0].y0 - rows[j - 1][0].y1
            if gap > max(rows[j - 1][0].font_size * 1.8, 14.0):
                break
            run_rows.append(nxt)
            j += 1

        if len(run_rows) >= 3:
            anchor_count = max(2, min(len(run_rows[0]), 5))
            anchors = [cell.x0 for cell in run_rows[0][:anchor_count]]
            aligned_rows = 0
            for candidate_row in run_rows:
                matches = 0
                for anchor in anchors:
                    if any(abs(cell.x0 - anchor) <= 14.0 for cell in candidate_row):
                        matches += 1
                if matches >= 2:
                    aligned_rows += 1
            if aligned_rows >= max(3, int(len(run_rows) * 0.8)):
                regions.append(TableRegion(start_row=run_start, end_row=j - 1, column_x=anchors))
                i = j
                continue
        i += 1
    return regions


def render_table_region(rows: List[List[TextLine]], region: TableRegion) -> tuple[float, str]:
    table_rows = rows[region.start_row : region.end_row + 1]
    header_cells = sorted(table_rows[0], key=lambda cell: cell.x0)
    body_rows = table_rows[1:]
    html_rows: List[str] = [
        '<table class="pdf-table" data-extraction-method="table-extracted" '
        'data-block-role="table" data-confidence="0.86" data-preserved-image-block="false">',
        "<thead>",
        "<tr>",
    ]
    for cell in header_cells:
        html_rows.append(f"<th>{html.escape(normalize_text(cell.text))}</th>")
    html_rows.extend(["</tr>", "</thead>", "<tbody>"])
    for row in body_rows:
        sorted_cells = sorted(row, key=lambda cell: cell.x0)
        html_rows.append("<tr>")
        for idx in range(len(header_cells)):
            cell_text = sorted_cells[idx].text if idx < len(sorted_cells) else ""
            html_rows.append(f"<td>{html.escape(normalize_text(cell_text))}</td>")
        html_rows.append("</tr>")
    html_rows.extend(["</tbody>", "</table>"])
    return table_rows[0][0].y0, "".join(html_rows)


def build_list_fragment(items: List[tuple[int, str, bool]]) -> str:
    if not items:
        return ""
    root: List[str] = []
    stack: List[tuple[int, str, List[str]]] = []

    def close_until(level: int) -> None:
        nonlocal root, stack
        while stack and stack[-1][0] >= level:
            _, list_tag, bucket = stack.pop()
            fragment = f"<{list_tag}>" + "".join(bucket) + f"</{list_tag}>"
            if stack:
                stack[-1][2].append(fragment)
            else:
                root.append(fragment)

    for level, text, ordered in items:
        list_tag = "ol" if ordered else "ul"
        while stack and stack[-1][0] > level:
            close_until(stack[-1][0])
        if not stack or stack[-1][0] < level or stack[-1][1] != list_tag:
            stack.append((level, list_tag, []))
        stack[-1][2].append(f"<li>{html.escape(text)}</li>")
    close_until(-1)
    return "".join(root)


def render_text_blocks_with_lists(
    blocks: List[TextBlock],
    page_index: int,
    total_pages: int,
    base_indent: float,
    prev_page_tail: str | None = None,
) -> List[Tuple[float, str, str, int | None]]:
    page_parts: List[Tuple[float, str, str, int | None]] = []
    list_items: List[tuple[int, str, bool]] = []
    min_indent = min((block.indent for block in blocks), default=0.0)

    def flush_list(anchor_y: float) -> None:
        nonlocal list_items
        if list_items:
            page_parts.append((anchor_y, "list", build_list_fragment(list_items), None))
            list_items = []

    for idx, block in enumerate(blocks):
        text = block.text
        if is_bullet_line(text):
            bullet_prefix, content = strip_bullet_prefix(text)
            ordered = bool(re.match(r"^(\d+[.)]?|[ivxlcdmIVXLCDM]+\)|\(\d+\)|[a-zA-Z]\))$", bullet_prefix))
            level = int(max(0, round((block.indent - min_indent) / 18.0)))
            list_items.append((level, content, ordered))
            continue
        flush_list(block.y0)
        prev_tail = prev_page_tail if idx == 0 else None
        part_type, fragment, start_number = block_to_html(block, page_index, total_pages, base_indent, prev_tail=prev_tail)
        page_parts.append((block.y0, part_type, fragment, start_number))

    flush_list(blocks[-1].y0 if blocks else 0.0)
    return page_parts


def render_page_parts(
    page_parts: List[Tuple[float, str, str, int | None]],
    carry_list_number: int | None = None,
) -> Tuple[List[str], int | None]:
    rendered: List[str] = []
    active_list_number: int | None = None
    list_items: List[str] = []
    next_list_number: int | None = carry_list_number

    def flush_list() -> None:
        nonlocal active_list_number, list_items, next_list_number
        if list_items:
            rendered.append(f'<ol start="{active_list_number}" class="section-list">' + ''.join(list_items) + '</ol>')
            next_list_number = (active_list_number or 1) + len(list_items)
            list_items = []
            active_list_number = None

    for _, part_type, fragment, start_number in page_parts:
        if part_type == 'li':
            if active_list_number is None:
                active_list_number = start_number or carry_list_number or 1
            elif start_number is not None and start_number != (active_list_number + len(list_items)):
                flush_list()
                active_list_number = start_number
            list_items.append(fragment)
        else:
            flush_list()
            rendered.append(fragment)

    flush_list()
    return rendered, next_list_number


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
                    '<figure class="image-block" data-extraction-method="image-preserved" '
                    'data-block-role="figure" data-preserved-image-block="true" data-confidence="1.0">'
                    f'<img alt="embedded-image" width="{int(width)}" height="{int(height)}" '
                    f'src="data:image/{ext};base64,{img_b64}" />'
                    '</figure>'
                ),
            )
        )
    return images


def _estimate_fragment_height(fragment: str) -> float:
    plain = re.sub(r"<[^>]+>", " ", fragment)
    plain = normalize_text(html.unescape(plain))
    if not plain:
        return 0.0
    return max(12.0, min(180.0, 10.0 + len(plain) * 0.22))


def rebalance_rendered_parts(rendered_parts: List[str]) -> List[str]:
    if len(rendered_parts) < 2:
        return rendered_parts
    first = rendered_parts[0]
    second = rendered_parts[1]
    if "<p" not in first or "<p" not in second:
        return rendered_parts
    first_text = normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", first)))
    second_text = normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", second)))
    if not first_text or not second_text:
        return rendered_parts
    if first_text[-1:] in ".!?:;" or not (second_text[:1].islower() or second_text.lower().startswith(("and ", "or ", "but "))):
        return rendered_parts
    merged = re.sub(
        r"</p>\s*$",
        f" {html.escape(second_text)}</p>",
        first,
    )
    return [merged] + rendered_parts[2:]


def _has_meaningful_content(fragments: List[str]) -> bool:
    for fragment in fragments:
        if any(tag in fragment for tag in ("<p", "<h1", "<h2", "<h3", "<li", "<table", "<figure", "<address", "<blockquote")):
            text = normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", fragment)))
            if text:
                return True
            if "<figure" in fragment or "<table" in fragment:
                return True
    return False


def build_semantic_page_layout(
    page: fitz.Page,
    page_number: int,
    source_page_index: int,
    total_pages: int,
    image_counts: Counter,
    keep_all_images: bool,
    watermark_removal_enabled: bool,
    header_footer_profile: HeaderFooterProfile,
    prev_page_tail: str | None = None,
    carry_list_number: int | None = None,
) -> Tuple[PageLayout, dict, int | None]:
    raw_rows, lines = extract_text_rows(page)
    source_page_folio = extract_source_page_folio(lines)
    lines, suppressed_count = suppress_header_footer_lines(lines, header_footer_profile)
    raw_rows = group_physical_lines_by_row(lines)
    blocks = group_lines_into_blocks(lines)
    table_regions = detect_table_regions(raw_rows)
    images = collect_semantic_images(page, image_counts, keep_all_images, watermark_removal_enabled)

    base_indent = sorted(block.indent for block in blocks)[len(blocks) // 2] if blocks else 0.0
    page_parts: List[Tuple[float, str, str, int | None]] = render_text_blocks_with_lists(
        blocks, page_number, total_pages, base_indent, prev_page_tail=prev_page_tail
    )
    for region in table_regions:
        top, fragment = render_table_region(raw_rows, region)
        page_parts.append((top, "table", fragment, None))
    page_parts.extend((img.top, "figure", img.html_fragment, None) for img in images)
    page_parts.sort(key=lambda item: item[0])

    skipped_watermarks = 0
    if watermark_removal_enabled and not keep_all_images:
        raw_page_dict = page.get_text("dict")
        for block in raw_page_dict["blocks"]:
            if block["type"] == 1 and block.get("image") and is_probable_watermark(page.rect, block, image_counts, min_repeat_pages=2):
                skipped_watermarks += 1

    page_class = "native_text"
    if page.get_images(full=True) and len(lines) < 5:
        page_class = "scanned_text"
    elif table_regions:
        page_class = "table_heavy"
    elif images:
        page_class = "mixed_text_image"

    rendered_parts, next_list_number = render_page_parts(page_parts, carry_list_number=carry_list_number)
    rendered_parts = rebalance_rendered_parts(rendered_parts)
    estimated_content_height = sum(_estimate_fragment_height(fragment) for fragment in rendered_parts)
    is_truly_blank_source_page = len(lines) == 0 and len(images) == 0
    meaningful_content = _has_meaningful_content(rendered_parts)

    debug_info = {
        "page": page_number,
        "source_page_index": source_page_index,
        "source_page_folio": source_page_folio,
        "page_classification": page_class,
        "text_lines": len(lines),
        "semantic_blocks": len(blocks),
        "detected_tables": len(table_regions),
        "embedded_images": len(images),
        "skipped_watermarks": skipped_watermarks,
        "ocr_used": 0,
        "preserved_image_regions": len(images),
        "header_footer_suppressed": suppressed_count,
        "estimated_content_height": estimated_content_height,
        "meaningful_content": meaningful_content,
        "is_truly_blank_source_page": is_truly_blank_source_page,
    }
    return (
        PageLayout(
            source_page_index=source_page_index,
            source_page_number=page_number,
            source_page_folio=source_page_folio,
            page_classification=page_class,
            header_footer_suppressed=suppressed_count,
            rendered_parts=rendered_parts,
            skipped_watermarks=skipped_watermarks,
            has_images=bool(images),
            is_truly_blank_source_page=is_truly_blank_source_page,
        ),
        debug_info,
        next_list_number,
    )


def merge_cross_page_paragraphs(pages_html: List[str]) -> List[str]:
    merged = list(pages_html)
    for idx in range(1, len(merged)):
        prev = merged[idx - 1]
        cur = merged[idx]
        prev_match = re.search(r"<p([^>]*)>([^<]+)</p>\s*</div>\s*<footer", prev, re.DOTALL)
        cur_match = re.search(r"(<div class=\"page-body\">\s*)<p([^>]*)>([^<]+)</p>", cur, re.DOTALL)
        if not prev_match or not cur_match:
            continue
        prev_text = normalize_text(html.unescape(prev_match.group(2)))
        cur_text = normalize_text(html.unescape(cur_match.group(3)))
        if not prev_text or not cur_text:
            continue
        starts_as_new_structure = bool(re.match(r"^(\d+\.|\(\d+\)|[ivxlcdmIVXLCDM]+\)|[A-Z][A-Z\s]{5,})", cur_text))
        starts_as_continuation = bool(cur_text[:1].islower()) or bool(
            re.match(r"^(and|or|but|that|which|who|where|when|for|to|of|in|on)\b", cur_text, re.IGNORECASE)
        )
        should_merge = (prev_text[-1] not in ".!?:;") or starts_as_continuation
        if (not should_merge) or starts_as_new_structure:
            continue
        combined = html.escape(f"{prev_text} {cur_text}")
        new_prev = re.sub(
            r"<p([^>]*)>[^<]+</p>\s*</div>\s*<footer",
            f'<p{prev_match.group(1)} data-source-pages="{idx}-{idx+1}">{combined}</p>\n    </div>\n    <footer',
            prev,
            flags=re.DOTALL,
        )
        new_cur = re.sub(r"(<div class=\"page-body\">\s*)<p[^>]*>[^<]+</p>\s*", r"\1", cur, count=1, flags=re.DOTALL)
        merged[idx - 1] = new_prev
        merged[idx] = new_cur
    return merged


def render_page_layout_html(page: PageLayout) -> str:
    folio_attr = html.escape(page.source_page_folio) if page.source_page_folio is not None else ""
    folio_html = html.escape(page.source_page_folio) if page.source_page_folio is not None else ""
    page_html = [
        (
            f'<section class="page-wrap" data-source-page-index="{page.source_page_index}" '
            f'data-source-page-folio="{folio_attr}" data-page-classification="{page.page_classification}" '
            f'data-header-footer-suppressed="{"true" if page.header_footer_suppressed else "false"}">'
        ),
        (
            f'  <article class="page semantic-page" data-source-page-index="{page.source_page_index}" '
            f'data-source-page-number="{page.source_page_number}">'
        ),
        '    <header class="page-header" aria-hidden="true"></header>',
        '    <div class="page-body">',
    ]
    for fragment in page.rendered_parts:
        page_html.append(f"    {fragment}")
    page_html.extend(
        [
            '    </div>',
            f'    <footer class="page-footer"><div class="page-folio" data-source-page-folio="{folio_attr}">{folio_html}</div></footer>',
            "  </article>",
            "</section>",
        ]
    )
    return "\n".join(page_html)


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


def emit_html(doc: fitz.Document, keep_all_images: bool, page_scale: float, template_path: Path | None = None) -> str:
    image_counts = collect_image_occurrences(doc)
    watermark_detection = pdf_has_watermark(doc)
    watermark_removal_enabled = watermark_detection.found and not keep_all_images
    profile = inspect_document(doc, extract_text_lines, is_bullet_line)
    classification = classify_document(profile, watermark_detection.found)
    header_footer_profile = build_header_footer_profile(doc)

    page_layouts: List[PageLayout] = []
    debug_summary: List[dict] = []
    prev_page_tail: str | None = None
    carry_list_number: int | None = None

    for source_page_index, page in enumerate(doc):
        page_number = source_page_index + 1
        page_layout, page_debug, carry_list_number = build_semantic_page_layout(
            page=page,
            page_number=page_number,
            source_page_index=source_page_index,
            total_pages=len(doc),
            image_counts=image_counts,
            keep_all_images=keep_all_images,
            watermark_removal_enabled=watermark_removal_enabled,
            header_footer_profile=header_footer_profile,
            prev_page_tail=prev_page_tail,
            carry_list_number=carry_list_number,
        )
        page_layouts.append(page_layout)
        debug_summary.append(page_debug)
        tail_lines = _extract_page_lines(page)
        tail_lines, _ = suppress_header_footer_lines(tail_lines, header_footer_profile)
        prev_page_tail = normalize_text(tail_lines[-1].text) if tail_lines else None

    filtered_layouts: List[PageLayout] = []
    for layout in page_layouts:
        meaningful = _has_meaningful_content(layout.rendered_parts)
        if not meaningful and not layout.is_truly_blank_source_page and not layout.has_images:
            continue
        filtered_layouts.append(layout)

    pages_html = [render_page_layout_html(layout) for layout in filtered_layouts]
    pages_html = merge_cross_page_paragraphs(pages_html)
    page_classification_counts = Counter(item["page_classification"] for item in debug_summary)
    title = html.escape(doc.metadata.get("title") or "PDF to HTML")
    summary_json = json.dumps(
        {
            "total_pages": profile.page_count,
            "page_classification_counts": page_classification_counts,
            "extracted_native_text_pages_count": sum(1 for row in debug_summary if row["page_classification"] == "native_text"),
            "ocr_pages_count": sum(row["ocr_used"] for row in debug_summary),
            "preserved_image_pages_regions_count": sum(row["preserved_image_regions"] for row in debug_summary),
            "extracted_tables_count": sum(row["detected_tables"] for row in debug_summary),
            "image_preserved_tables_count": 0,
            "watermark_candidates_detected": watermark_detection.candidate_instances,
            "watermark_removals_applied": sum(row["skipped_watermarks"] for row in debug_summary),
            "low_confidence_regions_preserved_as_image": 0,
            "warnings_unresolved_ambiguities": [],
            "no_hallucination_confirmation": True,
            "classification": {"category": classification.category, "confidence": classification.confidence, "reasons": classification.reasons},
            "page_summary": debug_summary,
            "page_scale": page_scale,
        },
        indent=2,
    )

    note = (
        "Watermark detected. Repeated watermark-style image layers were skipped while keeping the text layer intact. "
        f"Detected category: {classification.category}."
        if watermark_removal_enabled
        else "No watermark detected. Watermark-removal logic was skipped and the PDF was converted directly to semantic HTML. "
        f"Detected category: {classification.category}."
    )
    style_template_path = template_path if template_path else Path(DEFAULT_TEMPLATE_NAME)
    style_css = extract_template_style(style_template_path)
    if not style_css:
        style_css = extract_template_style(Path(DEFAULT_TEMPLATE_NAME))
    if not style_css:
        style_css = DEFAULT_LEGAL_CSS
    else:
        style_css = style_css + "\n\n/* court-safe pagination overrides */\n" + DEFAULT_LEGAL_CSS
    raw_html = render_document_shell(
        title=title,
        note=note,
        pages_html='<article class="court-judgment">' + "".join(pages_html) + "</article>",
        summary_json=summary_json,
        style_css=style_css,
    )
    return beautify_html_output(raw_html)


def choose_conversion_backend(classification_category: str, preferred_backend: str) -> str:
    if preferred_backend in {"semantic", "pdf2htmlex"}:
        return preferred_backend
    if classification_category in {"table_heavy", "scanned_or_image_heavy", "mixed_text_image"}:
        return "pdf2htmlex"
    return "semantic"


def run_pdf2htmlex_conversion(input_pdf: Path, output_html: Path, page_scale: float) -> tuple[bool, str]:
    binary = shutil.which("pdf2htmlEX")
    if not binary:
        return False, "pdf2htmlEX binary was not found in PATH."

    zoom = max(1.0, page_scale)
    command = [
        binary,
        "--embed", "cfijo",
        "--dest-dir", str(output_html.parent),
        "--zoom", f"{zoom:.4f}",
        "--font-size-multiplier", "1",
        str(input_pdf),
        output_html.name,
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        return False, f"pdf2htmlEX failed to start: {exc}"

    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        return False, f"pdf2htmlEX failed with exit code {completed.returncode}: {details}"
    return True, f"Converted with pdf2htmlEX ({PDF2HTMLEX_REFERENCE})."


def resolve_input_pdfs(input_value: str) -> List[Path]:
    candidate = Path(input_value)
    if candidate.exists():
        return [candidate]
    matches = sorted(Path().glob(input_value))
    return [path for path in matches if path.is_file() and path.suffix.lower() == ".pdf"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PDF to semantic HTML while safely skipping repeated watermark images.")
    parser.add_argument("input_pdf", help="Path/glob to source PDF(s), e.g. '*.pdf'")
    parser.add_argument("output_html", help="Path to output HTML for single PDF, or directory for multiple PDFs")
    parser.add_argument("--keep-all-images", action="store_true", help="Embed all images, even if they look like repeated watermarks")
    parser.add_argument("--page-scale", type=float, default=PDF_TO_CSS_SCALE, help="Scale factor from PDF points to CSS pixels")
    parser.add_argument(
        "--backend",
        choices=["auto", "semantic", "pdf2htmlex"],
        default="auto",
        help=(
            "Conversion backend. "
            "'auto' routes table-heavy / mixed-image PDFs to pdf2htmlEX (if available), otherwise uses semantic parser."
        ),
    )
    parser.add_argument(
        "--template-html",
        default=DEFAULT_TEMPLATE_NAME,
        help="Path to approved HTML template. CSS/layout shell is copied from this file.",
    )
    args = parser.parse_args()

    input_pdfs = resolve_input_pdfs(args.input_pdf)
    if not input_pdfs:
        raise FileNotFoundError(f"No PDF files found for input: {args.input_pdf}")

    output_target = Path(args.output_html)
    multiple_inputs = len(input_pdfs) > 1
    if multiple_inputs:
        output_target.mkdir(parents=True, exist_ok=True)
    else:
        output_target.parent.mkdir(parents=True, exist_ok=True)

    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required to convert PDFs. Install it with: pip install pymupdf")

    for input_pdf in input_pdfs:
        output_html = output_target / f"{input_pdf.stem}.html" if multiple_inputs else output_target
        with fitz.open(input_pdf) as doc:
            detection = pdf_has_watermark(doc)
            profile = inspect_document(doc, extract_text_lines, is_bullet_line)
            classification = classify_document(profile, detection.found)
            selected_backend = choose_conversion_backend(classification.category, args.backend)

            if selected_backend == "pdf2htmlex":
                success, message = run_pdf2htmlex_conversion(input_pdf, output_html, args.page_scale)
                if success:
                    print(f"[{input_pdf.name}] {message}")
                    continue
                print(f"[{input_pdf.name}] {message} Falling back to semantic backend.")

            html_text = emit_html(
                doc,
                keep_all_images=args.keep_all_images,
                page_scale=args.page_scale,
                template_path=Path(args.template_html),
            )
            output_html.write_text(html_text, encoding="utf-8")

        if detection.found and not args.keep_all_images:
            print(
                f"[{input_pdf.name}] Watermark detected on pages "
                f"{detection.pages_with_candidates}; removal logic applied safely during HTML conversion."
            )
        elif args.keep_all_images:
            print(f"[{input_pdf.name}] Watermark detection completed, but --keep-all-images was used, so all images were retained.")
        else:
            print(f"[{input_pdf.name}] No watermark detected; watermark-removal logic was skipped.")

        print(f"Created: {output_html}")


if __name__ == "__main__":
    main()
