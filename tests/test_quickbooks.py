import json
from urllib.parse import parse_qs, urlparse

import pytest

from bill_intake import quickbooks as qb
from bill_intake.models import Row
from bill_intake.quickbooks import (FakeQuickBooks, FileTokenStore, MemoryTokenStore, QBReconnect,
                                    QBUnreachable, QuickBooksClient, create_bills, doc_number,
                                    request_id, undo_bills)


# ------------------------------------------------------------- a fake HTTP session
class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """Answers the token endpoint and the API from scripted queues. Records every call."""

    def __init__(self, api=None, token=None):
        self.api = list(api or [])
        self.token = list(token or [])
        self.calls = []

    def post(self, url, headers=None, data=None, timeout=None):
        assert url == qb.TOKEN_URL
        self.calls.append(("TOKEN", data))
        r = self.token.pop(0) if self.token else Resp(200, {"access_token": f"at-{len(self.calls)}",
                                                             "refresh_token": f"rt-{len(self.calls)}",
                                                             "expires_in": 3600})
        if isinstance(r, Exception):
            raise r
        return r

    def request(self, method, url, params=None, headers=None, data=None, timeout=None):
        self.calls.append((method, url, dict(params or {}), headers, data))
        r = self.api.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(session, store=None):
    store = store or MemoryTokenStore({"refresh_token": "rt-0", "realm_id": "4620"})
    return QuickBooksClient("cid", "csecret", "4620", store, env="sandbox", session=session, sleep=lambda s: None)


def api_calls(sess):
    return [c for c in sess.calls if c[0] != "TOKEN"]


# ------------------------------------------------------------- tokens
def test_refresh_saves_the_rotated_token_before_using_it():
    store = MemoryTokenStore({"refresh_token": "rt-0", "realm_id": "4620"})
    seen_at_call = []

    class Watch(FakeSession):
        def request(self, *a, **k):
            seen_at_call.append(store.load()["refresh_token"])
            return super().request(*a, **k)

    sess = Watch(api=[Resp(200, {"CompanyInfo": {}})])
    client(sess, store)._call("GET", "companyinfo/4620")
    assert sess.calls[0] == ("TOKEN", {"grant_type": "refresh_token", "refresh_token": "rt-0"})
    assert seen_at_call == ["rt-1"]            # the new one was on disk before the first API call


def test_a_revoked_connection_says_reconnect():
    sess = FakeSession(token=[Resp(400, {"error": "invalid_grant"})])
    with pytest.raises(QBReconnect):
        client(sess)._call("GET", "companyinfo/4620")


def test_no_saved_token_says_reconnect():
    with pytest.raises(QBReconnect):
        client(FakeSession(), MemoryTokenStore())._call("GET", "x")


def test_token_for_another_company_is_refused():
    store = MemoryTokenStore({"refresh_token": "rt-0", "realm_id": "999"})
    with pytest.raises(QBReconnect):
        client(FakeSession(), store)._call("GET", "x")


def test_file_token_store_is_private_and_whole(tmp_path):
    store = FileTokenStore(tmp_path / "state" / "token.json")
    store.save({"refresh_token": "rt-9", "realm_id": "4620"})
    assert store.load()["refresh_token"] == "rt-9"
    assert (store.path.stat().st_mode & 0o077) == 0
    assert not list(store.path.parent.glob("*.tmp"))


def test_authorization_url():
    u = urlparse(QuickBooksClient.authorization_url("cid", "https://example.com/cb", "st"))
    q = parse_qs(u.query)
    assert q["scope"] == [qb.SCOPE] and q["state"] == ["st"] and q["response_type"] == ["code"]


# ------------------------------------------------------------- transport
def test_401_refreshes_once_then_retries():
    sess = FakeSession(api=[Resp(401, {}), Resp(200, {"ok": 1})])
    assert client(sess)._call("GET", "x") == {"ok": 1}
    assert [c[0] for c in sess.calls] == ["TOKEN", "GET", "TOKEN", "GET"]


def test_429_and_5xx_back_off_then_succeed():
    waits = []
    sess = FakeSession(api=[Resp(429), Resp(503), Resp(200, {"ok": 1})])
    c = client(sess)
    c.sleep = waits.append
    assert c._call("GET", "x") == {"ok": 1}
    assert waits == [1, 2]


def test_gives_up_after_retries():
    sess = FakeSession(api=[Resp(503)] * 4)
    with pytest.raises(QBUnreachable):
        client(sess)._call("GET", "x")


def test_fault_becomes_plain_words():
    fault = {"Fault": {"Error": [{"Message": "Duplicate Document Number Error", "Detail": "Number 7 used"}]}}
    with pytest.raises(qb.QBError, match="Duplicate Document Number Error"):
        client(FakeSession(api=[Resp(400, fault)]))._call("POST", "bill", body={})


def test_every_call_pins_the_minor_version_and_create_sends_requestid():
    sess = FakeSession(api=[Resp(200, {"Bill": {"Id": "1"}})])
    client(sess).create_bill({"x": 1}, "req-1")
    method, url, params, headers, data = api_calls(sess)[0]
    assert url.endswith("/v3/company/4620/bill")
    assert params == {"requestid": "req-1", "minorversion": qb.MINOR_VERSION}
    assert json.loads(data) == {"x": 1} and headers["Content-Type"] == "application/json"


