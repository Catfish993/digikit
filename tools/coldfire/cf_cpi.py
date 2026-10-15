"""Cycles per instruction of the firmware's executed mix, from the manual's timing tables.

Input: the per-scenario JSON that a `cf-mix` build of coupled-run writes (`DIGI_CF_MIX_OUT=DIR`,
packages/emulator/boot/src/cfmix.rs): every executed PC with its decoded instruction and exact run
counts per execution path (interpreted, fused loop, analytic idle skip, RAM-clear batch).

    uv run python -m tools.coldfire.cf_cpi out/clock/cpi/mix/*.json --out out/clock/cpi.json

Model (tools/coldfire/cf_timing.py): the manual's C(R/W) per instruction at zero wait states, no
dual issue credited (an upper bound on cycles), Bcc cost from a 128-entry 2-bit prediction table
simulated in the counter, pipeline stalls for the previous instruction's register used as an address
(adjacent instructions only: a lower bound on stalls) and the exposed EMAC pipeline. Classes: `idle`
(the PCs the analytic idle skip fast-forwards), `clear` (the reset RAM-clear loop), `busy` (all
other, interpreted or fused).
"""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import defaultdict

from tools.coldfire import cf_timing

CORE_HZ = 264e6  # 2 x the measured 132 MHz bus clock (docs/findings/03-ui-and-panel.md)
EMU_IPS = 132e6  # the emulator's instruction rate: one instruction per timer tick
STOPS = {
    "jump",
    "call",
    "ret",
    "trap",
    "halt",
}  # no fall-through to the next instruction


def region_of(page: int) -> str:
    """Operand-reference bucket of a 64 KiB page (page = addr >> 16)."""
    a = page << 16
    if 0x4000_0000 <= a < 0x4800_0000:
        return "ddr"  # ACR0 (0x4007e020): copyback
    if 0x8000_0000 <= a < 0x9000_0000:
        return "sram"  # RAMBAR valid, data bus: single cycle
    if 0x4800_0000 <= a < 0x8000_0000:
        return "ci_ram"  # mapped by the emulator above the 128 MiB DDR, cache-inhibited by default
    if a >= 0xC000_0000:
        return "peripheral"
    return "other"


def new_class() -> dict:
    return {
        "instructions": 0,
        "cycles_lo": 0,
        "cycles": 0,  # nominal, without stalls
        "cycles_hi": 0,
        "stall_cycles": 0,
        "bytes": 0,
        "table_reads": 0,
        "table_writes": 0,
        "mispredicts": 0,
    }


