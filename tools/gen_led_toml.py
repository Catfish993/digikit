#!/usr/bin/env python3
"""Write devices/digitakt-ii-leds.toml and devices/digitone-ii-leds.toml (panel key -> LED index map).

    uv run python tools/gen_led_toml.py

The tables below are the result of the 2026-10-13 sweep (DT2 OS 1.16, DN2 OS 1.11): scenarios `dt2-led-sweep` /
`dn2-led-sweep`, `DIGI_PANEL_TX_LOG`, `tools/led_map.py`; LED names are the firmware's LED-name table read at
logical n = idx + 1 (docs/findings/19-sequencer-and-leds.md). Edit the tables, not the output.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# code -> (panel label, LED name or None, how the LED was seen)
#   "own"      lit/changed by the key's own press in the sweep
#   "radio"    selected by the press, the previous member of its group goes out
#   "context"  index from the firmware name table; not lit by a plain press in the sweep (lit by a view)
#   "none"     no LED in the firmware LED table
COMMON = {
    1: ("TRIG", "PAGE TRIG", "radio", 0x71),
    2: ("SRC", "PAGE SRC", "radio", 0x71),
    3: ("FLTR", "PAGE FILTER", "radio", 0x71),
    4: ("AMP", "PAGE AMP", "radio", 0x71),
    5: ("FX", "PAGE FX", "radio", 0x71),
    6: ("MOD", "PAGE MOD", "radio", 0x71),
    7: ("PRESET", "PRESET MENU", "radio", 0x71),
    8: ("SETTINGS", "GLOBAL", "radio", 0x71),
    9: ("TEMPO", "TEMPO", "radio", 0x71),
    10: ("YES", "YES", "context", None),
    11: ("UP", "UP", "context", None),
    12: ("NO", "NO", "context", None),
    13: ("LEFT", "LEFT", "context", 0x71),
    14: ("DOWN", "DOWN", "context", None),
    15: ("RIGHT", "RIGHT", "context", 0x71),
    16: ("TRK", "TRACK", "own", 0x71),
    17: ("FUNC", "FUNCTION", "context", None),
    18: ("KEYBOARD", "KEYBOARD", "own", 0x71),
    19: ("RECORD", "RECORD", "own", 0x11),
    20: ("PLAY", "PLAY", "own", 0x21),
    21: ("STOP", "STOP", "own", 0x71),
    22: ("PAGE", "PAGE", "context", None),
    23: ("PTN", "PATTERN", "own", 0x71),
    24: ("SONG", "SONG", "own", 0x71),
}
for n in range(16):
    COMMON[25 + n] = (f"TRIG {n + 1}", f"GRID{n + 1:02d}", "own", 0x11)
for n in range(8):
    COMMON[41 + n] = (f"ENCODER {'ABCDEFGH'[n]} push", None, "none", None)
COMMON[49] = ("LEVEL push", None, "none", None)

LED_NAMES = {
    # idx = logical - 1; logical 0 is UNDEFINED. Both products share 0-48 apart from SAMPLE / VOICE.
    0: "TRACK",
    1: "KEYBOARD",
    2: "FUNCTION",
    3: "PATTERN",
    4: "SONG",
    5: "PRESET MENU",
    6: "GLOBAL",
    7: "SAMPLE",
    8: "TEMPO",
    9: "RECORD",
    10: "PLAY",
    11: "STOP",
    12: "YES",
    13: "NO",
    14: "UP",
    15: "DOWN",
    16: "LEFT",
    17: "RIGHT",
    18: "PAGE",
    19: "PAGE TRIG",
    20: "PAGE SRC",
    21: "PAGE FILTER",
    22: "PAGE AMP",
    23: "PAGE FX",
    24: "PAGE MOD",
    **{25 + n: f"GRID{n + 1:02d}" for n in range(16)},
    **{41 + n: f"PAGE{n + 1}" for n in range(8)},
}

DEVICES = {
    "digitakt-ii": dict(
        short="dt2",
        firmware="Digitakt II OS 1.16",
        sweep="dt2-led-sweep",
        extra={50: ("SAMPLING", "SAMPLE", "own", 0x71)},
        names={},
        extra_leds={},
    ),
    "digitone-ii": dict(
        short="dn2",
        firmware="Digitone II OS 1.11",
        sweep="dn2-led-sweep",
        extra={
            50: ("VOICE", "VOICE", "own", 0x71),
            51: ("ARP", "ARP", "own", 0x71),
            52: ("PLUS", "PLUS", "context", 0x66),
            53: ("STACK", "STACK", "own", 0x71),
            54: ("MINUS", "MINUS", "context", 0x66),
        },
        names={7: "VOICE"},
        extra_leds={49: "ARP", 50: "PLUS", 51: "STACK", 52: "MINUS"},
    ),
}

HEADER = """\
# {firmware}: panel key code -> LED index (the `idx` of a `b9 idx val` front-panel record, UART8).
#
# Source: scenario `{sweep}` (every control code pressed 6M instructions, released, 12M apart), captured with
# DIGI_PANEL_TX_LOG and read with tools/led_map.py, on the emulated {firmware}, 2026-10-13. The LED index is
# (firmware LED logical id - 1); the names are the firmware's LED-name table. Regenerate with
# tools/gen_led_toml.py. Analysis and caveats: docs/findings/19-sequencer-and-leds.md.
#
# `seen` is how the LED was observed: own = the key's own press lights it or changes it; radio = pressing the key
# selects it and the previous group member goes out; context = the index comes from the firmware table but a plain
# press did not change it (a view lights it); none = the firmware has no LED for the key.
# `on` is the value byte written while the LED is lit by the key (see [value]); the idle value is 0x01.
#
# Each LED record is `b9 idx val`. Records are written twice back to back by the firmware's UART queue.

