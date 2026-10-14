"""Read-only, narrow MMIO JSONL observation for :class:`emu.harness.Machine`."""

import json


class JsonlMmioTrace:
    """A small trace sink; ``event`` has no access to Unicorn state.

    The observer supplies values when Unicorn exposes them.  ``None`` plus
    ``read_phase`` documents an unavailable post-read value rather than
    pretending the memory backing is a hardware response.
    """

    def __init__(self, path):
        self.file = open(path, "w", encoding="utf-8")  # noqa: SIM115
        self.sequence = 0
        self.closed = False

    def event(
        self,
        *,
        pc,
        address,
        width,
        direction,
        value,
        register=None,
        instruction_count=None,
        read_phase=None,
    ):
        record = {
            "address": "0x%08x" % address,
            "direction": direction,
            "instruction_count": instruction_count,
            "pc": "0x%08x" % pc,
            "register": register,
            "sequence": self.sequence,
            "value": value,
            "width": width,
        }
        if read_phase is not None:
            record["read_phase"] = read_phase
        self.file.write(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.file.flush()
        self.sequence += 1

    def close(self):
        if not self.closed:
            self.file.flush()
            self.file.close()
            self.closed = True
