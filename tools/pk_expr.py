"""A stand-in for patchkit's manifest expression language, for tests of module.toml files.

Integers, symbols (dotted names), ``$field`` (a field of the entry a registry row is made for), ``image.u8|u16|u32(expr)``
(a big-endian value read from the firmware image), ``u8(expr)`` / ``u16(expr)`` (a typed row word, value only),
the operators ``+ - * / & | << >>`` and parentheses. The real evaluator is native/patchkit; this one keeps the
manifests honest in Python.
"""

from __future__ import annotations

import re
from collections.abc import Callable


def evaluate(
    expr: str | int,
    symbols: dict[str, int] | None = None,
    fields: dict[str, int] | None = None,
    image: Callable[[int, int], bytes] | None = None,
) -> int:
    e = str(expr)
    e = re.sub(r"\$(\w+)", r"_f_\1", e)
    e = re.sub(r"image\.u(8|16|32)", r"_img\1", e)
    e = re.sub(r"\bu(8|16)\(", r"_t\1(", e)
    e = re.sub(
        r"[A-Za-z_][\w.]*(?=[^(\w.]|$)", lambda m: m.group(0).replace(".", "_"), e
    )
    env: dict[str, object] = {f"_f_{k}": v for k, v in (fields or {}).items()}
    env.update({k.replace(".", "_"): v for k, v in (symbols or {}).items()})
    for w, nb in ((8, 1), (16, 2), (32, 4)):
        env[f"_img{w}"] = lambda a, nb=nb: int.from_bytes(image(a, nb), "big")  # type: ignore[misc]
    env["_t8"] = lambda v: v & 0xFF
    env["_t16"] = lambda v: v & 0xFFFF
    return int(eval(e.replace("/", "//"), {"__builtins__": {}}, env))  # noqa: S307
