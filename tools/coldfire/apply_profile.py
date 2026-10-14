# fmt: off
"""Apply a resolved symbol profile (from tools/coldfire/export_profile.py) as labels
onto a Ghidra program, via PyGhidra -- so Ghidra's cross-references and
decompiler benefit from the same names emu/symbols.py already resolved,
without re-deriving them by hand in the GUI.

    GHIDRA_INSTALL_DIR=/opt/homebrew/Cellar/ghidra/12.1.3/libexec \
    uv run python -m tools.coldfire.apply_profile PROFILE.json PROGRAM_NAME
                                          [--project ~/ghidra-projects/dt2]
                                          [--project-name dt2]
                                          [--dry-run]

`PROGRAM_NAME` is the program's path inside the project, e.g.
"dn2_MAIN_OS.bin" or "/dn2_MAIN_OS.bin" (the leading slash is optional).

Idempotent: a symbol is skipped, not reapplied, if a label with the exact
same name already sits at that exact address (whatever put it there). It
never removes or renames any existing symbol -- Ghidra allows more than one
label per address, so an existing symbol survives untouched even at the
address of a symbol this applies; this only ever *adds*. Applied labels use
SourceType.ANALYSIS (not USER_DEFINED), so this never contends with, and can
never masquerade as, an address a person has hand-labeled -- a real
USER_DEFINED label at the same address is left exactly as it is, alongside
the new one.

A tuple-valued profile entry (e.g. task_create_sites -- multiple call sites)
gets one label at each of its addresses, suffixed `_0`, `_1`, ... so they
stay distinct and idempotency still works address-by-address. An unresolved
symbol (address null) is skipped and counted separately -- there is nothing
to label.
"""
import argparse
import json
import os
import sys

DEFAULT_PROJECT = os.path.expanduser('~/ghidra-projects/dt2')
DEFAULT_PROJECT_NAME = 'dt2'


def labels_from_profile(profile):
    """-> [(name, address_int), ...] flattened from the JSON's `symbols` map."""
    out = []
    for name, entry in profile['symbols'].items():
        if 'address' in entry:
            if entry['address'] is not None:
                out.append((name, int(entry['address'], 16)))
        else:  # tuple-valued: addresses list
            for i, a in enumerate(entry.get('addresses', [])):
                out.append(('%s_%d' % (name, i), int(a, 16)))
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('profile', help='profile JSON produced by tools/coldfire/export_profile.py')
    ap.add_argument('program', help='program path inside the project, e.g. dn2_MAIN_OS.bin')
    ap.add_argument('--project', default=DEFAULT_PROJECT)
    ap.add_argument('--project-name', default=DEFAULT_PROJECT_NAME)
    ap.add_argument('--dry-run', action='store_true', help="report what would happen, apply nothing")
    args = ap.parse_args(argv)

    os.environ.setdefault('GHIDRA_INSTALL_DIR', '/opt/homebrew/Cellar/ghidra/12.1.3/libexec')
    import pyghidra
    pyghidra.start(verbose=False)

    with open(args.profile) as fh:
        profile = json.load(fh)
    wanted = labels_from_profile(profile)
    print('%s: %d labelable symbol(s) from %s (device=%s)'
          % (args.program, len(wanted), args.profile, profile.get('device')))

    program_path = args.program if args.program.startswith('/') else '/' + args.program

    project = pyghidra.open_project(args.project, args.project_name, create=False)
    try:
        with pyghidra.program_context(project, program_path) as program:
            from ghidra.program.model.symbol import SourceType

            af = program.getAddressFactory()
            space = af.getDefaultAddressSpace()
            symtab = program.getSymbolTable()

            created, skipped_identical, skipped_conflict = 0, 0, 0
            details = []

            def apply_one():
                nonlocal created, skipped_identical, skipped_conflict
                for name, addr_int in wanted:
                    addr = space.getAddress(addr_int)
                    existing = list(symtab.getSymbols(addr))
                    if any(s.getName() == name for s in existing):
                        skipped_identical += 1
                        continue
                    # Never overwritten -- createLabel only ADDS a symbol at this
                    # address; any existing symbol (including a real
                    # USER_DEFINED one) is left in place untouched. Ghidra's
                    # global namespace does require unique names across
                    # addresses though, so a name already claimed elsewhere
                    # (e.g. Ghidra's own default "entry" symbol at the raw
                    # image's base) raises DuplicateNameException -- caught
                    # and counted as a conflict, never overwritten.
                    if args.dry_run:
                        created += 1
                        details.append('would create %-20s 0x%08x' % (name, addr_int))
                        continue
                    try:
                        symtab.createLabel(addr, name, SourceType.ANALYSIS)
                        created += 1
                        details.append('created       %-20s 0x%08x' % (name, addr_int))
                    except Exception as e:
                        skipped_conflict += 1
                        details.append('CONFLICT      %-20s 0x%08x -- %s' % (name, addr_int, e))

            if args.dry_run:
                apply_one()
            else:
                tx = program.startTransaction('apply-profile: %s' % os.path.basename(args.profile))
                ok = True
                try:
                    apply_one()
                except Exception:
                    ok = False
                    raise
                finally:
                    program.endTransaction(tx, ok)
                if ok:
                    program.getDomainFile().save(pyghidra.task_monitor())

            for line in details:
                print('  ' + line)
            print('created=%d  skipped_identical=%d  skipped_conflict=%d  unresolved_in_profile=%d'
                  % (created, skipped_identical, skipped_conflict,
                     len(profile.get('unresolved', []))))
    finally:
        project.close()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
