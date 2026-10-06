"""coupled: pure helpers of tools/coupled.py (no cargo, no emulator)."""

import importlib.util
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "coupled.py"
_spec = importlib.util.spec_from_file_location("coupled", TOOL)
assert _spec and _spec.loader
coupled = importlib.util.module_from_spec(_spec)
sys.modules["coupled"] = coupled
_spec.loader.exec_module(coupled)


def test_bisect_configs_labels_and_overrides():
    configs = coupled.bisect_configs(
        {"SHARC_FAST_REGIONS": "0x1,0x2", "SHARC_BLOCKTRANS": "0x9"}
    )
    assert [label for label, _ in configs] == [
        "no-fast",
        "no-aot",
        "fast-minus-0x1",
        "fast-minus-0x2",
        "roots-minus-0x9",
    ]
    env = dict(configs)
    assert env["fast-minus-0x1"]["SHARC_FAST_REGIONS"] == "0x2"
    assert env["fast-minus-0x1"]["SHARC_BLOCKTRANS"] == "0x9"
    assert env["no-aot"]["SHARC_AOT"] == "0"


def test_first_jump():
    assert coupled.first_jump([0.1, 0.1, 0.4, 0.4]) == 2
    assert coupled.first_jump([0.1, 0.11]) is None
    assert coupled.first_jump([0.0, 0.0, 0.2]) == 2


def test_first_divergence():
    assert coupled.first_divergence([1.0, 2.0, 3.0], [1.0, 2.0, 3.5]) == 2
    assert coupled.first_divergence([1.0, 2.0], [1.0, 2.0]) is None


def test_run_args_passes_inputs_and_extra_in_order():
    inputs = {
        "firmware": Path("fw.syx"),
        "DSP image": Path("img.bin"),
        "DSP state": Path("state.dsp"),
        "CF snapshot": Path("cf.snap"),
    }
    argv = coupled.run_args("bin/coupled-run", inputs, ["--scenario", "idle", "--wav"])
    assert argv[0] == "bin/coupled-run"
    assert argv[argv.index("--dsp-image") + 1] == "img.bin"
    assert argv[-3:] == ["--scenario", "idle", "--wav"]


def test_scenario_results_reads_the_report_keys():
    report = {
        "scenarios": [
            {
                "name": "idle",
                "pcm": {"sha256": "ab" * 32, "window_rms_100ms": [0.0, 0.5]},
                "gates": {"ok": True},
            }
        ]
    }
    [result] = coupled.scenario_results(report)
    assert result["name"] == "idle"
    assert result["sha256"] == "ab" * 32
    assert result["ok"] is True
    assert result["windows"] == [0.0, 0.5]
