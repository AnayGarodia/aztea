"""
pdf_document_parser.py — Fetch a PDF URL and return structured content.

Input:
  {
    "url": "https://example.com/paper.pdf",   # required
    "max_pages": 50,                            # optional, hard cap 100
    "include_tables": true,                     # default true
    "max_text_chars": 60000                     # truncation guard, default 60k
  }

Output:
  {
    "url": str,
    "page_count": int,
    "pages_returned": int,
    "metadata": {
      "title": str | null,                # populated from PDF metadata
                                          # or, if empty, from a page-1
                                          # largest-font heuristic
      "title_source": "embedded" | "page1_heuristic" | null,
      "author": str | null,
      "subject": str | null,
      "creator": str | null,
      "producer": str | null,
      "creation_date": str | null
    },
    "text": str,                          # concatenated, truncated
    "pages": [{"page": int, "text": str, "char_count": int}],
    "tables": [
      {"page": int, "rows": int, "cols": int, "preview": [[str]]}
    ],
    "billing_units_actual": int           # = pages_returned (variable pricing)
  }

Runtime:
  pymupdf (fitz) for fast text extraction; pdfplumber for tabular parsing.
  Both pure Python — no system deps beyond what `pip install` provides.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import httpx

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_DEFAULT_MAX_PAGES = 50
_HARD_MAX_PAGES = 100
_DEFAULT_MAX_TEXT_CHARS = 60_000
_HARD_MAX_TEXT_CHARS = 200_000
_MIN_TEXT_CHARS = 1000
_FETCH_TIMEOUT_S = 20.0
_MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB hard cap
_TABLE_PREVIEW_ROWS = 10
_TABLE_PREVIEW_COLS = 8
_USER_AGENT = "Aztea-PDF-Parser/1.0"

# Title-fallback heuristic constants. Titles tend to sit in the top third of
# page 1 and use a notably larger font than body text. We bound length so a
# whole abstract glued together by a layout quirk doesn't get returned as a
# "title."
_TITLE_PAGE1_TOP_FRACTION = 0.30
_TITLE_MIN_CHARS = 4
_TITLE_MAX_CHARS = 200



def _stringify_meta(value: Any) -> str | None:
    if value is None or value == "":
        return None
    s = str(value).strip()
    return s or None


def _fetch_pdf(url: str) -> tuple[bytes | None, dict | None]:
    try:
        with httpx.Client(
            timeout=_FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*;q=0.5"},
        ) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return None, _err(
                        "pdf_document_parser.fetch_failed",
                        f"HTTP {resp.status_code} fetching PDF",
                    )["error"]
                ct = (resp.headers.get("content-type") or "").lower()
                # Some servers serve PDFs with octet-stream — accept both.
                if "pdf" not in ct and "octet-stream" not in ct:
                    # Fall through and let pymupdf decide; many CDNs lie about CT.
                    _LOG.info("Unexpected content-type for PDF fetch: %s", ct)
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > _MAX_PDF_BYTES:
                        return None, _err(
                            "pdf_document_parser.too_large",
                            f"PDF exceeds the {_MAX_PDF_BYTES // (1024 * 1024)} MB cap.",
                        )["error"]
                return b"".join(chunks), None
    except httpx.HTTPError as exc:
        return None, _err(
            "pdf_document_parser.fetch_failed",
            f"HTTP fetch failed: {type(exc).__name__}: {exc}",
        )["error"]


def _extract_tables(pdf_bytes: bytes, max_pages: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract tables and return ``(tables, attempt_info)``.

    The attempt_info dict surfaces whether pdfplumber was actually invoked,
    what it found, and the failure reason if any — so callers who asked for
    tables but got an empty list know the agent looked and what happened.
    """
    attempt: dict[str, Any] = {
        "attempted": True,
        "extractor": "pdfplumber",
        "available": False,
        "pages_scanned": 0,
        "tables_found": 0,
        "error": None,
    }
    try:
        import pdfplumber  # type: ignore[import]
    except ImportError:
        # Tables are optional; surface absence as empty rather than failing the call.
        attempt["available"] = False
        attempt["error"] = (
            "pdfplumber is not installed on this worker; "
            "install with `pip install pdfplumber>=0.11.0`."
        )
        return [], attempt
    attempt["available"] = True

    out: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages):
                if idx >= max_pages:
                    break
                attempt["pages_scanned"] = idx + 1
                try:
                    raw_tables = page.extract_tables() or []
                except Exception:
                    raw_tables = []
                for table in raw_tables:
                    if not table or not isinstance(table, list):
                        continue
                    cols = max((len(row) for row in table if row), default=0)
                    rows = len(table)
                    preview = [
                        [str(cell) if cell is not None else "" for cell in (row or [])][
                            :_TABLE_PREVIEW_COLS
                        ]
                        for row in table[:_TABLE_PREVIEW_ROWS]
                    ]
                    out.append(
                        {
                            "page": idx + 1,
                            "rows": rows,
                            "cols": cols,
                            "preview": preview,
                        }
                    )
    except Exception as exc:
        _LOG.warning("pdfplumber failed: %s", exc, exc_info=True)
        attempt["error"] = str(exc)[:200]
    attempt["tables_found"] = len(out)
    return out, attempt


