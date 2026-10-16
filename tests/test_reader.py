import threading
import time
from pathlib import Path

import pytest

from bill_intake.reader import (SYSTEM, BedrockReader, FakeReader, ReaderUnavailable, parse_json,
                                read_all, validate)
from bill_intake.text import Document


# ------------------------------------------------------------- validation
def test_validate_cleans_amounts_and_numbers():
    r = validate({"vendor": " Acme ", "doc_type": "invoice", "doc_date": "2026-03-04",
                  "date_confidence": "labelled", "amount": "$1,265.00", "invoice_no": 10442})
    assert r.vendor == "Acme"
    assert r.amount == 1265.0
    assert r.invoice_no == "10442"           # a JSON number is text from here on
    assert r.problems == []


def test_validate_throws_out_what_does_not_fit():
    r = validate({"doc_type": "receipt", "doc_date": "03/04/2026", "amount": "about forty",
                  "date_confidence": "sure"})
    assert r.doc_type is None
    assert r.doc_date is None
    assert r.amount is None
    assert r.date_confidence == "none"
    assert len(r.problems) == 3


def test_flags_err_toward_caution():
    # "paid" lets a zero-amount bill through, so only a real true counts
    assert validate({"is_paid": "yes"}).is_paid is False
    # "patient information" and "not a bill" make a row more careful, so anything but a clear no counts
    r = validate({"personal_data": "yes", "not_a_bill_document": "True"})
    assert r.personal_data and r.not_a_bill_document
    r = validate({"personal_data": "false", "not_a_bill_document": None})
    assert not r.personal_data and not r.not_a_bill_document


def test_model_notes_are_dropped_on_a_page_with_patient_information():
    r = validate({"personal_data": True, "notes": "for patient A. Sample"})
    assert r.notes == ""


def test_parse_json_ignores_prose_around_the_object():
    assert parse_json('Sure.\n{"vendor": "Acme"}\nDone.') == {"vendor": "Acme"}
    with pytest.raises(ValueError):
        parse_json("no json here")


# ------------------------------------------------------------- Bedrock
class StubBedrock:
    def __init__(self, reply='{"vendor": "Acme", "doc_type": "invoice"}', error=None):
        self.reply, self.error, self.requests = reply, error, []

    def converse(self, **kw):
        self.requests.append(kw)
        if self.error:
            raise self.error
        return {"output": {"message": {"content": [{"text": self.reply}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5}}


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def test_bedrock_request_shape():
    stub = StubBedrock()
    raw, usage = BedrockReader("model-x", client=stub).read(Document(Path("a.pdf"), "Vendor: Acme", True))
    req = stub.requests[0]
    assert req["modelId"] == "model-x"
    assert req["inferenceConfig"]["temperature"] == 0
    assert req["system"][0]["text"] == SYSTEM
    assert "Vendor: Acme" in req["messages"][0]["content"][0]["text"]
    assert raw["vendor"] == "Acme" and usage.input_tokens == 10


def test_bedrock_sends_images_when_a_renderer_supplied_them():
    stub = StubBedrock()
    BedrockReader("m", client=stub).read(Document(Path("a.pdf"), "", False, 1, images=[b"png"]))
    assert "image" in stub.requests[0]["messages"][0]["content"][0]


def test_bedrock_access_denied_is_fatal_throttling_is_not():
    with pytest.raises(ReaderUnavailable):
        BedrockReader("m", client=StubBedrock(error=ClientError("AccessDeniedException"))).read(
            Document(Path("a.pdf"), "x", True))
    with pytest.raises(ClientError):
        BedrockReader("m", client=StubBedrock(error=ClientError("ThrottlingException"))).read(
            Document(Path("a.pdf"), "x", True))


# ------------------------------------------------------------- the fake
def test_fake_reader_reads_labelled_lines():
    raw, _ = FakeReader().read(Document(Path("x.txt"), "Vendor: Acme\nInvoice Date: 2026-03-04\n"
                                                      "Invoice No: A-1\nAmount Due: $1,265.00\n", True))
    r = validate(raw)
    assert (r.vendor, r.doc_date, r.invoice_no, r.amount) == ("Acme", "2026-03-04", "A-1", 1265.0)


# ------------------------------------------------------------- reading a folder
def _files(tmp_path, n):
    paths = []
    for i in range(n):
        p = tmp_path / f"{i:02d}.txt"
        p.write_text(f"Vendor: V{i}\nInvoice Date: 2026-03-0{i % 9 + 1}\n")
        paths.append(p)
    return paths


def test_read_all_overlaps_calls_and_keeps_folder_order(tmp_path):
    active, peak, lock = [0], [0], threading.Lock()

    class Slow(FakeReader):
        def read(self, doc):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return super().read(doc)

    paths = _files(tmp_path, 8)
    got = read_all(Slow(), paths, workers=4)
    assert [g.path for g in got] == paths
    assert [g.reading.vendor for g in got] == [f"V{i}" for i in range(8)]
    assert 1 < peak[0] <= 4


def test_one_bad_file_does_not_spoil_the_others(tmp_path):
    paths = _files(tmp_path, 3)
    got = read_all(FakeReader(fail={"01.txt": RuntimeError("bad reply")}), paths, workers=2)
    assert got[0].reading and got[2].reading
    assert got[1].reading is None and "could not read" in got[1].error


def test_fatal_error_stops_the_run_and_unsent_files_are_never_sent(tmp_path):
    paths = _files(tmp_path, 12)
    reader = FakeReader(fail={"00.txt": ReaderUnavailable("bad key")})
    with pytest.raises(ReaderUnavailable):
        read_all(reader, paths, workers=1)
    assert reader.calls == ["00.txt"]


def test_scan_without_text_goes_to_a_person_without_a_call(tmp_path):
    from bill_intake.samples import make_pdf
    p = tmp_path / "scan.pdf"
    p.write_bytes(make_pdf([]))
    reader = FakeReader()
    got = read_all(reader, [p])
    assert got[0].reading is None and "no text layer" in got[0].error
    assert reader.calls == []