def test_query_pages_through_everything():
    page1 = {"QueryResponse": {"Vendor": [{"Id": str(i)} for i in range(qb.PAGE)]}}
    page2 = {"QueryResponse": {"Vendor": [{"Id": "last"}]}}
    sess = FakeSession(api=[Resp(200, page1), Resp(200, page2)])
    rows = client(sess).query("SELECT * FROM Vendor", "Vendor")
    assert len(rows) == qb.PAGE + 1
    texts = [c[4] for c in api_calls(sess)]
    assert "STARTPOSITION 1 " in texts[0] and f"STARTPOSITION {qb.PAGE + 1} " in texts[1]


def test_query_quotes_are_escaped():
    sess = FakeSession(api=[Resp(200, {"QueryResponse": {}})])
    client(sess).bills_by_number("1", "O'Brien-7")
    assert "DocNumber = 'O\\'Brien-7'" in api_calls(sess)[0][4]


def test_vendor_lookup_is_exact_after_normalising_and_refuses_two():
    vendors = {"QueryResponse": {"Vendor": [{"Id": "1", "DisplayName": "Acme Medical Supply, Inc."},
                                            {"Id": "2", "DisplayName": "Twin Co"}, {"Id": "3", "DisplayName": "Twin"}]}}
    c = client(FakeSession(api=[Resp(200, vendors)]))
    assert c.find_vendor("acme medical supply")[0]["Id"] == "1"
    assert c.find_vendor("Twin")[0] is None
    assert c.find_vendor("Fabrikam")[0] is None


# ------------------------------------------------------------- bills
def bill_row(**kw):
    base = dict(source_name="s.pdf", source_path="/in/s.pdf", folder="Acme Medical Supply",
                qb_vendor="Acme Medical Supply", account="Medical Supplies", doc_date="2026-03-04",
                amount=1265.0, invoice_no="A-10442", new_name="2026.03.04_Acme Medical Supply.pdf")
    base.update(kw)
    return Row(**base)


def company():
    return FakeQuickBooks(["Acme Medical Supply", "Northwind Office Company"], ["Medical Supplies", "Office Expenses"])


def test_doc_number_keeps_the_end_of_a_long_printed_number_and_the_start_of_a_file_name():
    assert doc_number(bill_row(invoice_no="INV-2026-000000000012345")) == "INV-2026-000000000012345"[-21:] == "-2026-000000000012345"
    r = bill_row(invoice_no=None, new_name="2026.03.04_Acme Medical Supply_1.pdf")
    assert doc_number(r) == "2026.03.04_Acme Med_1" and len(doc_number(r)) == 21


def test_request_id_is_stable_per_attempt():
    p = {"a": 1}
    assert request_id("r", p) == request_id("r", p)
    assert request_id("r", p, 0) != request_id("r", p, 1)
    assert request_id("r", p) != request_id("r", {"a": 2})


def test_create_and_skip_an_exact_copy():
    c = company()
    out = create_bills([bill_row()], "run-1", c)
    assert out[0]["status"] == "created"
    bill = c.bills[out[0]["bill_id"]]
    assert bill["DocNumber"] == "A-10442" and bill["TxnDate"] == "2026-03-04"
    assert bill["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] == "100"
    again = create_bills([bill_row()], "run-2", c)
    assert again[0]["status"] == "already there" and len(c.bills) == 1


def test_same_number_other_total_is_put_to_a_person():
    c = company()
    create_bills([bill_row()], "run-1", c)
    out = create_bills([bill_row(amount=1300.0)], "run-2", c)
    assert out[0]["status"] == "possible copy" and len(c.bills) == 1


def test_no_number_same_day_same_total_is_put_to_a_person():
    c = company()
    create_bills([bill_row(invoice_no=None)], "run-1", c)
    out = create_bills([bill_row(invoice_no=None, new_name="2026.03.04_Acme Medical Supply_1.pdf")], "run-2", c)
    assert out[0]["status"] == "possible copy"


def test_lost_reply_is_recovered_without_a_second_bill():
    c = company()
    c.lose_reply_once = True
    first = create_bills([bill_row(), bill_row(invoice_no="A-2", new_name="b.pdf")], "run-1", c)
    assert [o["status"] for o in first] == ["failed", "failed"]     # connection lost: the rest stop at once
    retry = create_bills([bill_row(), bill_row(invoice_no="A-2", new_name="b.pdf")], "run-1", c, attempt=1)
    assert [o["status"] for o in retry] == ["created", "created"]
    assert len(c.bills) == 2                                       # the first one was found by its tag


def test_never_creates_vendors_or_accounts():
    c = company()
    out = create_bills([bill_row(qb_vendor="Fabrikam Dental Lab"), bill_row(account="Lab Fees", invoice_no="A-3")], "r", c)
    assert [o["status"] for o in out] == ["needs a person", "needs a person"]
    assert len(c.vendors) == 2 and len(c.accounts) == 2 and not c.bills


def test_credit_or_zero_waits():
    out = create_bills([bill_row(amount=-20.0)], "r", company())
    assert out[0]["status"] == "needs a person"


def test_due_date_never_before_bill_date_and_paid_note():
    c = company()
    create_bills([bill_row(due_date="2026-01-01", is_paid=True)], "r", c)
    b = next(iter(c.bills.values()))
    assert b["DueDate"] == "2026-03-04" and "Already paid" in b["PrivateNote"]


def test_undo_deletes_unpaid_and_keeps_paid():
    c = company()
    out = create_bills([bill_row(), bill_row(invoice_no="A-2", new_name="b.pdf")], "r", c)
    c.bills[out[1]["bill_id"]]["Balance"] = 0.0                   # paid since
    undone = undo_bills(out, c)
    assert [u["status"] for u in undone] == ["deleted", "kept"]
    assert list(c.bills) == [out[1]["bill_id"]]
