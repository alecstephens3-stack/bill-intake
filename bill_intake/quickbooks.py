"""Bills into QuickBooks Online, through its Accounting API.

The app creates each bill from its own reading of the document; QuickBooks never sees the file.
It never creates a vendor or an account: a vendor QuickBooks does not know is a row for a person,
because a misspelled duplicate vendor is worse than a bill entered by hand.

Two clients share the small set of calls `create_bills()` needs:

  QuickBooksClient  the documented REST API (v3), OAuth 2.0 with refresh-token rotation, retries
                    with backoff, idempotent creates. `session` is injectable, so the tests drive
                    it with a fake HTTP session and nothing leaves the machine.
  FakeQuickBooks    an in-memory company for tests and the demo.

Every bill this app makes carries a tag in its private note ("run <id>, as <file name>"). If a
create succeeds but the reply is lost, the next attempt finds the tag and treats the bill as its
own instead of making a second one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Protocol

from .rules import norm

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"
API_BASE = {"production": "https://quickbooks.api.intuit.com",
            "sandbox": "https://sandbox-quickbooks.api.intuit.com"}
MINOR_VERSION = "75"
DOC_NUMBER_MAX = 21          # QuickBooks keeps 21 characters of Bill.DocNumber
PAGE = 1000                  # rows per query page, the API's maximum


class QBError(Exception):
    """QuickBooks said no. The message is already in plain words."""


class QBReconnect(QBError):
    """The connection is gone (revoked, expired, never made). Someone has to connect again."""


class QBUnreachable(QBError):
    """QuickBooks could not be reached after the retries. Later calls would wait the same way."""


# ------------------------------------------------------------------ tokens
class TokenStore(Protocol):
    def load(self) -> dict | None: ...
    def save(self, token: dict) -> None: ...


class MemoryTokenStore:
    def __init__(self, token: dict | None = None):
        self.token = dict(token) if token else None
        self.saves = 0

    def load(self) -> dict | None:
        return dict(self.token) if self.token else None

    def save(self, token: dict) -> None:
        self.token = dict(token)
        self.saves += 1


class FileTokenStore:
    """JSON on disk, readable only by its owner, written whole (temp file then replace) so a crash
    mid-write never leaves half a token. Use the OS keychain in a real install."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, token: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(token, f)
        os.replace(tmp, self.path)


