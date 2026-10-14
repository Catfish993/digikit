#!/usr/bin/env python3
"""Plan the patches that add an eighth machine to the MAIN OS.

`docs/findings/02-machines-and-parameters.md`'s "The ColdFire machine
dispatch" section established how the UI's machine list, dispatch, group id,
name tables, sort ranking, permission check and per-type tables are laid out.
`plan_b()` is the pure planner: it takes a `read(addr, n) -> bytes` callable
(a static MAIN OS image) and a profile (tools/patchkit/machineprofile.py) and returns
the list of `(addr, old, new)` writes, placing the new data in a cave. Only one
new machine type is supported -- every bound this module raises is raised to
exactly 7 (`NEW_TYPE`). `tools/patchkit/machinebuild.py` applies a plan to a firmware.

The parts (`PARTS`): `list` (the 8-entry source table), `dispatch` (a trampoline
for FUN_400caf48 and the new descriptor), `group` (FUN_4005d7b8 maps type 7 to
the same group as 6), `name` (a display-name row), `rank` (the sort
comparator's std::map gets eight pairs), `permit` (FUN_400dcab8 accepts type 7),
`hint` (the short-name and hint accessors), `pertype` (the 7-byte per-type
table) and `clone` (each firmware test of `type == clone_of` also accepts 7).
Bisecting results are in the finding above.

The eighth machine's names, display position and the stock descriptor it clones
are a `MachineSpec` (see `spec_from_arg`: NAME:SHORT[:CLONE_OF[:POSITION]]),
defaulting to `DEFAULT_SPEC` ("Placeholder"/"PLC", cloned from type 6).

The live-guest patcher that used to live here (it ran the patches in the
Python Unicorn reference emulator) was removed with that emulator; see tag
unicorn-reference-final.
"""

import struct
from dataclasses import dataclass

from tools.patchkit import machineprofile

END_SITE = 0x40052000
START_SITE = 0x4005200A
END_WANT = bytes.fromhex("4879401e1974")
START_WANT = bytes.fromhex("4879401e1958")
TABLE_D_LO, TABLE_D_HI = 0x401E1958, 0x401E1973
ORIGINAL_TABLE = (0, 1, 2, 3, 6, 4, 5)
NEW_TABLE = ORIGINAL_TABLE + (7,)
DEFAULT_CAVE = 0x402F9C14
DEFAULT_EIGHTH = 7

DISPATCH = 0x400CAF48
DISPATCH_WANT = bytes.fromhex("7206202f0004")
DESCRIPTOR_BASE = 0x42923644
DESCRIPTOR_STRIDE = 0x2C
FALLBACK_DESCRIPTOR = 0x4292374C
ENTRY6_FIELDS = (0xF8, 0xF9, 0, 0xFB, 0xFC, 0xFD, 0, 0xFE, 0x0A)
DESCRIPTOR_FIELDS = ENTRY6_FIELDS  # alias, kept for callers that used the old name
DEFAULT_CAVE_B = 0x40303E5C
TRAMP_OFF = 0x000
DESC_OFF = 0x100
LNAME_OFF = 0x140
LCHARS_OFF = 0x14C
SNAME_OFF = 0x160
SCHARS_OFF = 0x16C
TABLE_B_OFF = 0x180

GROUP_ADDR = 0x4005D7CA
GROUP_WANT = bytes.fromhex("7206b2806604")
GROUP_NEW = bytes.fromhex("7207b2806504")

NAME_ADDR = 0x400DCC50
NAME_HEAD_WANT = bytes.fromhex("7206202f0004b2806514")
NAME_LEA_ADDR = 0x400DCC60
NAME_LEA_WANT = bytes.fromhex("41f9401fbc50")
NAME_TABLE_SRC = 0x401FBC50
NAME_TABLE_ROWS = 7
NAME_TABLE_ROW_BYTES = 12
NAME_TABLE_OFF = 0x200
LONGSTR_OFF = 0x280
SHORTSTR_OFF = 0x290

RANK_CALL = 0x40051872
RANK_CALL_WANT = bytes.fromhex("4eb940198948")
RANK_INSERT = 0x40198948
RANK_GUARD = 0x40984CE8
RANK_SHIM_OFF = 0x2A0
RANK_TABLE_OFF = 0x2C0

