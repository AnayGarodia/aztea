"""
fetcher.py — SEC EDGAR data retrieval

Fetches the most recent 10-K or 10-Q filing for a given ticker using
SEC EDGAR's free public API. Returns the filing metadata and a text
excerpt suitable for passing to Claude.
"""

import requests

EDGAR_HEADERS = {
    "User-Agent": "agentmarket research-agent@agentmarket.dev",
    "Accept-Encoding": "gzip, deflate",
}

EDGAR_BASE = "https://data.sec.gov"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_FILING_URL = "https://www.sec.gov/Archives/edgar/full-index/"


def get_cik_for_ticker(ticker: str) -> str:
    """Look up the SEC CIK number for a ticker symbol."""
    url = "https://efts.sec.gov/LATEST/search-index?q=%22{}%22&dateRange=custom&startdt=2020-01-01&forms=10-K,10-Q".format(ticker)
    # Use the company tickers JSON maintained by SEC
    tickers_url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(tickers_url, headers=EDGAR_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            cik = str(entry["cik_str"]).zfill(10)
            return cik
    raise ValueError(f"Ticker '{ticker}' not found in SEC EDGAR company list.")


def get_latest_filing(cik: str) -> dict:
    """
    Fetch the most recent 10-K or 10-Q for a CIK.
    Returns a dict with: accession_number, filing_type, filing_date, document_url, company_name.
    """
    url = EDGAR_SUBMISSIONS.format(cik=cik)
    resp = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    company_name = data.get("name", "Unknown")
    filings = data.get("filings", {}).get("recent", {})

    forms = filings.get("form", [])
    dates = filings.get("filingDate", [])
    accessions = filings.get("accessionNumber", [])
    primary_docs = filings.get("primaryDocument", [])

    # Find most recent 10-K or 10-Q
    for i, form in enumerate(forms):
        if form in ("10-K", "10-Q"):
            accession = accessions[i].replace("-", "")
            primary_doc = primary_docs[i]
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accession}/{primary_doc}"
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


def fetch_filing_text(document_url: str, max_chars: int = 20000) -> str:
    """
    Download the filing document and return its text content, truncated to
    max_chars to stay within Claude's context window while capturing the
    material sections (business, risk factors, financials).
    """
    resp = requests.get(document_url, headers=EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()

    # SEC filings are HTML or plain text; strip tags if HTML
    content = resp.text
    if "<html" in content.lower() or "<!doctype" in content.lower():
        content = _strip_html(content)

    # Truncate to stay within Groq free-tier token limits (~12k TPM).
    # 20k chars ≈ 5k tokens, leaving headroom for prompt and response.
    return content[:max_chars]


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace without external dependencies."""
    import re
    # Remove script/style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove all tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&nbsp;", " ").replace("&#160;", " ").replace("&quot;", '"')
    # Collapse whitespace
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def get_filing_data(ticker: str) -> dict:
    """
    Top-level function: given a ticker, returns a dict with filing metadata
    and the extracted text, ready to pass to the synthesizer.
    """
    cik = get_cik_for_ticker(ticker)
    filing = get_latest_filing(cik)
    text = fetch_filing_text(filing["document_url"])
    filing["text"] = text
    filing["ticker"] = ticker.upper()
    return filing
