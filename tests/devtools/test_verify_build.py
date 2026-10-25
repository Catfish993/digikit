"""Daemon build environment publication, without invoking cargo or reading ready images."""

from pathlib import Path

import pytest

from tools.devtools import verify_build


def test_plain_daemons_publish_each_built_device_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path, bool]] = []
    monkeypatch.setattr(
        verify_build.pk_emud_pgo.native_pgo, "current_profile", lambda _device: None
    )
    monkeypatch.setattr(
        verify_build.pk_harness,
        "firmware_inputs",
        lambda cell: (Path(f"/{cell.device}.syx"), Path("/ready")),
    )

    def build(device: str, syx: Path, *, pgo: bool) -> Path:
        calls.append((device, syx, pgo))
        return Path(f"/built/{device}/digi-emud")

    monkeypatch.setattr(verify_build.pk_harness, "daemon_binary", build)
    assert verify_build.build_daemons(["dt2", "dn2"]) == {
        "DIGI_EMUD_DT2": "/built/dt2/digi-emud",
        "DIGI_EMUD_DN2": "/built/dn2/digi-emud",
    }
    assert calls == [("dt2", Path("/dt2.syx"), False), ("dn2", Path("/dn2.syx"), False)]
