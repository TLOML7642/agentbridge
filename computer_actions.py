"""Opt-in Mac file operations. Paths and approval policy are owned by the host."""
import hashlib
import ctypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
import plistlib
from agent_reads import DENIED


INPUTS = {
    'computer_open_project': {'project'},
    'computer_resize_image': {'source', 'destination', 'max_pixels'},
    'computer_edit_image': {'source', 'destination', 'operation', 'value'},
    'computer_trash_file': {'path'},
    'computer_run_project': {'project', 'configuration'},
    'computer_trash_files': {'paths'},
}
AUTOMATIC = frozenset(('computer_open_project', 'computer_resize_image', 'computer_edit_image'))
PROTECTED = frozenset(('Library', '.Trash', 'Applications'))
MAX_FILE = 256 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def locate(root, value, private_prefixes=(), absent=False):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise ValueError('computer root must be canonical')
    if not isinstance(value, str) or not value or '\0' in value or len(value) > 4096:
        raise ValueError('invalid path')
    path = Path(os.path.normpath(os.path.join(root, value)))
    if path == root or root not in path.parents:
        raise ValueError('path outside user files')
    parts = path.relative_to(root).parts
    if any(part.startswith('.') for part in parts) or parts[0] in PROTECTED or DENIED.search(str(path)):
        raise ValueError('protected user data')
    if any(path == Path(prefix) or Path(prefix) in path.parents for prefix in private_prefixes):
        raise ValueError('protected bridge data')
    current = root
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if absent and index == len(parts) - 1:
                return path
            raise
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('symlink paths are not allowed')
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError('invalid parent directory')
    if absent:
        raise ValueError('destination exists; overwrite is not authorized')
    return path


def file_identity(path, directory=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError('only bounded regular files without hard links are supported')
    with os.fdopen(descriptor, 'rb') as source:
        digest = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: source.read(65536), b''):
            total += len(chunk)
            if total > MAX_FILE:
                raise ValueError('source grew beyond the bounded file limit')
            digest.update(chunk)
        after = os.fstat(source.fileno())
        identity = lambda item: [item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns]
        if identity(info) != identity(after):
            raise ValueError('source changed during inspection')
        return {'stat': identity(info), 'sha256': digest.hexdigest()}


def directory_identity(path):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('not a directory')
    return [info.st_dev, info.st_ino]


