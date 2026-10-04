"""benchlock: readers-writer lock for builds (shared) and timed runs (exclusive)."""
# ruff: noqa: SIM117

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

TOOL = str(Path(__file__).resolve().parents[1] / "tools" / "benchlock.py")
_spec = importlib.util.spec_from_file_location("benchlock", TOOL)
assert _spec and _spec.loader
benchlock = importlib.util.module_from_spec(_spec)
sys.modules["benchlock"] = benchlock
_spec.loader.exec_module(benchlock)


@pytest.fixture
def lock(tmp_path, monkeypatch):
    path = tmp_path / ".benchlock"
    monkeypatch.setenv("BENCHLOCK_PATH", str(path))
    monkeypatch.delenv("BENCHLOCK", raising=False)
    return path


def test_shared_shared_concurrent(lock):
    with benchlock.hold("shared", "a", timeout=1):
        with benchlock.hold("shared", "b", timeout=1):
            assert len(benchlock.holders()) == 2
    assert benchlock.holders() == []


def test_exclusive_blocks_shared_and_back(lock):
    with benchlock.hold("exclusive", "bench"):
        with pytest.raises(benchlock.LockTimeout):
            with benchlock.hold("shared", "build", timeout=0.2):
                pass
        with pytest.raises(benchlock.LockTimeout):
            with benchlock.hold("exclusive", "bench2", timeout=0.2):
                pass
    with benchlock.hold("shared", "build"):
        with pytest.raises(benchlock.LockTimeout):
            with benchlock.hold("exclusive", "bench", timeout=0.2):
                pass


def test_waiter_proceeds_after_release(lock, capsys):
    got = []

    def waiter():
        with benchlock.hold("exclusive", "bench", timeout=5) as h:
            got.append(h.waited)

    with benchlock.hold("shared", "build"):
        thread = threading.Thread(target=waiter)
        thread.start()
        thread.join(0.3)
        assert thread.is_alive() and not got
    thread.join(5)
    assert got and got[0] >= 0.25
    err = capsys.readouterr().err
    assert "waiting for exclusive" in err and "build" in err
    assert "acquired exclusive" in err


def test_bad_mode(lock):
    with pytest.raises(ValueError):
        with benchlock.hold("both", "x"):
            pass


def test_stale_sidecar_cleanup(lock):
    sdir = lock.with_name(lock.name + ".d")
    sdir.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    stale = sdir / f"{proc.pid}.0.json"
    info = {"pid": proc.pid, "mode": "shared", "label": "dead", "start": 0}
    stale.write_text(json.dumps(info))
    live = sdir / f"{os.getpid()}.99.json"
    live.write_text(json.dumps({**info, "pid": os.getpid(), "label": "live"}))
    out = subprocess.run(
        [sys.executable, TOOL, "status"], capture_output=True, text=True, check=True
    ).stdout
    assert "live" in out and "dead" not in out
    assert not stale.exists() and live.exists()


def test_disabled_is_noop(lock, monkeypatch):
    monkeypatch.setenv("BENCHLOCK", "0")
    with benchlock.hold("exclusive", "a", timeout=0.1):
        with benchlock.hold("exclusive", "b", timeout=0.1):
            pass
    assert not lock.exists()


def test_cli_run_exit_code_and_blocking(lock):
    ok = subprocess.run([sys.executable, TOOL, "run", "--shared", "--", "true"])
    assert ok.returncode == 0
    bad = subprocess.run(
        [
            sys.executable,
            TOOL,
            "run",
            "--exclusive",
            "--label",
            "x",
            "--",
            "sh",
            "-c",
            "exit 7",
        ]
    )
    assert bad.returncode == 7
    with benchlock.hold("shared", "build"):
        blocked = subprocess.run(
            [
                sys.executable,
                TOOL,
                "run",
                "--exclusive",
                "--timeout",
                "0.3",
                "--",
                "true",
            ],
            capture_output=True,
            text=True,
        )
        assert blocked.returncode != 0
        assert "waiting for exclusive" in blocked.stderr


def _gate_busy(lock) -> bool:
    """True when something (an exclusive waiter) holds the gate file."""
    import fcntl

    gate = lock.with_name(lock.name + ".gate")
    if not gate.exists():
        return False
    with open(gate, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def _wait_until(predicate, seconds=5.0):
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _shared_cli(lock, extra_env=None, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(extra_env or {})
    env["BENCHLOCK_PATH"] = str(lock)
    return subprocess.run(
        [sys.executable, TOOL, "run", "--shared", "--timeout", "0.4", "--", "true"],
        env=env,
        capture_output=True,
        text=True,
    )


def test_exclusive_waiter_blocks_new_shared(lock):
    """A waiting exclusive acquirer stops new shared acquisitions (no starvation)."""
    got = []

    def writer():
        with benchlock.hold("exclusive", "bench", timeout=10):
            got.append("exclusive")

    with benchlock.hold("shared", "build-1"):
        thread = threading.Thread(target=writer)
        thread.start()
        assert _wait_until(lambda: _gate_busy(lock)), "writer never started waiting"
        # A fresh (non-nested) shared acquirer queues behind the writer.
        blocked = _shared_cli(lock, drop=(benchlock.HELD_ENV,))
        assert blocked.returncode != 0
        assert "waiting for shared" in blocked.stderr
        assert not got
    thread.join(10)
    assert got == ["exclusive"]
    # Once the writer is done, shared works again.
    assert _shared_cli(lock, drop=(benchlock.HELD_ENV,)).returncode == 0


def test_nested_shared_passes_a_waiting_writer(lock):
    """A child of a shared holder must not queue behind the writer waiting for it."""
    done = []
    before = os.environ.get(benchlock.HELD_ENV)

    def writer():
        with benchlock.hold("exclusive", "bench", timeout=10):
            done.append(True)

    with benchlock.hold("shared", "outer"):
        assert os.environ.get(benchlock.HELD_ENV) == "1"
        thread = threading.Thread(target=writer)
        thread.start()
        assert _wait_until(lambda: _gate_busy(lock))
        nested = _shared_cli(lock)  # inherits BENCHLOCK_SHARED_HELD=1
        assert nested.returncode == 0, nested.stderr
        assert not done
    thread.join(10)
    assert done
    assert os.environ.get(benchlock.HELD_ENV) == before


def test_exclusive_timeout_releases_gate(lock):
    with benchlock.hold("shared", "build"):
        with pytest.raises(benchlock.LockTimeout):
            with benchlock.hold("exclusive", "bench", timeout=0.2):
                pass
        assert not _gate_busy(lock)
