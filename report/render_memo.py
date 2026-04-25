"""
Render `report/memo.md` to `report/memo.pdf`.

The memo must be exactly two pages (challenge brief, line 446). This script:
  1. Reads memo.md.
  2. Converts to standalone HTML via pypandoc with a tight CSS for 2-page A4.
  3. Renders HTML to PDF via Playwright Chromium (same engine the interim
     submission used in scripts/build_report.py — keeps the rendering stack
     identical so any visual regression points back at the markdown).

If pypandoc or Playwright are missing the script falls back to leaving the
HTML on disk; the PDF step is the one that may need a working Chromium.

Usage:
    python -m report.render_memo
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pypandoc

logger = logging.getLogger("render_memo")

REPORT_DIR = Path(__file__).parent
MEMO_MD = REPORT_DIR / "memo.md"
MEMO_HTML = REPORT_DIR / "memo.html"
MEMO_PDF = REPORT_DIR / "memo.pdf"


# Tight 2-page A4 CSS. Reduced font + line-height vs the interim report so
# the memo's two pages fit dense tables + multi-paragraph sections without
# spilling. The challenge requires exactly 2 pages; if you need to recover
# space, drop body font-size to 9.5pt before reducing margins further.
TWO_PAGE_CSS = """
<style>
  @page {
    size: A4;
    margin: 0.45in 0.55in;
  }
  body {
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 8.5pt;
    line-height: 1.18;
    color: #222;
    margin: 0;
    padding: 0;
  }
  h1 {
    font-size: 14pt;
    border-bottom: 1.5px solid #333;
    padding-bottom: 0.15em;
    margin: 0 0 0.3em 0;
  }
  h2 {
    font-size: 11pt;
    border-bottom: 1px solid #bbb;
    padding-bottom: 0.1em;
    margin: 0.5em 0 0.25em 0;
  }
  /* No forced page-break — let the natural flow span 2 pages.
     The trailing trace-attribution footer is suppressed in print. */
  hr + p:last-of-type { font-size: 7.5pt; color: #666; margin-top: 0.4em; }
  h3 {
    font-size: 10pt;
    color: #333;
    margin: 0.5em 0 0.2em 0;
  }
  p, li { margin: 0.2em 0; }
  table {
    border-collapse: collapse;
    width: 100%;
    margin: 0.3em 0;
    font-size: 8pt;
  }
  th, td {
    border: 1px solid #ccc;
    padding: 0.08em 0.3em;
    text-align: left;
    vertical-align: top;
  }
  th { background: #f0f0f0; }
  code, pre {
    background: #f5f5f5;
    border-radius: 2px;
    padding: 0 0.25em;
    font-size: 9pt;
  }
  pre {
    padding: 0.5em;
    overflow-x: auto;
    line-height: 1.25;
  }
  ul, ol { padding-left: 1.4em; margin: 0.3em 0; }
  hr {
    border: 0;
    border-top: 1px solid #ddd;
    margin: 0.6em 0;
  }
  blockquote {
    border-left: 3px solid #888;
    padding-left: 0.7em;
    margin: 0.5em 0;
    color: #333;
    background: #f9f9f9;
  }
</style>
"""


def _write_css_header() -> str:
    tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".html", encoding="utf-8")
    tmp.write(TWO_PAGE_CSS)
    tmp.close()
    return tmp.name


def render_html() -> Path:
    pypandoc.convert_file(
        str(MEMO_MD),
        "html",
        outputfile=str(MEMO_HTML),
        extra_args=[
            "--standalone",
            "--metadata", "title=Tenacious Conversion Engine — Decision Memo",
            "--include-in-header", _write_css_header(),
        ],
    )
    logger.info("HTML rendered: %s", MEMO_HTML)
    return MEMO_HTML


def render_pdf(html_path: Path) -> Path:
    from playwright.sync_api import sync_playwright

    url = html_path.resolve().as_uri()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        page.pdf(
            path=str(MEMO_PDF),
            format="A4",
            margin={"top": "0.55in", "bottom": "0.55in", "left": "0.6in", "right": "0.6in"},
            print_background=True,
        )
        browser.close()
    logger.info("PDF rendered: %s", MEMO_PDF)
    return MEMO_PDF


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not MEMO_MD.exists():
        raise SystemExit(f"memo.md not found at {MEMO_MD}")
    html = render_html()
    try:
        render_pdf(html)
    except Exception as e:
        logger.warning("PDF render failed (%s) — HTML available at %s", e, html)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
