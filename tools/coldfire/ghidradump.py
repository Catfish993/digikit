# fmt: off
"""Dump one Ghidra program to files for grep and SQL.

    uv run python -m tools.coldfire.ghidradump --out out/ghidra/dt2-1.15C-emac \
        [--project ~/ghidra-projects/dt2-emac] [--project-name dt2-emac] \
        [--program /section_3_MAIN_OS.bin] [--threads N] [--timeout 60] \
        [--rtti out/symbols/dt2-1.15C-rtti.json] \
        [--image out/sections/dt2-1.16/section_3_MAIN_OS.bin] \
        [--only-functions ADDR ... | --limit N]

Every Ghidra query starts a JVM, and only one JVM can hold a project, so
agents wait for each other. This writes what those queries return, once:

  OUT/manifest.json        image and .syx SHA-256, project, program,
                           language, counts, decompile failures and timing;
                           "complete" is false until the run finishes
  OUT/functions.jsonl      one object per function: entry, name, size, body
                           ranges, signature, callers, callees, strings,
                           globals and MMIO referenced, file paths
  OUT/decomp/ENTRY_NAME.c  decompiled C
  OUT/disasm/ENTRY_NAME.s  address, bytes in 16-bit words, instruction
  OUT/xrefs.sqlite         functions, function_ranges, calls, data_refs,
                           strings, symbols, blocks; with --rtti also
                           typeinfos and vtables (one row per slot).
                           Addresses are integers; ranges are inclusive.

    rg -l 4094e4f OUT/decomp
    sqlite3 OUT/xrefs.sqlite "select printf('%x', site), f.name, kind
        from data_refs d join functions f on f.entry = d.func
        where to_addr between 0x4094e4f0 and 0x4094e4ff"

Read-only: no transaction, nothing saved. Close any other process that has
the project open first.

Metadata, disassembly and SQLite are rebuilt on every run. Decompiling is
the slow part: --only-functions decompiles only the functions containing
the given addresses and keeps the other .c files (use it after renaming a
few functions); --limit decompiles the first N functions, to time a run.
A .c file written under an older function name shows "decomp_stale": true
in functions.jsonl. After a seeding or RTTI pass, run the full dump again.

Decompiled C is Ghidra's interpretation: check claims against the bytes.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_here = os.path.dirname(os.path.abspath(__file__))

REPO = os.path.dirname(os.path.dirname(_here))
DEFAULT_PROJECT = os.path.expanduser('~/ghidra-projects/dt2-emac')
DEFAULT_PROJECT_NAME = 'dt2-emac'
DEFAULT_PROGRAM = '/section_3_MAIN_OS.bin'
DEFAULT_GHIDRA = '/opt/homebrew/Cellar/ghidra/12.1.3/libexec'
DEFAULT_TIMEOUT = 60
TOOL_VERSION = 1
MAX_STEM = 160
# tools/coldfire/ghidra/McfLabels.java creates a block named MCF5441X_<address> for
# each peripheral it labels, so references into those blocks are MMIO.
MMIO_BLOCK_PREFIX = 'MCF5441X_'
STEM = re.compile(r'([0-9a-f]{8})_.*')

TABLES = """
CREATE TABLE functions(entry INTEGER PRIMARY KEY, name TEXT, namespace TEXT,
    size INTEGER, signature TEXT, decomp TEXT, disasm TEXT, decomp_error TEXT);
CREATE TABLE function_ranges(func INTEGER, lo INTEGER, hi INTEGER);
CREATE TABLE calls(from_func INTEGER, to_func INTEGER, to_addr INTEGER,
    site INTEGER, kind TEXT);
CREATE TABLE data_refs(site INTEGER, func INTEGER, to_addr INTEGER, kind TEXT,
    label TEXT, block TEXT);
CREATE TABLE strings(addr INTEGER PRIMARY KEY, text TEXT);
CREATE TABLE symbols(addr INTEGER, name TEXT, namespace TEXT, type TEXT,
    source TEXT, is_primary INTEGER);
CREATE TABLE blocks(name TEXT, lo INTEGER, hi INTEGER, initialized INTEGER,
    type TEXT);
CREATE TABLE typeinfos(addr INTEGER PRIMARY KEY, kind TEXT, name TEXT,
    mangled TEXT, name_addr INTEGER, bases TEXT);
