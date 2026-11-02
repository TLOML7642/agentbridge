"""Synchronous fixed Mac interface. Failed/unknown calls are never spooled or retried."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socketserver
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

from agent_runtime import AgentError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Executor:
    def __init__(self, root, url, secret, projects, enabled_projects=()):
        parsed = urllib.parse.urlparse(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('executor must use the existing fixed loopback interface')
        self.url, self.secret, self.projects = url.rstrip('/') + '/executor', secret, projects
        self.enabled_projects = frozenset(enabled_projects)
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / 'executor-calls.db'
        self.lock, self.capabilities = threading.RLock(), {}
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,digest TEXT,state TEXT,response TEXT)')
        self.path.chmod(0o600)
        self.socket_path = str(self.root / ('exec-' + secrets.token_hex(4) + '.sock'))
        owner = self

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            block_on_close = False

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(6)
                try:
                    raw = self.rfile.readline(32769)
                    if len(raw) > 32768 or not raw.endswith(b'\n'):
                        raise ValueError
                    result = owner.rpc(json.loads(raw))
                except AgentError as exc:
                    result = {'ok': False, 'code': exc.code}
                except Exception:
                    result = {'ok': False, 'code': 'EXECUTOR_UNKNOWN'}
                try:
                    self.wfile.write((json.dumps(result, ensure_ascii=False) + '\n').encode())
                except OSError:
                    pass

        self.server = Server(self.socket_path, Handler)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def can_submit(self, project):
        return project in self.enabled_projects and self.projects.target(project) == 'mac'

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def issue(self, kind, subject, ttl=120, cancel=None):
        if kind != 'chat':
            raise AgentError('AUTH_FAIL', '服务器不签发实际执行授权')
        token = secrets.token_hex(32)
        with self.lock:
            self.capabilities[token] = (dict(subject), time.monotonic() + ttl, cancel or threading.Event())
        return {'socket': self.socket_path, 'capability': token}

    def revoke_capability(self, token):
        with self.lock:
            self.capabilities.pop(token, None)

    def rpc(self, payload):
        with self.lock:
            cap = self.capabilities.get(payload.get('capability'))
        if not cap or cap[1] <= time.monotonic() or cap[2].is_set():
            raise AgentError('AUTH_FAIL', '工具能力已失效')
        subject = cap[0]
        op = payload.get('op')
        allowed = {'submit': {'capability', 'op', 'call_id', 'tool', 'arguments'},
                   'read': {'capability', 'op', 'operation', 'value', 'range'}}
        if op not in allowed or set(payload) != allowed[op]:
            raise AgentError('AUTH_FAIL', '执行接口用途或字段无效')
        if op == 'submit' and not self.can_submit(subject['project']):
            raise AgentError('WRITES_DISABLED', '该项目没有后台动作授权')
        fields = {key: value for key, value in payload.items() if key not in ('capability', 'op')}
        return self.call(subject, op, fields)

    def call(self, subject, op, fields):
        subject = {key: subject[key] for key in ('sender', 'channel', 'project', 'conversation', 'attempt')}
        payload = dict(subject=subject, sender=subject['sender'], channel=subject['channel'], op=op, **fields)
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        digest = hashlib.sha256(body).hexdigest()
        identifier = hashlib.sha256(json.dumps([subject['sender'], subject['channel'], subject['attempt'], op,
                                    fields.get('call_id', fields.get('delivery_id', digest))]).encode()).hexdigest()
        mutating = op in ('submit', 'approve', 'deny', 'cancel')
        # Commit intent before opening the socket. A crash here is unknown, not
        # evidence of no side effect. No background loop scans this table.
        with self.lock:
            if mutating:
                with self.db() as db:
                    old = db.execute('SELECT digest,state,response FROM calls WHERE id=?', (identifier,)).fetchone()
                    if old:
                        if old[0] != digest:
                            raise AgentError('DELIVERY_CONFLICT', '工具调用ID已绑定其他参数')
                        if old[1] == 'returned':
                            return json.loads(old[2])
                        raise AgentError('EXECUTOR_UNKNOWN', '原电脑动作未确认，须查询原调用，不自动重试')
                    db.execute('INSERT INTO calls VALUES (?,?,?,?)', (identifier, digest, 'calling', ''))
            state, result = 'unknown', None
            try:
                request = urllib.request.Request(self.url, data=body,
                    headers={'X-AgentBridge-Secret': self.secret, 'Content-Type': 'application/json'})
                # Ignore ambient proxy variables; never change host networking.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open(request, timeout=3) as response:
                    raw = response.read(65537)
                    if len(raw) > 65536:
                        raise ValueError('oversized executor response')
                    result = json.loads(raw)
                if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
                    raise ValueError('invalid executor response')
                state = 'returned'
                return result
            except urllib.error.URLError as exc:
                state = 'not_accepted' if isinstance(exc.reason, ConnectionRefusedError) else 'unknown'
                raise AgentError('MAC_OFFLINE' if state == 'not_accepted' else 'EXECUTOR_UNKNOWN',
                                 'Mac不可达或结果未确认；没有离线积压，不会恢复后重放') from None
            except (OSError, ValueError):
                raise AgentError('EXECUTOR_UNKNOWN', '执行结果未知；没有自动重试') from None
            finally:
                if mutating:
                    with self.db() as db:
                        db.execute('UPDATE calls SET state=?,response=? WHERE id=?',
                                   (state, json.dumps(result, ensure_ascii=False) if result else '', identifier))

    def events(self, sender, channel, conversation='', project='', after=0):
        if not conversation or not project:
            return {'ok': True, 'events': []}
        subject = dict(sender=sender, channel=channel, conversation=conversation, project=project, attempt='events')
        try:
            return self.call(subject, 'events', {'after': str(after)})
        except AgentError:
            return {'ok': True, 'events': [], 'mac_status': 'unavailable_or_unknown'}

    def close(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        Path(self.socket_path).unlink(missing_ok=True)
