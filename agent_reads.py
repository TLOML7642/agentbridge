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

# The first real-project pilot has runtime data beside its source. Never infer
# approval from an extension, a model-supplied path, or the project's .gitignore.
SOURCE_PROJECT = os.path.join(os.path.expanduser('~'), 'agentbridge')
SOURCE_FILES = frozenset(('README.md', 'CHANGELOG.md', 'AGENTS.md', 'SECURITY.md', 'LICENSE',
                          'worker.py', 'shim.py', 'agent_runtime.py', 'agent_driver.mjs',
                          'agent_reads.py', 'agent_approvals.py', 'agent_tasks.py', 'agent_actions.py',
                          'agent_task_driver.mjs', 'agent_task_executor.py', 'agent_rpc.mjs',
                          'agent_persona.mjs', 'native_boundary.mjs', 'server/plugins/agentbridge_mac/main.py'))
PAGE_BYTES = 16384
FILE_BYTES = 1048576


class RangeError(ValueError):
    pass


def source_allowed(candidate, directory=False):
    if os.path.commonpath((candidate, SOURCE_PROJECT)) != SOURCE_PROJECT:
        return True
    relative = os.path.relpath(candidate, SOURCE_PROJECT)
    return (relative in SOURCE_FILES or directory and
            (relative == '.' or any(name.startswith(relative + '/') for name in SOURCE_FILES)))


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_range(source, request, candidate, before):
    start, count = request.get('start_line', 1), request.get('max_lines', 100)
    if type(start) is not int or start < 1 or type(count) is not int or not 1 <= count <= 200:
        raise RangeError('start_line must be >= 1 and max_lines must be 1..200')
    expected = request.get('expected_version')
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)):
        raise RangeError('invalid file version; restart reading')
    if start > 1 and expected is None:
        raise RangeError('continuation requires expected_version from the first page; restart at line 1')
    if before.st_size > FILE_BYTES:
        raise RangeError('file exceeds 1 MiB limit')
    data = source.read(FILE_BYTES + 1)
    if len(data) > FILE_BYTES or b'\0' in data:
        raise RangeError('file exceeds 1 MiB limit or is not safe text')
    after = os.fstat(source.fileno())
    if identity(before) != identity(after):
        raise RangeError('file changed during reading; restart at line 1')
    data.decode('utf-8')  # Validate the entire bounded snapshot, not just a fragment.
    version = hashlib.sha256(json.dumps([candidate, identity(after)]).encode() + data).hexdigest()
    if expected is not None and version != expected:
        raise RangeError('file changed between pages; discard previous fragments and restart at line 1')
    lines = re.findall(rb'[^\r\n]*(?:\r\n|\r|\n|$)', data)[:-1]
    if start > len(lines) and not (not lines and start == 1):
        raise RangeError('start_line is beyond end of file')

    def render(fragment, end):
        eof = end == len(lines)
        return json.dumps(dict(text=fragment.decode('utf-8'), start_line=start, end_line=end,
                               next_line=None if eof else end + 1, eof=eof, total_lines=len(lines),
                               file_version=version, fragment_sha256=hashlib.sha256(fragment).hexdigest(),
                               bytes=len(fragment), encoding='utf-8'), ensure_ascii=False)

    fragment, end = b'', start - 1
    result = render(fragment, end)
    for line in lines[start - 1:start - 1 + count]:
        page = render(fragment + line, end + 1)
        if len(page.encode('utf-8')) > PAGE_BYTES:
            if not fragment:
                raise RangeError('single line exceeds 16 KiB response limit; no content omitted')
            break
        fragment, end, result = fragment + line, end + 1, page
    return result


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
            or request['operation'] not in ('read_text', 'read_text_range', 'list_files')):
        raise ValueError('invalid read')
    # Symlinks are deliberately unsupported, including intermediate components.
    # realpath + O_NOFOLLOW on just the leaf would leave parent-swap races.
    candidate = os.path.normpath(os.path.join(root, value))
    if not source_allowed(candidate, request['operation'] == 'list_files'):
        raise ValueError('not reviewed project source')
    if (os.path.commonpath((root, SOURCE_PROJECT)) == SOURCE_PROJECT
            and os.path.commonpath((candidate, SOURCE_PROJECT)) != SOURCE_PROJECT):
        raise ValueError('outside source pilot')
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
                    if (private_path(full, request['private_prefixes'])
                            or not source_allowed(full, directory=True)):
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
            if request['operation'] == 'read_text_range':
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('not regular text')
                return read_range(source, request, candidate, info)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError('not small regular text')
            data = source.read(16385)
            if len(data) > 16384 or b'\0' in data:
                raise ValueError('not small regular text')
            text = data.decode('utf-8')
            return json.dumps(dict(text=text, sha256=hashlib.sha256(data).hexdigest(),
                                   bytes=len(data), encoding='utf-8', unicode_codepoints=len(text),
                                   ends_with_newline=text.endswith(('\n', '\r'))), ensure_ascii=False)
    finally:
        os.close(directory)


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.buffer.read(32769))
        result = {'ok': True, 'value': query(request)}
    except RangeError as error:
        result = {'ok': False, 'error': str(error)}
    except Exception:
        # File names and OS exceptions can contain private paths or contents.
        result = {'ok': False, 'error': 'read target is outside the reviewed scope or is not safe text'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
