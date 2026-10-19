"""rust_ws.affected_packages: changed files -> workspace packages, over the real cargo metadata."""

from tools.devtools import rust_ws

SHARC_USERS = [
    "digi-control",
    "digi-coupled",
    "digiemu-desktop",
    "elektron-native-boot",
    "sharc-fast-cl",
    "sharc-fast-wasm",
    "sharc-native",
]
# module and port files are read by these two crates' tests at run time: their dependents do not run
RUNTIME_READERS = ["elektron-native-boot", "patchkit"]
PATCHKIT_USERS = [
    "digi-control",
    "digi-coupled",
    "digiemu-desktop",
    "elektron-native-boot",
    "patchkit",
]


def sel(*files):
    return rust_ws.affected_packages(list(files))


def test_package_file_gives_package_and_reverse_deps():
    assert sel("packages/emulator/sharc/src/lib.rs") == SHARC_USERS
    # a leaf: only itself
    assert sel("apps/desktop/src-tauri/src/main.rs") == ["digiemu-desktop"]
    # longest prefix: sharc-gen is not part of sharc
    assert "sharc-gen" not in sel("packages/emulator/sharc/build.rs")


def test_device_toml_goes_to_every_reader():
    got = sel("devices/digitakt-ii.toml")
    assert got == sorted(set(PATCHKIT_USERS) | {"device-profile", "plusdrive-format"})
    # the LED tables are read by patchkit's build.rs only
    assert sel("devices/digitakt-ii-leds.toml") == PATCHKIT_USERS


def test_ports_and_cycle_factors():
    assert sel("devices/ports/dt2-1.16.toml") == RUNTIME_READERS
    assert sel("devices/cycle-factors.json") == ["digi-control", "digiemu-desktop"]


def test_module_build_output_goes_to_patchkit_readers():
    assert sel("modules/core/runtime/build/runtime.dko.json") == RUNTIME_READERS
    # a source file of patchkit still takes its dependents
    assert (
        sel(
            "modules/core/runtime/build/runtime.dko.json",
            "packages/patchkit/src/typed.rs",
        )
        == PATCHKIT_USERS
    )
    assert sel("modules/core/runtime/README.md") == []


def test_scenario_pin_read_by_coupled():
    assert sel("tests/scenarios/dn2.json") == [
        "digi-control",
        "digi-coupled",
        "digiemu-desktop",
    ]
    assert sel("tests/scenarios/dt2.json") == []


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
        sel("tools/patchkit/pk_build.py", "tests/devtools/test_x.py", "mise.toml") == []
    )
    assert sel("tools/emulator/fast_regions.json") == []
    assert sel() == []


def test_union_of_files():
    got = sel("docs/x.md", "packages/dkupload/src/lib.rs")
    assert got == sorted({"dkupload", "digiemu-desktop", *PATCHKIT_USERS})
