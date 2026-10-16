import os
from pathlib import Path

import pytest

from bill_intake import files
from bill_intake.models import Row


def test_move_never_overwrites(tmp_path):
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    src.write_text("new")
    dst.write_text("old")
    with pytest.raises(FileExistsError):
        files.move_file(src, dst)
    assert src.read_text() == "new" and dst.read_text() == "old"


def test_move_without_links_uses_exclusive_copy(tmp_path, monkeypatch):
    def no_links(a, b):
        raise OSError(1, "links not allowed")
    monkeypatch.setattr(os, "link", no_links)
    src, dst = tmp_path / "a.pdf", tmp_path / "sub" / "b.pdf"
    dst.parent.mkdir()
    src.write_text("x")
    files.move_file(src, dst)
    assert not src.exists() and dst.read_text() == "x"


def test_failed_copy_leaves_one_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "link", lambda a, b: (_ for _ in ()).throw(OSError(18, "cross-device")))
    real_unlink = os.unlink

    def locked(p, *a, **k):
        if Path(p).name == "a.pdf":
            raise PermissionError("open in another program")
        return real_unlink(p, *a, **k)
    monkeypatch.setattr(os, "unlink", locked)
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    src.write_text("x")
    with pytest.raises(PermissionError):
        files.move_file(src, dst)
    assert src.exists() and not dst.exists()


def _row(intake, dest_dir, name, ready=True, approved=True):
    src = intake / name
    src.write_text(name)
    r = Row(source_name=name, source_path=str(src), new_name=f"new-{name}", new_path=str(dest_dir / f"new-{name}"),
            folder="V", doc_date="2026-03-01", amount=1.0)
    if not ready:
        r.needs_person("date not clear on the page")
    r.approved = approved and ready
    return r


def test_file_rows_moves_sets_aside_and_logs(tmp_path):
    intake, dest = tmp_path / "in", tmp_path / "out"
    intake.mkdir(); dest.mkdir()
    rows = [_row(intake, dest, "a.txt"), _row(intake, dest, "b.txt", ready=False), _row(intake, dest, "c.txt", approved=False)]
    out = files.file_rows(rows, intake, "run-1")
    assert [o["status"] for o in out] == ["filed", "set aside", "set aside"]
    assert (dest / "new-a.txt").exists()
    assert (intake / "Not filed" / "b.txt").exists() and (intake / "Not filed" / "c.txt").exists()
    assert len(files.read_log(intake)) == 3


def test_stop_at_first_failed_move(tmp_path):
    intake, dest = tmp_path / "in", tmp_path / "out"
    intake.mkdir(); dest.mkdir()
    rows = [_row(intake, dest, n) for n in ("a.txt", "b.txt", "c.txt")]
    (dest / "new-a.txt").write_text("someone else's file")
    out = files.file_rows(rows, intake, "run-1")
    assert [o["status"] for o in out] == ["failed", "not attempted", "not attempted"]
    assert (dest / "new-a.txt").read_text() == "someone else's file"


def test_patient_page_notes_never_reach_the_log(tmp_path):
    intake, dest = tmp_path / "in", tmp_path / "out"
    intake.mkdir(); dest.mkdir()
    r = _row(intake, dest, "a.txt")
    r.personal_data, r.notes = True, "model text that should not travel"
    files.file_rows([r], intake, "run-1")
    assert "should not travel" not in files.log_path(intake).read_text()


def test_undo_last_run(tmp_path):
    intake, dest = tmp_path / "in", tmp_path / "out"
    intake.mkdir(); dest.mkdir()
    files.file_rows([_row(intake, dest, "a.txt"), _row(intake, dest, "b.txt", ready=False)], intake, "run-1")
    out = files.undo_last_run(intake)
    assert {o["status"] for o in out} == {"undone"}
    assert (intake / "a.txt").exists() and (intake / "b.txt").exists()
    assert files.undo_last_run(intake) == []          # a second undo has nothing left to do


def test_undo_reports_a_file_it_cannot_move_and_carries_on(tmp_path):
    intake, dest = tmp_path / "in", tmp_path / "out"
    intake.mkdir(); dest.mkdir()
    files.file_rows([_row(intake, dest, "a.txt"), _row(intake, dest, "b.txt")], intake, "run-1")
    (dest / "new-a.txt").unlink()                     # someone moved it by hand
    out = files.undo_last_run(intake)
    assert [o["status"] for o in out] == ["undo failed", "undone"]


def test_run_ids_are_unique():
    assert len({files.new_run_id() for _ in range(200)}) == 200
