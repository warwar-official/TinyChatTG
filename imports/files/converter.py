"""Document conversion utilities.

Provides two async functions:
  - convert_office_to_markdown: uses system pandoc to convert .docx/.odt/etc.
  - convert_pdf_to_markdown:    uses PyMuPDF to render pages + Gemini OCR.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
import tempfile
import fitz
import pypandoc as pypandoc
from pathlib import Path
from typing import List, Optional
from imports.providers.gemini import GeminiProvider

logger = logging.getLogger(__name__)

# Extensions that Pandoc can read from (input formats)
PANDOC_INPUT_EXTENSIONS = {
    ".docx", ".odt", ".rtf", ".pptx", ".odp",
    ".epub", ".rst", ".tex", ".latex", ".org",
    ".textile", ".wiki", ".opml",
}

# Windows/legacy media types that cannot be displayed or embedded — skip them
_SKIP_MEDIA_EXTENSIONS = {".emf", ".wmf", ".swf", ".ole", ".bin", ".wmz", ".emz"}

# OCR prompt for PDF pages
_PDF_OCR_PROMPT = (
    "You are an OCR assistant. Convert the following document page image to clean Markdown.\n"
    "Rules:\n"
    "- Preserve headings (use # ## ###), bold, italic, bullet lists, numbered lists, tables.\n"
    "- Describe images/figures/charts briefly in brackets, e.g.: [Figure: bar chart showing sales by quarter]\n"
    "- Do NOT add any introduction or commentary — return ONLY the Markdown content.\n"
    "- If the page is blank, return an empty string.\n"
    "- Use English for all Markdown structure keywords; preserve original language for content text."
)


# ─── Office conversion ────────────────────────────────────────────────────────

async def convert_office_to_markdown(
    src_path: Path,
    media_dir: Path,
) -> str:
    """Convert an office document to Markdown using Pandoc.

    Media files (images etc.) are extracted to *media_dir*.
    Unsupported media types (EMF, WMF, …) are logged as warnings and skipped
    from the returned markdown by replacing broken image references.

    Returns the Markdown text.
    Raises RuntimeError on pandoc failure.
    """
    media_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        out_path = Path(tmp.name)
        await asyncio.to_thread(
            pypandoc.convert_file,
            str(src_path),
            "md",
            outputfile=str(out_path),
            extra_args=["--wrap=none", f"--extract-media={media_dir}"],
        )
        md_text = out_path.read_text(encoding="utf-8", errors="replace")
            
    # Walk extracted media — warn about unsupported types
    md_text = _sanitize_media_references(md_text, media_dir, src_path.name)
    return md_text

def _sanitize_media_references(md_text: str, media_dir: Path, doc_name: str) -> str:
    """Remove image references to unsupported media types (EMF, WMF, …) with a log warning.

    Also rewrites image paths to be relative to the media_dir parent so that
    the file_read_lines tool can resolve them consistently.
    """
    image_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

    def _rewrite(m: re.Match) -> str:
        alt = m.group(1)
        raw_path = m.group(2)
        p = Path(raw_path)
        ext = p.suffix.lower()
        if ext in _SKIP_MEDIA_EXTENSIONS:
            logger.warning(
                "Skipping unconvertable media '%s' in '%s' (extension %s not supported)",
                p.name, doc_name, ext,
            )
            return f"<!-- skipped media: {p.name} (unsupported format {ext}) -->"
        # Keep the reference as-is; path resolution happens in file_read_lines
        return m.group(0)

    return image_pattern.sub(_rewrite, md_text)


# ─── PDF conversion ───────────────────────────────────────────────────────────

async def convert_pdf_to_markdown(
    src_path: Path,
    gemini_provider: "GeminiProvider",
    ocr_model: str = "gemini-3.1-flash-lite",
    dpi: int = 200,
    media_dir: Optional[Path] = None,
) -> str:
    """Convert a PDF to Markdown using PyMuPDF page rendering + Gemini OCR.

    Each page is rendered at *dpi* resolution (default 200, enough for OCR).
    Pages are sent to the Gemini model one-by-one as images.

    Returns combined Markdown text (pages separated by '\\n\\n---\\n\\n').
    Raises ImportError if fitz (PyMuPDF) is unavailable.
    Raises RuntimeError on critical processing errors.
    """

    fitz.TOOLS.mupdf_display_errors(False)
    doc = fitz.open(str(src_path))
    page_count = len(doc)
    logger.info("PDF conversion: %s has %d pages (DPI=%d)", src_path.name, page_count, dpi)

    page_markdowns: List[str] = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)  # 72 DPI is the PDF native resolution

    # Prepare media dir if requested
    use_temp_media = False
    if media_dir:
        media_dir.mkdir(parents=True, exist_ok=True)
    else:
        # If caller didn't provide a media_dir we create a temporary one which
        # will be removed when the function exits.
        tmp = tempfile.TemporaryDirectory()
        media_dir = Path(tmp.name)
        use_temp_media = True

    img_counter = 0
    for page_num in range(page_count):
        page = doc.load_page(page_num)

        # Analyze page content blocks to decide whether to OCR or extract
        page_dict = page.get_text("dict") or {}
        blocks = page_dict.get("blocks", [])
        page_rect = page.rect
        page_area = float(page_rect.width * page_rect.height) if page_rect.width and page_rect.height else 1.0

        image_area = 0.0
        has_text = False
        has_image = False

        for b in blocks:
            btype = b.get("type", 0)
            if btype == 1:  # image block
                has_image = True
                bbox = b.get("bbox") or [0, 0, 0, 0]
                try:
                    w = float(bbox[2]) - float(bbox[0])
                    h = float(bbox[3]) - float(bbox[1])
                    if w > 0 and h > 0:
                        image_area += max(0.0, w * h)
                except Exception:
                    pass
            elif btype == 0:  # text block
                # Inspect spans for actual text
                for line in b.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            has_text = True
                            break
                    if has_text:
                        break

        image_coverage = min(image_area / page_area, 1.0)

        logger.debug(
            "PDF conversion: page %d/%d — text=%s image=%s coverage=%.2f",
            page_num + 1, page_count, has_text, has_image, image_coverage,
        )

        # If the page is mostly images, use OCR
        if image_coverage > 0.7:
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("jpeg", jpg_quality=85)
            b64 = base64.b64encode(img_bytes).decode("utf-8")

            logger.debug("PDF conversion: OCR page %d/%d (%d bytes)", page_num + 1, page_count, len(img_bytes))

            messages = [
                {"role": "system", "content": _PDF_OCR_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Page {page_num + 1} of {page_count}:"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                },
            ]

            try:
                resp = await gemini_provider.chat(messages, model=ocr_model, timeout=120)
                if isinstance(resp, dict) and resp.get("error"):
                    logger.warning("PDF OCR: model error on page %d: %s", page_num + 1, resp["error"])
                    page_markdowns.append(f"<!-- OCR failed for page {page_num + 1} -->")
                    continue

                choices = resp.get("choices", [])
                if choices:
                    content = (choices[0].get("message") or {}).get("content") or ""
                    page_markdowns.append(content.strip())
                else:
                    page_markdowns.append(f"<!-- empty OCR for page {page_num + 1} -->")

            except Exception as e:
                logger.warning("PDF OCR: exception on page %d: %s", page_num + 1, e)
                page_markdowns.append(f"<!-- OCR error for page {page_num + 1}: {e} -->")
            continue

        # Otherwise, extract text and embedded images in visual order
        parts: List[str] = []
        for b in blocks:
            btype = b.get("type", 0)
            if btype == 0:
                # Text block: join spans into lines
                lines = []
                for line in b.get("lines", []):
                    spans = [s.get("text", "") for s in line.get("spans", [])]
                    if spans:
                        lines.append("".join(spans))
                text_block = "\n".join(lines).strip()
                if text_block:
                    parts.append(text_block)
            elif btype == 1:
                # Image block: try to extract the image by xref
                imginfo = b.get("image") or {}
                xref = imginfo.get("xref") if isinstance(imginfo, dict) else None
                image_bytes = None
                ext = None
                if xref:
                    try:
                        xref = int(xref)
                        imgdict = doc.extract_image(xref)
                        image_bytes = imgdict.get("image")
                        ext = imgdict.get("ext")
                    except Exception:
                        image_bytes = None

                # Fallback: try to grab any page images (first unseen) if xref not available
                if image_bytes is None:
                    try:
                        for img in page.get_images(full=True):
                            try_xref = img[0]
                            imgdict = doc.extract_image(try_xref)
                            image_bytes = imgdict.get("image")
                            ext = imgdict.get("ext")
                            break
                    except Exception:
                        image_bytes = None

                if image_bytes:
                    img_counter += 1
                    ext = ext or "png"
                    fname = f"{src_path.stem}_page{page_num + 1}_img{img_counter}.{ext}"
                    try:
                        media_dir.mkdir(parents=True, exist_ok=True)
                        outp = media_dir / fname
                        outp.write_bytes(image_bytes)
                        rel = f"{media_dir.name}/{fname}"
                        parts.append(f"![Figure: page {page_num + 1} image {img_counter}]({rel})")
                    except Exception as e:
                        logger.warning("Failed to write extracted image for page %d: %s", page_num + 1, e)

        page_text = "\n\n".join(parts).strip()
        if page_text:
            page_markdowns.append(page_text)

    # Clean up temporary media dir if we created it
    if use_temp_media:
        try:
            tmp.cleanup()
        except Exception:
            pass

    doc.close()
    return "\n\n---\n\n".join(p for p in page_markdowns if p)
