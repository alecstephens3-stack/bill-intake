"""One run, start to finish. Each stage is a function you can test on its own; this file only
puts them in order.

    files in intake -> read (model, in parallel) -> rows (rules) -> review (a person)
                    -> move files + log -> bills in QuickBooks (only filed bill rows)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import files, plan
from .models import Row
from .quickbooks import create_bills
from .reader import Reader, read_all
from .rules import Rules
from .text import list_intake

Review = Callable[[list[Row], list[str]], dict]


@dataclass
class RunResult:
    run_id: str
    decision: str
    rows: list[Row] = field(default_factory=list)
    filed: list[dict] = field(default_factory=list)
    bills: list[dict] = field(default_factory=list)


def propose(rules: Rules, reader: Reader, workers: int = 4) -> tuple[list[Row], list[str]]:
    paths = list_intake(rules.intake)
    results = read_all(reader, paths, workers=workers)
    cache: dict[int, list[str]] = {}
    rows = [plan.build_row(res.path, res.reading, rules, cache, res.error) for res in results]
    plan.resolve_collisions(rows)
    folders = sorted({f for fs in cache.values() for f in fs})
    return rows, folders


def run(rules: Rules, reader: Reader, review: Review, quickbooks=None, workers: int = 4,
        run_id: str | None = None) -> RunResult:
    run_id = run_id or files.new_run_id()
    rows, folders = propose(rules, reader, workers)
    if not rows:
        return RunResult(run_id, "nothing to do")
    decision = review(rows, folders)
    if decision.get("decision") != "go":
        return RunResult(run_id, "cancel", rows)
    plan.apply_edits(rows, decision.get("edits", []), rules)
    filed = files.file_rows(rows, rules.intake, run_id)
    filed_paths = {f["new_path"] for f in filed if f["status"] == "filed"}
    bill_rows = [r for r in rows if r.new_path in filed_paths and r.is_bill]
    bills = create_bills(bill_rows, run_id, quickbooks) if quickbooks is not None and bill_rows else []
    if bills:
        files.log_bills(rules.intake, run_id, bills)
    return RunResult(run_id, "go", rows, filed, bills)
