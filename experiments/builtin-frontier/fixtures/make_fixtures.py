"""Generate the local fixtures the builtin-frontier corpus references.

Reproducible + network-free so the document/OCR categories isolate the
capability gap (can the harness parse a PDF / OCR an image?) rather than a
fetch confound. Run from the repo root:

    .venv/bin/python experiments/builtin-frontier/fixtures/make_fixtures.py

Fixtures carry UNIQUE markers (values that exist nowhere else) so a correct
answer proves the file was actually read, not guessed.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_HERE = Path(__file__).resolve().parent

# Unique, unguessable values — a correct answer means the work was done.
INVOICE_TOTAL = "$48,217.63"
INVOICE_PO = "PO-7Q2X-9931"
RECEIPT_TOTAL = "$3,184.07"
RECEIPT_ID = "RX-55K8-2207"
CHART_PEAK = "1473"
# report.pdf quarterly table (text-layer PDF). Q3 is the peak; sum = 479600.
REPORT_Q = {"Q1": 112450, "Q2": 98200, "Q3": 147300, "Q4": 121650}
REPORT_PEAK_Q = "Q3"
REPORT_SUM = sum(REPORT_Q.values())  # 479600


def _text_pdf(text_lines: list[str]) -> bytes:
    """Build a minimal one-page text-layer PDF by hand (no PDF library).

    The content stream writes each line with a BT/ET text object; pdftotext
    and any real PDF parser can extract it, but the bytes are not plain text
    so 'just read the file' fails without a parser."""
    lines = "\n".join(
        f"BT /F1 12 Tf 72 {720 - i * 20} Td ({line}) Tj ET"
        for i, line in enumerate(text_lines)
    )
    stream = f"q\n{lines}\nQ".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objects) + 1, xref_pos,
    )
    return bytes(out)


def _make_invoice_pdf() -> None:
    pdf = _text_pdf([
        "ACME DYNAMICS - INVOICE",
        f"Purchase Order: {INVOICE_PO}",
        "Line items:",
        "  Widget assembly x 1200    $39,840.00",
        "  Calibration service        $5,977.63",
        "  Expedited freight          $2,400.00",
        f"TOTAL DUE: {INVOICE_TOTAL}",
        "Terms: net 30. Remit to ACME Dynamics LLC.",
    ])
    (_HERE / "invoice.pdf").write_bytes(pdf)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _make_scanned_receipt() -> None:
    """A receipt rendered as an IMAGE (no text layer) — needs OCR."""
    img = Image.new("RGB", (520, 360), "white")
    draw = ImageDraw.Draw(img)
    body = _font(20)
    head = _font(26)
    draw.text((24, 20), "NORTHWIND SUPPLY CO.", fill="black", font=head)
    draw.text((24, 70), f"Receipt: {RECEIPT_ID}", fill="black", font=body)
    draw.text((24, 110), "3x Server rack unit     $2,610.00", fill="black", font=body)
    draw.text((24, 145), "1x UPS battery          $   449.00", fill="black", font=body)
    draw.text((24, 180), "Tax                     $   125.07", fill="black", font=body)
    draw.text((24, 230), f"TOTAL  {RECEIPT_TOTAL}", fill="black", font=head)
    # Slight rotation so it reads as a scan, not clean synthetic text.
    img.rotate(-2, expand=True, fillcolor="white").save(_HERE / "receipt.png")


def _make_chart() -> None:
    """A bar chart as an image — the peak value is only legible visually."""
    img = Image.new("RGB", (520, 320), "white")
    draw = ImageDraw.Draw(img)
    label = _font(16)
    draw.text((180, 12), "Daily requests (k)", fill="black", font=label)
    bars = [("Mon", 880), ("Tue", 1120), ("Wed", 1473), ("Thu", 990), ("Fri", 1240)]
    base_y = 280
    for i, (day, val) in enumerate(bars):
        x = 50 + i * 90
        h = int(val / 6)
        draw.rectangle([x, base_y - h, x + 60, base_y], fill="steelblue")
        draw.text((x + 6, base_y + 6), day, fill="black", font=label)
        draw.text((x + 2, base_y - h - 22), str(val), fill="black", font=label)
    img.save(_HERE / "chart.png")


def _make_report_pdf() -> None:
    pdf = _text_pdf([
        "ORION ANALYTICS - QUARTERLY REVENUE REPORT FY2026",
        "Quarter    Revenue (USD)",
        f"Q1         {REPORT_Q['Q1']:,}",
        f"Q2         {REPORT_Q['Q2']:,}",
        f"Q3         {REPORT_Q['Q3']:,}",
        f"Q4         {REPORT_Q['Q4']:,}",
        "Confidential - internal distribution only.",
    ])
    (_HERE / "report.pdf").write_bytes(pdf)


def main() -> None:
    _make_invoice_pdf()
    _make_report_pdf()
    _make_scanned_receipt()
    _make_chart()
    markers = {
        "invoice.pdf": {"total": INVOICE_TOTAL, "po": INVOICE_PO, "calibration": "$5,977.63"},
        "report.pdf": {"q3": f"{REPORT_Q['Q3']:,}", "peak_quarter": REPORT_PEAK_Q, "sum": f"{REPORT_SUM:,}"},
        "receipt.png": {"total": RECEIPT_TOTAL, "id": RECEIPT_ID, "ups_line": "$449.00"},
        "chart.png": {"peak": CHART_PEAK, "peak_day": "Wed", "tuesday": "1120"},
    }
    import json
    (_HERE / "markers.json").write_text(json.dumps(markers, indent=2), encoding="utf-8")
    print("wrote:", ", ".join(sorted(p.name for p in _HERE.glob("*") if p.suffix in {".pdf", ".png", ".json"})))


if __name__ == "__main__":
    main()
