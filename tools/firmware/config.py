"""Where the firmware, its sections and the snapshots live.

Nothing in this project should hardcode a firmware filename. It did, in
thirteen files, and the cost was concrete: `unicorn-reference-final:emu/run.py` accepted a `.syx`
argument, passed it to nothing, and cheerfully ran a *different* firmware's
image and snapshots while printing the name you asked for. A hardcoded default
that silently wins over an explicit argument is worse than no default.

Every path here resolves in the same order:

    1. an explicit argument, if the caller passed one
    2. the environment variable
    3. discovery in the working directory
    4. an error that says what it looked for and how to tell it

Environment variables:

    DT2_SYX        the firmware .syx
    DT2_SECTIONS   the directory of extracted sections   (default: sections)
    DT2_SNAPSHOTS  the directory of boot snapshots        (default: snapshots)
    DT2_MAIN_IMG   the decompressed MAIN OS image

Discovery is by pattern, not by literal name, so another Elektron device's
sections work without editing anything -- `section_3_MAIN_OS.bin` is one
device's naming, not a law.
"""

import glob
import os

SECTIONS_DEFAULT = "sections"
SNAPSHOTS_DEFAULT = "snapshots"

# The build this project was reverse-engineered against. It breaks a tie when
# several firmware files are present, and it never overrides an explicit
# argument or DT2_SYX.
TESTED_SYX = "Digitakt_II_OS1.15C.syx"
TESTED_NAME = "Digitakt II OS 1.15C"


class NotFound(SystemExit):
    """A path could not be resolved. The message says how to resolve it."""


def sections_dir():
    return os.environ.get("DT2_SECTIONS", SECTIONS_DEFAULT)


def snapshots_dir():
    return os.environ.get("DT2_SNAPSHOTS", SNAPSHOTS_DEFAULT)


def firmware(explicit=None):
    """-> the .syx path.

    An explicit argument wins, then DT2_SYX, then the only `.syx` present.
    With several present the tested build breaks the tie -- every address in
    this project targets it, so that is a stated preference rather than a
    guess -- and several with none of them tested is a refusal. What must
    never happen is a default quietly overriding something you named.
    """
    if explicit:
        if not os.path.exists(explicit):
            raise NotFound("No such firmware file: %s" % explicit)
        return explicit
    env = os.environ.get("DT2_SYX")
    if env:
        if not os.path.exists(env):
            raise NotFound("DT2_SYX points at a missing file: %s" % env)
        return env
    found = sorted(glob.glob("*.syx"))
    if len(found) == 1:
        return found[0]
    # Several present: prefer the build this project is keyed to, which keeps
    # the many commands that have no firmware argument working. Only a real tie --
    # several, none of them tested -- refuses.
    if TESTED_SYX in found:
        return TESTED_SYX
    if not found:
        raise NotFound(
            "No firmware found.\n\n"
            "No Elektron firmware ships with this repository and none ever\n"
            "should -- it is copyright Elektron. Put your own lawfully-obtained\n"
            ".syx in the working directory, pass it as an argument, or set\n"
            "DT2_SYX. Only %s has been tested." % TESTED_NAME
        )
    raise NotFound(
        "Several firmware files here, so which one is ambiguous:\n\n%s\n\n"
        "Pass one as an argument or set DT2_SYX. This is deliberately not\n"
        "guessed: picking the wrong one runs one firmware under another's\n"
        "name." % "\n".join("    " + f for f in found)
    )


def main_image(explicit=None):
    """-> the decompressed MAIN OS section, found by pattern not by name."""
    if explicit:
        if not os.path.exists(explicit):
            raise NotFound("No such MAIN OS image: %s" % explicit)
        return explicit
    env = os.environ.get("DT2_MAIN_IMG")
    if env:
        if not os.path.exists(env):
            raise NotFound("DT2_MAIN_IMG points at a missing file: %s" % env)
        return env
    d = sections_dir()
    found = sorted(glob.glob(os.path.join(d, "*MAIN_OS*.bin")))
    if len(found) == 1:
        return found[0]
    if not found:
        raise NotFound(
            "No MAIN OS image in %s/.\n\n"
            "The sections inside a .syx are compressed. Decompress them once:\n\n"
            "    uv run python -m tools.firmware.extract <firmware.syx> -o %s/\n\n"
            "Or set DT2_MAIN_IMG to an image you already have." % (d, d)
        )
    raise NotFound(
        "Several MAIN OS images in %s/:\n%s\n\nSet DT2_MAIN_IMG."
        % (d, "\n".join("    " + f for f in found))
    )


def bootstrap(explicit=None):
    """-> the bootstrap section (section 2), which holds the acceptance code."""
    if explicit:
        if not os.path.exists(explicit):
            raise NotFound("No such bootstrap image: %s" % explicit)
        return explicit
    d = sections_dir()
    found = sorted(glob.glob(os.path.join(d, "section_2_*.bin")))
    if found:
        return found[0]
    raise NotFound(
        "No section 2 (bootstrap) in %s/. Extract the sections "
        "first; see main_image()." % d
    )


def snapshot(name):
    """-> a path inside the snapshot directory."""
    return os.path.join(snapshots_dir(), name)


def is_tested(syx):
    return os.path.basename(syx) == TESTED_SYX
