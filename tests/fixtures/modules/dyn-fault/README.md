# Faulting module (test)

A module whose `init` resets the machine (the firmware's reaction to a fault). With it selected, every boot resets until the runtime's boot counter reaches two and the next boot
starts in safe mode with no modules (docs/findings/37-boot-load.md). Test fixture only.
