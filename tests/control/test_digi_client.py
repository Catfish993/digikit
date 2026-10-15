"""The control protocol client (tools/control/digi_client.py) and, when a built
`digi-emud` and a firmware are present, the P1 acceptance checks of
docs/design/emulator-control.md against a real daemon.

    DIGI_EMUD=out/native/target/nogen/release/digi-emud uv run python -m pytest tests/control/test_digi_client.py

The daemon tests boot a CF-only emulator (no private DSP inputs); they skip
without DIGI_EMUD or the .syx (DIGI_EMU_SYX, else Digitone_II_OS1.11.syx).
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import tempfile
import threading

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
from tools.control import digi_client  # noqa: E402
from tools.control.digi_client import Emu, EmuError  # noqa: E402


@pytest.fixture()
def tmp_path():
    """A short directory: a socket path is limited to about 100 bytes."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="dg") as name:
        yield pathlib.Path(name)


def _serve(responder, tmp_path: pathlib.Path) -> str:
    """A one-connection fake emulator: RESPONDER(request) -> list of lines."""
    path = str(tmp_path / "fake.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)

    def run() -> None:
        conn, _ = listener.accept()
        with conn, conn.makefile("rwb", buffering=0) as stream:
            for line in stream:
                request = json.loads(line)
                for out in responder(request):
                    stream.write(json.dumps(out).encode() + b"\n")
        listener.close()

    threading.Thread(target=run, daemon=True).start()
    return path


def test_call_returns_results_and_raises_error_answers(tmp_path):
    def responder(request):
        if request["method"] == "hello":
            yield {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"schema_version": 1},
            }
        elif request["method"] == "bad":
            yield {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": 2, "message": "stale", "data": {"x": 1}},
            }
        else:
            yield {"jsonrpc": "2.0", "id": request["id"], "result": request["params"]}

    emu = Emu.connect(_serve(responder, tmp_path))
    assert emu.call("echo", a=1) == {"a": 1}
    with pytest.raises(EmuError) as raised:
        emu.call("bad")
    assert raised.value.code == digi_client.STALE_SESSION
    assert raised.value.data == {"x": 1}
    emu.close()


def test_leds_and_frame_hash_send_the_documented_params(tmp_path):
    seen = {}

    def responder(request):
        method = request["method"]
        seen[method] = request.get("params")
        if method == "hello":
            result = {"schema_version": 1}
        elif method == "leds.get":
            result = {"led_revision": 3, "leds": "00" * 64, "rgb": None}
        else:
            result = {"revision": 1, "sha256": "ab" * 32}
        yield {"jsonrpc": "2.0", "id": request["id"], "result": result}

    emu = Emu.connect(_serve(responder, tmp_path))
    assert emu.leds()["led_revision"] == 3
    assert emu.frame_hash() == "ab" * 32
    assert seen["framebuffer.get"] == {"hash": True, "data": False}
    emu.close()


