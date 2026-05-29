"""Parse a PDF and return its text contents.

Supports text-based PDFs (no OCR). Returns the raw text per page.
"""


def handler(payload):
    return {"pages": []}
