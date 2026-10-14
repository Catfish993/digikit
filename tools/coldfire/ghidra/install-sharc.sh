#!/bin/sh
# Generate the SHARC+ VISA language, compile it and install it into the Ghidra
# tree as Processors/SHARC_VISA (language SHARC_VISA:LE:32:default).
#
# tools/sharc_dev/sharcspec/ghidra/gen_sleigh.py writes the module from
# tools/sharc_dev/sharcspec/decode_table.json into tools/sharc_dev/sharcspec/ghidra/SHARC_VISA/
# (git-ignored). Stock Ghidra 12.1.3 ships no SHARC, Blackfin or ADSP processor
# module, and a Homebrew upgrade of Ghidra wipes the install, so re-run this
# after an upgrade. The older module from this repo, Processors/SHARC
# (SHARC:LE:32:VISA), is left in place for programs imported with it.
set -e
GHIDRA="${GHIDRA_INSTALL_DIR:-/opt/homebrew/Cellar/ghidra/12.1.3/libexec}"
HERE=$(cd "$(dirname "$0")" && pwd)
GEN=$(cd "$HERE/../../sharc_dev/sharcspec/ghidra" && pwd)
(cd "$GEN" && uv run python gen_sleigh.py)
"$GHIDRA/support/sleigh" -a "$GEN/SHARC_VISA/data/languages"
rm -rf "$GHIDRA/Ghidra/Processors/SHARC_VISA"
cp -R "$GEN/SHARC_VISA" "$GHIDRA/Ghidra/Processors/SHARC_VISA"
echo "installed SHARC_VISA:LE:32:default into $GHIDRA/Ghidra/Processors/SHARC_VISA"