@contextmanager
def parent_descriptor(root, path):
    """Pin every parent component; never follow a substituted symlink."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(path).parent.relative_to(root).parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def pinned_identity(root, path):
    with parent_descriptor(root, path) as directory:
        return file_identity(Path(path).name, directory)


@contextmanager
def pinned_directory(root, path):
    with parent_descriptor(root, path) as parent:
        directory = os.open(Path(path).name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            yield directory
        finally:
            os.close(directory)


def read_pinned(root, path, limit=1048576):
    with parent_descriptor(root, path) as directory:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink != 1:
                raise ValueError('not a bounded regular configuration')
            data = source.read(limit + 1)
            after = os.fstat(source.fileno())
            if len(data) > limit or (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('configuration changed during read')
            return data


def move_exclusive(source_fd, source_name, target_fd, target_name):
    # macOS RENAME_EXCL moves atomically and refuses collisions/other volumes.
    function = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True).renameatx_np
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source_name), target_fd, os.fsencode(target_name), 4) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, 'exclusive Trash move failed')


def projects(root, private_prefixes=()):
    project = locate(root, 'project', private_prefixes)
    result = []
    with pinned_directory(root, project) as directory:
        for name in sorted(os.listdir(directory), key=str.casefold):
            if len(result) >= 100:
                return {'projects': result, 'truncated': True}
            try:
                locate(root, str(project / name), private_prefixes)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    try:
                        info = os.stat('.idea', dir_fd=child, follow_symlinks=False)
                        ide = stat.S_ISDIR(info.st_mode)
                    except FileNotFoundError:
                        ide = False
                    result.append({'name': name, 'pycharm_project': ide})
                finally:
                    os.close(child)
            except (OSError, ValueError):
                continue
    return {'projects': result, 'truncated': False}


def journal(request, stage, body):
    path = request.get('journal')
    if path is None:
        return
    path = Path(path)
    if not any(Path(prefix) in path.parents for prefix in request['private_prefixes']):
        raise ValueError('action journal must be host-private')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with parent_descriptor('/', path) as directory:
        fd = os.open(path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'a') as output:
            info = os.fstat(output.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError('unsafe action journal')
            output.write(canonical({'stage': stage, **body}) + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.fsync(directory)


def project_path(root, name, private_prefixes):
    if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or '\\' in name:
        raise ValueError('project must be an exact folder name')
    path = locate(root, 'project/' + name, private_prefixes)
    directory_identity(path)
    return path


def configurations(root, name, private_prefixes=()):
    project = project_path(root, name, private_prefixes)
    result = []
    candidates = []
    for directory, pattern in ((project / '.idea', 'workspace.xml'),
                               (project / '.idea/runConfigurations', '*.xml'), (project / '.run', '*.run.xml')):
        if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents if parent != Path(root).parent):
            raise ValueError('symlink in project configuration')
        if directory.is_dir():
            candidates.extend(sorted(directory.glob(pattern)))
    if len(candidates) > 100:
        raise ValueError('too many project configurations')
    for file in candidates:
        identity = pinned_identity(root, file)
        if identity['stat'][2] > 1048576:
            raise ValueError('configuration is too large')
        data = read_pinned(root, file)
        if hashlib.sha256(data).hexdigest() != identity['sha256']:
            raise ValueError('configuration changed during inspection')
        if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
            raise ValueError('unsupported XML declarations')
        document = ET.fromstring(data)
        for config in document.iter('configuration'):
            config_name = config.get('name')
            if config.get('default') == 'true' or config.get('type') != 'PythonConfigurationType' or not config_name:
                continue
            if len(config_name) > 120 or any(ord(char) < 32 for char in config_name):
                raise ValueError('invalid configuration name')
            result.append({'name': config_name, 'type': 'PythonConfigurationType',
                           'file': str(file.relative_to(project)), 'file_identity': identity,
                           'content': ET.tostring(config, encoding='unicode')})
    return result


def run_binding(root, project, configuration):
    """Code contents deliberately do not form part of a persistent run grant."""
    config = ET.fromstring(configuration['content'])
    options = {item.get('name'): item.get('value', '') for item in config.findall('option')}
    def expand(value):
        value = value.replace('$PROJECT_DIR$', str(project))
        if not value or '$' in value or '\0' in value:
            raise ValueError('unresolved run path')
        return os.path.abspath(os.path.join(project, value))
    entry = expand(options.get('SCRIPT_NAME', ''))
    locate(root, entry)
    if not Path(entry).is_file() or options.get('MODULE_MODE', 'false') != 'false':
        raise ValueError('only an existing Python file entry is supported')
    cwd = expand(options.get('WORKING_DIRECTORY') or str(project))
    locate(root, cwd)
    working_identity = directory_identity(Path(cwd))
    sdk = options.get('SDK_HOME', '')
    if not sdk:
        sdk_name = options.get('SDK_NAME', '')
        if options.get('IS_MODULE_SDK', 'false') == 'true' or not sdk_name:
            misc = ET.fromstring(read_pinned(root, project / '.idea/misc.xml'))
            managers = [item for item in misc.iter('component') if item.get('name') == 'ProjectRootManager']
            if len(managers) != 1:
                raise ValueError('project SDK is ambiguous')
            sdk_name = managers[0].get('project-jdk-name', '')
        table_path = Path(root) / 'Library/Application Support/JetBrains/PyCharm2026.1/options/jdk.table.xml'
        table = ET.fromstring(read_pinned(root, table_path))
        candidates = [item for item in table.iter('jdk') if item.find('name') is not None
                      and item.find('name').get('value') == sdk_name and item.find('homePath') is not None]
        if not sdk_name or len(candidates) != 1:
            raise ValueError('actual Python interpreter cannot be determined')
        sdk = candidates[0].find('homePath').get('value', '').replace('$USER_HOME$', str(root))
    interpreter = expand(sdk)
    # A venv Python is commonly a symlink. Bind both the configured path and
    # the actual interpreter, without launching it to resolve its identity.
    actual = os.path.realpath(interpreter)
    interpreter_identity = pinned_identity('/', Path(actual))
    if not os.access(actual, os.X_OK):
        raise ValueError('run interpreter is not executable')
    return {'project_path': str(project), 'project_identity': directory_identity(project),
            'configuration': configuration['name'], 'content_sha256': hashlib.sha256(configuration['content'].encode()).hexdigest(),
            'entry': entry, 'working_directory': cwd, 'working_identity': working_identity,
            'interpreter': interpreter, 'actual_interpreter': actual, 'interpreter_identity': interpreter_identity}


def image_flags(tool, arguments):
    operation = 'resize' if tool == 'computer_resize_image' else arguments['operation']
    value = arguments['max_pixels'] if tool == 'computer_resize_image' else arguments['value']
    if operation == 'resize' and re.fullmatch(r'[1-9][0-9]{0,3}', value):
        return ['-Z', value]
    if operation == 'rotate' and value in ('90', '180', '270'):
        return ['-r', value]
    if operation == 'crop' and re.fullmatch(r'[1-9][0-9]{0,3}x[1-9][0-9]{0,3}', value):
        return ['--cropToHeightWidth', *value.split('x')]
    raise ValueError('unsupported image operation or dimensions')


def inspect(root, operation, value, private_prefixes=()):
    if operation == 'projects' and value == '':
        return projects(root, private_prefixes)
    if operation == 'configurations':
        return {'project': value, 'configurations': [{'name': item['name'], 'type': item['type']}
                for item in configurations(root, value, private_prefixes)],
                'execution': 'existing_selected_pycharm_configuration_only'}
    if operation != 'folder':
        raise ValueError('unsupported computer inspection')
    path = locate(root, value, private_prefixes)
    entries = []
    truncated = False
    with pinned_directory(root, path) as directory:
        for name in sorted(os.listdir(directory), key=str.casefold):
            try:
                locate(root, str(path / name), private_prefixes)
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    continue
                if len(entries) == 100:
                    truncated = True
                    break
                entries.append({'name': name, 'kind': 'folder' if stat.S_ISDIR(info.st_mode) else 'file',
                                'bytes': info.st_size if stat.S_ISREG(info.st_mode) else None})
            except (OSError, ValueError):
                continue
    return {'path': str(path), 'entries': entries, 'truncated': truncated,
            'evidence': 'directory_listing_not_garbage_classification'}


def prepare(root, tool, arguments, private_prefixes=()):
    if tool not in INPUTS or not isinstance(arguments, dict) or set(arguments) != INPUTS[tool]:
        raise ValueError('unsupported computer action')
    if any(not isinstance(value, str) or not value or '\0' in value or len(value) > 4096 for value in arguments.values()):
        raise ValueError('invalid computer parameters')
    binding = {'root': directory_identity(Path(root))}
    if tool == 'computer_trash_files':
        paths = json.loads(arguments['paths'])
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 20 or any(not isinstance(item, str) for item in paths)
                or len(set(paths)) != len(paths) or len(arguments['paths']) > 1800):
            raise ValueError('batch must list 1..20 unique file paths in a bounded confirmation')
        items = [prepare(root, 'computer_trash_file', {'path': item}, private_prefixes) for item in paths]
        identities = [json.loads(item['binding'])['source']['stat'][:2] for item in items]
        if len(set(map(tuple, identities))) != len(items):
            raise ValueError('batch names must not alias the same file')
        if sum(json.loads(item['binding'])['source']['stat'][2] for item in items) > 256 * 1024 * 1024:
            raise ValueError('batch is too large')
        binding['items'] = items
    elif tool in ('computer_open_project', 'computer_run_project'):
        name = arguments['project']
        path = project_path(root, name, private_prefixes)
        binding['project'] = directory_identity(path)
        if tool == 'computer_run_project':
            matches = [item for item in configurations(root, name, private_prefixes) if item['name'] == arguments['configuration']]
            if len(matches) != 1:
                raise ValueError('run configuration must match exactly one existing configuration')
            binding['configuration'] = {key: value for key, value in matches[0].items() if key != 'content'}
            binding['run'] = run_binding(root, path, matches[0])
    elif tool in ('computer_resize_image', 'computer_edit_image'):
        source = locate(root, arguments['source'], private_prefixes)
        destination = locate(root, arguments['destination'], private_prefixes, absent=True)
        if source.suffix.lower() not in ('.png', '.jpg', '.jpeg') or destination.suffix.lower() != source.suffix.lower():
            raise ValueError('image copy must preserve PNG/JPEG format')
        image_flags(tool, arguments)
        binding.update(source=pinned_identity(root, source), source_parent=directory_identity(source.parent),
                       destination_parent=directory_identity(destination.parent))
    else:
        path = locate(root, arguments['path'], private_prefixes)
        binding.update(source=pinned_identity(root, path), parent=directory_identity(path.parent))
    return {**arguments, 'binding': canonical(binding)}


def execute(request):
    if sys.platform != 'darwin':
        raise ValueError('computer actions require the existing Mac executor')
    root, tool, args = request['root'], request['tool'], request['arguments']
    raw = {key: value for key, value in args.items() if key != 'binding'}
    try:
        current = prepare(root, tool, raw, request['private_prefixes'])
    except (OSError, ValueError, TypeError):
        raise ValueError('computer target cannot be revalidated') from None
    if current != args:
        raise ValueError('computer target changed after authorization')
    env = {'PATH': '/usr/bin:/bin', 'HOME': root}
    if tool == 'computer_trash_files':
        items = json.loads(args['binding'])['items']
        results = [execute({**request, 'tool': 'computer_trash_file', 'arguments': item}) for item in items]
        return {'completion': 'verified', 'evidence': 'batch_moved_to_trash_content_verified',
                'files': results, 'count': len(results), 'permanently_deleted': False}
    if tool in ('computer_open_project', 'computer_run_project'):
        project = locate(root, 'project/' + raw['project'], request['private_prefixes'])
        if tool == 'computer_run_project':
            with open('/Applications/PyCharm.app/Contents/Info.plist', 'rb') as metadata:
                version = plistlib.load(metadata)
            if (version.get('CFBundleIdentifier') != 'com.jetbrains.pycharm'
                    or version.get('CFBundleShortVersionString') != '2026.1.2'):
                raise ValueError('unreviewed PyCharm version')
        process = subprocess.run(['/usr/bin/open', '-a', 'PyCharm', str(project)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=15)
        if tool == 'computer_run_project':
            if process.returncode != 0:
                raise ValueError('PyCharm could not open the project')
            if prepare(root, tool, raw, request['private_prefixes']) != args:
                raise ValueError('run configuration changed after approval')
            # argv is data, never interpolated into AppleScript. Do not switch
            # configurations, send keyboard shortcuts, or click an arbitrary window.
            process = subprocess.run(['/usr/bin/osascript', '-e', PYCHARM_RUN,
                                      raw['project'], raw['configuration'], str(project)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=25)
            return {'completion': 'process-ended', 'exit_code': process.returncode,
                    'evidence': 'pycharm_exact_run_menu_requested' if process.returncode == 0 else 'pycharm_run_not_confirmed',
                    'project': raw['project'], 'configuration': raw['configuration'],
                    'project_running_verified': False, 'user_goal_verified': False}
        return {'completion': 'process-ended', 'exit_code': process.returncode,
                'evidence': 'application_open_request_only', 'project': raw['project'],
                'project_running_verified': False}
    if tool == 'computer_trash_file':
        source = locate(root, raw['path'], request['private_prefixes'])
        trash = Path(root) / '.Trash'
        trash.mkdir(mode=0o700, exist_ok=True)
        info = trash.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe Trash directory')
        target = trash / (source.stem[:80] + '-' + uuid.uuid4().hex + source.suffix[:20])
        original = file_identity(source)
        if original != json.loads(args['binding'])['source']:
            raise ValueError('source changed before trash move')
        with parent_descriptor(root, source) as source_fd:
            trash_fd = os.open(trash, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                binding = json.loads(args['binding'])
                if [os.fstat(source_fd).st_dev, os.fstat(source_fd).st_ino] != binding['parent']:
                    raise ValueError('source directory changed')
                if file_identity(source.name, source_fd) != binding['source']:
                    raise ValueError('source changed at the pinned parent')
                evidence = {'path': str(source), 'trash_path': str(target), 'source_identity': original,
                            'permanently_deleted': False}
                journal(request, 'trash-intent', evidence)
                move_exclusive(source_fd, source.name, trash_fd, target.name)
                os.fsync(source_fd)
                os.fsync(trash_fd)
                observed = file_identity(target.name, trash_fd)
                if observed['sha256'] != original['sha256'] or observed['stat'][:3] != original['stat'][:3]:
                    raise ValueError('trash file identity changed')
                journal(request, 'trash-verified', {**evidence, 'target_identity': observed})
            finally:
                os.close(trash_fd)
        if source.exists() or target.is_symlink() or file_identity(target)['sha256'] != original['sha256']:
            raise ValueError('trash move verification failed')
        return {'completion': 'verified', 'path': str(source), 'trash_path': str(target),
                'sha256': original['sha256'], 'evidence': 'moved_to_trash_content_verified', 'permanently_deleted': False}
    source = locate(root, raw['source'], request['private_prefixes'])
    destination = locate(root, raw['destination'], request['private_prefixes'], absent=True)
    binding = json.loads(args['binding'])
    with parent_descriptor(root, source) as src_fd, parent_descriptor(root, destination) as dst_fd:
        for descriptor, expected in ((src_fd, binding['source_parent']), (dst_fd, binding['destination_parent'])):
            info = os.fstat(descriptor)
            if [info.st_dev, info.st_ino] != expected:
                raise ValueError('image directory changed')
        temporary = '.bridge-image-' + uuid.uuid4().hex + source.suffix
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_fd)
        original = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_fd)
        with os.fdopen(descriptor, 'wb') as output, os.fdopen(original, 'rb') as input_file:
            info = os.fstat(input_file.fileno())
            if [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns] != binding['source']['stat']:
                raise ValueError('image source changed')
            while chunk := input_file.read(65536):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if file_identity(temporary, dst_fd)['sha256'] != binding['source']['sha256']:
            raise ValueError('image source copy mismatch')
        # Pin the tool's working directory too. Parent-path swaps cannot redirect
        # sips to another file or publish into a substituted directory.
        trampoline = 'import os,sys;os.fchdir(int(sys.argv[1]));os.execve("/usr/bin/sips",sys.argv[2:],os.environ)'
        process = subprocess.run([sys.executable, '-I', '-c', trampoline, str(dst_fd), '/usr/bin/sips',
                                  *image_flags(tool, raw), './' + temporary], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=30,
                                 pass_fds=(dst_fd,))
        if process.returncode != 0:
            raise ValueError('image conversion failed; private partial copy retained')
        result = file_identity(temporary, dst_fd)
        if result['stat'][2] == 0 or prepare(root, tool, raw, request['private_prefixes']) != args:
            raise ValueError('image target changed or output is empty')
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dst_fd)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(temporary, destination.name, src_dir_fd=dst_fd, dst_dir_fd=dst_fd, follow_symlinks=False)
        os.unlink(temporary, dir_fd=dst_fd)
        os.fsync(dst_fd)
        with parent_descriptor(root, destination) as current:
            info = os.fstat(current)
            if [info.st_dev, info.st_ino] != binding['destination_parent']:
                raise ValueError('image publication location changed')
        if pinned_identity(root, source) != binding['source']:
            raise ValueError('original image changed during conversion')
        return {'completion': 'verified', 'path': str(destination), 'sha256': file_identity(destination.name, dst_fd)['sha256'],
                'evidence': 'new_image_copy_original_unchanged', 'original_preserved': True}


PYCHARM_RUN = '''on run args
set projectName to item 1 of args
set configName to item 2 of args
set projectPath to item 3 of args
tell application "System Events"
    set matches to application processes whose bundle identifier is "com.jetbrains.pycharm"
    if (count matches) is not 1 then error "IDE identity ambiguous"
    set ide to item 1 of matches
    tell ide
        set frontmost to true
        repeat 40 times
            set targets to {}
            repeat with w in windows
                try
                    set documentPath to value of attribute "AXDocument" of w
                    if documentPath is projectPath or documentPath starts with (projectPath & "/") or documentPath starts with ("file://" & projectPath & "/") then set end of targets to w
                end try
            end repeat
            if (count targets) is not 0 then exit repeat
            delay 0.25
        end repeat
        if (count targets) is not 1 then error "Project window not uniquely available"
        perform action "AXRaise" of item 1 of targets
        set documentPath to value of attribute "AXDocument" of front window
        if not (documentPath starts with (projectPath & "/") or documentPath starts with ("file://" & projectPath & "/")) then error "Project document path not verified"
        set runName to "Run '" & configName & "'"
        set runItem to menu item runName of menu 1 of menu bar item "Run" of menu bar 1
        if not (exists runItem) then error "Exact run configuration not selected"
        if not enabled of runItem then error "Run action unavailable"
        click runItem
    end tell
end tell
return "run-requested"
end run'''
