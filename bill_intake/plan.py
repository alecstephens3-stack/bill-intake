"""Readings plus rules -> one proposal row per file.

A row is "ready" only when every part of it has a known answer: a clear date, a year folder that
already exists, exactly one vendor folder, and an amount if it is going to be a bill. Anything
short of that is "needs a person", unticked, with the reason in plain words. The review page is
where a person fills the gap; the rules never fill it with a guess.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from .models import NEEDS_PERSON, READY, Reading, Row
from .rules import Rules, clean_name, file_name

NOT_A_BILL_PAGE = "this page does not look like a bill (a packing slip or a quote?): tick it only if it is one"
NO_AMOUNT = "no amount found: type the amount if it is a bill"
PERSONAL_NOTE = "patient information on the page (it never goes to QuickBooks)"


def list_folders(year_folder: Path) -> list[str]:
    # scandir: each entry already knows whether it is a folder, which matters on a network drive
    with os.scandir(year_folder) as it:
        return sorted(e.name for e in it if e.is_dir())


def build_row(path: Path, reading: Reading | None, rules: Rules, folder_cache: dict[int, list[str]],
              error: str = "") -> Row:
    path = Path(path)
    r = Row(source_name=path.name, source_path=str(path))
    if reading is None:
        return r.needs_person(error or "could not be read")
    r.vendor = reading.vendor
    r.amount = reading.amount
    r.is_paid = reading.is_paid
    r.invoice_no = reading.invoice_no
    r.due_date = reading.due_date
    r.personal_data = reading.personal_data
    r.notes = reading.notes
    if reading.doc_type is None:
        return r.needs_person("could not tell if this is an invoice, a statement or a payment")
    r.doc_type = reading.doc_type

    # date: a statement is filed under the end of the period it covers
    iso = reading.period_end if r.doc_type == "statement" and reading.period_end else reading.doc_date
    if not iso or reading.date_confidence in ("ambiguous", "none"):
        return r.needs_person("date not clear on the page")
    r.doc_date = iso
    year = date.fromisoformat(iso).year

    # the year folder must already exist: creating one is a person's decision, never a side effect
    yf = rules.year_folder(year)
    if not yf.is_dir():
        return r.needs_person(f"no folder for {year} ({yf.name})")
    folders = folder_cache.get(year)
    if folders is None:
        folders = folder_cache[year] = list_folders(yf)

    r.folder, r.match_how = rules.match_vendor(r.vendor, folders)
    if not r.folder:
        return r.needs_person(f"vendor '{r.vendor or '?'}': {r.match_how}. Pick a folder.")

    _place(r, rules, path.suffix)
    if reading.not_a_bill_document:
        return r.needs_person(NOT_A_BILL_PAGE)
    if r.is_bill and not r.amount and not r.is_paid:
        return r.needs_person(NO_AMOUNT)
    return r


def _place(r: Row, rules: Rules, ext: str, name: str | None = None) -> None:
    yf = rules.year_folder(date.fromisoformat(r.doc_date).year)
    r.new_name = clean_name(name, ext) if name else file_name(r.doc_date, r.folder, r.doc_type, ext)
    r.new_path = str(yf / r.folder / r.new_name)
    r.qb_vendor = rules.qb_vendor(r.folder)
    r.account = rules.account_for(r.folder)
    r.not_a_bill = rules.not_a_bill(r.doc_type, r.folder, r.vendor, r.qb_vendor)
    r.reason = r.not_a_bill or (PERSONAL_NOTE if r.personal_data else "")


def resolve_collisions(rows: list[Row]) -> None:
    """Never overwrite: _1, _2 ... against the files already on disk and every other row in this
    batch. Two bills from one vendor on one day are normal, and the second must not replace the
    first."""
    taken: set[str] = set()
    for r in rows:
        if r.status != READY or not r.new_path:
            continue
        p = Path(r.new_path)
        base, cand, n = p.with_suffix(""), p, 0
        while cand.exists() or str(cand).lower() in taken:
            n += 1
            cand = Path(f"{base}_{n}{p.suffix}")
        taken.add(str(cand).lower())
        r.new_path, r.new_name = str(cand), cand.name


def apply_edits(rows: list[Row], edits: list[dict], rules: Rules) -> None:
    """Apply what a person changed on the review page, then re-check names.

    An edit can tick or untick a row, pick a folder, rename the file, or type an amount. A row
    that still lacks a date or a folder cannot be ticked: the page sends the tick, but the row
    stays unticked with its reason."""
    by_index = {int(e["i"]): e for e in edits if "i" in e}
    for i, r in enumerate(rows):
        e = by_index.get(i)
        if not e:
            r.approved = False if r.status != READY else r.approved
            continue
        ext = Path(r.source_name).suffix
        folder = (e.get("folder") or "").strip()
        name = (e.get("new_name") or "").strip()
        if e.get("amount") not in (None, ""):
            from .reader import to_amount
            r.amount = to_amount(e["amount"])
        if folder and folder != r.folder and r.doc_date:
            if r.vendor and r.folder is None:
                rules.add_exception(r.vendor, folder)       # learned for next time
            r.folder = folder
            _place(r, rules, ext)
        elif name and r.folder and r.doc_date and name != r.new_name:
            _place(r, rules, ext, name)
        want = bool(e.get("approved"))
        if want and r.folder and r.doc_date and (not r.is_bill or r.amount or r.is_paid):
            r.status, r.approved = READY, True
            r.reason = r.not_a_bill or (PERSONAL_NOTE if r.personal_data else "")
        else:
            r.approved = False
            if r.status == READY and not want:
                r.reason = "unticked on the review page"
    resolve_collisions(rows)


__all__ = ["build_row", "resolve_collisions", "apply_edits", "list_folders", "NEEDS_PERSON", "READY"]
