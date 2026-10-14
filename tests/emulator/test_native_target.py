"""native_target: cargo target dirs keyed by the generated core."""

import tools.emulator.native_target as nt


def test_missing_or_unset_gen_is_nogen(tmp_path):
    assert nt.gen_key(None) == "nogen"
    assert nt.gen_key("") == "nogen"
    assert nt.gen_key(tmp_path / "absent") == "nogen"


def test_transpile_only_gen_is_generic_and_image_gen_is_hashed(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert nt.gen_key(plain) == "generic"
    with_image = tmp_path / "img"
    with_image.mkdir()
    (with_image / "image.rs").write_text("")
    other = tmp_path / "img2"
    other.mkdir()
    (other / "image.rs").write_text("")
    key = nt.gen_key(with_image)
    assert key.startswith("g-") and len(key) == 10
    assert key == nt.gen_key(with_image)
    assert key != nt.gen_key(other)


def test_prod_gen_and_target_dirs(tmp_path, monkeypatch):
    prod = tmp_path / "dn2-audio" / "gen"
    prod.mkdir(parents=True)
    monkeypatch.setattr(nt, "PROD_GEN", prod)
    monkeypatch.setattr(nt, "TARGET_ROOT", tmp_path / "target")
    assert nt.gen_key(prod) == "prod"
    assert nt.target_dir(prod) == tmp_path / "target" / "prod"
    assert nt.target_dir(prod, "pgo-use") == tmp_path / "target" / "prod" / "pgo-use"
    monkeypatch.setenv("SHARC_GEN_DIR", str(prod))
    assert nt.target_dir() == tmp_path / "target" / "prod"
    monkeypatch.delenv("SHARC_GEN_DIR")
    assert nt.target_dir() == tmp_path / "target" / "nogen"
