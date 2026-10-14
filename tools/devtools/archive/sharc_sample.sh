#!/bin/zsh
# usage: tools/sharc_sample.sh OUT [CONFIG]: run the fastcov scenario and sample the DSP process.
export DIGI_EMU_SYX=${DIGI_EMU_SYX:-$(cd "$(dirname "$0")/../.." && pwd)/Digitone_II_OS1.11.syx}
out=$1
config=${2:-default}
uv run python -m tools.devtools.fastcov --no-build --no-lock --diag no --seconds 10.5 --config $config > $out.run.txt 2>&1 &
pid=""
for i in $(seq 1 40); do
  pid=$(pgrep -f "release/coupled-run" | head -1)
  [ -n "$pid" ] && break
  sleep 0.5
done
sleep 3
sample $pid 4 -file $out >/dev/null 2>&1
wait
