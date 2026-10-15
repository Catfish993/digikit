# firmware/

Put your own Elektron firmware images (`.syx`) here. Nothing in this directory is
committed except this file: the firmware is copyright Elektron, and `.gitignore` keeps it
out of git. Get the files from Elektron's support site for your device.

The tools look for these names (the SHA-256 identifies the release; the file name is only
a hint):

| device | version | file | sha256 |
| --- | --- | --- | --- |
| Digitakt II | 1.17 | `Digitakt_II_OS1.17.syx` | `26c22f6652625ac2cfd47f7ee970d388ed8b6427dae3563c0a6a2f2d334350d5` |
| Digitone II | 1.12 | `Digitone_II_OS1.12.syx` | `7e5b8a733d4ab1fdf7e5d22757f7d8eddaa3cbf71d0858874a791844c02ceb35` |
| Digitakt II | 1.16 | `Digitakt_II_OS1.16.syx` | `278541e466edcd77d6b3e018a91fb90185932d3c7de224dd3e68294dddf3a9ec` |
| Digitone II | 1.11 | `Digitone_II_OS1.11.syx` | `2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0` |
| Digitakt II | 1.15C | `Digitakt_II_OS1.15C.syx` | `62d588456e47194bd56dfee9568fb9dd4521c4ff1e8b5427eb461355532e8c6c` |
| Digitone II | 1.10E | `Digitone_II_OS1.10E.syx` | `9881ee5ca4624be2b833e5eb94e294f2f8425371e6729781baccd1c256cac504` |

Python tools find an image with `tools/firmware/config.py`: `stock(name)` is the path of a
named image in this directory, and `firmware()` picks one (an explicit path, then `DT2_SYX`,
then the only `.syx` here). Data extracted from an image goes under `out/` (for example
`out/sections/dt2-1.16/`), also never committed.

Environment variables that override the paths:

- `DT2_SYX`: the firmware for tools that take one image.
- `DIGI_EMU_SYX`: the Digitone II image of the emulator launcher.
- `DIGI_EMU_SYX_<DEV>` (for example `DIGI_EMU_SYX_DT2`): the image for one device.

In a lane worktree, `tools/devtools/worktree-setup.sh` links the images from the main tree.
