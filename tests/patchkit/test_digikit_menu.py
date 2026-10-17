"""core/digikit-menu (finding 28): the committed objects, the anchors on every port, the hook site, the proof tool's parsers.

The emulator proof is tools/coldfire/digikit_menu_proof.py (minutes); these are the quick checks."""

import json
import subprocess
import tomllib

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.coldfire import arena_safety, digikit_menu_proof, panel_script  # noqa: E402
from tools.emulator import native_target  # noqa: E402
from tools.firmware import config  # noqa: E402
from tools.patchkit import pk_build, pk_menu_rules, pk_toolchains  # noqa: E402

MODULE = ROOT / "modules/core/digikit-menu"
IMAGES = {
    "dt2-1.16": "Digitakt_II_OS1.16.syx",
    "dn2-1.11": "Digitone_II_OS1.11.syx",
    "dt2-1.17": "Digitakt_II_OS1.17.syx",
    "dn2-1.12": "Digitone_II_OS1.12.syx",
}


def test_committed_objects_are_current_and_plain_coldfire():
    for name in ("digikit-menu-rs", "digikit-menu-cf"):
        obj = json.loads((MODULE / "build" / f"{name}.dko.json").read_text())
        assert {r["type"] for r in obj["relocs"]} == {"cf.32"}
    if not pk_toolchains.codegen_gcc_ready():
        pytest.skip("Rust-for-ColdFire toolchain not installed")
    pk_build.build_module(MODULE, check=True)


def test_every_rule_resolves_on_every_port():
    """Each anchor of pk_menu_rules is in all four port files, with the same length."""
    names = {n for n, _a, _l, _k in pk_menu_rules.SITES}
    lens = {n: ln for n, _a, ln, _k in pk_menu_rules.SITES}
    for image in IMAGES:
        port = tomllib.loads((ROOT / "devices" / "ports" / f"{image}.toml").read_text())
        have = {a["name"]: a for a in port["anchor"]}
        assert names <= set(have), (image, names - set(have))
        for n in names:
            assert have[n]["len"] == lens[n], (image, n)


def test_module_toml_rules_are_the_generated_ones():
    """The [[anchor_rule]] blocks of module.toml are the output of pk_menu_rules (one place for the sites)."""
    text = (MODULE / "module.toml").read_text()
    assert text.strip().endswith(
        pk_menu_rules.render(pk_menu_rules.build_rules("dt2-1.16")).strip()
    )


