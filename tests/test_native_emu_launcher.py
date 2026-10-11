"""The launcher restores local DN2 audio by default, with CF-only escape hatches."""

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import digi_env  # noqa: E402

sys.path.pop(0)


@pytest.fixture(autouse=True)
def pinned_rustc(monkeypatch):
    """Never ask rustup for the toolchain from these tests."""
    monkeypatch.setattr(digi_env, "toolchain_rustc", lambda: "/fake/bin/rustc")


@pytest.fixture(autouse=True)
def fresh_gen(monkeypatch):
    """The fake gen dirs here carry no CORE_FINGERPRINT."""
    monkeypatch.setenv("DIGI_ALLOW_STALE_GEN", "1")


def launcher():
    return runpy.run_path(str(ROOT / "tools/native_emu.py"), run_name="launcher_test")


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
    monkeypatch.setattr(digi_env, "DEFAULT_SYX", tmp_path / "Digitone_II_OS1.11.syx")
    monkeypatch.setattr(digi_env, "READY", ready)
    monkeypatch.setattr(digi_env, "DEFAULT_GENERATED", generated)
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    args = scope["default_audio_args"]()
    assert args[1] == "--auto-coupled"
    assert args[-2:] == ["--audio-buffer", "0.3"]
    assert Path(__import__("os").environ["SHARC_GEN_DIR"]) == generated


def test_default_no_audio_keeps_coupling_but_mutes_playback(monkeypatch, tmp_path):
    scope = launcher()
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
    monkeypatch.setattr(digi_env, "DEFAULT_SYX", tmp_path / "Digitone_II_OS1.11.syx")
    monkeypatch.setattr(digi_env, "READY", ready)
    monkeypatch.setattr(digi_env, "DEFAULT_GENERATED", generated)
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    assert scope["default_audio_args"](["--no-audio"])[-1] == "0.3"


def test_selected_firmware_keeps_its_path_but_profile_stays_known(
    monkeypatch, tmp_path
):
    scope = launcher()
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
    monkeypatch.setattr(digi_env, "DEFAULT_SYX", tmp_path / "Digitone_II_OS1.11.syx")
    monkeypatch.setattr(digi_env, "READY", ready)
    monkeypatch.setattr(digi_env, "DEFAULT_GENERATED", generated)
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    args = scope["default_audio_args"](["another.syx", "--card-image", "card.img"])
    assert args[:3] == ["another.syx", "--card-image", "card.img"]
    assert args[args.index("--audio-profile-syx") + 1] == str(profile)


def test_normal_launcher_arguments_get_audio_without_replacing_firmware(
    monkeypatch, tmp_path
):
    scope = launcher()
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
    monkeypatch.setattr(digi_env, "DEFAULT_SYX", tmp_path / "Digitone_II_OS1.11.syx")
    monkeypatch.setattr(digi_env, "READY", ready)
    monkeypatch.setattr(digi_env, "DEFAULT_GENERATED", generated)
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    monkeypatch.setenv("DIGI_EMU_PGO", "0")
    monkeypatch.setitem(scope["main"].__globals__, "build_panel", lambda environ: None)
    calls = []
    monkeypatch.setattr(
        "subprocess.run", lambda command, **_kwargs: calls.append(command)
    )
    scope["main"](["selected.syx", "--card-image", "card.img"])
    forwarded = calls[-1][1:]
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
        == "0x1c399a,0x1c3862,0x1c364f,0x1c5765,0xb823db,0x1c1884,0x1cdcef,0x1c3030,0xb85347,0x1c1da8,0xb80509"
    )


def test_known_dn2_1_11_hash_has_the_cluster_roots():
    table = launcher()["BLOCKTRANS_ROOTS"]
    roots = table["2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0"]
    assert roots == (
        "0xb8276c,0x1c369b,0xb809f2,0x1c5796,0xb84a8c,0xb8212b,"
        "0xb827b7,0x1c5110,0xb82568,0xb813cf,0xb8040f,0xb826a5"
    )
    assert len(roots.split(",")) == 12


