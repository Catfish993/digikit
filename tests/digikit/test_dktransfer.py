"""tools/device/dktransfer.py without an emulator: header groups, the merged list, the install plan and the install sequence (a mock uploader)."""

import hashlib
import struct

import pytest

from tools.device import dktransfer as T
from tools.device import dkupload as D


def header(
    name, api=(1, 1), machine=(0, 0), fx=(0, 0), service=(0, 0), size=500, dsp=0
):
    h = bytearray(D.HEADER_BYTES)
    h[:4] = b"DKMF"
    struct.pack_into(">II", h, 8, size, 0xA0)
    struct.pack_into(">I", h, 0x34, 0x000102)
    h[0x38:0x40] = bytes([*api, *machine, *fx, *service])
    h[0x40 : 0x40 + len(name)] = name.encode()
    struct.pack_into(">II", h, 0x64, 100, 20)
    struct.pack_into(">I", h, 0x9C, dsp)
    return bytes(h)


def test_header_groups():
    m = T.header_info(header("digikit/pluck", machine=(1, 0), service=(1, 0), dsp=64))
    assert (m["group"], m["name"], m["version"], m["arena"], m["dsp"]) == (
        "Machines",
        "digikit/pluck",
        "0.1.2",
        120,
        64,
    )
    assert T.header_info(header("a", fx=(1, 0)))["group"] == "Effects"
    assert T.header_info(header("a", service=(1, 0)))["group"] == "Tools"
    assert T.header_info(header("a"))["group"] == "Tools"
    assert T.header_info(b"nope" + bytes(200)) is None
    assert T.header_info(b"DKMF") is None


def row(
    mid, group="Machines", build="unchanged", size=500, arena=120, dsp=64, sha="aa"
):
    return {
        "id": mid, "name": mid, "label": mid, "group": group, "test": mid.startswith("test/"),
        "path": f"modules/{mid.split('/')[-1]}.dkm", "sha256": sha, "file_size": size, "arena": arena,
        "dsp": dsp, "build": build, "version": "0.1.0", "dkm": "x",
    }  # fmt: skip


CAP = {
    "MODULES": {"limit": 8, "used": 1, "largest": 0},
    "ARENA": {"limit": 1000, "used": 0, "largest": 300},
    "DSP_AREA": {"limit": 1000, "used": 0, "largest": 50},
}


def test_merge_state_groups_and_fit():
    rows = [
        row("test/sine", sha="s1"),
        row("digikit/pluck", sha="p1"),
        row("digikit/tool", group="Tools", dsp=0, sha="t1"),
    ]
    card = [
        ("modules/pluck.dkm", 500, "p1"),  # same file
        ("modules/tool.dkm", 500, "OLD"),  # same path, other bytes
        ("modules/other.dkm", 700, "zz"),  # not from the repo
        ("modules/junk.dkm", 9, "yy"),  # no readable header
    ]
    headers = {
        "modules/other.dkm": header("someone/effect", fx=(1, 0), size=700),
        "modules/junk.dkm": None,
    }
    v = T.merge(rows, card, ["digikit/pluck", "someone/effect"], CAP, headers)
    ids = [r["id"] for r in v["modules"]]
    assert (
        ids == ["digikit/pluck", "test/sine", "digikit/tool"][:0]
        or ids[0] == "digikit/pluck"
    )
    assert [r["group"] for r in v["modules"]] == ["Machines", "Machines", "Tools"]
    by = {r["id"]: r for r in v["modules"]}
    assert by["digikit/pluck"]["on_card"] == "yes" and by["digikit/pluck"]["loaded"]
    assert by["digikit/tool"]["on_card"] == "other version"
    assert by["test/sine"]["on_card"] == "no" and by["test/sine"]["test"]
    # a loaded module fits by definition; the others are checked against CAPACITY (dsp 64 > largest 50)
    assert by["digikit/pluck"]["fits"] is True
    assert (
        by["test/sine"]["fits"] is False
        and "DSP area" in by["test/sine"]["warnings"][0]
    )
    assert by["digikit/tool"]["fits"] is True
    extra = {r["path"]: r for r in v["card_only"]}
    assert (
        extra["modules/other.dkm"]["group"] == "Effects"
        and extra["modules/other.dkm"]["loaded"]
    )
    assert (
        extra["modules/junk.dkm"]["known"] is False
        and not extra["modules/junk.dkm"]["loaded"]
    )
    # no CAPACITY: no verdict
    assert T.merge(rows, [], [], None, {})["modules"][0]["fits"] is None