# ------------------------------------------------------------------ client
class QuickBooksClient:
    def __init__(self, client_id: str, client_secret: str, realm_id: str, store: TokenStore,
                 env: str = "sandbox", session=None, sleep: Callable[[float], None] = time.sleep):
        if env not in API_BASE:
            raise ValueError(f"env must be one of {sorted(API_BASE)}")
        self.client_id, self.client_secret, self.realm_id = client_id, client_secret, str(realm_id)
        self.store = store
        self.base = f"{API_BASE[env]}/v3/company/{self.realm_id}"
        self.sleep = sleep
        if session is None:
            import requests  # optional dependency, only for real runs
            session = requests.Session()
        self.sess = session
        self._access: str | None = None
        self._expires = 0.0
        self._vendors: dict[str, list[dict]] | None = None
        self._accounts: dict[str, list[dict]] | None = None

    # -- connecting (once, by a QuickBooks admin in a browser)
    @staticmethod
    def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
        q = {"client_id": client_id, "response_type": "code", "scope": SCOPE,
             "redirect_uri": redirect_uri, "state": state}
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def exchange_code(self, code: str, redirect_uri: str) -> None:
        body = self._token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        self._keep(body)

    # -- tokens
    def _basic(self) -> dict:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode(), "Accept": "application/json"}

    def _token_call(self, form: dict) -> dict:
        try:
            r = self.sess.post(TOKEN_URL, headers=self._basic(), data=form, timeout=30)
        except Exception as e:  # noqa: BLE001
            raise QBUnreachable("Could not reach QuickBooks to sign in. Check the connection.") from e
        if r.status_code in (400, 401):
            raise QBReconnect("QuickBooks needs to be connected again.")
        if r.status_code >= 400:
            raise QBUnreachable(f"QuickBooks sign-in answered {r.status_code}. Try again in a minute.")
        return r.json()

    def _keep(self, body: dict) -> None:
        """Intuit rotates the refresh token: once a refresh succeeds the old one may be dead. The
        new one is saved before the access token is used for anything, so a crash right after a
        refresh can never strand the connection."""
        old = self.store.load() or {}
        self.store.save(dict(old, refresh_token=body.get("refresh_token") or old.get("refresh_token"),
                             realm_id=self.realm_id, saved_at=int(time.time())))
        self._access = body["access_token"]
        self._expires = time.time() + int(body.get("expires_in", 3600)) - 120

    def _token(self) -> str:
        if self._access and time.time() < self._expires:
            return self._access
        tok = self.store.load()
        if not tok or not tok.get("refresh_token"):
            raise QBReconnect("QuickBooks is not connected yet.")
        if str(tok.get("realm_id", self.realm_id)) != self.realm_id:
            raise QBReconnect("The saved connection is for a different QuickBooks company.")
        self._keep(self._token_call({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}))
        return self._access

    # -- transport
    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
              text: str | None = None) -> dict:
        params = dict(params or {}, minorversion=MINOR_VERSION)
        url = f"{self.base}/{path.lstrip('/')}"
        refreshed = False
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
            data = None
            if text is not None:
                headers["Content-Type"], data = "application/text", text
            elif body is not None:
                headers["Content-Type"], data = "application/json", json.dumps(body)
            try:
                r = self.sess.request(method, url, params=params, headers=headers, data=data, timeout=45)
            except Exception as e:  # noqa: BLE001  (a dropped connection; creates are safe to resend)
                if attempt < 3:
                    self.sleep(2 ** attempt)
                    continue
                raise QBUnreachable("Could not reach QuickBooks. Check the connection and try again.") from e
            if r.status_code == 401 and not refreshed:
                self._access, refreshed = None, True       # expired early: refresh once and retry
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt < 3:
                    self.sleep(2 ** attempt)
                    continue
                break
            try:
                out = r.json()
            except ValueError:
                out = {}
            if r.status_code == 401:
                raise QBReconnect("QuickBooks needs to be connected again.")
            if r.status_code >= 400 or "Fault" in out:
                raise QBError(_fault_words(out, r.status_code))
            return out
        raise QBUnreachable("QuickBooks is busy right now. Wait a minute and try again.")

    def query(self, select: str, entity: str) -> list[dict]:
        out, start = [], 1
        while True:
            got = self._call("POST", "query", text=f"{select} STARTPOSITION {start} MAXRESULTS {PAGE}")
            rows = got.get("QueryResponse", {}).get(entity, [])
            out += rows
            if len(rows) < PAGE:
                return out
            start += PAGE

    # -- the calls create_bills() uses
    def find_vendor(self, name: str | None) -> tuple[dict | None, str]:
        if self._vendors is None:
            got: dict[str, list[dict]] = {}
            for v in self.query("SELECT * FROM Vendor WHERE Active = true", "Vendor"):
                got.setdefault(norm(v.get("DisplayName", "")), []).append(v)
            self._vendors = got     # kept only once the whole list arrived
        return _one(self._vendors.get(norm(name or ""), []), "vendor", name)

    def find_account(self, name: str | None) -> tuple[dict | None, str]:
        if self._accounts is None:
            got: dict[str, list[dict]] = {}
            for a in self.query("SELECT * FROM Account WHERE Active = true", "Account"):
                for key in {a.get("Name", ""), a.get("FullyQualifiedName", "")}:
                    if key:
                        got.setdefault(norm(key), []).append(a)
            self._accounts = got
        return _one(self._accounts.get(norm(name or ""), []), "expense account", name)

    def bills_by_number(self, vendor_id: str, doc_number: str) -> list[dict]:
        esc = doc_number.replace("\\", "\\\\").replace("'", "\\'")
        rows = self.query(f"SELECT * FROM Bill WHERE DocNumber = '{esc}'", "Bill")
        return [b for b in rows if str((b.get("VendorRef") or {}).get("value")) == str(vendor_id)]

    def bills_on_date(self, vendor_id: str, txn_date: str) -> list[dict]:
        rows = self.query(f"SELECT * FROM Bill WHERE TxnDate = '{txn_date}'", "Bill")
        return [b for b in rows if str((b.get("VendorRef") or {}).get("value")) == str(vendor_id)]

    def create_bill(self, payload: dict, request_id: str) -> dict:
        # requestid makes the create idempotent: a resend after a dropped reply cannot make a second bill
        return self._call("POST", "bill", params={"requestid": request_id}, body=payload).get("Bill", {})

    def get_bill(self, bill_id: str) -> dict | None:
        try:
            return self._call("GET", f"bill/{bill_id}").get("Bill")
        except QBError as e:
            if "not found" in str(e).lower() or "object not found" in str(e).lower():
                return None
            raise

    def delete_bill(self, bill_id: str, sync_token: str) -> None:
        self._call("POST", "bill", params={"operation": "delete"}, body={"Id": str(bill_id), "SyncToken": str(sync_token)})