def _known_roots(tmp_path, scope, monkeypatch, content=b"dn2"):
    syx = _known_syx(tmp_path, scope, monkeypatch, content)
    table = scope["blocktrans_roots_for"].__globals__["BLOCKTRANS_ROOTS"]
    monkeypatch.setitem(
        table, __import__("hashlib").sha256(content).hexdigest(), "0x30,0x40"
    )
    return syx


def test_blocktrans_roots_follow_the_firmware_hash(monkeypatch, tmp_path):
    scope = launcher()
    syx = _known_roots(tmp_path, scope, monkeypatch)
    other = tmp_path / "other.syx"
    other.write_bytes(b"other")
    assert scope["blocktrans_roots_for"](syx) == "0x30,0x40"
    assert scope["blocktrans_roots_for"](other) == ""
    assert scope["blocktrans_roots_for"](tmp_path / "missing.syx") == ""


def test_set_fast_regions_exports_the_cluster_roots(monkeypatch, tmp_path):
    scope = launcher()
    syx = _known_roots(tmp_path, scope, monkeypatch)
    args = ["--audio-profile-syx", str(syx)]
    environ: dict[str, str] = {}
    scope["set_fast_regions"](args, environ)
    assert environ["SHARC_BLOCKTRANS"] == "0x30,0x40"
    user = {"SHARC_BLOCKTRANS": "0x99"}
    scope["set_fast_regions"](args, user)
    assert user["SHARC_BLOCKTRANS"] == "0x99"
    assert user["SHARC_FAST_REGIONS"] == "0x10,0x20"
    off = {"DIGI_EMU_FAST": "0"}
    scope["set_fast_regions"](args, off)
    assert "SHARC_BLOCKTRANS" not in off
    other = tmp_path / "other.syx"
    other.write_bytes(b"other")
    unknown: dict[str, str] = {}
    scope["set_fast_regions"](["--audio-profile-syx", str(other)], unknown)
    assert unknown == {}


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
    monkeypatch.setitem(scope["main"].__globals__, "build_panel", lambda environ: None)
    calls = []
    monkeypatch.setattr("subprocess.run", lambda command, **kw: calls.append(command))
    scope["main"](["x.syx", "--coupled", "--audio-profile-syx", str(syx)])
    assert __import__("os").environ["SHARC_FAST_REGIONS"] == "0x10,0x20"
    monkeypatch.delenv("SHARC_FAST_REGIONS")


def _build_run_setup(monkeypatch):
    scope = launcher()
    monkeypatch.setenv("DIGI_EMU_PGO", "0")
    monkeypatch.setitem(scope["main"].__globals__, "build_panel", lambda environ: None)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("subprocess.run", run)
    return scope, calls


def test_build_only_prints_the_binary_and_does_not_launch(monkeypatch, capsys):
    scope, calls = _build_run_setup(monkeypatch)
    scope["main"](["x.syx", "--cf-only", "--build-only"])
    assert len(calls) == 1
    assert calls[0][:2] == ["cargo", "build"]
    assert capsys.readouterr().out.strip().endswith("release/digiemu-desktop")


def test_main_runs_the_built_binary_not_cargo_run(monkeypatch):
    scope, calls = _build_run_setup(monkeypatch)
    scope["main"](["x.syx", "--cf-only"])
    assert len(calls) == 2
    assert calls[1][0].endswith("digiemu-desktop")
    assert not any(c[:2] == ["cargo", "run"] for c in calls)
    assert calls[1][1:] == ["x.syx"]


def test_cargo_command_uses_the_gen_keyed_target_dir(monkeypatch, tmp_path):
    scope = launcher()
    plain = scope["cargo_command"](["example.syx"])
    assert plain[plain.index("--target-dir") + 1].endswith("/nogen")
    gen = tmp_path / "gen"
    gen.mkdir()
    monkeypatch.setenv("SHARC_GEN_DIR", str(gen))
    coupled = scope["cargo_command"](["example.syx", "--coupled"])
    target = coupled[coupled.index("--target-dir") + 1]
    assert target.endswith("/generic")
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
    box.devices = []

    def pgo_state(_environ=None, device="dn2"):
        box.devices.append(device)
        return box.states.pop(0) if len(box.states) > 1 else box.states[0]

    def run(command, **_kwargs):
        box.commands.append(command)
        return subprocess.CompletedProcess(command, box.train_exit)

    monkeypatch.setattr(native_pgo, "pgo_state", pgo_state)
    monkeypatch.setattr(
        native_pgo, "training_blocker", lambda _env=None, device="dn2": box.blocker
    )
    monkeypatch.setattr(
        native_pgo, "plan_for", lambda detail, _env=None: ("plan", detail)
    )
    monkeypatch.setattr(native_pgo, "current_profile", lambda device="dn2": None)
    monkeypatch.setattr(subprocess, "run", run)
    return box


