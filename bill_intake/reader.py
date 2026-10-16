"""The only step that uses a model: turn one messy document into a few typed fields.

Everything the model returns is treated as input, not truth. `validate()` checks each field
against the schema and throws out what does not fit (a date that is not a date, a doc type that
is not one of three, an amount that is not a number). A thrown-out field never becomes a guess;
it becomes a row that waits for a person.

Two readers share one interface:

  BedrockReader  Claude on AWS Bedrock through the Converse API. Real documents go here, and only
                 here, because it runs inside an AWS account covered by a business associate
                 agreement with invocation logging off.
  FakeReader     Reads labelled lines ("Vendor: ...", "Invoice Date: ...") with regular
                 expressions. No network, no credentials. Used by the tests and the demo.

`read_all()` reads several documents at once. Most of the wall-clock time per document is
waiting on the reply, so overlapping a few calls is what makes a folder quick; a small pool keeps
the account under its rate limit. One document per call keeps every answer tied to one file, and
one bad file cannot spoil the others.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from .models import DATE_CONFIDENCE, DOC_TYPES, Reading
from .text import Document, extract

SCHEMA = {
    "vendor": "string or null. The business that sent the document, as printed in the letterhead, remit-to block or payable-to line.",
    "doc_type": "one of: invoice | statement | payment. invoice = a bill or a receipt for a purchase. statement = an account summary over a period. payment = a remittance advice or payment confirmation only.",
    "doc_date": "YYYY-MM-DD or null. The date printed beside its label (Invoice Date, Date, Bill Date, Statement Date). Never today's date.",
    "period_end": "YYYY-MM-DD or null. Statements only: the last day of the period covered, if printed.",
    "due_date": "YYYY-MM-DD or null. Only a printed, labelled due date. Never compute one from terms such as Net 30.",
    "date_confidence": "one of: labelled | single_unlabelled | ambiguous | none",
    "amount": "number or null. The balance or amount due if above zero; otherwise the stated total.",
    "is_paid": "true if the amount due is zero because it was already paid",
    "invoice_no": "the vendor's own invoice or statement number, or null. Never a patient, order or account number.",
    "personal_data": "true if the page shows a patient name or clinical details",
    "not_a_bill_document": "true only for a packing slip, quote, shipping label, letter or similar page that is not a bill, receipt, statement or payment record",
    "notes": "empty unless a bookkeeper must know something unusual (a credit, freight outside the total, several dates). At most 8 words. Never names or clinical details.",
}

SYSTEM = (
    "You are the reading step of an accounts-payable filing tool for a small healthcare practice.\n"
    "You get the text of ONE vendor document. Return ONLY a JSON object with exactly these keys:\n"
    + json.dumps(SCHEMA, indent=2)
    + "\n\nRules:\n"
    "- Find a date's label first, then read the value beside it. If there are several unlabelled dates, "
    "set date_confidence to ambiguous and leave doc_date null.\n"
    "- If the amount due is 0.00 because it was paid, give the total and set is_paid true.\n"
    "- If patient information is present, set personal_data true and write none of it anywhere.\n"
    "- No prose outside the JSON."
)

MAX_TEXT_CHARS = 20_000      # a cap on what one call can cost; bills are far shorter


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


class ReaderUnavailable(Exception):
    """The reader cannot work at all (bad key, no model access). Stops the run: every other file
    would fail the same way, and sending them would only spend time."""


class Reader(Protocol):
    def read(self, doc: Document) -> tuple[dict, Usage]: ...


# ----------------------------------------------------------------- validation
def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    out = json.loads(m.group(0))
    if not isinstance(out, dict):
        raise ValueError("reply is not a JSON object")
    return out


def _iso(v, key: str, problems: list[str]) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, str):
        try:
            date.fromisoformat(v)
            return v
        except ValueError:
            pass
    problems.append(f"{key}: not a YYYY-MM-DD date")
    return None


def to_amount(v) -> float | None:
    """265, 265.0, "265.00" and "$1,265.00" are all 265-ish numbers; anything else is None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v else None
    try:
        x = float(str(v).strip().replace("$", "").replace(",", ""))
    except ValueError:
        return None
    return x if x == x else None


def _loose_flag(v) -> bool:
    """For flags that make a row MORE careful: anything that is not a clear "no" counts as yes."""
    return v not in (None, False, 0) and str(v).strip().lower() not in ("", "false", "no", "0", "null", "none")


def validate(raw: dict) -> Reading:
    p: list[str] = []
    doc_type = raw.get("doc_type")
    if doc_type not in DOC_TYPES:
        p.append("doc_type: not invoice, statement or payment")
        doc_type = None
    conf = raw.get("date_confidence")
    if conf not in DATE_CONFIDENCE:
        conf = "none"
    amount = to_amount(raw.get("amount"))
    if raw.get("amount") not in (None, "") and amount is None:
        p.append("amount: not a number")
    no = raw.get("invoice_no")
    vendor = raw.get("vendor")
    personal = _loose_flag(raw.get("personal_data"))
    return Reading(
        vendor=vendor.strip() if isinstance(vendor, str) and vendor.strip() else None,
        doc_type=doc_type,
        doc_date=_iso(raw.get("doc_date"), "doc_date", p),
        period_end=_iso(raw.get("period_end"), "period_end", p),
        due_date=_iso(raw.get("due_date"), "due_date", p),
        date_confidence=conf,
        amount=amount,
        is_paid=raw.get("is_paid") is True,            # makes a row less careful: only a real true counts
        # an all-digit invoice number can come back as a JSON number: it is text from here on
        invoice_no=(str(no).strip() or None) if no is not None and not isinstance(no, bool) else None,
        personal_data=personal,
        not_a_bill_document=_loose_flag(raw.get("not_a_bill_document")),
        # the model's free text never travels with a page that holds patient information
        notes="" if personal else str(raw.get("notes") or "")[:120],
        problems=p,
    )


