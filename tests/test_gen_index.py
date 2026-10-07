"""The generated core is picked by the SHA-256 of the packed DSP image."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import digi_env  # noqa: E402


def test_registered_core_is_found_by_image_content(monkeypatch, tmp_path):
    monkeypatch.setattr(digi_env, "GEN_INDEX", tmp_path / "gen-index.json")
    monkeypatch.delenv("SHARC_GEN_DIR", raising=False)
    image = tmp_path / "image.bin"
    image.write_bytes(b"dt2 image")
    other = tmp_path / "other.bin"
    other.write_bytes(b"dn2 image")
    gen = tmp_path / "gen"
    gen.mkdir()
    assert digi_env.gen_for_image(image) is None
    digi_env.register_gen(image, gen)
    assert digi_env.gen_for_image(image) == gen.resolve()
    # A copy of the image elsewhere resolves the same; another image does not.
    copy = tmp_path / "copy.bin"
    copy.write_bytes(image.read_bytes())
    assert digi_env.generated_dir(image=copy) == gen.resolve()
    assert digi_env.generated_dir(image=other) == digi_env.DEFAULT_GENERATED


def test_environment_overrides_the_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(digi_env, "GEN_INDEX", tmp_path / "gen-index.json")
    image = tmp_path / "image.bin"
    image.write_bytes(b"x")
    gen = tmp_path / "gen"
    gen.mkdir()
    digi_env.register_gen(image, gen)
    monkeypatch.setenv("SHARC_GEN_DIR", str(tmp_path / "forced"))
    assert digi_env.generated_dir(image=image) == tmp_path / "forced"
