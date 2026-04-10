from __future__ import annotations

from dataclasses import dataclass

from pdf_inspector import DocumentProfile


@dataclass
class ClassificationResult:
    category: str
    confidence: float
    reasons: list[str]


def classify_document(profile: DocumentProfile, watermark_detected: bool) -> ClassificationResult:
    reasons: list[str] = []
    total_lines = sum(p.text_line_count for p in profile.pages) or 1
    total_images = sum(p.image_block_count for p in profile.pages)
    bullet_ratio = profile.total_bullets / total_lines
    table_ratio = profile.total_table_rows / total_lines
    heading_ratio = profile.total_headings / total_lines

    if profile.image_only_pages and len(profile.image_only_pages) >= max(1, profile.page_count // 2):
        reasons.append("Most pages have almost no text layer and include image blocks.")
        return ClassificationResult("scanned_or_image_heavy", 0.92, reasons)

    if table_ratio >= 0.28:
        reasons.append("Detected repeated row alignment and dense multi-column rows.")
        return ClassificationResult("table_heavy", 0.83, reasons)

    if bullet_ratio >= 0.22:
        reasons.append("Detected high bullet/numbered-list density.")
        return ClassificationResult("bullet_heavy", 0.81, reasons)

    if heading_ratio >= 0.16 and profile.total_words > 300:
        reasons.append("Detected frequent heading-like centered rows with body paragraphs.")
        return ClassificationResult("report_with_numbered_headings", 0.74, reasons)

    if total_images >= profile.page_count and profile.total_words > 0:
        reasons.append("Detected both text and non-trivial image presence across pages.")
        return ClassificationResult("mixed_text_image", 0.68, reasons)

    if watermark_detected:
        reasons.append("Detected repeated centered image candidates behaving as watermark overlays.")
        return ClassificationResult("watermark_background_pdf", 0.79, reasons)

    reasons.append("General legal/prose layout with structured paragraphs and occasional numbering.")
    return ClassificationResult("legal_judgment", 0.62, reasons)
