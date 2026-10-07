#!/bin/zsh
# usage: tools/sharc_xctrace.sh OUT.trace [CONFIG]: CPU Profiler trace of the DSP scenario.
export DIGI_EMU_SYX=${DIGI_EMU_SYX:-/Users/em/src/digi/digitakt2/Digitone_II_OS1.11.syx}
out=$1
config=${2:-default}
rm -rf $out
uv run python tools/fastcov.py --no-build --no-lock --diag no --seconds 10.5 --config $config > $out.run.txt 2>&1 &
pid=""
for i in $(seq 1 40); do
  pid=$(pgrep -f "release/coupled-run" | head -1)
  [ -n "$pid" ] && break
  sleep 0.5
done
sleep 2
xcrun xctrace record --template 'CPU Profiler' --attach $pid --time-limit 5s --output $out > $out.rec.txt 2>&1
wait
