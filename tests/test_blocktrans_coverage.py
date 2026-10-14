"""blocktrans_coverage: log parsing, reason families and ranking (no emulator)."""

import importlib.util
import sys

from support.paths import REPO_ROOT

TOOL = REPO_ROOT / "tools" / "blocktrans_coverage.py"
_spec = importlib.util.spec_from_file_location("blocktrans_coverage", TOOL)
bc = importlib.util.module_from_spec(_spec)
sys.modules["blocktrans_coverage"] = bc
_spec.loader.exec_module(bc)

LOG = """\
fast: noise line
cov built root=0xb8276c insns=40 kernel_ops=900 windows=3 pcs=0xb8276c,0xb8276d
cov stop root=0xb8276c at=0xb827d3 form=9b_abs why=insn 0 (9b_abs): form 9b_abs
cov built root=0x1c5765 insns=2 kernel_ops=10 windows=0 pcs=0x1c5765
cov stop root=0x1c5765 at=0x1c579f form=3b why=cannot be lowered in this region
cov deny root=0x1c5765 at=0x1c579f form=3b why=0x1c579f: insn 3 (3b): 3b access width
cov refused root=0xb8b526 why=the entry instruction cannot run here: insn 0 (2a_short): recips in a region with kernel flags
cov run root=0xb8276c built=1 refused=0 calls=10 insns=8460 declined=[0, 0, 0, 0, 0] exits=0xb827d3:stop:10:8460:0 declwhy=
cov run root=0x1c5765 built=1 refused=0 calls=4 insns=100 declined=[0, 3, 0, 0, 0] exits=0x1c579f:stop:1:100:26 declwhy=3x req: known r#
"""


def test_parse_log():
    roots = bc.parse_log(LOG)
    r = roots[0xB8276C]
    assert r.built and r.insns == 40 and r.pcs == [0xB8276C, 0xB8276D]
    assert r.calls == 10 and r.exits[0]["n"] == 10 and r.exits[0]["insns"] == 8460
    q = roots[0x1C5765]
    assert q.declined["req"] == 3 and q.decl_why == {"req: known r#": 3}
    assert q.exits[0]["gap"] == 26
    assert roots[0xB8B526].refused.startswith("the entry instruction")


def test_classify():
    fam, _, form = bc.classify("0x1c579f: insn 3 (3b): 3b access width")
    assert fam.startswith("3b/4b access width") and form == "3b"
    assert bc.classify("insn 0 (9b_abs): form 9b_abs")[0] == "unsupported form 9b_abs"
    assert bc.classify("call")[0] == "CALL not lowered"
    assert bc.classify("loop at 0x1c26c5: end not decoded")[0].startswith("loop shape")
    assert bc.classify("0xb8: insn 1 (3a): address range unknown")[0] == (
        "address range unknown"
    )
    assert bc.inner_pc("0x1c579f: insn 3 (3b): x", 7) == 0x1C579F
    assert bc.inner_pc("plain", 7) == 7


class FakeDb:
    def insn(self, sw):
        return ("9b_abs", "RETURN") if sw == 0xB827D3 else ("3b", "R2 = DM(I4, M7)")

    def text(self, sw):
        return self.insn(sw)[1]

    def is_return(self, sw):
        return self.text(sw) == "RETURN"

    def func(self, sw):
        return None

    def block(self, sw):
        return None


def test_rows_and_families():
    roots = bc.parse_log(LOG)
    db = FakeDb()
    rows = [
        bc.analyse_root(roots[0xB8276C], 50.0, 100.0, 2.0, db),
        bc.analyse_root(roots[0x1C5765], 40.0, 100.0, 2.0, db),
        bc.analyse_root(roots[0xB8B526], 20.0, 100.0, 2.0, db),
    ]
    assert rows[0]["translated_fraction"] == 1.0 and bc.fully_covered(rows[0])
    assert not bc.fully_covered(rows[1])
    # 3 of 4 calls declined on req; the one that ran covered 100 of 126.
    assert rows[1]["decline_fraction"] == 0.75
    assert rows[1]["first_stop"]["family"].startswith("entry decline: register unknown")
    assert rows[2]["blocked_ms"] == 20.0
    fam = bc.families(rows)["by_first_stop"]
    names = [f["family"] for f in fam]
    assert names[0].startswith("entry decline") and "flag-forgetting op" in names
    assert all(f["first_stop_ms"] >= 0 for f in fam)
