"""Symbolic tracer: memory transfer forms and DM canonicalization."""

import os
import struct
import sys
import unittest
from importlib import import_module

from support.paths import REPO_ROOT

sys.path.insert(0, os.path.join(str(REPO_ROOT), "tools"))
T = import_module("sharc_trace")
Instruction = import_module("sharc_disasm").Instruction
L = import_module("sharcldr")
from support.sharc_trace_build import insn, loader_block, loader_memory  # noqa: E402

_floats = import_module("sharc_core.floats")  # _double_to_fixed isn't re-exported by T

from support.sharc_trace_build import TraceRunMixin  # noqa: E402


class TraceMemoryFormsTest(TraceRunMixin, unittest.TestCase):
    """Memory transfer forms."""

    def test_type7a_modify_uses_its_m_register_and_type3a_moves_one_word(self):
        modified = self.run_one(
            T.State(
                1,
                {
                    23: T.Const(0x100),
                    1: T.Const(4),
                    2: T.Const(5),
                    T.UREG_CODES["M7"]: T.Const(1),
                },
            ),
            insn(
                "7a",
                {
                    "g": 0,
                    "cond[4:0]": 31,
                    "is[2:2]": 1,
                    "is[1:0]": 3,
                    "m[2:0]": 7,
                    "idis[2:0]": 0,
                    "compute[22:16]": 0x28,
                    "compute[15:0]": 0x8310,
                },
                6,
            ),
        )
        self.assertEqual(modified.trace[0]["action"], "i-modify")
        self.assertEqual(modified.trace[0]["source"], "I7")
        self.assertEqual(modified.uregs[23], T.Const(0x101))
        self.assertEqual(modified.uregs[3], T.Const(29))

        memory = loader_memory(loader_block(1, 0x80, 4, payload=b"\0" * 4))
        moved = self.run_one(
            T.State(
                1,
                {16: T.Const(0x80), 32: T.Const(1), 2: T.Const(0xAABBCCDD)},
                concrete=memory,
                assume_nw32=True,
            ),
            insn(
                "3a",
                {
                    "u": 1,
                    "i": 0,
                    "m": 0,
                    "cond": 31,
                    "g": 0,
                    "d": 1,
                    "l": 0,
                    "ureg": 2,
                    "compute": 0,
                },
                6,
            ),
        )
        self.assertTrue(moved.trace[0]["concrete_write"])
        self.assertEqual(T._dm_read(moved, 0x80, 4), T.Const(0xAABBCCDD))
        self.assertEqual(moved.uregs[16], T.Const(0x84))

    def test_type7a_not_sv_pex_true_modifies_even_in_simd(self):
        state = self.run_one(
            T.State(
                0x10,
                {
                    T.UREG_CODES["I12"]: T.Const(0x100),
                    T.UREG_CODES["M9"]: T.Const(1),
                    T.UREG_CODES["L12"]: T.Const(0),
                    T.UREG_CODES["ASTATX"]: T.Const(0),  # SV clear: NOT SV is true.
                    T.UREG_CODES["MODE1"]: T.Const(1 << 21),
                },
                assume_nw32=True,
            ),
            insn(
                "7a",
                {
                    "g": 1,
                    "cond[4:0]": 0x17,
                    "is[2:2]": 1,
                    "is[1:0]": 0,
                    "m[2:0]": 1,
                    "idis[2:0]": 0,
                    "w": 1,  # (nw): scaled by 4 in byte space
                    "compute[22:16]": 0,
                    "compute[15:0]": 0,
                },
                6,
            ),
        )
        self.assertEqual(state.uregs[T.UREG_CODES["I12"]], T.Const(0x104))
        self.assertEqual(state.pc_sw, 0x13)
        self.assertEqual(state.steps, 1)
        self.assertEqual(state.trace[0]["action"], "i-modify")

    def test_type7a_not_sv_false_skips_only_in_known_sisd(self):
        state = self.run_one(
            T.State(
                0x10,
                {
                    T.UREG_CODES["I12"]: T.Const(0x100),
                    T.UREG_CODES["M9"]: T.Const(1),
                    T.UREG_CODES["L12"]: T.Const(0),
                    T.UREG_CODES["ASTATX"]: T.Const(1 << T.SV_BIT),
                    T.UREG_CODES["MODE1"]: T.Const(0),
                },
                assume_nw32=True,
            ),
            insn(
                "7a",
                {
                    "g": 1,
                    "cond[4:0]": 0x17,
                    "is[2:2]": 1,
                    "is[1:0]": 0,
                    "m[2:0]": 1,
                    "idis[2:0]": 0,
                    "compute[22:16]": 0,
                    "compute[15:0]": 0,
                },
                6,
            ),
        )
        self.assertEqual(state.uregs[T.UREG_CODES["I12"]], T.Const(0x100))
        self.assertEqual(state.trace[0]["action"], "i-modify-skipped")
        self.assertEqual((state.pc_sw, state.steps), (0x13, 1))

    def test_type7a_not_sv_uncertain_outcomes_taint_destination_and_store(self):
        fields = {
            "g": 1,
            "cond[4:0]": 0x17,
            "is[2:2]": 1,
            "is[1:0]": 0,
            "m[2:0]": 1,
            "idis[2:0]": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        base = {
            T.UREG_CODES["I12"]: T.Const(0x100),
            T.UREG_CODES["M9"]: T.Const(1),
            T.UREG_CODES["L12"]: T.Const(0),
        }
        for label, registers in (
            (
                "PEx false SIMD",
                {
                    **base,
                    T.UREG_CODES["ASTATX"]: T.Const(1 << T.SV_BIT),
                    T.UREG_CODES["MODE1"]: T.Const(1 << 21),
                },
            ),
            (
                "PEx false unknown MODE1",
                {**base, T.UREG_CODES["ASTATX"]: T.Const(1 << T.SV_BIT)},
            ),
            ("unknown ASTATX", {**base, T.UREG_CODES["MODE1"]: T.Const(0)}),
        ):
            with self.subTest(label=label):
                state = self.run_one(
                    T.State(0x10, registers, assume_nw32=True), insn("7a", fields, 6)
                )
                self.assertEqual(
                    state.uregs[T.UREG_CODES["I12"]],
                    T.Unknown("conditional Type7a modify outcome"),
                )
                self.assertEqual((state.pc_sw, state.steps), (0x13, 1))
                self.assertEqual(state.trace[0]["action"], "i-modify-uncertain")
                # A downstream DM store through the conditional I12 is not definite.
                address = T._json_value(state.uregs[T.UREG_CODES["I12"]])
                W = import_module("sharcwriters")
                classification, _ = W.classify_store_address(
                    address, 4, 0x100, None, None
                )
                self.assertEqual(classification, "UNRESOLVED")

    def test_type7a_not_sv_fails_closed_for_circular_or_compute_forms(self):
        fields = {
            "g": 1,
            "cond[4:0]": 0x17,
            "is[2:2]": 1,
            "is[1:0]": 0,
            "m[2:0]": 1,
            "idis[2:0]": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        base = {
            T.UREG_CODES["I12"]: T.Const(0x100),
            T.UREG_CODES["M9"]: T.Const(1),
            T.UREG_CODES["ASTATX"]: T.Const(0),
            T.UREG_CODES["MODE1"]: T.Const(0),
        }
        for label, registers, altered, reason in (
            (
                "nonzero L",
                {**base, T.UREG_CODES["L12"]: T.Const(1)},
                {},
                "unsupported Type7a circular modify",
            ),
            ("missing L", base, {}, "unsupported Type7a circular modify"),
        ):
            with self.subTest(label=label):
                state = self.run_one(
                    T.State(0x10, registers, assume_nw32=True),
                    insn("7a", {**fields, **altered}, 6),
                )
                self.assertEqual(state.stopped, reason)

    def test_type7a_other_condition_is_evaluated_not_refused(self):
        # Any cond is accepted (PRM p.14-47: the condition covers the whole
        # instruction); SV is clear, so IF SV (0x07) skips the modify.
        fields = {
            "g": 1,
            "cond[4:0]": 0x07,
            "is[2:2]": 1,
            "is[1:0]": 0,
            "m[2:0]": 1,
            "idis[2:0]": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        registers = {
            T.UREG_CODES["I12"]: T.Const(0x100),
            T.UREG_CODES["M9"]: T.Const(1),
            T.UREG_CODES["L12"]: T.Const(0),
            T.UREG_CODES["ASTATX"]: T.Const(0),
            T.UREG_CODES["MODE1"]: T.Const(0),
        }
        state = self.run_one(
            T.State(0x10, registers, assume_nw32=True), insn("7a", fields, 6)
        )
        self.assertIsNone(state.stopped)
        self.assertEqual(state.uregs[T.UREG_CODES["I12"]], T.Const(0x100))

    def test_type14a_direct_load_and_store(self):
        fields = {
            "addr[31:16]": 0x310C,
            "addr[15:0]": 0x90C0,
            "g": 0,
            "d": 1,
            "l": 0,
            "ureg[6:0]": 16,
        }
        stored = self.run_one(
            T.State(10, {16: T.Const(0x3DEF7B9C)}),
            insn("14a", fields, 6),
        )
        self.assertEqual(
            stored.trace[0],
            {
                "pc_sw": 10,
                "form": "14a",
                "action": "store",
                "space": "DM",
                "ureg": "I0",
                "value": 0x3DEF7B9C,
                "address": 0x310C90C0,
                "expression": "0x310c90c0",
                "simd_companion_possible": True,
            },
        )

        loaded = self.run_one(
            T.State(10, {16: T.Const(7)}),
            insn("14a", {**fields, "g": 1, "d": 0}, 6),
        )
        self.assertEqual(
            (loaded.trace[0]["space"], loaded.trace[0]["simd_companion_possible"]),
            ("PM", True),
        )
        self.assertEqual(loaded.trace[0]["address"], 0x310C90C0)
        self.assertEqual(loaded.uregs[16], T.Unknown("memory-address 0x310c90c0"))

        memory = loader_memory(
            loader_block(0, 0x200, 8, payload=b"\0" * 8),
        )
        long_fields = {
            **fields,
            "addr[31:16]": 0,
            "addr[15:0]": 0x200,
            "ureg[6:0]": 6,
            "l": 1,
        }
        long_word = self.run_one(
            T.State(
                10,
                {6: T.Const(0x12345678), 7: T.Const(0x9ABCDEF0)},
                concrete=memory,
                assume_nw32=True,
            ),
            insn("14a", long_fields, 6),
        )
        self.assertEqual(T._dm_read(long_word, 0x200, 4), T.Const(0x12345678))
        self.assertEqual(T._dm_read(long_word, 0x204, 4), T.Const(0x9ABCDEF0))
        self.assertEqual(long_word.trace[0]["ureg_pair"], ["R6", "R7"])
        self.assertEqual(long_word.trace[0]["access_width"], "long-word")

        long_word.pc_sw = 10
        loaded_pair = self.run_one(
            long_word,
            insn("14a", {**long_fields, "d": 0, "ureg[6:0]": 10}, 6),
        )
        self.assertEqual(loaded_pair.uregs[10], T.Const(0x12345678))
        self.assertEqual(loaded_pair.uregs[11], T.Const(0x9ABCDEF0))

        # An odd-coded ureg pairs with ureg-1, not ureg+1 (PRM p.6-5's odd
        # DAG-register case): R7 (explicit, low half) with R6 (its
        # neighbor, high half) -- the reverse order from the even R6/R7
        # pairing above, but the same two addresses.
        odd_store = self.run_one(
            T.State(
                10,
                {6: T.Const(0x11111111), 7: T.Const(0x22222222)},
                concrete=memory,
                assume_nw32=True,
            ),
            insn("14a", {**long_fields, "ureg[6:0]": 7}, 6),
        )
        self.assertIsNone(odd_store.stopped)
        self.assertEqual(odd_store.trace[0]["ureg_pair"], ["R7", "R6"])
        self.assertEqual(T._dm_read(odd_store, 0x200, 4), T.Const(0x22222222))
        self.assertEqual(T._dm_read(odd_store, 0x204, 4), T.Const(0x11111111))

        odd_store.pc_sw = 10
        odd_load = self.run_one(
            odd_store,
            insn("14a", {**long_fields, "d": 0, "ureg[6:0]": 7}, 6),
        )
        self.assertIsNone(odd_load.stopped)
        self.assertEqual(odd_load.trace[0]["ureg_pair"], ["R7", "R6"])
        self.assertEqual(odd_load.uregs[7], T.Const(0x22222222))
        self.assertEqual(odd_load.uregs[6], T.Const(0x11111111))

    def test_type14a_long_word_system_register_and_unpaired_uregs(self):
        """Real SW 0x1c32ad (1847981): "USTAT2 = DM(0xc0210f0c), long"
        (ureg[6:0]=113=USTAT2, d=0, g=0, l=1) -- previously refused
        outright as "unsupported Type14a odd UREG pair". USTAT1/USTAT2 is
        one of Table 2-3's complementary pairs (PRM p.2-12), not a
        register-file neighbor pair, so a (LW) load fills only the named
        register (p.2-9: "USTAT1 = DM (LW address); /* Loads only USTAT1
        in SISD mode */"); a store still writes both (same page: "DM (LW
        address) = USTAT1; /* Stores both USTAT1 and USTAT2 */"). A UREG
        with no pair at all (PC here) instead replicates its one value
        into both halves on a store (p.2-9/2-10's "Uncomplementary Ureg to
        Memory LW Transfers") and, symmetrically, a load fills only the
        named register.
        """
        memory = loader_memory(loader_block(0, 0x400, 8, payload=b"\0" * 8))
        fields = {
            "addr[31:16]": 0,
            "addr[15:0]": 0x400,
            "g": 0,
            "l": 1,
        }

        store = self.run_one(
            T.State(
                10,
                {
                    T.UREG_CODES["USTAT1"]: T.Const(0x11111111),
                    T.UREG_CODES["USTAT2"]: T.Const(0x22222222),
                },
                concrete=memory,
                assume_nw32=True,
            ),
            insn("14a", {**fields, "d": 1, "ureg[6:0]": T.UREG_CODES["USTAT2"]}, 6),
        )
        self.assertIsNone(store.stopped)
        self.assertEqual(store.trace[0]["ureg_pair"], ["USTAT2", "USTAT1"])
        self.assertEqual(T._dm_read(store, 0x400, 4), T.Const(0x22222222))
        self.assertEqual(T._dm_read(store, 0x404, 4), T.Const(0x11111111))

        # Each State has its own write overlay (concrete memory is
        # immutable and shared), so seed this fresh state's own overlay
        # directly rather than relying on the store above.
        load_state = T.State(10, {}, concrete=memory, assume_nw32=True)
        self.assertTrue(T._dm_write(load_state, 0x400, 4, T.Const(0x22222222)))
        self.assertTrue(T._dm_write(load_state, 0x404, 4, T.Const(0x11111111)))
        load = self.run_one(
            load_state,
            insn("14a", {**fields, "d": 0, "ureg[6:0]": T.UREG_CODES["USTAT2"]}, 6),
        )
        self.assertIsNone(load.stopped)
        # Only the named register is filled -- USTAT1 is left untouched.
        self.assertEqual(load.trace[0]["ureg_pair"], ["USTAT2"])
        self.assertEqual(load.uregs[T.UREG_CODES["USTAT2"]], T.Const(0x22222222))
        self.assertNotIn(T.UREG_CODES["USTAT1"], load.uregs)

        pc_memory = loader_memory(loader_block(0, 0x500, 8, payload=b"\0" * 8))
        pc_store = self.run_one(
            T.State(
                10,
                {T.UREG_CODES["PC"]: T.Const(0x33333333)},
                concrete=pc_memory,
                assume_nw32=True,
            ),
            insn(
                "14a",
                {
                    **fields,
                    "addr[15:0]": 0x500,
                    "d": 1,
                    "ureg[6:0]": T.UREG_CODES["PC"],
                },
                6,
            ),
        )
        self.assertIsNone(pc_store.stopped)
        self.assertEqual(pc_store.trace[0]["ureg_pair"], ["PC", "PC"])
        # No real neighbor: the one value is replicated into both halves.
        self.assertEqual(T._dm_read(pc_store, 0x500, 4), T.Const(0x33333333))
        self.assertEqual(T._dm_read(pc_store, 0x504, 4), T.Const(0x33333333))

        pc_load_state = T.State(10, {}, concrete=pc_memory, assume_nw32=True)
        self.assertTrue(T._dm_write(pc_load_state, 0x500, 4, T.Const(0x33333333)))
        self.assertTrue(T._dm_write(pc_load_state, 0x504, 4, T.Const(0x99999999)))
        pc_load = self.run_one(
            pc_load_state,
            insn(
                "14a",
                {
                    **fields,
                    "addr[15:0]": 0x500,
                    "d": 0,
                    "ureg[6:0]": T.UREG_CODES["PC"],
                },
                6,
            ),
        )
        self.assertIsNone(pc_load.stopped)
        self.assertEqual(pc_load.trace[0]["ureg_pair"], ["PC"])
        # The high half (0x99999999) is never consulted for an unpaired
        # UREG's load.
        self.assertEqual(pc_load.uregs[T.UREG_CODES["PC"]], T.Const(0x33333333))

    def test_type7d_aconv_matches_prm_worked_example(self):
        # out/refs/sharc-plus-prm p.351 ACONV Example: "I0 = B2W(I2);" and
        # "IF AV B4 = W2B(B1);" -- is is the source register number within
        # its class; the destination is is XOR idis, as in Type7a/Type19a.
        b2w = self.run_one(
            T.State(1, {T.UREG_CODES["I2"]: T.Const(0x400)}),
            insn(
                "7d",
                {
                    "g": 0,
                    "is[2:2]": 0,
                    "is[1:0]": 2,
                    "breg": 0,
                    "toby": 0,
                    "idis[2:0]": 2,
                },
                6,
            ),
        )
        self.assertEqual(b2w.uregs[T.UREG_CODES["I0"]], T.Const(0x100))
        self.assertEqual(
            b2w.trace[0],
            {
                "pc_sw": 1,
                "form": "7d",
                "action": "aconv",
                "direction": "b2w",
                "source": "I2",
                "destination": "I0",
                "value": 0x100,
                "semantics": "prm-likely",
            },
        )

        w2b = self.run_one(
            T.State(1, {T.UREG_CODES["B1"]: T.Const(0x40)}),
            insn(
                "7d",
                {
                    "g": 0,
                    "is[2:2]": 0,
                    "is[1:0]": 1,
                    "breg": 1,
                    "toby": 1,
                    "idis[2:0]": 5,
                },
                6,
            ),
        )
        self.assertEqual(w2b.uregs[T.UREG_CODES["B4"]], T.Const(0x100))
        self.assertEqual(w2b.trace[0]["direction"], "w2b")
        self.assertEqual(w2b.trace[0]["source"], "B1")
        self.assertEqual(w2b.trace[0]["destination"], "B4")

    def test_type7d_exact_bytes_propagate_symbolic_addresses_conservatively(self):
        # Hand-built pure-ACONV byte fixtures. The first is I7 = B2W(I7);
        # the second exercises g=1, B class, source B9 and XOR destination
        # B12: B12 = W2B(B9). They are decoder fixtures, not firmware claims.
        b2w = T.decode_at(bytes.fromhex("bf0480c00000"), 0, 0)
        w2b = T.decode_at(bytes.fromhex("fe04805d0000"), 0, 0)
        compute_bearing = T.decode_at(bytes.fromhex("bf0480c00100"), 0, 0)
        self.assertEqual(
            (b2w.raw, b2w.type_name, b2w.fields),
            (
                0x04BFC0800000,
                "7d",
                {
                    "g": 0,
                    "is[2:2]": 1,
                    "is[1:0]": 3,
                    "breg": 0,
                    "toby": 0,
                    "idis[2:0]": 0,
                },
            ),
        )
        self.assertEqual(
            (w2b.raw, w2b.type_name, w2b.fields),
            (
                0x04FE5D800000,
                "7d",
                {
                    "g": 1,
                    "is[2:2]": 0,
                    "is[1:0]": 1,
                    "breg": 1,
                    "toby": 1,
                    "idis[2:0]": 5,
                },
            ),
        )
        # PIN_FIELDS admits only pure ACONV here; PRM optional-compute Type7d
        # rows must not silently execute through this bounded handler.
        self.assertNotEqual(compute_bearing.type_name, "7d")

        # A divisible affine B2W has an exact affine result.  An unaligned
        # affine source instead becomes an opaque symbol: B2W is not linear
        # over unknown low bits and the address map may raise ILAD.
        exact = self.run_one(
            T.State(1, {T.UREG_CODES["I7"]: T.Affine(0x20, (("byte_base", 4),))}),
            b2w,
        )
        self.assertEqual(
            exact.uregs[T.UREG_CODES["I7"]], T.Affine(8, (("byte_base", 1),))
        )
        symbolic = self.run_one(T.State(1, {T.UREG_CODES["I7"]: T.symbol("B7e")}), b2w)
        self.assertEqual(
            symbolic.uregs[T.UREG_CODES["I7"]],
            T.symbol("aconv_b2w_23_1_0_B7e_1"),
        )
        self.assertEqual(symbolic.trace[0]["semantics"], "prm-likely")

        banked = self.run_one(
            T.State(1, {T.UREG_CODES["B9"]: T.Affine(3, (("word_base", 1),))}),
            w2b,
        )
        self.assertEqual(
            banked.uregs[T.UREG_CODES["B12"]], T.Affine(12, (("word_base", 4),))
        )
        self.assertEqual(banked.trace[0]["direction"], "w2b")

    def test_type7d_firmware_instance_stops_without_a_concrete_source(self):
        # Unknown remains a hard stop: neither address-map conversion nor its
        # ILAD outcome is modeled by the bounded tracer.
        fields = {
            "g": 0,
            "is[2:2]": 1,
            "is[1:0]": 3,
            "breg": 0,
            "toby": 0,
            "idis[2:0]": 0,
        }
        stopped = self.run_one(T.State(1), insn("7d", fields, 6))
        self.assertEqual(stopped.stopped, "Type7d B2W(I7) source is not concrete")

    def test_type14d_short_word_store_and_zero_extended_load(self):
        # out/refs/sharc-plus-prm pp.384-386: w=0,ex=0,l=1 is (sw)/(sw) BH
        # (store) / BHSE with x=0 (zero-extend load). Firmware instances
        # 0x1c8119 (store) and 0x1c811c (load) share DM 0x269454.
        fields = {
            "ex": 0,
            "l": 1,
            "w": 0,
            "x": 0,
            "dreg[3:0]": 2,
            "addr[31:16]": 0x26,
            "addr[15:0]": 0x9454,
        }
        memory = loader_memory(loader_block(1, 0x269454, 4, payload=b"\0" * 4))
        stored = self.run_one(
            T.State(1, {2: T.Const(0x1234ABCD)}, concrete=memory),
            insn("14d", {**fields, "d": 1}, 6),
        )
        self.assertEqual(stored.trace[0]["access_width"], "short-word")
        self.assertTrue(stored.trace[0]["concrete_write"])
        self.assertEqual(T._dm_read(stored, 0x269454, 2), T.Const(0xABCD))

        memory = loader_memory(
            loader_block(1, 0x269454, 2, payload=struct.pack("<H", 0xBEEF))
        )
        loaded = self.run_one(
            T.State(1, concrete=memory), insn("14d", {**fields, "d": 0}, 6)
        )
        self.assertEqual(loaded.uregs[2], T.Const(0xBEEF))
        self.assertEqual(loaded.trace[0]["access_width"], "short-word")

    def test_type14d_byte_sign_and_zero_extended_loads(self):
        # Firmware instance 0xb8cdaf: w=0,ex=0,l=0,x=1 is (bwse), a
        # sign-extended byte load (p.386 BHSE Encode Table).
        fields = {
            "ex": 0,
            "l": 0,
            "w": 0,
            "d": 0,
            "dreg[3:0]": 2,
            "addr[31:16]": 0x2D,
            "addr[15:0]": 0x722C,
        }
        memory = loader_memory(loader_block(1, 0x2D722C, 1, payload=bytes([0x80])))
        signed = self.run_one(
            T.State(1, concrete=memory), insn("14d", {**fields, "x": 1}, 6)
        )
        self.assertEqual(signed.uregs[2], T.Const(0xFFFFFF80))
        self.assertEqual(signed.trace[0]["access_width"], "byte-sign-extended")

        memory = loader_memory(loader_block(1, 0x2D722C, 1, payload=bytes([0x80])))
        unsigned = self.run_one(
            T.State(1, concrete=memory), insn("14d", {**fields, "x": 0}, 6)
        )
        self.assertEqual(unsigned.uregs[2], T.Const(0x80))
        self.assertEqual(unsigned.trace[0]["access_width"], "byte")

    def test_type14d_stops_on_exclusive_access_and_undocumented_encodings(self):
        base = {
            "d": 0,
            "l": 0,
            "x": 0,
            "dreg[3:0]": 0,
            "addr[31:16]": 0,
            "addr[15:0]": 0,
        }
        exclusive = self.run_one(T.State(1), insn("14d", {**base, "ex": 1, "w": 1}, 6))
        self.assertEqual(exclusive.stopped, "unsupported Type14d exclusive access")

        undocumented_w = self.run_one(
            T.State(1), insn("14d", {**base, "ex": 0, "w": 1}, 6)
        )
        self.assertEqual(
            undocumented_w.stopped,
            "undocumented Type14d encoding (w=1, ex=0)",
        )

        undocumented_store_x = self.run_one(
            T.State(1),
            insn("14d", {**base, "ex": 0, "w": 0, "d": 1, "x": 1}, 6),
        )
        self.assertEqual(
            undocumented_store_x.stopped,
            "undocumented Type14d store encoding (x=1)",
        )

    def test_type15a_symbolic_load_leaves_the_i_register_unmodified(self):
        # out/refs/sharc-plus-prm p.388: "The I register is pre-modified
        # with an immediate value ... The I register is not updated,"
        # unlike Type19a's post-modify MODIFY.
        loaded = self.run_one(
            T.State(1, {16: T.symbol("buf")}),
            insn(
                "15a",
                {
                    "g": 0,
                    "i[2:0]": 0,
                    "d": 0,
                    "l": 0,
                    "ureg[6:0]": 7,
                    "addr[31:16]": 0,
                    "addr[15:0]": 0x18,
                },
                6,
            ),
        )
        self.assertEqual(loaded.trace[0]["expression"], "buf + 0x18")
        self.assertEqual(loaded.uregs[7], T.Unknown("memory-address buf + 0x18"))
        self.assertEqual(loaded.uregs[16], T.symbol("buf"))

    def test_type15a_dm_store_and_pm_dag2_load(self):
        # Under --assume-32bit-normal-words the <data32> displacement is in
        # byte-addressed-space normal-word units, so it is scaled by 4 (out/
        # refs/sharc-plus-prm Table 6-2, p.6-10/6-11) -- the same scaling
        # Type4a/4b/15b already apply to their own immediate modifiers. The
        # store lands at I0 + 0x1000*4 = 0x6000, not I0 + 0x1000.
        memory = loader_memory(loader_block(1, 0x6000, 4, payload=b"\0" * 4))
        stored = self.run_one(
            T.State(
                1,
                {16: T.Const(0x2000), 5: T.Const(0xCAFEBABE)},
                concrete=memory,
                assume_nw32=True,
            ),
            insn(
                "15a",
                {
                    "g": 0,
                    "i[2:0]": 0,
                    "d": 1,
                    "l": 0,
                    "ureg[6:0]": 5,
                    "addr[31:16]": 0,
                    "addr[15:0]": 0x1000,
                },
                6,
            ),
        )
        self.assertTrue(stored.trace[0]["concrete_write"])
        self.assertEqual(T._dm_read(stored, 0x6000, 4), T.Const(0xCAFEBABE))
        # Pre-modify only: I0 keeps its value.
        self.assertEqual(stored.uregs[16], T.Const(0x2000))

        loaded_pm = self.run_one(
            T.State(1, {T.UREG_CODES["I11"]: T.Const(5)}),
            insn(
                "15a",
                {
                    "g": 1,
                    "i[2:0]": 3,
                    "d": 0,
                    "l": 0,
                    "ureg[6:0]": 0,
                    "addr[31:16]": 0,
                    "addr[15:0]": 0x10,
                },
                6,
            ),
        )
        self.assertEqual(loaded_pm.trace[0]["space"], "PM")
        self.assertIsInstance(loaded_pm.uregs[0], T.Unknown)

    def test_type15a_forced_long_word_register_pair(self):
        # Scaled the same way as the plain-register case above: I0 + 0x100*4
        # = 0x700, not I0 + 0x100. The (lw) pair still steps its second word
        # by a fixed 4 bytes (out/refs/sharc-plus-prm p.6-15's (lw) row scales
        # the same as an unqualified access, not by 8), independent of this
        # displacement scaling.
        memory = loader_memory(loader_block(1, 0x700, 8, payload=b"\0" * 8))
        long_word = self.run_one(
            T.State(
                1,
                {16: T.Const(0x300), 6: T.Const(0x11111111), 7: T.Const(0x22222222)},
                concrete=memory,
                assume_nw32=True,
            ),
            insn(
                "15a",
                {
                    "g": 0,
                    "i[2:0]": 0,
                    "d": 1,
                    "l": 1,
                    "ureg[6:0]": 6,
                    "addr[31:16]": 0,
                    "addr[15:0]": 0x100,
                },
                6,
            ),
        )
        self.assertEqual(T._dm_read(long_word, 0x700, 4), T.Const(0x11111111))
        self.assertEqual(T._dm_read(long_word, 0x704, 4), T.Const(0x22222222))
        self.assertEqual(long_word.trace[0]["access_width"], "long-word")
        self.assertEqual(long_word.trace[0]["simd_companion_possible"], False)

        long_word.pc_sw = 1
        loaded_pair = self.run_one(
            long_word,
            insn(
                "15a",
                {
                    "g": 0,
                    "i[2:0]": 0,
                    "d": 0,
                    "l": 1,
                    "ureg[6:0]": 10,
                    "addr[31:16]": 0,
                    "addr[15:0]": 0x100,
                },
                6,
            ),
        )
        self.assertEqual(loaded_pair.uregs[10], T.Const(0x11111111))
        self.assertEqual(loaded_pair.uregs[11], T.Const(0x22222222))

        # R7 (explicit, odd) pairs with R6, not R8 (PRM p.6-5's odd
        # DAG-register case): no concrete memory here, only the pairing.
        odd_pair = self.run_one(
            T.State(1, {16: T.Const(0x300)}),
            insn(
                "15a",
                {
                    "g": 0,
                    "i[2:0]": 0,
                    "d": 0,
                    "l": 1,
                    "ureg[6:0]": 7,
                    "addr[31:16]": 0,
                    "addr[15:0]": 0x100,
                },
                6,
            ),
        )
        self.assertIsNone(odd_pair.stopped)
        self.assertEqual(odd_pair.trace[0]["ureg_pair"], ["R7", "R6"])
        self.assertIsInstance(odd_pair.uregs[7], T.Unknown)
        self.assertIsInstance(odd_pair.uregs[6], T.Unknown)

    def test_type4a_store_and_type15a_load_of_the_same_normal_word_slot(self):
        """A Type4a store and a Type15a load of the same architectural
        word offset from the same I register must hit the same byte
        address once --assume-32bit-normal-words is in effect.

        This reproduces the DT2 1.16 wavetable stage-6 kernel's own local-
        variable re-read: Type4a stores DM(I6-4)=R0 at sw 0x1cbf72 (a 6-bit
        signed word offset of -4), and Type15a later loads
        R12=DM(0xfffffffc,I6) at sw 0x1cbff1 (a 32-bit displacement that
        also decodes to -4). Per out/refs/sharc-plus-prm's "Enhanced Modify
        Instruction for Address Scaling" (pp.6-9-6-10) and Table 6-2
        (p.6-10/6-11), both are normal-word displacements into byte-
        addressed space and both must be scaled by 4 -- so both
        instructions must resolve to the same byte address, I6-16.
        """
        i6 = T.UREG_CODES["I6"]
        state = T.State(
            1,
            {i6: T.Const(0x90001000), 0: T.Const(0xCAFEBABE)},
            concrete=loader_memory(),
            assume_nw32=True,
        )
        store_fields = {
            "i[2:0]": 6,
            "g": 0,
            "d": 1,
            "cond[4:0]": 0x1F,
            "data[5:5]": 1,
            "data[4:0]": 0x1C,  # 6-bit signed -4
            "dreg[3:0]": 0,
            "u": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        stored = self.run_one(state, insn("4a", store_fields, 6))
        self.assertEqual(stored.trace[0]["address"], 0x90001000 - 16)
        self.assertTrue(stored.trace[0]["concrete_write"])
        # I6 is unchanged: the frame pointer stays valid for the later load.
        self.assertEqual(stored.uregs[i6], T.Const(0x90001000))

        load_fields = {
            "g": 0,
            "i[2:0]": 6,
            "d": 0,
            "l": 0,
            "ureg[6:0]": 12,
            "addr[31:16]": 0xFFFF,
            "addr[15:0]": 0xFFFC,  # 32-bit displacement, also decodes to -4
        }
        loaded = self.run_one(stored, insn("15a", load_fields, 6))
        self.assertEqual(loaded.trace[0]["address"], 0x90001000 - 16)
        self.assertEqual(loaded.uregs[12], T.Const(0xCAFEBABE))

    def test_type4a_store_and_type15b_load_of_the_same_normal_word_slot(self):
        """Type15b's own (non-lw) immediate modifier was already scaled the
        same way as Type4a's (tools/sharc_trace.py's existing assume_nw32
        handling for name == "15b"); this guards that the two stay in
        agreement now that Type15a's data32 is scaled too."""
        i6 = T.UREG_CODES["I6"]
        state = T.State(
            1,
            {i6: T.Const(0x2000), 3: T.Const(0x11223344)},
            concrete=loader_memory(),
            assume_nw32=True,
        )
        store_fields = {
            "i[2:0]": 6,
            "g": 0,
            "d": 1,
            "cond[4:0]": 0x1F,
            "data[5:5]": 0,
            "data[4:0]": 5,  # +5 words
            "dreg[3:0]": 3,
            "u": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        stored = self.run_one(state, insn("4a", store_fields, 6))
        self.assertEqual(stored.trace[0]["address"], 0x2000 + 20)

        load_fields = {
            "i[2:0]": 6,
            "g": 0,
            "d": 0,
            "l": 0,
            "ureg[6:0]": 4,
            "data[6:0]": 5,  # same +5 word offset, Type15b's own field name
        }
        loaded = self.run_one(stored, insn("15b", load_fields, 4))
        self.assertEqual(loaded.trace[0]["address"], 0x2000 + 20)
        self.assertEqual(loaded.uregs[4], T.Const(0x11223344))

    def test_type15b_forced_long_word_register_pair(self):
        """Type15b's (lw) is the same register-pair access as Type15a's own
        (lw) (out/refs/sharc-plus-prm p.391: "if the instruction type uses
        optional forced long word modifier (LW)... register pair access is
        done"), not a single wider (8-byte) transfer into one register --
        sharc_core.memory._dm_read intentionally refuses width > 4 ("a
        register pair, which this tracer does not model"), so before this
        every (lw) Type15b load/store read/wrote Unknown at every site,
        including docs/findings/06's voice render polyphase interpolator's
        own coefficient-table reads (FUN_1c4f81, sw 0x1c50b9 etc., all (lw)
        Type15b). The displacement is scaled the same as an unqualified
        access (p.6-10's Table 6-2, cited in Type15a's own docstring), not
        by 8, independent of l -- I0 + 5*4 = 0x2000 + 20, then the pair's
        own second word is a fixed +4 (0x2000 + 24)."""
        i6 = T.UREG_CODES["I0"]
        state = T.State(
            1,
            {i6: T.Const(0x2000)},
            concrete=loader_memory(
                loader_block(1, 0x2014, 8, payload=b"\x44\x33\x22\x11\x88\x77\x66\x55")
            ),
            assume_nw32=True,
        )
        load_fields = {
            "i[2:0]": 0,
            "g": 0,
            "d": 0,
            "l": 1,
            "ureg[6:0]": 4,
            "data[6:0]": 5,  # +5 words, same scaling as the non-lw case above
        }
        loaded = self.run_one(state, insn("15b", load_fields, 4))
        self.assertEqual(loaded.uregs[4], T.Const(0x11223344))
        self.assertEqual(loaded.uregs[5], T.Const(0x55667788))
        self.assertEqual(loaded.trace[0]["access_width"], "long-word")

        # R7 (explicit, odd) pairs with R6, not R8: no concrete data backs
        # this address, only the pairing.
        odd_pair = self.run_one(
            T.State(1, {i6: T.Const(0x2000)}, concrete=loader_memory()),
            insn(
                "15b",
                {"i[2:0]": 0, "g": 0, "d": 0, "l": 1, "ureg[6:0]": 7, "data[6:0]": 5},
                4,
            ),
        )
        self.assertIsNone(odd_pair.stopped)
        self.assertEqual(odd_pair.trace[0]["ureg_pair"], ["R7", "R6"])
        self.assertIsInstance(odd_pair.uregs[7], T.Unknown)
        self.assertIsInstance(odd_pair.uregs[6], T.Unknown)

        store_state = T.State(
            1,
            {i6: T.Const(0x2000), 4: T.Const(0x11223344), 5: T.Const(0x55667788)},
            concrete=loader_memory(),
            assume_nw32=True,
        )
        stored = self.run_one(
            store_state,
            insn(
                "15b",
                {"i[2:0]": 0, "g": 0, "d": 1, "l": 1, "ureg[6:0]": 4, "data[6:0]": 5},
                4,
            ),
        )
        self.assertEqual(T._dm_read(stored, 0x2014, 4), T.Const(0x11223344))
        self.assertEqual(T._dm_read(stored, 0x2018, 4), T.Const(0x55667788))

    def test_pm_normal_word_load_into_px_splits_loader_backed_48_bits(self):
        address = T.L1_BLOCK3_NW_BASE + 0x20
        byte_address = L.sw_to_byte(T.L1_BLOCK3_SW_BASE) + 6 * 0x20
        payload = struct.pack("<3H", 0x1234, 0x5678, 0x9ABC)
        memory = loader_memory(
            loader_block(1, byte_address, len(payload), payload=payload)
        )
        fields = {
            "addr[31:16]": address >> 16,
            "addr[15:0]": address & 0xFFFF,
            "g": 1,
            "d": 0,
            "l": 0,
            "ureg[6:0]": T.UREG_CODES["PX"],
        }
        loaded = self.run_one(T.State(10, concrete=memory), insn("14a", fields, 6))
        self.assertEqual(loaded.uregs[T.UREG_CODES["PX1"]], T.Const(0x9ABC0000))
        self.assertEqual(loaded.uregs[T.UREG_CODES["PX2"]], T.Const(0x12345678))
        self.assertIsInstance(loaded.uregs[T.UREG_CODES["PX"]], T.Unknown)
        self.assertEqual(
            loaded.trace[-1]["concrete_value"],
            {"PX1": 0x9ABC0000, "PX2": 0x12345678},
        )

        missing = self.run_one(
            T.State(
                10,
                {
                    T.UREG_CODES["PX"]: T.Const(1),
                    T.UREG_CODES["PX1"]: T.Const(2),
                    T.UREG_CODES["PX2"]: T.Const(3),
                },
                concrete=memory,
            ),
            insn(
                "14a",
                {**fields, "addr[15:0]": (address + 1) & 0xFFFF},
                6,
            ),
        )
        self.assertIsInstance(missing.uregs[T.UREG_CODES["PX"]], T.Unknown)
        self.assertIsInstance(missing.uregs[T.UREG_CODES["PX1"]], T.Unknown)
        self.assertIsInstance(missing.uregs[T.UREG_CODES["PX2"]], T.Unknown)

        dm_loaded = self.run_one(
            T.State(10, concrete=memory),
            insn("14a", {**fields, "g": 0}, 6),
        )
        self.assertEqual(dm_loaded.uregs[T.UREG_CODES["PX1"]], T.Const(0x9ABC0000))
        self.assertEqual(dm_loaded.uregs[T.UREG_CODES["PX2"]], T.Const(0x12345678))

    def test_type3b_pm_px_load_uses_normal_word_alias_and_post_modifies(self):
        address = T.L1_BLOCK3_NW_BASE + 0x21
        byte_address = L.sw_to_byte(T.L1_BLOCK3_SW_BASE) + 6 * 0x21
        payload = struct.pack("<3H", 0xABCD, 0x0123, 0x4567)
        memory = loader_memory(
            loader_block(1, byte_address, len(payload), payload=payload)
        )
        fields = {
            "u": 1,
            "i[2:0]": 0,
            "m[2:0]": 6,
            "cond[4:0]": 31,
            "g": 1,
            "d": 0,
            "l": 0,
            "ureg[6:0]": T.UREG_CODES["PX"],
            "w": 1,
            "x": 1,
        }
        state = T.State(
            10,
            {24: T.Const(address), 46: T.Const(1)},
            concrete=memory,
        )
        loaded = self.run_one(state, insn("3b", fields))
        self.assertEqual(loaded.uregs[24], T.Const(address + 1))
        self.assertEqual(loaded.uregs[T.UREG_CODES["PX1"]], T.Const(0x45670000))
        self.assertEqual(loaded.uregs[T.UREG_CODES["PX2"]], T.Const(0xABCD0123))
        self.assertEqual(loaded.trace[-1]["access_width"], "normal-word")

    def test_type25_negative_pcrel_target_wraps_24bit_current_sw_address(self):
        call = insn(
            "25a_pcrel",
            {"reladdr[23:16]": 0x80, "reladdr[15:0]": 0},
            6,
        )
        state = self.run_one(T.State(0x100), call)
        state = self.run_one(state, insn("21c", {}, length=2))
        state = self.run_one(state, insn("21c", {}, length=2))
        self.assertEqual(state.stopped, "external-call")
        self.assertEqual(state.trace[-1]["target_sw"], 0x800100)

        dt2_116 = insn(
            "25a_pcrel",
            {"reladdr[23:16]": 0x9C, "reladdr[15:0]": 0x843D},
            6,
        )
        state = self.run_one(T.State(0x1C0FA5), dt2_116)
        state = self.run_one(state, insn("21c", {}, length=2))
        state = self.run_one(state, insn("21c", {}, length=2))
        self.assertEqual(state.trace[-1]["target_sw"], 0xB893E2)


class DmCanonicalizationTest(unittest.TestCase):
    """``_canonical_dm_address``'s raw-vs-``SW_ALIAS_BASE`` family choice and
    ``dm_write_range``, the helper watchpoint/poke code should use to find
    where a write to an architectural DM address actually lands.

    SHARC+ byte address space is one universal address map (SHARC+ Core
    Programming Reference, out/refs/sharc-plus-prm, "Byte Address Space
    Overview of Data Accesses", p.7-4/extraction page 221): the same bytes
    back every access width at a given address, so which physical location
    -- raw, or the loader's SW_ALIAS_BASE-relative mirror -- a DM pointer
    resolves to must not depend on how wide the particular access happens
    to be.
    """

    ADDR = 0x1000

    def test_family_choice_is_width_independent(self):
        # ADDR has exactly one real byte at the RAW location (not the
        # alias) -- an "already mapped direct address" per
        # _canonical_dm_address's docstring -- so every width must agree
        # it is the raw family, not just the width that happens to match
        # what's actually backed there.
        memory = loader_memory(loader_block(1, self.ADDR, 1, payload=b"\x99"))
        state = T.State(1, concrete=memory)
        self.assertEqual(T._canonical_dm_address(state, self.ADDR, 1), self.ADDR)
        for width in (1, 2, 4):
            with self.subTest(width=width):
                self.assertEqual(
                    T._canonical_dm_address(state, self.ADDR, width, for_write=True),
                    self.ADDR,
                )

    def test_family_choice_agrees_between_a_narrow_write_and_a_wide_read(self):
        # A 1-byte write to a fresh (nowhere-backed) low address aliases,
        # like any other unmapped low pointer; a subsequent differently
        # sized access to the very same address must resolve to the same
        # physical family the write used, not silently split across two
        # locations.
        memory = loader_memory()
        state = T.State(1, concrete=memory)
        self.assertTrue(T._dm_write(state, self.ADDR, 1, T.Const(0xAB)))
        alias = L.SW_ALIAS_BASE + self.ADDR
        self.assertIn(alias, state.overlay)
        for width in (1, 2, 4):
            with self.subTest(width=width):
                self.assertEqual(
                    T._canonical_dm_address(state, self.ADDR, width, for_write=True),
                    alias,
                )

    def test_dm_write_range_aliases_an_unmapped_low_address(self):
        memory = loader_memory()
        state = T.State(1, concrete=memory)
        alias = L.SW_ALIAS_BASE + self.ADDR
        self.assertEqual(T.dm_write_range(state, self.ADDR, 4), (alias, alias + 4))

    def test_dm_write_range_keeps_an_already_mapped_direct_address(self):
        memory = loader_memory(loader_block(1, 0x310CA300, 4, payload=b"\0" * 4))
        state = T.State(1, concrete=memory)
        self.assertEqual(
            T.dm_write_range(state, 0x310CA300, 4), (0x310CA300, 0x310CA304)
        )

    def test_dm_write_range_none_without_concrete_memory(self):
        self.assertIsNone(T.dm_write_range(T.State(1), self.ADDR, 4))

    def test_watchpoint_on_raw_or_aliased_address_sees_an_aliased_write(self):
        # _dm_write redirects a store to an unmapped low DM pointer through
        # SW_ALIAS_BASE. sr.Watchpoint canonicalises its range at attach
        # time, so a watch on the raw address fires, and so does one built
        # from dm_write_range's (already canonical) range.
        sr = import_module("sharc_run")
        memory = loader_memory()
        runner = sr.Runner(
            memory, 0x10, watchpoints=[sr.Watchpoint(self.ADDR, self.ADDR + 4)]
        )
        with self.assertRaises(sr._WatchpointStop):
            T._dm_write(runner.state, self.ADDR, 4, T.Const(0x11223344))
        start, end = T.dm_write_range(runner.state, self.ADDR, 4)
        runner.attach_watchpoints([sr.Watchpoint(start, end)])
        with self.assertRaises(sr._WatchpointStop):
            T._dm_write(runner.state, self.ADDR, 4, T.Const(0x55667788))


if __name__ == "__main__":
    unittest.main()