def _one(hits: list[dict], what: str, name: str | None) -> tuple[dict | None, str]:
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, f"no {what} called '{name}' in QuickBooks: add it there or fix the rules file"
    return None, f"more than one {what} matches '{name}' in QuickBooks: fix the rules file"


def _fault_words(out: dict, code: int) -> str:
    errs = (out.get("Fault") or {}).get("Error") or []
    if errs:
        msg = (errs[0].get("Message") or "").strip()
        detail = (errs[0].get("Detail") or "").strip()
        return f"QuickBooks said: {msg}" + (f" ({detail[:200]})" if detail and detail != msg else "")
    return f"QuickBooks answered with an error ({code})."


# ----------------------------------------------------------- fake company
class FakeQuickBooks:
    """An in-memory QuickBooks company with the same calls as QuickBooksClient."""

    def __init__(self, vendors: list[str], accounts: list[str]):
        self.vendors = [{"Id": str(i + 1), "DisplayName": n} for i, n in enumerate(vendors)]
        self.accounts = [{"Id": str(100 + i), "Name": n, "FullyQualifiedName": n} for i, n in enumerate(accounts)]
        self.bills: dict[str, dict] = {}
        self.request_ids: dict[str, str] = {}
        self.lose_reply_once = False     # create the bill, then act as if the reply never came
        self.down = False

    def _check(self):
        if self.down:
            raise QBUnreachable("Could not reach QuickBooks. Check the connection and try again.")

    def find_vendor(self, name):
        self._check()
        return _one([v for v in self.vendors if norm(v["DisplayName"]) == norm(name or "")], "vendor", name)

    def find_account(self, name):
        self._check()
        return _one([a for a in self.accounts if norm(a["Name"]) == norm(name or "")], "expense account", name)

    def bills_by_number(self, vendor_id, doc_number):
        self._check()
        return [b for b in self.bills.values() if b["VendorRef"]["value"] == vendor_id and b.get("DocNumber") == doc_number]

    def bills_on_date(self, vendor_id, txn_date):
        self._check()
        return [b for b in self.bills.values() if b["VendorRef"]["value"] == vendor_id and b["TxnDate"] == txn_date]

    def create_bill(self, payload, request_id):
        self._check()
        if request_id in self.request_ids:
            return self.bills[self.request_ids[request_id]]
        bid = str(len(self.bills) + 1000)
        total = round(sum(ln["Amount"] for ln in payload["Line"]), 2)
        self.bills[bid] = dict(payload, Id=bid, SyncToken="0", TotalAmt=total, Balance=total)
        self.request_ids[request_id] = bid
        if self.lose_reply_once:
            self.lose_reply_once = False
            raise QBUnreachable("Could not reach QuickBooks. Check the connection and try again.")
        return self.bills[bid]

    def get_bill(self, bill_id):
        self._check()
        return self.bills.get(str(bill_id))

    def delete_bill(self, bill_id, sync_token):
        self._check()
        self.bills.pop(str(bill_id), None)


# ------------------------------------------------------------ bills for a run
def doc_number(row) -> str:
    """A printed invoice number keeps its END (long numbers carry their unique digits there).
    Without one, the file name stands in and keeps its START, the date and vendor, plus any _1."""
    printed = str(row.invoice_no).strip() if row.invoice_no else ""
    if printed:
        return printed[-DOC_NUMBER_MAX:]
    stem = Path(row.new_name or "").stem
    head, _, tail = stem.rpartition("_")
    if head and tail.isdigit():
        return head[: DOC_NUMBER_MAX - len(tail) - 1] + "_" + tail
    return stem[:DOC_NUMBER_MAX]


def tag(run_id: str, row) -> str:
    return f"run {run_id}, as {row.new_name}"


def bill_payload(row, vendor: dict, account: dict, run_id: str) -> dict:
    due = row.due_date if row.due_date and row.due_date >= row.doc_date else row.doc_date
    note = f"Filed by bill-intake, {tag(run_id, row)}"
    if row.is_paid:
        note += ". Already paid per the receipt"
    return {
        "VendorRef": {"value": str(vendor["Id"]), "name": vendor.get("DisplayName", "")},
        "TxnDate": row.doc_date,
        "DueDate": due,
        "DocNumber": doc_number(row),
        "PrivateNote": note[:4000],
        "Line": [{
            "Amount": round(float(row.amount), 2),
            "DetailType": "AccountBasedExpenseLineDetail",
            "Description": Path(row.new_name or "").stem[:4000],
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": str(account["Id"])}},
        }],
    }


