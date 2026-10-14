#!/usr/bin/env python3
"""Map ColdFire function entries from one cfdb image to others (tools/coldfire/cfmatch.py).

    uv run python -m tools.coldfire.cfport dt2-1.16-emac dn2-1.11-emac dt2-1.17-emac -- 0x400db24e 0x400df7f6

The first name is the source image, then the destination images, then `--`
and the source function entries. Prints one line per entry and image:
`SRC_ENTRY  DST_IMAGE  DST_ENTRY  method`, with `-` where no match exists.
A match found by the `gap` or `caller` stage is weak; check it by reading
both functions before relying on it.
"""

from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))

from tools.coldfire import cfmatch  # noqa: E402


def main(argv: list[str]) -> int:
    if "--" not in argv or len(argv) < 4:
        print(__doc__)
        return 2
    cut = argv.index("--")
    images, addrs = argv[:cut], [int(a, 0) for a in argv[cut + 1 :]]
    src, dsts = images[0], images[1:]
    for dst in dsts:
        mt = cfmatch.load_pair(src, dst)
        for a in addrs:
            d = mt.m.get(a)
            if d is None:
                print(f"{a:#x}  {dst}  -")
            else:
                print(f"{a:#x}  {dst}  {d:#x}  {mt.method.get(a, '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
