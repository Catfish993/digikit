# test/dyn-demo

A test fixture for the digikit runtime (`core/runtime`), not for players. A `#![no_std]` Rust crate with `dkm_init`,
`dkm_slot_0` and `dkm_fini`. Pack it with `patchkit pack test/dyn-demo -o dyn-demo.dkm`, put the file in
`plus_drive/<device>/digikit/modules/`, and ask the runtime to load it (`modules/core/runtime/README.md`). While it is
loaded, its slot-0 function counts the UI task's event-loop passes. Emulator only.
