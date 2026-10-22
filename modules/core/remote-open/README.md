# core/remote-open

A marker module with one function, `remote_open`. The runtime tests whether its address is non-zero: with it in the patch set, the REMOTE switch
(DIGIKIT menu) starts on at every boot; without it, off. The emulator's patch set has it (`devices/*.toml` `[patch_sets]`), a device's does not.
The switch is never saved; the default applies at every boot. See `docs/plans/remote-control.md`.
