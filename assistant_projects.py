"""Host-owned logical projects. No model-supplied paths or executor URLs."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
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
        manifest = json.loads((path / 'snapshot.json').read_text())
        from agent_reads import SOURCE_FILES
        if set(manifest['files']) != SOURCE_FILES:
            raise AgentError('INVALID_CONFIG', '源码快照范围不匹配')
        for file, digest in manifest['files'].items():
            target = path / file
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise AgentError('PROJECT_CHANGED', '源码快照校验失败')
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
