"""The fit rule: does a module, from the needs its .dkm header declares, fit the capacity of a device (docs/plans/what-fits.md)?

The rule is written three times and kept equal by one set of vectors (sdk/fit-vectors.json): `dkrt_sdk::fit` (Rust: the runtime and the DIGIKIT
menu), apps/web/src/modules/dkfit.ts (the host app) and this file (dev tools: `dk run`, `module:check`).

`needs` is `dkupload.dkm_needs(header)` plus `file_size`; `caps` is `Uploader.capacity()`: kind name -> {limit, used, largest}.
`fits_now` returns None, or the first short resource as (name, need, free). `take` returns the capacity left once the module is loaded, and
`fits_set` loads several in turn (the sum rule: it takes each module's needs and does not model fragmentation, so it is conservative).
"""

from __future__ import annotations

from typing import Any

HDR = 16  # the arena's block header

Caps = dict[str, dict[str, int]]
Short = tuple[str, int, int]

# the order the first short resource is looked for in: the loader's own order of refusals, with the slots first
ORDER = (
    "STAGE",
    "MODULES",
    "SELECTION",
    "DSP_AREA",
    "ARENA",
    "VOICE_ROWS",
    "MACHINE_ROWS",
    "PARAM_ROWS",
    "PAGE_ROWS",
    "FX_SLOTS",
)
ROWS = ("VOICE_ROWS", "MACHINE_ROWS", "PARAM_ROWS", "PAGE_ROWS", "FX_SLOTS")


def _cap(caps: Caps, kind: str) -> dict[str, int]:
    return caps.get(kind) or {"limit": 0, "used": 0, "largest": 0}


def _free(c: dict[str, int]) -> int:
    return max(c["limit"] - c["used"], 0)


def dsp_need(needs: dict[str, int]) -> int:
    """DSP area bytes with the room a placement can lose to alignment (up to ALIGN - 8 bytes)."""
    n = needs.get("DSP_AREA", 0)
    return n + max(needs.get("DSP_ALIGN", 0), 8) - 8 if n else 0


def fits_now(needs: dict[str, int], caps: Caps) -> Short | None:
    """None when the module fits the capacity, else (resource, need, free) of the first short resource."""
    for kind in ORDER:
        c = _cap(caps, kind)
        if kind == "STAGE":
            need, free = needs.get("file_size", 0), c["largest"]
        elif kind in ("MODULES", "SELECTION"):
            need, free = 1, _free(c)
        elif kind == "DSP_AREA":
            need, free = dsp_need(needs), min(_free(c), c["largest"])
        elif kind == "ARENA":
            # the module's block and the block of its DSP image are in the arena together until the transfer ends
            total = needs.get("ARENA", 0) + needs.get("ARENA_XFER", 0)
            block = max(needs.get("ARENA", 0), needs.get("ARENA_XFER", 0))
            if total > _free(c):
                return (kind, total, _free(c))
            need, free = block, c["largest"] + HDR
        else:
            need, free = needs.get(kind, 0), _free(c)
        if need > free:
            return (kind, need, free)
    return None


def take(needs: dict[str, int], caps: Caps) -> Caps:
    """A copy of CAPS with the module's needs taken out (what is left once it is loaded)."""
    out = {k: dict(v) for k, v in caps.items()}

    def use(kind: str, n: int) -> None:
        c = out.setdefault(kind, {"limit": 0, "used": 0, "largest": 0})
        c["used"] += n
        c["largest"] = max(c["largest"] - n, 0)

    use("MODULES", 1)
    use("SELECTION", 1)
    use("ARENA", needs.get("ARENA", 0) + needs.get("ARENA_XFER", 0))
    use("DSP_AREA", dsp_need(needs))
    for kind in ROWS:
        use(kind, needs.get(kind, 0))
    return out


def fits_set(
    needs: list[dict[str, int]], caps: Caps
) -> tuple[int, str, int, int] | None:
    """Load the modules in order: None when all fit, else (index, resource, need, free) of the first that does not."""
    for i, n in enumerate(needs):
        short = fits_now(n, caps)
        if short:
            return (i, *short)
        caps = take(n, caps)
    return None


def describe(short: Short) -> str:
    """One line for a person: `DSP area 400 KiB needed, 254 KiB free`."""
    name, need, free = short
    label = {
        "STAGE": "file",
        "MODULES": "module records",
        "SELECTION": "selection paths",
        "DSP_AREA": "DSP area",
        "ARENA": "memory",
        "VOICE_ROWS": "voice rows",
        "MACHINE_ROWS": "machine rows",
        "PARAM_ROWS": "parameter rows",
        "PAGE_ROWS": "page rows",
        "FX_SLOTS": "effect slots",
    }[name]
    if name in ("STAGE", "DSP_AREA", "ARENA"):
        return f"{label} {_size(need, True)} needed, {_size(free, False)} free"
    return f"{label} {need} needed, {free} free"


def _size(n: int, up: bool) -> str:
    """`400 KiB` (rounded up for what is needed, down for what is free); bytes below 1 KiB."""
    if n < 1024:
        return f"{n} bytes"
    return f"{-(-n // 1024) if up else n // 1024} KiB"


def vector_caps(base: dict[str, Any], over: dict[str, Any] | None) -> Caps:
    """The capacity of a vector: BASE ([limit, used, largest] per kind) with OVER laid over it."""
    merged = {**base, **(over or {})}
    return {
        k: {"limit": v[0], "used": v[1], "largest": v[2]} for k, v in merged.items()
    }
