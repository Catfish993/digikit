"""Copy a program between Ghidra projects without opening it.

    uv run python -m tools.coldfire.ghidracopy \\
        --from ~/ghidra-projects/dt2-emac dt2-emac /section_3_MAIN_OS.bin \\
        --to ~/ghidra-projects/elektron-emac elektron-emac /dt2-1.15C/section_3_MAIN_OS.bin \\
        [--gzf out/ghidra/packed/dt2-1.15C.gzf]

Packs the source project's stored file into a .gzf with DomainFile.packFile,
then creates the destination file from it, creating the destination project
and folders as needed. It fails if the destination file exists. The program is
never opened, so the source project is not analysed or saved. A headless
`-process` script cannot do this: the script runs inside a transaction, so
packing the open program fails, and the analyzer then saves the program back.
"""

import argparse
import os
import shutil
import sys
import tempfile

DEFAULT_GHIDRA = '/opt/homebrew/Cellar/ghidra/12.1.3/libexec'


def split_path(path):
    """'/a/b/name' -> ('/a/b', 'name'); the folder of a top-level file is '/'."""
    if not path.startswith('/'):
        path = '/' + path
    folder, name = path.rsplit('/', 1)
    if not name:
        raise ValueError('no file name in %r' % path)
    return folder or '/', name


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--from', dest='src', nargs=3, required=True,
                   metavar=('PROJECT_DIR', 'NAME', 'PATH'))
    p.add_argument('--to', dest='dst', nargs=3, required=True,
                   metavar=('PROJECT_DIR', 'NAME', 'PATH'))
    p.add_argument('--gzf', help='keep the packed file here (default: a temporary file)')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    src_dir, src_name, src_path = args.src
    dst_dir, dst_name, dst_path = args.dst
    src_dir, dst_dir = os.path.expanduser(src_dir), os.path.expanduser(dst_dir)
    src_path = src_path if src_path.startswith('/') else '/' + src_path
    folder_path, file_name = split_path(dst_path)
    tmp = None
    if args.gzf:
        gzf = os.path.abspath(args.gzf)
        os.makedirs(os.path.dirname(gzf), exist_ok=True)
    else:
        tmp = tempfile.mkdtemp(prefix='ghidracopy-')
        gzf = os.path.join(tmp, file_name + '.gzf')

    os.environ.setdefault('GHIDRA_INSTALL_DIR', DEFAULT_GHIDRA)
    import pyghidra
    pyghidra.start(verbose=False)
    from java.io import File
    monitor = pyghidra.task_monitor()

    try:
        project = pyghidra.open_project(src_dir, src_name, create=False)
        try:
            source = project.getProjectData().getFile(src_path)
            if source is None:
                raise SystemExit('no %s in project %s' % (src_path, src_name))
            if os.path.exists(gzf):
                os.unlink(gzf)
            source.packFile(File(gzf), monitor)
        finally:
            project.close()
        print('packed %s:%s to %s (%d bytes)' % (src_name, src_path, gzf, os.path.getsize(gzf)))

        os.makedirs(dst_dir, exist_ok=True)
        project = pyghidra.open_project(dst_dir, dst_name, create=True)
        try:
            folder = project.getProjectData().getRootFolder()
            for part in [p for p in folder_path.split('/') if p]:
                folder = folder.getFolder(part) or folder.createFolder(part)
            if folder.getFile(file_name) is not None:
                raise SystemExit('%s:%s already exists' % (dst_name, dst_path))
            created = folder.createFile(file_name, File(gzf), monitor)
            print('created %s:%s' % (dst_name, created.getPathname()))
        finally:
            project.close()
    finally:
        if tmp:
            shutil.rmtree(tmp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