[value]
# Inference from the observed values (no panel-MCU firmware to read): the byte is a colour mask in bits 4-7 plus a
# 4-bit mode in bits 0-3. [O]
#   0x01            LED off (the idle value of every LED)
#   0x11 0x21 0x41  one colour channel at mode 1: bit 4 = red (RECORD armed, held or playhead trig key),
#                   bit 5 = green (PLAY), bit 6 = blue [D]
#   0x71            bits 4, 5 and 6 together at mode 1: white (page keys, TRK, STOP, a programmed trig)
#   0x81            bit 7 channel at mode 1 (a constant in the LED code; not seen on the wire here)
#   0x1a 0x5a 0x6a  colour mask with mode 0xa (FUNC held lights the trig keys 0x1a; TRK held: 0x6a; both are
#                   the firmware's pulse/blink values); 0x66 0x86 mode 6 (DN2 STACK mode: PLUS/MINUS 0x66, stacked
#                   steps 0x86)
#   0x02 0x06 0x0a  no colour bits, modes 2, 6 and 0xa: the dim levels of the PAGE1..8 indicator LEDs (0x0a = the
#                   current page, 0x02 = the other pages) and of the selected track's key (0x06)
bit0_idle = 1
colour_mask = 0xf0
mode_mask = 0x0f
red = 0x10
green = 0x20
blue = 0x40
other = 0x80
"""


def main() -> None:
    for name, dev in DEVICES.items():
        out = [HEADER.format(**dev)]
        keys = dict(COMMON)
        keys.update(dev["extra"])
        for code in sorted(keys):
            label, led_name, seen, on = keys[code]
            out.append("[[key]]")
            out.append(f"code = {code}")
            out.append(f'label = "{label}"')
            if led_name is None:
                out.append('seen = "none"')
            else:
                idx = (
                    code
                    if 25 <= code <= 40
                    else next(
                        i
                        for i, n in {
                            **LED_NAMES,
                            **dev["names"],
                            **dev["extra_leds"],
                        }.items()
                        if n == led_name
                    )
                )
                out.append(f"led = {idx}")
                out.append(f'led_name = "{led_name}"')
                out.append(f'seen = "{seen}"')
                if on is not None:
                    out.append(f"on = 0x{on:02x}")
            out.append("")
        leds = {**LED_NAMES, **dev["names"], **dev["extra_leds"]}
        out.append(
            "# LEDs that no key drives: the step playhead is a trig key's own LED, the page indicators below"
        )
        out.append("# are the pattern-page LEDs.")
        for idx in sorted(leds):
            if 41 <= idx <= 48:
                out.append("[[indicator]]")
                out.append(f"led = {idx}")
                out.append(f'name = "{leds[idx]}"')
                out.append("")
        (ROOT / "devices" / f"{name}-leds.toml").write_text("\n".join(out))


if __name__ == "__main__":
    main()