def trained(box):
    return [c for c in box.commands if c[-1] == "train"]


def test_stale_profile_trains_then_uses_the_new_one(pgo, monkeypatch, capsys):
    monkeypatch.setenv("DIGI_EMU_PGO_TRAIN", "1")
    pgo.states = [("stale", "changed: sources"), ("ok", "/p/abc.profdata")]
    plan = launcher()["pgo_plan"](COUPLED)
    assert plan == ("plan", "/p/abc.profdata")
    assert len(trained(pgo)) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == (
        "native PGO: profile stale (changed: sources); training (~6 min, "
        "DIGI_EMU_PGO_TRAIN=1)"
    )
    assert err[-1] == "native PGO: using profile abc.profdata"


def test_missing_profile_trains_with_the_opt_in(pgo, monkeypatch, capsys):
    monkeypatch.setenv("DIGI_EMU_PGO_TRAIN", "1")
    pgo.states = [("missing", "no manifest"), ("ok", "/p/abc.profdata")]
    assert launcher()["pgo_plan"](COUPLED) is not None
    assert "profile missing (no manifest); training" in capsys.readouterr().err


def test_current_profile_does_not_train(pgo):
    pgo.states = [("ok", "/p/abc.profdata")]
    assert launcher()["pgo_plan"](COUPLED) == ("plan", "/p/abc.profdata")
    assert pgo.commands == []


def test_stale_profile_is_used_with_a_warning_without_training(
    pgo, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys.modules["native_pgo"],
        "current_profile",
        lambda device="dn2": "old.profdata",
    )
    assert launcher()["pgo_plan"](COUPLED) == ("plan", "old.profdata")
    assert trained(pgo) == []
    err = capsys.readouterr().err
    assert "profile stale" in err
    assert "mise run pgo" in err


def test_missing_profile_does_not_train_by_default(pgo, capsys):
    pgo.states = [("missing", "no manifest")]
    assert launcher()["pgo_plan"](COUPLED) is None
    assert trained(pgo) == []
    assert "mise run pgo" in capsys.readouterr().err


def test_pgo_opt_out_is_silent(pgo, monkeypatch, capsys):
    pgo.states = [("off", "")]
    monkeypatch.setenv("DIGI_EMU_PGO", "0")
    assert launcher()["pgo_plan"](COUPLED) is None
    assert pgo.commands == []
    assert capsys.readouterr().err == ""


def test_training_failure_falls_back_without_blocking(pgo, monkeypatch, capsys):
    monkeypatch.setenv("DIGI_EMU_PGO_TRAIN", "1")
    pgo.train_exit = 3
    assert launcher()["pgo_plan"](COUPLED) is None
    assert len(trained(pgo)) == 1
    err = capsys.readouterr().err
    assert "WARNING: native PGO: training failed (exit 3); building without PGO" in err


def test_training_that_leaves_the_profile_stale_falls_back(pgo, monkeypatch, capsys):
    monkeypatch.setenv("DIGI_EMU_PGO_TRAIN", "1")
    pgo.states = [("stale", "changed: sources"), ("stale", "changed: rustc")]
    assert launcher()["pgo_plan"](COUPLED) is None
    assert (
        "WARNING: native PGO: profile stale (changed: rustc)" in capsys.readouterr().err
    )


def test_missing_training_inputs_fall_back_without_training(pgo, monkeypatch, capsys):
    monkeypatch.setenv("DIGI_EMU_PGO_TRAIN", "1")
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


def dt2_syx(monkeypatch, tmp_path):
    syx = tmp_path / "dt2.syx"
    syx.write_bytes(b"dt2")
    monkeypatch.setattr(
        digi_env, "device_for_syx", lambda p: "dt2" if p == syx else None
    )
    return syx


