# Late faulting module (test)

A module that stops the machine a configured number of DSP blocks after the first UI-loop pass (reset or hang), to test that the digikit
runtime's boot counter counts a boot that dies before its stable period as bad and one that dies after as good
(docs/findings/37-boot-load.md). The delay is the store file `cfg/fault-after.bin`. Test fixture only.
