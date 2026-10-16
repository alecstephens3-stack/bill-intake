"""The two records that move through the pipeline.

Reading  what the model said about one document, after validation.
Row      one file's proposal: the name it gets, where it goes, whether it becomes a bill,
         and whether it is ready or waits for a person.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

DOC_TYPES = ("invoice", "statement", "payment")
DATE_CONFIDENCE = ("labelled", "single_unlabelled", "ambiguous", "none")

READY = "ready"
NEEDS_PERSON = "needs a person"


@dataclass
class Reading:
    vendor: str | None = None
    doc_type: str | None = None          # None when the model gave something outside DOC_TYPES
    doc_date: str | None = None          # YYYY-MM-DD
    period_end: str | None = None        # statements only
    due_date: str | None = None          # only when printed; never computed from terms
    date_confidence: str = "none"
    amount: float | None = None
    is_paid: bool = False
    invoice_no: str | None = None
    personal_data: bool = False          # patient names or clinical details on the page
    not_a_bill_document: bool = False    # packing slip, quote, letter
    notes: str = ""
    problems: list[str] = field(default_factory=list)   # fields the validator threw away, and why


@dataclass
class Row:
    source_name: str
    source_path: str
    vendor: str | None = None
    folder: str | None = None
    match_how: str = ""
    doc_type: str = "invoice"
    doc_date: str | None = None
    due_date: str | None = None
    amount: float | None = None
    is_paid: bool = False
    invoice_no: str | None = None
    personal_data: bool = False
    new_name: str | None = None
    new_path: str | None = None
    qb_vendor: str | None = None
    account: str | None = None
    not_a_bill: str = ""                 # why a filed document is never entered, or "" when it is a bill
    status: str = READY
    reason: str = ""
    notes: str = ""
    approved: bool = True

    @property
    def is_bill(self) -> bool:
        return not self.not_a_bill

    def needs_person(self, reason: str) -> "Row":
        self.status, self.reason, self.approved = NEEDS_PERSON, reason, False
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_bill"] = self.is_bill
        return d
