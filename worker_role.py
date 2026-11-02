"""Persist the explicit executor-only role across ordinary worker restarts."""
import os
from pathlib import Path
import stat


def role(root):
    requested = os.environ.get('AGENTBRIDGE_WORKER_ROLE', '')
    if requested not in ('', 'combined', 'executor_only'):
        raise ValueError('invalid worker role')
    path = Path(root) / 'worker-role'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if requested != 'executor_only':
            return 'combined'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(b'executor_only\n')
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return 'executor_only'
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or source.read(64) != b'executor_only\n'):
            raise ValueError('invalid persisted worker role')
    if requested == 'combined':
        raise ValueError('explicit migration rollback required to restore chat writer')
    return 'executor_only'