PERMIT_BOUND_ADDR = 0x400DCABA  # FUN_400dcab8: moveq #6,D2 -- rejects type > 6
PERMIT_BOUND_WANT = bytes.fromhex("7406")
PERMIT_LEA_ADDR = 0x400DCAD0  # lea (0x401fbda6).l,A0 -- per-type mask table
PERMIT_TABLE_SRC = 0x401FBDA6  # 7 longs; the first word of each is a track mask
PERMIT_LEA_WANT = bytes.fromhex("41f9") + struct.pack(">I", PERMIT_TABLE_SRC)
PERMIT_TABLE_OFF = 0x1A0  # 8 longs, between TABLE_B_OFF's 32 bytes and NAME_TABLE_OFF

# 'hint': the other two accessors of the name table, short name (+4) and
# header hint (+8). Same head as FUN_400dcc50, but `addi.l #table+column,D0`.
LABEL_FUNCS = ((0x400DCC76, 4), (0x400DCC9C, 8))
LABEL_ADDI_OFF = 0x12

# 'pertype': a 7-byte per-type table; byte 7 is the start of an unrelated string.
PERTYPE_TABLE_SRC = 0x401D9F30
PERTYPE_TABLE_OFF = 0x1C0
PERTYPE_SITES = (  # (moveq #6,Dn bound, lea (0x401d9f30).l,An) in each reader
    (0x400166FC, 0x40016702),  # FUN_400166b8
    (0x400178C6, 0x400178D0),  # FUN_40017828
    (0x40017D46, 0x40017D50),  # FUN_40017b56
    (0x40017D9A, 0x40017DA8),  # FUN_40017b56, second read
    (0x4001709E, 0x400170A4),  # FUN_40017080
    (0x40016628, 0x40016648),  # FUN_40016624
)

# 'clone': the profile's `clone_sites` are the `type == 6` tests (SLICE) a
# clone must also match. Only the site address is per image; the replayed
# compare, the type register and both branch targets are read from the bytes
# at the site by decode_clone_site.
CLONE_SHIM_OFF = 0x300
CLONE_SITES_OF = 6
# FUN_4005cb7c (1.15C): `(type & ~2) == 4` matches types 4 and 6.
CLONE_MASK_HEAD = bytes.fromhex("72fdc0817204b280")


def _decode_bcc(read, at):
    """beq/bne at `at`, .b or .w -> (opcode byte, length, target)."""
    op, d8 = read(at, 2)
    if op not in (0x66, 0x67) or d8 == 0xFF:
        raise SystemExit(
            "machinepatch: %#010x is not beq/bne.b/.w (%02x%02x)" % (at, op, d8)
        )
    if d8 == 0:
        (disp,) = struct.unpack(">h", read(at + 2, 2))
        return op, 4, at + 2 + disp
    return op, 2, at + 2 + (d8 - 256 if d8 >= 0x80 else d8)


def decode_clone_site(read, site):
    """-> ('eq', (addr, original, prefix, type register, match, no-match))
    for `<4-byte prefix ending in cmp.l Dy,Dx>; beq/bne`, or
    ('mask', (addr, original, match, no-match)) for the mask test."""
    head = read(site, 8)
    if head == CLONE_MASK_HEAD:
        kind, plen = "mask", 8
    else:
        (cmp,) = struct.unpack(">H", head[2:4])
        if cmp & 0xF1F8 != 0xB080:  # cmp.l Dy,Dx
            raise SystemExit(
                "machinepatch: %#010x is not a cmp.l Dy,Dx test (%s)"
                % (site, head.hex())
            )
        kind, plen = "eq", 4
    op, size, target = _decode_bcc(read, site + plen)
    end = site + plen + size
    match, nomatch = (target, end) if op == 0x67 else (end, target)
    want = read(site, plen + size).hex()
    if plen + size < 6:
        raise SystemExit(
            "machinepatch: %#010x: %d bytes cannot hold a jmp.l" % (site, plen + size)
        )
    if kind == "mask":
        return kind, (site, want, match, nomatch)
    return kind, (site, want, head[:4].hex(), cmp & 7, match, nomatch)


def clone_sites(read, sites):
    """The profile's (function, site) pairs -> (eq tuples, mask tuple or None)."""
    eq, mask = [], None
    for _function, site in sites:
        kind, t = decode_clone_site(read, site)
        if kind == "eq":
            eq.append(t)
        elif mask is not None:
            raise SystemExit(
                "machinepatch: two mask sites, %#010x and %#010x" % (mask[0], site)
            )
        else:
            mask = t
    return eq, mask


PARTS = (
    "list",
    "dispatch",
    "group",
    "name",
    "rank",
    "permit",
    "hint",
    "pertype",
    "clone",
)