# ---------------------------------------------------------------- the readers
class BedrockReader:
    """Claude on AWS Bedrock. `client` is injectable so tests never touch the network."""

    FATAL_CODES = {"AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException",
                   "ResourceNotFoundException"}

    def __init__(self, model_id: str, region: str = "us-east-1", client=None, max_tokens: int = 600):
        self.model_id = model_id
        self.max_tokens = max_tokens
        if client is None:
            import boto3  # optional dependency, only for real runs
            from botocore.config import Config

            # "slow down" answers are waited out and retried by the SDK, never a stopped run
            client = boto3.client("bedrock-runtime", region_name=region,
                                  config=Config(retries={"max_attempts": 6, "mode": "standard"}))
        self.client = client

    def read(self, doc: Document) -> tuple[dict, Usage]:
        if doc.images:
            content = [{"image": {"format": "png", "source": {"bytes": b}}} for b in doc.images]
        else:
            content = [{"text": "<document>\n" + doc.text[:MAX_TEXT_CHARS] + "\n</document>"}]
        content.append({"text": "Read this document and return the JSON."})
        t0 = time.time()
        try:
            resp = self.client.converse(
                modelId=self.model_id,
                system=[{"text": SYSTEM}],
                messages=[{"role": "user", "content": content}],
                inferenceConfig={"maxTokens": self.max_tokens, "temperature": 0},
            )
        except Exception as e:  # noqa: BLE001
            code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
            if code in self.FATAL_CODES:
                raise ReaderUnavailable(f"Bedrock refused the call ({code}). Check the key and model access.") from e
            raise
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        u = resp.get("usage", {})
        return parse_json(text), Usage(u.get("inputTokens", 0), u.get("outputTokens", 0), time.time() - t0)


_LABELS = {
    "vendor": r"^(?:vendor|from)\s*:\s*(.+)$",
    "doc_type": r"^(?:document|type)\s*:\s*(invoice|statement|payment)\b",
    "doc_date": r"^(?:invoice date|statement date|date)\s*:\s*(\d{4}-\d{2}-\d{2})",
    "period_end": r"^period end\s*:\s*(\d{4}-\d{2}-\d{2})",
    "due_date": r"^due date\s*:\s*(\d{4}-\d{2}-\d{2})",
    "amount": r"^(?:amount due|balance due|total)\s*:\s*\$?([\d,]+\.\d{2})",
    "invoice_no": r"^(?:invoice|statement) (?:no|number|#)\.?\s*:\s*(\S+)",
}


class FakeReader:
    """Deterministic stand-in for the model. Reads labelled lines; `overrides` maps a file name to
    the exact reply to return (for testing how the pipeline treats a bad or odd reply)."""

    def __init__(self, overrides: dict[str, dict] | None = None, fail: dict[str, Exception] | None = None):
        self.overrides = overrides or {}
        self.fail = fail or {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def read(self, doc: Document) -> tuple[dict, Usage]:
        name = doc.path.name
        with self._lock:
            self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]
        if name in self.overrides:
            return dict(self.overrides[name]), Usage()
        out: dict = {}
        for key, rx in _LABELS.items():
            m = re.search(rx, doc.text, re.I | re.M)
            if m:
                out[key] = m.group(1).strip()
        out["doc_type"] = (out.get("doc_type") or "invoice").lower()
        out["date_confidence"] = "labelled" if out.get("doc_date") else "none"
        out["is_paid"] = bool(re.search(r"^paid in full", doc.text, re.I | re.M))
        out["not_a_bill_document"] = bool(re.search(r"^packing slip", doc.text, re.I | re.M))
        out["personal_data"] = bool(re.search(r"^patient\s*:", doc.text, re.I | re.M))
        return out, Usage()


# ------------------------------------------------------------- reading a folder
@dataclass
class ReadResult:
    path: Path
    reading: Reading | None
    error: str = ""


def read_one(reader: Reader, path: Path) -> ReadResult:
    try:
        doc = extract(path)
    except Exception as e:  # noqa: BLE001  (corrupt, not really a PDF, open in another program)
        return ReadResult(path, None, f"could not open the file ({type(e).__name__})")
    if not doc.has_text_layer and not doc.images:
        return ReadResult(path, None, "no text layer (a scan?): file it by hand")
    try:
        raw, _usage = reader.read(doc)
    except ReaderUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        return ReadResult(path, None, f"the reader could not read it ({type(e).__name__})")
    return ReadResult(path, validate(raw))


def read_all(reader: Reader, paths: list[Path], workers: int = 4,
             on_progress: Callable[[int, int], None] | None = None) -> list[ReadResult]:
    """Read every file, `workers` at a time, and return results in the folder's order.
    A ReaderUnavailable stops the run, and files not yet sent are never sent."""
    stopped = threading.Event()
    results: list[ReadResult | None] = [None] * len(paths)

    def one(p: Path) -> ReadResult:
        if stopped.is_set():
            return ReadResult(p, None, "not read: the run stopped")
        return read_one(reader, p)

    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {ex.submit(one, p): i for i, p in enumerate(paths)}
        done = 0
        for f in as_completed(futures):
            results[futures[f]] = f.result()
            done += 1
            if on_progress:
                on_progress(done, len(paths))
    except BaseException:
        stopped.set()
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    return [r for r in results if r is not None]
