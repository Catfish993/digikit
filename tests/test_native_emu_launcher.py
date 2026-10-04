"""The launcher restores local DN2 audio by default, with CF-only escape hatches."""

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def launcher():
    return runpy.run_path(str(ROOT / "tools/native_emu.sh"), run_name="launcher_test")


def test_default_command_preserves_locked_cf_only_build():
    command = launcher()["cargo_command"](["example.syx", "--card-image", "card.img"])
    assert "--locked" in command
    assert "--features" not in command
    assert command[command.index("--") + 1 :] == [
        "example.syx",
        "--card-image",
        "card.img",
    ]


def test_default_ready_inputs_select_live_coupled_audio(monkeypatch, tmp_path):
    scope = launcher()
    globals_ = scope["default_audio_args"].__globals__
    ready = tmp_path / "snapshots" / "dn2-audio-ready-2026-10-03"
    ready.mkdir(parents=True)
    for name in [
        "Digitone_II_OS1.11.syx",
        "digi-audio-dn2-image.bin",
        "digi-audio-m5.snap.dsp",
        "digi-audio-m5.snap",
    ]:
        (tmp_path / name if name.endswith(".syx") else ready / name).touch()
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    monkeypatch.setitem(globals_, "READY", ready)
    monkeypatch.setitem(globals_, "DEFAULT_GENERATED", generated)
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    args = scope["default_audio_args"]()
    assert args[1] == "--auto-coupled"
    assert args[-2:] == ["--audio-buffer", "0"]
    assert Path(__import__("os").environ["SHARC_GEN_DIR"]) == generated


def test_default_no_audio_keeps_coupling_but_mutes_playback(monkeypatch, tmp_path):
    scope = launcher()
    globals_ = scope["default_audio_args"].__globals__
    ready = tmp_path / "ready"
    ready.mkdir()
    for name in [
        "digi-audio-dn2-image.bin",
        "digi-audio-m5.snap.dsp",
        "digi-audio-m5.snap",
    ]:
        (ready / name).touch()
    (tmp_path / "Digitone_II_OS1.11.syx").touch()
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    monkeypatch.setitem(globals_, "READY", ready)
    monkeypatch.setitem(globals_, "DEFAULT_GENERATED", generated)
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    assert scope["default_audio_args"](["--no-audio"])[-1] == "0"


def test_selected_firmware_keeps_its_path_but_profile_stays_known(
    monkeypatch, tmp_path
):
    scope = launcher()
    globals_ = scope["default_audio_args"].__globals__
    ready = tmp_path / "ready"
    ready.mkdir()
    for name in [
        "digi-audio-dn2-image.bin",
        "digi-audio-m5.snap.dsp",
        "digi-audio-m5.snap",
    ]:
        (ready / name).touch()
    profile = tmp_path / "Digitone_II_OS1.11.syx"
    profile.touch()
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    monkeypatch.setitem(globals_, "READY", ready)
    monkeypatch.setitem(globals_, "DEFAULT_GENERATED", generated)
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    args = scope["default_audio_args"](["another.syx", "--card-image", "card.img"])
    assert args[:3] == ["another.syx", "--card-image", "card.img"]
    assert args[args.index("--audio-profile-syx") + 1] == str(profile)


def test_normal_launcher_arguments_get_audio_without_replacing_firmware(
    monkeypatch, tmp_path
):
    scope = launcher()
    globals_ = scope["default_audio_args"].__globals__
    ready = tmp_path / "ready"
    ready.mkdir()
    for name in [
        "digi-audio-dn2-image.bin",
        "digi-audio-m5.snap.dsp",
        "digi-audio-m5.snap",
    ]:
        (ready / name).touch()
    profile = tmp_path / "Digitone_II_OS1.11.syx"
    profile.touch()
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    monkeypatch.setitem(globals_, "READY", ready)
    monkeypatch.setitem(globals_, "DEFAULT_GENERATED", generated)
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    monkeypatch.setenv("DIGI_EMU_PGO", "0")
    calls = []
    monkeypatch.setattr(
        "subprocess.run", lambda command, **_kwargs: calls.append(command)
    )
    scope["main"](["selected.syx", "--card-image", "card.img"])
    command = calls[-1]
    forwarded = command[command.index("--") + 1 :]
    assert forwarded[:3] == ["selected.syx", "--card-image", "card.img"]
    assert "--auto-coupled" in forwarded
    assert forwarded[forwarded.index("--audio-profile-syx") + 1] == str(profile)


def test_coupled_command_selects_local_feature_and_preserves_arguments():
    args = ["example.syx", "--coupled", "--dsp-image", "image.bin"]
    command = launcher()["cargo_command"](args)
    assert command[command.index("--features") + 1] == "coupled-audio"
    assert command[command.index("--") + 1 :] == args


