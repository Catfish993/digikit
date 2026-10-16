# Early-init contract v1

`docs/contracts/early-init-v1.json` is hand-authored evidence, not firmware
output.  It records seven documented PPM command writes and the separate,
unidentified `UNKNOWN_0xec09000e` read boundary.  Provenance remains distinct:
only the Digitakt SHA has QEMU evidence; Digitone is static-only.

Analyze explicit user-supplied MAIN images (the command never writes stdout):

```sh
uv run python -m tools.coldfire.early_init_contract \
  --main /path/to/digitakt-MAIN.bin --main /path/to/digitone-MAIN.bin \
  --qemu-seed /tmp/qemu-coldfire-spike/early-init-seed.json --out /tmp/early-init.json
```

The analyzer uses the existing narrow `tools.coldfire.disasm` decoder and emits stable,
sorted JSON. It rejects a changed or ambiguous bootstrap sequence. `emu.Machine`
can optionally observe explicit MMIO ranges with `install_mmio_trace(sink,
ranges=..., registers=...)`; it installs no trace hooks without a sink and
writes read-only JSONL events. `unicorn-reference-final:emu/longrun.py`'s `build` accepts `trace_path`,
`trace_ranges`, and `trace_registers` for the same narrow observer. A sink
created by `trace_path` is owned by its `Machine`; call `m.close()` (or use it
as a context manager) to close and flush that JSONL file.

The firmware-dependent integration check is deliberately opt-in:

```sh
DT2_EARLY_INIT_REAL=1 DT2_DIGITAKT_MAIN=/tmp/sec-dt/section_3_MAIN_OS.bin \
DT2_DIGITONE_MAIN=/tmp/sec-dn/section_3_MAIN_OS.bin \
DT2_EARLY_INIT_QEMU_SEED=/tmp/qemu-coldfire-spike/early-init-seed.json \
uv run python -m unittest tests.test_early_init_real
```

It verifies both checked-in SHA-256 values, deterministic output, eight exact
Digitakt QEMU joins, and zero Digitone QEMU joins.
