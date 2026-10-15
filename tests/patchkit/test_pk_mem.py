"""pk_mem: the readout arithmetic on a synthetic region table and plan (no firmware, no placement run)."""

from tools.patchkit import pk_mem as m

PORT = {
    "region": [
        {"name": "cf.a", "cpu": "cf", "start": 0x1000, "end": 0x2000, "excludes": []},
        {
            "name": "dsp.b",
            "cpu": "sharc",
            "start": 0x20000,
            "end": 0x30000,
            "excludes": [[0x28000, 0x2C000]],
        },
    ]
}


def write(owner, addr, n):
    return {"kind": "place", "owner": owner, "addr": addr, "len": n}


def test_region_size_is_net_of_excludes():
    r = m.regions_of(PORT)
    assert r["cf.a"]["size"] == 0x1000
    assert r["dsp.b"]["size"] == 0x10000 - 0x4000


def test_usage_attributes_by_address_and_keeps_the_placement_total():
    plan = {
        "modules": ["x@1", "y@1"],
        "regions": {"cf.a": [300, 4096], "dsp.b": [1000, 0xC000]},
        "writes": [
            write("x@1", 0x1000, 100),
            write("y@1", 0x1080, 150),
            write("y@1", 0x20000, 800),
            {"kind": "patch", "owner": "x@1", "addr": 0x1010, "len": 6},
        ],
    }
    u = m.usage(m.regions_of(PORT), plan)
    cf, dsp = u["regions"]
    assert cf["modules"] == {"x@1": 100, "y@1": 150}
    assert (cf["used"], cf["free"], cf["other"]) == (300, 3796, 50)
    assert dsp["modules"] == {"y@1": 800} and dsp["other"] == 200
    assert round(cf["pct"], 2) == round(100 * 300 / 4096, 2)
    assert "x@1" in m.table(u) and "(alignment, bss)" in m.table(u)


def test_no_plan_shows_the_free_regions():
    u = m.usage(m.regions_of(PORT), None)
    assert [r["free"] for r in u["regions"]] == [0x1000, 0xC000]
    assert u["modules"] == []


def test_real_port_files_parse():
    for device, fw in (("dt2", "1.17"), ("dn2", "1.12")):
        regions = m.port_regions(device, fw)
        assert {"cf.expansion", "dsp.l2"} <= set(regions)