def test_invalid_configured_core_fails_before_build(monkeypatch):
    monkeypatch.setenv("SHARC_GEN_DIR", "/missing-generated-core")
    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit, match="SHARC_GEN_DIR"):
        launcher()["main"](["example.syx", "--coupled"])
    assert calls == []


def _known_syx(tmp_path, scope, monkeypatch, content=b"dn2"):
    syx = tmp_path / "fw.syx"
    syx.write_bytes(content)
    table = scope["fast_regions_for"].__globals__["FAST_REGIONS"]
    monkeypatch.setitem(
        table, __import__("hashlib").sha256(content).hexdigest(), "0x10,0x20"
    )
    return syx


def test_fast_regions_follow_the_firmware_hash(monkeypatch, tmp_path):
    scope = launcher()
    syx = _known_syx(tmp_path, scope, monkeypatch)
    other = tmp_path / "other.syx"
    other.write_bytes(b"other")
    assert scope["fast_regions_for"](syx) == "0x10,0x20"
    assert scope["fast_regions_for"](other) == ""
    assert scope["fast_regions_for"](tmp_path / "missing.syx") == ""


def test_known_dn2_1_11_hash_has_the_verified_regions():
    table = launcher()["FAST_REGIONS"]
    assert (
        table["2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0"]
        == "0x1c399a,0x1c3862,0x1c364f"
    )


def test_set_fast_regions_enables_known_firmware(monkeypatch, tmp_path):
    scope = launcher()
    syx = _known_syx(tmp_path, scope, monkeypatch)
    environ: dict[str, str] = {}
    scope["set_fast_regions"](["--audio-profile-syx", str(syx)], environ)
    assert environ["SHARC_FAST_REGIONS"] == "0x10,0x20"


def test_set_fast_regions_respects_user_choices(monkeypatch, tmp_path):
    scope = launcher()
    syx = _known_syx(tmp_path, scope, monkeypatch)
    args = ["--audio-profile-syx", str(syx)]
    user = {"SHARC_FAST_REGIONS": "0x99"}
    scope["set_fast_regions"](args, user)
    assert user == {"SHARC_FAST_REGIONS": "0x99"}
    off = {"DIGI_EMU_FAST": "0"}
    scope["set_fast_regions"](args, off)
    assert "SHARC_FAST_REGIONS" not in off
    empty = {"SHARC_FAST_REGIONS": ""}
    scope["set_fast_regions"](args, empty)
    assert empty["SHARC_FAST_REGIONS"] == ""


def test_set_fast_regions_leaves_unknown_firmware_off(tmp_path):
    scope = launcher()
    syx = tmp_path / "unknown.syx"
    syx.write_bytes(b"unknown")
    environ: dict[str, str] = {}
    scope["set_fast_regions"](["--audio-profile-syx", str(syx)], environ)
    assert "SHARC_FAST_REGIONS" not in environ


def test_main_sets_fast_regions_for_the_coupled_run(monkeypatch, tmp_path):
    scope = launcher()
    syx = _known_syx(tmp_path, scope, monkeypatch)
    generated = tmp_path / "gen"
    generated.mkdir()
    monkeypatch.setenv("SHARC_GEN_DIR", str(generated))
    monkeypatch.setenv("DIGI_EMU_PGO", "0")
    monkeypatch.delenv("SHARC_FAST_REGIONS", raising=False)
    monkeypatch.delenv("DIGI_EMU_FAST", raising=False)
    calls = []
    monkeypatch.setattr("subprocess.run", lambda command, **kw: calls.append(command))
    scope["main"](["x.syx", "--coupled", "--audio-profile-syx", str(syx)])
    assert __import__("os").environ["SHARC_FAST_REGIONS"] == "0x10,0x20"
    monkeypatch.delenv("SHARC_FAST_REGIONS")


def test_cargo_command_uses_the_gen_keyed_target_dir(monkeypatch, tmp_path):
    scope = launcher()
    plain = scope["cargo_command"](["example.syx"])
    assert plain[plain.index("--target-dir") + 1].endswith("out/native/target/nogen")
    gen = tmp_path / "gen"
    gen.mkdir()
    monkeypatch.setenv("SHARC_GEN_DIR", str(gen))
    coupled = scope["cargo_command"](["example.syx", "--coupled"])
    target = coupled[coupled.index("--target-dir") + 1]
    assert target.endswith("out/native/target/generic")
    assert coupled.index("--target-dir") < coupled.index("--")


# --- PGO decisions --------------------------------------------------------------

COUPLED = ["x.syx", "--coupled"]