CREATE TABLE vtables(addr INTEGER, class TEXT, typeinfo INTEGER,
    offset_to_top INTEGER, slot INTEGER, target INTEGER, owner TEXT);
"""

INDEXES = """
CREATE INDEX function_ranges_lo ON function_ranges(lo, hi);
CREATE INDEX calls_from ON calls(from_func);
CREATE INDEX calls_to ON calls(to_func);
CREATE INDEX calls_to_addr ON calls(to_addr);
CREATE INDEX calls_site ON calls(site);
CREATE INDEX data_refs_to ON data_refs(to_addr);
CREATE INDEX data_refs_func ON data_refs(func);
CREATE INDEX data_refs_site ON data_refs(site);
CREATE INDEX symbols_addr ON symbols(addr);
CREATE INDEX symbols_name ON symbols(name);
CREATE INDEX vtables_addr ON vtables(addr);
CREATE INDEX vtables_target ON vtables(target);
"""


def log(message):
    print(message, file=sys.stderr, flush=True)


def hx(value):
    return '0x%08x' % value


def file_stem(entry, name):
    """'4002d652_Class__method': the entry, then the name reduced to [A-Za-z0-9_.-]."""
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', name.replace('::', '__')) or 'unnamed'
    return ('%08x_%s' % (entry, safe))[:MAX_STEM]


def disasm_line(addr, raw, text):
    """One listing line: address, bytes in 16-bit words, instruction."""
    words = ' '.join(raw[i:i + 2].hex() for i in range(0, len(raw), 2))
    return '%08x  %-24s  %s' % (addr, words, text)


def build_rows(funcs, targets):
    """-> (calls, data_refs) rows from collected function facts.

    A call reference is a call row whether or not a function starts at its
    target (to_func is None when none does). A jump to another function's
    entry is a call row too (a tail call); other jumps are control flow
    inside the function and are dropped. Every non-flow reference to memory
    is a data_refs row.
    """
    calls, data_refs = [], []
    for entry in sorted(funcs):
        for ref in funcs[entry]['refs']:
            to = ref['to']
            if ref['call'] or (ref['flow'] and to in funcs and to != entry):
                calls.append((entry, to if to in funcs else None, to, ref['site'], ref['kind']))
            elif not ref['flow']:
                t = targets.get(to, {})
                data_refs.append((ref['site'], entry, to, ref['kind'], t.get('label'), t.get('block')))
    return calls, data_refs


def existing_decomp(directory):
    """-> {entry: file stem} for the .c files already in a dump's decomp/."""
    found = {}
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else ():
        m = STEM.fullmatch(name[:-2]) if name.endswith('.c') else None
        if m:
            found[int(m.group(1), 16)] = name[:-2]
    return found


def decomp_status(funcs, files, errors):
    """-> {entry: {'path', 'error', 'stale'}}; stale means the .c file was
    written under a different function name."""
    status = {}
    for entry, fn in funcs.items():
        stem = files.get(entry)
        status[entry] = {'path': 'decomp/%s.c' % stem if stem else None,
                         'error': errors.get(entry),
                         'stale': stem is not None and stem != fn['stem']}
    return status


def function_records(funcs, calls, data_refs, targets, status):
    """-> functions.jsonl objects in entry order."""
    callers, callees, refs = {}, {}, {}
    for frm, to, _, _, _ in calls:
        if to is not None:
            callees.setdefault(frm, set()).add(to)
            callers.setdefault(to, set()).add(frm)
    for row in data_refs:
        refs.setdefault(row[1], []).append(row)

    def named(entries):
        return [{'entry': hx(e), 'name': funcs[e]['name']} for e in sorted(entries)]

    records = []
    for entry in sorted(funcs):
        fn = funcs[entry]
        strings, mmio, globals_ = {}, [], []
        for site, _, to, kind, label, block in refs.get(entry, []):
            text = targets.get(to, {}).get('string')
            if text is not None:
                strings[to] = text
            elif block and block.startswith(MMIO_BLOCK_PREFIX):
                mmio.append({'site': hx(site), 'addr': hx(to), 'label': label, 'kind': kind})
            else:
                globals_.append({'site': hx(site), 'addr': hx(to), 'label': label,
                                 'kind': kind, 'block': block})
        st = status.get(entry, {})
        records.append({
            'entry': hx(entry), 'name': fn['name'], 'namespace': fn['namespace'],
            'size': fn['size'], 'ranges': [[hx(lo), hx(hi)] for lo, hi in fn['ranges']],
            'signature': fn['signature'],
            'callers': named(callers.get(entry, ())), 'callees': named(callees.get(entry, ())),
            'strings': [{'addr': hx(a), 'text': strings[a]} for a in sorted(strings)],
            'globals': globals_, 'mmio': mmio,
            'decomp': st.get('path'), 'decomp_error': st.get('error'),
            'decomp_stale': st.get('stale', False),
            'disasm': 'disasm/%s.s' % fn['stem'],
        })
    return records


