"""The machine-type machinery of each known MAIN OS image.

`tools/patchkit/machinepatch.py` installs an extra machine on Digitakt II by patching a
dozen bounds and tables. Every address in it is a Digitakt II 1.15C address,
so it cannot run against any other image. This module holds the same addresses
for each image we have mapped, keyed by the SHA-256 of the MAIN OS image, so a
tool stops on an unknown image instead of writing 1.15C offsets into it. Same
contract as `tools/sharc_dev/framelink.py`.

What the anchors are, and why each one matters to a patch, is in
docs/findings/02-machines-and-parameters.md: "The ColdFire machine dispatch",
"The display names are a separate table", "Type 7 did not stick: a
permission check was the sixth bound", "Copying SLICE's behaviour to type 7"
and "Digitone II 1.11 has the same machine machinery, with five machines".

Anchor keys:

  machine_count     stock machine types, 0..machine_count-1. The compiler
                    baked machine_count-1 into every bound as a `moveq`.
  type_field        byte offset of the machine type in the per-track object
                    the slot's vtable +0x28 returns.
  dispatch          the descriptor dispatch: bound, stride, array base,
                    out-of-range fallback.
  field_accessor    reads `*(descriptor + 8 + field*4)`.
  list_source       rodata table of longs, the machine list in display order;
                    list_source_end is one past it. list_builder copies the
                    range, bracketed by the two `pea` immediates at
                    list_start_site and list_end_site.
  list_filter       a second, shorter list. Digitone II has none.
  rank_call         the `jsr` to rank_insert that fills the sort comparator's
                    function-local `std::map` once; rank_guard is that
                    static's guard byte. Digitone II has no runtime sort.
  name_table        display names: name_table_rows rows of 12 bytes, three
                    big-endian `char *`. name_accessors gives each accessor
                    and the table address its `lea`/`addi` points at.
  permit_check      permits a (type, index) pair; permit_bound is its `moveq`
                    bound, permit_lea the `lea` of permit_table, which is
                    permit_table_rows longs of which only the high word is
                    read. Every high word is 0xffff on all three images.
  setter            stores the type byte after asking permit_check;
                    setter_early_return is where it leaves on a refusal.
  commit            calls the setter; on MIDI it also writes the SRAM MIDI
                    flags. type_reader reads the type byte back.
  group_helper      maps a type to a UI group for the list separators.
  pertype_table     a byte per type, read behind a bound at pertype_sites,
                    each `(bound_addr, lea_addr)`.
  clone_sites       `type == 6` (SLICE) tests that a cloned machine must also
                    match, each `(function, site)`, the mask test
                    `(type & ~2) == 4` last. The shim bytes are decoded from
                    the site by tools/patchkit/machinepatch.py decode_clone_site; the
                    order sets the shim layout in the cave.
  cave_a, cave_b    free space. cave_b needs at least 0x400 bytes.
  flash_cave        (address, size) of cave space that survives a cold boot,
                    for patches baked into a flashed image. cave_b is cleared
                    by the reset path, so it only works for live patches on a
                    resumed snapshot.

`None` means not found, not "absent": see the `missing` tuple on each profile
for what was searched for and not located, and
docs/findings/02-machines-and-parameters.md for which of
those are believed genuinely absent rather than merely unfound.

`checks` are byte preconditions at fixed addresses, in the shape
`tools/patchkit/machinepatch.py` already uses. They are what makes a wrong address
fail loudly instead of corrupting an image. Digitone II has none yet: its
anchors were found by pattern and call-graph work but no byte precondition
has been pinned down, so `verify` reports it as unchecked rather than as
passing.
"""

import hashlib
import struct

LOAD_ADDR = 0x40000400

