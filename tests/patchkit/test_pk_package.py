"""`.dkmod` export and import (tools/patchkit/pk_package.py)."""

import zipfile
from pathlib import Path

import pytest

from tools.patchkit import pk_package as pkg

MODULE = """[module]
id = "em/demo"
version = "0.1.0"
name = "Demo"
category = "machines"
type = "machine"
author = "{author}"
status = "experimental"
summary = "A demo."
dynamic = true
ports = ["dt2-1.17"]
requires = []
conflicts = []

"""


def make(root: Path, author: str = "em", name: str = "demo") -> Path:
    d = root / name
    (d / "src" / "dsp").mkdir(parents=True)
    (d / "build" / "gen").mkdir(parents=True)
    (d / "module.toml").write_text(MODULE.format(author=author))
    (d / "src" / "dsp" / "demo.s").write_text("// demo\n")
    (d / "README.md").write_text("# Demo\n")
    (d / "build" / "demo.dko.json").write_text("{}")
    (d / "build" / "gen" / "x.inc").write_text("x")
    (d / ".DS_Store").write_bytes(b"junk")
    (d / "src" / "__pycache__").mkdir()
    (d / "src" / "__pycache__" / "x.pyc").write_bytes(b"junk")
    return d


@pytest.fixture
def roots(tmp_path, monkeypatch):
    src = tmp_path / "src_root"
    src.mkdir()
    monkeypatch.setenv("PATCHKIT_MODULES", str(src))
    return src


def tree(d: Path) -> dict[str, bytes]:
    return {
        p.relative_to(d).as_posix(): p.read_bytes()
        for p in sorted(d.rglob("*"))
        if p.is_file()
    }


def test_round_trip_equals_the_source_without_build(roots, tmp_path, monkeypatch):
    d = make(roots)
    out = pkg.export("em/demo", tmp_path / "demo.dkmod")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "package.toml" and names[1:] == sorted(names[1:])
        assert not any(
            n.startswith("build") or ".DS_Store" in n or "pycache" in n for n in names
        )
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist())
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setenv("PATCHKIT_MODULES", str(dest))
    got = pkg.import_package(out)
    assert got == dest / "demo"
    want = {
        k: v
        for k, v in tree(d).items()
        if not k.startswith("build/") and k != ".DS_Store" and "pycache" not in k
    }
    assert tree(got) == want


def test_two_exports_are_byte_identical(roots, tmp_path):
    make(roots)
    a = pkg.export("em/demo", tmp_path / "a.dkmod")
    b = pkg.export("em/demo", tmp_path / "b.dkmod")
    assert a.read_bytes() == b.read_bytes()


def rewrite(src: Path, dst: Path, change) -> None:
    with zipfile.ZipFile(src) as z, zipfile.ZipFile(dst, "w") as o:
        for i in z.infolist():
            o.writestr(i.filename, change(i.filename, z.read(i)))


def test_a_tampered_extra_or_missing_file_is_refused(roots, tmp_path, monkeypatch):
    make(roots)
    out = pkg.export("em/demo", tmp_path / "demo.dkmod")
    monkeypatch.setenv("PATCHKIT_MODULES", str(tmp_path / "dest"))
    bad = tmp_path / "bad.dkmod"
    rewrite(out, bad, lambda n, d: d + b"x" if n == "src/dsp/demo.s" else d)
    with pytest.raises(SystemExit, match="sha256"):
        pkg.import_package(bad)
    with zipfile.ZipFile(out) as z, zipfile.ZipFile(bad, "w") as o:
        for i in z.infolist():
            o.writestr(i.filename, z.read(i))
        o.writestr("extra.txt", b"x")
    with pytest.raises(SystemExit, match="not listed"):
        pkg.import_package(bad)
    with zipfile.ZipFile(out) as z, zipfile.ZipFile(bad, "w") as o:
        for i in z.infolist():
            if i.filename != "README.md":
                o.writestr(i.filename, z.read(i))
    with pytest.raises(SystemExit, match="missing"):
        pkg.import_package(bad)
    assert not (tmp_path / "dest").exists()


def test_the_core_author_and_the_framework_type_are_refused(
    roots, tmp_path, monkeypatch
):
    make(roots, author="core")
    out = pkg.export("em/demo", tmp_path / "core.dkmod")
    monkeypatch.setenv("PATCHKIT_MODULES", str(tmp_path / "dest"))
    with pytest.raises(SystemExit, match="core"):
        pkg.import_package(out)
    (roots / "demo" / "module.toml").write_text(
        MODULE.format(author="em").replace('type = "machine"', 'type = "framework"')
    )
    monkeypatch.setenv("PATCHKIT_MODULES", str(roots))
    out = pkg.export("em/demo", tmp_path / "fw.dkmod")
    monkeypatch.setenv("PATCHKIT_MODULES", str(tmp_path / "dest"))
    with pytest.raises(SystemExit, match="framework"):
        pkg.import_package(out)


def test_a_known_id_needs_replace_and_the_default_target_is_patchkit_modules(
    roots, tmp_path, monkeypatch
):
    make(roots)
    out = pkg.export("em/demo", tmp_path / "demo.dkmod")
    with pytest.raises(SystemExit, match="--replace"):
        pkg.import_package(out, tmp_path / "other")
    assert pkg.import_package(out, roots, replace=True) == roots / "demo"
    with pytest.raises(SystemExit, match="lives in"):
        pkg.import_package(out, tmp_path / "other", replace=True)
    monkeypatch.delenv("PATCHKIT_MODULES")
    with pytest.raises(SystemExit, match="--dir"):
        pkg.import_package(out)