def write_sqlite(path, funcs, status, calls, data_refs, strings, symbols, blocks, rtti=None):
    """Build the database next to path, then move it into place."""
    tmp = path + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    try:
        db.executescript(TABLES)
        db.executemany('INSERT INTO functions VALUES (?,?,?,?,?,?,?,?)',
                       [(e, f['name'], f['namespace'], f['size'], f['signature'],
                         status[e]['path'], 'disasm/%s.s' % f['stem'], status[e]['error'])
                        for e, f in sorted(funcs.items())])
        db.executemany('INSERT INTO function_ranges VALUES (?,?,?)',
                       [(e, lo, hi) for e, f in sorted(funcs.items()) for lo, hi in f['ranges']])
        db.executemany('INSERT INTO calls VALUES (?,?,?,?,?)', calls)
        db.executemany('INSERT INTO data_refs VALUES (?,?,?,?,?,?)', data_refs)
        db.executemany('INSERT INTO strings VALUES (?,?)', strings)
        db.executemany('INSERT INTO symbols VALUES (?,?,?,?,?,?)', symbols)
        db.executemany('INSERT INTO blocks VALUES (?,?,?,?,?)', blocks)
        if rtti:
            db.executemany('INSERT INTO typeinfos VALUES (?,?,?,?,?,?)',
                           [(t['addr'], t['kind'], t['name'], t['mangled'], t['name_addr'],
                             json.dumps(t['bases'])) for t in rtti['typeinfos']])
            db.executemany('INSERT INTO vtables VALUES (?,?,?,?,?,?,?)',
                           [(v['addr'], v['class'], v['typeinfo'], v['offset_to_top'], i, slot, owner)
                            for v in rtti['vtables']
                            for i, (slot, owner) in enumerate(zip(v['slots'], v['owners']))])
        db.executescript(INDEXES)
        db.commit()
    finally:
        db.close()
    os.replace(tmp, path)


def write_json(path, obj):
    with open(path + '.tmp', 'w') as f:
        json.dump(obj, f, indent=1)
        f.write('\n')
    os.replace(path + '.tmp', path)


def write_jsonl(path, records):
    with open(path + '.tmp', 'w') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')
    os.replace(path + '.tmp', path)


def read_prior(out, partial):
    """-> the dump's manifest.json, or None for a new or empty directory."""
    path = os.path.join(out, 'manifest.json')
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    if os.path.isdir(out) and os.listdir(out):
        raise SystemExit('%s is not empty and has no manifest.json; not touching it' % out)
    if partial:
        raise SystemExit('--only-functions updates an existing dump; %s has none' % out)
    return None


