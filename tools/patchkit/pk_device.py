"""Device facts for the patchkit tools, read from the device files (`devices/*.toml`) instead of being repeated in code.

    from tools.patchkit import pk_device
    s = pk_device.store()        # the module store's geometry (absolute sectors), the same on every device
    pk_device.digikit_devices()  # short names of the devices that have a module store

The digikit runtime is one image for all devices, so a store fact it uses must be equal in every device file; `store()` raises
when two files differ (the same rule `patchkit gen` applies to the runtime's constants, docs/design/device-data.md).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@cache
def load_all(root: Path = ROOT) -> dict[str, dict]:
    """short name -> the parsed device file (files without a `[device]` table, the LED maps, are skipped)."""
    out = {}
    for f in sorted((root / "devices").glob("*.toml")):
        d = tomllib.loads(f.read_text())
        if "device" in d:
            out[d["device"]["short"]] = d
    return out


def load(short: str) -> dict:
    try:
        return load_all()[short]
    except KeyError:
        raise KeyError(f"no device file for {short!r}: {sorted(load_all())}") from None


def digikit_devices() -> list[str]:
    """Devices with a `[card.store]` and a `[digikit]` section."""
    return [
        s
        for s, d in load_all().items()
        if "digikit" in d and "store" in d.get("card", {})
    ]


@dataclass(frozen=True)
class Store:
    first: int  # first card sector of the store
    sectors: int  # sectors in the range, with the selection record at the end
    sel_a: int  # selection record copy A (absolute)
    sel_b: int  # selection record copy B (absolute)
    header_b: int  # store header copy B (absolute); the store's files end before it

    @property
    def limit(self) -> int:
        """Sectors a store (header A, table, data) may use from `first`."""
        return self.header_b - self.first


def store_of(short: str) -> Store:
    s = load(short)["card"]["store"]
    first, end = s["first_sector"], s["end_sector"]
    sel = s.get("selection_sectors", 2)
    return Store(first, end - first, end - sel, end - sel + 1, s["header_b_sector"])


def store() -> Store:
    """The store geometry that every digikit device agrees on."""
    devs = digikit_devices()
    if not devs:
        raise RuntimeError("no device file has a [card.store]")
    geo = {d: store_of(d) for d in devs}
    first = geo[devs[0]]
    for d, g in geo.items():
        if g != first:
            raise RuntimeError(
                f"the module store differs between {devs[0]} and {d}: {first} / {g}; the runtime is one image for all devices"
            )
    return first


def working_project_sector() -> int:
    """First sector of the working project record (`[[card.regions]]` role `working-project`), the same on every digikit device:
    the card above it is the firmware's (project slots follow at the first project pool sector)."""
    found = {}
    for d in digikit_devices():
        rs = [
            r["sector"]
            for r in load(d)["card"].get("regions", [])
            if r["role"] == "working-project"
        ]
        if len(rs) != 1:
            raise RuntimeError(
                f"{d}: expected one working-project region, found {len(rs)}"
            )
        found[d] = rs[0]
    if len(set(found.values())) != 1:
        raise RuntimeError(
            f"the working-project region differs between devices: {found}"
        )
    return next(iter(found.values()))


def port_images(short: str) -> tuple[str, ...]:
    """Image keys (`dt2-1.17`) of the firmware of a device that has a port lockfile, in device-file order."""
    return tuple(
        f"{short}-{fw['version']}"
        for fw in load(short).get("firmware", [])
        if fw.get("port")
    )