DT2_115C = {
    "name": "Digitakt II 1.15C",
    "device": "dt2",
    "load_addr": LOAD_ADDR,
    "machine_count": 7,
    "type_field": 0xA2,
    "dispatch": 0x400CAF48,
    "descriptor_base": 0x42923644,
    "descriptor_stride": 0x2C,
    "fallback_descriptor": 0x4292374C,
    "field_accessor": 0x4001762C,
    "list_source": 0x401E1958,
    "list_source_end": 0x401E1974,
    "list_filter": 0x401E1940,
    "list_filter_end": 0x401E1958,
    "list_builder": 0x40051FBC,
    "list_end_site": 0x40052000,
    "list_start_site": 0x4005200A,
    "comparator": 0x400517C4,
    "rank_call": 0x40051872,
    "rank_insert": 0x40198948,
    "rank_guard": 0x40984CE8,
    "name_table": 0x401FBC50,
    "name_table_rows": 7,
    "name_table_row_bytes": 12,
    "name_accessors": (
        (0x400DCC50, 0x401FBC50),
        (0x400DCC76, 0x401FBC54),
        (0x400DCC9C, 0x401FBC58),
    ),
    "permit_check": 0x400DCAB8,
    "permit_bound": 0x400DCABA,
    "permit_lea": 0x400DCAD0,
    "permit_table": 0x401FBDA6,
    "permit_table_rows": 7,
    "setter": 0x40050CD6,
    "setter_early_return": 0x40050CFE,
    "commit": 0x40035E90,
    "type_reader": 0x4004FC02,
    "group_helper": 0x4005D7B8,
    "group_bound": 0x4005D7CA,
    "pertype_table": 0x401D9F30,
    "pertype_sites": (
        (0x400166FC, 0x40016702),
        (0x400178C6, 0x400178D0),
        (0x40017D46, 0x40017D50),
        (0x40017D9A, 0x40017DA8),
        (0x4001709E, 0x400170A4),
        (0x40016628, 0x40016648),
    ),
    "list_view": 0x4005E022,
    "selection_view": 0x400607B2,
    # (function, site) of each `type == 6` test a SLICE clone must also match.
    "clone_sites": (
        (0x4005F0C0, 0x4005F1A0),
        (0x4005EDD6, 0x4005EEAC),
        (0x4003065A, 0x40030766),
        (0x40048660, 0x400488EC),
        (0x4005BE94, 0x4005BEEA),
        (0x4005CB7C, 0x4005D014),
    ),
    "cave_a": 0x402F9C14,
    "cave_b": 0x40303E5C,
    # Cave space that survives a cold boot: cave_a, which ends at the clear
    # loop's start (0x402fa000). cave_b is zeroed at reset, as on 1.16.
    "flash_cave": (0x402F9C14, 1004),
    "missing": (),
    # Byte preconditions, copied from tools/patchkit/machinepatch.py's *_WANT
    # constants, which were verified against this image.
    "checks": (
        (0x400CAF48, "7206202f0004", "dispatch head"),
        (0x40052000, "4879401e1974", "list end pea"),
        (0x4005200A, "4879401e1958", "list start pea"),
        (0x4005D7CA, "7206b2806604", "group bound"),
        (0x400DCC50, "7206202f0004b2806514", "name accessor head"),
        (0x400DCC60, "41f9401fbc50", "name table lea"),
        (0x40051872, "4eb940198948", "rank insert jsr"),
        (0x400DCABA, "7406", "permit bound"),
        (0x400DCAD0, "41f9401fbda6", "permit table lea"),
    ),
}

