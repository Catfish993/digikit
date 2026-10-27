"""ColdFire patchkit toolchain: gcc (m68k-elf, -mcpu=54418) -> object prototype -> tools/patchkit/pk_link.py -> our decoder
-> the native ColdFire core runs the function on a DT2 1.17 ready state.

Skips when the m68k toolchain (tools/patchkit/pk_toolchains.py m68k) or the 1.17 image is absent."""

import json
import zlib

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.firmware import config  # noqa: E402
from tools.patchkit import (  # noqa: E402
    pk_link,  # noqa: E402
    pk_toolchains,  # noqa: E402
)

SYX = config.stock("Digitakt_II_OS1.17.syx")
SRC = ROOT / "tests" / "patchkit" / "pk_src"
BASE = 0x47E8B000  # spike 0.3 expansion region (DT2)
DATA = 0x47EC0000  # scratch data inside the region
# Firmware 1.17 entries (tools/coldfire/cf_names_dt2_1_17.py, tools/coldfire/cfdb.py): object accessors at +0x10 and +0x0c.
SYMS = {
    "fw_flag_bit": 0x40114A28,
    "fw_field_0c": 0x40114984,
    "active_kit_getter": 0x40041D24,
}

needs_gcc = pytest.mark.skipif(
    pk_toolchains.m68k_bin("gcc") is None,
    reason="m68k toolchain not installed (tools/patchkit/pk_toolchains.py m68k)",
)


@pytest.fixture(scope="module")
def module():
    obj, linked, hist = pk_link.build([SRC / "cksum.c", SRC / "stub.s"], BASE, SYMS)
    return obj, linked, hist


@needs_gcc
def test_object_prototype_is_self_contained_json(module):
    obj, linked, _ = module
    again = json.loads(json.dumps(obj))
    relinked = pk_link.link(again, BASE, SYMS)
    assert relinked.image == linked.image and relinked.symbols == linked.symbols
    assert (
        obj["toolchain"]["gcc"].startswith("m68k-elf-gcc")
        and len(obj["source_sha256"]) == 64
    )
    # a different base moves only the relocated words
    moved = pk_link.link(again, BASE + 0x1000, SYMS)
    assert moved.image != linked.image and len(moved.image) == len(linked.image)


@needs_gcc
def test_relocations_seen_and_all_instructions_are_isa_a_b_c(module):
    _, linked, hist = module
    assert {"32"} <= set(
        linked.relocs_applied
    )  # absolute jsr/data references (firmware calls, .data/.bss)
    assert {"byterev", "bitrev", "ff1"} <= set(
        hist
    )  # the assembly stub's ISA_C instructions
    assert sum(hist.values()) > 20


