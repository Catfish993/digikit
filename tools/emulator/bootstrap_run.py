#!/usr/bin/env python3
"""Run the device bootstrap (OS section 2) on the native ColdFire core and drive its recovery path.

The Digitakt II bootstrap is the code that loads MAIN OS, offers the STARTUP menu ([FUNC] held at power-on) and
receives an OS upgrade over MIDI DIN ([TRIG 4]). packages/emulator/bootstrap executes its real instructions with a minimal
board: NOR flash (SPI transport replaced), panel UART (keys, display records), MIDI UART. See
packages/emulator/bootstrap/src/lib.rs for what is modelled and docs/findings/01-container-and-patching.md ("Recovery").

    uv run python -m tools.emulator.bootstrap_run verify IMAGE.syx [--base STOCK.syx] [--png OUT.png]
        Flash holds BASE (default Digitakt_II_OS1.16.syx). Boot with [FUNC] held, press [TRIG 4], send IMAGE over
        MIDI, let the bootstrap verify (content checksum, HMAC) and write it, reboot, and check that MAIN OS comes
        up from the flash with IMAGE's own section 3. Exit 0 only when IMAGE is accepted end to end.
    uv run python -m tools.emulator.bootstrap_run menu [--syx STOCK.syx] [--png OUT.png]
        Flash holds a MAIN OS that faults at its first instruction; boot with [FUNC] held; save the STARTUP MENU.

Supports the Digitakt II 1.16 / 1.17 bootstrap (version 0x0201) only. Never touches hardware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools.devtools import benchlock  # noqa: E402
from tools.emulator import digi_env, native_target  # noqa: E402
from tools.firmware import build, container  # noqa: E402
from tools.firmware.elz import depack_section  # noqa: E402
from tools.firmware.extract import classify  # noqa: E402

DEFAULT_SYX = ROOT / "Digitakt_II_OS1.16.syx"
FLASH_SIZE = 0x100_0000
CONTAINER_AT = 0x8_0000
SECTOR = 0x4_0000
# SHA-256 of the depacked bootstrap (section 2, with its length word) of DT2 1.16 and 1.17.
KNOWN_BOOTSTRAP_SHA256 = (
    "79667563ab7d8da070f461fbaa888207fe7a457748e538386cbd172239bf8d43"
)
MAIN_LOAD = 0x4000_0400

# Addresses in the 1.16 bootstrap (docs/findings/01-container-and-patching.md, "Recovery").
PC_UPGRADE_GATE_EXIT = (
    0x8000_21DC  # FUN_80001c48: running version >= installed bootstrap, no upgrade
)
PC_UPGRADE_ROUTINE = 0x8000_1C48
PC_MENU = 0x8000_2DC4  # STARTUP MENU
PC_KEY_WAIT = 0x8000_13AC  # menu waits for a panel event
PC_OS_UPGRADE = 0x8000_3E5A  # [TRIG 4]: READY TO RECEIVE
PC_CONTENT_CHECK = (
    0x8000_3C9C  # content checksum, then HMAC (0x80005e3a), then erase and write
)
PC_HMAC = 0x8000_5E3A
PC_ABORT_SPIN = 0x8000_3C9A  # UPGRADE ABORTED: spins here
PC_LOAD_MAIN = 0x8000_0596  # find section 3 in the flash container, depack, call

BIN = None


def binary() -> pathlib.Path:
    return native_target.target_dir("") / "release" / "bootstrap-run"


def build_binary() -> pathlib.Path:
    """cargo build of packages/emulator/bootstrap (no generated SHARC core needed), under the shared bench lock."""
    env = digi_env.rust_env()
    env.pop("SHARC_GEN_DIR", None)
    cmd = ["rustup", "run", digi_env.TOOLCHAIN, "cargo", "build", "--release", "-p", "bootstrap-emu",
           "--target-dir", str(native_target.target_dir(""))]  # fmt: skip
    with benchlock.hold("shared", "bootstrap-emu build"):
        subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    return binary()


def build_example(package: str, example: str) -> pathlib.Path:
    """cargo build of one example of a native crate that needs no generated core (CF-only)."""
    env = digi_env.rust_env()
    env.pop("SHARC_GEN_DIR", None)
    target = native_target.target_dir("")
    cmd = ["rustup", "run", digi_env.TOOLCHAIN, "cargo", "build", "--release", "-p", package,
           "--example", example, "--target-dir", str(target)]  # fmt: skip
    with benchlock.hold("shared", f"{example} build"):
        subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    return target / "release" / "examples" / example


def ensure_binary() -> pathlib.Path:
    global BIN
    if BIN is None:
        BIN = build_binary()
    return BIN


def extract_boot(syx: pathlib.Path | str) -> bytes:
    """The depacked bootstrap (section 2) of a .syx, as tools.firmware.extract writes it."""
    c, secs = container.sections(syx)
    for sid, off, clen, _dest in secs:
        if sid == 2:
            kind, payload = classify(bytes(c[off : off + clen]))
            data = depack_section(payload) if kind == "packed" else payload
            digest = hashlib.sha256(data).hexdigest()
            if digest != KNOWN_BOOTSTRAP_SHA256:
                raise ValueError(
                    f"bootstrap {digest[:16]} is not the Digitakt II 1.16/1.17 bootstrap"
                )
            return data
    raise ValueError("no section 2")


def container_of(syx: pathlib.Path | str) -> bytes:
    """The ELE3 container exactly as the device stores it in flash: total_len bytes from the magic."""
    dec = container.decode_syx(syx)
    total_len = struct.unpack_from(">I", dec, 0)[0]
    return bytes(dec[8 : 8 + total_len])


def flash_with(c: bytes) -> bytearray:
    flash = bytearray(b"\xff" * FLASH_SIZE)
    flash[CONTAINER_AT : CONTAINER_AT + len(c)] = c
    return flash


def section3(syx: pathlib.Path | str) -> bytes:
    """Section 3 (MAIN OS) of a .syx, depacked."""
    c, secs = container.sections(syx)
    for sid, off, clen, _dest in secs:
        if sid == 3:
            kind, payload = classify(bytes(c[off : off + clen]))
            return depack_section(payload) if kind == "packed" else payload
    raise ValueError("no section 3")


@dataclass
class Run:
    report: dict
    flash: bytes | None = None
    panel: bytes = b""
    dumps: dict[str, bytes] = field(default_factory=dict)

    def hits(self, pc: int) -> int:
        return self.report["pc_hits"].get(f"{pc:#x}", {}).get("count", 0)


def run(
    boot: bytes,
    flash: bytes,
    *,
    hold: tuple[str, ...] = (),
    presses: tuple[str, ...] = (),
    midi: pathlib.Path | str | None = None,
    stops: tuple[int, ...] = (),
    watches: tuple[int, ...] = (),
    limit: int = 400_000_000,
    seconds: float = 120.0,
    reboots: int = 0,
    run_main: bool = False,
    dumps: tuple[tuple[int, int], ...] = (),
) -> Run:
    exe = ensure_binary()
    with tempfile.TemporaryDirectory(prefix="bootstrap-run-") as tmp:
        t = pathlib.Path(tmp)
        (t / "boot.bin").write_bytes(boot)
        (t / "in.flash").write_bytes(flash)
        cmd = [str(exe), "--boot", str(t / "boot.bin"), "--flash", str(t / "in.flash"),
               "--flash-out", str(t / "out.flash"), "--panel-out", str(t / "panel.bin"),
               "--limit", str(limit), "--seconds", str(seconds), "--reboots", str(reboots)]  # fmt: skip
        if hold:
            cmd += ["--hold", ",".join(hold)]
        for p in presses:
            cmd += ["--press-at-pc", p]
        if midi is not None:
            cmd += ["--midi", str(midi)]
        for pc in stops:
            cmd += ["--stop-pc", hex(pc)]
        for pc in watches:
            cmd += ["--watch", hex(pc)]
        if run_main:
            cmd += ["--run-main"]
        for i, (addr, n) in enumerate(dumps):
            cmd += ["--dump-ram", f"{addr:#x}:{n}:{t / f'dump{i}.bin'}"]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(f"bootstrap-run failed: {done.stderr.strip()}")
        return Run(
            report=json.loads(done.stdout),
            flash=(t / "out.flash").read_bytes(),
            panel=(t / "panel.bin").read_bytes(),
            dumps={
                f"{a:#x}": (t / f"dump{i}.bin").read_bytes()
                for i, (a, _n) in enumerate(dumps)
            },
        )


def render_panel(stream: bytes):
    """The 128x64 OLED as the panel shows it after `stream` (what the bootstrap sent on its panel UART).

    Records: 0x10|page, column, 8 data bytes (eight columns, one byte of eight rows each). Page 0 is the bottom
    page of the picture. Other commands are skipped.
    """
    from PIL import Image

    args = {
        0xB9: 2,
        0xB5: 4,
        0xBB: 3,
        0xB7: 1,
        0xBC: 1,
        0xBA: 1,
        0x60: 1,
        0x70: 1,
        0x71: 1,
        0x51: 1,
        0xF0: 1,
    }
    pix = [[0] * 128 for _ in range(64)]
    i = 0
    while i < len(stream):
        c = stream[i]
        if 0x10 <= c <= 0x17 and i + 10 <= len(stream):
            page, col = c & 7, stream[i + 1]
            for k, b in enumerate(stream[i + 2 : i + 10]):
                for bit in range(8):
                    if col + k < 128:
                        pix[(7 - page) * 8 + bit][col + k] = (b >> bit) & 1
            i += 10
        else:
            i += 1 + args.get(c, 0)
    img = Image.new("1", (128, 64))
    img.putdata([p for row in pix for p in row])
    return img


def save_png(stream: bytes, path: pathlib.Path | str, scale: int = 4) -> None:
    from PIL import Image

    img = render_panel(stream)
    img.resize((128 * scale, 64 * scale), Image.NEAREST).save(path)


def faulting_main_syx(
    base: pathlib.Path | str, out: pathlib.Path | str
) -> pathlib.Path:
    """`base` with a MAIN OS that faults at its first instruction (entry word, then ILLEGAL opcodes)."""
    bad = struct.pack(">I", MAIN_LOAD + 4) + b"\x4a\xfc" * 64
    pathlib.Path(out).write_bytes(build.rebuild(base, replacements={3: bad}))
    return pathlib.Path(out)


def _encode(base: pathlib.Path | str, stream: bytes) -> bytes:
    """A .syx from a raw stream (8-byte preamble then container) with the base's own framing."""
    raw = pathlib.Path(base).read_bytes()
    first, last = build._source_framing(raw)
    return build.encode_syx(stream, first[1:-1][3], first, last)


