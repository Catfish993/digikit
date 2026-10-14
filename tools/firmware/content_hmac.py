# fmt: off
"""CLI for the container's 32-byte HMAC-SHA256 trailer.

    uv run python -m tools.content_hmac Digitakt_II_OS1.15C.syx
    uv run python -m tools.content_hmac Digitone_II_OS1.10E.syx --device dn2

See dt2/authcode.py for the format, derivation and provenance details.
"""
import argparse
import sys

from dt2.authcode import DEVICES, derive_key, split_container, compute_trailer, verify


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('syx')
    ap.add_argument('--device', choices=sorted(DEVICES), default='dt2')
    args = ap.parse_args(argv)

    total_len, trailer, computed = verify(args.syx, args.device)
    print('total_len   %d' % total_len)
    print('in file     %s' % trailer.hex())
    print('computed    %s' % computed.hex())
    print('match       %s' % (trailer == computed))
    return 0 if trailer == computed else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
