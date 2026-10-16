"""The review step: a local web page where a person ticks the rows to file.

Nothing moves and nothing reaches QuickBooks until someone presses Go. The page is served from
127.0.0.1 on a random port and only for the browser on this machine:

- every request must name this server as its Host (a page on another site cannot use DNS
  rebinding to read the proposal);
- every POST must carry a token baked into the page when it was served, and an Origin, if the
  browser sends one, that is this server (another tab cannot press Go);
- file names and vendor text are written into the page with textContent, never innerHTML, so a
  file named like a script stays a file name.
"""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .models import Row

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Review bills</title>
<style>
 body{font:16px system-ui,sans-serif;margin:0;background:#f7f7f5;color:#1d1d1b}
 header,main{padding:16px 24px} h1{font-size:20px;margin:0}
 table{border-collapse:collapse;width:100%;background:#fff}
 th,td{padding:8px 10px;border-bottom:1px solid #e6e6e1;text-align:left;vertical-align:top}
 th{font-size:13px;color:#6a6a64} tr.wait td{background:#fff8ea}
 input[type=text]{width:100%;font:inherit;padding:4px 6px;box-sizing:border-box}
 .bar{position:sticky;bottom:0;background:#fff;border-top:1px solid #e6e6e1;padding:12px 24px;display:flex;gap:12px}
 button{font:inherit;padding:8px 18px;cursor:pointer}
</style></head><body>
<header><h1>Review bills</h1><p id="summary"></p></header>
<main><table><thead><tr><th></th><th>File</th><th>New name</th><th>Folder</th><th>Date</th><th>Amount</th><th>Note</th></tr></thead>
<tbody id="rows"></tbody></table></main>
<div class="bar"><button id="go">Go</button><button id="cancel">Cancel</button><span id="count"></span></div>
<datalist id="folders"></datalist>
<script>
const TOKEN = "__TOKEN__";
const ROWS = __ROWS__;
const FOLDERS = __FOLDERS__;
const tbody = document.getElementById("rows");
const dl = document.getElementById("folders");
FOLDERS.forEach(f => { const o = document.createElement("option"); o.value = f; dl.appendChild(o); });
function cell(tr, text) { const td = document.createElement("td"); td.textContent = text == null ? "" : text; tr.appendChild(td); return td; }
function input(tr, value, list) {
  const td = document.createElement("td"); const i = document.createElement("input");
  i.type = "text"; i.value = value || ""; if (list) i.setAttribute("list", list); td.appendChild(i); tr.appendChild(td); return i;
}
const inputs = ROWS.map((r, i) => {
  const tr = document.createElement("tr"); if (r.status !== "ready") tr.className = "wait";
  const tdc = document.createElement("td"); const cb = document.createElement("input");
  cb.type = "checkbox"; cb.checked = r.approved; tdc.appendChild(cb); tr.appendChild(tdc);
  cell(tr, r.source_name);
  const name = input(tr, r.new_name);
  const folder = input(tr, r.folder, "folders");
  cell(tr, r.doc_date);
  const amount = input(tr, r.amount == null ? "" : Number(r.amount).toFixed(2));
  cell(tr, r.reason || r.notes);
  tbody.appendChild(tr);
  return {cb, name, folder, amount};
});
const ready = ROWS.filter(r => r.status === "ready").length;
document.getElementById("summary").textContent =
  ROWS.length + " files. " + ready + " ready, " + (ROWS.length - ready) + " waiting for you.";
function count() { document.getElementById("count").textContent = inputs.filter(x => x.cb.checked).length + " will be filed"; }
document.addEventListener("change", count); count();
async function send(decision) {
  const edits = inputs.map((x, i) => ({i, approved: x.cb.checked, new_name: x.name.value, folder: x.folder.value, amount: x.amount.value}));
  await fetch("/decide", {method: "POST", headers: {"content-type": "application/json", "x-review-token": TOKEN},
                          body: JSON.stringify({decision, edits})});
  document.body.textContent = decision === "go" ? "Filing now. You can close this tab." : "Nothing moved. You can close this tab.";
}
document.getElementById("go").onclick = () => send("go");
document.getElementById("cancel").onclick = () => send("cancel");
</script></body></html>"""


def _script_json(obj) -> str:
    # JSON inside a <script> block: "</" would end the block early, so it is escaped
    return json.dumps(obj).replace("</", "<\\/")


class ReviewServer:
    def __init__(self, rows: list[Row], folders: list[str]):
        self.token = secrets.token_urlsafe(24)
        self.html = (PAGE.replace("__TOKEN__", self.token)
                     .replace("__ROWS__", _script_json([r.to_dict() for r in rows]))
                     .replace("__FOLDERS__", _script_json(sorted(set(folders))))).encode()
        self.decision: dict = {}
        self._done = threading.Event()
        self._srv: ThreadingHTTPServer | None = None
        self.url = ""

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the console clean
                pass

            def _host_ok(self) -> bool:
                return (self.headers.get("host") or "").lower() == outer.host

            def _refuse(self):
                self.send_response(403)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_GET(self):
                if not self._host_ok() or self.path not in ("/", "/index.html"):
                    return self._refuse()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(outer.html)))
                self.send_header("cache-control", "no-store")
                self.end_headers()
                self.wfile.write(outer.html)

            def do_POST(self):
                origin = (self.headers.get("origin") or "").lower()
                ok = (self._host_ok() and self.path == "/decide"
                      and (not origin or origin == f"http://{outer.host}")
                      and secrets.compare_digest(self.headers.get("x-review-token") or "", outer.token))
                if not ok:
                    return self._refuse()
                n = min(int(self.headers.get("content-length") or 0), 1_000_000)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._refuse()
                if body.get("decision") not in ("go", "cancel") or outer._done.is_set():
                    return self._refuse()
                outer.decision = body
                self.send_response(204)
                self.end_headers()
                outer._done.set()

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self._srv.server_address[1]}"
        self.url = f"http://{self.host}/"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self.url

    def wait(self, timeout: float | None = None) -> dict:
        self._done.wait(timeout)
        return self.decision or {"decision": "cancel", "edits": []}

    def close(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()


def review_in_browser(rows: list[Row], folders: list[str], open_browser: bool = True,
                      on_url=None) -> dict:
    srv = ReviewServer(rows, folders)
    url = srv.start()
    if on_url:
        on_url(url)
    if open_browser:
        webbrowser.open(url)
    print(f"Review page: {url}")
    try:
        return srv.wait()
    finally:
        srv.close()


def approve_ready(rows: list[Row], folders: list[str]) -> dict:
    """Headless review for scripted runs and the demo: file exactly the rows the rules marked
    ready, touch nothing else."""
    return {"decision": "go", "edits": [{"i": i, "approved": r.approved} for i, r in enumerate(rows)]}
