"""A made-up practice, "Example Clinic", built on disk by code: rules file, vendor folders and a
handful of sample bills (small generated PDFs and plain text). Used by the demo and the tests, so
the repository itself holds no documents at all.

Every vendor here is fictional.
"""
from __future__ import annotations

import zlib
from pathlib import Path

RULES = """\
# Filing rules for Example Clinic. Re-read on every run.

[where]
intake = intake
vendor_root = vendors
year_folder = <YYYY> Bills

[vendor name exceptions]
ACME MED SUPPLY -> Acme Medical Supply

[statement is the bill]
Lakeside Lab Services

[never entered]
Brightwater Utilities

[quickbooks vendor names]
Northwind Office Co -> Northwind Office Company

[expense accounts]
Acme Medical Supply -> Medical Supplies
Northwind Office Co -> Office Expenses
Lakeside Lab Services -> Lab Fees
default -> Uncategorized Expense
"""

VENDOR_FOLDERS = ["Acme Medical Supply", "Northwind Office Co", "Lakeside Lab Services",
                  "Brightwater Utilities", "Contoso Cleaning"]

QB_VENDORS = ["Acme Medical Supply", "Northwind Office Company", "Lakeside Lab Services",
              "Contoso Cleaning"]
QB_ACCOUNTS = ["Medical Supplies", "Office Expenses", "Lab Fees", "Uncategorized Expense"]

# file name -> the lines printed on it
BILLS: dict[str, list[str]] = {
    "scan_0001.pdf": ["ACME MED SUPPLY LLC", "Vendor: ACME MED SUPPLY LLC", "Document: Invoice",
                      "Invoice Date: 2026-03-04", "Invoice No: A-10442", "Due Date: 2026-04-03",
                      "Gloves, nitrile, case of 10", "Amount Due: $1,265.00"],
    "scan_0002.pdf": ["ACME MED SUPPLY LLC", "Vendor: ACME MED SUPPLY LLC", "Document: Invoice",
                      "Invoice Date: 2026-03-04", "Invoice No: A-10457", "Amount Due: $88.40"],
    "northwind-feb.txt": ["Northwind Office Co.", "Vendor: Northwind Office Co.", "Document: Statement",
                          "Statement Date: 2026-03-02", "Period End: 2026-02-28", "Balance Due: $412.75"],
    "northwind-receipt.txt": ["Vendor: Northwind Office Co.", "Document: Invoice", "Invoice Date: 2026-03-09",
                              "Invoice No: NW-5521", "Total: $59.99", "Paid in full"],
    "lakeside-mar.pdf": ["Lakeside Lab Services", "Vendor: Lakeside Lab Services", "Document: Statement",
                         "Statement Date: 2026-03-31", "Period End: 2026-03-31", "Statement No: LLS-0326",
                         "Patient: A. Sample", "Balance Due: $2,140.00"],
    "lakeside-inv.txt": ["Vendor: Lakeside Lab Services", "Document: Invoice", "Invoice Date: 2026-03-12",
                         "Invoice No: L-881", "Amount Due: $310.00"],
    "brightwater.txt": ["Vendor: Brightwater Utilities", "Document: Invoice", "Invoice Date: 2026-03-15",
                        "Amount Due: $201.18"],
    "contoso-slip.txt": ["Vendor: Contoso Cleaning", "Packing slip", "Date: 2026-03-16"],
    "fabrikam.txt": ["Vendor: Fabrikam Dental Lab", "Document: Invoice", "Invoice Date: 2026-03-18",
                     "Invoice No: F-12", "Amount Due: $75.00"],
    "old-bill.txt": ["Vendor: Contoso Cleaning", "Document: Invoice", "Invoice Date: 2025-12-30",
                     "Invoice No: C-77", "Amount Due: $150.00"],
}


def make_pdf(lines: list[str], compress: bool = True) -> bytes:
    """A minimal, valid one-page PDF that shows `lines` as text."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = "BT /F1 11 Tf 14 TL 72 740 Td\n" + "".join(f"({esc(ln)}) Tj T*\n" for ln in lines) + "ET"
    stream = content.encode("latin-1")
    extra = b""
    if compress:
        stream = zlib.compress(stream)
        extra = b" /Filter /FlateDecode"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + extra + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def build_example_clinic(root: Path, bills: dict[str, list[str]] | None = None) -> Path:
    """Create the practice under `root` and return the rules file path."""
    root = Path(root)
    (root / "intake").mkdir(parents=True, exist_ok=True)
    year = root / "vendors" / "2026 Bills"
    for v in VENDOR_FOLDERS:
        (year / v).mkdir(parents=True, exist_ok=True)
    # already filed last week: the next Acme bill from the same day must not replace it
    (year / "Acme Medical Supply" / "2026.03.04_Acme Medical Supply.pdf").write_bytes(make_pdf(["filed earlier"]))
    for name, lines in (BILLS if bills is None else bills).items():
        p = root / "intake" / name
        if name.endswith(".pdf"):
            p.write_bytes(make_pdf(lines))
        else:
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rules = root / "filing-rules.txt"
    rules.write_text(RULES, encoding="utf-8")
    return rules