def test_dt2_firmware_selects_the_dt2_profile(pgo, monkeypatch, tmp_path):
    syx = dt2_syx(monkeypatch, tmp_path)
    pgo.states = [("ok", "/p/dt2.profdata")]
    args = ["x.syx", "--coupled", "--audio-profile-syx", str(syx)]
    assert launcher()["pgo_plan"](args) == ("plan", "/p/dt2.profdata")
    assert pgo.devices == ["dt2"]
    assert launcher()["pgo_plan"](COUPLED) == ("plan", "/p/dt2.profdata")
    assert pgo.devices == ["dt2", "dn2"]


def test_missing_dt2_profile_says_how_to_train_it(pgo, monkeypatch, tmp_path, capsys):
    syx = dt2_syx(monkeypatch, tmp_path)
    pgo.states = [("missing", "no manifest")]
    assert (
        launcher()["pgo_plan"](["--coupled", "--audio-profile-syx", str(syx)]) is None
    )
    assert "native_pgo.py train --device dt2" in capsys.readouterr().err


def test_dt2_opt_in_training_trains_the_dt2_profile(pgo, monkeypatch, tmp_path):
    syx = dt2_syx(monkeypatch, tmp_path)
    monkeypatch.setenv("DIGI_EMU_PGO_TRAIN", "1")
    pgo.states = [("missing", "no manifest"), ("ok", "/p/dt2.profdata")]
    args = ["--coupled", "--audio-profile-syx", str(syx)]
    assert launcher()["pgo_plan"](args) is not None
    assert trained(pgo) == []
    assert pgo.commands[0][-3:] == ["train", "--device", "dt2"]


def test_pgo_verify_names_the_device_and_the_built_binary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_k: (
            calls.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )
    command = ["cargo", "run", "--target-dir", "/t/x", "--", "a"]
    launcher()["verify_pgo"](command, {}, "dt2")
    verify = calls[1]
    assert verify[verify.index("--device") + 1] == "dt2"
    assert verify[verify.index("--binary") + 1].startswith("/t/x/")


def test_cf_only_build_never_asks_about_pgo(pgo):
    assert launcher()["pgo_plan"](["x.syx"]) is None
    assert pgo.commands == []


def test_train_command_failure_to_start_falls_back(pgo, monkeypatch, capsys):
    monkeypatch.setenv("DIGI_EMU_PGO_TRAIN", "1")

    def boom(*_a, **_k):
        raise OSError("no python")

    monkeypatch.setattr(subprocess, "run", boom)
    assert launcher()["pgo_plan"](COUPLED) is None
    assert "could not start training: no python" in capsys.readouterr().err


def test_build_command_is_the_run_command_without_program_arguments():
    command = ["rustup", "run", "1", "cargo", "run", "--release", "--", "x.syx"]
    assert launcher()["build_command"](command) == [
        "rustup",
        "run",
        "1",
        "cargo",
        "build",
        "--release",
    ]


@pytest.mark.parametrize("verify_exit, warns", [(0, False), (1, True), (2, False)])
def test_pgo_mismatch_warns_but_never_blocks(monkeypatch, capsys, verify_exit, warns):
    scope = launcher()
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        code = verify_exit if "verify" in command else 0
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(subprocess, "run", run)
    scope["verify_pgo"](["cargo", "run", "--", "x"], {})
    err = capsys.readouterr().err
    assert ("PGO profile does not match this binary" in err) is warns
    assert calls[0] == ["cargo", "build"]


def test_pgo_build_failure_skips_the_check(monkeypatch, capsys):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 101)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        launcher()["verify_pgo"](["cargo", "run", "--", "x"], {})
    assert exc.value.code == 101
    assert len(calls) == 1
    assert capsys.readouterr().err == ""


