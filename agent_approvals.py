"""Private durable approval authority. No model, HTTP listener or executor."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from agent_runtime import AgentError, required


SUBJECT_KEYS = frozenset(('sender', 'channel', 'conversation', 'session', 'project', 'cwd', 'attempt'))
REQUEST_KEYS = frozenset(('version', 'epoch', 'execution', 'subject', 'tool', 'arguments', 'callId', 'rootCallId', 'nested', 'digest'))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ApprovalAuthority:
    """Only the trusted host can register attempts and decide approvals.

    Every boot invalidates pending authorizations. Consumed authorizations stay
    consumed even when the action's outcome is unknown. No automatic replay.
    """
    def __init__(self, root, tools=(), enabled=False, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / 'approvals.db'
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        self.lease = open(self.root / 'approval-authority.lock', 'a')
        os.chmod(self.lease.name, 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lease.close()
            raise
        self.enabled, self.tools, self.clock = enabled, frozenset(tools), clock
        self.boot = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.closed = False
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attempts(
                  id TEXT PRIMARY KEY,boot TEXT,subject TEXT,device INTEGER,inode INTEGER,state TEXT);
                CREATE TABLE IF NOT EXISTS approvals(
                  id TEXT PRIMARY KEY,attempt TEXT,call_id TEXT,request TEXT,digest TEXT,epoch TEXT,
                  state TEXT,created REAL,expires REAL,updated REAL,
                  UNIQUE(attempt,call_id));
                CREATE TABLE IF NOT EXISTS decisions(
                  sender TEXT,channel TEXT,delivery TEXT,digest TEXT,response TEXT,
                  PRIMARY KEY(sender,channel,delivery));
            ''')
            db.execute("UPDATE attempts SET state='interrupted' WHERE state='active'")
            db.execute("UPDATE approvals SET state='revoked',updated=? WHERE state IN ('pending','approved')", (self.clock(),))

    @contextmanager
    def db(self):
        with self.lock:
            if self.closed:
                raise AgentError('APPROVAL_UNAVAILABLE', '审批通道已关闭')
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def register(self, subject):
        if not self.enabled:
            raise AgentError('WRITES_DISABLED', '工具写入尚未启用')
        if not isinstance(subject, dict) or set(subject) != SUBJECT_KEYS:
            raise AgentError('APPROVAL_DENIED', '执行来源不完整')
        for value in subject.values():
            required(value, 4096)
        cwd = os.path.realpath(subject['cwd'])
        info = os.stat(cwd)
        if cwd != subject['cwd'] or not os.path.isdir(cwd):
            raise AgentError('APPROVAL_DENIED', '执行目录必须固定为真实目录')
        with self.db() as db:
            # Duplicate registration is not a restart/resume operation.
            if db.execute('SELECT 1 FROM attempts WHERE id=?', (subject['attempt'],)).fetchone():
                raise AgentError('APPROVAL_DENIED', '执行尝试已存在，不会复用授权')
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                       (subject['attempt'], self.boot, canonical(subject), info.st_dev, info.st_ino, 'active'))

    def _attempt(self, db, attempt):
        row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt,)).fetchone()
        if not self.enabled or row is None or row['boot'] != self.boot or row['state'] != 'active':
            raise AgentError('APPROVAL_DENIED', '执行尝试无效或已撤销')
        subject = json.loads(row['subject'])
        try:
            info = os.stat(subject['cwd'])
            valid = (os.path.realpath(subject['cwd']) == subject['cwd']
                     and (info.st_dev, info.st_ino) == (row['device'], row['inode']))
        except OSError:
            valid = False
        if not valid:
            raise AgentError('APPROVAL_DENIED', '执行目录已发生变化')
        return row, subject

    def _request(self, db, attempt, request):
        _, subject = self._attempt(db, attempt)
        if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
            raise AgentError('APPROVAL_DENIED', '审批请求字段无效')
        for key in ('epoch', 'execution', 'tool', 'callId', 'rootCallId', 'digest'):
            required(request[key], 256)
        # This version accepts only flat string parameters from reviewed tools.
        # That also avoids Python/JS numeric JSON canonicalization differences.
        if (type(request['version']) is not int or request['version'] != 1 or request['subject'] != subject
                or request['tool'] not in self.tools or request['nested'] is not False
                or not isinstance(request['arguments'], dict)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in request['arguments'].items())):
            raise AgentError('APPROVAL_DENIED', '审批工具、参数或来源不匹配')
        payload = {key: value for key, value in request.items() if key != 'digest'}
        if len(canonical(request)) > 32000 or request['digest'] != fingerprint(payload):
            raise AgentError('APPROVAL_DENIED', '实际参数摘要不匹配')
        return subject

    def request(self, attempt, request):
        with self.db() as db:
            self._request(db, attempt, request)
            old = db.execute('SELECT * FROM approvals WHERE attempt=? AND call_id=?', (attempt, request['callId'])).fetchone()
            if old:
                if old['request'] != canonical(request):
                    raise AgentError('APPROVAL_DENIED', '调用标识已绑定其他工具参数')
                return self._view(db, old)
            now = self.clock()
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (identifier, attempt, request['callId'], canonical(request), request['digest'], request['epoch'],
                        'pending', now, now + 240, now))
            return self._view(db, db.execute('SELECT * FROM approvals WHERE id=?', (identifier,)).fetchone())

    def _view(self, db, row):
        now = self.clock()
        if row['state'] in ('pending', 'approved') and (now < row['created'] or now >= row['expires']):
            db.execute("UPDATE approvals SET state='expired',updated=? WHERE id=?", (now, row['id']))
            row = db.execute('SELECT * FROM approvals WHERE id=?', (row['id'],)).fetchone()
        request = json.loads(row['request'])
        return {'ok': True, 'approval_id': row['id'], 'state': row['state'], 'tool': request['tool'],
                'arguments': request['arguments'], 'digest': row['digest'], 'subject': request['subject'],
                'created': row['created'], 'expires': row['expires'],
                'grant': {'approvalId': row['id'], 'digest': row['digest'], 'epoch': row['epoch'],
                          'expiresAt': row['expires'] * 1000} if row['state'] == 'approved' else None}

    def inspect(self, sender, channel, conversation, project, identifier=''):
        with self.db() as db:
            rows = db.execute('SELECT * FROM approvals ORDER BY created DESC').fetchall()
            values = []
            for row in rows:
                subject = json.loads(row['request'])['subject']
                if (subject['sender'], subject['channel'], subject['conversation'], subject['project']) != (sender, channel, conversation, project):
                    continue
                if identifier and row['id'] != identifier:
                    continue
                values.append(self._view(db, row))
                if len(values) == 50:
                    break
            if identifier and not values:
                raise AgentError('NOT_FOUND', '审批不存在或不属于当前来源、会话和项目')
            return values[0] if identifier else {'ok': True, 'approvals': values}

    def decide(self, sender, channel, conversation, project, identifier, delivery, allow):
        required(delivery, 128)
        if type(allow) is not bool:
            raise AgentError('APPROVAL_DENIED', '审批决定无效')
        with self.db() as db:
            row = db.execute('SELECT * FROM approvals WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise AgentError('NOT_FOUND', '审批不存在')
            subject = json.loads(row['request'])['subject']
            if (subject['sender'], subject['channel'], subject['conversation'], subject['project']) != (sender, channel, conversation, project):
                raise AgentError('APPROVAL_DENIED', '审批来源、会话或项目不匹配')
            decision_digest = fingerprint([identifier, allow])
            old = db.execute('SELECT * FROM decisions WHERE sender=? AND channel=? AND delivery=?', (sender, channel, delivery)).fetchone()
            if old:
                if old['digest'] != decision_digest:
                    raise AgentError('DELIVERY_CONFLICT', '原确认消息已绑定其他审批动作')
                return {**self._view(db, row), 'reused': True}
            self._attempt(db, row['attempt'])
            value = self._view(db, row)
            if value['state'] != 'pending':
                raise AgentError('APPROVAL_DENIED', '审批已处理、撤销或过期')
            db.execute('UPDATE approvals SET state=?,updated=? WHERE id=?', ('approved' if allow else 'denied', self.clock(), identifier))
            db.execute('INSERT INTO decisions VALUES (?,?,?,?,?)', (sender, channel, delivery, decision_digest, '{}'))
            return self._view(db, db.execute('SELECT * FROM approvals WHERE id=?', (identifier,)).fetchone())

    def consume(self, attempt, request, grant):
        with self.db() as db:
            self._request(db, attempt, request)
            if not isinstance(grant, dict) or set(grant) != {'approvalId', 'digest', 'epoch', 'expiresAt'}:
                return False
            row = db.execute('SELECT * FROM approvals WHERE id=? AND attempt=?', (grant['approvalId'], attempt)).fetchone()
            if row is None or row['request'] != canonical(request):
                return False
            value = self._view(db, row)
            if value['state'] != 'approved' or grant != value['grant']:
                return False
            claim = db.execute("UPDATE approvals SET state='consumed',updated=? WHERE id=? AND state='approved'", (self.clock(), row['id']))
            return claim.rowcount == 1

    def revoke(self, attempt):
        with self.db() as db:
            db.execute("UPDATE attempts SET state='closed' WHERE id=? AND boot=?", (attempt, self.boot))
            db.execute("UPDATE approvals SET state='revoked',updated=? WHERE attempt=? AND state IN ('pending','approved')", (self.clock(), attempt))

    def close(self):
        with self.lock:
            if self.closed:
                return
            with self.db() as db:
                db.execute("UPDATE attempts SET state='closed' WHERE boot=? AND state='active'", (self.boot,))
                db.execute("UPDATE approvals SET state='revoked',updated=? WHERE state IN ('pending','approved')", (self.clock(),))
            self.closed = True
            self.lease.close()
