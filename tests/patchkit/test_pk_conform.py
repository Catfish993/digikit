"""pk_conform: what the machine conformance suite reads from the manifests (no core runs here)."""

from tools.patchkit import pk_conform as c


def test_pluck_parameters_come_from_the_manifest_and_the_device_frame():
    dt2 = c.spec("digikit/pluck", "dt2")
    dn2 = c.spec("digikit/pluck", "dn2")
    assert dt2 is not None and dn2 is not None
    # Digitakt II: TUNE is the stock row 0xca on knob 0; knobs 1, 2, 4, 5 are the module's; the frame skips two halfwords per group of four
    assert [(p.id, p.knob) for p in dt2.params] == [
        ("tune", 0),
        ("pluck_decay", 1),
        ("pluck_pick", 2),
        ("pluck_bright", 4),
        ("pluck_color", 5),
    ]
    assert [dt2.frame.hw(0, p.knob) - dt2.frame.base for p in dt2.params] == [
        0,
        1,
        2,
        6,
        7,
    ]
    assert [(p.id, p.knob) for p in dn2.params] == [
        (f"pluck_{n}", k) for k, n in enumerate(("decay", "pick", "bright", "color"))
    ]
    assert dn2.frame.hw(3, 1) == dn2.frame.base + 3 * dn2.frame.stride + 1
    assert dt2.machine_type == dn2.machine_type == 9
    assert [p.default for p in dn2.params] == [0x4000, 0x2000, 0x5000, 0x2800]


def test_machines_are_found_and_the_framework_is_not():
    labels = {s.label for s in c.machine_specs()}
    assert {
        "digikit/pluck@dt2",
        "digikit/pluck@dn2",
        "test/sine@dt2",
        "test/sine@dn2",
    } <= labels
    assert not any(label.startswith("core/") for label in labels)
    assert "test/dyn-dsp-hang@dt2" in {s.label for s in c.machine_specs(negative=True)}
    assert not c.is_machine_module("test/xslice")  # a machine row without a DSP routine


def test_the_budget_follows_the_headroom_of_the_device():
    for dev in c.DEVICES:
        b = c.budget(dev)
        assert b.voice_block == b.headroom // c.VOICES > 0
        assert b.note_on_block == b.headroom
    # the Digitone II stock load is higher, so its headroom is smaller
    assert c.budget("dn2").headroom < c.budget("dt2").headroom


def test_the_cases_cover_the_groups_and_are_deterministic():
    sp = c.spec("digikit/pluck", "dt2")
    assert sp is not None
    cases = c.cases(sp)
    assert cases == c.cases(sp)
    by_group = {
        g: [x for x in cases if x.group == g]
        for g in ("limits", "corners", "modulation", "long", "retrigger")
    }
    assert all(by_group.values())
    n = len(sp.params)
    assert len(by_group["corners"]) == 3 * 2**n
    assert {x.note for x in by_group["limits"]} == set(c.NOTES_LIMITS)
    assert max(x.blocks for x in by_group["long"]) == c.LONG_BLOCKS
    assert all(x.blocks == c.WALK_BLOCKS for x in by_group["modulation"])
    assert {x.vel for x in by_group["retrigger"]} == {1, 127}
    # every limits case sets every parameter, inside its declared range
    rng = {p.id: (p.lo, p.hi) for p in sp.params}
    for x in cases:
        for row in x.words:
            for pid, w in row:
                assert rng[pid][0] <= w <= rng[pid][1], (x.name, pid, w)
