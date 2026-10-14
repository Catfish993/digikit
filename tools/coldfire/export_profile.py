# fmt: off
"""Export a resolved symbol profile to JSON, so Ghidra and anything else that
needs firmware addresses can reuse what emu/symbols.py already worked out
instead of re-deriving or re-hardcoding it.

    uv run python -m tools.coldfire.export_profile <image> --device LABEL --out FILE.json
                                           [--load-addr 0x40000400] [--syx FILE]

`image` is either a firmware `.syx` (extracted the same way emu.run does, via
emu.extract.extract -- this file does NOT reimplement depacking) or an
already-extracted `section_3_MAIN_OS.bin`-style image, read directly. In the
second case the source `.syx` sha256 is recovered from the sibling
`.source-sha256` marker emu.extract.record_source leaves next to it (see
emu/extract.py), or pass --syx to hash a specific file, or --syx-sha256 to
supply it literally. None of these change what gets resolved; they only
affect what provenance the JSON records.

Output JSON shape:

    {
      "device": "...",
      "source_syx_sha256": "..." | null,
      "main_os_md5": "...",
      "load_addr": "0x40000400",
      "resolved_count": N,
      "unresolved_count": M,
      "unresolved": ["name", ...],
      "resolution_error": "..." | null,
      "symbols": {
        "entry":             {"required": true,  "address": "0x400004e8"},
        "task_create_sites":  {"required": false, "count": 16,
                                "addresses": ["0x...", ...]},
        "panel_diff":         {"required": false, "address": null,
                                "detail": "why it failed"}
      }
    }

A scalar symbol resolves to {"address": "0x...."}. A tuple-valued symbol
(Xrefs, ScanAll, OperandGroup) resolves to {"count": N, "addresses": [...]}.
An unresolved symbol always has "address": null (or "addresses": [] for a
tuple-valued rule) plus a "detail" explaining why, whether or not it was
required -- this is what apply-profile.py and anything else downstream reads
to know what it can and cannot rely on.
"""
import argparse
import hashlib
import json
import os
import sys

from emu import extract, symbols

SOURCE_MARKER = extract.SOURCE_MARKER  # '.source-sha256'


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def md5_bytes(data):
    return hashlib.md5(data).hexdigest()


def load_main_os(image_path, syx_override=None, syx_sha256_override=None):
    """-> (main_os_bytes, source_syx_sha256_or_None).

    Reuses emu.extract.extract when given a `.syx`; reads a pre-extracted section_3
    directly otherwise. Never reimplements the depack/checksum logic itself.
    """
    if syx_sha256_override:
        forced_sha = syx_sha256_override
    elif syx_override:
        forced_sha = sha256_file(syx_override)
    else:
        forced_sha = None

    if image_path.lower().endswith('.syx'):
        syx_sha = forced_sha or sha256_file(image_path)
        import tempfile
        outdir = tempfile.mkdtemp(prefix='export-profile-')
        written = extract.extract(image_path, outdir)  # calls the real pipeline
        for sid, _kind, path, _n, _dest in written:
            if sid == 3:
                with open(path, 'rb') as fh:
                    return fh.read(), syx_sha
        raise SystemExit('no section 3 (MAIN OS) found in %s' % image_path)

    # Already-extracted image: read as-is, recover provenance from the
    # sibling marker emu.extract.record_source wrote when it was extracted.
    with open(image_path, 'rb') as fh:
        img = fh.read()
    syx_sha = forced_sha
    if syx_sha is None:
        marker = os.path.join(os.path.dirname(os.path.abspath(image_path)), SOURCE_MARKER)
        if os.path.exists(marker):
            with open(marker) as fh:
                syx_sha = fh.read().strip()
    return img, syx_sha


def symbol_entry(name, required, value, detail):
    if isinstance(value, tuple):
        return {
            'required': required,
            'count': len(value),
            'addresses': ['0x%08x' % v for v in value],
        }
    entry = {'required': required, 'address': None if value is None else '0x%08x' % value}
    if value is None and detail:
        entry['detail'] = detail
    return entry


def build_profile_json(device, img, load_addr, syx_sha256):
    """Resolve every symbol and shape the JSON. Prefers emu.symbols.resolve()
    (the real entry point); if a REQUIRED symbol is unresolved that raises,
    so this falls back to running the same per-rule loop resolve() uses
    internally (nothing firmware-specific, just orchestration already public
    in symbols.py) so the report is still complete rather than aborting.
    """
    resolution_error = None
    try:
        profile = symbols.resolve(img, load_addr=load_addr)
        got, detail = dict(profile._values), dict(profile._detail)
    except symbols.SymbolResolutionError as e:
        resolution_error = str(e)
        got, detail = {}, {}
        for name, rule, _required in symbols.SYMBOLS:
            val, why = rule.resolve(img, load_addr, got)
            got[name] = val
            detail[name] = why

    sym_out = {}
    resolved_count = 0
    unresolved = []
    for name, _rule, required in symbols.SYMBOLS:
        val = got.get(name)
        sym_out[name] = symbol_entry(name, required, val, detail.get(name))
        if val is None:
            unresolved.append(name)
        else:
            resolved_count += 1

    return {
        'device': device,
        'source_syx_sha256': syx_sha256,
        'main_os_md5': md5_bytes(img),
        'load_addr': '0x%08x' % load_addr,
        'resolved_count': resolved_count,
        'unresolved_count': len(unresolved),
        'unresolved': unresolved,
        'resolution_error': resolution_error,
        'symbols': sym_out,
    }


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image', help='.syx firmware or an extracted section_3 MAIN OS .bin')
    ap.add_argument('--device', required=True, help='device label, e.g. "Digitakt II 1.15C"')
    ap.add_argument('--out', required=True, help='output JSON path')
    ap.add_argument('--load-addr', default='0x40000400', help='default 0x40000400')
    ap.add_argument('--syx', help='hash this .syx for provenance instead of the marker/image arg')
    ap.add_argument('--syx-sha256', help='supply the source .syx sha256 literally')
    args = ap.parse_args(argv)

    load_addr = int(args.load_addr, 16)
    img, syx_sha256 = load_main_os(args.image, args.syx, args.syx_sha256)
    print('%s: %d-byte MAIN OS image, load=0x%08x' % (args.device, len(img), load_addr))

    result = build_profile_json(args.device, img, load_addr, syx_sha256)

    with open(args.out, 'w') as fh:
        json.dump(result, fh, indent=2, sort_keys=False)
        fh.write('\n')

    print('%d/%d symbols resolved -> %s'
          % (result['resolved_count'], result['resolved_count'] + result['unresolved_count'], args.out))
    if result['unresolved']:
        print('unresolved: %s' % ', '.join(result['unresolved']))
    if result['resolution_error']:
        print('REQUIRED symbol resolution failed:\n%s' % result['resolution_error'])
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
