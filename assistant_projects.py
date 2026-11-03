"""Host-owned logical projects. No model-supplied paths or executor URLs."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from contextlib import contextmanager

from agent_runtime import AgentError, required


class Projects:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.path = self.root / 'projects.db'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS bindings(name TEXT PRIMARY KEY,workspace TEXT UNIQUE,
                  device INTEGER,inode INTEGER,tool_target TEXT,origin_cwd TEXT);
                CREATE TABLE IF NOT EXISTS selections(sender TEXT,channel TEXT,project TEXT,
                  PRIMARY KEY(sender,channel));
                CREATE TABLE IF NOT EXISTS controls(sender TEXT,channel TEXT,delivery TEXT,digest TEXT,response TEXT,
                  PRIMARY KEY(sender,channel,delivery));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, name, origin_cwd='', tool_target='unavailable'):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            raise AgentError('INVALID_CONFIG', '逻辑项目名无效')
        # Tool targets are enumerated host roles, not endpoints or paths.
        if tool_target not in ('unavailable', 'mac', 'source_snapshot'):
            raise AgentError('INVALID_CONFIG', '工具目标未审核')
        with self.lock, self.connect() as db:
            old = db.execute('SELECT * FROM bindings WHERE name=?', (name,)).fetchone()
            if old:
                if old['origin_cwd'] != origin_cwd or old['tool_target'] != tool_target:
                    raise AgentError('PROJECT_CHANGED', '既有绑定不可隐式替换')
                return
            workspace = self.root / 'workspaces' / name
            workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
            info = workspace.stat()
            db.execute('INSERT INTO bindings VALUES (?,?,?,?,?,?)',
                       (name, str(workspace), info.st_dev, info.st_ino, tool_target, origin_cwd))

    def workspace(self, name):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT * FROM bindings WHERE name=?', (name,)).fetchone()
        if row is None:
            raise AgentError('NOT_FOUND', '服务器项目不存在')
        path = Path(row['workspace'])
        info = path.stat()
        if path.is_symlink() or str(path.resolve()) != row['workspace'] or (info.st_dev, info.st_ino) != (row['device'], row['inode']):
            raise AgentError('PROJECT_CHANGED', '服务器工作空间身份已改变')
        return str(path)

    def current(self, sender, channel):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT project FROM selections WHERE sender=? AND channel=?', (sender, channel)).fetchone()
        return row['project'] if row else 'assistant'

    def target(self, name):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT tool_target FROM bindings WHERE name=?', (name,)).fetchone()
        return row['tool_target'] if row else 'unavailable'

    def snapshot(self, name):
        if self.target(name) != 'source_snapshot':
            return None
        path = self.root / 'source-snapshot'
        if path.is_symlink() or not path.is_dir():
            raise AgentError('INVALID_CONFIG', '已绑定源码快照缺失或为链接')
        from agent_reads import FILE_BYTES, LEGACY_SCOPE, SOURCE_SCOPES
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        def read(name, limit):
            directory = os.dup(descriptor)
            try:
                parts = name.split('/')
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(fd, 'rb') as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                        raise ValueError('not bounded regular source')
                    data = source.read(limit + 1)
                    if len(data) > limit or b'\0' in data:
                        raise ValueError('not bounded text')
                    data.decode('utf-8')
                    return data
            finally:
                os.close(directory)
        try:
            manifest = json.loads(read('snapshot.json', 16384))
            if not isinstance(manifest, dict):
                raise ValueError('invalid source manifest')
            scope = manifest.get('scope', LEGACY_SCOPE)
            if (type(manifest.get('format')) is not int or manifest['format'] != 1
                    or not isinstance(scope, str) or scope not in SOURCE_SCOPES
                    or not isinstance(manifest.get('files'), dict)
                    or set(manifest['files']) != SOURCE_SCOPES[scope]
                    or not re.fullmatch('[0-9a-f]{40}', manifest['commit'])
                    or type(manifest['created']) not in (int, float) or not math.isfinite(manifest['created'])
                    or manifest['created'] <= 0
                    or manifest.get('evidence_location') != 'github_source_snapshot'
                    or manifest.get('runtime_or_mac_state') is not False):
                raise ValueError('invalid source manifest')
            for file, digest in manifest['files'].items():
                if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest) or hashlib.sha256(read(file, FILE_BYTES)).hexdigest() != digest:
                    raise ValueError('source hash mismatch')
            manifest['scope'] = scope
        except (OSError, ValueError, TypeError, KeyError):
            raise AgentError('PROJECT_CHANGED', '源码快照范围或校验失败') from None
        finally:
            os.close(descriptor)
        return str(path), manifest

    def listing(self, sender, channel):
        with self.lock, self.connect() as db:
            rows = db.execute('SELECT name,tool_target FROM bindings ORDER BY name').fetchall()
        return {'ok': True, 'current': self.current(sender, channel), 'projects': [dict(row) for row in rows]}

    def use(self, sender, channel, name, delivery):
        required(delivery, 128)
        self.workspace(name)
        digest = hashlib.sha256(json.dumps(['agent.use', name]).encode()).hexdigest()
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM controls WHERE sender=? AND channel=? AND delivery=?',
                             (sender, channel, delivery)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise AgentError('DELIVERY_CONFLICT', '项目切换投递已绑定其他操作')
                return {**json.loads(old['response']), 'reused': True}
            result = {'ok': True, 'current': name}
            db.execute('INSERT OR REPLACE INTO selections VALUES (?,?,?)', (sender, channel, name))
            db.execute('INSERT INTO controls VALUES (?,?,?,?,?)', (sender, channel, delivery, digest, json.dumps(result)))
        return result
