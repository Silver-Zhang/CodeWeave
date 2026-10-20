"""Saved input vectors. Every analyzer mount comes from a verified private copy."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from codeweave.index import IndexError, _json, _sha256

MAX_FILES = 50_000
MAX_BYTES = 1_073_741_824
ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}


def read_regular(path: Path, maximum: int = MAX_BYTES) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_size > maximum:
            raise IndexError('unsupported_input', 'input must be a bounded regular file')
        with os.fdopen(os.dup(fd), 'rb') as f:
            content = f.read(maximum + 1)
        after = os.fstat(fd)
        if len(content) > maximum or (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise IndexError('workspace_changed_during_capture', 'file changed while being captured')
        return content
    finally:
        os.close(fd)


def inventory(root: Path, destination: Path | None = None, *, aliases: list[Path] | None = None,
              excluded: set[str] | None = None, sealed: bool = False) -> list[dict]:
    """Walk pinned directory descriptors; never open a project symlink/device/FIFO."""
    result: list[dict] = []
    total = 0
    excluded = excluded or set()
    if destination:
        destination.mkdir(parents=True, exist_ok=False)

    def walk(fd: int, relative: Path) -> None:
        nonlocal total
        for entry in sorted(os.scandir(fd), key=lambda x: x.name):
            if not relative.parts and entry.name in excluded:
                continue
            try:
                entry.name.encode('utf-8', 'strict')
            except UnicodeError as error:
                raise IndexError('unsupported_path', 'input names must be UTF-8') from error
            path = relative / entry.name
            info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            kind = stat.S_IFMT(info.st_mode)
            item = {'path': path.as_posix(), 'mode': stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                if aliases is None and not sealed:
                    raise IndexError('unsupported_snapshot_link', f'project/dependency symlink: {path}')
                target = os.readlink(entry.name, dir_fd=fd)
                physical = Path(target)
                if not sealed:
                    try:
                        physical = (root / path).resolve(strict=True)
                    except (OSError, RuntimeError) as error:
                        raise IndexError('unsupported_snapshot_link', f'broken/cyclic toolchain alias: {path}') from error
                    if not any(physical.is_relative_to(allowed) for allowed in aliases):
                        raise IndexError('unsupported_snapshot_link', f'toolchain alias requires an explicit authorized root: {root/path}')
                # Resolve trusted aliases into the sealed namespace, never through
                # host /etc/alternatives or another live intermediate symlink.
                item.update(kind='link', target=str(physical), original_target=target)
                if destination:
                    (destination/path).symlink_to(physical)
            elif stat.S_ISDIR(info.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    item.update(kind='directory')
                    if destination:
                        (destination/path).mkdir()
                    walk(child, path)
                    if destination:
                        (destination/path).chmod(stat.S_IMODE(info.st_mode))
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                file = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    first = os.fstat(file)
                    if not stat.S_ISREG(first.st_mode) or first.st_ino != info.st_ino:
                        raise IndexError('workspace_changed_during_capture', 'input type/identity changed')
                    total += first.st_size
                    if total > MAX_BYTES or len(result) >= MAX_FILES:
                        raise IndexError('capture_limit', 'capture exceeds 1 GiB / 50000 entries per root')
                    with os.fdopen(os.dup(file), 'rb') as stream:
                        content = stream.read(MAX_BYTES + 1)
                    last = os.fstat(file)
                    if len(content) != first.st_size or (first.st_ino, first.st_size, first.st_mtime_ns, first.st_ctime_ns) != (last.st_ino, last.st_size, last.st_mtime_ns, last.st_ctime_ns):
                        raise IndexError('workspace_changed_during_capture', 'file changed during copy')
                    item.update(kind='file', bytes=len(content), sha256=_sha256(content), mtime_ns=first.st_mtime_ns)
                    if destination:
                        output = destination/path
                        output.write_bytes(content)
                        output.chmod(stat.S_IMODE(first.st_mode))
                        os.utime(output, ns=(first.st_atime_ns, first.st_mtime_ns))
                finally:
                    os.close(file)
            else:
                raise IndexError('unsupported_input', f'nonregular input: {path}')
            current = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            if (current.st_ino, stat.S_IFMT(current.st_mode)) != (info.st_ino, kind):
                raise IndexError('workspace_changed_during_capture', 'namespace changed during capture')
            result.append(item)
            if len(result) > MAX_FILES:
                raise IndexError('capture_limit', 'too many input entries')

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(fd, Path())
    finally:
        os.close(fd)
    return sorted(result, key=lambda x: x['path'])


def verify_copy(destination: Path, entries: list[dict]) -> None:
    # Verify the entire namespace, including absence claims and aliases. Do not
    # resolve sealed absolute aliases against today's host filesystem.
    def normalized(rows):
        return [{k:v for k,v in row.items() if k != 'original_target'} for row in rows]
    if normalized(inventory(destination, sealed=True)) != normalized(entries):
        raise IndexError('sealed_input_changed', 'sealed namespace, bytes or metadata changed')


def header_configuration(resource: Path, extra: list[Path]) -> tuple[list[Path], list[Path]]:
    """Ask the installed trusted system driver about defaults, using empty owned input."""
    compiler = Path('/usr/bin/c++')
    if not compiler.is_file():
        raise IndexError('toolchain_unavailable', 'install a C++ toolchain or provide standard header roots')
    result = subprocess.run([str(compiler), '-E', '-x', 'c++', '-', '-v'], input='', text=True,
                            env=ENV, capture_output=True, timeout=15, cwd='/')
    if result.returncode:
        raise IndexError('toolchain_unavailable', 'cannot discover system C++ headers')
    match = re.search(r'#include <\.\.\.> search starts here:\n(.*?)End of search list\.', result.stderr, re.S)
    if not match:
        raise IndexError('toolchain_unavailable', 'driver did not report its standard include roots')
    search = [Path(line.strip()).resolve(strict=True) for line in match[1].splitlines() if line.strip()]
    search = list(dict.fromkeys([resource/'include', *search, *extra]))
    # /usr/include is a common parent of native C++/libc roots. Do not capture /usr itself.
    candidates = [*search, resource, *extra]
    roots = []
    for path in sorted(set(candidates), key=lambda p: len(p.parts)):
        if path in {Path('/'), Path('/usr'), Path('/lib'), Path('/lib64')}:
            raise IndexError('invalid_toolchain_root', 'authorize concrete header directories')
        if not any(path.is_relative_to(parent) for parent in roots):
            roots.append(path)
    # Installed standard-header aliases can point at concrete package include
    # directories under /usr/lib. Authorize only those concrete header trees,
    # never /usr, arbitrary user directories, executables, or /etc.
    for _ in range(16):
        additions = []
        for root in roots:
            for path in root.rglob('*'):
                if not path.is_symlink():
                    continue
                physical = path.resolve(strict=True)
                if any(physical.is_relative_to(r) for r in roots):
                    continue
                if not physical.is_relative_to('/usr') or 'include' not in physical.parts:
                    raise IndexError('unsupported_snapshot_link', f'authorize the toolchain alias target explicitly: {path}')
                target = physical if physical.is_dir() else physical.parent
                additions.append(target)
        if not additions:
            break
        roots = list(dict.fromkeys([*roots, *additions]))
    else:
        raise IndexError('capture_limit', 'too many toolchain alias roots')
    return search, roots


def runtime_files(analyzer: Path) -> list[Path]:
    """Analyzer is a trusted CodeWeave ELF, never a compiler command from a project."""
    if read_regular(analyzer, 268_435_456)[:4] != b'\x7fELF':
        raise IndexError('analyzer_unavailable', 'workspace analyzer must be a trusted built ELF binary')
    result = subprocess.run(['/usr/bin/ldd', str(analyzer)], env=ENV, cwd='/', capture_output=True, text=True, timeout=15)
    if result.returncode or 'not found' in result.stdout:
        raise IndexError('analyzer_unavailable', 'analyzer runtime libraries are missing')
    paths = [analyzer]
    for line in result.stdout.splitlines():
        found = re.findall(r'(/\S+)', line)
        if found:
            paths.append(Path(found[0]))
    return list(dict.fromkeys(paths))


def capture(roots: list[tuple[Path, bool]], stage: Path, excluded: set[str]) -> tuple[list[dict], list[tuple[Path, Path]]]:
    manifests, mounts = [], []
    physical = [root for root, _ in roots]
    for n, (root, trusted_aliases) in enumerate(roots):
        if any(root != other and (root.is_relative_to(other) or other.is_relative_to(root)) for other in physical):
            raise IndexError('overlapping_snapshot_roots', 'project/dependency/header roots must not overlap')
        destination = stage/f'root-{n}'
        entries = inventory(root, destination, aliases=physical if trusted_aliases else None,
                            excluded=excluded if n == 0 else set())
        if inventory(root, aliases=physical if trusted_aliases else None, excluded=excluded if n == 0 else set()) != entries:
            raise IndexError('workspace_changed_during_capture', 'input namespace/bytes changed while copying')
        verify_copy(destination, entries)
        manifests.append({'root': str(root), 'entries': entries})
        mounts.append((destination, root))
    return manifests, mounts


def sandbox(analyzer: Path, mounts: list[tuple[Path, Path]], runtime: list[tuple[Path, Path]],
            ccdb: Path, output: Path, project: Path, directories: set[Path]) -> list[str]:
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise IndexError('sandbox_unavailable', 'Bubblewrap is required; no unisolated fallback')
    argv = [bwrap, '--die-with-parent', '--unshare-all', '--new-session', '--clearenv',
            '--setenv', 'PATH', '/usr/bin:/bin', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    for source, target in [*runtime, *mounts, (ccdb, ccdb)]:
        argv += ['--ro-bind', str(source), str(target)]
    for path in sorted(directories):
        if not any(path.is_relative_to(target) for _, target in mounts):
            argv += ['--dir', str(path)]
    argv += ['--bind', str(output), str(output), '--chdir', str(project)]
    return argv + [str(analyzer), '--fact-protocol', '3', '--compilation-database', str(ccdb),
                   '--output', str(output/'facts.jsonl'), '--project-root', str(project)]