def request_id(run_id: str, payload: dict, attempt: int = 0) -> str:
    """Same run + same bill + same attempt -> same id, so a resend never makes a second bill.
    A deliberate retry later is a new attempt with a new id, because Intuit can replay a stored
    refusal for a reused id even after the cause was fixed."""
    raw = f"{run_id}|{attempt}|{json.dumps(payload, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:36]


def find_existing(client, row, vendor_id: str, no: str, run_id: str) -> tuple[str, dict | None, str]:
    """Is this bill already in QuickBooks? Never silent either way.
      ours   made by this run earlier; the reply was lost
      there  same vendor, number, date and total: skip it
      ask    maybe the same (same number, other total or date; or no number, same date and total)
      new    nothing like it"""
    printed = bool(row.invoice_no)
    by_no = client.bills_by_number(vendor_id, no)
    by_date = [] if printed else client.bills_on_date(vendor_id, row.doc_date)
    mine = tag(run_id, row)
    for b in by_no + by_date:
        if mine in (b.get("PrivateNote") or ""):
            return "ours", b, ""
    same_amt = lambda b: abs(float(b.get("TotalAmt") or 0) - float(row.amount)) < 0.005  # noqa: E731
    for b in by_no:
        if b.get("TxnDate") == row.doc_date and same_amt(b):
            return "there", b, f"bill {no} is already in QuickBooks"
    for b in by_no:
        if printed:
            return "ask", b, (f"QuickBooks already has bill {no} from this vendor "
                              f"(dated {b.get('TxnDate')}, ${float(b.get('TotalAmt') or 0):,.2f}). "
                              "If this is a separate bill, enter it by hand")
    for b in by_date:
        if same_amt(b):
            return "ask", b, ("QuickBooks already has a bill from this vendor on this date for the same total, "
                              "and this page has no number (a second scan?)")
    return "new", None, ""


def create_bills(rows: list, run_id: str, client, attempt: int = 0) -> list[dict]:
    """One unpaid bill per row. One bad row never stops the others; a lost connection stops the
    rest at once, since every later row would only wait and fail the same way. Exactly one
    outcome per row: created | already there | possible copy | needs a person | failed."""
    out: list[dict] = []
    for k, r in enumerate(rows):
        try:
            if r.amount is None or r.amount <= 0:
                out.append(_outcome(r, "needs a person", "zero or negative amount (a credit?): enter it by hand"))
                continue
            vendor, why = client.find_vendor(r.qb_vendor or r.folder)
            if not vendor:
                out.append(_outcome(r, "needs a person", why))
                continue
            account, why = client.find_account(r.account)
            if not account:
                out.append(_outcome(r, "needs a person", why))
                continue
            no = doc_number(r)
            verdict, found, why = find_existing(client, r, vendor["Id"], no, run_id)
            if verdict == "there":
                out.append(_outcome(r, "already there", why, found.get("Id")))
                continue
            if verdict == "ask":
                out.append(_outcome(r, "possible copy", why, found.get("Id")))
                continue
            if verdict == "ours":
                bill = found
            else:
                payload = bill_payload(r, vendor, account, run_id)
                bill = client.create_bill(payload, request_id(run_id, payload, attempt))
            out.append(_outcome(r, "created", "", bill.get("Id"), bill.get("SyncToken")))
        except (QBReconnect, QBUnreachable) as e:
            out += [_outcome(x, "failed", str(e)) for x in rows[k:]]
            break
        except QBError as e:
            out.append(_outcome(r, "failed", str(e)))
    return out


def undo_bills(created: list[dict], client) -> list[dict]:
    """Delete bills a run created, unless one has been paid (fully or in part) since. A paid bill
    is a person's decision to unwind, not this app's."""
    out = []
    for o in created:
        if o["status"] != "created" or not o["bill_id"]:
            continue
        live = client.get_bill(o["bill_id"])
        if live is None:
            out.append(dict(o, status="already gone"))
        elif abs(float(live.get("Balance") or 0) - float(live.get("TotalAmt") or 0)) > 0.005:
            out.append(dict(o, status="kept", why="a payment was applied since: undo it in QuickBooks by hand"))
        else:
            client.delete_bill(o["bill_id"], live.get("SyncToken", o["sync"] or "0"))
            out.append(dict(o, status="deleted"))
    return out


def _outcome(r, status: str, why: str = "", bill_id=None, sync=None) -> dict:
    return {"file": r.new_name, "vendor": r.qb_vendor or r.folder or "", "amount": r.amount,
            "date": r.doc_date, "no": doc_number(r), "status": status, "why": why,
            "bill_id": str(bill_id) if bill_id else "", "sync": str(sync) if sync is not None else ""}
