from bill_intake.single_instance import InstanceLock


def test_second_copy_is_refused_and_finds_the_first_page(tmp_path):
    first, second = InstanceLock(tmp_path / "x.lock"), InstanceLock(tmp_path / "x.lock")
    assert first.acquire()
    first.publish("http://127.0.0.1:5000/")
    assert not second.acquire()
    assert second.other_url() == "http://127.0.0.1:5000/"
    first.release()
    assert second.acquire()          # the lock goes with the process, no stale file to clear
    assert second.other_url() is None
    second.release()