NEW_TYPE = 7  # the one new machine type this tool installs


class Anchors:
    """The image-specific addresses `plan_b` patches, taken from a profile.

    `tools/patchkit/machineprofile.py` holds these per image, keyed by MAIN OS SHA-256.
    Its `checks` tuple already carries the expected bytes at the nine
    load-bearing sites -- the same values this module kept as separate `*_WANT`
    constants -- so both the address and the bytes come from one `checks` entry
    and cannot drift apart.

    Cave offsets are deliberately not here. `TRAMP_OFF` and its siblings are
    this tool's own scratch layout, not firmware structure, so they stay
    module constants and are the same on every image.

    An anchor the profile does not carry is None rather than absent, and
    `require()` turns that into an error naming the part and the image. That is
    how Digitone II, which has no sort comparator and no per-type table, says
    so instead of planning a write to address None.
    """

    # (address attribute, expected-bytes attribute, check label, profile key).
    # The address comes from the named profile field where there is one, so an
    # image whose anchors are known but whose bytes have not been checked yet
    # still plans; the expected bytes come from `checks` and are None without
    # one. `name table lea` has no named field, so that site is reachable only
    # through its check -- a profile gap worth closing if another image needs it.
    FROM_CHECKS = (
        ("DISPATCH", "DISPATCH_WANT", "dispatch head", "dispatch"),
        ("END_SITE", "END_WANT", "list end pea", "list_end_site"),
        ("START_SITE", "START_WANT", "list start pea", "list_start_site"),
        ("GROUP_ADDR", "GROUP_WANT", "group bound", "group_bound"),
        ("NAME_ADDR", "NAME_HEAD_WANT", "name accessor head", None),
        ("NAME_LEA_ADDR", "NAME_LEA_WANT", "name table lea", None),
        ("RANK_CALL", "RANK_CALL_WANT", "rank insert jsr", "rank_call"),
        ("PERMIT_BOUND_ADDR", "PERMIT_BOUND_WANT", "permit bound", "permit_bound"),
        ("PERMIT_LEA_ADDR", "PERMIT_LEA_WANT", "permit table lea", "permit_lea"),
    )

    # (profile key, attribute): the anchors that are a plain address or count.
    FROM_FIELDS = (
        ("descriptor_base", "DESCRIPTOR_BASE"),
        ("descriptor_stride", "DESCRIPTOR_STRIDE"),
        ("fallback_descriptor", "FALLBACK_DESCRIPTOR"),
        ("list_source", "TABLE_D_LO"),
        ("name_table", "NAME_TABLE_SRC"),
        ("name_table_rows", "NAME_TABLE_ROWS"),
        ("name_table_row_bytes", "NAME_TABLE_ROW_BYTES"),
        ("rank_insert", "RANK_INSERT"),
        ("rank_guard", "RANK_GUARD"),
        ("permit_table", "PERMIT_TABLE_SRC"),
        ("permit_table_rows", "PERMIT_TABLE_ROWS"),
        ("pertype_table", "PERTYPE_TABLE_SRC"),
        ("pertype_sites", "PERTYPE_SITES"),
        ("cave_a", "DEFAULT_CAVE"),
        ("cave_b", "DEFAULT_CAVE_B"),
    )

    def __init__(self, profile):
        self.profile = profile
        self.name = profile["name"]
        self.count = profile["machine_count"]
        # The new machine takes the next type number after the stock ones, and
        # every bound this tool raises is raised to exactly that.
        self.NEW_TYPE = self.count
        checks = {
            label: (addr, bytes.fromhex(want))
            for addr, want, label in profile["checks"]
        }
        accessors = profile.get("name_accessors") or ()
        for addr_attr, want_attr, label, key in self.FROM_CHECKS:
            checked, want = checks.get(label, (None, None))
            addr = profile.get(key) if key else None
            if addr_attr == "NAME_ADDR" and addr is None and accessors:
                addr = accessors[0][0]
            if addr is not None and checked is not None and addr != checked:
                raise SystemExit(
                    "machinepatch: %s disagrees with itself: %s is %#010x but "
                    "its %r check is at %#010x"
                    % (self.name, key or addr_attr, addr, label, checked)
                )
            setattr(self, addr_attr, checked if addr is None else addr)
            setattr(self, want_attr, want)
        for key, attr in self.FROM_FIELDS:
            setattr(self, attr, profile.get(key))
        end = profile.get("list_source_end")
        self.TABLE_D_HI = None if end is None else end - 1
        # The other two name-table accessors, as (function, column offset).
        self.LABEL_FUNCS = tuple(
            (addr, table - self.NAME_TABLE_SRC) for addr, table in accessors[1:]
        )
        # `moveq #count,D1; cmp.l D0,D1; bcs.s +4`: the range test that replaces
        # the stock equality against count - 1.
        self.GROUP_NEW = bytes([0x72, self.count]) + bytes.fromhex("b2806504")
        # Entry `clone_of`'s nine descriptor longs. The descriptor array is
        # bss, so these cannot be read from a static image; a profile that has
        # not had them captured from a run leaves this None and the caller must
        # pass spec.fields.
        self.ENTRY_FIELDS = profile.get("entry_fields")

    def require(self, part, *attrs):
        """SystemExit naming every anchor this image does not carry."""
        missing = [a for a in attrs if getattr(self, a, None) is None]
        if missing:
            raise SystemExit(
                "machinepatch: %s carries no %s, so the %r part cannot be "
                'planned for it. See machineprofile.PROFILES[...]["missing"].'
                % (self.name, ", ".join(missing), part)
            )


