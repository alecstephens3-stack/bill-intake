"""The whole run on Example Clinic: fake reader, headless review, fake QuickBooks."""
from bill_intake import files
from bill_intake.__main__ import main
from bill_intake.pipeline import run
from bill_intake.quickbooks import FakeQuickBooks, create_bills, undo_bills
from bill_intake.reader import FakeReader
from bill_intake.review import approve_ready
from bill_intake.samples import QB_ACCOUNTS, QB_VENDORS


def by_source(res):
    return {r.source_name: r for r in res.rows}


def test_example_clinic_run(clinic):
    qbo = FakeQuickBooks(QB_VENDORS, QB_ACCOUNTS)
    res = run(clinic, FakeReader(), approve_ready, quickbooks=qbo, run_id="run-1")
    rows = by_source(res)
    year = clinic.year_folder(2026)

    # filed under the rules
    assert (year / "Acme Medical Supply" / "2026.03.04_Acme Medical Supply_1.pdf").exists()
    assert (year / "Acme Medical Supply" / "2026.03.04_Acme Medical Supply_2.pdf").exists()
    assert (year / "Acme Medical Supply" / "2026.03.04_Acme Medical Supply.pdf").read_bytes().startswith(b"%PDF")
    assert (year / "Northwind Office Co" / "2026.02.28_Northwind Office Co_stmt.txt").exists()
    assert (year / "Lakeside Lab Services" / "2026.03.31_Lakeside Lab Services_stmt.pdf").exists()

    # waiting for a person, moved out of the way, nothing created for them
    for name in ("fabrikam.txt", "old-bill.txt", "contoso-slip.txt"):
        assert not rows[name].approved
        assert (clinic.intake / "Not filed" / name).exists()
    assert not clinic.year_folder(2025).exists()

    # only real bills reach QuickBooks: not the Northwind statement, not the Lakeside invoice,
    # not the utility, and the patient page as numbers only
    entered = sorted(b["DocNumber"] for b in qbo.bills.values())
    assert entered == ["A-10442", "A-10457", "LLS-0326", "NW-5521"]
    lakeside = next(b for b in qbo.bills.values() if b["DocNumber"] == "LLS-0326")
    assert "Sample" not in json_dump(lakeside)
    northwind = next(b for b in qbo.bills.values() if b["DocNumber"] == "NW-5521")
    assert northwind["VendorRef"]["name"] == "Northwind Office Company"      # QuickBooks spelling from the rules

    # a later run that meets the same bills enters nothing new
    again = create_bills([r for r in res.rows if r.approved and r.is_bill], "run-2", qbo)
    assert {o["status"] for o in again} == {"already there"} and len(qbo.bills) == 4

    # undo removes the run's bills and puts every file back where it was
    created = files.bills_of_last_run(clinic.intake)
    assert len(created) == 4
    assert {u["status"] for u in undo_bills(created, qbo)} == {"deleted"} and not qbo.bills
    files.undo_last_run(clinic.intake)
    assert sorted(p.name for p in clinic.intake.iterdir() if p.suffix in (".pdf", ".txt")) == sorted(rows)


def json_dump(obj):
    import json
    return json.dumps(obj)


def test_cancel_moves_nothing(clinic):
    before = sorted(p.name for p in clinic.intake.iterdir())
    res = run(clinic, FakeReader(), lambda rows, folders: {"decision": "cancel"})
    assert res.decision == "cancel"
    assert sorted(p.name for p in clinic.intake.iterdir()) == before


def test_empty_intake(empty_clinic):
    assert run(empty_clinic, FakeReader(), approve_ready).decision == "nothing to do"


def test_demo_command_runs_offline(tmp_path, capsys):
    assert main(["demo", "--keep", str(tmp_path / "clinic")]) == 0
    out = capsys.readouterr().out
    assert "created" in out and "Example Clinic is in" in out
