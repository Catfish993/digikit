"""Extract a firmware .syx into decompressed sections, with no outside tool.

    uv run python -m tools.firmware.extract [firmware.syx] [-o DIR]

Packed sections are decompressed with tools/firmware/elz.py, a byte-level decoder for
the device's aPLib-style codec. It reads every firmware in the repo root,
Digitakt II 1.16 and Digitone II 1.11 included, in about a second. (The
former --oracle option ran the updater's own depacker under the Python
Unicorn reference emulator; that emulator was removed, see tag
unicorn-reference-final.)

A section's 8-byte header is [u32 compressed_len][u32 sum of those bytes], big
endian, and the sum is checked here. Two sections opt out of it:

    4  updater  header present, sum 0, payload stored raw
    5  meta     15 ASCII bytes of build stamp, no header at all
"""

import glob
import hashlib
import os
import struct
import sys

from tools.firmware.container import sections
from tools.firmware.elz import depack_section

# The names elektron-firmware-tool uses, so a directory extracted either way
# looks the same to tools/firmware/config.py's globs. Section 2 is the bootstrap, not the
# DSP -- that name is the tool's mistake, kept here for compatibility.
NAMES = {2: "DSP", 3: "MAIN_OS", 4: "UPDATER", 5: "META", 7: "BLOB"}

# The same file unicorn-reference-final:emu/run.py reads; see MARKER there.
SOURCE_MARKER = ".source-sha256"


def classify(stream):
    """-> ('packed'|'raw', bytes)

    Packed only when the 8-byte header is self-consistent: the declared length
    fits, and the bytes after it sum to the declared sum. Anything else is
    stored plainly, with a header to strip (section 4) or without one
    (section 5). Guessing from the section id instead would be a guess; this
    is a check.
    """
    if len(stream) >= 8:
        ln, sm = struct.unpack_from(">II", stream, 0)
        if ln + 8 <= len(stream) and (sum(stream[8 : 8 + ln]) & 0xFFFFFFFF) == sm:
            return "packed", stream
        if sm == 0:
            return "raw", stream[8:]
    return "raw", stream


def extract(syx, outdir, progress=None):
    """Decompress every section of `syx` into `outdir`.

    -> [(id, kind, path, nbytes, dest)], one entry per section. Also records
    which .syx these came from, which unicorn-reference-final:emu/run.py checks before pairing them
    with a firmware. `progress(sid, kind, filename)` is called before each
    section is decompressed.
    """
    c, secs = sections(syx)
    os.makedirs(outdir, exist_ok=True)
    written = []
    for sid, off, clen, dest in secs:
        kind, payload = classify(bytes(c[off : off + clen]))
        name = "section_%d_%s.bin" % (sid, NAMES.get(sid, "SECTION"))
        if progress:
            progress(sid, kind, name)
        if kind == "packed":
            payload = depack_section(payload)
        # Drop any earlier extraction of this section whatever it was named,
        # so a tool-made directory cannot leave a duplicate behind.
        for stale in glob.glob(os.path.join(outdir, "section_%d_*.bin" % sid)):
            os.remove(stale)
        path = os.path.join(outdir, name)
        with open(path, "wb") as fh:
            fh.write(payload)
        written.append((sid, kind, path, len(payload), dest))
    record_source(syx, outdir)
    return written


def record_source(syx, outdir):
    """Stamp `outdir` with the hash of the .syx it came from."""
    h = hashlib.sha256()
    with open(syx, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    with open(os.path.join(outdir, SOURCE_MARKER), "w") as fh:
        fh.write(h.hexdigest() + "\n")


USAGE = """usage: python -m tools.firmware.extract [firmware.syx] [-o DIR]

Decompress a firmware .syx into its sections.

  firmware.syx   the firmware to extract; resolved like every other path in
                 this project -- this argument, then DT2_SYX, then the only
                 .syx present (see tools/firmware/config.py)
  -o DIR         where to write them; overrides DT2_SECTIONS
  --help         this"""


def main(argv):
    from tools.firmware import config

    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    outdir = None
    if "-o" in argv:
        i = argv.index("-o")
        if i + 1 >= len(argv):
            raise SystemExit("-o needs a directory")
        outdir = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    rest = [a for a in argv if not a.startswith("-")]
    syx = config.firmware(rest[0] if rest else None)
    # -o is the "explicit argument" tier of config.py's resolution order, so
    # without it this must fall through to DT2_SECTIONS rather than a literal.
    outdir = outdir or config.sections_dir()
    print("Extracting %s -> %s/\n" % (syx, outdir.rstrip("/")), flush=True)

    def started(sid, kind, name):
        print("  %-26s %-14s" % (name, kind + "..."), end="", flush=True)

    for _sid, kind, path, n, dest in extract(syx, outdir, progress=started):
        print(
            "\r  %-26s %-14s%9d bytes  dest=%#010x"
            % (os.path.basename(path), kind, n, dest),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
