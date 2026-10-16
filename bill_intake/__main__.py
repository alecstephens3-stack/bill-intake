"""Command line.

    python3 -m bill_intake demo                      Example Clinic in a temp folder, fake reader, fake QuickBooks
    python3 -m bill_intake run --rules FILE          read, review in the browser, file, enter bills
    python3 -m bill_intake connect --redirect-uri U  connect QuickBooks once (an admin approves in the browser)
    python3 -m bill_intake undo --rules FILE         move the last run's files back
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import webbrowser
from pathlib import Path

from . import files, rules as rules_mod
from .pipeline import RunResult, run
from .quickbooks import FakeQuickBooks, FileTokenStore, QuickBooksClient, undo_bills
from .reader import BedrockReader, FakeReader
from .review import approve_ready, review_in_browser
from .samples import QB_ACCOUNTS, QB_VENDORS, build_example_clinic
from .single_instance import InstanceLock


def print_result(res: RunResult) -> None:
    print(f"\nRun {res.run_id}: {res.decision}\n")
    for r in res.rows:
        mark = "x" if r.approved else " "
        print(f" [{mark}] {r.source_name:<24} -> {r.new_name or '-':<44} {r.reason}")
    if res.bills:
        print("\nQuickBooks:")
        for b in res.bills:
            amount = f"${b['amount']:,.2f}" if b["amount"] is not None else "-"
            print(f"  {b['status']:<15} {b['vendor']:<26} {b['no']:<22} {amount:>10}  {b['why']}")


def cmd_demo(args) -> int:
    root = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="example-clinic-"))
    rules = rules_mod.load(build_example_clinic(root))
    qb = FakeQuickBooks(QB_VENDORS, QB_ACCOUNTS)
    res = run(rules, FakeReader(), approve_ready, quickbooks=qb)
    print_result(res)
    print(f"\nExample Clinic is in {root}")
    return 0


def _quickbooks(args):
    if args.quickbooks == "none":
        return None
    need = ["QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_REALM_ID"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit(f"QuickBooks needs {', '.join(missing)} in the environment.")
    return QuickBooksClient(os.environ["QBO_CLIENT_ID"], os.environ["QBO_CLIENT_SECRET"], os.environ["QBO_REALM_ID"],
                            FileTokenStore(Path(args.qb_token_file).expanduser()), env=args.qb_env)


def cmd_run(args) -> int:
    rules = rules_mod.load(Path(args.rules))
    lock = InstanceLock(rules.intake / ".bill-intake.lock")
    if not lock.acquire():
        url = lock.other_url()
        print("Already running." + (f" Opening its page: {url}" if url else " Wait for it to finish."))
        if url:
            webbrowser.open(url)
        return 1
    with lock:
        reader = FakeReader() if args.reader == "fake" else BedrockReader(args.model, args.region)
        if args.review == "browser":
            def review(rows, folders):
                return review_in_browser(rows, folders, on_url=lock.publish)
        else:
            review = approve_ready
        res = run(rules, reader, review, quickbooks=_quickbooks(args), workers=args.workers)
        print_result(res)
    return 0


def cmd_connect(args) -> int:
    """One time, by a QuickBooks admin: approve the app in the browser, then paste back the code
    and company id from the page Intuit redirects to."""
    import secrets
    cid = os.environ.get("QBO_CLIENT_ID")
    if not cid or not os.environ.get("QBO_CLIENT_SECRET"):
        sys.exit("Set QBO_CLIENT_ID and QBO_CLIENT_SECRET first.")
    from urllib.parse import parse_qs, urlparse
    state = secrets.token_urlsafe(16)
    webbrowser.open(QuickBooksClient.authorization_url(cid, args.redirect_uri, state))
    back = parse_qs(urlparse(input("Paste the whole address the browser landed on: ").strip()).query)
    if not secrets.compare_digest(back.get("state", [""])[0], state):
        sys.exit("That address is from another attempt. Run connect again.")
    code, realm = back.get("code", [""])[0], back.get("realmId", [""])[0]
    if not code or not realm:
        sys.exit("That address has no code or realmId. Run connect again.")
    client = QuickBooksClient(cid, os.environ["QBO_CLIENT_SECRET"], realm,
                              FileTokenStore(Path(args.qb_token_file).expanduser()), env=args.qb_env)
    client.exchange_code(code, args.redirect_uri)
    print(f"Connected. Set QBO_REALM_ID={realm} for runs.")
    return 0


def cmd_undo(args) -> int:
    rules = rules_mod.load(Path(args.rules))
    qbo = _quickbooks(args)
    if qbo is not None:
        for b in undo_bills(files.bills_of_last_run(rules.intake), qbo):
            print(f"bill {b['status']:<12} {b['vendor']:<26} {b['no']:<22} {b.get('why', '')}")
    out = files.undo_last_run(rules.intake)
    for r in out:
        print(f"{r['status']:<12} {r['new_name']:<44} {r.get('notes', '')}")
    if not out:
        print("No files to move back.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bill_intake")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--keep", help="build Example Clinic here instead of a temp folder")
    r = sub.add_parser("run")
    r.add_argument("--rules", required=True)
    r.add_argument("--reader", choices=["bedrock", "fake"], default="bedrock")
    r.add_argument("--model", help="Bedrock model or inference profile id")
    r.add_argument("--region", default="us-east-1")
    r.add_argument("--review", choices=["browser", "approve-ready"], default="browser")
    r.add_argument("--quickbooks", choices=["live", "none"], default="none")
    r.add_argument("--qb-env", choices=["sandbox", "production"], default="sandbox")
    r.add_argument("--qb-token-file", default="~/.bill-intake/qbo-token.json")
    r.add_argument("--workers", type=int, default=4)
    c = sub.add_parser("connect")
    c.add_argument("--redirect-uri", required=True, help="the redirect URI registered for your Intuit app")
    c.add_argument("--qb-env", choices=["sandbox", "production"], default="sandbox")
    c.add_argument("--qb-token-file", default="~/.bill-intake/qbo-token.json")
    u = sub.add_parser("undo")
    u.add_argument("--rules", required=True)
    u.add_argument("--quickbooks", choices=["live", "none"], default="none",
                   help="live also deletes the bills the last run created, unless one has been paid")
    u.add_argument("--qb-env", choices=["sandbox", "production"], default="sandbox")
    u.add_argument("--qb-token-file", default="~/.bill-intake/qbo-token.json")
    args = ap.parse_args(argv)
    if args.cmd == "run" and args.reader == "bedrock" and not args.model:
        ap.error("--model is required with --reader bedrock")
    return {"demo": cmd_demo, "run": cmd_run, "connect": cmd_connect, "undo": cmd_undo}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
