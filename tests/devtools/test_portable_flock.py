"""tools/devtools/portable_flock.py: flock's shared and exclusive locks, on every host."""

from __future__ import annotations

import pytest

from tools.devtools import portable_flock as lock


def test_shared_holders_coexist_and_exclude_an_exclusive_one(tmp_path):
    path = tmp_path / "lock"
    with open(path, "a+") as a, open(path, "a+") as b, open(path, "a+") as c:
        lock.flock(a, lock.LOCK_SH)
        lock.flock(b, lock.LOCK_SH | lock.LOCK_NB)  # a second shared holder: granted
        with pytest.raises(BlockingIOError):
            lock.flock(c, lock.LOCK_EX | lock.LOCK_NB)
        lock.flock(a, lock.LOCK_UN)
        lock.flock(b, lock.LOCK_UN)
        lock.flock(c, lock.LOCK_EX | lock.LOCK_NB)  # nobody holds it now
        with pytest.raises(BlockingIOError):
            lock.flock(a, lock.LOCK_SH | lock.LOCK_NB)
        lock.flock(c, lock.LOCK_UN)


def test_unlocking_an_unlocked_file_is_not_an_error(tmp_path):
    with open(tmp_path / "lock", "a+") as handle:
        lock.flock(handle, lock.LOCK_UN)


def test_closing_the_handle_releases_the_lock(tmp_path):
    path = tmp_path / "lock"
    with open(path, "a+") as a:
        lock.flock(a, lock.LOCK_EX)
    with open(path, "a+") as b:
        lock.flock(b, lock.LOCK_EX | lock.LOCK_NB)
        lock.flock(b, lock.LOCK_UN)