def anchors_for(profile=None):
    """-> Anchors for a profile dict, or for Digitakt II 1.15C by default."""
    if profile is None:
        profile = machineprofile.DT2_115C
    if isinstance(profile, Anchors):
        return profile
    return Anchors(profile)


@dataclass(frozen=True)
class MachineSpec:
    """One new machine, type NEW_TYPE. Only one is supported: every bound this
    tool raises is raised to exactly 7."""

    name: str = "Placeholder"  # display-name table, long; the UI upper-cases it
    short: str = "PLC"  # display-name table, abbreviation
    desc_name: str = "PLACEHOLDER"  # descriptor std::string rep, long
    desc_short: str = "PLHD"  # descriptor std::string rep, short
    clone_of: int = 6  # stock type whose nine descriptor fields are copied
    fields: tuple = None  # nine longwords overriding clone_of's (rung 3)
    position: int = 7  # display position in the source list, 0..7


DEFAULT_SPEC = MachineSpec()

# The fixed cave layout's slot sizes: the descriptor reps' chars run from
# LCHARS_OFF/SCHARS_OFF to the next slot, 0x14 bytes each including the NUL;
# LONGSTR_OFF/SHORTSTR_OFF to the next slot are 0x10 bytes each including
# the NUL.
DESC_NAME_MAX = SNAME_OFF - LCHARS_OFF - 1
DESC_SHORT_MAX = TABLE_B_OFF - SCHARS_OFF - 1
NAME_MAX = SHORTSTR_OFF - LONGSTR_OFF - 1
SHORT_MAX = RANK_SHIM_OFF - SHORTSTR_OFF - 1


def spec_from_arg(value):
    """'NAME:SHORT[:CLONE_OF[:POSITION]]' -> MachineSpec. The descriptor's own
    names are the display names upper-cased."""
    parts = value.split(":")
    if not 2 <= len(parts) <= 4:
        raise SystemExit(
            "machinepatch: --machine wants NAME:SHORT[:CLONE_OF[:POSITION]], "
            "got %r" % value
        )
    name, short = parts[0], parts[1]
    clone_of = int(parts[2], 0) if len(parts) >= 3 else MachineSpec.clone_of
    position = int(parts[3], 0) if len(parts) >= 4 else MachineSpec.position
    return MachineSpec(
        name=name,
        short=short,
        desc_name=name.upper(),
        desc_short=short.upper(),
        clone_of=clone_of,
        position=position,
    )