def clear_out(out, partial):
    """Remove what this run rewrites: disasm/ always, decomp/ unless partial."""
    for sub in ('disasm',) if partial else ('disasm', 'decomp'):
        shutil.rmtree(os.path.join(out, sub), ignore_errors=True)
    for sub in ('disasm', 'decomp'):
        os.makedirs(os.path.join(out, sub), exist_ok=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def git_head():
    try:
        return subprocess.run(['git', '-C', REPO, 'rev-parse', 'HEAD'], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def describe(program, args, program_path, rtti):
    """-> the manifest fields known before the dump starts."""
    image_sha = program.getExecutableSHA256()
    image_sha = str(image_sha) if image_sha is not None else None
    # The image on disk, by default sections/<program path>; --image names
    # another, such as out/sections/dt2-1.16/section_3_MAIN_OS.bin.
    sections_image = (os.path.abspath(args.image) if args.image
                      else os.path.join(REPO, 'sections', program_path.lstrip('/')))
    sections_sha = sha256_file(sections_image) if os.path.exists(sections_image) else None
    source_marker = os.path.join(os.path.dirname(sections_image), '.source-sha256')
    source_sha = None
    if os.path.exists(source_marker):
        with open(source_marker) as f:
            source_sha = f.read().strip()
    manifest = {
        'tool': 'tools/coldfire/ghidradump.py', 'tool_version': TOOL_VERSION, 'git_head': git_head(),
        'generated': utc_now(),
        'project': os.path.abspath(args.project), 'project_name': args.project_name,
        'program': program_path,
        'language': str(program.getLanguageID().getIdAsString()),
        'compiler': str(program.getCompilerSpec().getCompilerSpecID().getIdAsString()),
        'image_path': str(program.getExecutablePath()), 'image_sha256': image_sha,
        'sections_image': os.path.relpath(sections_image, REPO) if sections_sha else None,
        'sections_image_sha256': sections_sha,
        'image_matches_sections': sections_sha is not None and sections_sha == image_sha,
        'sections_source_sha256': source_sha,
        'error_bookmarks': int(program.getBookmarkManager().getBookmarkCount('Error')),
        'rtti': os.path.abspath(args.rtti) if args.rtti else None,
        'rtti_matches_image': rtti.get('sha256') == image_sha if rtti else None,
    }
    if sections_sha is not None and not manifest['image_matches_sections']:
        log('warning: %s differs from the image imported into Ghidra' % manifest['sections_image'])
    if rtti and not manifest['rtti_matches_image']:
        log('warning: %s was scanned from a different image' % args.rtti)
    return manifest


def containing_entries(program, addrs):
    """-> sorted entries of the functions containing each hex address."""
    fm = program.getFunctionManager()
    space = program.getAddressFactory().getDefaultAddressSpace()
    entries, missing = set(), []
    for a in addrs:
        fn = fm.getFunctionContaining(space.getAddress(int(a, 16)))
        if fn is None:
            missing.append(a)
        else:
            entries.add(int(fn.getEntryPoint().getOffset()))
    if missing:
        raise SystemExit('no function contains %s' % ', '.join(missing))
    return sorted(entries)


def collect(program, disasm_dir):
    """-> (funcs, targets). Writes disasm/<stem>.s for each function on the way.

    funcs: {entry: {'name', 'namespace', 'size', 'signature', 'ranges',
    'stem', 'refs': [{'site', 'to', 'kind', 'call', 'flow'}]}}.
    targets: {address: {'label', 'block', 'string'}} for every non-flow
    reference target.
    """
    from ghidra.program.model.listing import CodeUnitFormat
    fmt = CodeUnitFormat.DEFAULT
    listing = program.getListing()
    memory = program.getMemory()
    symtab = program.getSymbolTable()
    funcs, targets = {}, {}

    def target(addr):
        off = int(addr.getOffset())
        if off in targets:
            return
        sym = symtab.getPrimarySymbol(addr)
        block = memory.getBlock(addr)
        data = listing.getDataAt(addr)
        value = data.getValue() if data is not None and data.hasStringValue() else None
        targets[off] = {'label': str(sym.getName(True)) if sym is not None else None,
                        'block': str(block.getName()) if block is not None else None,
                        'string': str(value) if value is not None else None}

    it = program.getFunctionManager().getFunctions(True)
    while it.hasNext():
        fn = it.next()
        entry = int(fn.getEntryPoint().getOffset())
        name = str(fn.getName(True))
        body = fn.getBody()
        fact = {'name': name, 'namespace': str(fn.getParentNamespace().getName(True)),
                'size': int(body.getNumAddresses()), 'signature': str(fn.getSignature()),
                'ranges': [(int(r.getMinAddress().getOffset()), int(r.getMaxAddress().getOffset()))
                           for r in body.getAddressRanges()],
                'stem': file_stem(entry, name), 'refs': []}
        lines = ['; %s @ %s, %d bytes' % (name, hx(entry), fact['size']),
                 '; %s' % fact['signature']]
        insns = listing.getInstructions(body, True)
        while insns.hasNext():
            ins = insns.next()
            site = int(ins.getAddress().getOffset())
            label = ins.getLabel()
            if label is not None:
                lines.append('%s:' % label)
            raw = bytes(b & 0xFF for b in ins.getBytes())
            lines.append(disasm_line(site, raw, str(fmt.getRepresentationString(ins))))
            for ref in ins.getReferencesFrom():
                to = ref.getToAddress()
                if not to.isMemoryAddress():
                    continue
                rt = ref.getReferenceType()
                fact['refs'].append({'site': site, 'to': int(to.getOffset()), 'kind': str(rt),
                                     'call': bool(rt.isCall()), 'flow': bool(rt.isFlow())})
                if not rt.isFlow():
                    target(to)
        with open(os.path.join(disasm_dir, fact['stem'] + '.s'), 'w') as f:
            f.write('\n'.join(lines) + '\n')
        funcs[entry] = fact
    return funcs, targets


def collect_tables(program):
    """-> (strings, symbols, blocks) rows for SQLite."""
    listing = program.getListing()
    strings = []
    it = listing.getDefinedData(True)
    while it.hasNext():
        d = it.next()
        if d.hasStringValue():
            value = d.getValue()
            if value is not None:
                strings.append((int(d.getAddress().getOffset()), str(value)))
    symbols = []
    it = program.getSymbolTable().getAllSymbols(True)
    while it.hasNext():
        s = it.next()
        a = s.getAddress()
        if a.isMemoryAddress():
            symbols.append((int(a.getOffset()), str(s.getName()),
                            str(s.getParentNamespace().getName(True)), str(s.getSymbolType()),
                            str(s.getSource()), int(bool(s.isPrimary()))))
    blocks = [(str(b.getName()), int(b.getStart().getOffset()), int(b.getEnd().getOffset()),
               int(bool(b.isInitialized())), str(b.getType()))
              for b in program.getMemory().getBlocks()]
    return strings, symbols, blocks


def decompile(program, stems, entries, decomp_dir, threads, timeout):
    """Decompile entries on `threads` threads, one DecompInterface each.

    Writes decomp/<stem>.c; -> {entry: error} for those that did not complete.
    """
    from ghidra.app.decompiler import DecompInterface, DecompileOptions
    fm = program.getFunctionManager()
    space = program.getAddressFactory().getDefaultAddressSpace()
    local, opened, lock = threading.local(), [], threading.Lock()

    def interface():
        ifc = getattr(local, 'ifc', None)
        if ifc is None:
            ifc = DecompInterface()
            ifc.setOptions(DecompileOptions())
            with lock:
                opened.append(ifc)
            if not ifc.openProgram(program):
                raise RuntimeError('decompiler did not start: %s' % ifc.getLastMessage())
            local.ifc = ifc
        return ifc

    def one(entry):
        try:
            fn = fm.getFunctionAt(space.getAddress(entry))
            res = interface().decompileFunction(fn, timeout, None)
            if not res.decompileCompleted():
                return entry, str(res.getErrorMessage()) or 'did not complete'
            with open(os.path.join(decomp_dir, stems[entry] + '.c'), 'w') as f:
                f.write(str(res.getDecompiledFunction().getC()))
            return entry, None
        except Exception as e:  # a Java exception, recorded per function
            return entry, '%s: %s' % (type(e).__name__, e)

    errors, done = {}, 0
    start = last = time.time()
    try:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            for future in as_completed([pool.submit(one, e) for e in entries]):
                entry, error = future.result()
                done += 1
                if error is not None:
                    errors[entry] = error
                now = time.time()
                if now - last >= 30 or done == len(entries):
                    rate = done / max(now - start, 1e-9)
                    log('decompiled %d/%d, %.1f/s, %d failed, about %.0f s left'
                        % (done, len(entries), rate, len(errors), (len(entries) - done) / rate))
                    last = now
    finally:
        for ifc in opened:
            ifc.dispose()
    return errors


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', required=True, help='dump directory, e.g. out/ghidra/dt2-1.15C-emac')
    p.add_argument('--project', default=DEFAULT_PROJECT)
    p.add_argument('--project-name', default=DEFAULT_PROJECT_NAME)
    p.add_argument('--program', default=DEFAULT_PROGRAM)
    p.add_argument('--threads', type=int, default=os.cpu_count() or 4)
    p.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT, help='seconds per function')
    p.add_argument('--rtti', help='tools/coldfire/rttiscan.py JSON for the typeinfos and vtables tables')
    p.add_argument('--image', help='the image file imported into Ghidra, to check its SHA-256 '
                   '(default: sections/ plus the program path)')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--only-functions', nargs='+', metavar='ADDR',
                   help='decompile only the functions containing these addresses')
    g.add_argument('--limit', type=int, help='decompile only the first N functions')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    out = os.path.abspath(args.out)
    partial = bool(args.only_functions)
    prior = read_prior(out, partial)
    rtti = None
    if args.rtti:
        with open(args.rtti) as f:
            rtti = json.load(f)
    program_path = args.program if args.program.startswith('/') else '/' + args.program
    if args.image and not os.path.exists(args.image):
        raise SystemExit('no image at %s' % args.image)
    decomp_dir = os.path.join(out, 'decomp')
    manifest_path = os.path.join(out, 'manifest.json')

    os.environ.setdefault('GHIDRA_INSTALL_DIR', DEFAULT_GHIDRA)
    import pyghidra
    pyghidra.start(verbose=False)
    project = pyghidra.open_project(args.project, args.project_name, create=False)
    try:
        with pyghidra.program_context(project, program_path) as program:
            manifest = describe(program, args, program_path, rtti)
            errors = {}
            if partial:
                if (prior.get('program'), prior.get('image_sha256')) != (program_path, manifest['image_sha256']):
                    raise SystemExit('%s holds a dump of %s (%s), not this program'
                                     % (out, prior.get('program'), prior.get('image_sha256')))
                entries = containing_entries(program, args.only_functions)
                errors = {int(f['entry'], 16): f['error'] for f in prior.get('decompile_failures', [])}
            os.makedirs(out, exist_ok=True)
            clear_out(out, partial)
            write_json(manifest_path, dict(manifest, complete=False))

            t = time.time()
            funcs, targets = collect(program, os.path.join(out, 'disasm'))
            strings, symbols, blocks = collect_tables(program)
            manifest['collect_seconds'] = round(time.time() - t, 1)
            log('collected %d functions in %.0f s' % (len(funcs), manifest['collect_seconds']))

            if partial:
                selected = set(entries)
                for e in selected:
                    errors.pop(e, None)
                for e, stem in existing_decomp(decomp_dir).items():
                    if e in selected or e not in funcs:
                        os.remove(os.path.join(decomp_dir, stem + '.c'))
            else:
                entries = sorted(funcs)[:args.limit] if args.limit else sorted(funcs)
            t = time.time()
            run_errors = decompile(program, {e: funcs[e]['stem'] for e in entries}, entries,
                                   decomp_dir, args.threads, args.timeout)
            seconds = time.time() - t
            errors.update(run_errors)
    finally:
        project.close()

    calls, data_refs = build_rows(funcs, targets)
    errors = {e: msg for e, msg in errors.items() if e in funcs}
    status = decomp_status(funcs, existing_decomp(decomp_dir), errors)
    write_jsonl(os.path.join(out, 'functions.jsonl'),
                function_records(funcs, calls, data_refs, targets, status))
    write_sqlite(os.path.join(out, 'xrefs.sqlite'), funcs, status, calls, data_refs,
                 strings, symbols, blocks, rtti)
    manifest.update({
        'function_count': len(funcs),
        'instruction_refs': sum(len(f['refs']) for f in funcs.values()),
        'call_count': len(calls), 'data_ref_count': len(data_refs),
        'string_count': len(strings), 'symbol_count': len(symbols),
        'decompile': {'mode': 'only-functions' if partial else ('limit' if args.limit else 'full'),
                      'threads': args.threads, 'timeout': args.timeout,
                      'selected': len(entries), 'failed': len(run_errors),
                      'seconds': round(seconds, 1),
                      'per_second': round(len(entries) / max(seconds, 1e-9), 2)},
        'decompiled_files': sum(1 for s in status.values() if s['path']),
        'stale_files': sum(1 for s in status.values() if s['stale']),
        'decompile_failures': [{'entry': hx(e), 'name': funcs[e]['name'], 'error': errors[e]}
                               for e in sorted(errors)],
        'partial_updates': (prior.get('partial_updates', []) + [
            {'generated': utc_now(), 'entries': [hx(e) for e in entries]}] if partial else []),
        'complete': True,
    })
    write_json(manifest_path, manifest)
    log('%d functions, %d decompiled this run in %.0f s (%d failed), dump in %s'
        % (len(funcs), len(entries), seconds, len(run_errors), out))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