def test_plan_build_upload_select():
    rows = [
        row("digikit/pluck", sha="p1"),
        row("test/sine", build="needs build", sha="s1"),
    ]
    card = [("modules/pluck.dkm", 500, "p1")]
    p = T.plan_install(rows, ["digikit/pluck", "test/sine"], card, ["modules/old.dkm"])
    assert p == {
        "build": ["test/sine"],
        "upload": ["test/sine"],
        "select": ["modules/pluck.dkm", "modules/sine.dkm", "modules/old.dkm"],
    }
    with pytest.raises(T.TransferError, match="not a run-time module"):
        T.plan_install(rows, ["digikit/nothing"], card)
    with pytest.raises(T.TransferError, match="at most 8"):
        T.plan_install(
            rows, ["digikit/pluck"], card, [f"modules/k{i}.dkm" for i in range(8)]
        )
    # the same path in the wanted set and in --keep is selected once
    assert T.plan_install(rows, ["digikit/pluck"], card, ["modules/pluck.dkm"])[
        "select"
    ] == ["modules/pluck.dkm"]


class MockUp:
    """An uploader that records what an install asks of it; `modules()` lists the selection after `restart()`."""

    def __init__(self, card, loaded_after, runtime=0x000204, corrupt=False):
        self.card, self.loaded_after, self.runtime = list(card), loaded_after, runtime
        self.calls, self.restarted, self.data, self.corrupt = [], False, {}, corrupt

    def info(self):
        return {
            "runtime": self.runtime,
            "card_read_only": False,
            "port": "dt2",
            "free_bytes": 1 << 20,
            "caps": 0x1FF,
        }

    def ls(self):
        return list(self.card)

    def put(self, path, data):
        self.calls.append(("put", path))
        self.data[path] = data
        self.card = [c for c in self.card if c[0] != path] + [
            (path, len(data), hashlib.sha256(data).hexdigest())
        ]

    def verify(self, path):
        self.calls.append(("verify", path))
        got = self.data.get(path)
        if got is None:  # on the card from before: its listed hash
            return next(h for p, _, h in self.card if p == path)
        return hashlib.sha256(got + (b"!" if self.corrupt else b"")).hexdigest()

    def select(self, paths):
        self.calls.append(("select", list(paths)))

    def restart(self):
        self.calls.append(("restart",))
        self.restarted = True

    def modules(self):
        names = self.loaded_after if self.restarted else []
        return {"modules": [{"name": n} for n in names]}


def _two_rows(tmp_path):
    pl = tmp_path / "pluck.dkm"
    pl.write_bytes(b"P" * 600)
    sn = tmp_path / "sine.dkm"
    sn.write_bytes(b"S" * 700)
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    rows = [
        {**row("digikit/pluck", sha=sha(b"P" * 600), size=600), "dkm": str(pl)},
        {**row("test/sine", build="needs build", sha="stale", size=1), "dkm": str(sn)},
    ]
    fresh = lambda device, ids, pack: [  # noqa: E731
        {**row(ids[0], sha=sha(b"S" * 700), size=700), "dkm": str(sn)}
    ]
    card = [("modules/pluck.dkm", 600, sha(b"P" * 600))]
    return rows, fresh, card


def test_install_without_restart_builds_uploads_verifies_selects(tmp_path, monkeypatch):
    rows, fresh, card = _two_rows(tmp_path)
    monkeypatch.setattr(T, "repo_rows", fresh)
    built, events = [], []
    up = MockUp(card, ["digikit/pluck", "test/sine"])
    out = T.install(
        up,
        ["digikit/pluck", "test/sine"],
        ["modules/loaded.dkm"],
        events.append,
        rows=rows,
        build=built.append,
    )
    assert built == ["test/sine"]  # only the changed one is built
    assert (
        up.calls
        == [  # per module in order: upload (pluck is on the card: skipped), verify; then one SELECT; no RESTART
            ("verify", "modules/pluck.dkm"),
            ("put", "modules/sine.dkm"),
            ("verify", "modules/sine.dkm"),
            ("select", ["modules/pluck.dkm", "modules/sine.dkm", "modules/loaded.dkm"]),
        ]
    )
    assert out["uploaded"] == ["test/sine"] and out["restarted"] is False
    assert "loaded" not in out
    kinds = [e["event"] for e in events]
    assert kinds == [
        "start", "upload", "skip", "verify", "build", "upload", "verify", "select", "done",
    ]  # fmt: skip
    assert [e["ok"] for e in events if e["event"] == "verify"] == [True, True]
    assert events[-1] == {
        "event": "done",
        "restarted": False,
        "selected": out["selected"],
    }


def test_install_with_restart_waits_for_modules(tmp_path, monkeypatch):
    from tools.device import dkrun as R

    rows, fresh, card = _two_rows(tmp_path)
    monkeypatch.setattr(T, "repo_rows", fresh)
    monkeypatch.setattr(R, "restart_and_wait", lambda up, *a: (up.restart(), up)[1])
    events = []
    up = MockUp(card, ["digikit/pluck", "test/sine"])
    out = T.install(
        up, ["digikit/pluck", "test/sine"], None, events.append, rows=rows,
        build=lambda m: None, restart=True,
    )  # fmt: skip
    assert up.calls[-2:] == [
        ("select", ["modules/pluck.dkm", "modules/sine.dkm"]),
        ("restart",),
    ]
    assert out["restarted"] and out["loaded"] == ["digikit/pluck", "test/sine"]
    assert [e["event"] for e in events][-3:] == ["restart", "wait", "done"]


