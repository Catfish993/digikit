"""One CPU budget for the whole verify gate (docs/TESTING.md, "CPU budget").

Before this module every step had its own pool (tier sweep cpus - 4 emulator processes, python-tests
one process per file, digikit files 3 daemons each, cargo and libtest on all cores) and up to 8 steps
ran at once, so the pools multiplied: a full run reached load average 141 on a 16-core machine
(2026-10-19). Now verify has a total budget of BUDGET cores and every running step holds a share of it:

* the budget is the physical cores - 2 (DIGI_VERIFY_BUDGET or `verify --jobs N` override it);
* a step asks for a fixed fraction of the budget (WANT); the scheduler admits it when that many cores
  (at least half of the ask, never fewer than one) are free, and the step gets what was free, at most
  its ask. The shares of the running steps never add up to more than the budget (class Budget);
* the share sets every inner pool of the step (step_env): CARGO_BUILD_JOBS, RUST_TEST_THREADS,
  RAYON_NUM_THREADS, DIGI_JOBS (scenario renders), DIGI_TIER_JOBS, DIGI_PY_JOBS, DIGIKIT_JOBS,
  DIGI_CONFORM_JOBS, DIGI_PK_JOBS. A coupled emulator process runs two busy threads (the ColdFire loop
  and the `sharc-dsp` worker of packages/emulator/boot/src/sharc_peer.rs; no environment variable
  changes that), so a pool of emulator processes has share // EMULATOR_CORES workers.

`lower_priority` puts verify and its children at nice 10 (not `taskpolicy -b`, which pins a process to
the efficiency cores of an Apple-silicon machine).
"""

from __future__ import annotations

import math
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field

EMULATOR_CORES = 2  # threads one coupled-run / digi-emud process keeps busy
NICE = 10
MAX_STEPS = 6  # steps running at once, whatever the budget

# What a step asks for, as a fraction of the budget (rounded, at least one core). Builds and cargo
# test take the cores that rustc can use (the generated core is one huge crate: more jobs do not
# help); the pools of emulator processes ask for the most.
WANT: dict[str, float] = {
    "build": 0.45,
    "build-dt2": 0.45,
    "build-daemons": 0.45,
    "desktop": 0.15,
    "web": 0.15,
    "rust-tests": 0.30,
    "python-tests": 0.45,
    "card": 0.15,
    "dt2-tone": 0.15,
    "pins": 0.50,
    "check": 0.50,
    "check-no-aot": 0.50,
    "dt2-check": 0.50,
    "tier-quick": 0.75,
    "tier-sweep": 0.75,
    "dt2-tier-quick": 0.75,
    "dt2-tier-sweep": 0.75,
}
DEFAULT_WANT = 0.30
BUILDS = ("build", "build-dt2", "build-daemons")
EMULATOR_POOLS = (
    "pins",
    "check",
    "check-no-aot",
    "dt2-check",
    "tier-quick",
    "tier-sweep",
    "dt2-tier-quick",
    "dt2-tier-sweep",
)