DT2_116 = {
    "name": "Digitakt II 1.16",
    "device": "dt2",
    "load_addr": LOAD_ADDR,
    "machine_count": 7,
    "type_field": 0xA2,
    "dispatch": 0x400C8840,
    "descriptor_base": 0x4293B960,
    "descriptor_stride": 0x2C,
    "fallback_descriptor": 0x4293BA68,
    "field_accessor": 0x40017CD4,
    "list_source": 0x401F5048,
    "list_source_end": 0x401F5064,
    "list_filter": 0x401F5030,
    "list_filter_end": 0x401F5048,
    "list_builder": 0x40052A08,
    "list_end_site": 0x40052A4C,
    "list_start_site": 0x40052A56,
    "comparator": 0x40052210,
    "rank_call": 0x400522BE,
    "rank_insert": 0x401AAB60,
    "rank_guard": 0x4099CCE8,
    "name_table": 0x4020EB68,
    "name_table_rows": 7,
    "name_table_row_bytes": 12,
    "name_accessors": (
        (0x400DA548, 0x4020EB68),
        (0x400DA56E, 0x4020EB6C),
        (0x400DA594, 0x4020EB70),
    ),
    "permit_check": 0x400DA3B0,
    "permit_bound": 0x400DA3B2,
    "permit_lea": 0x400DA3C8,
    "permit_table": 0x4020ECBE,
    "permit_table_rows": 7,
    "setter": 0x40051712,
    "setter_early_return": 0x4005173A,
    "commit": 0x40036798,
    "type_reader": 0x4005063E,
    "group_helper": 0x4005E204,
    "group_bound": 0x4005E216,
    "pertype_table": 0x401ED620,
    "pertype_sites": (
        (0x40016DA4, 0x40016DAA),
        (0x40017F6E, 0x40017F78),
        (0x400183EE, 0x400183F8),
        (0x40018442, 0x40018450),
        (0x40017746, 0x4001774C),
        (0x40016CD0, 0x40016CF0),
    ),
    "list_view": 0x4005EA6E,
    "selection_view": 0x400611FE,
    "clone_sites": (
        (0x4005FB0C, 0x4005FBEC),
        (0x4005F822, 0x4005F8F8),
        (0x40030D12, 0x40030E1E),
        (0x40048FB2, 0x4004923E),
        (0x4005C8E0, 0x4005C936),
        (0x4005D5C8, 0x4005DA60),
    ),
    # The whole trailing free region is 1.15C's shifted by +0x18000. cave_b
    # sits in the largest unreferenced gap, 0x4031be59-0x4031ff60.
    "cave_a": 0x40311C14,
    "cave_b": 0x4031BE5C,
    # Cave space that survives a cold boot. cave_b does not: the reset path's
    # FUN_400004b2 zeroes 0x40312000..0x47e28470 (movea.l #0x40312000,a0 at
    # 0x400004ba), so bytes flashed there are gone before the OS runs. The
    # zero run below that boundary is 2108 bytes, cave_a being its top 1004.
    "flash_cave": (0x403117C4, 2108),
    "missing": (),
    # Same instruction shapes as 1.15C -- only the embedded absolute operands
    # differ. The one function whose shape did change is the setter: 1.16
    # pushes an extra argument before the commit call (371 bytes against
    # 1.15C's 363), so a byte check spanning past 0x4005182a would fail.
    # Nothing here spans it.
    "checks": (
        (0x400C8840, "7206202f0004", "dispatch head"),
        (0x40052A4C, "4879401f5064", "list end pea"),
        (0x40052A56, "4879401f5048", "list start pea"),
        (0x4005E216, "7206b2806604", "group bound"),
        (0x400DA548, "7206202f0004b2806514", "name accessor head"),
        (0x400DA558, "41f94020eb68", "name table lea"),
        (0x400522BE, "4eb9401aab60", "rank insert jsr"),
        (0x400DA3B2, "7406", "permit bound"),
        (0x400DA3C8, "41f94020ecbe", "permit table lea"),
    ),
}

