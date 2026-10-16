import json
import urllib.error
import urllib.request

import pytest

from bill_intake.models import Row
from bill_intake.review import ReviewServer, approve_ready


def rows():
    r1 = Row(source_name="<img src=x onerror=alert(1)>.pdf", source_path="/in/a.pdf", new_name="n.pdf", folder="V")
    r2 = Row(source_name="b.pdf", source_path="/in/b.pdf").needs_person("date not clear on the page")
    return [r1, r2]


def post(url, body, headers):
    req = urllib.request.Request(url + "decide", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    return urllib.request.urlopen(req, timeout=5).status


@pytest.fixture
def server():
    s = ReviewServer(rows(), ["V", "W"])
    s.start()
    yield s
    s.close()


def test_page_carries_rows_as_data_never_as_markup(server):
    html = urllib.request.urlopen(server.url, timeout=5).read().decode()
    assert "innerHTML" not in html
    assert "<img src=x" not in html.split("const ROWS =")[0]     # only inside the JSON data
    assert server.token in html


def test_post_without_the_token_is_refused(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(server.url, {"decision": "go", "edits": []}, {})
    assert e.value.code == 403
    assert not server._done.is_set()


def test_post_from_another_origin_is_refused(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(server.url, {"decision": "go"}, {"x-review-token": server.token, "origin": "http://evil.example.com"})
    assert e.value.code == 403


def test_wrong_host_header_is_refused(server):
    req = urllib.request.Request(server.url, headers={"host": "attacker.example.com"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403


def test_decision_is_taken_once(server):
    assert post(server.url, {"decision": "go", "edits": [{"i": 0, "approved": True}]},
                {"x-review-token": server.token}) == 204
    assert server.wait(1)["decision"] == "go"
    with pytest.raises(urllib.error.HTTPError):
        post(server.url, {"decision": "cancel"}, {"x-review-token": server.token})


def test_approve_ready_touches_only_ready_rows():
    d = approve_ready(rows(), [])
    assert d["decision"] == "go" and [e["approved"] for e in d["edits"]] == [True, False]