@needs_gcc
def test_decoder_rejects_a_68020_form():
    asm = ROOT / "out" / "cache" / "pk_cfcall" / "bad020.s"
    asm.parent.mkdir(parents=True, exist_ok=True)
    asm.write_text(".text\n.globl f\nf:\n  bfextu (%a0){4:8},%d0\n  rts\n")
    import subprocess

    obj = asm.with_suffix(".o")
    r = subprocess.run(
        [str(pk_toolchains.m68k_bin("as")), "-m68020", "-o", str(obj), str(asm)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    # No sources: the file does not assemble for ColdFire, so its dependency listing would fail.
    o = pk_link.object_from_elf(obj.read_bytes(), [])
    with pytest.raises(pk_link.PkLinkError, match="outside ISA_A/B/C"):
        pk_link.check_isa(pk_link.link(o, BASE))


@needs_gcc
def test_unresolved_extern_is_an_error(module):
    obj, _, _ = module
    with pytest.raises(pk_link.PkLinkError, match="unresolved extern symbol"):
        pk_link.link(obj, BASE, {})


@needs_gcc
def test_pc_relative_relocations():
    obj, linked, _ = pk_link.build(
        [SRC / "pcrel.s"], BASE, {**SYMS, "near_ext": BASE + 0x100}
    )
    assert linked.relocs_applied == {"PC32": 1, "PC16": 1}
    # bsr.l to 0x40114984 from 0x47e8b00a: displacement is relative to the extension word's address
    image = linked.image
    i = image.index(bytes.fromhex("61ff")) + 2
    assert int.from_bytes(image[i : i + 4], "big", signed=True) == SYMS[
        "fw_field_0c"
    ] - (BASE + i)
    with pytest.raises(pk_link.PkLinkError, match="out of range"):
        pk_link.link(obj, BASE, {**SYMS, "near_ext": SYMS["fw_field_0c"]})


@pytest.mark.skipif(
    not SYX.exists(), reason="firmware/Digitakt_II_OS1.17.syx not present"
)
@needs_gcc
class TestOnTheCore:
    @pytest.fixture(scope="class")
    @classmethod
    def machine(cls):
        from tools.patchkit.pk_cfcall import CfMachine

        return CfMachine(SYX)

    @staticmethod
    def pokes(linked, extra=None):
        image = {
            linked.base: linked.image,
            linked.base + len(linked.image): b"\0" * linked.bss_size,
        }
        return {**image, **(extra or {})}

    def test_adler32_matches_zlib(self, machine, module):
        _, linked, _ = module
        data = bytes((i * 37 + 11) & 0xFF for i in range(1000))
        r = machine.call(
            linked.symbols["pk_adler32"],
            [DATA, len(data)],
            self.pokes(linked, {DATA: data}),
            reads=[(DATA, 4)],
        )
        assert r.ok and r.clobbered == [] and r.d0 == zlib.adler32(data)
        assert r.insns > 1000

    def test_data_and_bss_survive_between_calls(self, machine, module):
        _, linked, _ = module
        call = {
            "pc": linked.symbols["pk_bump"],
            "args": [3],
            "reads": [(linked.symbols["pk_scratch"], 16)],
        }
        res = machine.run(self.pokes(linked), [call, {**call, "args": [4]}])
        assert [x.d0 for x in res] == [8, 12]  # counter starts at 5 in .data
        assert res[1].reads[0] == b"".join(v.to_bytes(4, "big") for v in (12, 0, 0, 0))

    def test_calls_a_firmware_function_through_an_extern(self, machine, module):
        _, linked, _ = module
        obj = DATA
        for w10, w0c in ((0x01, 0x1234), (0x09, 7), (0x08, 0), (0x00, 5)):
            blob = b"\0" * 12 + w0c.to_bytes(4, "big") + w10.to_bytes(4, "big")
            direct = machine.call(SYMS["fw_flag_bit"], [obj], {obj: blob})
            direct2 = machine.call(SYMS["fw_field_0c"], [obj], {obj: blob})
            r = machine.call(
                linked.symbols["pk_call_fw"], [obj], self.pokes(linked, {obj: blob})
            )
            want = ((direct2.d0 << 1) | (direct.d0 & 0xFF)) & 0xFFFFFFFF
            assert r.ok and r.clobbered == [] and r.d0 == want
            flag = (
                ((w10 >> 3) ^ 1) & 1 if w10 & 1 else 0
            )  # python model of the firmware accessor
            assert direct.d0 & 0xFF == flag and direct2.d0 == w0c

    def test_firmware_getter_with_pointer_result(self, machine, module):
        _, linked, _ = module
        direct = machine.call(SYMS["active_kit_getter"], [0x44F00000])
        r = machine.call(
            linked.symbols["pk_kit_plus"], [0x44F00000, 0x10], self.pokes(linked)
        )
        assert (
            direct.ok
            and r.ok
            and direct.d0 == 0x44F000F4
            and r.d0 == direct.d0 + 0x10
            and r.clobbered == []
        )

    def test_pc32_call_to_the_firmware(self, machine):
        obj, linked, _ = pk_link.build(
            [SRC / "pcrel.s"], BASE, {**SYMS, "near_ext": BASE + 0x100}
        )
        blob = b"\0" * 12 + (0xCAFE).to_bytes(4, "big")
        r = machine.call(
            linked.symbols["pk_far"], [DATA], self.pokes(linked, {DATA: blob})
        )
        assert r.ok and r.clobbered == [] and r.d0 == 0xCAFE

    def test_assembly_stub(self, machine, module):
        _, linked, _ = module
        for x in (0x12345678, 0, 0x80000000, 1):
            r = machine.call(linked.symbols["pk_rev"], [x], self.pokes(linked))
            rev = int.from_bytes(x.to_bytes(4, "big"), "little")
            bitrev = int(f"{rev:032b}"[::-1], 2)
            ff1 = 32 if bitrev == 0 else 32 - bitrev.bit_length()
            assert r.ok and r.d0 == (rev + ff1) & 0xFFFFFFFF
