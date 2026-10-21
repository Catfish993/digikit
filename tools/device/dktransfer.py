#!/usr/bin/env python3
"""Transfer: the repo's run-time modules on a target (emulator or device), and install of a chosen set.

    uv run python -m tools.device.dktransfer list --emu [--json]
    uv run python -m tools.device.dktransfer install digikit/pluck [MOD ...] --emu [--keep PATH ...] [--json]
    mise run transfer -- list --emu --json
    mise run transfer -- install digikit/pluck --port Digitakt

`list` is one merged view for the target's device (INFO): the repo's run-time modules that pack for it (never `core/*`; `test/` ones are
marked), with group (machine, fx or service contract in the packed header: Machines, Effects, Tools), version, sizes, build state
(`unchanged` = the committed objects carry the hash of their sources; `needs build`), whether the file is on the card (LIST, same sha256),
whether it is loaded (MODULES), and whether it fits (CAPACITY; a warning only). Card files that match no repo module are listed with
their group, read from the 0xa0-byte header (READ, runtime 0.2.4).

`install MOD...` builds only the modules whose objects changed, packs them, uploads the files the card does not have (same sha256),
SELECTs exactly these modules plus the `--keep` paths, RESTARTs and waits until MODULES lists them. `--json` prints one JSON object per line
(`{"event": ...}`); `list --json` prints one JSON document.

Target: `--emu` (the running emulator; `--run-dir DIR` or `--socket PATH` pick another one) or `--port NAME` (python-rtmidi). A running
emulator is driven in real time. A paused emulator is refused (nothing would step it): resume it, or pass `--step` to let this tool step it
(a daemon of `mise run emu:serve`, the test rigs).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.device import dkrun as R
from tools.device import dkupload as D
from tools.patchkit import modroots

MAGIC = b"DKMF"
GROUPS = ("Machines", "Effects", "Tools")
SELECTION_MAX = 8


class TransferError(RuntimeError):
    pass


# ---- the .dkm header ----------------------------------------------------------------------------------------------------------


def header_info(h: bytes) -> dict[str, Any] | None:
    """The fields of a .dkm header (docs/design/digikit-dkm.md) that `list` shows; None when `h` is not one."""
    if len(h) < D.HEADER_BYTES or h[:4] != MAGIC:
        return None
    size, _hdr = struct.unpack_from(">II", h, 8)
    version = struct.unpack_from(">I", h, 0x34)[0]
    contracts = {
        k: (h[0x38 + 2 * i], h[0x39 + 2 * i])
        for i, k in enumerate(("api", "machine", "fx", "service"))
    }
    image, bss = struct.unpack_from(">II", h, 0x64)
    dsp = struct.unpack_from(">I", h, 0x9C)[0]
    return {
        "name": h[0x40:0x60].split(b"\0")[0].decode("ascii", "replace"),
        "version": D.fmt_runtime(version),
        "contracts": contracts,
        "group": group_of(contracts),
        "size": size,
        "arena": image + bss,
        "dsp": dsp,
    }


def group_of(contracts: dict[str, tuple[int, int]]) -> str:
    """Machine contract: Machines; fx: Effects; service or api only: Tools."""
    if contracts["machine"][0]:
        return "Machines"
    if contracts["fx"][0]:
        return "Effects"
    return "Tools"


# ---- the repo's modules ---------------------------------------------------------------------------------------------------------


def candidates() -> list[str]:
    """Module ids that can be run-time modules: not `core/*`, not a framework, and either `dynamic` or a raw ColdFire module."""
    out = []
    for mid, d in sorted(modroots.manifests().items()):
        m = tomllib.loads((d / "module.toml").read_text())["module"]
        if mid.startswith("core/") or m.get("type") == "framework":
            continue
        if m.get("dynamic") or m.get("type") == "raw":
            out.append(mid)
    return out


def pack_for_transfer(module: str, device: str) -> Path:
    """Pack into out/patch/transfer/<device>/, so a list or install does not rewrite the files `dk run` leaves in out/patch."""
    name = module.rsplit("/", 1)[-1]
    return R.pack_module(
        module, device, R.ROOT / "out" / "patch" / "transfer" / device / f"{name}.dkm"
    )


def repo_rows(
    device: str,
    ids: list[str] | None = None,
    pack: Callable[[str, str], Path] = pack_for_transfer,
    status: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """The repo modules that pack for DEVICE (a dry-run pack of each: the packer is the truth, `ports` is not), one dict per module with its
    packed file (`dkm`), header fields and build state. A module that does not pack for the device is left out."""
    from tools.patchkit import pk_build

    status = status or (lambda mid: pk_build.module_status(modroots.module_dir(mid)))
    rows = []
    for mid in ids if ids is not None else candidates():
        try:
            dkm = pack(mid, device)
        except R.RunError:
            continue
        data = dkm.read_bytes()
        h = header_info(data[: D.HEADER_BYTES])
        if h is None:
            continue
        m = tomllib.loads((modroots.module_dir(mid) / "module.toml").read_text())[
            "module"
        ]
        rows.append(
            {
                **h,
                "id": mid,
                "label": m.get("name", mid),
                "test": mid.startswith("test/"),
                "path": R.module_path(mid),
                "sha256": hashlib.sha256(data).hexdigest(),
                "file_size": len(data),
                "build": status(mid),
                "dkm": str(dkm),
            }
        )
    return rows


# ---- the merged view ----------------------------------------------------------------------------------------------------------


def fit_warnings(
    row: dict[str, Any], cap: dict[str, dict[str, int]] | None
) -> list[str]:
    """What would not fit on the target (CAPACITY). Only a warning: the device refuses a load that does not fit."""
    if cap is None or row["loaded"]:
        return []
    out = []
    m = cap.get("MODULES")
    if m and m["used"] >= m["limit"]:
        out.append(f"no free module record ({m['used']}/{m['limit']})")
    a = cap.get("ARENA")
    if a and row["arena"] > a["largest"]:
        out.append(f"arena: needs {row['arena']} bytes, largest free {a['largest']}")
    d = cap.get("DSP_AREA")
    if d and row["dsp"] > d["largest"]:
        out.append(f"DSP area: needs {row['dsp']} bytes, largest free {d['largest']}")
    return out


def merge(
    rows: list[dict[str, Any]],
    card: list[tuple[str, int, str]],
    loaded: list[str],
    cap: dict[str, dict[str, int]] | None,
    headers: dict[str, bytes | None],
) -> dict[str, Any]:
    """The `list` result from its parts: the repo rows (`repo_rows`), the card's LIST, the names MODULES reports loaded, CAPACITY (or None),
    and the header bytes of the card files (`headers`: path -> 0xa0 bytes, None when unreadable)."""
    on_card = {p: (n, h) for p, n, h in card}
    mine = []
    seen = set()
    for r in rows:
        r = dict(r)
        have = on_card.get(r["path"])
        seen.add(r["path"])
        r["on_card"] = (
            "no"
            if have is None
            else "yes"
            if have == (r["file_size"], r["sha256"])
            else "other version"
        )
        r["loaded"] = r["name"] in loaded
        r["warnings"] = fit_warnings(r, cap)
        r["fits"] = None if cap is None else not r["warnings"]
        mine.append(r)
    mine.sort(key=lambda r: (GROUPS.index(r["group"]), r["test"], r["id"]))
    extra = []
    for path, size, _sha in card:
        if path in seen:
            continue
        h = header_info(headers.get(path) or b"")
        name = h["name"] if h else None
        extra.append(
            {
                "id": None,
                "path": path,
                "name": name or path.rsplit("/", 1)[-1],
                "group": h["group"] if h else "Tools",
                "known": h is not None,
                "version": h["version"] if h else None,
                "file_size": size,
                "test": False,
                "on_card": "yes",
                "loaded": name in loaded if name else False,
            }
        )
    return {"modules": mine, "card_only": extra}


def card_headers(
    up: D.Uploader, card: list[tuple[str, int, str]], skip: set[str]
) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for path, _size, _sha in card:
        if path in skip:
            continue
        try:
            out[path] = up.header(path)
        except D.UploadError:
            out[path] = None
    return out


def list_view(
    up: D.Uploader, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """`list` against a connected uploader."""
    info = up.info()
    R.require_runtime(info)
    device = info["port"]
    if rows is None:
        rows = repo_rows(device)
    card = up.ls()
    loaded = (
        [m["name"] for m in up.modules()["modules"]]
        if D.has_cap(info, "MODULES")
        else []
    )
    cap = up.capacity() if D.has_cap(info, "CAPACITY") else None
    mine = {r["path"] for r in rows}
    headers = card_headers(up, card, mine) if D.has_cap(info, "READ") else {}
    out = merge(rows, card, loaded, cap, headers)
    out.update(
        device=device,
        runtime=D.fmt_runtime(info["runtime"]),
        free_bytes=info["free_bytes"],
        loaded=loaded,
    )
    return out


# ---- install ------------------------------------------------------------------------------------------------------------------


def plan_install(
    rows: list[dict[str, Any]],
    wanted: list[str],
    card: list[tuple[str, int, str]],
    keep: list[str] | None = None,
) -> dict[str, Any]:
    """What an install does: `build` (ids whose objects need a build), `upload` (ids whose packed file the card lacks, by sha256 -- decided after
    the pack: here by the rows' own hash, which a build can change), `select` (the paths: the wanted modules, then `keep`)."""
    by_id = {r["id"]: r for r in rows}
    missing = [m for m in wanted if m not in by_id]
    if missing:
        raise TransferError(
            f"not a run-time module that packs for this device: {', '.join(missing)}"
        )
    on_card = {p: (n, h) for p, n, h in card}
    select = []
    for m in wanted:
        select.append(by_id[m]["path"])
    for p in keep or []:
        if p not in select:
            select.append(p)
    if len(select) > SELECTION_MAX:
        raise TransferError(
            f"a selection holds at most {SELECTION_MAX} modules, this one has {len(select)}"
        )
    return {
        "build": [m for m in wanted if by_id[m]["build"] != "unchanged"],
        "upload": [
            m
            for m in wanted
            if on_card.get(by_id[m]["path"])
            != (by_id[m]["file_size"], by_id[m]["sha256"])
        ],
        "select": select,
    }


def wait_loaded(
    up: D.Uploader,
    names: list[str],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    timeout_s: float = 60.0,
) -> list[str]:
    """Poll MODULES until every name is listed; returns the loaded names. Raises TransferError at the timeout."""
    end = now() + timeout_s
    got: list[str] = []
    while True:
        try:
            got = [m["name"] for m in up.modules()["modules"]]
        except (TimeoutError, D.UploadError):
            got = []
        if all(n in got for n in names):
            return got
        if now() >= end:
            miss = [n for n in names if n not in got]
            raise TransferError(
                f"MODULES does not list {', '.join(miss)} after the restart (see the log: dk:log)"
            )
        sleep(0.5)


def install(
    up: D.Uploader,
    wanted: list[str],
    keep: list[str] | None = None,
    emit: Callable[[dict[str, Any]], None] = lambda e: None,
    connect: Callable[[], D.Uploader] | None = None,
    wait_boot: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    rows: list[dict[str, Any]] | None = None,
    build: Callable[[str], None] = R.build_objects,
    pack: Callable[[str, str], Path] = pack_for_transfer,
) -> dict[str, Any]:
    """Build the changed modules of WANTED, pack, upload what the card lacks, SELECT the wanted modules and the `keep` paths (nothing else:
    the selection is replaced), RESTART and wait until MODULES lists the wanted modules. EMIT gets one dict per step."""
    quiet = lambda _m: None  # noqa: E731
    info = up.info()
    R.require_runtime(info)
    device = info["port"]
    emit({"event": "start", "device": device, "modules": wanted})
    if rows is None:
        rows = repo_rows(device, pack=pack, ids=wanted)
    plan = plan_install(rows, wanted, up.ls(), keep)
    by_id = {r["id"]: r for r in rows}
    for mid in plan["build"]:
        emit({"event": "build", "id": mid})
        build(mid)
        by_id[mid] = repo_rows(device, [mid], pack)[0]  # the packed file changed
    have = {p: (n, h) for p, n, h in up.ls()}
    uploaded = []
    for mid in wanted:
        r = by_id[mid]
        data = Path(r["dkm"]).read_bytes()
        emit({"event": "upload", "id": mid, "path": r["path"], "bytes": len(data)})
        if R.upload_if_changed(up, r["path"], data, have, quiet):
            uploaded.append(mid)
        else:
            emit({"event": "skip", "id": mid, "reason": "same sha256 on the card"})
    emit({"event": "select", "paths": plan["select"]})
    up.select(plan["select"])
    emit({"event": "restart"})
    up = R.restart_and_wait(up, connect, wait_boot, sleep, now, quiet)
    names = [by_id[m]["name"] for m in wanted]
    emit({"event": "wait", "modules": names})
    loaded = wait_loaded(up, names, sleep, now)
    emit({"event": "done", "loaded": loaded})
    return {
        "built": plan["build"],
        "uploaded": uploaded,
        "selected": plan["select"],
        "loaded": loaded,
    }


# ---- the command line ---------------------------------------------------------------------------------------------------------


class LiveTransport(D.EmuTransport):
    """A running emulator: replies are polled in real time, nothing steps the guest."""

    def recv(self, timeout_s: float = 5.0) -> bytes:
        end = time.monotonic() + timeout_s
        while True:
            self.buf += self.emu.usb_out()
            self.ready += [m for m in D.split_messages(self.buf) if D.parse_reply(m)]
            if self.ready:
                return self.ready.pop(0)
            if time.monotonic() >= end:
                raise TimeoutError("no reply from the emulated device")
            time.sleep(0.005)


def connect_target(args: argparse.Namespace) -> tuple[D.Uploader, dict[str, Any]]:
    """The uploader and the install wiring (`connect`, `wait_boot`, `sleep`, `now`) for the target of the command line."""
    if args.emu:
        emu = D._emu(args.run_dir) if not args.socket else _emu_at(args.socket)
        mode = emu.status().get("mode")
        if mode == "paused" and not args.step:
            raise TransferError(
                "the emulator is paused: nothing would run the transfer. Resume it (the Run button of the workbench), "
                "or pass --step to let this tool step it"
            )
        if args.step:
            emu.subscribe()
            steps = {"n": 0}

            def sleep(s: float) -> None:
                n = int(s * R.EMU_IPS)
                emu.step(n)
                steps["n"] += n

            wiring = {
                "connect": None,
                "wait_boot": R.emu_boot_waiter(emu),
                "sleep": sleep,
                "now": lambda: steps["n"] / R.EMU_IPS,
            }
            return D.Uploader(D.EmuTransport(emu)), wiring
        wiring = {
            "connect": None,
            "wait_boot": None,
            "sleep": time.sleep,
            "now": time.monotonic,
        }
        return D.Uploader(LiveTransport(emu)), wiring
    port = args.port
    wiring = {
        "connect": lambda: D.Uploader(D.RtMidiTransport(port)),
        "wait_boot": None,
        "sleep": time.sleep,
        "now": time.monotonic,
    }
    return D.Uploader(D.RtMidiTransport(port)), wiring


def _emu_at(socket_path: str) -> Any:
    from tools.control.digi_client import Emu

    return Emu.connect(socket_path=socket_path)


def text_row(r: dict[str, Any]) -> str:
    flags = [f"card {r['on_card']}"]
    if r["loaded"]:
        flags.append("loaded")
    if r.get("build"):
        flags.append(r["build"])
    if r.get("warnings"):
        flags.append("WARNING " + "; ".join(r["warnings"]))
    size = f"{r['file_size']} B" + (
        f" (arena {r['arena']}, dsp {r['dsp']})" if "arena" in r else ""
    )
    name = r["id"] or r["path"]
    return f"  {name:<28} {r.get('version') or '-':<8} {size:<34} {', '.join(flags)}"


def print_list(v: dict[str, Any]) -> None:
    print(f"{v['device']}, runtime {v['runtime']}, free {v['free_bytes']} bytes")
    for g in GROUPS:
        rows = [r for r in v["modules"] if r["group"] == g]
        print(f"{g}:" + ("" if rows else "  (none)"))
        for r in rows:
            print(text_row(r) + ("  [test]" if r["test"] else ""))
    if v["card_only"]:
        print("On the card, not from this repo:")
        for r in v["card_only"]:
            print(text_row(r) + f"  [{r['group']}]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--emu", action="store_true", help="the running emulator")
    where.add_argument("--port", help="MIDI port name substring (a real device)")
    ap.add_argument("--run-dir", type=Path, help="with --emu: a run directory")
    ap.add_argument("--socket", help="with --emu: the control socket path")
    ap.add_argument(
        "--step",
        action="store_true",
        help="with --emu: step the emulator from here (a paused daemon); default: real time, a paused emulator is refused",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("install")
    p.add_argument("modules", nargs="+", help="module ids, e.g. digikit/pluck")
    p.add_argument(
        "--keep",
        nargs="*",
        default=[],
        metavar="PATH",
        help="store paths of loaded modules to keep selected",
    )
    args = ap.parse_args(argv)

    def emit(e: dict[str, Any]) -> None:
        if args.json:
            print(json.dumps(e), flush=True)
        else:
            rest = {k: v for k, v in e.items() if k != "event"}
            print(
                f"transfer: {e['event']} {json.dumps(rest) if rest else ''}".rstrip(),
                flush=True,
            )

    try:
        up, wiring = connect_target(args)
        if args.cmd == "list":
            v = list_view(up)
            if args.json:
                print(json.dumps(v))
            else:
                print_list(v)
        else:
            out = install(up, args.modules, args.keep, emit, **wiring)
            if args.json:
                print(json.dumps({"event": "result", **out}), flush=True)
    except (TransferError, R.RunError, D.UploadError, TimeoutError, OSError) as e:
        if args.json:
            print(json.dumps({"event": "error", "message": str(e)}), flush=True)
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