def validate_spec(spec, count=7):
    """SystemExit on a spec that cannot be installed on a `count`-machine image."""
    for field_name in ("name", "short", "desc_name", "desc_short"):
        value = getattr(spec, field_name)
        if not value:
            raise SystemExit("machinepatch: spec.%s must not be empty" % field_name)
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            raise SystemExit(  # noqa: B904
                "machinepatch: spec.%s must be pure ASCII, got %r" % (field_name, value)
            )
    if len(spec.desc_name) > DESC_NAME_MAX:
        raise SystemExit(
            "machinepatch: spec.desc_name %r is longer than %d "
            "chars" % (spec.desc_name, DESC_NAME_MAX)
        )
    if len(spec.desc_short) > DESC_SHORT_MAX:
        raise SystemExit(
            "machinepatch: spec.desc_short %r is longer than %d "
            "chars" % (spec.desc_short, DESC_SHORT_MAX)
        )
    if len(spec.name) > NAME_MAX:
        raise SystemExit(
            "machinepatch: spec.name %r is longer than %d chars" % (spec.name, NAME_MAX)
        )
    if len(spec.short) > SHORT_MAX:
        raise SystemExit(
            "machinepatch: spec.short %r is longer than %d chars"
            % (spec.short, SHORT_MAX)
        )
    if spec.clone_of not in range(count):
        raise SystemExit(
            "machinepatch: spec.clone_of must be 0..%d, got %r"
            % (count - 1, spec.clone_of)
        )
    if spec.position not in range(count + 1):
        raise SystemExit(
            "machinepatch: spec.position must be 0..%d, got %r" % (count, spec.position)
        )
    if spec.fields is not None and (
        len(spec.fields) != 9
        or not all(isinstance(f, int) and 0 <= f <= 0xFFFFFFFF for f in spec.fields)
    ):
        raise SystemExit(
            "machinepatch: spec.fields must be 9 ints, each "
            "0..0xffffffff, got %r" % (spec.fields,)
        )


# These describe DEFAULT_SPEC only -- kept as module constants because
# tools/uidrive.py reads them.
LONG_NAME = DEFAULT_SPEC.desc_name
SHORT_NAME = DEFAULT_SPEC.desc_short
LONGSTR = DEFAULT_SPEC.name.encode("ascii") + b"\x00"
SHORTSTR = DEFAULT_SPEC.short.encode("ascii") + b"\x00"


def build_trampoline(cave_b, profile=None):
    """The cave dispatch: the new type gets the cave descriptor, a stock type
    indexes the original array, anything else falls back -- the same forgiving
    failure mode as the code it replaces."""
    a = anchors_for(profile)
    desc = cave_b + DESC_OFF
    return (
        bytes.fromhex("202f0004")
        + bytes([0x72, a.NEW_TYPE])  # moveq #new,D1
        + bytes.fromhex("b280")
        + bytes.fromhex("6608")
        + bytes.fromhex("203c")
        + struct.pack(">I", desc)
        + bytes.fromhex("4e75")
        + bytes([0x72, a.count - 1])  # moveq #count-1,D1
        + bytes.fromhex("b280")
        + bytes.fromhex("6510")
        + bytes.fromhex("123c")
        + struct.pack(">H", a.DESCRIPTOR_STRIDE)
        + bytes.fromhex("4c010800")
        + bytes.fromhex("0680")
        + struct.pack(">I", a.DESCRIPTOR_BASE)
        + bytes.fromhex("4e75")
        + bytes.fromhex("203c")
        + struct.pack(">I", a.FALLBACK_DESCRIPTOR)
        + bytes.fromhex("4e75")
    )


def build_descriptor(cave_b, fields):
    return struct.pack(">11I", cave_b + LCHARS_OFF, cave_b + SCHARS_OFF, *fields)


def build_rep(name):
    chars = name.encode("ascii") + b"\x00"
    return struct.pack(">IIi", len(name), len(name), -1) + chars


def build_rank_table(order):
    """(type, display position) pairs, the same shape as FUN_400517c4's own
    seven-pair initialiser -- which is exactly this over ORIGINAL_TABLE."""
    return b"".join(struct.pack(">II", t, i) for i, t in enumerate(order))


def build_rank_shim(cave_b, table_len, rank_insert=RANK_INSERT):
    """Replace the insert's [begin, end) stack arguments, then tail-jump to it.

    Entered by the repointed jsr at RANK_CALL, so 4(a7) is the map, 8(a7)
    begin and 0xc(a7) end; the caller's `lea $20(a7), a7` discards both
    afterwards, so overwriting them is safe.

    `rank_insert` is the image's range insert (`a.RANK_INSERT`); the 1.15C
    default is only for callers without a profile.
    """
    table = cave_b + RANK_TABLE_OFF
    return (
        bytes.fromhex("2f7c")
        + struct.pack(">I", table)
        + bytes.fromhex("0008")
        + bytes.fromhex("2f7c")
        + struct.pack(">I", table + table_len)
        + bytes.fromhex("000c")
        + bytes.fromhex("4ef9")
        + struct.pack(">I", rank_insert)
    )