def physical_cores() -> int:
    """Physical cores (Apple silicon: performance + efficiency); os.cpu_count() when sysctl has no answer."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.physicalcpu"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return max(int(out), 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return os.cpu_count() or 2


def default_budget(cores: int | None = None) -> int:
    """Physical cores - 2, at least 1."""
    return max(1, (physical_cores() if cores is None else cores) - 2)


def resolve_budget(jobs: int = 0, environ: Mapping[str, str] | None = None) -> int:
    """The total budget: `--jobs N`, else DIGI_VERIFY_BUDGET, else the default."""
    environ = os.environ if environ is None else environ
    if jobs:
        if jobs < 1:
            raise SystemExit("verify: --jobs must be at least 1")
        return jobs
    env = environ.get("DIGI_VERIFY_BUDGET")
    if env:
        if not env.isdigit() or int(env) < 1:
            raise SystemExit("verify: DIGI_VERIFY_BUDGET must be an integer >= 1")
        return int(env)
    return default_budget()


def lower_priority(foreground: bool = False) -> int | None:
    """Set this process (and so every child) to nice NICE unless FOREGROUND; the new nice value,
    None when left alone. A process already niced more is not made more favoured."""
    if foreground:
        return None
    try:
        current = os.getpriority(os.PRIO_PROCESS, 0)
        if current >= NICE:
            return current
        os.setpriority(os.PRIO_PROCESS, 0, NICE)
        return NICE
    except OSError:
        return None


def want_cores(node: str, total: int) -> int:
    """The cores NODE asks for in a budget of TOTAL (an emulator pool asks for at least one process)."""
    cores = max(1, round(WANT.get(node, DEFAULT_WANT) * total))
    if node in EMULATOR_POOLS:
        cores = max(cores, min(EMULATOR_CORES, total))
    return min(cores, total)


@dataclass
class Budget:
    """The cores of the running steps; `admit` never lets them add up to more than `total`."""

    total: int
    max_steps: int = MAX_STEPS
    running: dict[str, int] = field(default_factory=dict)

    @property
    def free(self) -> int:
        return self.total - sum(self.running.values())

    def admit(self, node: str) -> int | None:
        """The share NODE gets (and holds until `release`), or None when it must wait: fewer than
        half of its ask is free (a step may start on less than the ask, not on a sliver of it), or
        MAX_STEPS steps run already. Alone, a step always starts."""
        if node in self.running:
            raise ValueError(f"{node} is running already")
        want = want_cores(node, self.total)
        share = min(want, self.free)
        if share < 1 or len(self.running) >= self.max_steps:
            return None
        if self.running and share < math.ceil(want / 2):
            return None
        self.running[node] = share
        return share

    def release(self, node: str) -> None:
        self.running.pop(node, None)


def wave_plan(
    nodes: list[str], deps: dict[str, tuple[str, ...]], total: int
) -> list[list[tuple[str, int]]]:
    """The shares in waves for `--explain`: each wave admits, in order, the nodes whose dependencies
    ended in an earlier wave, and all of it ends before the next wave starts. A real run starts a
    node as soon as cores are free, so it overlaps waves; the shares it hands out obey the same rule."""
    done: set[str] = set()
    pending = list(nodes)
    waves: list[list[tuple[str, int]]] = []
    while pending:
        budget = Budget(total)
        wave: list[tuple[str, int]] = []
        for node in list(pending):
            if all(d in done for d in deps[node]):
                share = budget.admit(node)
                if share:
                    wave.append((node, share))
                    pending.remove(node)
        if not wave:
            break
        done.update(n for n, _ in wave)
        waves.append(wave)
    return waves


@dataclass(frozen=True)
class Workers:
    """The inner pools of one step, from its share."""

    share: int
    cargo: int  # CARGO_BUILD_JOBS (rustc processes)
    libtest: int  # RUST_TEST_THREADS
    rayon: int  # RAYON_NUM_THREADS (ruff, oxlint, ...)
    emulators: int  # coupled-run processes at once: DIGI_JOBS, DIGI_TIER_JOBS
    py_files: int  # test files at once: DIGI_PY_JOBS
    per_file: int  # daemons inside one test file: DIGIKIT_JOBS, DIGI_CONFORM_JOBS, DIGI_PK_JOBS
    node: int  # node --test-concurrency


def workers_for(node: str, share: int) -> Workers:
    share = max(1, share)
    emulators = max(1, share // EMULATOR_CORES)
    if node == "python-tests":
        # `py_files` test files at once; each file holds at most `share // py_files` cores, which
        # its cargo and rayon use or its daemons do (two threads each)
        py_files = max(1, share // EMULATOR_CORES)
        own = max(1, share // py_files)
        return Workers(
            share=share,
            cargo=own,
            libtest=own,
            rayon=own,
            emulators=max(1, own // EMULATOR_CORES),
            py_files=py_files,
            per_file=max(1, own // EMULATOR_CORES),
            node=own,
        )
    return Workers(
        share=share,
        cargo=share,
        libtest=emulators,
        rayon=share,
        emulators=emulators,
        py_files=emulators,
        per_file=1,
        node=share,
    )


def step_env(node: str, share: int) -> dict[str, str]:
    """The environment variables that bound every pool of NODE to its SHARE."""
    w = workers_for(node, share)
    return {
        "CARGO_BUILD_JOBS": str(w.cargo),
        "RUST_TEST_THREADS": str(w.libtest),
        "RAYON_NUM_THREADS": str(w.rayon),
        "DIGI_JOBS": str(w.emulators),
        "DIGI_TIER_JOBS": str(w.emulators),
        "DIGI_PY_JOBS": str(w.py_files),
        "DIGIKIT_JOBS": str(w.per_file),
        "DIGI_CONFORM_JOBS": str(w.per_file),
        "DIGI_PK_JOBS": str(w.per_file),
    }


def bounded_env(
    node: str, share: int, base: dict[str, str] | None = None
) -> dict[str, str]:
    """BASE (default os.environ) with the pool bounds of NODE for SHARE cores. A variable the user set in
    the environment of the gate is not replaced: it overrides the budget (verify prints which)."""
    out = dict(os.environ if base is None else base)
    for key, value in step_env(node, share).items():
        if key not in os.environ:
            out[key] = value
    return out


def user_overrides() -> list[str]:
    """The pool variables the user set, which replace the share of the budget."""
    return [key for key in step_env("", 1) if key in os.environ]


# Pools the step does not read are left out of the printed line.
SHOWN: dict[str, tuple[str, ...]] = {
    "python-tests": ("py_files", "per_file", "cargo"),
    "rust-tests": ("cargo", "libtest"),
    "desktop": ("cargo", "node"),
    "web": ("cargo", "node"),
    **{n: ("cargo",) for n in BUILDS},
    **{n: ("emulators",) for n in EMULATOR_POOLS},
}
LABELS = {
    "py_files": "files",
    "per_file": "daemons/file",
    "cargo": "cargo jobs",
    "libtest": "test threads",
    "node": "node",
    "emulators": "emulator processes",
}


def describe(node: str, share: int) -> str:
    w = workers_for(node, share)
    parts = [f"{LABELS[k]} {getattr(w, k)}" for k in SHOWN.get(node, ("cargo",))]
    return ", ".join(parts)
