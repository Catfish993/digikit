"""The permanent machine type numbers (modules/core/machines/types.toml) and `pk_new.allocate`."""

from __future__ import annotations

import tomllib
from pathlib import Path

from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.patchkit import pk_new  # noqa: E402

TYPES = ROOT / "modules" / "core" / "machines" / "types.toml"


def machine_entries() -> list[tuple[str, str, list[str]]]:
    out = []
    for m in sorted((ROOT / "modules").rglob("module.toml")):
        d = tomllib.loads(m.read_text())
        for e in d.get("entry", []):
            if e["registry"] == "machines":
                # a module without a device filter is meant for the devices its [targets] name
                on = {k.split("-")[0] for k in d.get("targets", {})}
                out.append((d["module"]["id"], e["id"], e.get("devices") or sorted(on)))
    return out


def test_every_machine_entry_has_a_number_per_device() -> None:
    t = tomllib.loads(TYPES.read_text())
    for mod, eid, devs in machine_entries():
        for dev in devs:
            assert f"{mod}:{eid}" in t[dev]["alloc"], f"{mod}:{eid} on {dev}"


def test_numbers_are_unique_in_range_and_never_stock() -> None:
    t = tomllib.loads(TYPES.read_text())
    for dev, first in pk_new.STOCK_TYPES.items():
        nums = list(t[dev]["alloc"].values())
        assert len(nums) == len(set(nums)), dev
        assert all(first <= n <= t[dev]["last"] for n in nums), dev
    assert t["dt2"]["last"] < 24  # the DSP selector table was read to entry 23


def test_allocate_takes_the_next_number_and_keeps_existing_ones(tmp_path: Path) -> None:
    p = tmp_path / "types.toml"
    p.write_text(TYPES.read_text())
    before = tomllib.loads(p.read_text())
    got = pk_new.allocate("machines/foo:foo", ["dt2", "dn2"], p)
    after = tomllib.loads(p.read_text())
    for dev in ("dt2", "dn2"):
        top = max(before[dev]["alloc"].values())
        assert got[dev] == top + 1
        assert after[dev]["alloc"]["machines/foo:foo"] == top + 1
        for k, v in before[dev]["alloc"].items():
            assert after[dev]["alloc"][k] == v
    assert pk_new.allocate("machines/foo:foo", ["dt2"], p) == {"dt2": got["dt2"]}
