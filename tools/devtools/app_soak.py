"""Drive the running desktop app over its control socket for a soak test.

Start the app with its log captured, then run this against the log:

    DIGI_SPIKE_LOG=/tmp/spikes mise run emu -- [FIRMWARE] [--audio-buffer S] > app.log 2>&1 &
    uv run python -m tools.devtools.app_soak app.log 60 [--dt2]
    uv run python -m tools.devtools.spikes /tmp/spikes

It reads the control socket from the app log, waits for the status call, taps Trig 1 every 2 s and
cycles the TRIG/SRC/FLTR/AMP/FX pages every 5 s (printing frame revision and frame hash), then quits
the app. --dt2 dismisses DT2's "FILE SYSTEM OK" window with NO first. The DSP's cost does not depend
on the FX settings, so this load stands for normal playing; page changes load the ColdFire.
"""

import hashlib
import json
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools.control.digi_client import Emu  # noqa: E402

log, dur = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 60
dt2 = "--dt2" in sys.argv
root = str(ROOT) + "/"
sock = None
t0 = time.time()
while time.time() - t0 < 3600:
    m = re.findall(
        r"control socket: (\S+\.sock)", pathlib.Path(log).read_text(errors="replace")
    )
    if m:
        sock = m[-1]
        break
    time.sleep(2)
if not sock:
    sys.exit("no socket line")
if not sock.startswith("/"):
    sock = root + sock
print("socket", sock, flush=True)
emu = None
while time.time() - t0 < 3600:
    try:
        emu = Emu.connect(sock)
        st = emu.status()
        break
    except Exception:
        time.sleep(2)
print("status", json.dumps(st)[:300], flush=True)
time.sleep(5)  # settle after boot
NAMES = {1: "TRIG", 2: "SRC", 3: "FLTR", 4: "AMP", 5: "FX"}


def fr():
    rev, d = emu.frame()
    return rev, hashlib.md5(d).hexdigest()[:8]


if dt2:
    print("pre-NO frame", fr(), flush=True)
    emu.button(12, True)
    time.sleep(0.3)
    emu.button(12, False)
    time.sleep(3)
    print("post-NO frame", fr(), flush=True)
pages = [1, 2, 3, 4, 5, 4, 3, 2]
s = time.time()
n = 0
pi = 0
nt = 0
np_ = 0
while (now := time.time() - s) < dur:
    if now >= nt:
        emu.button(25, True)
        time.sleep(0.15)
        emu.button(25, False)
        nt += 2
    if now >= np_:
        c = pages[pi % len(pages)]
        pi += 1
        emu.button(c, True)
        time.sleep(0.15)
        emu.button(c, False)
        np_ += 5
        time.sleep(1.0)
        print("page", NAMES[c], "frame", fr(), flush=True)
    time.sleep(0.1)
print("quit", emu.quit(), flush=True)
