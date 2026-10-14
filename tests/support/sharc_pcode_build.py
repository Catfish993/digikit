"""Field and branch encoders for the SHARC+ p-code tests."""

from pathlib import Path

from tools.sharc_dev import sharc_visa_tables as T  # noqa: E402
from support.sharc_encode import encode  # noqa: E402


def field(name, label, value):
    """-> the bits that put value into field `label` of form `name`."""
    hi, lo = T.get_type(name)["fields"][label]
    return (value & ((1 << (hi - lo + 1)) - 1)) << lo


def branch(name, cond=None, b=None, target=None):
    """-> bytes of a form-`name` branch with the given cond, b bit and absolute target."""
    extra = 0
    if cond is not None:
        extra |= field(name, "cond[4:0]", cond)
    if b is not None:
        extra |= field(name, "b", b)
    if target is not None:
        extra |= field(name, "addr[23:16]", target >> 16) | field(
            name, "addr[15:0]", target
        )
    return encode(name, extra)
