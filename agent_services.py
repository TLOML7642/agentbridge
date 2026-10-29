"""Independent personal-service capability and durable server request mailbox."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import socketserver
import sqlite3
import threading
import time

from agent_runtime import AgentError, required


SERVICE_FIELDS = {
    'reminder_draft': ({'due_at', 'content'}, {'timezone', 'reminder_id'}),
    'reminder_list': (set(), set()), 'reminder_get': ({'reminder_id'}, set()),
    'reminder_cancel': ({'reminder_id'}, set()),
}


class ServiceQueue:
    def __init__(self, root, clock=time.time, profile=None):
        self.root, self.clock = Path(root), clock
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / 'services.db'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.lock, self.capabilities = threading.RLock(), {}
        self.closed = False
        self.profile = profile
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS service_requests(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,sender TEXT,channel TEXT,
                  conversation TEXT,project TEXT,attempt TEXT,call_id TEXT,tool TEXT,arguments TEXT,
                  digest TEXT,state TEXT,result TEXT,expires REAL,UNIQUE(attempt,call_id));
            ''')
        self.socket_path = str(self.root / ('svc-' + secrets.token_hex(5) + '.sock'))
        if len(self.socket_path.encode()) >= 104:
            raise AgentError('UNAVAILABLE', '个人服务私有套接字路径过长')
        owner = self

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            block_on_close = False

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(5)
                try:
                    data = self.rfile.readline(16385)
                    if len(data) > 16384 or not data.endswith(b'\n'):
                        raise ValueError('invalid service frame')
                    result = owner.rpc(json.loads(data))
                except Exception:
                    result = {'ok': False, 'code': 'SERVICE_DENIED'}
                try:
                    self.wfile.write((json.dumps(result, ensure_ascii=False) + '\n').encode())
                except OSError:
                    pass  # Admission remains durable when a native caller stops.

        self.server = Server(self.socket_path, Handler)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, name='agent-service-rpc', daemon=True)
        self.thread.start()

    @contextmanager
    def db(self):
        with self.lock:
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

    def issue(self, subject, cancel, direct_text=''):
        with self.lock:
            if self.closed:
                raise AgentError('UNAVAILABLE', '个人服务已停止')
            for key in ('sender', 'channel', 'conversation', 'project', 'attempt'):
                required(subject.get(key))
            token = secrets.token_hex(32)
            # Drafts still need human confirmation. Cancellation is immediate,
            # so require a short, direct cancellation request from the user,
            # not a model decision based on a file or tool output.
            cancel_allowed = (isinstance(direct_text, str) and len(direct_text) <= 300
                              and not re.search(r'[\n>“”"`]|文档|文件|引用|转发|https?://', direct_text)
                              and bool(re.search(r'(?:取消|撤销|删除|cancel).*(?:提醒|remind)|(?:提醒|remind).*(?:取消|撤销|删除|cancel)', direct_text, re.I)))
            self.capabilities[token] = (dict(subject), cancel, time.monotonic() + 120, cancel_allowed)
            return {'socket': self.socket_path, 'capability': token, 'purpose': 'personal-service'}

    def revoke_capability(self, token):
        with self.lock:
            self.capabilities.pop(token, None)

    def rpc(self, payload):
        if not isinstance(payload, dict):
            raise AgentError('AUTH_FAIL', '无效个人服务请求')
        token = required(payload.get('capability'), 128)
        with self.lock:
            cap = self.capabilities.get(token)
            if not cap or self.closed or cap[1].is_set() or time.monotonic() >= cap[2]:
                raise AgentError('AUTH_FAIL', '个人服务 capability 已撤销或用途不匹配')
            subject = cap[0]
            if payload.get('op') == 'profile' and set(payload) == {'op', 'capability', 'call_id', 'tool', 'arguments'}:
                if self.profile is None:
                    raise AgentError('SERVICE_DISABLED', '显式偏好工具尚未启用')
                required(payload['call_id'], 128)
                result = self.profile(subject, payload['tool'], payload['arguments'], payload['call_id'])
                return {'ok': True, 'state': 'resolved', 'result': result}
            if payload.get('op') == 'result' and set(payload) == {'op', 'capability', 'request_id'}:
                return self.result(subject, payload['request_id'])
            if payload.get('op') != 'request' or set(payload) != {'op', 'capability', 'call_id', 'tool', 'arguments'}:
                raise AgentError('AUTH_FAIL', '个人服务只允许固定提醒意图，不能提交文件动作或确认')
            call = required(payload['call_id'], 128)
            tool, args = payload['tool'], payload['arguments']
            if tool == 'reminder_cancel' and not cap[3]:
                raise AgentError('AUTH_FAIL', '需要用户直接明确取消提醒，不能按文件或工具内容取消')
            if (not isinstance(tool, str) or tool not in SERVICE_FIELDS or not isinstance(args, dict)
                    or not SERVICE_FIELDS[tool][0] <= args.keys() or args.keys() - set.union(*SERVICE_FIELDS[tool])
                    or any(not isinstance(v, str) or len(v) > 1000 or '\0' in v for v in args.values())):
                raise AgentError('INVALID_MESSAGE', '个人服务参数无效，不允许指定来源/接收者或执行动作')
            encoded = json.dumps(args, sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(json.dumps([subject, tool, encoded], sort_keys=True).encode()).hexdigest()
            identifier = hashlib.sha256(json.dumps([subject['attempt'], call]).encode()).hexdigest()
            with self.db() as db:
                old = db.execute('SELECT * FROM service_requests WHERE id=?', (identifier,)).fetchone()
                if old:
                    if old['digest'] != digest:
                        raise AgentError('DELIVERY_CONFLICT', '服务调用 ID 已绑定其他参数')
                else:
                    count = db.execute("SELECT count(*) FROM service_requests WHERE state='pending'").fetchone()[0]
                    if count >= 100:
                        raise AgentError('BUSY', '个人服务队列已满，请查询原请求')
                    db.execute('INSERT INTO service_requests(id,sender,channel,conversation,project,attempt,call_id,'
                               "tool,arguments,digest,state,expires) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
                               (identifier, subject['sender'], subject['channel'], subject['conversation'], subject['project'],
                                subject['attempt'], call, tool, encoded, digest, self.clock() + 120))
        # Bounded wait for the existing server poller, never a reminder timer.
        deadline = time.monotonic() + 3
        while not self.closed and time.monotonic() < deadline and not cap[1].wait(.05):
            result = self.result(subject, identifier)
            if result['state'] != 'pending':
                return result
        return self.result(subject, identifier)

    @staticmethod
    def _expire(db, now):
        db.execute("UPDATE service_requests SET state='expired',result=? WHERE state='pending' AND expires<=?",
                   (json.dumps({'ok': False, 'error': '服务请求过期，请查询原请求，不自动重建。'}), now))

    def result(self, subject, identifier):
        required(identifier, 128)
        with self.db() as db:
            self._expire(db, self.clock())
            row = db.execute('SELECT * FROM service_requests WHERE id=? AND sender=? AND channel=? AND conversation=? AND project=?',
                             (identifier, subject['sender'], subject['channel'], subject['conversation'], subject['project'])).fetchone()
            if row is None:
                raise AgentError('AUTH_FAIL', '服务结果不属于当前原生会话/项目')
            return {'ok': True, 'request_id': identifier, 'state': row['state'],
                    'result': json.loads(row['result']) if row['result'] else None}

    def pending(self, sender, channel):
        with self.db() as db:
            self._expire(db, self.clock())
            rows = db.execute("SELECT id,tool,arguments,expires FROM service_requests WHERE sender=? AND channel=? AND state='pending' ORDER BY seq LIMIT 20",
                              (sender, channel)).fetchall()
            return {'ok': True, 'requests': [dict(row) | {'arguments': json.loads(row['arguments'])} for row in rows]}

    def resolve(self, sender, channel, identifier, result):
        if not isinstance(result, str) or len(result.encode()) > 16384:
            raise AgentError('INVALID_MESSAGE', '服务结果超出限制')
        parsed = json.loads(result)
        if not isinstance(parsed, dict) or type(parsed.get('ok')) is not bool:
            raise AgentError('INVALID_MESSAGE', '服务结果缺少明确状态')
        with self.db() as db:
            row = db.execute('SELECT * FROM service_requests WHERE id=? AND sender=? AND channel=?', (identifier, sender, channel)).fetchone()
            if row is None:
                raise AgentError('AUTH_FAIL', '服务结果来源不匹配')
            if row['state'] == 'resolved' and json.loads(row['result']) != parsed:
                raise AgentError('DELIVERY_CONFLICT', '服务结果已绑定其他内容')
            # A server may have committed just before mailbox expiry. Preserve
            # its definitive evidence; never replay the operation on timeout.
            db.execute("UPDATE service_requests SET state='resolved',result=? WHERE id=?", (result, identifier))
            return {'ok': True}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.capabilities.clear()
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        Path(self.socket_path).unlink(missing_ok=True)
