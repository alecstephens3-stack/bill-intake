from pathlib import Path

from bill_intake.models import NEEDS_PERSON, READY, Reading
from bill_intake.plan import apply_edits, build_row, resolve_collisions


def reading(**kw):
    base = dict(vendor="Acme Medical Supply", doc_type="invoice", doc_date="2026-03-20",
                date_confidence="labelled", amount=100.0, invoice_no="A-1")
    base.update(kw)
    return Reading(**base)


def row(clinic, name="in.pdf", **kw):
    return build_row(clinic.intake / name, reading(**kw), clinic, {})


def test_ready_invoice(clinic):
    r = row(clinic)
    assert r.status == READY and r.approved
    assert r.new_name == "2026.03.20_Acme Medical Supply.pdf"
    assert Path(r.new_path).parent == clinic.year_folder(2026) / "Acme Medical Supply"
    assert r.account == "Medical Supplies" and r.is_bill


def test_statement_is_filed_under_period_end_with_stmt(clinic):
    r = row(clinic, doc_type="statement", vendor="Northwind Office Co.", doc_date="2026-03-02",
            period_end="2026-02-28")
    assert r.new_name == "2026.02.28_Northwind Office Co_stmt.pdf"
    assert not r.is_bill and r.status == READY


def test_payment_gets_pymt_and_is_not_a_bill(clinic):
    r = row(clinic, doc_type="payment", amount=None)
    assert r.new_name.endswith("_pymt.pdf") and not r.is_bill and r.status == READY


def test_unclear_date_waits(clinic):
    for kw in ({"date_confidence": "ambiguous"}, {"doc_date": None}, {"date_confidence": "none"}):
        r = row(clinic, **kw)
        assert r.status == NEEDS_PERSON and not r.approved and "date" in r.reason


def test_missing_year_folder_waits_and_is_not_created(clinic):
    r = row(clinic, doc_date="2025-12-30")
    assert r.status == NEEDS_PERSON and "no folder for 2025" in r.reason
    assert not clinic.year_folder(2025).exists()


def test_unknown_vendor_waits(clinic):
    r = row(clinic, vendor="Fabrikam Dental Lab")
    assert r.status == NEEDS_PERSON and r.folder is None and "Pick a folder" in r.reason


def test_bill_without_amount_waits_but_a_statement_does_not(clinic):
    assert row(clinic, amount=None).status == NEEDS_PERSON
    assert row(clinic, amount=None, is_paid=True).status == READY
    assert row(clinic, doc_type="statement", amount=None, vendor="Northwind Office Co").status == READY


def test_not_a_bill_page_waits_unticked(clinic):
    r = row(clinic, not_a_bill_document=True)
    assert r.status == NEEDS_PERSON and not r.approved and r.new_name   # named, in case it is one after all


def test_unreadable_file_waits(clinic):
    r = build_row(clinic.intake / "x.pdf", None, clinic, {}, "no text layer (a scan?): file it by hand")
    assert r.status == NEEDS_PERSON and "scan" in r.reason


def test_bad_doc_type_waits(clinic):
    assert row(clinic, doc_type=None).status == NEEDS_PERSON


def test_never_overwrite_disk_or_batch(clinic):
    # Example Clinic already has 2026.03.04_Acme Medical Supply.pdf on disk
    rows = [row(clinic, f"{i}.pdf", doc_date="2026-03-04", invoice_no=f"A-{i}") for i in range(3)]
    resolve_collisions(rows)
    assert [r.new_name for r in rows] == ["2026.03.04_Acme Medical Supply_1.pdf",
                                          "2026.03.04_Acme Medical Supply_2.pdf",
                                          "2026.03.04_Acme Medical Supply_3.pdf"]


def test_collisions_ignore_case(clinic):
    a, b = row(clinic, "a.pdf"), row(clinic, "b.pdf")
    b.new_path = b.new_path.upper()
    resolve_collisions([a, b])
    assert a.new_path.lower() != b.new_path.lower()


def test_review_edit_picks_folder_and_learns_it(clinic):
    rows = [build_row(clinic.intake / "c.pdf", reading(vendor="CCS Intl"), clinic, {})]
    assert rows[0].status == NEEDS_PERSON
    apply_edits(rows, [{"i": 0, "approved": True, "folder": "Contoso Cleaning"}], clinic)
    assert rows[0].status == READY and rows[0].approved
    assert rows[0].new_name == "2026.03.20_Contoso Cleaning.pdf"
    assert "CCS Intl" in clinic.path.read_text()


def test_review_edit_cannot_tick_a_row_with_no_date(clinic):
    rows = [row(clinic, doc_date=None)]
    apply_edits(rows, [{"i": 0, "approved": True}], clinic)
    assert not rows[0].approved and rows[0].status == NEEDS_PERSON


def test_review_edit_amount_and_name(clinic):
    rows = [row(clinic, amount=None), row(clinic, "b.pdf", doc_date="2026-03-21")]
    apply_edits(rows, [{"i": 0, "approved": True, "amount": "$42.50"},
                       {"i": 1, "approved": True, "new_name": "../../odd:name"}], clinic)
    assert rows[0].status == READY and rows[0].amount == 42.5
    assert rows[1].new_name == "....oddname.pdf"     # path characters stripped, extension kept
    assert Path(rows[1].new_path).parent.name == "Acme Medical Supply"


def test_untick_on_review(clinic):
    rows = [row(clinic)]
    apply_edits(rows, [{"i": 0, "approved": False}], clinic)
    assert not rows[0].approved
