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
      "title": str | null,
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

_LOG = logging.getLogger(__name__)

_DEFAULT_MAX_PAGES = 50
_HARD_MAX_PAGES = 100
_DEFAULT_MAX_TEXT_CHARS = 60_000
_HARD_MAX_TEXT_CHARS = 200_000
_FETCH_TIMEOUT_S = 20.0
_MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB hard cap
_TABLE_PREVIEW_ROWS = 10
_TABLE_PREVIEW_COLS = 8
_USER_AGENT = "Aztea-PDF-Parser/1.0"


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


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


def _extract_tables(pdf_bytes: bytes, max_pages: int) -> list[dict[str, Any]]:
    try:
        import pdfplumber  # type: ignore[import]
    except ImportError:
        # Tables are optional; surface absence as empty rather than failing the call.
        return []

    out: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages):
                if idx >= max_pages:
                    break
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
    return out


def run(payload: dict) -> dict:
    """Fetch a PDF URL and extract structured text + tables + metadata.

    Variable-priced per page returned. Hard-capped at 100 pages and 25 MB.
    Tables are best-effort via pdfplumber; metadata via pymupdf.
    """
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
    max_text_chars = max(1000, min(max_text_chars, _HARD_MAX_TEXT_CHARS))

    include_tables = bool(payload.get("include_tables", True))

    pdf_bytes, fetch_err = _fetch_pdf(url)
    if pdf_bytes is None:
        return {"error": fetch_err} if fetch_err else _err(
            "pdf_document_parser.fetch_failed", "PDF fetch returned no bytes"
        )

    try:
        import fitz  # type: ignore[import]  # PyMuPDF
    except ImportError:
        return _err(
            "pdf_document_parser.runtime_missing",
            "pymupdf (fitz) is not installed. Add `pymupdf` to requirements.txt.",
        )

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        return _err(
            "pdf_document_parser.parse_failed",
            f"Could not open PDF: {type(exc).__name__}: {exc}",
        )

    try:
        page_count = doc.page_count
        meta_raw = doc.metadata or {}
        metadata = {
            "title": _stringify_meta(meta_raw.get("title")),
            "author": _stringify_meta(meta_raw.get("author")),
            "subject": _stringify_meta(meta_raw.get("subject")),
            "creator": _stringify_meta(meta_raw.get("creator")),
            "producer": _stringify_meta(meta_raw.get("producer")),
            "creation_date": _stringify_meta(meta_raw.get("creationDate")),
        }

        pages_to_read = min(page_count, max_pages)
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
            char_count = len(page_text)
            pages_out.append(
                {"page": idx + 1, "text": page_text, "char_count": char_count}
            )
            if running_total < max_text_chars:
                budget_left = max_text_chars - running_total
                snippet = page_text if len(page_text) <= budget_left else page_text[:budget_left]
                all_text_parts.append(snippet)
                running_total += len(snippet)
    finally:
        doc.close()

    full_text = "\n\n".join(all_text_parts)
    if running_total >= max_text_chars:
        full_text += f"\n[truncated at {max_text_chars} chars]"

    tables: list[dict[str, Any]] = []
    if include_tables:
        tables = _extract_tables(pdf_bytes, pages_to_read)

    return {
        "url": url,
        "page_count": page_count,
        "pages_returned": len(pages_out),
        "metadata": metadata,
        "text": full_text,
        "pages": pages_out,
        "tables": tables,
        "billing_units_actual": max(1, len(pages_out)),
    }