def test_events_arriving_during_a_call_are_kept_for_events(tmp_path):
    def responder(request):
        if request["method"] == "hello":
            yield {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"schema_version": 1},
            }
        elif request["method"] == "events.subscribe":
            yield {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"subscribed": True},
            }
            yield {"jsonrpc": "2.0", "method": "event", "params": {"kind": "mode"}}
        else:
            yield {"jsonrpc": "2.0", "method": "event", "params": {"kind": "a"}}
            yield {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    emu = Emu.connect(_serve(responder, tmp_path))
    emu.call("anything")
    stream = emu.events(timeout=0.5)
    assert next(stream)["kind"] == "a"
    assert next(stream)["kind"] == "mode"
    emu.close()


def test_discover_prefers_the_discovery_file_and_ignores_dead_sockets(tmp_path):
    live = tmp_path / "live.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(live))
    listener.listen(1)
    (tmp_path / "current.json").write_text(json.dumps({"socket": str(live)}))
    assert digi_client.discover(tmp_path) == str(live)
    listener.close()
    live.unlink()
    (tmp_path / "current.json").write_text(json.dumps({"socket": str(live)}))
    with pytest.raises(FileNotFoundError):
        digi_client.discover(tmp_path)


def test_oled_png_is_a_valid_png_of_the_scaled_panel():
    import struct
    import zlib

    frame = bytes(1024)
    png = digi_client.oled_png(frame, scale=2)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (256, 128)
    start = png.index(b"IDAT") + 4
    length = struct.unpack(">I", png[start - 8 : start - 4])[0]
    raw = zlib.decompress(png[start : start + length])
    assert len(raw) == 128 * (1 + 256)
    # One lit pixel: top row of byte 7, column 0 is y=0 -> bit 0 of byte 7.
    lit = bytearray(1024)
    lit[7] = 1
    raw = zlib.decompress(digi_client.oled_png(bytes(lit), scale=1)[41:-12])
    assert raw[1] == 255 and raw[2] == 0


def test_describe_load_summarises_a_hot_inject():
    text = digi_client.describe_load(
        {
            "start_used": "hot-dsp",
            "main_changed": False,
            "sections_changed": [7],
            "dsp": {
                "applied": True,
                "changed_bytes": 3,
                "ranges": 1,
                "injected": {"at_frame": 12},
            },
            "seconds": 0.25,
        }
    )
    assert "hot-dsp" in text and "3 bytes in 1 range(s)" in text and "frame 12" in text


# ----- against a real daemon ---------------------------------------------------

EMUD = os.environ.get("DIGI_EMUD", "")
SYX = pathlib.Path(os.environ.get("DIGI_EMU_SYX") or ROOT / "Digitone_II_OS1.11.syx")
if not SYX.is_file():
    # A worktree's root has no firmware: the main tree's does.
    for parent in ROOT.parents:
        if (parent / "Digitone_II_OS1.11.syx").is_file():
            SYX = parent / "Digitone_II_OS1.11.syx"
            break
needs_daemon = pytest.mark.skipif(
    not (EMUD and pathlib.Path(EMUD).is_file() and SYX.is_file()),
    reason="needs DIGI_EMUD (a built digi-emud) and the DN2 .syx",
)


@pytest.fixture()
def daemon():
    with Emu.spawn(str(SYX)) as e:
        yield e


@needs_daemon
def test_step_advances_the_instruction_count_by_exactly_n(daemon):
    before = daemon.status()["cf"]["icount"]
    for n in (1, 7, 250_000, 250_001, 600_000):
        result = daemon.step(n)
        assert result["icount"] - before == n
        before = result["icount"]


@needs_daemon
def test_an_input_at_instr_is_recorded_at_exactly_that_count(daemon):
    daemon.step(1_000_000)
    at = daemon.status()["elapsed"] + 123_457
    assert daemon.button(25, True, at_instr=at)["scheduled_at"] == at
    daemon.step(300_000)
    assert [e["at_instr"] for e in daemon.recorded()["events"]] == [at]
    with pytest.raises(EmuError) as raised:
        daemon.button(25, False, at_instr=1)
    assert raised.value.code == -32602


@needs_daemon
def test_replaying_the_recorded_inputs_gives_the_same_state_digest(daemon):
    daemon.step(500_000)
    base = daemon.status()["elapsed"]
    daemon.schedule(
        [
            {"at_instr": base + 100_000, "button": 25, "down": True},
            {"at_instr": base + 400_001, "button": 25, "down": False},
            {"at_instr": base + 450_000, "turn": 1, "detents": 2},
        ]
    )
    daemon.step(1_000_000)
    first = daemon.state_digest()
    recorded = daemon.recorded()
    assert len(recorded["events"]) == 3
    daemon.restart()
    daemon.step(500_000)
    daemon.schedule(recorded["events"])
    daemon.step(1_000_000)
    assert daemon.state_digest() == first


@needs_daemon
def test_status_and_errors_before_and_after_a_load(tmp_path):
    with Emu.spawn() as bare:
        status = bare.status()
        assert status["firmware"] is None
        with pytest.raises(EmuError) as raised:
            bare.step(10)
        assert raised.value.code == digi_client.NO_FIRMWARE
        with pytest.raises(EmuError) as raised:
            bare.call("no.such.method")
        assert raised.value.code == -32601
        report = bare.load(SYX)
        assert report["start_used"] == "cold" and report["main_changed"] is False
        assert bare.status()["firmware"]["syx_sha256"] == report["syx_sha256"]
        again = bare.load(SYX)
        assert again["start_used"] == "none" and again["sections_changed"] == []


@needs_daemon
def test_quit_ends_the_daemon_and_removes_its_socket():
    e = Emu.spawn(str(SYX))
    path = e.path
    assert e.quit()["quitting"] is True
    assert e._proc is not None
    e._proc.wait(timeout=20)
    assert not os.path.exists(path)
    e.close()


# ----- firmware injection ------------------------------------------------------

PATCH = ROOT / "tools" / "patchkit" / "dsp_patches" / "dn2-1.11-master-half.patch"


@pytest.fixture(scope="module")
def patched_dsp_syx():
    """The stock .syx with the master-half DSP patch written into its DSP stream."""
    from tools.patchkit import dsp_patch

    with tempfile.TemporaryDirectory(prefix="dgp") as name:
        out = pathlib.Path(name) / "master-half.syx"
        parsed = dsp_patch.parse_for_stream(PATCH.read_text(), SYX)
        edits, changed = dsp_patch.write_syx(SYX, parsed, out)
        assert (edits, changed) == (1, 1)
        yield out


@needs_daemon
def test_a_dsp_only_change_without_audio_is_reported_and_not_applied(patched_dsp_syx):
    with Emu.spawn(str(SYX)) as emu:
        report = emu.inject(patched_dsp_syx)
        assert report["sections_changed"] == [7]
        assert report["main_changed"] is False
        assert report["start_used"] == "none"
        assert report["dsp"]["applied"] is False
        # No restart: the guest kept running from the same state.
        assert emu.status()["cf"]["icount"] == 0
        back = emu.inject(SYX)
        assert back["sections_changed"] == [7]


@needs_daemon
@pytest.mark.slow
def test_a_patched_main_cold_boots_once_then_hits_the_snapshot_cache():
    from dt2.build import rebuild
    from tools.patchkit import dsp_patch

    main = bytearray(dsp_patch.section_blob(SYX, 3))
    at = main.find(b"UNTITLED")
    assert at >= 0
    main[at + 7] = ord("B")
    with tempfile.TemporaryDirectory(prefix="dgm") as name:
        patched = pathlib.Path(name) / "main.syx"
        patched.write_bytes(rebuild(str(SYX), {3: bytes(main)}))
        with Emu.spawn(str(SYX)) as emu:
            cold = emu.inject(patched)
            assert cold["start_used"] == "cold" and cold["main_changed"] is True
            assert (
                cold["audio"] is False
                and cold["coupled_note"] == "no coupled audio inputs"
            )
            assert emu.status()["cf"]["ready"] is True
            emu.step(1_000_000)
            digest = emu.state_digest()
            stock = emu.inject(SYX)
            assert stock["start_used"] == "cold"  # CF-only: nothing to restore
            hit = emu.inject(patched)
            assert hit["start_used"] == "snapshot-cache" and hit["cache_hit"] is True
            assert emu.status()["cf"]["ready"] is True
            emu.step(1_000_000)
            assert emu.state_digest() == digest


@needs_daemon
@pytest.mark.slow
def test_hot_dsp_inject_halves_the_master_level_and_the_original_restores_it(
    patched_dsp_syx, monkeypatch
):
    """The master-half patch (-6 dB) injected into the running DSP halves the
    PCM level; injecting the original .syx gives the reference PCM back."""
    from tools.emulator import digi_env

    env = {**os.environ, "DIGI_EMU_SYX": str(SYX)}
    ready = digi_env.ready_inputs(env)
    if not all(p.is_file() for p in ready.values()):
        pytest.skip("needs the private DN2 ready inputs")
    for name, value in digi_env.fast_env(env).items():
        monkeypatch.setenv(name, value)
    args = [
        str(SYX),
        "--auto-coupled",
        "--audio-profile-syx",
        str(SYX),
        "--dsp-image",
        str(ready["DSP image"]),
        "--dsp-state",
        str(ready["DSP state"]),
        "--cf-snapshot",
        str(ready["CF snapshot"]),
        "--no-audio",
    ]
    tap = [
        {"at_instr": 0, "button": 12, "down": True},
        {"at_instr": 1_000_000, "button": 12, "down": False},
        {"at_instr": 60_000_000, "button": 25, "down": True},
        {"at_instr": 100_000_000, "button": 25, "down": False},
    ]

    def render(emu, *inject):
        emu.restart()
        for syx in inject:
            emu.inject(syx)
        emu.pcm_start()
        emu.schedule(tap)
        emu.run_until(audio_s=3, timeout_wall_s=600)
        return emu.pcm_report()

    with Emu.spawn(*args, timeout=300) as emu:
        if emu.status()["audio"] is None:
            pytest.skip("digi-emud was built without coupled audio")
        reference = render(emu)
        hot = emu.inject(patched_dsp_syx)
        assert hot["start_used"] == "hot-dsp" and hot["dsp"]["changed_bytes"] == 1
        halved = render(emu, patched_dsp_syx)
        assert halved["rms"] == pytest.approx(reference["rms"] / 2, rel=1e-3)
        assert halved["peak"] == pytest.approx(reference["peak"] / 2, rel=1e-3)
        restored = render(emu, patched_dsp_syx, SYX)
        assert restored["sha256"] == reference["sha256"]
