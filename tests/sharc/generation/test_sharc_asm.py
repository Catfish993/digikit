"""tools/sharc_asm.py encodes the forms the DSP hooks need; the oracle is the
firmware's own words for the same instruction shapes (register and immediate
fields differ only where the instruction does)."""

import sys

import pytest
from support.paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tools"))
import dsp_master_hook  # noqa: E402
import sharc_asm  # noqa: E402


def one(build):
    a = sharc_asm.Asm(0x1000)
    build(a)
    ((_, parcels),) = a.words()
    return "".join(f"{p:04x}" for p in parcels)


def test_forms_match_firmware_words():
    assert one(lambda a: a.ureg_imm("I3", 0x25F180)) == "0f130025f180"
    assert one(lambda a: a.ureg_imm16("R13", 4)) == "0f8d0004"
    assert one(lambda a: a.load_abs("R2", 0x25F780)) == "10020025f780"
    assert one(lambda a: a.store_abs(0x25F780, "R1")) == "11010025f780"
    assert one(lambda a: a.store("M13", 4, 4, post=True)) == "593e96bf"
    assert one(lambda a: a.call_abs(0x1C74A1)) == "1804001c74a1"
    assert one(a_nop) == "000000000000"


def a_nop(a):
    a.nop()


def test_do_loop_matches_firmware():
    # DT2 0x1c747b: DO ... UNTIL LCE, 16 iterations, last instruction 0x18 on
    a = sharc_asm.Asm(0x1C747B)
    a.do_lcntr(16, 0x1C7493)
    ((_, parcels),) = a.words()
    assert "".join(f"{p:04x}" for p in parcels) == "0c0010800018"


def test_hook_routine_decodes_as_intended():
    for device in dsp_master_hook.DEVICES:
        text = [
            line.split(None, 2)[2]
            for line in dsp_master_hook.build_hook(device).listing()
        ]
        assert text[0].startswith("R0 = DM(0x200a0100)")
        assert text[1].startswith("I4 = ")
        assert "trip count 64" in text[2]
        assert text[4] == "F1 = F1 * F0"
        assert text[-1].startswith("JUMP")


def test_wrong_form_is_rejected():
    with pytest.raises(ValueError):
        sharc_asm.check_decode([0x0F14, 0x0025, 0xF180], "14a")


def test_patch_text_guards_the_call_site():
    want = {
        "dt2": "PM 1c7734 180400bd0000 was 1804001c74a1",
        "dn2": "PM 1c9fd2 180400bd0000 was 1804001c9d3f",
    }
    for device, line in want.items():
        text = dsp_master_hook.patch_text(device, 0.5, dsp_master_hook.GAIN_ADDR, True)
        assert line in text
        assert "DM 200a0100 3f000000" in text


def test_param_variant_reads_the_frame_word():
    text = [
        line.split(None, 2)[2]
        for line in dsp_master_hook.build_hook("dt2", param_word=0x2559BA).listing()
    ]
    assert text[0] == "R0 = DM(0x2559ba) (sw)"
    assert "F0 = float(F0)" in text
    assert "F0 = fadd(F0, F2)" in text
