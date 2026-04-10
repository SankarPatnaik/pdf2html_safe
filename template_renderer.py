from __future__ import annotations

import html
import re
from pathlib import Path


def extract_template_style(template_path: Path) -> str:
    if not template_path.exists():
        return ""
    source = template_path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"<style>(.*?)</style>", source, re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip()


def render_document_shell(*, title: str, note: str, pages_html: str, summary_json: str, style_css: str) -> str:
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{html.escape(title)}</title>
  <style>
{style_css}
  </style>
</head>
<body>
  <main class=\"doc-shell\">
    <div class=\"doc-meta\">{html.escape(note)}</div>
{pages_html}
  </main>
  <!-- conversion-summary
  {html.escape(summary_json)}
  -->
</body>
</html>
"""
