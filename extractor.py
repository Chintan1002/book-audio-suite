"""
extractor.py — Stage 1 of the PDF -> Audiobook pipeline.

Extracts text from a PDF page-by-page, auto-detecting whether each page is
text-based (fast path via PyMuPDF) or scanned (OCR fallback via Tesseract).

Design goals:
  * Per-PAGE detection, not per-file (books can be mixed).
  * Streaming (generator) so large books don't blow up memory.
  * Graceful failure: corrupt PDFs, blank pages, missing OCR deps.

Install:
    pip install pymupdf
    # OCR fallback (only needed for scanned PDFs):
    pip install pytesseract pillow
    # and the Tesseract binary itself:
    #   macOS:  brew install tesseract
    #   Ubuntu: sudo apt install tesseract-ocr
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator

import fitz  # PyMuPDF


# --- Tunables ----------------------------------------------------------------

# If a page yields fewer than this many characters from the text layer, we
# assume it's a scanned image (or effectively blank) and consider OCR.
MIN_CHARS_FOR_TEXT_PAGE = 25

# DPI to render a page at before OCR. 300 is the sweet spot for accuracy vs.
# speed; 150 is faster but hurts on small fonts.
OCR_RENDER_DPI = 300


class PageSource(str, Enum):
    """How a page's text was obtained — useful for logging/QA."""
    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    BLANK = "blank"
    ERROR = "error"


@dataclass
class PageResult:
    """One page's worth of extracted text plus provenance."""
    index: int          # 0-based page number
    text: str
    source: PageSource
    char_count: int


# --- OCR (lazy-imported so text-only users don't need Tesseract) -------------

def _ocr_page(page: "fitz.Page", dpi: int = OCR_RENDER_DPI) -> str:
    """Render a page to an image and OCR it. Raises if OCR deps are missing."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "OCR needed for a scanned page but 'pytesseract'/'pillow' are not "
            "installed. Run: pip install pytesseract pillow  (and install the "
            "Tesseract binary)."
        ) from e

    pix = page.get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return pytesseract.image_to_string(img)


# --- Public API --------------------------------------------------------------

def extract_pages(
    pdf_path: str | Path,
    *,
    min_chars: int = MIN_CHARS_FOR_TEXT_PAGE,
    enable_ocr: bool = True,
) -> Iterator[PageResult]:
    """
    Yield one PageResult per page, in reading order.

    The text layer is tried first. If it comes back too short, the page is
    treated as scanned and (optionally) sent to OCR. Streaming by design:
    iterate and write each page out rather than collecting them all.
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"No such PDF: {path}")

    try:
        doc = fitz.open(path)
    except Exception as e:  # corrupt / password / not-a-pdf
        raise RuntimeError(f"Could not open PDF '{path}': {e}") from e

    try:
        for i, page in enumerate(doc):
            try:
                text = page.get_text("text").strip()

                if len(text) >= min_chars:
                    yield PageResult(i, text, PageSource.TEXT_LAYER, len(text))
                    continue

                # Sparse text layer -> probably scanned. But confirm there's
                # actually an image before spending OCR time on a blank page.
                has_image = bool(page.get_images(full=True))
                if enable_ocr and has_image:
                    ocr_text = _ocr_page(page).strip()
                    src = PageSource.OCR if ocr_text else PageSource.BLANK
                    yield PageResult(i, ocr_text, src, len(ocr_text))
                else:
                    # Keep whatever little text there was (or nothing).
                    yield PageResult(i, text, PageSource.BLANK, len(text))

            except Exception as e:  # never let one bad page kill the whole book
                yield PageResult(i, "", PageSource.ERROR, 0)
                print(f"[warn] page {i} failed: {e}")
    finally:
        doc.close()


def extract_to_text(pdf_path: str | Path, **kwargs) -> str:
    """Convenience wrapper: full document as one cleaned-ish string."""
    parts = [p.text for p in extract_pages(pdf_path, **kwargs) if p.text]
    return "\n\n".join(parts)


# --- CLI / smoke test --------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python extractor.py <file.pdf> [out.txt]")
        raise SystemExit(1)

    src_pdf = sys.argv[1]
    out_txt = sys.argv[2] if len(sys.argv) > 2 else None

    counts: dict[str, int] = {}
    chunks: list[str] = []
    for page in extract_pages(src_pdf):
        counts[page.source.value] = counts.get(page.source.value, 0) + 1
        if page.text:
            chunks.append(page.text)

    print("pages by source:", counts)

    if out_txt:
        Path(out_txt).write_text("\n\n".join(chunks), encoding="utf-8")
        print(f"wrote {out_txt}")