DN2_111 = {
    "name": "Digitone II 1.11",
    "device": "dn2",
    "load_addr": LOAD_ADDR,
    "machine_count": 5,
    "type_field": 0xDE,
    "dispatch": 0x400C248E,
    "descriptor_base": 0x42432B24,
    "descriptor_stride": 0x2C,
    "fallback_descriptor": 0x42432BD4,
    "field_accessor": None,
    "list_source": 0x401DDD58,
    "list_source_end": 0x401DDD6C,
    "list_filter": None,
    "list_filter_end": None,
    "list_builder": 0x4004D8B6,
    "list_end_site": 0x4004D900,
    "list_start_site": 0x4004D90A,
    "comparator": None,
    "rank_call": None,
    "rank_insert": None,
    "rank_guard": None,
    "name_table": 0x401F77F0,
    "name_table_rows": 5,
    "name_table_row_bytes": 12,
    # Digitone II's rows are {hint, name, abbrev}, against Digitakt's
    # {name, abbrev, hint} -- confirmed by decoding all five rows, where
    # column 0 is null throughout and columns 1 and 2 hold "FM Tone"/"FMT"
    # and the rest. Its three accessors point at table+4, +8 and +0xc where
    # Digitakt's point at table+0, +4 and +8, which follows. A patch that
    # relocates this table must write the new name into column 1 and the
    # abbreviation into column 2, not columns 0 and 1 as on Digitakt.
    "name_accessors": (
        (0x400DC332, 0x401F77F4),
        (0x400DC358, 0x401F77F8),
        (0x400DC37E, 0x401F77FC),
    ),
    "permit_check": 0x400DC19A,
    "permit_bound": None,
    "permit_lea": None,
    "permit_table": 0x401F7932,
    "permit_table_rows": 5,
    "setter": 0x4004CC08,
    "setter_early_return": None,
    "commit": 0x4004DA90,
    "type_reader": 0x4004B7F2,
    "group_helper": 0x40059274,
    "group_bound": None,
    "pertype_table": None,
    "pertype_sites": (),
    "list_view": 0x40059AB0,
    "selection_view": 0x4005B56E,
    "clone_sites": (),
    # The trailing zero run is 0x402fb7a8-0x4030b980, but only the part below
    # 0x402fc000 is free: from there on it holds live bss scalars that happen
    # to be zero in the image and are referenced by real instructions (119
    # distinct targets found by tools/coldfire/refscan.py). These two ranges are zero
    # and unreferenced, and leave 0x80 bytes of margin before 0x402fc000.
    "cave_a": 0x402FB7A8,
    "cave_b": 0x402FBB80,
    # From unicorn-reference-final:tools/cavefind.py: below the reset clear loop's start (0x402fc000).
    "flash_cave": (0x402FB7A8, 2136),
    "missing": (
        "field_accessor: too many generic matches to isolate",
        "list_filter: no Digitakt-shaped filter table adjacent to list_source",
        "comparator/rank_*: no stable_sort in the image; the list may never "
        "be sorted at runtime",
        "pertype_table: not found in the accessor cluster",
        "clone_sites: not searched; depends on which machine is cloned",
        "permit_bound/permit_lea/group_bound/setter_early_return: the "
        "functions are located but these sites inside them are not",
    ),
    "checks": (),
}

PROFILES = {
    "6a6a887b0573a557b71badf32cd9392777c60b4d1f33dfae12bb8346a014a37b": DT2_115C,
    "57bb4dfa8df07d846adc72fdb4fb0d3cd3c5680c524bf498338460207e008e7d": DT2_116,
    "57b06a7960b7c3dc9803bde6b31896b89a2fbdcc404a932ff503f3856d4d7a61": DN2_111,
}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def profile_for(image):
    """-> (sha256, profile) for a MAIN OS image file; SystemExit if it has none."""
    sha = sha256_file(image)
    if sha not in PROFILES:
        raise SystemExit(
            "%s (sha-256 %s) has no machine profile; known: %s"
            % (image, sha, ", ".join(p["name"] for p in PROFILES.values()))
        )
    return sha, PROFILES[sha]


