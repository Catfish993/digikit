/* core/remote-open: a marker. The runtime's REMOTE switch (DIGIKIT menu > REMOTE; the commands of the remote range, sdk/sdk.toml
 * `[protocol] guarded`) starts on at every boot when this function exists in the image, and off when it does not. The emulator's patch set has the
 * module, a device's does not: on a device the owner switches REMOTE on. The runtime never calls the function, it only tests that its address is not 0. */

int remote_open(void)
{
    return 1;
}
