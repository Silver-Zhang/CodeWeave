"""Read raw Git objects through a private configuration-free Git directory."""
import os
from pathlib import Path
import stat
import subprocess

from codeweave.index import IndexError, _sha256, _json
from codeweave.snapshot import ENV, MAX_BYTES, MAX_FILES, inventory, read_regular


def ref_identity(project: Path) -> str:
    directory=project/'.git'
    if directory.is_symlink() or not directory.is_dir():
        raise IndexError('unsupported_git_layout','plain Git directory required')
    rows={name:_sha256(read_regular(directory/name,67_108_864)) if (directory/name).exists() else None for name in ('HEAD','packed-refs','index')}
    rows['refs']=inventory(directory/'refs') if (directory/'refs').is_dir() else []
    return _sha256(_json(rows).encode())


def collect(project: Path, stage: Path) -> dict:
    stage.mkdir(parents=True, exist_ok=False)
    gitdir = project/'.git'
    if not gitdir.exists():
        return {'base_git_head': None, 'tree': {}, 'staged': [], 'limitation': 'not a plain Git worktree'}
    if gitdir.is_symlink() or not gitdir.is_dir():
        raise IndexError('unsupported_git_layout', 'Git worktree links require explicit separate authorization')
    identity=ref_identity(project)
    objects = gitdir/'objects'
    if not objects.is_dir() or objects.is_symlink() or (objects/'info/alternates').exists():
        raise IndexError('unsupported_git_layout', 'alternate/linked Git object stores are not authorized')
    # Git never receives the imported config, hooks, filters, attributes or index
    # as a worktree. Its immutable objects are checked for link escapes first.
    for current, dirs, files in os.walk(objects, followlinks=False):
        for name in [*dirs, *files]:
            p = Path(current)/name
            if p.is_symlink():
                raise IndexError('unsupported_git_layout', 'symlink in Git object store')
    stub = stage/'git'
    stub.mkdir()
    (stub/'objects').mkdir()
    (stub/'HEAD').write_bytes(read_regular(gitdir/'HEAD', 1024))
    (stub/'config').write_text('[core]\nrepositoryformatversion = 0\nbare = true\n')
    if (gitdir/'refs').is_dir():
        inventory(gitdir/'refs', stub/'refs')
    else:
        (stub/'refs').mkdir()
    if (gitdir/'packed-refs').exists():
        (stub/'packed-refs').write_bytes(read_regular(gitdir/'packed-refs', 16_777_216))
    # Detect object format from captured refs, without evaluating imported Git
    # configuration (which may contain includes, commands and filters).
    head_ref = (stub/'HEAD').read_text().strip()
    if head_ref.startswith('ref: '):
        ref = head_ref[5:]
        if not ref.startswith('refs/') or '..' in Path(ref).parts:
            raise IndexError('unsupported_git_layout','unsafe HEAD reference')
        candidate = stub/ref
        if candidate.is_file():
            head_ref = read_regular(candidate,1024).decode().strip()
        elif (stub/'packed-refs').exists():
            head_ref = next((line.split()[0] for line in (stub/'packed-refs').read_text().splitlines() if line.endswith(' '+ref)), '')
    if len(head_ref)==64 and all(c in '0123456789abcdef' for c in head_ref):
        (stub/'config').write_text('[core]\nrepositoryformatversion = 1\nbare = true\n[extensions]\nobjectFormat = sha256\n')
    env = {**ENV, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_OBJECT_DIRECTORY': str(objects)}

    def git(*args: str) -> bytes:
        p = subprocess.run(['/usr/bin/git', '--git-dir', str(stub), '-c', 'core.fsmonitor=false', *args],
                           cwd=stage, env=env, capture_output=True, timeout=30)
        if p.returncode:
            raise IndexError('head_unavailable', 'Git objects are unavailable or invalid; no project command was run')
        return p.stdout

    try:
        head = git('rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
    except IndexError:
        return {'base_git_head': None, 'tree': {}, 'staged': [], 'limitation': 'HEAD is unborn or unavailable'}
    if len(head) == 64:
        (stub/'config').write_text('[core]\nrepositoryformatversion = 1\nbare = true\n[extensions]\nobjectFormat = sha256\n')
    tree = {}
    total = 0
    for row in git('ls-tree', '-r', '-z', head).split(b'\0'):
        if not row:
            continue
        desc, raw_name = row.split(b'\t', 1)
        mode, kind, oid = desc.decode('ascii').split()
        name = raw_name.decode('utf-8', 'strict')
        if Path(name).is_absolute() or '..' in Path(name).parts or name.startswith('.git/'):
            raise IndexError('unsupported_git_path', 'unsafe Git tree path')
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise IndexError('unsupported_git_layout', 'HEAD symlinks/submodules are not supported')
        size = int(git('cat-file', '-s', oid))
        total += size
        if total > MAX_BYTES or len(tree) >= MAX_FILES:
            raise IndexError('capture_limit', 'HEAD tree exceeds capture quota')
        content = git('cat-file', 'blob', oid)
        if content.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
            raise IndexError('head_unavailable', 'HEAD requires materialized historical LFS input')
        tree[name] = {'content': content, 'sha256': _sha256(content), 'mode': int(mode[-3:], 8), 'oid': oid}
    staged = [];index_entries={}
    if (gitdir/'index').exists():
        indexed = set()
        (stub/'index').write_bytes(read_regular(gitdir/'index', 67_108_864))
        for row in git('ls-files', '--stage', '-z').split(b'\0'):
            if not row:
                continue
            desc, name = row.split(b'\t', 1)
            mode, oid, level = desc.decode().split()
            path = name.decode('utf-8', 'strict')
            indexed.add(path)
            index_entries[path]={'oid':oid,'mode':int(mode[-3:],8),'stage':level}
            if level != '0' or path not in tree or tree[path]['oid'] != oid or tree[path]['mode'] != int(mode[-3:], 8):
                staged.append(path)
        staged.extend(set(tree)-indexed)
    if ref_identity(project)!=identity:
        raise IndexError('workspace_changed_during_capture','Git refs/index changed while capturing raw HEAD')
    return {'base_git_head': head, 'tree': tree, 'staged': sorted(staged), 'index_entries':index_entries,
            'ref_identity':identity,'limitation': None}


def export(tree: dict, destination: Path) -> None:
    destination.mkdir()
    for name, item in tree.items():
        path = destination/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item['content'])
        path.chmod(item['mode'])
