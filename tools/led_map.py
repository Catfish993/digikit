#!/usr/bin/env python3
"""Per-key LED changes from a sweep capture (scenarios `dt2-led-sweep` / `dn2-led-sweep`).

    DIGI_PANEL_TX_LOG=FILE uv run python tools/coupled.py scenario --device dt2 dt2-led-sweep
    uv run python tools/led_map.py FILE [--names NAME_TABLE_ADDR]

For every injected button (the `@button` lines of the capture) prints the `b9 idx val` changes made while it was
held ("press") and from its release to the next press ("after"). The firmware repeats every LED record, so a
record that does not change the shadow state is dropped.
"""

import argparse
import collections

import panel_tx


def changes(recs, lo, hi, state):
    out = []
    for t, c, d in recs:
        if t < lo or t >= hi or c != 0xB9:
            continue
        idx, val = d[0], d[1]
        old = state.get(idx)
        if old != val:
            out.append((idx, old, val))
            state[idx] = val
    return out


def sweep(path):
    """[(code, press_changes, after_changes)] in press order."""
    recs = panel_tx.parse(panel_tx.load(path))
    marks = panel_tx.marks(path)
    state = {}
    result = []
    downs = [(t, c) for t, c, d in marks if d]
    ups = {c: t for t, c, d in marks if not d}
    # Anything before the first press only settles the initial state.
    changes(recs, 0, downs[0][0], state)
    for i, (td, code) in enumerate(downs):
        tu = ups.get(code, td)
        nxt = downs[i + 1][0] if i + 1 < len(downs) else float("inf")
        result.append(
            (code, changes(recs, td, tu, state), changes(recs, tu, nxt, state))
        )
    return result


def fmt(ch):
    return " ".join(
        f"{i:02x}:{'--' if o is None else f'{o:02x}'}>{n:02x}" for i, o, n in ch
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    a = ap.parse_args()
    seen = collections.Counter()
    for code, press, after in sweep(a.log):
        seen[code] += 1
        print(
            f"key {code:2d}  press: {fmt(press) or '-':<60} after: {fmt(after) or '-'}"
        )


if __name__ == "__main__":
    main()
