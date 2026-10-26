"""mypy check for the SHARC core and its concrete/symbolic entry points.

The SHARC core (tools/sharc_dev/sharc_core) also runs under PyPy 3.11
(tests/devtools/test_pypy.py), so it is checked against Python 3.11 here too (see
[tool.mypy] in pyproject.toml). Add a path here once it passes cleanly; the
list should only grow.
"""

import subprocess
import sys

from support.paths import REPO_ROOT

ROOT = str(REPO_ROOT)

TYPED = [
    "tools/__init__.py",
    "tools/patchkit/pk_dsplink.py",
    "tools/patchkit/modroots.py",
    "tools/patchkit/pk_conform.py",
    "tools/patchkit/pk_modcheck.py",
    "tools/patchkit/pk_digikit_memaudit.py",
    "tools/patchkit/pk_capacity.py",
    "tools/patchkit/pk_digikit_capacity.py",
    "tools/patchkit/pk_digikit_fx.py",
    "tools/patchkit/pk_digikit_fxsend.py",
    "tools/patchkit/pk_digikit_fxroute.py",
    "tools/patchkit/pk_digikit_fxroutes.py",
    "tools/patchkit/pk_digikit_settings.py",
    "tools/patchkit/pk_digikit_fxpage.py",
    "tools/patchkit/pk_digikit_fxpersist.py",
    "tools/patchkit/pk_digikit_fxshift.py",
    "tools/patchkit/pk_idx65_poke.py",
    "tools/patchkit/freqshift_ref.py",
    "tools/device/dkfit.py",
    "tools/device/dksettings.py",
    "tools/device/dkneeds.py",
    "tools/patchkit/pk_device.py",
    "tools/patchkit/pk_modaudit.py",
    "tools/patchkit/pk_package.py",
    "tools/coldfire/fw.py",
    "tools/coldfire/atlas.py",
    "tests/firmware/test_fw.py",
    "tools/emulator/audiocheck.py",
    "tools/emulator/fxcheck.py",
    "tools/coldfire/paramtable.py",
    "tools/emulator/scenario_dsl.py",
    "tools/emulator/dt2fx.py",
    "tests/dsp/test_dt2fx.py",
    "tests/dsp/test_trackfx.py",
    "tests/dsp/test_sendfx.py",
    "tools/emulator/dspdiff.py",
    "tests/dsp/test_dspdiff.py",
    "tests/dsp/test_audiocheck.py",
    "tools/sharc_dev/aot_fxprof.py",
    "tools/devtools/benchlock.py",
    "tools/devtools/verify_budget.py",
    "tools/devtools/verify_times.py",
    "tools/control/digi_client.py",
    "tools/sharc_dev/sharc_peer.py",
    "tools/coldfire/cf.py",
    "tools/coldfire/cf_names.py",
    "tools/coldfire/cfdb.py",
    "tools/coldfire/cfisa/gen.py",
    "tools/coldfire/cfisa/oracle.py",
    "tools/emulator/live_audio.py",
    "tools/sharc_dev/sharc.py",
    "tools/sharc_dev/sharc_core",
    "tools/sharc_dev/sharc_dac.py",
    "tools/sharc_dev/sharc_diff.py",
    "tools/sharc_dev/sharc_disasm.py",
    "tools/sharc_dev/sharc_dn2_aot.py",
    "tools/sharc_dev/sharc_framemap.py",
    "tools/sharc_dev/sharc_harness",
    "tools/sharc_dev/sharc_inputs.py",
    "tools/sharc_dev/sharc_proc.py",
    "tools/sharc_dev/sharc_rsgen.py",
    "tools/sharc_dev/sharc_run.py",
    "tools/sharc_dev/sharc_subset_lint.py",
    "tools/sharc_dev/sharc_survey.py",
    "tools/sharc_dev/sharc_symbols.py",
    "tools/sharc_dev/sharc_trace.py",
    "tools/sharc_dev/sharc_transpile",
    "tools/sharc_dev/sharc_transpile_infer.py",
    "tools/sharc_dev/sharc_transpile_run.py",
    "tools/sharc_dev/sharc_widthaudit.py",
    "tools/sharc_dev/sharcldr.py",
    "tools/emulator/snapread.py",
    "tools/devtools/pathcheck.py",
    "tests/devtools/test_pathcheck.py",
    "tests/devtools/test_pathcheck_repo.py",
    "tools/devtools/changed_tests.py",
    "tools/devtools/agent_guard.py",
]


def test_mypy():
    result = subprocess.run(
        [sys.executable, "-m", "mypy", *TYPED],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
