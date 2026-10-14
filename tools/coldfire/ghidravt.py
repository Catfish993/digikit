"""Run Ghidra's automatic Version Tracking between two programs; export the matches.

    uv run python -m tools.coldfire.ghidravt run \\
        --project ~/ghidra-projects/elektron-emac --project-name elektron-emac \\
        --source /dt2-1.15C/section_3_MAIN_OS.bin --dest /dt2-1.16/section_3_MAIN_OS.bin \\
        --session /vt/dt2-1.15C_to_dt2-1.16 --json out/vt/dt2-1.15C_to_dt2-1.16.json \\
        [--heap 32G] [--no-duplicates] [--dupe-min-len 10] [--replace] [--all]
    uv run python -m tools.coldfire.ghidravt export --project ... --project-name ... \\
        --session /vt/dt2-1.15C_to_dt2-1.16 --json out/vt/dt2-1.15C_to_dt2-1.16.json [--all]

`run` does what Ghidra's AutoVersionTrackingScript.java does with its default
options: it creates the session, runs AutoVersionTrackingTask, which accepts
matches and applies their markup (names, labels, comments) to the
destination, then saves the destination and the session. If the task fails,
nothing is saved. It fails if the session exists, unless --replace deletes it
first. Run headless through analyzeHeadless, the script gets a 2 GB heap and
prints nothing until it ends; on Digitakt II 1.15C -> 1.16 its duplicate
function correlator ran out of memory after 30 minutes.

The JSON holds the programs, the task's status message, the match count of
each correlator by association status, and the accepted matches (every match
with --all): correlator, type, status, source and destination address and
name, similarity, confidence and lengths.
"""

import argparse
import json
import os
import sys
import time

from tools.coldfire.ghidracopy import split_path  # imports no ghidra module

# tools/coldfire/ghidra/ is a plain directory of Java scripts that shadows the real
# `ghidra` Java-bridge namespace PyGhidra needs, once tools/ lands on
# sys.path -- which it does when this is run as `python -m tools.coldfire.ghidravt`.
_here = os.path.dirname(os.path.abspath(__file__))

DEFAULT_GHIDRA = '/opt/homebrew/Cellar/ghidra/12.1.3/libexec'
DEFAULT_HEAP = '32G'


def summarize(matches):
    """-> {correlator: {status: count}} over match records."""
    out = {}
    for m in matches:
        by_status = out.setdefault(m['correlator'], {})
        by_status[m['status']] = by_status.get(m['status'], 0) + 1
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('run', 'export'):
        s = sub.add_parser(name)
        s.add_argument('--project', required=True, help='project directory')
        s.add_argument('--project-name', required=True)
        s.add_argument('--session', required=True, help='session path in the project')
        s.add_argument('--json', help='write the matches here')
        s.add_argument('--all', action='store_true', help='export every match, not only accepted ones')
        s.add_argument('--heap', default=DEFAULT_HEAP, help='JVM -Xmx (default %s)' % DEFAULT_HEAP)
        if name == 'run':
            s.add_argument('--source', required=True, help='source program path')
            s.add_argument('--dest', required=True, help='destination program path')
            s.add_argument('--no-duplicates', action='store_true',
                           help='skip the duplicate function correlator')
            s.add_argument('--dupe-min-len', type=int, default=10,
                           help='minimum function length for the duplicate function correlator')
            s.add_argument('--replace', action='store_true',
                           help='delete an existing session of that name first')
    return p.parse_args(argv)


def start_ghidra(heap):
    os.environ.setdefault('GHIDRA_INSTALL_DIR', DEFAULT_GHIDRA)
    from pyghidra.launcher import HeadlessPyGhidraLauncher
    launcher = HeadlessPyGhidraLauncher(verbose=False)
    launcher.add_vmargs('-Xmx' + heap)
    launcher.start()


def tool_options(args):
    """The options of AutoVersionTrackingScript.createDefaultOptions()."""
    from ghidra.feature.vt.api.util import VTOptions
    from ghidra.feature.vt.gui.util import VTOptionDefines as D
    o = VTOptions('Dummy')
    o.setBoolean(D.CREATE_IMPLIED_MATCHES_OPTION, True)
    o.setBoolean(D.RUN_EXACT_SYMBOL_OPTION, True)
    o.setBoolean(D.RUN_EXACT_DATA_OPTION, True)
    o.setBoolean(D.RUN_EXACT_FUNCTION_BYTES_OPTION, True)
    o.setBoolean(D.RUN_EXACT_FUNCTION_INST_OPTION, True)
    o.setBoolean(D.RUN_DUPE_FUNCTION_OPTION, not args.no_duplicates)
    o.setBoolean(D.RUN_REF_CORRELATORS_OPTION, True)
    o.setInt(D.DATA_CORRELATOR_MIN_LEN_OPTION, 5)
    o.setInt(D.SYMBOL_CORRELATOR_MIN_LEN_OPTION, 3)
    o.setInt(D.FUNCTION_CORRELATOR_MIN_LEN_OPTION, 10)
    o.setInt(D.DUPE_FUNCTION_CORRELATOR_MIN_LEN_OPTION, args.dupe_min_len)
    o.setDouble(D.REF_CORRELATOR_MIN_SCORE_OPTION, 0.95)
    o.setDouble(D.REF_CORRELATOR_MIN_CONF_OPTION, 10.0)
    o.setBoolean(D.APPLY_IMPLIED_MATCHES_OPTION, True)
    o.setInt(D.MIN_VOTES_OPTION, 2)
    o.setInt(D.MAX_CONFLICTS_OPTION, 0)
    return o


