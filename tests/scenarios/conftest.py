"""Fixtures for the scenario oracle tests (tests/scenarios/README.md, "Oracle tests").

A test asks ``render`` for scenarios by name. Every scenario a selected test
declares (``@pytest.mark.scn(name, ...)``) is rendered over parallel ``coupled-run``
processes (``--jobs``) per scenario file the first time any of them is needed, together with
its dry reference and companions; each scenario restores the ready snapshot,
so nothing is shared between them. All tests here are marked ``slow`` and
``scenario``; tests marked ``sweep`` need ``--sweep`` (or ``-m sweep``).
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
HERE = pathlib.Path(__file__).resolve().parent

from tools.emulator import audiocheck as ac  # noqa: E402
from tools.emulator import coupled, digi_env, fxcheck  # noqa: E402

# DT2 scenario file by name prefix. Standard Sound/Fx peeks come from fxcheck.
FILES = {"tf": "dt2-trackfx.json", "sf": "dt2-sendfx.json"}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--diag", default=None, metavar="LIST", help="coupled.py --diag")
    parser.addoption(
        "--keep-runs",
        action="store_true",
        help="keep the WAVs of the runs (default: delete them at the end; "
        "reports stay under out/runs/tests/<stamp>/)",
    )
    parser.addoption(
        "--device",
        default="dt2",
        choices=["dt2"],
        help="DT2 runs test_dt2_fx.py on the one-sample card; DN2 uses native Vitest",
    )
    parser.addoption(
        "--no-build",
        action="store_true",
        help="use the existing coupled-run binary (no cargo build)",
    )
    parser.addoption(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="coupled-run processes rendering a batch in parallel "
        "(default: DIGI_JOBS or min(cpus - 2, 6); 1 = one process)",
    )
    parser.addoption(
        "--sweep", action="store_true", help="also run the full sweeps (~20 min)"
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "scenario: renders scenarios headless")
    config.addinivalue_line("markers", "sweep: full sweep (--sweep or -m sweep)")
    config.addinivalue_line("markers", "scn(*names): scenarios the test renders")


def _mine(item: pytest.Item) -> bool:
    return HERE in pathlib.Path(str(item.path)).parents


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    sweep = config.getoption("--sweep") or "sweep" in (config.option.markexpr or "")
    keep: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        if _mine(item):
            item.add_marker(pytest.mark.slow)
            item.add_marker(pytest.mark.scenario)
            if "sweep" in item.keywords and not sweep:
                dropped.append(item)
                continue
        keep.append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep


# --- rendering ---------------------------------------------------------------


@dataclass
class Rendered:
    """One rendered scenario."""

    name: str
    rate: int
    x: ac.F  # (n, 2) float64
    entry: dict[str, Any]  # the report's scenario entry
    vals: dict[str, int]  # read-back arrays by 'snd[i] Name' / 'fx[i] Name'
    row: dict[str, Any]  # fxcheck.check row (analysis, bits, tf/sf measures, ...)
    run_dir: pathlib.Path
    hold: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def ui(self, prefix: str) -> int | None:
        """UI value (high byte) of the read-back entry that starts with
        ``prefix`` (for example 'fx[35]'), or None."""
        for k, v in self.vals.items():
            if k.startswith(prefix + " "):
                return (v & 0xFFFF) >> 8
        return None

    def raw(self, prefix: str) -> int | None:
        """The stored s16 (as unsigned) of a read-back entry, or None."""
        for k, v in self.vals.items():
            if k.startswith(prefix + " "):
                return v & 0xFFFF
        return None

    def changed(self, ref: Rendered) -> dict[str, int]:
        """Stored parameters whose value differs from ``ref``'s."""
        return {
            k: (v & 0xFFFF) >> 8 for k, v in self.vals.items() if ref.vals.get(k) != v
        }