def build_eq_shim(prefix, reg, match, nomatch, new_type=NEW_TYPE):
    """Replay a site's 4-byte compare; go to `match` if it matched or the
    type is the new one."""
    assert len(prefix) == 4
    return (
        prefix
        + bytes.fromhex("67000012")  # beq.w match
        + struct.pack(">HI", 0x0C80 | reg, new_type)  # cmpi.l #new,Dreg
        + bytes.fromhex("67000008")  # beq.w match
        + bytes.fromhex("4ef9")
        + struct.pack(">I", nomatch)
        + bytes.fromhex("4ef9")
        + struct.pack(">I", match)
    )


def build_mask_shim(clone_of, match, nomatch, new_type=NEW_TYPE):
    """FUN_4005cb7c: turn the new type into clone_of, then replay
    (type & ~2) == 4."""
    return (
        struct.pack(">HI", 0x0C80, new_type)  # cmpi.l #new,D0
        + bytes.fromhex("6602")  # bne.b +2
        + bytes([0x70, clone_of])  # moveq #clone_of,D0
        + bytes.fromhex(
            "72fdc0817204b280"
        )  # moveq #-3,D1; and.l D1,D0; moveq #4,D1; cmp.l D0,D1
        + bytes.fromhex("67000008")  # beq.w match
        + bytes.fromhex("4ef9")
        + struct.pack(">I", nomatch)
        + bytes.fromhex("4ef9")
        + struct.pack(">I", match)
    )


def check_parts(parts):
    unknown = [p for p in parts if p not in PARTS]
    if unknown or not parts:
        raise SystemExit(
            "machinepatch: unknown part(s) %s; known parts are %s"
            % (", ".join(repr(p) for p in unknown) or "(none given)", "+".join(PARTS))
        )