def _normalize_run_inputs(
    payload: dict,
) -> dict | tuple[str, int, int, bool]:
    """Pure: validate ``url``/``max_pages``/``max_text_chars``; returns parsed bag or error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("pdf_document_parser.missing_url", "url is required")
    try:
        url = validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("pdf_document_parser.invalid_url", str(exc))
    try:
        max_pages = int(payload.get("max_pages") or _DEFAULT_MAX_PAGES)
    except (TypeError, ValueError):
        max_pages = _DEFAULT_MAX_PAGES
    max_pages = max(1, min(max_pages, _HARD_MAX_PAGES))
    try:
        max_text_chars = int(payload.get("max_text_chars") or _DEFAULT_MAX_TEXT_CHARS)
    except (TypeError, ValueError):
        max_text_chars = _DEFAULT_MAX_TEXT_CHARS
    max_text_chars = max(_MIN_TEXT_CHARS, min(max_text_chars, _HARD_MAX_TEXT_CHARS))
    include_tables = bool(payload.get("include_tables", True))
    return url, max_pages, max_text_chars, include_tables


def _shape_pdf_metadata(meta_raw: dict[str, Any]) -> dict[str, str | None]:
    """Pure: shape pymupdf's metadata blob into the agent's stable shape.

    ``title_source`` is filled in later by ``_attach_title_source`` once we
    know whether the embedded title was usable or we fell back to a page-1
    heuristic.
    """
    return {
        "title": _stringify_meta(meta_raw.get("title")),
        "title_source": None,
        "author": _stringify_meta(meta_raw.get("author")),
        "subject": _stringify_meta(meta_raw.get("subject")),
        "creator": _stringify_meta(meta_raw.get("creator")),
        "producer": _stringify_meta(meta_raw.get("producer")),
        "creation_date": _stringify_meta(meta_raw.get("creationDate")),
    }


def _is_plausible_title(text: str) -> bool:
    """Pure: bound-check title candidates so we don't return body paragraphs."""
    if not text:
        return False
    stripped = text.strip()
    return _TITLE_MIN_CHARS <= len(stripped) <= _TITLE_MAX_CHARS


def _largest_span_in_top_region(page: Any) -> str | None:
    """Side-effect: find the largest-font span in the top region of page 1.

    Why: when ``doc.metadata["title"]`` is empty (true for most uploaded
    PDFs), the title is right there on page 1 — typically the line with
    the biggest font. We use pymupdf's "dict" output to walk spans with
    their font sizes and y-coordinates. Returns None if no good candidate.
    """
    try:
        layout = page.get_text("dict")
    except Exception:  # noqa: BLE001 — heuristic, never raises
        _LOG.debug("pymupdf dict layout failed", exc_info=True)
        return None
    rect = getattr(page, "rect", None)
    page_height = float(getattr(rect, "height", 0)) if rect else 0.0
    if page_height <= 0:
        return None
    cutoff_y = page_height * _TITLE_PAGE1_TOP_FRACTION
    best_size = 0.0
    best_text: str | None = None
    for block in layout.get("blocks", []) or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines", []) or []:
            spans = line.get("spans") or []
            line_text_parts: list[str] = []
            line_max_size = 0.0
            line_top_y = float("inf")
            for span in spans:
                if not isinstance(span, dict):
                    continue
                bbox = span.get("bbox") or [0, 0, 0, 0]
                if len(bbox) >= 2:
                    line_top_y = min(line_top_y, float(bbox[1]))
                size = float(span.get("size") or 0)
                if size > line_max_size:
                    line_max_size = size
                text = str(span.get("text") or "").strip()
                if text:
                    line_text_parts.append(text)
            if line_top_y > cutoff_y:
                continue
            joined = " ".join(line_text_parts).strip()
            if not _is_plausible_title(joined):
                continue
            if line_max_size > best_size:
                best_size = line_max_size
                best_text = joined
    return best_text


def _first_nonempty_line(text: str) -> str | None:
    """Pure: return the first non-empty stripped line of ``text`` within length bounds."""
    if not text:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if _is_plausible_title(line):
            return line
    return None


