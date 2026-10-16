from bill_intake import rules as rules_mod
from bill_intake.rules import Rules, file_name, listed, norm

FOLDERS = ["Acme Medical Supply", "Northwind Office Co", "Lakeside Lab Services", "Contoso Cleaning",
           "Lakeside Lab Services East"]


def test_norm_ignores_case_punctuation_and_legal_suffixes():
    assert norm("ACME Medical Supply, Inc.") == norm("Acme Medical Supply") == "acme medical supply"
    assert norm("Smith & Sons LLC") == "smith and sons"


def test_listed_is_whole_words_and_spacing_tolerant():
    assert listed("Brightwater Utilities", ["Bright Water Utilities"])
    assert listed("Lakeside Lab Services of the Valley", ["Lakeside Lab Services"])
    assert not listed("Contosoft Cleaning", ["Contoso"])     # a short name never matches inside a word


def test_match_order(tmp_path):
    r = Rules(tmp_path, tmp_path, exceptions={"ACME MED SUPPLY": "Acme Medical Supply"})
    assert r.match_vendor("ACME MED SUPPLY LLC", FOLDERS) == ("Acme Medical Supply", "exception: ACME MED SUPPLY -> Acme Medical Supply")
    assert r.match_vendor("Northwind Office Co.", FOLDERS)[1] == "exact"
    assert r.match_vendor("North Wind Office Co.", FOLDERS)[1] == "exact (spacing)"
    assert r.match_vendor("Contoso Cleaning Services Group", FOLDERS) == ("Contoso Cleaning", "folder name appears in vendor")


def test_two_candidates_is_never_a_guess(tmp_path):
    r = Rules(tmp_path, tmp_path)
    folder, how = r.match_vendor("Lakeside Lab Services East Annex", FOLDERS)
    assert folder is None and how.startswith("more than one folder fits")
    assert r.match_vendor("Fabrikam", FOLDERS) == (None, "no folder fits")
    assert r.match_vendor(None, FOLDERS) == (None, "no vendor read")


def test_file_names():
    assert file_name("2026-03-04", "Acme Medical Supply", "invoice") == "2026.03.04_Acme Medical Supply.pdf"
    assert file_name("2026-02-28", "Northwind Office Co", "statement") == "2026.02.28_Northwind Office Co_stmt.pdf"
    assert file_name("2026-03-01", "Acme", "payment", ".TXT") == "2026.03.01_Acme_pymt.txt"
    assert file_name("2026-03-01", "A/B: Co", "invoice") == "2026.03.01_AB Co.pdf"


def test_what_becomes_a_bill(tmp_path):
    r = Rules(tmp_path, tmp_path, statement_is_the_bill=["Lakeside Lab Services"],
              never_entered=["Brightwater Utilities"])
    assert r.not_a_bill("invoice", "Acme Medical Supply") == ""
    assert "invoices are entered one by one" in r.not_a_bill("statement", "Acme Medical Supply")
    assert r.not_a_bill("statement", "Lakeside Lab Services") == ""
    assert "statement is the bill" in r.not_a_bill("invoice", "Lakeside Lab Services")
    assert "entered another way" in r.not_a_bill("invoice", "Brightwater Utilities")
    assert "payment record" in r.not_a_bill("payment", "Acme Medical Supply")


def test_load_and_learn(clinic):
    assert clinic.year_folder(2026).name == "2026 Bills"
    assert clinic.exceptions == {"ACME MED SUPPLY": "Acme Medical Supply"}
    assert clinic.qb_vendor("Northwind Office Co") == "Northwind Office Company"
    assert clinic.account_for("Contoso Cleaning") == "Uncategorized Expense"
    assert clinic.account_for("Lakeside Lab Services") == "Lab Fees"
    clinic.add_exception("Contoso Cleaning Svcs", "Contoso Cleaning")
    again = rules_mod.load(clinic.path)
    assert again.exceptions["Contoso Cleaning Svcs"] == "Contoso Cleaning"
    assert again.statement_is_the_bill == ["Lakeside Lab Services"]     # the next section is untouched


def test_missing_paths_is_an_error(tmp_path):
    p = tmp_path / "rules.txt"
    p.write_text("[where]\nintake = in\n")
    try:
        rules_mod.load(p)
    except ValueError as e:
        assert "vendor_root" in str(e)
    else:
        raise AssertionError("expected ValueError")
