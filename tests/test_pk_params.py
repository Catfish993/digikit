"""core/params port rules: the Python scan that records the sites agrees with the recorded ports, and its pieces."""

import os
import sys
import tomllib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import port_resolve as pr  # noqa: E402


def test_the_site_scan_finds_references_and_clamp_bounds_but_not_loop_ends():
    # base 0x1000: bound, a reference into [0x2000, 0x2000 + 5 * 60), a loop end (bne), a bound (bhi)
    main = bytes.fromhex(
        "0c800000000555c141f9000020040c820000000566000c8100000004620400000000"
    )
    refs, bounds = pr.scan_param_sites(main, 0x1000, 0x2000, 5)
    assert refs == [0x1000 + 10]
    assert bounds == [(0x1000, 5), (0x1000 + 22, 4)]
    digest = pr.params_sites_digest(refs, bounds)
    assert len(digest) == 64 and digest != pr.params_sites_digest(refs, bounds[:1])


def test_rules_are_declared_for_the_three_anchors():
    with open(
        os.path.join(ROOT, "modules", "core", "params", "module.toml"), "rb"
    ) as f:
        m = tomllib.load(f)
    rules = {r["name"]: r for r in m["anchor_rule"]}
    assert set(rules) == {"cf.params.t1", "cf.params.sites", "cf.params.t2"}
    assert rules["cf.params.t1"]["kind"] == "cf_params_t1"
    assert rules["cf.params.sites"]["kind"] == "cf_params_sites"
    assert (
        len(rules["cf.params.t2"]["hash"]) == 32 and rules["cf.params.t2"]["len"] == 10
    )
    assert all(r["devices"] == ["dt2"] for r in rules.values())


@pytest.mark.parametrize("version", ["1.16", "1.17"])
def test_the_scan_reproduces_the_recorded_port(version):
    syx = os.path.join(ROOT, f"Digitakt_II_OS{version}.syx")
    port = os.path.join(ROOT, "devices", "ports", f"dt2-{version}.toml")
    if not (os.path.exists(syx) and os.path.exists(port)):
        pytest.skip("needs the stock image and its port")
    with open(port, "rb") as f:
        anchors = {a["name"]: a for a in tomllib.load(f)["anchor"]}
    main = pr.sections_of(type(pr.ROOT)(syx))[3]
    t1 = anchors["cf.params.t1"]
    refs, bounds = pr.scan_param_sites(main, 0x40000400, t1["addr"], t1["len"] // 60)
    sites = anchors["cf.params.sites"]
    assert len(refs) + len(bounds) == sites["len"] == 111
    assert pr.params_sites_digest(refs, bounds) == sites["sha256"]