def _extract_title_from_page1(doc: Any) -> str | None:
    """Side-effect: fall-back title extraction from PDF page 1.

    Tries the layout-aware (largest-font) heuristic first, falls back to
    "first non-empty line of plain-text page 1." Returns None when nothing
    plausible is on the page. Heuristic — never raises.
    """
    try:
        if doc.page_count < 1:
            return None
        page = doc.load_page(0)
    except Exception:  # noqa: BLE001 — heuristic, never raises
        _LOG.debug("page-1 title heuristic failed to load page", exc_info=True)
        return None
    candidate = _largest_span_in_top_region(page)
    if candidate:
        return candidate
    try:
        raw_text = page.get_text("text") or ""
    except Exception:  # noqa: BLE001
        return None
    return _first_nonempty_line(raw_text)


def _attach_title_source(metadata: dict[str, str | None], doc: Any) -> None:
    """Side-effect: fill metadata['title'] from page 1 if embedded title is empty.

    Sets metadata['title_source'] to 'embedded' / 'page1_heuristic' / None
    so callers know how trustworthy the title is.
    """
    if metadata.get("title"):
        metadata["title_source"] = "embedded"
        return
    fallback = _extract_title_from_page1(doc)
    if fallback:
        metadata["title"] = fallback
        metadata["title_source"] = "page1_heuristic"
    else:
        metadata["title_source"] = None


def _read_pages(
    doc: Any, pages_to_read: int, max_text_chars: int,
) -> tuple[list[dict[str, Any]], str]:
    """Side-effect: walk pymupdf's pages and shape the per-page output + truncated full text."""
    pages_out: list[dict[str, Any]] = []
    all_text_parts: list[str] = []
    running_total = 0
    for idx in range(pages_to_read):
        try:
            page = doc.load_page(idx)
            page_text = page.get_text("text") or ""
        except Exception as exc:
            _LOG.warning("Failed to read page %d: %s", idx + 1, exc, exc_info=True)
            continue
        pages_out.append({"page": idx + 1, "text": page_text, "char_count": len(page_text)})
        if running_total < max_text_chars:
            budget_left = max_text_chars - running_total
            snippet = page_text if len(page_text) <= budget_left else page_text[:budget_left]
            all_text_parts.append(snippet)
            running_total += len(snippet)
    full_text = "\n\n".join(all_text_parts)
    if running_total >= max_text_chars:
        full_text += f"\n[truncated at {max_text_chars} chars]"
    return pages_out, full_text


def _open_pdf(pdf_bytes: bytes) -> Any:
    """Side-effect: open a PyMuPDF doc; returns the doc or an error envelope.

    Why (rule 11): pymupdf is a heavy native dep — keeping the import lazy
    lets the agent module import on machines without the wheel installed.
    """
    try:
        import fitz  # type: ignore[import]  # PyMuPDF
    except ImportError:
        return _err(
            "pdf_document_parser.runtime_missing",
            "pymupdf (fitz) is not installed. Add `pymupdf` to requirements.txt.",
        )
    try:
        return fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        return _err(
            "pdf_document_parser.parse_failed",
            f"Could not open PDF: {type(exc).__name__}: {exc}",
        )


def run(payload: dict) -> dict:
    """Fetch a PDF URL and extract structured text + tables + metadata.

    Why: pricing is variable per page returned; capping page+byte budgets at
    the boundary stops a malicious caller from amortising a 100MB scan at
    the price of a single page.
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    url, max_pages, max_text_chars, include_tables = parsed
    pdf_bytes, fetch_err = _fetch_pdf(url)
    if pdf_bytes is None:
        return {"error": fetch_err} if fetch_err else _err(
            "pdf_document_parser.fetch_failed", "PDF fetch returned no bytes"
        )
    doc = _open_pdf(pdf_bytes)
    if isinstance(doc, dict):  # error envelope
        return doc
    try:
        page_count = doc.page_count
        metadata = _shape_pdf_metadata(doc.metadata or {})
        _attach_title_source(metadata, doc)
        pages_to_read = min(page_count, max_pages)
        pages_out, full_text = _read_pages(doc, pages_to_read, max_text_chars)
    finally:
        doc.close()
    if include_tables:
        tables, table_extraction = _extract_tables(pdf_bytes, pages_to_read)
    else:
        tables = []
        table_extraction = {
            "attempted": False,
            "extractor": "pdfplumber",
            "available": True,
            "pages_scanned": 0,
            "tables_found": 0,
            "error": None,
        }
    return {
        "url": url,
        "page_count": page_count,
        "pages_returned": len(pages_out),
        "metadata": metadata,
        "text": full_text,
        "pages": pages_out,
        "tables": tables,
        # 2026-05-20: surface that the agent looked even when zero tables
        # were found, so callers can distinguish "agent didn't try" from
        # "agent tried and the PDF genuinely has no extractable tables".
        "table_extraction": table_extraction,
        "billing_units_actual": max(1, len(pages_out)),
    }
