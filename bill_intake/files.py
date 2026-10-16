"""Moving files, the run log, and Undo.

The log is what makes a run reversible: one JSON line per file per outcome, with absolute paths,
appended and never rewritten. Undo reads it back and moves the last run's files home.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from .models import READY, Row

LOG_NAME = "filing-log.jsonl"
SET_ASIDE = "Not filed"


def move_file(src: Path, dst: Path) -> None:
    """Move one file. Never overwrites, and never leaves two copies.

    `os.rename` replaces an existing destination without a word on macOS and Linux, so it is not
    used for the move itself. A hard link fails if the destination exists, atomically, so link
    then unlink is a no-clobber move on one drive. Across drives (or where links are not allowed)
    the copy goes to a file opened with exclusive create, and a half-made copy is removed if
    anything fails."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        raise FileExistsError(f"{dst.name} already exists")
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise
    else:
        try:
            os.unlink(src)
        except OSError:
            os.unlink(dst)          # the original is locked: undo the link, keep one copy
            raise
        return
    made = False
    try:
        with open(src, "rb") as fin, open(dst, "xb") as fout:
            made = True
            shutil.copyfileobj(fin, fout)
        shutil.copystat(src, dst)
        os.unlink(src)
    except BaseException:
        if made and src.exists() and dst.exists():
            dst.unlink()
        raise


def new_run_id() -> str:
    """Seconds plus a random tail: two runs in the same second never share an id, and both Undo
    and the QuickBooks request ids key on it."""
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)


def log_path(intake: Path) -> Path:
    return Path(intake) / LOG_NAME


def _append(intake: Path, records: list[dict]) -> None:
    with log_path(intake).open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")


def read_log(intake: Path) -> list[dict]:
    p = log_path(intake)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def set_aside(src: Path, intake: Path) -> Path | None:
    """Move a file the person did not approve into intake/Not filed, so the next run starts with
    only new files. Returns where it went, or None if it was not in the intake folder."""
    src = Path(src)
    if not src.exists() or src.parent.resolve() != Path(intake).resolve():
        return None
    folder = Path(intake) / SET_ASIDE
    folder.mkdir(exist_ok=True)
    dest, n = folder / src.name, 2
    while dest.exists():
        dest = folder / f"{src.stem} ({n}){src.suffix}"
        n += 1
    move_file(src, dest)
    return dest


def file_rows(rows: list[Row], intake: Path, run_id: str) -> list[dict]:
    """Move approved, ready rows; set the rest aside. Stop moving at the first failure: a folder
    that cannot be written to will fail the same way for every file after it."""
    out: list[dict] = []
    stopped = False
    for r in rows:
        rec = {"run_id": run_id, "at": datetime.now().isoformat(timespec="seconds"),
               "source_name": r.source_name, "source_path": r.source_path,
               "new_name": r.new_name or "", "new_path": r.new_path or "",
               "vendor": r.folder or "", "doc_type": r.doc_type, "doc_date": r.doc_date or "",
               "amount": r.amount, "invoice_no": r.invoice_no or "", "status": "",
               # our own reason stays; the model's free text never goes in the log for a page
               # with patient information on it
               "notes": "; ".join(x for x in (r.reason, "" if r.personal_data else r.notes) if x)}
        if r.status != READY or not r.approved:
            rec.update(status="set aside", new_name="", new_path="")
            try:
                dest = set_aside(Path(r.source_path), intake)
                if dest:
                    rec.update(new_name=dest.name, new_path=str(dest))
            except OSError as e:
                rec.update(status="left in intake", notes=f"{rec['notes']}; could not move ({type(e).__name__})")
        elif stopped:
            rec.update(status="not attempted", notes="an earlier move failed")
        else:
            try:
                Path(r.new_path).parent.mkdir(exist_ok=True)   # the vendor folder; never the year folder
                move_file(Path(r.source_path), Path(r.new_path))
                rec["status"] = "filed"
            except Exception as e:  # noqa: BLE001
                rec.update(status="failed", notes=f"move failed: {e}")
                stopped = True
        out.append(rec)
    _append(intake, out)
    return out


def log_bills(intake: Path, run_id: str, outcomes: list[dict]) -> None:
    """Record what QuickBooks did with each bill, so Undo can find the bills a run created."""
    _append(intake, [dict(o, run_id=run_id, kind="bill") for o in outcomes])


def bills_of_last_run(intake: Path) -> list[dict]:
    log = read_log(intake)
    bills = [r for r in log if r.get("kind") == "bill"]
    return [b for b in bills if b["run_id"] == bills[-1]["run_id"]] if bills else []


def undo_last_run(intake: Path) -> list[dict]:
    """Move the last run's files back to the intake folder. A file that cannot go back is reported
    and the rest still go; every outcome is logged, so a second Undo picks up where this stopped."""
    log = read_log(intake)
    moved = [r for r in log if r.get("kind") != "bill" and r["status"] in ("filed", "set aside") and r["new_path"]]
    if not moved:
        return []
    run_id = moved[-1]["run_id"]
    done = {r["new_path"] for r in log if r["run_id"] == run_id and r["status"] == "undone"}
    out = []
    for r in moved:
        if r["run_id"] != run_id or r["new_path"] in done:
            continue
        rec = dict(r, at=datetime.now().isoformat(timespec="seconds"))
        src, dst = Path(r["new_path"]), Path(r["source_path"])
        try:
            move_file(src, dst)
            rec.update(status="undone", notes="moved back to intake")
        except FileNotFoundError:
            rec.update(status="undo failed", notes=f"no longer at {src}")
        except FileExistsError:
            rec.update(status="undo failed", notes=f"{dst.name} is already back in intake")
        except OSError as e:
            rec.update(status="undo failed", notes=f"could not be moved back ({type(e).__name__})")
        out.append(rec)
    _append(intake, out)
    return out