class Renderer:
    """Renders scenarios in batches and caches the results for the session."""

    def __init__(self, device: str, binary: pathlib.Path, config: pytest.Config):
        self.device = device
        self.binary = binary
        self.diag: str | None = config.getoption("--diag")
        jobs = config.getoption("--jobs")
        self.jobs: int = coupled.default_jobs() if jobs is None else jobs
        self.keep = bool(config.getoption("--keep-runs"))
        self.base = digi_env.run_dir("tests")
        self.names = fxcheck.name_maps(fxcheck.DEFAULT_IMAGE)
        self.peeks = (fxcheck.SND_PEEK, fxcheck.FX_PEEK)
        self.cache: dict[str, Rendered] = {}
        self.wanted: dict[str, set[str]] = {}
        self.batches = 0
        self.events = fxcheck.scenario_events()

    def file_of(self, name: str) -> str:
        return FILES[name.split("-")[0]]

    def need(self, names: list[str]) -> None:
        """Declare scenarios that will be asked for (rendered together)."""
        for n in names:
            self.wanted.setdefault(self.file_of(n), set()).add(n)

    def closure(self, names: set[str]) -> list[str]:
        out: dict[str, None] = {}
        for n in sorted(names):
            hold = fxcheck.hold_seconds(self.events.get(n, []))
            for m in (fxcheck.reference_for(n, hold), *fxcheck.companions(n), n):
                out[m] = None
        return [n for n in out if n not in self.cache]

    def _batch(self, fname: str, names: list[str]) -> None:
        self.batches += 1
        run = self.base / f"{pathlib.Path(fname).stem}-{self.batches}"
        coupled.run_batch(
            self.device,
            ROOT / "tests" / "scenarios" / fname,
            names,
            run,
            peeks=self.peeks,
            diag=self.diag,
            binary=self.binary,
            jobs=self.jobs,
        )
        rows = fxcheck.check([run], None, False, fxcheck.DEFAULT_IMAGE)
        loaded = fxcheck.load_run(run)
        for name in names:
            entry, wav = loaded[name]
            rate, x = ac.load(wav)
            self.cache[name] = Rendered(
                name=name,
                rate=rate,
                x=x,
                entry=entry,
                vals=fxcheck.decode_arrays(entry.get("peek") or {}, self.names),
                row=rows[name],
                run_dir=run,
                hold=fxcheck.hold_seconds(self.events.get(name, [])),
            )

    def get(self, name: str) -> Rendered:
        if name not in self.cache:
            self.need([name])
            fname = self.file_of(name)
            self._batch(fname, self.closure(self.wanted[fname]))
        return self.cache[name]

    def prune(self) -> None:
        if not self.keep:
            for wav in self.base.rglob("*.wav"):
                wav.unlink()


@pytest.fixture(scope="session")
def device(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("--device"))


@pytest.fixture(scope="session")
def emulator(request: pytest.FixtureRequest, device: str) -> Renderer:
    """Binary built, ready inputs present, generated core current. Skips (with
    the reason) when an input is missing or the core is stale."""
    fxcheck.configure(device)
    try:
        if device == "dt2":
            coupled.enable_dt2_card()
        coupled.inputs(device)
        gen = coupled.generated(device)
        digi_env.check_generated(gen)
    except SystemExit as exc:
        pytest.skip(f"emulator inputs unavailable: {exc}")
    if not fxcheck.DEFAULT_IMAGE.exists():
        pytest.skip(
            f"MAIN OS image for read-back names missing: {fxcheck.DEFAULT_IMAGE}"
        )
    try:
        binary = coupled.ensure_binary(
            bool(request.config.getoption("--no-build")), device
        )
    except SystemExit as exc:
        pytest.fail(f"coupled-run build failed: {exc}")
    rend = Renderer(device, binary, request.config)
    # Everything the selected tests declared is rendered in the first batch.
    session = request.session
    for item in session.items:
        for mark in item.iter_markers("scn"):
            rend.need(list(mark.args))
    print(f"\nscenario runs: {rend.base}", file=sys.stderr)
    request.addfinalizer(rend.prune)
    return rend


@pytest.fixture(scope="session")
def render(emulator: Renderer) -> Any:
    """``render(name)`` -> Rendered; ``render(a, b)`` -> list of Rendered."""

    def get(*names: str) -> Any:
        got = [emulator.get(n) for n in names]
        return got[0] if len(got) == 1 else got

    return get