def analyze(mix: dict) -> dict:
    rows = {r["pc"]: r for r in mix["insns"]}
    idle_pcs = {pc for pc, r in rows.items() if r["n"][2] > 0}
    clear_pcs = {pc for pc, r in rows.items() if r["n"][3] > 0}

    def klass(pc: int) -> str:
        return "idle" if pc in idle_pcs else "clear" if pc in clear_pcs else "busy"

    cls = defaultdict(new_class)
    unmapped: dict[str, int] = defaultdict(int)
    by_id: dict[str, dict] = defaultdict(lambda: {"n": 0, "cycles": 0})
    by_path = [0, 0, 0, 0]
    interp_busy = 0
    for pc, r in rows.items():
        n = sum(r["n"])
        for i, v in enumerate(r["n"]):
            by_path[i] += v
        k = klass(pc)
        if k == "busy":
            interp_busy += r["n"][0]
        t = cf_timing.timing(r) if r.get("id") else None
        c = cls[k]
        c["instructions"] += n
        c["bytes"] += n * r.get("len", 0)
        if t is None:
            unmapped[r.get("id") or "undecoded"] += n
            continue
        c["table_reads"] += t.r * n
        c["table_writes"] += t.w * n
        if r["id"] == "bcc":
            taken = r["taken"]
            lo, nom = cf_timing.bcc_cycles(taken, n - taken, r["miss"])
            hi = (n - taken) + 8 * taken
            c["mispredicts"] += r["miss"]
        else:
            lo = nom = t.c * n
            hi = t.worst() * n
        c["cycles_lo"] += lo
        c["cycles"] += nom
        c["cycles_hi"] += max(hi, nom)
        by_id[r["id"]]["n"] += n
        by_id[r["id"]]["cycles"] += nom

    # Pipeline stalls between adjacent instructions that run one after the other.
    stall_pairs = 0
    for pc, a in rows.items():
        if not a.get("id") or a["flow"] in STOPS:
            continue
        n = sum(a["n"])
        fall = n - a["taken"] if a["id"] == "bcc" else n
        b = rows.get(pc + a["len"])
        if b is None or not b.get("id") or fall <= 0:
            continue
        pairs = min(fall, sum(b["n"]))
        s = cf_timing.stall_cycles(a, b)
        if s and pairs:
            cls[klass(b["pc"])]["stall_cycles"] += s * pairs
            stall_pairs += pairs

    # Instruction working set: 16-byte lines (the I-cache is 8 KiB, 4-way, 512 lines) needed to
    # cover a share of the executed busy instruction bytes. Static footprint, not a cache model.
    weight: dict[int, int] = defaultdict(int)
    for pc, r in rows.items():
        if klass(pc) == "busy" and r.get("len"):
            weight[pc >> 4] += sum(r["n"]) * r["len"]
    ordered = sorted(weight.values(), reverse=True)
    total_w = sum(ordered) or 1
    coverage = {}
    acc = 0
    for want in (0.5, 0.9, 0.99, 0.999):
        while ordered and acc < want * total_w:
            acc += ordered.pop(0)
            coverage.setdefault(want, 0)
            coverage[want] += 1
        coverage.setdefault(want, 0)
    icache = {
        "distinct_lines": len(weight),
        "lines_for_share": {str(k): v for k, v in coverage.items()},
    }

    seconds = mix["guest_instructions"] / EMU_IPS
    busy = cls["busy"]
    work = cls["clear"]["cycles"] + cls["clear"]["stall_cycles"]
    busy_cycles = busy["cycles"] + busy["stall_cycles"]
    busy_instr = busy["instructions"]
    cpi = busy_cycles / busy_instr if busy_instr else 0.0
    cpi_lo = busy["cycles_lo"] / busy_instr if busy_instr else 0.0
    cpi_hi = (
        (busy["cycles_hi"] + busy["stall_cycles"]) / busy_instr if busy_instr else 0.0
    )
    total_instr = sum(c["instructions"] for c in cls.values())
    total_cycles = sum(c["cycles"] + c["stall_cycles"] for c in cls.values())
    refs_pages: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for page, rd, wr in mix["pages"]:
        refs_pages[region_of(page)][0] += rd
        refs_pages[region_of(page)][1] += wr
    interp_instr = max(
        1, by_path[0] - sum(rows[p]["n"][0] for p in idle_pcs | clear_pcs)
    )
    out = {
        "scenario": mix["scenario"],
        "emulated_seconds": seconds,
        "guest_instructions": mix["guest_instructions"],
        "check_sum_instructions": total_instr,
        "softfloat_hle_calls": mix["softfloat_hle_calls"],
        "paths": dict(zip(mix["paths"], by_path, strict=True)),
        "classes": {k: dict(v) for k, v in cls.items()},
        "unmapped": dict(unmapped),
        "stall_pairs": stall_pairs,
        "busy": {
            "instructions": busy_instr,
            "cpi": cpi,
            "cpi_lo": cpi_lo,
            "cpi_hi": cpi_hi,
            "instr_per_s_at_264m": CORE_HZ / cpi if cpi else None,
            "instr_per_s_lo_cpi": CORE_HZ / cpi_lo if cpi_lo else None,
            "instr_per_s_hi_cpi": CORE_HZ / cpi_hi if cpi_hi else None,
            "instructions_per_emulated_s": busy_instr / seconds,
            "cycles_per_emulated_s": (busy_cycles + work) / seconds,
            "core_load": (busy_cycles + work) / seconds / CORE_HZ,
            "core_load_hi": (busy["cycles_hi"] + busy["stall_cycles"] + work)
            / seconds
            / CORE_HZ,
        },
        "all": {"instructions": total_instr, "cpi": total_cycles / total_instr},
        "idle_cpi": (cls["idle"]["cycles"] / cls["idle"]["instructions"])
        if cls["idle"]["instructions"]
        else None,
        "refs": {
            "bus_data_refs_interpreted": {
                k: v for k, v in zip(mix["regions"], mix["data_refs"], strict=True)
            },
            "by_region_64k_pages": {
                k: {"reads": v[0], "writes": v[1]} for k, v in refs_pages.items()
            },
            "interpreted_busy_instructions": interp_instr,
            "fetch_bytes_busy": busy["bytes"],
            "table_reads_busy": busy["table_reads"],
            "table_writes_busy": busy["table_writes"],
        },
        "icache_lines": icache,
        "ctrl": mix.get("ctrl"),
        "top_ids": sorted(
            ({"id": k, **v} for k, v in by_id.items()), key=lambda x: -x["cycles"]
        )[:12],
    }
    return out


