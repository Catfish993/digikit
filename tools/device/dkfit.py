"""The fit rule: does a module, from the needs inferred from its .dkm, fit the capacity of a device (docs/plans/what-fits.md)?

The compare is written three times and kept equal by one set of vectors (sdk/fit-vectors.json): `dkrt_fit` (Rust: the DIGIKIT menu),
apps/web/src/modules/dkfit.ts (the host app) and this file (dev tools: `dk run`, `module:check`). The needs themselves are computed once, in Rust
(packages/dkneeds), not here.

`needs` is `dkneeds.needs_of(file)` (it holds `file_size`); `caps` is `Uploader.capacity()`: kind name -> {limit, used, largest}.
`fits_now` returns None, or the first short resource as (name, need, free); a kind that CAPS lacks is not judged. `take` returns the capacity left once the module is loaded, and
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


def _free(c: dict[str, int]) -> int:
    return max(c["limit"] - c["used"], 0)


def dsp_need(needs: dict[str, int]) -> int:
    """DSP area bytes with the room a placement can lose to alignment (up to ALIGN - 8 bytes)."""
    n = needs.get("DSP_AREA", 0)
    return n + max(needs.get("DSP_ALIGN", 0), 8) - 8 if n else 0


def fits_now(needs: dict[str, int], caps: Caps) -> Short | None:
    """None when the module fits the capacity, else (resource, need, free) of the first short resource."""
    for kind in ORDER:
        c = caps.get(kind)
        if c is None:  # a kind the capacity lacks (an older runtime): not judged
            continue
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


def empty_caps(caps: Caps) -> Caps:
    """CAPS as the next boot sees the runtime: nothing used, the whole limit free (the module set is fixed per session, so a selection loads into an
    empty runtime). The arena starts one block header short of its limit and the DSP area 8 bytes short (its first range starts at an offset that is not
    8-aligned); the staging buffer is not a resource that a load uses up."""
    out = {k: dict(v) for k, v in caps.items()}
    for kind, c in out.items():
        if kind == "STAGE":
            continue
        c["used"] = 0
        c["largest"] = c["limit"] - {"ARENA": HDR, "DSP_AREA": 8}.get(kind, 0)
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


# the resources the load line shows, in this order, with the label the device shows (dkrt_fit `LINE`)
LINE = (
    ("DSP_AREA", "DSP"),
    ("ARENA", "CPU"),
)


def _percent(used: int, limit: int) -> int:
    """USED as a percent of LIMIT, rounded up: a set that takes anything never reads 0 %, and a resource reads 100 % only when it is full."""
    return -(-min(used, limit) * 100 // limit)


def load_line(caps: Caps) -> str:
    """The load line of the DIGIKIT menu's MODULES page (`load_line` of modules/core/runtime/fit): always the DSP memory and then the CPU memory of CAPS, which is
    the capacity once the ticked modules are taken out of an empty one, as `DSP 52% CPU 1%`; a resource that CAPS lacks or that has no limit is left out."""
    parts = []
    for kind, label in LINE:
        c = caps.get(kind)
        if c is None or c["limit"] == 0:
            continue
        parts.append(f"{label} {_percent(c['used'], c['limit'])}%")
    return " ".join(parts)


def describe(short: Short) -> str:
    """One line for a person: `DSP memory 400 KiB needed, 254 KiB free`."""
    name, need, free = short
    label = {
        "STAGE": "file",
        "MODULES": "module records",
        "SELECTION": "selection paths",
        "DSP_AREA": "DSP memory",
        "ARENA": "CPU memory",
        "VOICE_ROWS": "voice rows",
        "MACHINE_ROWS": "machine rows",
        "PARAM_ROWS": "parameter rows",
        "PAGE_ROWS": "page rows",
        "FX_SLOTS": "effect slots",
    }[name]
    if name in ("STAGE", "DSP_AREA", "ARENA"):
        room = "at most" if name == "STAGE" else "free"
        return f"{label} {_size(need, True)} needed, {_size(free, False)} {room}"
    return f"{label} {need} needed, {free} free"


def menu_line(short: Short, compact: bool = False) -> str:
    """The text of the DIGIKIT menu for a short resource (`short_text` in modules/core/digikit-menu): `DSP 400K > 254K FREE`; COMPACT (the list's RESULT
    row): `DSP 400K>254K`."""
    name, need, free = short
    label = {
        "MODULES": "MODULES",
        "SELECTION": "SELECTED",
        "ARENA": "CPU",
        "STAGE": "FILE",
        "DSP_AREA": "DSP",
        "MACHINE_ROWS": "MACHINES",
        "VOICE_ROWS": "VOICES",
        "PARAM_ROWS": "PARAMS",
        "PAGE_ROWS": "PAGES",
        "FX_SLOTS": "FX",
    }[name]
    gt = ">" if compact else " > "
    if name in ("ARENA", "DSP_AREA", "STAGE"):
        out = f"{label} {-(-need // 1024)}K{gt}{free // 1024}K"
    else:
        out = f"{label} {need}{gt}{free}"
    if not compact:
        out += " MAX" if name == "STAGE" else " FREE"
    return out


def _size(n: int, up: bool) -> str:
    """`400 KiB` (rounded up for what is needed, down for what is free); bytes below 1 KiB."""
    if n < 1024:
        return f"{n} bytes"
    return f"{-(-n // 1024) if up else n // 1024} KiB"


def vector_caps(base: dict[str, Any], over: dict[str, Any] | None) -> Caps:
    """The capacity of a vector: BASE ([limit, used, largest] per kind) with OVER laid over it (null: the kind is left out)."""
    merged = {**base, **(over or {})}
    return {
        k: {"limit": v[0], "used": v[1], "largest": v[2]}
        for k, v in merged.items()
        if v is not None  # null in OVER: the kind is left out
    }
