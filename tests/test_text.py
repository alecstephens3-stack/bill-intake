from bill_intake.samples import make_pdf
from bill_intake.text import _stdlib_pdf_text, extract, list_intake


def test_compressed_pdf_text(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(["Vendor: Acme Medical Supply", "Amount Due: $12.00"]))
    doc = extract(p)
    assert doc.has_text_layer
    assert "Vendor: Acme Medical Supply" in doc.text.splitlines()
    assert "Amount Due: $12.00" in doc.text


def test_uncompressed_pdf_and_escapes():
    data = make_pdf(["Northwind (East) Office", "back\\slash"], compress=False)
    text, pages = _stdlib_pdf_text(data)
    assert pages == 1
    assert text.splitlines() == ["Northwind (East) Office", "back\\slash"]


def test_pdf_with_no_text_is_flagged_not_guessed(tmp_path):
    p = tmp_path / "scan.pdf"
    p.write_bytes(make_pdf([]))
    doc = extract(p)
    assert not doc.has_text_layer


def test_not_a_pdf_raises(tmp_path):
    p = tmp_path / "fake.pdf"
    p.write_text("hello")
    try:
        extract(p)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_intake_lists_supported_files_only(tmp_path):
    for n in ("b.pdf", "a.txt", "c.docx", "filing-log.jsonl"):
        (tmp_path / n).write_text("x")
    (tmp_path / "Not filed").mkdir()
    assert [p.name for p in list_intake(tmp_path)] == ["a.txt", "b.pdf"]