def miss_table(res: dict) -> dict:
    """Instructions per second for 0/2/5 % miss rates and five DDR line-fill penalties.

    The miss rate is per cacheable reference: instruction longword fetches (executed bytes / 4)
    plus operand references to DDR (ACR0, copyback). Penalties are core cycles from the DDR timing
    registers the bootstrap programs (CL 4, tRCD 4, tRP 4 DDR clocks, 8-bit datapath, so a 16-byte
    line is 8 data clocks). The bootstrap sets MISCCR2[DDR2CLK], so the DDR clock is the VCO / 2 =
    fsys = 264 MHz at the least (OUTDIV1 >= 1), one core cycle per DDR clock: 6 = row open and
    critical word first (4 + 2), 12 = row open, full line (4 + 8), 20 = row closed, full line
    (4 + 4 + 4 + 8). 24 and 40 are the same two with a 132 MHz DDR clock (DDR2CLK = 0, not what the
    bootstrap programs). No bus or crossbar latency is added (the manual gives no number).
    """
    busy = res["busy"]
    n = busy["instructions"]
    ref = res["refs"]
    bus = ref["bus_data_refs_interpreted"]
    pages = ref["by_region_64k_pages"]
    ddr_total = sum(pages.get("ddr", {"reads": 0, "writes": 0}).values())
    interp = max(1, ref["interpreted_busy_instructions"])
    ddr_per_interp = ddr_total / interp
    fetch_per_instr = ref["fetch_bytes_busy"] / 4 / n if n else 0.0
    refs_per_instr = fetch_per_instr + ddr_per_interp
    out = {
        "ddr_refs_per_busy_instr": ddr_per_interp,
        "fetch_longwords_per_busy_instr": fetch_per_instr,
        "cacheable_refs_per_busy_instr": refs_per_instr,
        "bus_total_refs": {k: sum(v) for k, v in bus.items()},
        "cases": [],
    }
    # Miss rate at which the real core is exactly saturated (load 1.0), per penalty.
    headroom = (
        CORE_HZ / busy["instructions_per_emulated_s"] - busy["cpi"]
    )  # cycles/instr
    out["break_even_miss_rate"] = {
        str(p): headroom / (refs_per_instr * p) for p in (6, 12, 20, 24, 40)
    }
    for penalty in (6, 12, 20, 24, 40):
        for rate in (0.0, 0.02, 0.05):
            stall = refs_per_instr * rate * penalty
            cpi = busy["cpi"] + stall
            out["cases"].append(
                {
                    "penalty_cycles": penalty,
                    "miss_rate": rate,
                    "cpi": cpi,
                    "instr_per_s": CORE_HZ / cpi,
                    "core_load": busy["cycles_per_emulated_s"] / CORE_HZ
                    + busy["instructions_per_emulated_s"] * stall / CORE_HZ,
                }
            )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mix", nargs="+", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    results = []
    for path in args.mix:
        res = analyze(json.loads(path.read_text()))
        res["memory"] = miss_table(res)
        results.append(res)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"core_hz": CORE_HZ, "scenarios": results}, indent=1)
    )
    for r in results:
        b = r["busy"]
        print(
            f"{r['scenario']:<20} busy {b['instructions'] / r['emulated_seconds'] / 1e6:6.1f} Minstr/s"
            f"  CPI {b['cpi']:.3f} [{b['cpi_lo']:.3f}..{b['cpi_hi']:.3f}]"
            f"  {b['instr_per_s_at_264m'] / 1e6:6.1f} Minstr/s @264M"
            f"  load {100 * b['core_load']:5.1f}%"
        )


if __name__ == "__main__":
    main()
