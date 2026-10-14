"""digi_env: the generated-core fingerprint guard and coupled's per-device gen."""

import importlib.util
import sys
from pathlib import Path

import pytest
from support.paths import REPO_ROOT

TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))
try:
    import digi_env
finally:
    sys.path.pop(0)


def _load_coupled():
    spec = importlib.util.spec_from_file_location("coupled", TOOLS / "coupled.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["coupled"] = module
    spec.loader.exec_module(module)
    return module


def _tree(root: Path) -> None:
    core = root / "tools" / "sharc_core"
    core.mkdir(parents=True)
    (core / "a.py").write_text("x = 1\n")
    (root / "tools" / "sharc_transpile.py").write_text("t = 1\n")
    (root / "tools" / "sharc_rsgen.py").write_text("r = 1\n")


def test_fingerprint_is_stable_and_tracks_content(tmp_path):
    _tree(tmp_path)
    first = digi_env.core_fingerprint(tmp_path)
    assert digi_env.core_fingerprint(tmp_path) == first
    (tmp_path / "tools" / "sharc_core" / "a.py").write_text("x = 2\n")
    assert digi_env.core_fingerprint(tmp_path) != first


def test_check_generated(tmp_path, monkeypatch, capsys):
    gen = tmp_path / "gen"
    gen.mkdir()
    monkeypatch.setattr(digi_env, "core_fingerprint", lambda root=None: "abc")
    with pytest.raises(SystemExit, match="missing"):
        digi_env.check_generated(gen, {})
    (gen / digi_env.CORE_FINGERPRINT_FILE).write_text("old\n")
    with pytest.raises(SystemExit, match="old") as err:
        digi_env.check_generated(gen, {})
    assert "DIGI_ALLOW_STALE_GEN=1" in str(err.value)
    digi_env.check_generated(gen, {"DIGI_ALLOW_STALE_GEN": "1"})
    assert "stale" in capsys.readouterr().err
    (gen / digi_env.CORE_FINGERPRINT_FILE).write_text("abc\n")
    digi_env.check_generated(gen, {})


def test_coupled_picks_the_gen_of_the_device_image(tmp_path, monkeypatch):
    coupled = _load_coupled()
    dt2_gen = tmp_path / "dt2-gen"
    dt2_gen.mkdir()
    seen = []

    def gen_for_image(image):
        seen.append(Path(image))
        return dt2_gen if Path(image).name == "dt2.bin" else None

    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    monkeypatch.setattr(digi_env, "gen_for_image", gen_for_image)
    assert coupled.generated("dt2", tmp_path / "dt2.bin") == dt2_gen
    assert coupled.generated("dn2", tmp_path / "dn2.bin") == digi_env.DEFAULT_GENERATED
    assert coupled.generated("dt2", Path("/x/dt2.bin")) == dt2_gen
    coupled.generated("dt2")
    assert seen[-1] == digi_env.ready_inputs(device="dt2")["DSP image"]
