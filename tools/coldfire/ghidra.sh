#!/bin/sh
# Ghidra headless over the MAIN OS image, for the things dt2/coldfire.py cannot do.
#
# dt2/coldfire.py is a linear sweep. It has no cross-references, no function
# boundaries and no decompiler, and its "xref" substitute -- searching the image
# for the big-endian address constant -- silently misses PC-relative calls. That
# is not a corner case: `jsr $401772cc(pc)` at 0x4017845e encodes as 4eba ee6c
# and contains the target nowhere, so a byte search reports the function as
# never called while it is on the boot path. Use Ghidra whenever the question is
# "who calls this" or "what does this actually do".
#
#   tools/coldfire/ghidra.sh import                 # one-time, ~3 min for the 3.1MB image
#   tools/coldfire/ghidra.sh run MyScript.java [args...]
#
# GHIDRA_FOLDER=dt2-1.16 puts the program in that project folder, so several
# images named section_3_MAIN_OS.bin fit in one project; `run` then processes
# the program in that folder. tools/coldfire/ghidracopy.py copies a program between
# projects.
#
# Scripts go in tools/coldfire/ghidra/ and must be Java: this Ghidra is built without
# PyGhidra, so .py scripts fail with "Python is not available".
set -e

GHIDRA=${GHIDRA:-/opt/homebrew/Cellar/ghidra/12.1.3/libexec/support/analyzeHeadless}
# NOT under ~/.cache: Ghidra rejects any project path element beginning with a
# dot ("Path element starting with '.' is not permitted") and both `import`
# and `run` died on it, `run` quietly enough that it looked like the scripts
# were returning no results.
PROJ=${GHIDRA_PROJ:-$HOME/ghidra-projects/dt2}
NAME=${GHIDRA_NAME:-dt2}
IMG=${GHIDRA_IMG:-sections/section_3_MAIN_OS.bin}
BASE=0x40000400            # dspboot.MAIN_LOAD
# NOT plain 68000: MVS/MVZ and FF1 decode wrong. GHIDRA_LANG=68000:BE:32:ColdfireEMAC
# (tools/coldfire/ghidra/install-coldfire-emac.sh) also decodes the EMAC instructions.
LANG=${GHIDRA_LANG:-68000:BE:32:Coldfire}
# analyzeHeadless takes the project folder after the project name.
TARGET=$NAME${GHIDRA_FOLDER:+/$GHIDRA_FOLDER}

[ -x "$GHIDRA" ] || { echo "no analyzeHeadless at $GHIDRA (set GHIDRA=)" >&2; exit 1; }

case "$1" in
import)
    [ -f "$IMG" ] || { echo "missing $IMG -- extract sections first" >&2; exit 1; }
    mkdir -p "$PROJ"
    exec "$GHIDRA" "$PROJ" "$TARGET" -import "$IMG" \
         -processor "$LANG" -loader BinaryLoader -loader-baseAddr "$BASE"
    ;;
run)
    shift
    script=$1; shift
    exec "$GHIDRA" "$PROJ" "$TARGET" -process "$(basename $IMG)" -noanalysis \
         -scriptPath "$(cd "$(dirname "$0")/ghidra" && pwd)" -postScript "$script" "$@"
    ;;
*)
    sed -n '2,21p' "$0"; exit 1;;
esac
