"""Public synthetic L1 ISA vectors and opt-in software interrupt semantics."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from support.sharc_swi import native_core, runner, word

import sharc_diff as sd
import sharc_run as sr
import sharcldr
from sharc_core.sequencer import _software_interrupt_candidate
from sharc_core.state import _bank_complete, _write_ureg
from sharc_core.values import Const
from sharc_disasm import decode_isa48


@pytest.mark.parametrize("native", [False, True])
def test_isa_vector_delay_slots_and_rti(native):
    ref = runner()
    core = native_core(ref) if native else None
    for expected in (0x9007D, 0x9007E, 0x80, 0x40):
        ref.step()
        assert ref.state.pc_sw == expected
        if core:
            assert core.run(1) == 1, core.halt_reason
            assert (
                sd.compare_states(sd.export_state(ref.state), core.export_state()) == []
            )
        if expected != 0x40:
            assert ref.state.pc_stack == [0x01000040]
            assert len(ref.state.status_stack) == 1
            assert ref.state.uregs[114] == Const(0)
            assert ref.state.uregs[122] == Const(0)
            assert ref.state.uregs[124] == Const(0x80000000)
    assert ref.state.pc_stack == ref.state.status_stack == []
    assert ref.state.uregs[114] == Const(0x1000)
    assert ref.state.uregs[112] == Const(0x123)
    assert ref.state.uregs[113] == Const(0x456)
    assert ref.state.uregs[124] == Const(0)


@pytest.mark.parametrize("native", [False, True])
def test_default_disabled_leaves_eligible_irq_pending(native):
    ref = runner(False)
    core = native_core(ref) if native else None
    ref.step()
    assert ref.state.pc_sw == 0x43
    assert ref.state.uregs[122] == Const(0x80000000)
    assert ref.state.pc_stack == []
    if core:
        assert core.run(1) == 1
        assert sd.compare_states(sd.export_state(ref.state), core.export_state()) == []


@pytest.mark.parametrize("control", ["bank_model", "stack_model", "sysctl"])
@pytest.mark.parametrize("native", [False, True])
def test_invalid_entry_prerequisite_does_not_mutate_state(control, native):
    ref = runner()
    if control == "sysctl":
        ref.state.mmrs[0x30024] = Const(8)
    else:
        setattr(ref.state, control, False)
    before = sd.export_state(ref.state)
    if native:
        core = native_core(ref)
        assert core.run(1) == 0
        assert core.halted
        assert sd.compare_states(before, core.export_state()) == []
    else:
        with pytest.raises(sr.Halt, match="interrupt"):
            ref.step()
        assert sd.compare_states(before, sd.export_state(ref.state)) == []


def test_priority_nesting_and_active_latch_write():
    state = runner().state
    state.uregs[122] = state.uregs[123] = Const(0xF0000000)
    assert _software_interrupt_candidate(state) == 0x10000000
    state.uregs[124] = Const(0x40000000)
    assert _software_interrupt_candidate(state) == 0
    state.uregs[114] = Const(0x1800)
    assert _software_interrupt_candidate(state) == 0x10000000
    _write_ureg(state, 122, Const(0xF0000000))
    assert state.uregs[122] == Const(0xB0000000)


def test_isa_encoding_is_little_endian_whole_word():
    raw = word(
        "8a_abs",
        **{
            "addr[23:16]": 0x12,
            "addr[15:0]": 0x3456,
            "cond[4:0]": 31,
            "b": 0,
            "j": 1,
            "a": 0,
        },
    )
    insn = decode_isa48(raw.to_bytes(6, "little"))
    assert insn.type_name == "8a_abs"
    assert insn.fields["addr[23:16]"] == 0x12
    assert insn.fields["addr[15:0]"] == 0x3456
    assert insn.fields["j"] == 1


@pytest.mark.parametrize("native", [False, True])
def test_decoded_latch_write_cannot_relatch_active_interrupt(native):
    write = word("17a", **{"ureg[6:0]": 122, "data[31:16]": 0xC000, "data[15:0]": 0})
    rti = word(
        "11a",
        **{
            "x": 1,
            "cond[4:0]": 31,
            "j": 0,
            "lr": 0,
            "e": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        },
    )
    ref = runner(handler=[write, rti])
    core = native_core(ref) if native else None
    for _ in range(5):
        ref.step()
        if core:
            assert core.run(1) == 1, core.halt_reason
            assert (
                sd.compare_states(sd.export_state(ref.state), core.export_state()) == []
            )
    assert ref.state.pc_sw == 0x40
    assert ref.state.uregs[122] == Const(0x40000000)
    assert ref.state.uregs[124] == Const(0)


@pytest.mark.parametrize("native", [False, True])
def test_entry_settles_banks_and_rti_restores_with_following_instruction(native):
    ref = runner()
    ref.state.uregs[0] = Const(11)
    ref.state.bank_alt[0] = Const(22)
    _write_ureg(ref.state, 114, Const(0x1400))
    _bank_complete(ref.state)
    _bank_complete(ref.state)
    ref.state.uregs[115] = Const(0x1400)
    assert ref.state.uregs[0] == Const(22)
    core = native_core(ref) if native else None
    for index in range(5):
        ref.step()
        if core:
            assert core.run(1) == 1, core.halt_reason
            assert (
                sd.compare_states(sd.export_state(ref.state), core.export_state()) == []
            )
        assert ref.state.uregs[0] == Const(22 if index == 4 else 11)
    assert ref.state.bank_active_mask == 0x400


def delayed_rti() -> int:
    return word(
        "11a",
        **{
            "x": 1,
            "cond[4:0]": 31,
            "j": 1,
            "lr": 0,
            "e": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        },
    )


def banked_runner(handler):
    ref = runner(handler=handler)
    ref.state.uregs[0] = Const(11)
    ref.state.bank_alt[0] = Const(22)
    _write_ureg(ref.state, 114, Const(0x1400))
    _bank_complete(ref.state)
    _bank_complete(ref.state)
    ref.state.uregs[115] = Const(0x1400)
    return ref


@pytest.mark.parametrize("native", [False, True])
def test_delayed_rti_restores_banks_at_the_return_target(native):
    ref = banked_runner([delayed_rti(), word("21a"), word("21a")])
    assert ref.state.uregs[0] == Const(22)
    core = native_core(ref) if native else None
    for index in range(7):
        ref.step()
        if core:
            assert core.run(1) == 1, core.halt_reason
            assert (
                sd.compare_states(sd.export_state(ref.state), core.export_state()) == []
            )
        # Steps 3-5 are the RTI and its two delay slots; both slots still
        # see the interrupt's bank, the target at 0x40 sees the restored one.
        assert ref.state.uregs[0] == Const(22 if index >= 5 else 11)
        if index == 5:
            assert ref.state.pc_sw == 0x40
    assert ref.state.bank_active_mask == 0x400
    assert ref.state.bank_requested_mask == ref.state.bank_pending_mask == -1


def test_mode1_write_in_delayed_rti_slot_fails_closed():
    ref = banked_runner([delayed_rti(), word("21a"), word("21a")])
    for _ in range(4):
        ref.step()
    assert ref.state.bank_requested_mask == 0x400
    with pytest.raises(ValueError, match="delayed RTI"):
        _write_ureg(ref.state, 114, Const(0))


def test_vector_fetch_failure_preserves_successful_entry():
    ref = runner()
    # Public format: an unpopulated IVT has no readable backing bytes.
    ref.state.concrete = sharcldr.LoadedMemory(
        bytes(512),
        [
            dict(
                target_address=sharcldr.SW_ALIAS_BASE,
                byte_count=512,
                fill=False,
                payload_offset=0,
                payload_len=512,
            )
        ],
    )
    core = native_core(ref)
    assert core.run(1) == 0
    assert core.halted
    state = core.export_state()
    assert state["pc_sw"] == 0x9007C
    assert state["pc_stack"] == [0x01000040]
    assert len(state["status_stack"]) == 1
    assert state["uregs"][122]["value"] == 0
    assert state["uregs"][124]["value"] == 0x80000000
