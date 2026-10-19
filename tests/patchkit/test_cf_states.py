"""The saved boot states of the ColdFire-only digikit rigs (tools/patchkit/pk_dkrt_boot.py): their key and their pruning."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.patchkit import pk_dkrt_boot as B


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    exe, exe2 = tmp_path / "emud", tmp_path / "emud2"
    exe.write_bytes(b"binary one")
    exe2.write_bytes(b"binary two")
    img, img2 = tmp_path / "a.syx", tmp_path / "b.syx"
    img.write_bytes(b"image one")
    img2.write_bytes(b"image two")
    card, card2 = tmp_path / "c1.img", tmp_path / "c2.img"
    card.write_bytes(b"x" * 512)
    card2.write_bytes(b"y" * 512)
    return {
        "exe": exe,
        "exe2": exe2,
        "img": img,
        "img2": img2,
        "card": card,
        "card2": card2,
    }


def test_the_key_names_binary_image_card_and_boot_environment(
    files: dict[str, Path],
) -> None:
    f = files
    base = B.state_key(f["exe"], f["img"], f["card"], {"DIGI_COLD_RELAX_FS": "1"})
    assert base == B.state_key(
        f["exe"], f["img"], f["card"], {"DIGI_COLD_RELAX_FS": "1"}
    )
    others = [
        B.state_key(
            f["exe2"], f["img"], f["card"], {"DIGI_COLD_RELAX_FS": "1"}
        ),  # another emulator
        B.state_key(
            f["exe"], f["img2"], f["card"], {"DIGI_COLD_RELAX_FS": "1"}
        ),  # another image
        B.state_key(
            f["exe"], f["img"], f["card2"], {"DIGI_COLD_RELAX_FS": "1"}
        ),  # another card
        B.state_key(f["exe"], f["img"], f["card"], {}),  # another boot environment
    ]
    assert len({base, *others}) == 5


def test_the_write_watch_does_not_change_the_key(files: dict[str, Path]) -> None:
    f = files
    plain = B.state_key(f["exe"], f["img"], f["card"], {})
    watched = {
        "DIGI_WRITE_WATCH": "0x1-0x2",
        "DIGI_WRITE_WATCH_OUT": "/x",
        "DIGI_FLUSH_CHECK": "1",
    }
    assert B.state_key(f["exe"], f["img"], f["card"], watched) == plain


def test_a_rewritten_card_is_another_key(files: dict[str, Path]) -> None:
    f = files
    before = B.state_key(f["exe"], f["img"], f["card"], {})
    f["card"].write_bytes(b"z" * 1024)
    os.utime(f["card"], ns=(1, 1))
    assert B.state_key(f["exe"], f["img"], f["card"], {}) != before


def test_prune_keeps_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(B, "STATE_DIR", tmp_path)
    for i in range(5):
        p = tmp_path / f"s{i}.snap"
        p.write_bytes(b"s")
        os.utime(p, (1000 + i, 1000 + i))
    B.prune_states(keep=2)
    assert sorted(p.name for p in tmp_path.glob("*.snap")) == ["s3.snap", "s4.snap"]
