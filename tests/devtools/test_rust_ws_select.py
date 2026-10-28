"""rust_ws.affected_packages: changed files -> workspace packages, over the real cargo metadata."""

from tools.devtools import rust_ws


def sel(*files):
    return rust_ws.affected_packages(list(files))


def test_package_file_gives_package_and_reverse_deps():
    assert "digi-coupled" in sel("packages/emulator/sharc/src/lib.rs")
    # a leaf: only itself
    assert sel("apps/desktop/src-tauri/src/main.rs") == ["digiemu-desktop"]
    # longest prefix: sharc-gen is not part of sharc
    assert "sharc-gen" not in sel("packages/emulator/sharc/build.rs")


def test_whole_workspace_files():
    for f in [
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        ".cargo/config.toml",
        "unknown.bin",
    ]:
        assert sel(f) is None, f
    # one such file among harmless ones is enough
    assert sel("docs/x.md", "Cargo.lock") is None


def test_no_rust_relevance():
    assert sel("docs/x.md") == []
    assert (
        sel("tools/patchkit/pk_new.py", "tests/devtools/test_x.py", "mise.toml") == []
    )
    assert sel("tools/emulator/fast_regions.json") == []
    assert sel("modules/core/runtime/README.md") == []
    assert sel() == []
