"""
textpdf.py — render extracted text into a paginated PDF.

Used by the pipeline to save a readable text version of a *scanned* book after
OCR (text-based PDFs already have a text layer, so this is skipped for them).

Uses PyMuPDF's Story engine, which reflows text and paginates across pages
automatically — no manual overflow bookkeeping. Plain text is wrapped into
minimal HTML (one <p> per paragraph) because Story lays out HTML.
"""

from __future__ import annotations

import html as _html
import re
from pathlib import Path

import fitz  # PyMuPDF


def _text_to_html(text: str, title: str | None, font_pt: int) -> str:
    """Wrap plain text as minimal HTML: a heading plus one <p> per paragraph."""
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    body = "".join(f"<p>{_html.escape(p)}</p>" for p in paragraphs)
    heading = f"<h1>{_html.escape(title)}</h1>" if title else ""
    return (
        "<html><head><style>"
        f"body{{font-family:serif;font-size:{font_pt}pt;}}"
        "h1{font-family:serif;font-size:1.6em;margin:0 0 0.6em 0;}"
        "p{margin:0 0 0.5em 0;text-align:justify;line-height:1.4;}"
        "</style></head><body>"
        f"{heading}{body}</body></html>"
    )


def write_text_pdf(
    text: str,
    out_path: str | Path,
    *,
    title: str | None = None,
    font_pt: int = 12,
    margin: float = 54.0,   # 0.75 inch
) -> Path:
    """
    Lay `text` into a paginated PDF at out_path. Returns the path. Raises loudly
    on empty text (nothing to render).
    """
    if not text.strip():
        raise ValueError("write_text_pdf: no text to render.")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    story = fitz.Story(html=_text_to_html(text, title, font_pt))
    mediabox = fitz.paper_rect("letter")
    where = mediabox + (margin, margin, -margin, -margin)

    writer = fitz.DocumentWriter(str(out))
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return out


# --- CLI / smoke test --------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        # Self-contained demo: render enough text to force multiple pages.
        demo = "\n\n".join(
            f"Paragraph {i}. " + ("The quick brown fox jumps over the lazy dog. " * 8)
            for i in range(1, 40)
        )
        out = write_text_pdf(demo, "textpdf_demo.pdf", title="Demo Book")
        doc = fitz.open(out)
        print(f"wrote {out}  ({doc.page_count} pages)")
        doc.close()
    else:
        raw = Path(sys.argv[1]).read_text(encoding="utf-8")
        out = write_text_pdf(raw, sys.argv[2] if len(sys.argv) > 2 else "out_text.pdf")
        doc = fitz.open(out)
        print(f"wrote {out}  ({doc.page_count} pages)")
        doc.close()