def corrupt_syx(base: pathlib.Path | str, kind: str) -> bytes:
    """A variant of `base` that the bootstrap must refuse, with valid transport framing.

    kind: "content" one container byte flipped, preamble checksum left alone;
          "hmac"    one container byte flipped and the preamble checksum recomputed (only the HMAC is wrong);
          "packet"  one transport byte flipped (the per-message checksum then fails).
    """
    raw = pathlib.Path(base).read_bytes()
    if kind == "packet":
        flipped = bytearray(raw)
        flipped[len(raw) // 2] ^= 0x01
        return bytes(flipped)
    dec = container.decode_syx(base)
    total_len = struct.unpack_from(">I", dec, 0)[0]
    c = bytearray(dec[8 : 8 + total_len])
    c[total_len // 2] ^= 0x01
    checksum = (
        build.content_checksum(bytes(c))
        if kind == "hmac"
        else struct.unpack_from(">I", dec, 4)[0]
    )
    return _encode(base, struct.pack(">II", total_len, checksum) + bytes(c))


@dataclass
class Verdict:
    accepted: bool
    detail: str
    run: Run

    def summary(self) -> dict:
        r = self.run.report
        return {"accepted": self.accepted, "detail": self.detail, "end": r["end"], "icount": r["icount"],
                "flash": r["flash"], "midi_fed": r["midi_fed"]}  # fmt: skip


def receive(
    syx: pathlib.Path | str,
    base: pathlib.Path | str = DEFAULT_SYX,
    *,
    midi_bytes: bytes | None = None,
    boot: bytes | None = None,
) -> Verdict:
    """The STARTUP-menu OS UPGRADE of `syx` (or `midi_bytes`) on a flash that holds `base`'s container."""
    boot = boot if boot is not None else extract_boot(base)
    expected_main = section3(syx) if midi_bytes is None else None
    with tempfile.TemporaryDirectory(prefix="bootstrap-midi-") as tmp:
        midi = pathlib.Path(tmp) / "in.syx"
        midi.write_bytes(
            midi_bytes if midi_bytes is not None else pathlib.Path(syx).read_bytes()
        )
        n_main = len(expected_main) if expected_main is not None else 64
        result = run(
            boot,
            bytes(flash_with(container_of(base))),
            hold=("func",),
            presses=(f"{PC_KEY_WAIT:#x}:trig4:down",),
            midi=midi,
            stops=(PC_ABORT_SPIN,),
            watches=(PC_OS_UPGRADE, PC_CONTENT_CHECK, PC_HMAC, PC_LOAD_MAIN),
            reboots=1,
            limit=1_500_000_000,
            dumps=((MAIN_LOAD, n_main),),
        )
    rep = result.report
    if result.hits(PC_ABORT_SPIN):
        return Verdict(False, "UPGRADE ABORTED", result)
    if not rep["end"].startswith("main_os"):
        return Verdict(False, f"did not finish: {rep['end']}", result)
    if rep["flash"]["programmed_bytes"] == 0:
        return Verdict(False, "nothing written", result)
    if expected_main is not None and result.dumps[f"{MAIN_LOAD:#x}"] != expected_main:
        return Verdict(
            False, "MAIN OS after reboot differs from the image's section 3", result
        )
    return Verdict(True, "verified, written, rebooted into the image's MAIN OS", result)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser(
        "verify", help="send IMAGE over emulated MIDI DIN to the emulated bootstrap"
    )
    v.add_argument("image")
    v.add_argument("--base", default=str(DEFAULT_SYX))
    v.add_argument("--png", help="save the final panel picture")
    m = sub.add_parser(
        "menu", help="STARTUP MENU with [FUNC] held over a MAIN OS that faults at reset"
    )
    m.add_argument("--syx", default=str(DEFAULT_SYX))
    m.add_argument("--png", help="save the panel picture")
    args = ap.parse_args(argv)
    if args.cmd == "verify":
        verdict = receive(args.image, args.base)
        print(json.dumps(verdict.summary(), indent=2))
        if args.png:
            save_png(verdict.run.panel, args.png)
        return 0 if verdict.accepted else 1
    with tempfile.TemporaryDirectory() as tmp:
        bad = faulting_main_syx(args.syx, pathlib.Path(tmp) / "faulting.syx")
        res = run(
            extract_boot(args.syx),
            bytes(flash_with(container_of(bad))),
            hold=("func",),
            stops=(PC_KEY_WAIT,),
            watches=(PC_MENU, PC_LOAD_MAIN),
        )
    print(
        json.dumps(
            {k: res.report[k] for k in ("end", "icount", "pc_hits", "main_os_entry")},
            indent=2,
        )
    )
    if args.png:
        save_png(res.panel, args.png)
    return 0 if res.hits(PC_MENU) and not res.hits(PC_LOAD_MAIN) else 1


if __name__ == "__main__":
    sys.exit(main())
