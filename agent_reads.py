"""Reviewed descriptor-relative reads. No shell and no filesystem mutations."""
import hashlib
import json
import os
import re
import stat
import sys


DENIED = re.compile(r'(?:^|/)(?:\.ssh|\.aws|\.codex|\.deepseek-harness|\.git|node_modules)(?:/|$)|'
                    r'(?:^|/)(?:\.env[^/]*|[^/]*(?:credential|secret|password)[^/]*|id_rsa[^/]*|id_ed25519[^/]*)(?:/|$)|'
                    r'\.(?:pem|key|db|sqlite|sqlite3)(?:$|/)', re.I)


def private_path(value, prefixes):
    # Default projects may be a home directory. Its hidden application state
    # and macOS Library must not become readable just because they are in cwd.
    hidden = any(part.startswith('.') for part in value.split(os.sep) if part)
    library = os.path.expanduser('~/Library')
    return (hidden or DENIED.search(value) or os.path.commonpath((value, library)) == library
            or any(os.path.commonpath((value, prefix)) == prefix for prefix in prefixes))


def query(request):
    root, value = request['root'], request['value']
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value
            or request['operation'] not in ('read_text', 'list_files')):
        raise ValueError('invalid read')
    # Symlinks are deliberately unsupported, including intermediate components.
    # realpath + O_NOFOLLOW on just the leaf would leave parent-swap races.
    candidate = os.path.normpath(os.path.join(root, value))
    if request['operation'] == 'read_text' and candidate in ('/etc/hosts', '/private/etc/hosts'):
        root = os.path.realpath('/etc')
        candidate = os.path.join(root, 'hosts')
        expected = os.stat(root)
        expected_identity = (expected.st_dev, expected.st_ino)
    else:
        expected_identity = (request['device'], request['inode'])
        if os.path.commonpath((root, candidate)) != root:
            raise ValueError('outside scope')
    if private_path(candidate, request['private_prefixes']):
        raise ValueError('private path')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity = os.fstat(directory)
        if (identity.st_dev, identity.st_ino) != expected_identity:
            raise ValueError('project replaced')
        relative = os.path.relpath(candidate, root)
        components = [] if relative == '.' else relative.split(os.sep)
        for component in components[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        leaf = components[-1] if components else '.'
        if request['operation'] == 'list_files':
            descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                rows = []
                for name in sorted(os.listdir(descriptor)):
                    full = os.path.join(candidate, name)
                    if private_path(full, request['private_prefixes']):
                        continue
                    try:
                        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    except OSError:
                        continue
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        continue
                    rows.append(dict(name=name, directory=stat.S_ISDIR(info.st_mode), bytes=info.st_size, modified=info.st_mtime))
                    if len(rows) == 100:
                        break
                return json.dumps(rows, ensure_ascii=False)
            finally:
                os.close(descriptor)
        descriptor = os.open(leaf, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError('not small regular text')
            data = source.read(16385)
            if len(data) > 16384 or b'\0' in data:
                raise ValueError('not small regular text')
            return json.dumps(dict(text=data.decode('utf-8'), sha256=hashlib.sha256(data).hexdigest()), ensure_ascii=False)
    finally:
        os.close(directory)


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.buffer.read(32769))
        result = {'ok': True, 'value': query(request)}
    except Exception:
        # File names and OS exceptions can contain private paths or contents.
        result = {'ok': False, 'error': 'read target is outside the reviewed scope or is not safe text'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