def image_reader(path, load_addr=LOAD_ADDR):
    """-> read(addr, n) -> bytes over a MAIN OS image file, by guest address."""
    with open(path, "rb") as f:
        img = f.read()

    def read(addr, n):
        off = addr - load_addr
        if off < 0 or off + n > len(img):
            raise SystemExit(
                "%#010x+%d is outside %s (%#010x-%#010x)"
                % (addr, n, path, load_addr, load_addr + len(img))
            )
        return img[off : off + n]

    return read


def verify(profile, read):
    """-> (failures, checked). Each failure is (addr, want, got, what)."""
    failures = []
    for addr, want, what in profile["checks"]:
        want = bytes.fromhex(want)
        got = read(addr, len(want))
        if got != want:
            failures.append((addr, want, got, what))
    return failures, len(profile["checks"])


def derived_checks(profile):
    """-> [(what, ok, detail)] for facts a profile asserts about itself."""
    out = []
    base = profile["descriptor_base"]
    stride = profile["descriptor_stride"]
    last = base + (profile["machine_count"] - 1) * stride
    out.append(
        (
            "fallback is the last descriptor",
            profile["fallback_descriptor"] == last,
            "%#010x vs %#010x" % (profile["fallback_descriptor"], last),
        )
    )
    span = profile["list_source_end"] - profile["list_source"]
    out.append(
        (
            "list_source holds machine_count longs",
            span == profile["machine_count"] * 4,
            "%d bytes, want %d" % (span, profile["machine_count"] * 4),
        )
    )
    if profile["list_filter"] is not None:
        span = profile["list_filter_end"] - profile["list_filter"]
        out.append(
            ("list_filter is a whole number of longs", span % 4 == 0, "%d bytes" % span)
        )
    return out


def report(image):
    """Print a profile's checks against an image. -> exit status."""
    sha, prof = profile_for(image)
    print("%s: %s (sha-256 %s)" % (image, prof["name"], sha))
    print(
        "  %d machines, type byte at +%#x" % (prof["machine_count"], prof["type_field"])
    )
    read = image_reader(image, prof["load_addr"])

    failures, checked = verify(prof, read)
    if not checked:
        print("  byte checks: NONE DEFINED -- this profile is unchecked")
    else:
        for addr, want, got, what in failures:
            print(
                "  FAIL %#010x %s: want %s, got %s"
                % (addr, what, want.hex(), got.hex())
            )
        if not failures:
            print("  byte checks: %d/%d pass" % (checked, checked))

    for what, ok, detail in derived_checks(prof):
        if not ok:
            print("  FAIL %s: %s" % (what, detail))

    if prof["missing"]:
        print("  not found on this image:")
        for m in prof["missing"]:
            print("    - %s" % m)

    bad = failures or [d for d in derived_checks(prof) if not d[1]]
    return 1 if bad else 0


def decode_name_table(profile, read):
    """-> [(long, abbrev, hint)] decoded from the display-name table."""
    rows = []
    for i in range(profile["name_table_rows"]):
        row = read(profile["name_table"] + i * profile["name_table_row_bytes"], 12)
        ptrs = struct.unpack(">III", row)
        rows.append(tuple(_cstring(read, p) for p in ptrs))
    return rows


def _cstring(read, addr, limit=64):
    if not addr:
        return None
    out = bytearray()
    while len(out) < limit:
        b = read(addr + len(out), 1)
        if b == b"\x00":
            break
        out += b
    return out.decode("latin-1")


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("image", help="a MAIN OS image (section_3_MAIN_OS.bin)")
    p.add_argument(
        "--names", action="store_true", help="also decode the display-name table"
    )
    args = p.parse_args(argv)

    status = report(args.image)
    if args.names:
        sha, prof = profile_for(args.image)
        read = image_reader(args.image, prof["load_addr"])
        print("  display names, as three char* per row:")
        for i, row in enumerate(decode_name_table(prof, read)):
            print("    %d %s" % (i, " | ".join("-" if c is None else c for c in row)))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
