"""A patched image resolves to its stock base through its DKPR record (tools/emulator/digi_env.py)."""

import hashlib

import pytest
from support.paths import REPO_ROOT  # noqa: F401

from tools.emulator import digi_env
from tools.firmware import config

DT2_117 = config.stock("Digitakt_II_OS1.17.syx")


def test_unknown_file_has_no_patch_base_and_no_device(tmp_path):
    junk = tmp_path / "junk.syx"
    junk.write_bytes(b"\xf0" + b"x" * 200_000)  # looks like a .syx, has no record
    assert digi_env.patch_base(junk) is None
    assert digi_env.device_for_syx(junk) is None
    assert digi_env.base_firmware(junk) == junk


def test_device_and_base_come_from_the_record(monkeypatch, tmp_path):
    patched = tmp_path / "patched.syx"
    patched.write_bytes(b"patched bytes")
    monkeypatch.setattr(digi_env, "patch_base", lambda p: ("dt2", "cd" * 32))
    monkeypatch.setattr(digi_env, "stock_for_sha", lambda d: DT2_117)
    assert digi_env.device_for_syx(patched) == "dt2"
    assert digi_env.base_firmware(patched) == DT2_117


def test_stock_for_sha_matches_the_device_files():
    if not DT2_117.is_file():
        pytest.skip("stock firmware not present")
    digest = hashlib.sha256(DT2_117.read_bytes()).hexdigest()
    assert digi_env.stock_for_sha(digest) == DT2_117
    assert digi_env.stock_for_sha("00" * 32) is None


def test_real_patched_image(tmp_path):
    """The patchkit binary reads the record of an image patched here."""
    if not DT2_117.is_file():
        pytest.skip("stock firmware not present")
    from tools.patchkit import patchkit

    out = tmp_path / "p.syx"
    code = patchkit.main(
        [
            "patch",
            "--firmware",
            str(DT2_117),
            "--modules",
            "core/runtime",
            "-o",
            str(out),
        ]
    )
    assert code == 0
    digest = hashlib.sha256(DT2_117.read_bytes()).hexdigest()
    assert digi_env.patch_base(out) == ("dt2", digest)
    assert digi_env.device_for_syx(out) == "dt2"
    assert digi_env.base_firmware(out) == DT2_117
