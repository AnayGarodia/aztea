"""
fetcher.py — SEC EDGAR data retrieval with in-process filing cache.

Fetches the most recent 10-K or 10-Q for a given ticker using SEC EDGAR's
free public API. Returns filing metadata and a text excerpt for the synthesizer.

Cache: module-level dict keyed by (cik, accession_number) with a 24-hour TTL.
Each server restart clears the cache, which is acceptable — the TTL exists to
avoid redundant network calls within a session, not for cross-restart persistence.
"""

import re
import time
from typing import Optional

import requests

EDGAR_HEADERS = {
    "User-Agent": "aztea research-agent@aztea.dev",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json,text/html,*/*",
}

EDGAR_BASE = "https://data.sec.gov"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"

_CACHE_TTL = 86_400  # 24 hours in seconds
_filing_cache: dict[str, tuple[dict, float]] = {}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_get(key: str) -> Optional[dict]:
    entry = _filing_cache.get(key)
    if entry:
        data, ts = entry
        if time.monotonic() - ts < _CACHE_TTL:
            return data
        del _filing_cache[key]
    return None


def _cache_set(key: str, data: dict) -> None:
    _filing_cache[key] = (data, time.monotonic())


# ---------------------------------------------------------------------------
# EDGAR API calls
# ---------------------------------------------------------------------------

def get_cik_for_ticker(ticker: str) -> str:
    """Look up the SEC CIK number for a ticker symbol."""
    tickers_url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(tickers_url, headers=EDGAR_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10)
    raise ValueError(f"Ticker '{ticker}' not found in SEC EDGAR company list.")


def get_latest_filing(cik: str) -> dict:
    """
    Fetch the most recent 10-K or 10-Q for a CIK.
    Returns: accession_number, filing_type, filing_date, document_url, company_name, cik.
    """
    url = EDGAR_SUBMISSIONS.format(cik=cik)
    resp = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    company_name = data.get("name", "Unknown")
    filings = data.get("filings", {}).get("recent", {})
    forms         = filings.get("form", [])
    dates         = filings.get("filingDate", [])
    accessions    = filings.get("accessionNumber", [])
    primary_docs  = filings.get("primaryDocument", [])

    for i, form in enumerate(forms):
        if form in ("10-K", "10-Q"):
            accession = accessions[i].replace("-", "")
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accession}/{primary_docs[i]}"
            )
            return {
                "company_name": company_name,
                "filing_type": form,
                "filing_date": dates[i],
                "accession_number": accessions[i],
                "document_url": doc_url,
                "cik": cik,
            }

    raise ValueError(f"No 10-K or 10-Q found for CIK {cik}.")


def fetch_filing_text(document_url: str, max_chars: int = 20_000) -> str:
    """
    Download the filing document and return its text content truncated to max_chars.
    Strips HTML tags if the document is HTML.
    """
    resp = requests.get(document_url, headers=EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()
    content = resp.text
    if "<html" in content.lower() or "<!doctype" in content.lower():
        content = _strip_html(content)
    return content[:max_chars]


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace using stdlib only."""
    html = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>", " ", html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(r"<[^>]+>", " ", html)
    html = (
        html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&nbsp;", " ").replace("&#160;", " ").replace("&quot;", '"')
    )
    return re.sub(r"\s+", " ", html).strip()


# ---------------------------------------------------------------------------
# Top-level entry point (with cache)
# ---------------------------------------------------------------------------

def get_filing_data(ticker: str) -> dict:
    """
    Given a ticker, return a dict with filing metadata and extracted text,
    ready to pass to the synthesizer. Results are cached for 24 hours.
    """
    ticker = ticker.upper()

    # Step 1: CIK lookup (fast; no cache needed — tiny response)
    cik = get_cik_for_ticker(ticker)

    # Step 2: Filing metadata (cached per CIK — rarely changes intraday)
    filing_meta_key = f"meta:{cik}"
    filing = _cache_get(filing_meta_key)
    if filing is None:
        filing = get_latest_filing(cik)
        _cache_set(filing_meta_key, filing)

    # Step 3: Full filing text (cached per accession number)
    text_key = f"text:{cik}:{filing['accession_number']}"
    text = _cache_get(text_key)
    if text is None:
        text = fetch_filing_text(filing["document_url"])
        _cache_set(text_key, {"text": text})
    else:
        text = text["text"]

    return {**filing, "text": text, "ticker": ticker}