def test_daemon_command_builds_digi_emud_without_pgo_or_the_panel():
    scope = launcher()
    plain = scope["daemon_command"](["x.syx"])
    assert plain[plain.index("--manifest-path") + 1] == "native/control/Cargo.toml"
    assert plain[plain.index("--bin") + 1] == "digi-emud"
    assert "--features" not in plain
    assert plain[plain.index("--") + 1 :] == ["x.syx"]
    coupled = scope["daemon_command"](["x.syx", "--coupled"])
    assert coupled[coupled.index("--features") + 1] == "coupled-audio"
    assert scope["app_binary"](plain, None).name == "digi-emud"
    assert scope["app_binary"](scope["cargo_command"](["x.syx"]), None).name == (
        "digiemu-desktop"
    )


def test_main_daemon_builds_and_runs_digi_emud(monkeypatch):
    scope, calls = _build_run_setup(monkeypatch)
    panel = []
    monkeypatch.setitem(
        scope["main"].__globals__, "build_panel", lambda environ: panel.append(1)
    )
    scope["main"](["x.syx", "--cf-only", "--daemon"])
    assert panel == []
    assert calls[0][:2] == ["cargo", "build"]
    assert calls[1][0].endswith("release/digi-emud")
    assert calls[1][1:] == ["x.syx"]


def _dt2_launch(monkeypatch, tmp_path, manifest_core):
    """A DT2 launch with ready inputs, a registered core and a PGO manifest."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import native_pgo
    finally:
        sys.path.pop(0)
    syx = dt2_syx(monkeypatch, tmp_path)
    ready = tmp_path / "ready-dt2"
    ready.mkdir()
    for name in [
        "digi-audio-dn2-image.bin",
        "digi-audio-m5.snap.dsp",
        "digi-audio-m5.snap",
    ]:
        (ready / name).touch()
    registered = tmp_path / "registered"
    registered.mkdir()
    monkeypatch.setitem(digi_env.READY_DIRS, "dt2", ready)
    monkeypatch.setattr(digi_env, "gen_for_image", lambda _image: registered)
    monkeypatch.setattr(
        native_pgo,
        "read_manifest",
        lambda device="dn2": {"generated_dir": str(manifest_core)},
    )
    # default_audio_args exports SHARC_GEN_DIR: have monkeypatch undo it.
    monkeypatch.setenv("SHARC_GEN_DIR", "x")
    monkeypatch.delenv("SHARC_GEN_DIR")
    return syx, registered


def test_dt2_default_core_is_the_one_the_profile_was_trained_on(
    monkeypatch, tmp_path, capsys
):
    trained = tmp_path / "trained"
    trained.mkdir()
    syx, _ = _dt2_launch(monkeypatch, tmp_path, trained)
    args = launcher()["default_audio_args"]([str(syx)])
    assert "--auto-coupled" in args
    assert os.environ["SHARC_GEN_DIR"] == str(trained)
    err = capsys.readouterr().err
    assert f"native core: {trained}" in err and "PGO profile" in err


def test_dt2_default_core_falls_back_to_the_registered_one(
    monkeypatch, tmp_path, capsys
):
    syx, registered = _dt2_launch(monkeypatch, tmp_path, tmp_path / "gone")
    launcher()["default_audio_args"]([str(syx)])
    assert os.environ["SHARC_GEN_DIR"] == str(registered)
    assert "registered core" in capsys.readouterr().err


def test_dt2_default_core_skips_a_stale_trained_core(monkeypatch, tmp_path):
    trained = tmp_path / "trained"
    trained.mkdir()
    syx, registered = _dt2_launch(monkeypatch, tmp_path, trained)
    # Only the registered core carries the current fingerprint.
    (registered / digi_env.CORE_FINGERPRINT_FILE).write_text(
        digi_env.core_fingerprint()
    )
    monkeypatch.delenv("DIGI_ALLOW_STALE_GEN")
    launcher()["default_audio_args"]([str(syx)])
    assert os.environ["SHARC_GEN_DIR"] == str(registered)


def test_dt2_explicit_gen_dir_wins(monkeypatch, tmp_path):
    trained = tmp_path / "trained"
    trained.mkdir()
    syx, _ = _dt2_launch(monkeypatch, tmp_path, trained)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    monkeypatch.setenv("SHARC_GEN_DIR", str(chosen))
    launcher()["default_audio_args"]([str(syx)])
    assert os.environ["SHARC_GEN_DIR"] == str(chosen)
