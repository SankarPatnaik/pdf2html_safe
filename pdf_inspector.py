from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import List


@dataclass
class PageSignals:
    page_number: int
    text_line_count: int
    image_block_count: int
    word_count: int
    table_like_rows: int
    bullet_like_rows: int
    heading_like_rows: int
    repeated_header_hits: int = 0
    repeated_footer_hits: int = 0


@dataclass
class DocumentProfile:
    page_count: int
    pages: List[PageSignals] = field(default_factory=list)
    repeated_headers: List[str] = field(default_factory=list)
    repeated_footers: List[str] = field(default_factory=list)
    image_only_pages: List[int] = field(default_factory=list)

    @property
    def total_words(self) -> int:
        return sum(p.word_count for p in self.pages)

    @property
    def total_bullets(self) -> int:
        return sum(p.bullet_like_rows for p in self.pages)

    @property
    def total_table_rows(self) -> int:
        return sum(p.table_like_rows for p in self.pages)

    @property
    def total_headings(self) -> int:
        return sum(p.heading_like_rows for p in self.pages)


def _compact(text: str) -> str:
    return " ".join(text.split()).strip()


def inspect_document(doc, line_extractor, is_bullet_line) -> DocumentProfile:
    pages: List[PageSignals] = []
    header_candidates: Counter[str] = Counter()
    footer_candidates: Counter[str] = Counter()

    for page_number, page in enumerate(doc, start=1):
        rows = line_extractor(page)
        word_count = sum(len(row.text.split()) for row in rows)
        bullet_rows = sum(1 for row in rows if is_bullet_line(row.text))
        heading_rows = sum(1 for row in rows if len(row.text) <= 120 and row.is_centered and row.uppercase_ratio >= 0.55)
        table_like_rows = sum(1 for row in rows if row.text.count("  ") >= 1)

        if rows:
            header_text = _compact(rows[0].text)
            footer_text = _compact(rows[-1].text)
            if header_text:
                header_candidates[header_text] += 1
            if footer_text:
                footer_candidates[footer_text] += 1

        image_block_count = sum(1 for b in page.get_text("dict")["blocks"] if b.get("type") == 1)
        pages.append(
            PageSignals(
                page_number=page_number,
                text_line_count=len(rows),
                image_block_count=image_block_count,
                word_count=word_count,
                table_like_rows=table_like_rows,
                bullet_like_rows=bullet_rows,
                heading_like_rows=heading_rows,
            )
        )

    repeated_headers = [t for t, c in header_candidates.items() if c >= 2 and len(t) <= 160]
    repeated_footers = [t for t, c in footer_candidates.items() if c >= 2 and len(t) <= 160]

    image_only_pages = [
        p.page_number for p in pages if p.text_line_count <= 2 and p.image_block_count >= 1
    ]

    header_set = set(repeated_headers)
    footer_set = set(repeated_footers)
    for p in pages:
        # best effort: only uses first and last row heuristics already tracked above
        if repeated_headers and p.text_line_count:
            p.repeated_header_hits = 1
        if repeated_footers and p.text_line_count:
            p.repeated_footer_hits = 1

    return DocumentProfile(
        page_count=len(doc),
        pages=pages,
        repeated_headers=repeated_headers,
        repeated_footers=repeated_footers,
        image_only_pages=image_only_pages,
    )
