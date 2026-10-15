#!/usr/bin/env python3
"""Add the panel LED sweep scenarios to tests/scenarios/dt2.json and tests/scenarios/dn2.json.

    uv run python -m tools.emulator.gen_led_sweep

`dt2-led-sweep` / `dn2-led-sweep`: NO (12) closes the boot modal at 0, then every control code is pressed for
2M instructions and released, 4M apart, in ascending order from 40M (DT2 1..50, DN2 1..54). Capture with
`DIGI_PANEL_TX_LOG=FILE` and decode with `tools/emulator/led_map.py FILE --device dt2|dn2`. Not a PCM pin.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
START, SPACING, HOLD = 40_000_000, 12_000_000, 6_000_000
DEVICES = {"dt2": 50, "dn2": 54}


def scenario(dev: str, codes: int) -> dict:
    events = [
        {"at_instr": 0, "button": 12, "down": True},
        {"at_instr": 1_000_000, "button": 12, "down": False},
    ]
    for i in range(codes):
        t = START + i * SPACING
        events.append({"at_instr": t, "button": i + 1, "down": True})
        events.append({"at_instr": t + HOLD, "button": i + 1, "down": False})
    return {
        "name": f"{dev}-led-sweep",
        "description": f"Probe: NO, then buttons 1..{codes} pressed one at a time, "
        f"{SPACING // 1_000_000}M apart, for the LED index map. Not a pin.",
        "instructions": START + codes * SPACING + SPACING,
        "exact_events": True,
        "clip_last_chunk": True,
        "events": events,
    }


def main() -> None:
    for dev, codes in DEVICES.items():
        path = ROOT / "tests" / "scenarios" / f"{dev}.json"
        doc = json.loads(path.read_text())
        name = f"{dev}-led-sweep"
        doc["scenarios"] = [s for s in doc["scenarios"] if s["name"] != name]
        doc["scenarios"].append(scenario(dev, codes))
        path.write_text(json.dumps(doc, indent=2) + "\n")


if __name__ == "__main__":
    main()
