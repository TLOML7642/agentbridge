"""Fixed action bodies. Called only behind native tool approval and guard."""
from contextlib import contextmanager
import hashlib
import json
import os
import selectors
import stat
import subprocess
import sys

from agent_reads import private_path


# Commands are always side-effecting for authorization, even `git status`.
# No shell, sudo, interpreters, dynamic executors or arbitrary PATH resolution.
PROGRAMS = frozenset(('/bin/echo', '/usr/bin/wc', '/usr/bin/git'))
GIT_STATUS = frozenset((('status',), ('status', '--short'), ('status', '--short', '--branch')))
INPUTS = {'create_text': {'path', 'text'}, 'make_directory': {'path'}, 'run_command': {'program', 'argv'}}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identity(info):
    return [info.st_dev, info.st_ino]


@contextmanager
def parent(root, value, private_prefixes):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
        raise ValueError('invalid target')
    target = os.path.normpath(os.path.join(root, value))
    if target == root or os.path.commonpath((root, target)) != root or private_path(target, private_prefixes):
        raise ValueError('target is not in reviewed project scope')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in os.path.relpath(os.path.dirname(target), root).split(os.sep):
            if component == '.':
                continue
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, os.path.basename(target), target
    finally:
        os.close(descriptor)


def prepare(root, tool, arguments, private_prefixes=()):
    if tool not in INPUTS or not isinstance(arguments, dict) or set(arguments) != INPUTS[tool]:
        raise ValueError('unreviewed tool or parameters')
    if any(not isinstance(value, str) or '\0' in value or len(value.encode()) > 16384 for value in arguments.values()):
        raise ValueError('invalid parameters')
    root_info = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(root_info.st_mode) or os.path.realpath(root) != root:
        raise ValueError('project is not a real directory')
    binding = {'root': identity(root_info)}
    if tool == 'run_command':
        program = arguments['program']
        if program not in PROGRAMS:
            raise ValueError('program is not in the reviewed allowlist')
        info = os.stat(program)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe program')
        argv = json.loads(arguments['argv'])
        if not isinstance(argv, list) or len(argv) > 64 or any(not isinstance(item, str) or '\0' in item or len(item) > 4096 for item in argv):
            raise ValueError('invalid command argv')
        if program == '/usr/bin/git' and tuple(argv) not in GIT_STATUS:
            raise ValueError('only reviewed git status forms are supported')
        binding['program'] = [*identity(info), info.st_size, info.st_mtime_ns]
    else:
        with parent(root, arguments['path'], private_prefixes) as (descriptor, leaf, _target):
            binding['parent'] = identity(os.fstat(descriptor))
            try:
                os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('target already exists; overwrite is not authorized')
    return {**arguments, 'binding': encode(binding)}


def execute(request):
    root, tool, arguments = request['root'], request['tool'], request['arguments']
    if tool not in INPUTS or set(arguments) != INPUTS[tool] | {'binding'}:
        raise ValueError('unreviewed action')
    binding = json.loads(arguments['binding'])
    raw = {key: value for key, value in arguments.items() if key != 'binding'}
    current = prepare(root, tool, raw, request['private_prefixes'])
    if current != arguments:
        raise ValueError('approved target or executable changed')
    if tool == 'run_command':
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if identity(os.fstat(descriptor)) != binding['root']:
                raise ValueError('project changed')
            os.fchdir(descriptor)
        finally:
            os.close(descriptor)
        # No inherited keys, auth sockets, provider env, Git identity or proxy.
        argv = json.loads(raw['argv'])
        if raw['program'] == '/usr/bin/git':
            # Repository configuration is untrusted. In particular status may
            # execute core.fsmonitor; never inherit it or recurse into submodules.
            argv = ['--no-pager', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                    '-c', 'core.untrackedCache=false', 'status', '--short', '--ignore-submodules=all',
                    *(['--branch'] if '--branch' in argv else [])]
        process = subprocess.Popen([raw['program'], *argv], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={'PATH': '/usr/bin:/bin', 'HOME': request['home'], 'GIT_CONFIG_NOSYSTEM': '1',
                                        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
                                        'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'})
        output, count = {'stdout': bytearray(), 'stderr': bytearray()}, 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                for key, _mask in selector.select():
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    retained = max(0, 8192 - count)
                    output[key.data].extend(data[:retained])
                    count += len(data)
        code = process.wait()
        return {'completion': 'process-ended', 'exit_code': code, 'output_truncated': count > 8192,
                **{key: value.decode('utf-8', 'replace') for key, value in output.items()},
                'evidence': 'process_exit_only'}
    with parent(root, raw['path'], request['private_prefixes']) as (directory, leaf, target):
        if identity(os.fstat(directory)) != binding['parent'] or identity(os.stat(root, follow_symlinks=False)) != binding['root']:
            raise ValueError('approved directory changed')
        if tool == 'make_directory':
            os.mkdir(leaf, mode=0o700, dir_fd=directory)
            descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            digest = None
        else:
            descriptor = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            data = raw['text'].encode()
            with os.fdopen(descriptor, 'w+b') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                output.seek(0)
                observed = output.read(16385)
                if observed != data:
                    raise ValueError('created bytes do not match the approved content')
                info = os.fstat(output.fileno())
            digest = hashlib.sha256(observed).hexdigest()
        os.fsync(directory)
        actual = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
        if identity(actual) != identity(info):
            raise ValueError('created target replaced before verification')
        with parent(root, raw['path'], request['private_prefixes']) as (current_parent, _leaf, _target):
            if identity(os.fstat(current_parent)) != binding['parent'] or identity(os.stat(root, follow_symlinks=False)) != binding['root']:
                raise ValueError('created target location changed before verification')
        return {'completion': 'verified', 'path': target, 'sha256': digest,
                'evidence': 'created_new_directory' if tool == 'make_directory' else 'created_new_file_fsynced',
                'device': info.st_dev, 'inode': info.st_ino}


if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.buffer.read(65537))
        value = {'ok': True, 'result': execute(payload)}
    except Exception:
        # A failure may follow an actual mutation. Never label it denied or
        # safe-to-retry: the supervisor will retain an uncertain task instead.
        value = {'ok': False, 'code': 'ACTION_OUTCOME_UNKNOWN'}
    sys.stdout.write(encode(value))
