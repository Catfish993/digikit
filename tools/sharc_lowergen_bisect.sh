#!/bin/zsh
# PCM and tier split with subsets of the generated forms (SHARC_FAST_GEN).
# usage: tools/sharc_lowergen_bisect.sh SECONDS CONFIG FORMS...
#   CONFIG a fastcov --config (static-insns+follow, default, fast-auto, ...)
#   FORMS  none, all, or a comma list of forms
export DIGI_EMU_SYX=${DIGI_EMU_SYX:-$PWD/../../../Digitone_II_OS1.11.syx}
secs=$1
config=$2
shift 2
for forms in "$@"; do
  if [ "$forms" = "none" ]; then
    out=$(uv run python tools/fastcov.py --no-build --no-lock --diag no --seconds $secs --config $config 2>&1)
  else
    out=$(SHARC_FAST_GEN=$forms uv run python tools/fastcov.py --no-build --no-lock --diag no --seconds $secs --config $config 2>&1)
  fi
  echo "$config $forms: $(echo "$out" | rg -o 'pcm [0-9a-f]+') $(echo "$out" | rg -o 'fast [0-9.]+%') $(echo "$out" | rg -o 'dsp cpu/audio s [0-9.]+')"
done
