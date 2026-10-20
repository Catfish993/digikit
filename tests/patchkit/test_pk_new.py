"""`module:new -- AUTHOR/NAME --machine` (tools/patchkit/pk_new.py): a run-time machine from the template of test/sine."""

import os
import subprocess
import sys
import tomllib

import pytest

from tools.patchkit import pk_new, pk_toolchains
from tools.patchkit.pk_new import ROOT


def test_the_template_renames_the_ids_and_symbols(tmp_path):
    files = pk_new.machine_files("em", "my-synth")
    man = tomllib.loads(files["module.toml"])
    assert man["module"]["id"] == "em/my-synth" and man["module"]["author"] == "em"
    assert man["module"]["type"] == "machine" and man["module"]["dynamic"] is True
    assert man["module"]["category"] == "machines"
    assert (
        man["machine"][0]["id"] == "my_synth"
        and man["param"][0]["id"] == "my_synth_level"
    )
    assert man["voice"][0]["render"] == "_my_synth_render"
    assert man["object"][0]["sources"] == ["src/dsp/my-synth.s"]
    assert "src/dsp/my-synth.s" in files and "_sn_" not in files["src/dsp/my-synth.s"]
    assert not any(k.startswith("build") for k in files)


def test_core_is_refused_as_author(tmp_path):
    with pytest.raises(SystemExit, match="core"):
        pk_new.create("core/x", False, False, machine=True, dir_=tmp_path)
    assert not (tmp_path / "x").exists()


@pytest.mark.skipif(pk_toolchains.selache_dir() is None, reason="selache not built")
def test_a_new_machine_in_an_out_of_tree_folder_passes_the_check_on_both_devices(
    tmp_path,
):
    env = {**os.environ, "PATCHKIT_MODULES": str(tmp_path)}
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.patchkit.pk_new",
            "em/demo",
            "--machine",
            "--dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "demo" / "build" / "demo-dsp.dko.json").is_file()
    c = subprocess.run(
        [sys.executable, "-m", "tools.patchkit.pk_modcheck", "em/demo"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert c.returncode == 0, c.stdout + c.stderr
    assert " passed" in c.stdout and " failed" not in c.stdout
    assert "dt2: size" in c.stdout and "dn2: size" in c.stdout
