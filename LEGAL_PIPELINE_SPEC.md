# Legal Judgment PDF→HTML Pipeline Contract (Indian Supreme Court / High Courts)

## A) Extraction and Reconstruction Strategy

1. **Classify page/region first** into `native_text`, `mixed_text_image`, `scanned_text`, `table_heavy`, `annexure_or_exhibit`, `signature_or_stamp_region`, `watermark_candidate`.
2. **Use extraction by class**:
   - native text: extract with PDF coordinates/font metadata.
   - mixed pages: native extraction + selective OCR for textless regions.
   - scanned pages: OCR plus preserved page image.
   - annexure/signature/stamp regions: preserve in-place as figures.
   - table-heavy regions: table engine with confidence gate.
3. **Reconstruct reading order** with bbox, indentation, alignment, heading cues, and legal numbering progression.
4. **Preserve legal structure**: caption/cause-title/front matter/coram/headings/numbered paragraphs/operative directions/signature blocks.
5. **Merge page-break paragraph continuations** only when continuity signals are strong and no heading/list/table boundary starts the next page.
6. **Render semantic HTML** with provenance attributes (`data-source-page`, extraction method, confidence, block role).

## B) Detailed Implementation Plan

1. `PageClassifier`:
   - per-page and per-region signals (text density, image area ratio, OCR need, table signatures).
2. `ExtractorRouter`:
   - native extractor, OCR extractor, hybrid region extractor.
3. `LegalStructureBuilder`:
   - heading/caption/coram detection, numbered paragraph parser, list nesting parser.
4. `ContinuationMerger`:
   - cross-page paragraph continuation rules (sentence-ending + indentation + marker checks).
5. `TableDecisionEngine`:
   - high-confidence table → `<table>`; else preserve as `<figure class="preserved-table-image">`.
6. `WatermarkSafetyFilter`:
   - repeated background-only watermark removal with explicit safeguards for seals/signatures/stamps.
7. `HTMLAssembler`:
   - emits a single `article.court-judgment`, semantic sections, restrained CSS (screen+print A4).
8. `ConversionSummaryBuilder`:
   - machine-readable summary JSON for auditability and no-hallucination traceability.

## C) Final HTML Output Contract

- Exactly one root legal article:
  - `<article class="court-judgment">`
- Includes:
  - `<header>` for court/case metadata
  - logical `<section>` blocks for facts/submissions/analysis/findings/directions/annexures
  - semantic paragraph/list/table/figure elements
- Required attributes wherever available:
  - `data-source-page`
  - `data-source-pages`
  - `data-extraction-method="native|ocr|hybrid|image-preserved|table-extracted"`
  - `data-confidence`
  - `data-block-role`
  - `data-watermark-removed`
  - `data-watermark-removal-reason`
  - `data-preserved-image-block`
- Conversion summary is embedded as machine-readable JSON.

## D) Post-processing Rules

1. **Paragraph repair**:
   - merge only when prior block is non-terminal and next block is continuation-like.
   - never merge when next block begins heading/list/fresh numbering/table/figure/annexure/signature.
2. **List reconstruction**:
   - preserve decimal/alphabetic/roman markers and nesting depth.
3. **Numbering fidelity**:
   - keep exact markers (`1.`, `1.1`, `(a)`, `i.`, `I.`), never normalize away legal meaning.
4. **Table safety**:
   - no paragraph-to-table coercion.
   - low-confidence/complex tables become preserved images.
5. **Watermark safety**:
   - remove only repeated background candidates and log reason.

## E) Failure-handling Rules

1. Never fabricate unreadable text.
2. On low OCR confidence, preserve region/page as image and mark low confidence.
3. If table extraction confidence is low, preserve the table region as image.
4. If reading order is ambiguous, keep source order and add warning in conversion summary.
5. If watermark classification is uncertain, do **not** remove.
6. Prefer evidentiary preservation over aggressive cleanup.

## F) Conversion Summary Schema

```json
{
  "total_pages": 0,
  "page_classification_counts": {
    "native_text": 0,
    "mixed_text_image": 0,
    "scanned_text": 0,
    "table_heavy": 0,
    "annexure_or_exhibit": 0
  },
  "extracted_native_text_pages_count": 0,
  "ocr_pages_count": 0,
  "preserved_image_pages_regions_count": 0,
  "extracted_tables_count": 0,
  "image_preserved_tables_count": 0,
  "watermark_candidates_detected": 0,
  "watermark_removals_applied": 0,
  "low_confidence_regions_preserved_as_image": 0,
  "warnings_unresolved_ambiguities": [],
  "no_hallucination_confirmation": true,
  "page_summary": []
}
```