def hx(addr):
    return '0x%08x' % addr.getOffset()


def symbol_name(program, addr):
    sym = program.getSymbolTable().getPrimarySymbol(addr)
    return str(sym.getName(True)) if sym is not None else None


def score(vt_score):
    return float(vt_score.getScore()) if vt_score is not None else None


def export(session, everything):
    """-> (records, summary) for the session's matches."""
    src, dst = session.getSourceProgram(), session.getDestinationProgram()
    records = []
    for match_set in session.getMatchSets():
        info = match_set.getProgramCorrelatorInfo()
        correlator = str(info.getName()) if info is not None else None
        for m in match_set.getMatches():
            a = m.getAssociation()
            records.append({
                'correlator': correlator, 'type': str(a.getType()), 'status': str(a.getStatus()),
                'source': hx(m.getSourceAddress()), 'dest': hx(m.getDestinationAddress()),
                'source_name': symbol_name(src, m.getSourceAddress()),
                'dest_name': symbol_name(dst, m.getDestinationAddress()),
                'similarity': score(m.getSimilarityScore()),
                'confidence': score(m.getConfidenceScore()),
                'source_length': int(m.getSourceLength()),
                'dest_length': int(m.getDestinationLength()),
            })
    summary = summarize(records)
    if not everything:
        records = [r for r in records if r['status'] == 'ACCEPTED']
    records.sort(key=lambda r: (r['source'], r['dest'], r['correlator'] or ''))
    return records, summary


def write_report(path, session, args, records, summary, status=None, seconds=None):
    src, dst = session.getSourceProgram(), session.getDestinationProgram()
    report = {
        'tool': 'tools/coldfire/ghidravt.py', 'project': args.project, 'session': args.session,
        'source': str(src.getDomainFile().getPathname()),
        'source_sha256': str(src.getExecutableSHA256()),
        'dest': str(dst.getDomainFile().getPathname()),
        'dest_sha256': str(dst.getExecutableSHA256()),
        'status': status, 'seconds': seconds, 'all_matches': args.all,
        'summary': summary, 'matches': records,
    }
    if args.cmd == 'run':
        report['options'] = {'duplicates': not args.no_duplicates,
                             'dupe_min_len': args.dupe_min_len, 'heap': args.heap}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(report, f, indent=1)
    print('wrote %d matches to %s' % (len(records), path))


def print_summary(summary):
    for correlator in sorted(summary, key=lambda c: c or ''):
        counts = ', '.join('%s %d' % kv for kv in sorted(summary[correlator].items()))
        print('  %s: %s' % (correlator, counts))


def run(args, project, monitor):
    from java.lang import Object
    from ghidra.feature.vt.api.db import VTSessionDB
    from ghidra.feature.vt.gui.actions import AutoVersionTrackingTask
    from ghidra.util.task import ConsoleTaskMonitor
    data = project.getProjectData()
    folder_path, name = split_path(args.session)
    folder = data.getRootFolder()
    for part in [p for p in folder_path.split('/') if p]:
        folder = folder.getFolder(part) or folder.createFolder(part)
    existing = folder.getFile(name)
    if existing is not None:
        if not args.replace:
            raise SystemExit('%s exists (use --replace)' % args.session)
        if existing.getContentType() != 'VersionTracking':
            raise SystemExit('%s is a %s, not a session' % (args.session, existing.getContentType()))
        existing.delete()
    files = {}
    for role, path in (('source', args.source), ('dest', args.dest)):
        files[role] = data.getFile(path)
        if files[role] is None:
            raise SystemExit('no %s program %s' % (role, path))
    consumer = Object()
    source = files['source'].getDomainObject(consumer, False, False, monitor)
    try:
        dest = files['dest'].getDomainObject(consumer, False, False, monitor)
        try:
            session = VTSessionDB(name, source, dest, consumer)
            try:
                folder.createFile(name, session, monitor)
                task = AutoVersionTrackingTask(session, tool_options(args))
                started = time.time()
                task.monitoredRun(ConsoleTaskMonitor())
                seconds = round(time.time() - started, 1)
                dest.save('Updated with Auto Version Tracking', monitor)
                session.save()
                status = str(task.getStatusMsg())
                print('%.1f s: %s' % (seconds, status))
                records, summary = export(session, args.all)
                print_summary(summary)
                if args.json:
                    write_report(args.json, session, args, records, summary, status, seconds)
            finally:
                session.release(consumer)
        finally:
            dest.release(consumer)
    finally:
        source.release(consumer)


def export_session(args, project, monitor):
    from java.lang import Object
    df = project.getProjectData().getFile(args.session)
    if df is None:
        raise SystemExit('no session %s' % args.session)
    consumer = Object()
    session = df.getDomainObject(consumer, False, False, monitor)
    try:
        records, summary = export(session, args.all)
        print_summary(summary)
        if args.json:
            write_report(args.json, session, args, records, summary)
    finally:
        session.release(consumer)


def main(argv=None):
    args = parse_args(argv)
    args.project = os.path.expanduser(args.project)
    start_ghidra(args.heap)
    import pyghidra
    monitor = pyghidra.task_monitor()
    project = pyghidra.open_project(args.project, args.project_name, create=False)
    try:
        (run if args.cmd == 'run' else export_session)(args, project, monitor)
    finally:
        project.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