def patchkit(*args):
    target = native_target.target_dir(None).parent / "nogen"
    subprocess.run(
        [
            "cargo",
            "build",
            "-p",
            "patchkit",
            "--release",
            "--offline",
            "--target-dir",
            str(target),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    r = subprocess.run(
        [str(target / "release" / "patchkit"), "--root", str(ROOT), *map(str, args)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr + r.stdout


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_hook_displaces_the_selection_restore(image, tmp_path):
    """On every image the hook sits before `movea.l (a2),a0; pea (2).w; move.l d4,-(sp)` of the SETTINGS rebuild and the
    two displaced instructions run in the tail."""
    fw = config.stock(IMAGES[image])
    if not fw.exists():
        pytest.skip(f"{IMAGES[image]} missing")
    out, plan_path = tmp_path / "x.syx", tmp_path / "plan.json"
    patchkit(
        "apply",
        "--firmware",
        fw,
        "--modules",
        "core/digikit-menu",
        "-o",
        out,
        "--plan",
        plan_path,
    )
    hooks = [
        h
        for h in json.loads(plan_path.read_text())["hooks"]
        if h["anchor"] != "dkrt.hook.boot"  # the runtime's own boot hook
    ]
    assert len(hooks) == 1 and hooks[0]["anchor"] == "cf.menu.settings_tail"
    assert [i.split(None, 2)[2] for i in hooks[0]["displaced_insns"]] == [
        "movea.l (a2),a0",
        "pea.l (0x2).w",
    ]


def test_plan_helpers(tmp_path):
    plan = tmp_path / "p.json"
    plan.write_text(
        json.dumps(
            {
                "base": {"device": "dt2", "version": "1.16"},
                "regions": {"cf.expansion": [0x1000, 0x100000]},
                "symbols": {
                    "core/digikit-menu@0.1.0:.sec..bss._RNvCsAbC_15pk_digikit_menu4TICK": 0x47E8C100,
                    "core/runtime@0.1.0:DKRT_STATE": 0x47E8C200,
                },
            }
        )
    )
    p = digikit_menu_proof.Plan(plan)
    assert p.addr("TICK") == 0x47E8C100 and p.addr("DKRT_STATE") == 0x47E8C200
    lo, hi = arena_safety.expansion("dt2-1.16")
    assert arena_safety.plan_allow_pc(plan) == "0x%x-0x%x" % (lo, lo + 0x1000)


def test_peek_at_parser(tmp_path):
    (tmp_path / "probe.stdout").write_text(
        '{"peek_at":"0x47e8c100","t":5,"value":Some(7)}\n{"peek_at":"0x47e8c100","t":9,"value":None}\n'
        '{"peek":"0x47e8c104","value":Some(1)}\n'
    )
    assert digikit_menu_proof.peek_ats(tmp_path) == [7, None]


def test_menu_calls_the_runtime_by_symbol():
    """The menu `requires` core/runtime and names its ABI functions as undefined symbols; the runtime exports them. No
    table, no stand-in module (finding 28)."""
    menu = json.loads((MODULE / "build/digikit-menu-rs.dko.json").read_text())
    rt = json.loads((ROOT / "modules/core/runtime/build/runtime.dko.json").read_text())
    names = {s["name"] for s in menu["symbols"]}
    wanted = {
        "dkrt_selection_get",
        "dkrt_selection_set",
        "dkrt_restart",
        "dkrt_boot_info",
        "dkrt_status",
        "dkrt_module",
    }
    assert wanted <= {r["symbol"] for r in menu["relocs"]}
    assert wanted.isdisjoint(
        {s["name"] for s in menu["symbols"] if s["global"] and s.get("section")}
    )
    assert wanted <= {s["name"] for s in rt["symbols"] if s["global"]}
    assert "DKRT_API" not in names
    assert tomllib.loads((MODULE / "module.toml").read_text())["module"][
        "requires"
    ] == ["core/runtime"]
    assert not (ROOT / "modules" / "test" / "digikit-rt-stub").exists()


def test_the_menu_and_the_runtime_share_the_store_reader():
    """One reader (`dkrt_sdk::store`): neither crate has its own table or header parser."""
    menu = (MODULE / "src/rs/src/lib.rs").read_text()
    rt = (ROOT / "modules/core/runtime/src/rs/src").glob("*.rs")
    assert "store::open" in menu and 'b"DKST"' not in menu and "0x444b_5354" not in menu
    assert not any("0x444b_5354" in f.read_text() for f in rt)


def test_peek_parser(tmp_path):
    (tmp_path / "probe.stdout").write_text(
        '{"peek":"0x47e8c100","value":Some(16909060)}\n{"peek":"0x47e8c104","value":None}\n'
    )
    assert digikit_menu_proof.peek(tmp_path, 0x47E8C100) == 0x01020304
    assert digikit_menu_proof.peek(tmp_path, 0x47E8C104) is None
    assert digikit_menu_proof.peek_bytes(tmp_path, 0x47E8C100, 3) == b"\x01\x02\x03"


def test_digikit_scripts_are_registered():
    for name in ("digikit_main", "digikit_about"):
        s = panel_script.SCRIPTS[name]()
        assert [n for n, _ in s.frames][0] == "settings_digikit"
    for name in ("digikit_errors", "digikit_gate"):
        s = panel_script.SCRIPTS[name]()
        assert [n for n, _ in s.frames][:2] == ["settings_digikit", "digikit_main"]
    # the gate reads the module's counter through the runtime's log entry 0 (magic, three samples, then the marker `fini` left), twice more after unload
    s = panel_script.digikit_gate_script(0x47E8C200)
    peeks = [spec for _t, spec in s.events if spec.startswith("--peek-at")]
    assert len(peeks) == 8 and all(":" in p.split()[1] for p in peeks)


@pytest.mark.slow
def test_gate_loads_and_unloads_from_the_menu(tmp_path):
    """The Phase 4 gate on DT2 1.16 (about 30 s): tick dyn-demo, LOAD, its counter rises, ABOUT, the row is marked loaded,
    untick, LOAD unloads, its counter stops; write-watch and flush check clean."""
    if not config.stock("Digitakt_II_OS1.16.syx").exists():
        pytest.skip("Digitakt_II_OS1.16.syx missing")
    if not pk_toolchains.codegen_gcc_ready():
        pytest.skip("Rust-for-ColdFire toolchain not installed")
    r = subprocess.run(
        ["uv", "run", "python", "-m", "tools.coldfire.digikit_menu_proof", "run", "--image", "dt2-1.16",
         "--only", "digikit_gate,digikit_errors"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