@pytest.fixture
def pgo(monkeypatch):
    """The real native_pgo module with its state, blocker and subprocess mocked."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import native_pgo
    finally:
        sys.path.pop(0)
    for name in ("DIGI_EMU_PGO", "DIGI_EMU_PGO_TRAIN"):
        monkeypatch.delenv(name, raising=False)
    box = type("Box", (), {})()
    box.states = [("stale", "changed: sources")]  # consumed one per pgo_state call
    box.blocker = ""
    box.train_exit = 0
    box.commands = []

    def pgo_state(_environ=None):
        return box.states.pop(0) if len(box.states) > 1 else box.states[0]

    def run(command, **_kwargs):
        box.commands.append(command)
        return subprocess.CompletedProcess(command, box.train_exit)

    monkeypatch.setattr(native_pgo, "pgo_state", pgo_state)
    monkeypatch.setattr(native_pgo, "training_blocker", lambda _env=None: box.blocker)
    monkeypatch.setattr(
        native_pgo, "plan_for", lambda detail, _env=None: ("plan", detail)
    )
    monkeypatch.setattr(subprocess, "run", run)
    return box


def trained(box):
    return [c for c in box.commands if c[-1] == "train"]


def test_stale_profile_trains_then_uses_the_new_one(pgo, capsys):
    pgo.states = [("stale", "changed: sources"), ("ok", "/p/abc.profdata")]
    plan = launcher()["pgo_plan"](COUPLED)
    assert plan == ("plan", "/p/abc.profdata")
    assert len(trained(pgo)) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == (
        "native PGO: profile stale (changed: sources); training (~6 min, "
        "DIGI_EMU_PGO_TRAIN=0 to skip)"
    )
    assert err[-1] == "native PGO: using profile abc.profdata"


def test_missing_profile_trains_too(pgo, capsys):
    pgo.states = [("missing", "no manifest"), ("ok", "/p/abc.profdata")]
    assert launcher()["pgo_plan"](COUPLED) is not None
    assert "profile missing (no manifest); training" in capsys.readouterr().err


def test_current_profile_does_not_train(pgo):
    pgo.states = [("ok", "/p/abc.profdata")]
    assert launcher()["pgo_plan"](COUPLED) == ("plan", "/p/abc.profdata")
    assert pgo.commands == []


def test_train_opt_out_falls_back_with_a_warning(pgo, monkeypatch, capsys):
    monkeypatch.setenv("DIGI_EMU_PGO_TRAIN", "0")
    assert launcher()["pgo_plan"](COUPLED) is None
    assert pgo.commands == []
    err = capsys.readouterr().err
    assert err.startswith("WARNING: native PGO: profile stale (changed: sources)")


def test_pgo_opt_out_is_silent(pgo, monkeypatch, capsys):
    pgo.states = [("off", "")]
    monkeypatch.setenv("DIGI_EMU_PGO", "0")
    assert launcher()["pgo_plan"](COUPLED) is None
    assert pgo.commands == []
    assert capsys.readouterr().err == ""


def test_training_failure_falls_back_without_blocking(pgo, capsys):
    pgo.train_exit = 3
    assert launcher()["pgo_plan"](COUPLED) is None
    assert len(trained(pgo)) == 1
    err = capsys.readouterr().err
    assert "WARNING: native PGO: training failed (exit 3); building without PGO" in err


def test_training_that_leaves_the_profile_stale_falls_back(pgo, capsys):
    pgo.states = [("stale", "changed: sources"), ("stale", "changed: rustc")]
    assert launcher()["pgo_plan"](COUPLED) is None
    assert (
        "WARNING: native PGO: profile stale (changed: rustc)" in capsys.readouterr().err
    )


def test_missing_training_inputs_fall_back_without_training(pgo, capsys):
    pgo.blocker = "missing private input /x"
    assert launcher()["pgo_plan"](COUPLED) is None
    assert pgo.commands == []
    err = capsys.readouterr().err
    assert err.startswith("WARNING: native PGO:")
    assert "cannot train: missing private input /x" in err


def test_rustflags_skip_warns_and_never_trains(pgo, capsys):
    pgo.states = [("skipped", "RUSTFLAGS is set")]
    assert launcher()["pgo_plan"](COUPLED) is None
    assert pgo.commands == []
    assert capsys.readouterr().err.startswith("WARNING: native PGO: skipped")


def test_cf_only_build_never_asks_about_pgo(pgo):
    assert launcher()["pgo_plan"](["x.syx"]) is None
    assert pgo.commands == []


def test_train_command_failure_to_start_falls_back(pgo, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise OSError("no python")

    monkeypatch.setattr(subprocess, "run", boom)
    assert launcher()["pgo_plan"](COUPLED) is None
    assert "could not start training: no python" in capsys.readouterr().err
