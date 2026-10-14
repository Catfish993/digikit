"""The core/dsp-voice test rig: stand-in addresses, `Rig` and `stops_at`."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tools.patchkit import pk_sharc  # noqa: E402

CORE = ROOT / "modules" / "core" / "dsp-voice" / "src" / "dsp" / "dsp-voice.s"
SINE = ROOT / "modules" / "test" / "sine-voice" / "src" / "dsp" / "sine_voice.s"
CORE_BASE, SINE_BASE = 0xB90000, 0xB92000
STOCK_BP = 0xB9F000  # stands in for the stock renderer's second instruction
RETURN_SW = 0xB9F100  # the caller's return point
# byte addresses of the stand-ins (L2), one 4 KiB page each
TYPE_CACHE, FRAME, RECORDS, OUT, TABLE, STATE, STACK = (
    0x20091000,
    0x20092000,
    0x20093000,
    0x20098000,
    0x2009A000,
    0x2009B000,
    0x2009C000,
)
REC = 0x1D8
TYPE = 8


def nw(byte: int) -> int:
    return byte // 4


class Rig:
    def __init__(self) -> None:
        self.core = pk_sharc.assemble(CORE.read_text())
        self.sine = pk_sharc.assemble(SINE.read_text())
        self.sine_linked = None
        ext_core = {
            "_fw_type_cache": TYPE_CACHE,
            "_fw_render_next": STOCK_BP,
            "_fw_frame": FRAME,
            "_dv_table": TABLE,
        }
        # the sine links against the core's symbols; the core against the table address
        core_syms = pk_sharc.link(self.core, CORE_BASE, ext_core).symbols
        self.core_linked = pk_sharc.link(self.core, CORE_BASE, ext_core)
        ext_sine = {
            "_dv_exp2": core_syms["_dv_exp2"],
            "_dv_frame_hw": core_syms["_dv_frame_hw"],
            "_sv_state": STATE,
        }
        self.sine_linked = pk_sharc.link(self.sine, SINE_BASE, ext_sine)
        self.render_pc = self.sine_linked.symbols["_sv_render"]
        self.entry = "_dv_entry"
        self.dm: dict[int, int] = {}
        self.reset()

    def reset(self) -> None:
        self.dm = {}
        for k in range(34):
            self.dm[nw(TABLE) + k] = 0
        for k in range(32):
            self.dm[nw(STATE) + k] = 0
        for k in range(8):
            self.dm[nw(TYPE_CACHE) + k] = 0
        for k in range(256):
            self.dm[nw(FRAME) + k] = 0
        for k in range(32 * REC // 4):
            self.dm[nw(RECORDS) + k] = 0
        self.dm[nw(TABLE)] = TYPE
        self.dm[nw(TABLE) + 1] = self.render_pc

    def hw(self, k: int, v: int) -> None:
        word = nw(FRAME) + k // 2
        cur = self.dm.get(word, 0)
        sh = 16 * (k % 2)
        self.dm[word] = (cur & ~(0xFFFF << sh) & 0xFFFFFFFF) | ((v & 0xFFFF) << sh)

    def set_type(self, track: int, t: int) -> None:
        word = nw(TYPE_CACHE) + track // 2
        cur = self.dm.get(word, 0)
        sh = 16 * (track % 2)
        self.dm[word] = (cur & ~(0xFFFF << sh)) | (t << sh)

    def set_note(
        self, track: int, note: float, tune: float = 64.0, vel: int = 100
    ) -> None:
        self.hw(1 + track, int(note * 256))
        self.hw(0x6D + 0x30 * track, int(tune * 256))
        self.hw(0x1A + track, vel << 8)

    def rec_word(self, v: int, off: int, value: int) -> None:
        self.dm[nw(RECORDS + v * REC + off)] = value

    def call(
        self,
        v: int,
        regs: dict | None = None,
        extra_reads: dict | None = None,
        max_steps: int = 20000,
    ) -> dict:
        r4 = RECORDS + v * REC
        r8 = OUT + v * 0x80
        saved = {f"R{n}": 0x7000 + n for n in (3, 5, 6, 7, 9, 10, 11, 13, 15)}
        saved.update(
            {
                "I0": 0x20000010,
                "I1": 0x20000020,
                "I2": 0x20000030,
                "I3": 0x20000040,
                "I5": 0x20000050,
            }
        )
        saved.update(
            {"M1": 11, "M2": 12, "M3": 13, "I8": 0x20000080, "I9": 0x20000090, "M8": 5}
        )
        r = {
            "R4": r4,
            "R8": r8,
            "R12": 32,
            "R14": 32 - v,
            "M5": 0,
            "M6": 1,
            "M7": -1,
            "M13": 0,
            "M14": 1,
            "M15": -1,
            "I6": STACK,
            "I7": STACK,
            **saved,
            **(regs or {}),
        }
        self.dm[nw(STACK) - 1] = (
            RETURN_SW - 1
        )  # the return address the CJUMP pushed: [I6 - 1]
        self.dm[nw(STACK)] = 0x20000FF0  # the caller's frame pointer: [I6]
        reads = {"out": (nw(OUT + v * 0x80), 32), "table": (nw(TABLE), 4)}
        reads.update(extra_reads or {})
        out = pk_sharc.run_call(
            self.core_linked,
            self.entry,
            regs=r,
            dm_words=dict(self.dm),
            reads=reads,
            return_sw=RETURN_SW,
            others=(self.sine_linked,),
            read_regs=(
                "I6",
                "I7",
                "I4",
                "M5",
                "M6",
                "M7",
                "I8",
                "I9",
                "M8",
                "R3",
                "R5",
                "R15",
                "I0",
                "I5",
                "M1",
                "M3",
            ),
            max_steps=max_steps,
            extra_breaks=(STOCK_BP,),
        )
        out["in"] = r
        return out


def stops_at(out: dict, sw: int) -> bool:
    return out["halt"]["reason"] == "breakpoint" and out["final_pc_sw"] == sw
