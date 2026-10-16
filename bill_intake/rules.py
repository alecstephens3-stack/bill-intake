"""Filing rules: everything that has a known answer, decided without the model.

The rules live in a plain text file the bookkeeper owns and can read. It is re-read on every run,
so nothing about a vendor is remembered anywhere else, and a correction made on the review page
is written back into the same file (see `add_exception`).

    [where]
    intake = ./intake
    vendor_root = ./vendors
    year_folder = <YYYY> Bills

    [vendor name exceptions]
    ACME MED SUPPLY LLC -> Acme Medical Supply

    [statement is the bill]
    Lakeside Lab Services

    [never entered]
    Brightwater Utilities

    [quickbooks vendor names]
    Northwind Office Co -> Northwind Office Company

    [expense accounts]
    Acme Medical Supply -> Medical Supplies
    default -> Uncategorized Expense

Relative paths are relative to the rules file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SECTION = re.compile(r"^\[(.+)\]\s*$")
_SUFFIXES = r"\b(inc|incorporated|llc|l l c|ltd|limited|co|corp|corporation|company)\b"
_UNSAFE = re.compile(r'[\\/:*?"<>|]')

NAME_SUFFIX = {"invoice": "", "statement": "_stmt", "payment": "_pymt"}


def norm(s: str) -> str:
    """Compare vendor names the way a person would: case, punctuation, '&' and legal suffixes
    (Inc, LLC, Co) do not make two vendors different."""
    s = s.lower().replace("&", " and ")
    s = re.sub(r"['’.,\-_/]", " ", s)
    s = re.sub(_SUFFIXES, " ", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def listed(name: str | None, names: list[str]) -> bool:
    """Is this vendor on the list? Whole words only, so a short name never matches inside a longer
    one, and spacing is ignored when the whole name is the same ("Bright Water" = "Brightwater").
    A wrong yes here would enter a document twice, so it errs toward no."""
    if not name:
        return False
    v = f" {norm(name)} "
    flat = v.replace(" ", "")
    for n in names:
        nn = norm(n)
        if nn and (f" {nn} " in v or nn.replace(" ", "") == flat):
            return True
    return False


@dataclass
class Rules:
    intake: Path
    vendor_root: Path
    year_folder_pattern: str = "<YYYY>"
    exceptions: dict[str, str] = field(default_factory=dict)     # name as printed -> vendor folder
    statement_is_the_bill: list[str] = field(default_factory=list)
    never_entered: list[str] = field(default_factory=list)
    qb_vendor_names: dict[str, str] = field(default_factory=dict)  # vendor folder -> QuickBooks name
    accounts: dict[str, str] = field(default_factory=dict)         # vendor folder -> expense account
    default_account: str = "Uncategorized Expense"
    path: Path | None = None

    def year_folder(self, year: int) -> Path:
        return self.vendor_root / self.year_folder_pattern.replace("<YYYY>", str(year))

    # -- vendor folder
    def match_vendor(self, vendor: str | None, folders: list[str]) -> tuple[str | None, str]:
        """(folder, how). Exceptions first, then a normalised exact match, then a single folder
        whose whole name appears in the vendor's (or the other way round). Two candidates, or
        none, is None: the row waits for a person. Never a best guess."""
        if not vendor:
            return None, "no vendor read"
        by_norm = {norm(f): f for f in folders}
        v = norm(vendor)
        for printed, folder in self.exceptions.items():
            if listed(vendor, [printed]):
                hit = folder if folder in folders else by_norm.get(norm(folder))
                if hit:
                    return hit, f"exception: {printed} -> {folder}"
        if v in by_norm:
            return by_norm[v], "exact"
        flat = v.replace(" ", "")
        for nf, f in by_norm.items():
            if nf.replace(" ", "") == flat:
                return f, "exact (spacing)"
        inside = [f for nf, f in by_norm.items() if nf and f" {nf} " in f" {v} "]
        if len(inside) == 1:
            return inside[0], "folder name appears in vendor"
        around = [f for nf, f in by_norm.items() if v and f" {v} " in f" {nf} "]
        if len(around) == 1 and not inside:
            return around[0], "vendor appears in folder name"
        if inside or around:
            return None, f"more than one folder fits: {', '.join(sorted(set(inside + around)))}"
        return None, "no folder fits"

    # -- what becomes a bill
    def not_a_bill(self, doc_type: str, *names: str | None) -> str:
        """Why a filed document is never entered in QuickBooks, or "" when it is a bill.

        Most vendors send invoices that are entered one by one; their monthly statement only says
        what to pay, so entering it too would count every invoice twice. A few vendors are the
        reverse: the statement total is what gets entered, so their single invoices are filed
        only. Some vendors are never entered from paper at all (autopay through the bank feed)."""
        names = tuple(n for n in names if n)
        if doc_type == "payment":
            return "a payment record; nothing to enter"
        if any(listed(n, self.never_entered) for n in names):
            return "filed only: this vendor is entered another way"
        statement_billed = any(listed(n, self.statement_is_the_bill) for n in names)
        if doc_type == "statement" and not statement_billed:
            return "a statement; its invoices are entered one by one"
        if doc_type == "invoice" and statement_billed:
            return "filed only: the monthly statement is the bill"
        return ""

    # -- QuickBooks fields
    def qb_vendor(self, folder: str) -> str:
        return self.qb_vendor_names.get(folder, folder)

    def account_for(self, folder: str) -> str:
        return self.accounts.get(folder, self.default_account)

    # -- learning a correction
    def add_exception(self, printed: str, folder: str) -> None:
        """A person picked the folder for a vendor the rules could not place. Write it into the
        rules file so the next run places it on its own. Nothing is learned anywhere else."""
        self.exceptions[printed] = folder
        if self.path:
            _append_line(self.path, "vendor name exceptions", f"{printed} -> {folder}")


def file_name(iso_date: str, folder: str, doc_type: str, ext: str = ".pdf") -> str:
    """YYYY.MM.DD_Vendor Name.pdf, with _stmt for a statement and _pymt for a payment record."""
    safe = _UNSAFE.sub("", folder).strip()
    return f"{iso_date.replace('-', '.')}_{safe}{NAME_SUFFIX.get(doc_type, '')}{ext.lower()}"


def clean_name(name: str, ext: str) -> str:
    """A name typed on the review page: no path characters, and the original extension kept."""
    name = _UNSAFE.sub("", name).strip()
    if not name.lower().endswith(ext.lower()):
        name += ext.lower()
    return name


# ------------------------------------------------------------------ the file
def load(path: Path) -> Rules:
    path = Path(path)
    sections = _sections(path.read_text(encoding="utf-8"))
    where = dict(_pairs(sections.get("where", []), "="))
    if not where.get("intake") or not where.get("vendor_root"):
        raise ValueError(f"{path.name}: [where] needs intake and vendor_root")
    base = path.parent
    accounts = dict(_pairs(sections.get("expense accounts", []), "->"))
    default = accounts.pop("default", "Uncategorized Expense")
    return Rules(
        intake=(base / where["intake"]).resolve(),
        vendor_root=(base / where["vendor_root"]).resolve(),
        year_folder_pattern=where.get("year_folder", "<YYYY>"),
        exceptions=dict(_pairs(sections.get("vendor name exceptions", []), "->")),
        statement_is_the_bill=_names(sections.get("statement is the bill", [])),
        never_entered=_names(sections.get("never entered", [])),
        qb_vendor_names=dict(_pairs(sections.get("quickbooks vendor names", []), "->")),
        accounts=accounts,
        default_account=default,
        path=path,
    )


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        m = _SECTION.match(line)
        if m:
            current = m.group(1).strip().lower()
            out.setdefault(current, [])
        elif line and current:
            out[current].append(line)
    return out


def _pairs(lines: list[str], sep: str) -> list[tuple[str, str]]:
    out = []
    for ln in lines:
        if sep in ln:
            a, b = (x.strip() for x in ln.split(sep, 1))
            if a and b:
                out.append((a, b))
    return out


def _names(lines: list[str]) -> list[str]:
    return [n.strip() for ln in lines for n in ln.split(",") if n.strip()]


def _append_line(path: Path, section: str, line: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.strip().lower() == f"[{section}]"), None)
    if head is None:
        lines += ["", f"[{section}]", line]
    else:
        end = next((i for i in range(head + 1, len(lines)) if _SECTION.match(lines[i].strip())), len(lines))
        while end > head + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