def plan_b(read, cave_b, parts=PARTS, eighth=None, spec=DEFAULT_SPEC, profile=None):
    """-> [(addr, old, new), ...] in write order.

    Every byte comes from `read(addr, n) -> bytes` or from the spec, and every
    address from `profile`, so the same plan can be applied to live guest
    memory or to a static MAIN OS image, for any image `machineprofile.py`
    carries anchors for. `profile` defaults to Digitakt II 1.15C, which is what
    every address in this module used to be hard-coded to.

    One image-specific value is still not in the profile: `ENTRY6_FIELDS`, the
    nine descriptor longs cloned when `spec.fields` is None. The descriptor
    array is bss, so it cannot be read from a static image and has to be
    captured from a run. Pass `spec.fields` explicitly for any image other
    than 1.15C.
    """
    a = anchors_for(profile)
    prof = profile if profile is not None else machineprofile.DT2_115C
    check_parts(parts)
    validate_spec(spec, a.count)
    if eighth is None:
        eighth = a.NEW_TYPE
    writes = []

    if "dispatch" in parts:
        cur = read(a.DISPATCH, 6)
        if cur != a.DISPATCH_WANT:
            raise SystemExit(
                "machinepatch: %#010x holds %s, expected %s"
                % (a.DISPATCH, cur.hex(), a.DISPATCH_WANT.hex())
            )
    if "list" in parts:
        for site, want in ((a.END_SITE, a.END_WANT), (a.START_SITE, a.START_WANT)):
            cur = read(site, 6)
            if cur != want:
                raise SystemExit(
                    "machinepatch: %#010x holds %s, expected %s"
                    % (site, cur.hex(), want.hex())
                )
    if "group" in parts:
        cur = read(a.GROUP_ADDR, len(a.GROUP_WANT))
        if cur != a.GROUP_WANT:
            raise SystemExit(
                "machinepatch: %#010x holds %s, expected %s"
                % (a.GROUP_ADDR, cur.hex(), a.GROUP_WANT.hex())
            )
    if "name" in parts:
        cur = read(a.NAME_ADDR, len(a.NAME_HEAD_WANT))
        if cur != a.NAME_HEAD_WANT:
            raise SystemExit(
                "machinepatch: %#010x holds %s, expected %s"
                % (a.NAME_ADDR, cur.hex(), a.NAME_HEAD_WANT.hex())
            )
        cur = read(a.NAME_LEA_ADDR, len(a.NAME_LEA_WANT))
        if cur != a.NAME_LEA_WANT:
            raise SystemExit(
                "machinepatch: %#010x holds %s, expected %s"
                % (a.NAME_LEA_ADDR, cur.hex(), a.NAME_LEA_WANT.hex())
            )
    if "rank" in parts:
        cur = read(a.RANK_CALL, len(a.RANK_CALL_WANT))
        if cur != a.RANK_CALL_WANT:
            raise SystemExit(
                "machinepatch: %#010x holds %s, expected %s"
                % (a.RANK_CALL, cur.hex(), a.RANK_CALL_WANT.hex())
            )
    if "permit" in parts:
        cur = read(a.PERMIT_BOUND_ADDR, len(a.PERMIT_BOUND_WANT))
        if cur != a.PERMIT_BOUND_WANT:
            raise SystemExit(
                "machinepatch: %#010x holds %s, expected %s"
                % (a.PERMIT_BOUND_ADDR, cur.hex(), a.PERMIT_BOUND_WANT.hex())
            )
        cur = read(a.PERMIT_LEA_ADDR, len(a.PERMIT_LEA_WANT))
        if cur != a.PERMIT_LEA_WANT:
            raise SystemExit(
                "machinepatch: %#010x holds %s, expected %s"
                % (a.PERMIT_LEA_ADDR, cur.hex(), a.PERMIT_LEA_WANT.hex())
            )

    if spec.fields is not None:
        fields = spec.fields
    elif spec.clone_of == 6:
        fields = ENTRY6_FIELDS
    else:
        # The descriptor array is bss, so a static-image caller must pass
        # `fields` for any clone other than 6.
        fields = struct.unpack(
            ">9I", read(a.DESCRIPTOR_BASE + spec.clone_of * a.DESCRIPTOR_STRIDE + 8, 36)
        )

    order = list(struct.unpack(">%dI" % a.count, read(a.TABLE_D_LO, a.count * 4)))
    order.insert(spec.position, eighth)

    def add(addr, new):
        old = read(addr, len(new))
        writes.append((addr, old, new))

    if "dispatch" in parts:
        add(cave_b + TRAMP_OFF, build_trampoline(cave_b, a))
        add(cave_b + DESC_OFF, build_descriptor(cave_b, fields))
        add(cave_b + LNAME_OFF, build_rep(spec.desc_name))
        add(cave_b + SNAME_OFF, build_rep(spec.desc_short))

    if "list" in parts:
        tbytes = struct.pack(">8I", *order)
        add(cave_b + TABLE_B_OFF, tbytes)
        for site, new_ptr in (
            (a.START_SITE, cave_b + TABLE_B_OFF),
            (a.END_SITE, cave_b + TABLE_B_OFF + len(tbytes)),
        ):
            add(site + 2, struct.pack(">I", new_ptr))

    if "group" in parts:
        add(a.GROUP_ADDR, a.GROUP_NEW)

    if "name" in parts:
        table_addr = cave_b + NAME_TABLE_OFF
        long_addr = cave_b + LONGSTR_OFF
        short_addr = cave_b + SHORTSTR_OFF
        longstr = spec.name.encode("ascii") + b"\x00"
        shortstr = spec.short.encode("ascii") + b"\x00"

        rows = read(a.NAME_TABLE_SRC, a.NAME_TABLE_ROWS * a.NAME_TABLE_ROW_BYTES)
        add(table_addr, rows)

        row8 = struct.pack(">III", long_addr, short_addr, 0)
        row8_addr = table_addr + a.NAME_TABLE_ROWS * a.NAME_TABLE_ROW_BYTES
        add(row8_addr, row8)

        add(long_addr, longstr)
        add(short_addr, shortstr)

        # The bound is the moveq's IMMEDIATE, the second byte of `72 06`, not
        # the opcode byte -- writing at NAME_ADDR itself destroys the
        # instruction.
        add(a.NAME_ADDR + 1, b"\x07")

        add(a.NAME_LEA_ADDR + 2, struct.pack(">I", table_addr))

    if "rank" in parts:
        table = build_rank_table(order)
        add(cave_b + RANK_TABLE_OFF, table)

        shim = build_rank_shim(cave_b, len(table), a.RANK_INSERT)
        add(cave_b + RANK_SHIM_OFF, shim)

        # The jsr's operand only, not its opcode.
        add(a.RANK_CALL + 2, struct.pack(">I", cave_b + RANK_SHIM_OFF))

    if "dispatch" in parts:
        add(a.DISPATCH, b"\x4e\xf9" + struct.pack(">I", cave_b))

    if "permit" in parts:
        slot = cave_b + PERMIT_TABLE_OFF
        old = read(slot, 32)
        if old != b"\x00" * 32:
            raise SystemExit(
                "machinepatch: cave slot %#010x is not free (holds %s)"
                % (slot, old.hex())
            )
        stock = read(a.PERMIT_TABLE_SRC, 28)
        table = stock + stock[spec.clone_of * 4 : spec.clone_of * 4 + 4]
        writes.append((slot, old, table))
        writes.append(
            (
                a.PERMIT_LEA_ADDR,
                a.PERMIT_LEA_WANT,
                bytes.fromhex("41f9") + struct.pack(">I", slot),
            )
        )
        writes.append(
            (a.PERMIT_BOUND_ADDR, a.PERMIT_BOUND_WANT, bytes([0x74, a.NEW_TYPE]))
        )

    if "hint" in parts:
        if "name" not in parts:
            raise SystemExit(
                "machinepatch: hint needs name, whose relocated table it fills"
            )
        table_addr = cave_b + NAME_TABLE_OFF
        for func, column in a.LABEL_FUNCS:
            addi = bytes.fromhex("0680") + struct.pack(">I", a.NAME_TABLE_SRC + column)
            for addr, want in ((func, a.NAME_HEAD_WANT), (func + LABEL_ADDI_OFF, addi)):
                cur = read(addr, len(want))
                if cur != want:
                    raise SystemExit(
                        "machinepatch: %#010x holds %s, expected %s"
                        % (addr, cur.hex(), want.hex())
                    )
            add(func + 1, bytes([a.NEW_TYPE]))
            add(func + LABEL_ADDI_OFF + 2, struct.pack(">I", table_addr + column))
        # The name part leaves row 8's hint 0; give it clone_of's.
        add(
            table_addr + a.NAME_TABLE_ROWS * a.NAME_TABLE_ROW_BYTES + 8,
            read(a.NAME_TABLE_SRC + spec.clone_of * a.NAME_TABLE_ROW_BYTES + 8, 4),
        )

    if "pertype" in parts:
        slot = cave_b + PERTYPE_TABLE_OFF
        old = read(slot, 8)
        if old != bytes(8):
            raise SystemExit(
                "machinepatch: cave slot %#010x is not free (holds %s)"
                % (slot, old.hex())
            )
        for bound, lea in a.PERTYPE_SITES:
            cur = read(bound, 2)
            if cur[0] & 0xF1 != 0x70 or cur[1] != 6:
                raise SystemExit(
                    "machinepatch: %#010x holds %s, expected moveq #6,Dn"
                    % (bound, cur.hex())
                )
            cur = read(lea, 6)
            if (
                cur[0] & 0xF1 != 0x41
                or cur[1] != 0xF9
                or cur[2:] != struct.pack(">I", a.PERTYPE_TABLE_SRC)
            ):
                raise SystemExit(
                    "machinepatch: %#010x holds %s, expected lea (%#010x).l,An"
                    % (lea, cur.hex(), a.PERTYPE_TABLE_SRC)
                )
        stock = read(a.PERTYPE_TABLE_SRC, 7)
        add(slot, stock + stock[spec.clone_of : spec.clone_of + 1])
        for bound, lea in a.PERTYPE_SITES:
            add(bound + 1, bytes([a.NEW_TYPE]))
            add(lea + 2, struct.pack(">I", slot))

    if "clone" in parts:
        eq, mask = clone_sites(
            read, prof["clone_sites"] if spec.clone_of == CLONE_SITES_OF else ()
        )
        sites = [
            (
                addr,
                bytes.fromhex(want),
                build_eq_shim(bytes.fromhex(prefix), reg, match, nomatch, a.NEW_TYPE),
            )
            for addr, want, prefix, reg, match, nomatch in eq
        ]
        if mask is not None:
            addr, want, match, nomatch = mask
            sites.append(
                (
                    addr,
                    bytes.fromhex(want),
                    build_mask_shim(spec.clone_of, match, nomatch, a.NEW_TYPE),
                )
            )
        shim = cave_b + CLONE_SHIM_OFF
        total = sum(len(code) for _, _, code in sites)
        old = read(shim, total)
        if old != bytes(total):
            raise SystemExit(
                "machinepatch: cave slot %#010x is not free (holds %s)"
                % (shim, old.hex())
            )
        for addr, want, code in sites:
            cur = read(addr, len(want))
            if cur != want:
                raise SystemExit(
                    "machinepatch: %#010x holds %s, expected %s"
                    % (addr, cur.hex(), want.hex())
                )
            add(shim, code)
            jump = bytes.fromhex("4ef9") + struct.pack(">I", shim)
            add(addr, jump + bytes.fromhex("4e71") * ((len(want) - len(jump)) // 2))
            shim += len(code)

    return writes