def test_a_verify_mismatch_is_an_error_and_nothing_is_selected(tmp_path, monkeypatch):
    rows, fresh, card = _two_rows(tmp_path)
    monkeypatch.setattr(T, "repo_rows", fresh)
    events = []
    up = MockUp(card, [], corrupt=True)
    with pytest.raises(T.TransferError, match="did not keep the file"):
        T.install(
            up, ["test/sine"], None, events.append, rows=rows, build=lambda m: None
        )
    assert not [c for c in up.calls if c[0] == "select"]
    assert events[-1] == {
        "event": "verify", "id": "test/sine", "path": "modules/sine.dkm", "ok": False,
    }  # fmt: skip


def test_restart_reports_loaded_and_missing_with_the_reason(monkeypatch):
    from tools.device import dkrun as R

    monkeypatch.setattr(R, "restart_and_wait", lambda up, *a: (up.restart(), up)[1])
    up = MockUp([], ["digikit/pluck"])
    t = {"v": 0.0}
    events = []
    out = T.restart_device(
        up, ["digikit/pluck", "test/sine"], events.append,
        sleep=lambda s: t.__setitem__("v", t["v"] + s), now=lambda: t["v"],
        paths=["modules/pluck.dkm", "modules/sine.dkm"],
    )  # fmt: skip
    assert out["loaded"] == ["digikit/pluck"] and out["missing"] == ["test/sine"]
    # the store is empty after the restart: the reason is said
    assert out["not_on_card"] == ["modules/pluck.dkm", "modules/sine.dkm"]
    assert events[-1]["event"] == "done" and events[-1]["missing"] == ["test/sine"]


def test_install_with_restart_says_when_the_restart_lost_the_files(
    tmp_path, monkeypatch
):
    from tools.device import dkrun as R

    rows, fresh, card = _two_rows(tmp_path)
    monkeypatch.setattr(T, "repo_rows", fresh)
    up = MockUp(card, [])

    def lose(up, *a):
        up.restart()
        up.card = []  # a soft reset without the overlay
        return up

    monkeypatch.setattr(R, "restart_and_wait", lose)
    t = {"v": 0.0}
    with pytest.raises(T.TransferError, match="restart lost the card's writes"):
        T.install(
            up, ["digikit/pluck"], None, rows=rows, build=lambda m: None, restart=True,
            sleep=lambda s: t.__setitem__("v", t["v"] + s), now=lambda: t["v"],
        )  # fmt: skip


def test_the_packed_file_is_reused_until_an_input_changes(tmp_path, monkeypatch):
    import os

    from tools.device import dkrun as R

    mod = tmp_path / "mod"
    mod.mkdir()
    (mod / "module.toml").write_text("x")
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(T.modroots, "module_dir", lambda m: mod)
    monkeypatch.setattr(
        T, "inputs_mtime", lambda d: max(f.stat().st_mtime for f in d.iterdir())
    )
    packs = []

    def pack(module, device, out):
        packs.append(module)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"dkm")
        return out

    monkeypatch.setattr(R, "pack_module", pack)
    a = T.pack_for_transfer("test/m", "dt2")
    assert T.pack_for_transfer("test/m", "dt2") == a and packs == ["test/m"]  # reused
    later = a.stat().st_mtime + 10
    os.utime(mod / "module.toml", (later, later))  # a source changed
    T.pack_for_transfer("test/m", "dt2")
    assert packs == ["test/m", "test/m"]

    # a module that does not pack is remembered until its inputs change
    def refuse(module, device, out):
        packs.append("refused")
        raise R.RunError("nothing for device dt2")

    monkeypatch.setattr(R, "pack_module", refuse)
    (tmp_path / "out/patch/transfer/dt2/m.dkm").unlink()
    os.utime(mod / "module.toml", (1_000_000, 1_000_000))
    for _ in range(2):
        with pytest.raises(R.RunError, match="nothing for device"):
            T.pack_for_transfer("test/m", "dt2")
    assert packs.count("refused") == 1


def test_a_paused_emulator_is_refused():
    class Emu:
        def status(self):
            return {"mode": "paused"}

    import argparse

    args = argparse.Namespace(emu=True, run_dir=None, socket="s", step=False, port=None)
    orig = T._emu_at
    T._emu_at = lambda s: Emu()
    try:
        with pytest.raises(T.TransferError, match="paused"):
            T.connect_target(args)
    finally:
        T._emu_at = orig


def test_pk_build_hash_matches_the_committed_objects():
    # the skip-if-unchanged hash is the one the builders write: every committed object of the repo carries it
    from tools.patchkit import modroots, pk_build

    stale = [
        str(d)
        for d in modroots.manifests().values()
        if pk_build.module_status(d) != "unchanged"
    ]
    assert stale == []
