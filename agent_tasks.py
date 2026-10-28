"""Worker-owned background actions. Private RPC, no second model or daemon."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socketserver
import sqlite3
import threading
import time
import uuid

from agent_approvals import ApprovalAuthority, canonical, fingerprint
from agent_runtime import AgentError, required
from agent_actions import prepare


TOOLS = {'create_text': {'path', 'text'}, 'make_directory': {'path'},
         'run_command': {'program', 'argv'}}
TERMINAL = frozenset(('verified', 'process-ended', 'denied', 'cancelled', 'uncertain', 'failed'))


class TaskRuntime:
    def __init__(self, root, runner, project, enabled=False, recover=lambda _row: None, check_external=lambda: None,
                 allowed_projects=None):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / 'tasks.db'
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        self.enabled, self.runner, self.project = enabled, runner, project
        self.allowed_projects = None if allowed_projects is None else frozenset(allowed_projects)
        self.check_external = check_external
        self.close_lock = threading.Lock()
        self.closed = False
        self.lock = threading.RLock()
        self.execution_lock = threading.Lock()
        self.stop, self.wake = threading.Event(), threading.Event()
        self.uncertain = threading.Event()
        self.cancels, self.capabilities = {}, {}
        self.approvals = ApprovalAuthority(self.root / 'approval', tools=TOOLS, enabled=enabled)
        try:
            with self.db() as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS tasks(
                      seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,origin TEXT,delivery TEXT,
                      sender TEXT,channel TEXT,conversation TEXT,project TEXT,cwd TEXT,device INTEGER,inode INTEGER,
                      session TEXT,tool TEXT,arguments TEXT,digest TEXT,state TEXT,result TEXT,created REAL,
                      UNIQUE(origin,delivery));
                    CREATE TABLE IF NOT EXISTS task_events(
                      seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,task TEXT,kind TEXT,
                      sender TEXT,channel TEXT,conversation TEXT,project TEXT,body TEXT,created REAL,
                      UNIQUE(task,kind));
                    CREATE TABLE IF NOT EXISTS task_controls(
                      sender TEXT,channel TEXT,delivery TEXT,digest TEXT,PRIMARY KEY(sender,channel,delivery));
                ''')
                columns = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
                for name, kind in (('exec_pgid', 'INTEGER'), ('exec_identity', 'TEXT'), ('exec_state', 'TEXT')):
                    if name not in columns:
                        db.execute('ALTER TABLE tasks ADD COLUMN ' + name + ' ' + kind)
                for row in db.execute("SELECT * FROM tasks WHERE state='running'").fetchall():
                    try:
                        result = recover(dict(row))
                        canonical(result)
                    except Exception:
                        result = None
                    state = result.get('completion') if isinstance(result, dict) else 'uncertain'
                    if state not in ('verified', 'process-ended', 'denied'):
                        state, result = 'uncertain', None
                    db.execute('UPDATE tasks SET state=?,result=? WHERE id=?', (state, canonical(result) if result else '', row['id']))
                    self._event(db, row, 'terminal', {'state': state, 'result': result})
                if db.execute("SELECT 1 FROM tasks WHERE state='uncertain' LIMIT 1").fetchone():
                    self.uncertain.set()
            # A unique unpredictable path avoids unlinking another process's
            # listening socket after an unclean shutdown. No public TCP port.
            self.socket_path = str(self.root / ('rpc-' + secrets.token_hex(5) + '.sock'))
            if len(self.socket_path.encode()) >= 104:
                raise AgentError('UNAVAILABLE', '任务私有套接字路径过长')
            owner = self

            class Server(socketserver.ThreadingUnixStreamServer):
                daemon_threads = True
                block_on_close = False

            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    self.connection.settimeout(5)
                    try:
                        line = self.rfile.readline(65537)
                        if len(line) > 65536 or not line.endswith(b'\n'):
                            raise ValueError('invalid frame')
                        result = owner.rpc(json.loads(line))
                    except Exception:
                        result = {'ok': False, 'code': 'TASK_RPC_DENIED'}
                    self.wfile.write((canonical(result) + '\n').encode())

            self.server = Server(self.socket_path, Handler)
            os.chmod(self.socket_path, 0o600)
            self.server_thread = threading.Thread(target=self.server.serve_forever, name='agent-task-rpc', daemon=True)
            self.thread = threading.Thread(target=self._loop, name='agent-task-lane', daemon=True)
            self.server_thread.start()
            self.thread.start()
        except BaseException:
            if hasattr(self, 'server'):
                self.server.server_close()
                Path(self.socket_path).unlink(missing_ok=True)
            self.approvals.close()
            raise

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

    def can_submit(self, project):
        return (self.enabled and not self.stop.is_set()
                and (self.allowed_projects is None or project in self.allowed_projects))

    def issue(self, kind, subject, ttl=120, cancel=None):
        if not self.can_submit(subject.get('project')) or kind not in ('chat', 'task'):
            raise AgentError('WRITES_DISABLED', '后台动作尚未启用')
        token = secrets.token_hex(32)
        with self.lock:
            self.capabilities[token] = {'kind': kind, 'subject': dict(subject), 'expires': time.monotonic() + ttl,
                                        'cancel': cancel or threading.Event()}
        return {'socket': self.socket_path, 'capability': token}

    def revoke_capability(self, token):
        with self.lock:
            self.capabilities.pop(token, None)

    def rpc(self, payload):
        if not isinstance(payload, dict) or set(payload) - {'capability', 'op', 'call_id', 'tool', 'arguments', 'request', 'grant'}:
            raise AgentError('AUTH_FAIL', '任务请求含非法字段')
        token = required(payload.get('capability'), 128)
        # Capabilities are host-created, never read from a tool's arguments.
        with self.lock:
            cap = self.capabilities.get(token)
            if not cap or not self.enabled or self.stop.is_set() or cap['cancel'].is_set() or time.monotonic() >= cap['expires']:
                raise AgentError('AUTH_FAIL', '任务执行授权不存在或已撤销')
            subject = cap['subject']
            if not self.can_submit(subject.get('project')):
                raise AgentError('WRITES_DISABLED', '当前项目未启用后台动作灰度')
            if cap['kind'] == 'chat' and payload.get('op') == 'submit':
                if set(payload) != {'capability', 'op', 'call_id', 'tool', 'arguments'}:
                    raise AgentError('AUTH_FAIL', '任务登记字段错误')
                return self.submit(subject, payload['call_id'], payload['tool'], payload['arguments'])
            if cap['kind'] != 'task' or payload.get('op') not in ('approval', 'consume'):
                raise AgentError('AUTH_FAIL', '任务执行授权用途不匹配')
            request = payload.get('request')
            if not isinstance(request, dict) or request.get('subject') != subject:
                raise AgentError('AUTH_FAIL', '任务工具身份不匹配')
            with self.db() as db:
                row = db.execute("SELECT * FROM tasks WHERE id=? AND state='running'", (subject['attempt'],)).fetchone()
                if row is None or request.get('tool') != row['tool'] or request.get('arguments') != json.loads(row['arguments']):
                    raise AgentError('AUTH_FAIL', '任务工具或实际参数已改变')
            if payload['op'] == 'consume':
                if set(payload) != {'capability', 'op', 'request', 'grant'}:
                    raise AgentError('AUTH_FAIL', '消费字段错误')
                return {'ok': True, 'consumed': self.approvals.consume(subject['attempt'], request, payload.get('grant'))}
            if set(payload) != {'capability', 'op', 'request'}:
                raise AgentError('AUTH_FAIL', '审批字段错误')
            value = self.approvals.request(subject['attempt'], request)
            with self.db() as db:
                self._event(db, row, 'approval', {key: value[key] for key in ('approval_id', 'state', 'tool', 'arguments', 'digest', 'expires')})
            return value

    def submit(self, subject, delivery, tool, arguments):
        if not self.can_submit(subject.get('project')):
            raise AgentError('WRITES_DISABLED', '后台动作尚未启用')
        required(delivery, 128)
        if tool not in TOOLS or not isinstance(arguments, dict) or set(arguments) != TOOLS[tool]:
            raise AgentError('INVALID_MESSAGE', '后台动作或参数不在白名单')
        for value in arguments.values():
            required(value, 16384)
        if tool == 'run_command':
            if len(canonical(arguments)) > 1800:
                raise AgentError('INVALID_MESSAGE', '命令参数过长，无法完整展示本次确认')
            argv = json.loads(arguments['argv'])
            if not isinstance(argv, list) or len(argv) > 64 or any(not isinstance(item, str) or '\0' in item or len(item) > 4096 for item in argv):
                raise AgentError('INVALID_MESSAGE', '命令参数必须为有限字符串数组')
        digest = fingerprint({'tool': tool, 'arguments': arguments})
        with self.db() as db:
            old = db.execute('SELECT * FROM tasks WHERE origin=? AND delivery=?', (subject['attempt'], delivery)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise AgentError('DELIVERY_CONFLICT', '原调用已绑定其他动作')
                return self._view(old, reused=True)
            if db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued','running')").fetchone()[0] >= 32:
                raise AgentError('BUSY', '后台队列已满')
            if db.execute("SELECT 1 FROM tasks WHERE state='uncertain' LIMIT 1").fetchone():
                raise AgentError('TASK_UNCERTAIN', '存在结果未知的后台动作，请先在本机核查')
            cwd = os.path.realpath(self.project(subject['project']))
            if cwd != subject['cwd']:
                raise AgentError('PROJECT_CHANGED', '项目目录已改变')
            info = os.stat(cwd)
            private_root = self.root.parent
            prefixes = [str(private_root), *[str(private_root.parent / name) for name in ('control', 'logs', 'audit')]]
            try:
                actual_arguments = prepare(cwd, tool, arguments, prefixes)
            except (OSError, ValueError, TypeError):
                raise AgentError('INVALID_MESSAGE', '目标、命令或参数不在已审核范围，未登记动作') from None
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO tasks(id,origin,delivery,sender,channel,conversation,project,cwd,device,inode,session,'
                       'tool,arguments,digest,state,result,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?)',
                       (identifier, subject['attempt'], delivery, subject['sender'], subject['channel'], subject['conversation'],
                        subject['project'], cwd, info.st_dev, info.st_ino, uuid.uuid4().hex, tool, canonical(actual_arguments), digest, 'queued', '', time.time()))
            row = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            self._event(db, row, 'accepted', {'state': 'queued', 'tool': tool, 'arguments': arguments})
            value = self._view(row)
        self.wake.set()
        return value

    @staticmethod
    def _view(row, reused=False):
        return {'ok': True, 'task_id': row['id'], 'state': row['state'], 'project': row['project'],
                'conversation': row['conversation'], 'tool': row['tool'], 'arguments': json.loads(row['arguments']),
                'result': json.loads(row['result']) if row['result'] else None, 'reused': reused}

    def _event(self, db, row, kind, body):
        db.execute('INSERT OR IGNORE INTO task_events(id,task,kind,sender,channel,conversation,project,body,created) '
                   'VALUES (?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, row['id'], kind, row['sender'], row['channel'],
                                                  row['conversation'], row['project'], canonical(body), time.time()))

    def events(self, sender, channel, conversation='', project='', after=0):
        with self.db() as db:
            query = 'SELECT * FROM task_events WHERE sender=? AND channel=? AND seq>?'
            values = [sender, channel, after]
            for key, value in (('conversation', conversation), ('project', project)):
                if value:
                    query += ' AND ' + key + '=?'
                    values.append(value)
            rows = db.execute(query + ' ORDER BY seq LIMIT 100', values).fetchall()
            return {'ok': True, 'events': [{**dict(row), 'body': json.loads(row['body'])} for row in rows]}

    def inspect(self, sender, channel, conversation, project, identifier=''):
        with self.db() as db:
            query = 'SELECT * FROM tasks WHERE sender=? AND channel=? AND conversation=? AND project=?'
            values = [sender, channel, conversation, project]
            if identifier:
                query += ' AND id=?'
                values.append(identifier)
            rows = db.execute(query + ' ORDER BY seq DESC LIMIT 50', values).fetchall()
            if identifier:
                rows = [row for row in rows if row['id'] == identifier]
                if not rows:
                    raise AgentError('NOT_FOUND', '任务不存在或来源、会话、项目不匹配')
                return self._view(rows[0])
            return {'ok': True, 'tasks': [self._view(row) for row in rows]}

    def cancel(self, sender, channel, conversation, project, identifier, delivery):
        required(delivery, 128)
        digest = fingerprint(['cancel', identifier, conversation, project])
        with self.lock:
            self.inspect(sender, channel, conversation, project, identifier)
            with self.db() as db:
                prior = db.execute('SELECT digest FROM task_controls WHERE sender=? AND channel=? AND delivery=?', (sender, channel, delivery)).fetchone()
                if prior and prior['digest'] != digest:
                    raise AgentError('DELIVERY_CONFLICT', '取消投递已绑定其他动作')
                if not prior:
                    row = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
                    if row['state'] == 'queued':
                        db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (identifier,))
                        self._event(db, row, 'terminal', {'state': 'cancelled', 'result': None})
                    elif row['state'] == 'running':
                        self.cancels[identifier].set()
                        self.approvals.revoke(identifier)
                    db.execute('INSERT OR IGNORE INTO task_controls VALUES (?,?,?,?)', (sender, channel, delivery, digest))
            return self.inspect(sender, channel, conversation, project, identifier)

    @contextmanager
    def execution_fence(self, cancel):
        # Conservative global fence shared with legacy tasks. Approval waiting
        # can hold this lane, never the reserved chat lane or control APIs.
        while not self.execution_lock.acquire(timeout=.05):
            if cancel.is_set() or self.stop.is_set():
                raise AgentError('CANCELLED', '后台执行尚未开始')
        try:
            if self.uncertain.is_set():
                raise AgentError('TASK_UNCERTAIN', '结果未知的执行通道已隔离')
            with self.db() as db:
                if db.execute("SELECT 1 FROM tasks WHERE state='uncertain' LIMIT 1").fetchone():
                    raise AgentError('TASK_UNCERTAIN', '结果未知的动作尚未核查，执行通道已隔离')
            self.check_external()
            yield
        finally:
            self.execution_lock.release()

    def _loop(self):
        while not self.stop.is_set():
            with self.db() as db:
                row = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
                if row:
                    row = dict(row)
                    cancel = self.cancels.setdefault(row['id'], threading.Event())
                    db.execute("UPDATE tasks SET state='running',exec_state='starting' WHERE id=?", (row['id'],))
            if not row:
                self.wake.wait(.2)
                self.wake.clear()
                continue
            subject = {key: row[key] for key in ('sender', 'channel', 'conversation', 'project', 'cwd', 'session')}
            subject['attempt'] = row['id']
            result, state, capability = None, 'uncertain', None
            started = False
            try:
                with self.execution_fence(cancel):
                    if not self.can_submit(row['project']):
                        raise AgentError('WRITES_DISABLED', '当前项目未启用后台动作灰度')
                    if cancel.is_set():
                        raise AgentError('CANCELLED', '后台执行尚未开始')
                    info = os.stat(row['cwd'])
                    if (os.path.realpath(self.project(row['project'])) != row['cwd']
                            or (info.st_dev, info.st_ino) != (row['device'], row['inode'])):
                        raise AgentError('PROJECT_CHANGED', '项目目录已改变')
                    self.approvals.register(subject)
                    capability = self.issue('task', subject, ttl=300, cancel=cancel)
                    started = True
                    try:
                        def register_executor(pgid, identity):
                            with self.db() as db:
                                if cancel.is_set() or self.stop.is_set():
                                    raise AgentError('CANCELLED', '后台执行器未放行')
                                claim = db.execute("UPDATE tasks SET exec_pgid=?,exec_identity=?,exec_state='registered' WHERE id=? AND state='running'",
                                                   (pgid, identity, row['id']))
                                if claim.rowcount != 1:
                                    raise AgentError('TASK_UNCERTAIN', '后台执行登记失效')
                        result = self.runner({**row, 'subject': subject, 'broker': capability,
                                              'register_executor': register_executor}, cancel)
                        if not isinstance(result, dict) or result.get('completion') not in ('verified', 'process-ended', 'denied'):
                            raise AgentError('TASK_UNCERTAIN', '执行未返回匹配收据')
                        canonical(result)
                    except BaseException as error:
                        result = None
                        if not isinstance(error, AgentError) or error.code != 'CANCELLED':
                            self.uncertain.set()
                        raise
                    state = result['completion']
            except AgentError as error:
                if error.code == 'CANCELLED':
                    state = 'cancelled'
                elif not started:
                    state = 'failed'
            except Exception:
                if not started:
                    state = 'failed'
            finally:
                if capability:
                    self.revoke_capability(capability['capability'])
                self.approvals.revoke(row['id'])
            with self.db() as db:
                db.execute('UPDATE tasks SET state=?,result=? WHERE id=?', (state, canonical(result) if result else '', row['id']))
                self._event(db, row, 'terminal', {'state': state, 'result': result})
                self.cancels.pop(row['id'], None)

    def close(self):
        with self.close_lock:
            self._close()

    def _close(self):
        if self.closed:
            return
        self.stop.set()
        with self.lock:
            for cancel in self.cancels.values():
                cancel.set()
            self.capabilities.clear()
        self.wake.set()
        self.thread.join(20)
        if self.thread.is_alive():
            raise RuntimeError('task executor did not quiesce; authority lease retained')
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(5)
        Path(self.socket_path).unlink(missing_ok=True)
        self.approvals.close()
        self.closed = True
