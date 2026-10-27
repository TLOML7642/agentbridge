"""Persistent, source-scoped chat lane owned by the existing worker process."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid


class AgentError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def required(value, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise AgentError('INVALID_MESSAGE', '消息字段缺失或格式错误')
    return value


class Preferences:
    """Conservative finite preferences from authenticated direct user text only."""
    CORE = {'name': 'DSH 助手', 'style': '简洁自然的中文，不虚构人设或执行结果'}
    PRIVATE = re.compile(r'sk-|password|passwd|token|secret|api.?key|密钥|密码|验证码|身份证|银行卡|手机号|健康|疾病|医院|病情|\d{5,}', re.I)
    QUOTED = re.compile(r'他说|她说|它说|别人|对方|客户|同事|网页|文档|工具输出|日志|引用|转发|总结|翻译|https?://|```|[“”"\n>]', re.I)
    RULES = [
        ('verbosity', 'brief', r'简洁|简短|短一点|简单点|少啰嗦'),
        ('verbosity', 'detailed', r'详细|展开|解释清楚'),
        ('language', 'zh', r'用中文|中文回复|中文回答'),
        ('language', 'en', r'用英文|英文回复|英文回答'),
        ('format', 'bullets', r'用列表|分点|列要点'),
        ('format', 'natural', r'不要列表|别分点|自然聊天|口语'),
        ('workflow', 'test_first', r'先测试|先跑测试'),
        ('workflow', 'backup_first', r'先备份|修改前备份'),
    ]

    def __init__(self, allow_learning=False):
        self.allow_learning = allow_learning

    def policy(self, db, owner):
        db.execute('INSERT OR IGNORE INTO preference_policy VALUES (?,?)', (owner, int(self.allow_learning)))
        return bool(db.execute('SELECT learning FROM preference_policy WHERE owner=?', (owner,)).fetchone()[0])

    def learn(self, db, owner, source, text, now=None):
        now = time.time() if now is None else now
        if not self.allow_learning or not self.policy(db, owner) or self.PRIVATE.search(text) or self.QUOTED.search(text):
            return
        if re.search(r'不要记|别记|不用记|不要学习|停止学习', text):
            return
        # Learn only short, self-contained first-person preference statements,
        # not arbitrary pasted instructions that happen to mention "remember".
        explicit = len(text) <= 100 and bool(re.match(r'^(?:请)?(?:记住|以后|我喜欢|我希望|一直)', text.strip()))
        candidates = [(key, value) for key, value, pattern in self.RULES if re.search(pattern, text)] if explicit else (
            [('verbosity', 'brief')] if re.fullmatch(r'(?:太长了|太啰嗦了|短一点|说简单点)[！!。.]?', text.strip()) else [])
        # An ambiguous direct message must not accidentally pick the last rule.
        keys = [key for key, _ in candidates]
        for key, value in candidates:
            if keys.count(key) != 1:
                continue
            if db.execute('SELECT 1 FROM preference_forgotten WHERE owner=? AND category=? AND value=?', (owner, key, value)).fetchone():
                continue
            db.execute('INSERT OR IGNORE INTO preference_evidence VALUES (?,?,?,?,?)', (owner, key, value, source, now))
            count = db.execute('SELECT count(*) FROM preference_evidence WHERE owner=? AND category=? AND value=? AND created>?',
                               (owner, key, value, now - 90 * 86400)).fetchone()[0]
            if not explicit and count < 2:
                continue
            old = db.execute('SELECT * FROM preferences WHERE owner=? AND category=?', (owner, key)).fetchone()
            if old and old['value'] != value and not explicit:
                continue
            is_explicit = explicit or bool(old and old['explicit'] and old['value'] == value)
            db.execute('INSERT INTO preferences VALUES (?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(owner,category) DO UPDATE SET value=excluded.value,explicit=excluded.explicit,'
                       'updated=excluded.updated,source=excluded.source,expires=excluded.expires',
                       (old['id'] if old else uuid.uuid4().hex, owner, key, value, int(is_explicit), now,
                        old['source'] if old and old['explicit'] and not explicit else source,
                        None if is_explicit else now + 90 * 86400))

    def view(self, db, owner, now=None):
        now = time.time() if now is None else now
        rows = db.execute('SELECT id,category,value,explicit,updated,source,expires FROM preferences '
                          'WHERE owner=? AND (expires IS NULL OR expires>?) ORDER BY category', (owner, now)).fetchall()
        core = db.execute('SELECT name FROM persona_core WHERE owner=?', (owner,)).fetchone()
        configured_learning = self.policy(db, owner)
        return {'core': {**self.CORE, **({'name': core['name']} if core else {})}, 'learning': self.allow_learning and configured_learning,
                'learning_available': self.allow_learning, 'preferences': [dict(row) for row in rows],
                'forgotten_categories': [row[0] for row in db.execute('SELECT DISTINCT category FROM preference_forgotten WHERE owner=?', (owner,))]}

    def control(self, db, owner, op, memory_id='', name=''):
        if op == 'agent.persona.set':
            if not re.fullmatch(r'[\w \-]{1,40}', name, re.UNICODE):
                raise AgentError('INVALID_MESSAGE', '名字仅支持 1 至 40 个文字、数字、空格或连字符')
            db.execute('INSERT OR REPLACE INTO persona_core VALUES (?,?)', (owner, name))
        if op == 'agent.forget':
            row = db.execute('SELECT * FROM preferences WHERE id=? AND owner=?', (memory_id, owner)).fetchone()
            if row is None:
                raise AgentError('NOT_FOUND', '偏好 ID 不存在或不属于当前发送者')
            db.execute('INSERT OR IGNORE INTO preference_forgotten VALUES (?,?,?)', (owner, row['category'], row['value']))
            db.execute('DELETE FROM preferences WHERE id=? AND owner=?', (memory_id, owner))
            return {'ok': True, 'message': '已停止后续注入和重新提取该偏好；没有删除原始聊天记录。'}
        if op in ('agent.memory.on', 'agent.memory.off'):
            if op.endswith('.on') and not self.allow_learning:
                raise AgentError('LEARNING_DISABLED', '自动学习功能尚未灰度启用')
            db.execute('INSERT OR REPLACE INTO preference_policy VALUES (?,?)', (owner, int(op.endswith('.on'))))
        return {'ok': True, **self.view(db, owner)}


class AgentRuntime:
    """One reserved chat slot, independent of the legacy execution scheduler.

    A committed running intent precedes process launch. Unknown turns block
    their native session instead of automatically replaying a possibly used input.
    """
    def __init__(self, root, runner, project, evidence=lambda *_: [], recover=lambda *_: None, allow_learning=False):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / 'conversations.db'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.lease = open(self.root / 'coordinator.lock', 'a')
        os.chmod(self.lease.name, 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lease.close()
            raise
        self.runner, self.project, self.evidence = runner, project, evidence
        self.preferences = Preferences(allow_learning)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.cancels = {}
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions(
                  id TEXT PRIMARY KEY,sender TEXT,channel TEXT,project TEXT,cwd TEXT,
                  device INTEGER,inode INTEGER,blocked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS heads(
                  sender TEXT,channel TEXT,project TEXT,cwd TEXT,session TEXT,
                  PRIMARY KEY(sender,channel,project,cwd));
                CREATE TABLE IF NOT EXISTS messages(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,
                  sender TEXT,channel TEXT,delivery TEXT,digest TEXT,session TEXT,
                  text TEXT,state TEXT,response TEXT,created REAL,ended REAL,
                  UNIQUE(sender,channel,delivery));
                CREATE TABLE IF NOT EXISTS controls(
                  sender TEXT,channel TEXT,delivery TEXT,digest TEXT,response TEXT,
                  PRIMARY KEY(sender,channel,delivery));
                CREATE TABLE IF NOT EXISTS preference_policy(owner TEXT PRIMARY KEY,learning INTEGER);
                CREATE TABLE IF NOT EXISTS persona_core(owner TEXT PRIMARY KEY,name TEXT);
                CREATE TABLE IF NOT EXISTS preferences(id TEXT UNIQUE,owner TEXT,category TEXT,value TEXT,
                  explicit INTEGER,updated REAL,source TEXT,expires REAL,PRIMARY KEY(owner,category));
                CREATE TABLE IF NOT EXISTS preference_evidence(owner TEXT,category TEXT,value TEXT,source TEXT,created REAL,
                  PRIMARY KEY(owner,category,value,source));
                CREATE TABLE IF NOT EXISTS preference_forgotten(owner TEXT,category TEXT,value TEXT,
                  PRIMARY KEY(owner,category,value));
            ''')
            for row in db.execute("SELECT * FROM messages WHERE state='running'").fetchall():
                restored = recover(dict(row))
                if restored is not None:
                    db.execute("UPDATE messages SET state='done',response=?,ended=? WHERE id=?",
                               (restored, time.time(), row['id']))
                else:
                    db.execute("UPDATE messages SET state='uncertain',ended=? WHERE id=?", (time.time(), row['id']))
                    db.execute('UPDATE sessions SET blocked=1 WHERE id=?', (row['session'],))
            db.execute("UPDATE messages SET state='uncertain',ended=? WHERE state='queued' "
                       "AND session IN (SELECT id FROM sessions WHERE blocked=1)", (time.time(),))
        self.thread = threading.Thread(target=self._loop, name='agent-chat-lane', daemon=True)
        self.thread.start()

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _session(self, db, sender, channel, project, cwd, new=False):
        row = db.execute('SELECT session FROM heads WHERE sender=? AND channel=? AND project=? AND cwd=?',
                         (sender, channel, project, cwd)).fetchone()
        if row and not new:
            return db.execute('SELECT * FROM sessions WHERE id=?', (row['session'],)).fetchone()
        sid = uuid.uuid4().hex
        stat = os.stat(cwd)
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,0)',
                   (sid, sender, channel, project, cwd, stat.st_dev, stat.st_ino))
        db.execute('INSERT OR REPLACE INTO heads VALUES (?,?,?,?,?)', (sender, channel, project, cwd, sid))
        return db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()

    def submit(self, sender, channel, delivery, text, project):
        required(sender); required(channel); required(delivery, 128); required(text, 12000)
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        with self.db() as db:
            old = db.execute('SELECT * FROM messages WHERE sender=? AND channel=? AND delivery=?',
                             (sender, channel, delivery)).fetchone()
            if old:
                if old['digest'] != fingerprint:
                    raise AgentError('DELIVERY_CONFLICT', '原投递 ID 已绑定其他消息，不会重新执行')
                return self._result(db, old, reused=True)
            if self.stop.is_set():
                raise AgentError('UNAVAILABLE', '对话通道正在停止')
            if db.execute("SELECT count(*) FROM messages WHERE state IN ('queued','running')").fetchone()[0] >= 64:
                raise AgentError('BUSY', '对话队列已满，请保留原消息 ID')
            cwd = os.path.realpath(self.project(project))
            session = self._session(db, sender, channel, project, cwd)
            if session['blocked']:
                raise AgentError('SESSION_UNCERTAIN', '上一轮结果未知；请先查询，或用 /agent new 开新会话')
            mid = uuid.uuid4().hex
            db.execute('INSERT INTO messages(id,sender,channel,delivery,digest,session,text,state,created) '
                       "VALUES (?,?,?,?,?,?,?,'queued',?)", (mid, sender, channel, delivery, fingerprint, session['id'], text, time.time()))
            # Learning failure must not roll back the chat admission itself.
            db.execute('SAVEPOINT preference_learning')
            try:
                self.preferences.learn(db, sender, mid, text)
            except Exception:
                db.execute('ROLLBACK TO preference_learning')
            finally:
                db.execute('RELEASE preference_learning')
            result = self._result(db, db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone())
        self.wake.set()
        self.export_profile(sender)
        return result

    @staticmethod
    def _result(db, row, reused=False):
        session = db.execute('SELECT * FROM sessions WHERE id=?', (row['session'],)).fetchone()
        return {'ok': True, 'message_id': row['id'], 'session_id': row['session'],
                'project': session['project'], 'state': row['state'],
                'reply': row['response'] or '', 'reused': reused,
                'completion': 'model_turn_completed' if row['state'] == 'done' else row['state']}

    def export_profile(self, owner):
        # Serialize the DB snapshot and publication with forgetting/learning.
        # Otherwise an older exporter can overwrite a newly forgotten profile.
        with self.lock:
            return self._export_profile(owner)

    def _export_profile(self, owner):
        directory = self.root / 'personas'
        target = directory / (hashlib.sha256(owner.encode()).hexdigest() + '.json')
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            with self.db() as db:
                profile = self.preferences.view(db, owner)
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(profile, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return True
        except Exception:
            # A stale desktop projection must not keep injecting a forgotten
            # value. These are generated snapshots, not original chat records.
            for file in (temporary, target):
                try:
                    file.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def control(self, sender, channel, op, project, message_id='', delivery='', memory_id='', name=''):
        result = self._control(sender, channel, op, project, message_id, delivery, memory_id, name)
        if op in ('agent.memory', 'agent.memory.on', 'agent.memory.off', 'agent.forget', 'agent.persona', 'agent.persona.set'):
            synced = self.export_profile(sender)
            result['desktop_profile_synced'] = synced
            if op == 'agent.forget' and not synced:
                result['message'] += ' 桌面偏好快照同步失败，尚不能确认桌面已停止使用。'
        return result

    def _control(self, sender, channel, op, project, message_id='', delivery='', memory_id='', name=''):
        required(sender); required(channel)
        if op == 'agent.cancel':
            required(delivery, 128); required(message_id, 128)
            fingerprint = hashlib.sha256(json.dumps([op, message_id]).encode()).hexdigest()
            event = None
            with self.db() as db:
                row = db.execute('SELECT * FROM messages WHERE id=? AND sender=? AND channel=?',
                                 (message_id, sender, channel)).fetchone()
                if row is None:
                    raise AgentError('NOT_FOUND', '消息不存在或来源不匹配')
                old = db.execute('SELECT * FROM controls WHERE sender=? AND channel=? AND delivery=?',
                                 (sender, channel, delivery)).fetchone()
                if old and old['digest'] != fingerprint:
                    raise AgentError('DELIVERY_CONFLICT', '取消投递已绑定其他动作')
                if old is None:
                    if row['state'] == 'queued':
                        db.execute("UPDATE messages SET state='cancelled',ended=? WHERE id=?", (time.time(), row['id']))
                    elif row['state'] == 'running':
                        event = self.cancels[row['id']]
                    db.execute('INSERT INTO controls VALUES (?,?,?,?,?)',
                               (sender, channel, delivery, fingerprint, '{}'))
                result = self._result(db, db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone(), reused=old is not None)
            if event is not None:
                event.set()
            return result
        with self.db() as db:
            if op in ('agent.memory', 'agent.memory.on', 'agent.memory.off', 'agent.forget', 'agent.persona', 'agent.persona.set'):
                if op in ('agent.memory.on', 'agent.memory.off', 'agent.forget', 'agent.persona.set'):
                    required(delivery, 128)
                    fingerprint = hashlib.sha256(json.dumps([op, memory_id, name]).encode()).hexdigest()
                    old = db.execute('SELECT * FROM controls WHERE sender=? AND channel=? AND delivery=?',
                                     (sender, channel, delivery)).fetchone()
                    if old:
                        if old['digest'] != fingerprint:
                            raise AgentError('DELIVERY_CONFLICT', '偏好控制投递已绑定其他动作')
                        if op == 'agent.forget':
                            return {'ok': True, 'reused': True, 'message': '此前已忘记该偏好，没有删除原始聊天。'}
                        return {'ok': True, 'reused': True, **self.preferences.view(db, sender)}
                    result = self.preferences.control(db, sender, op, memory_id, name)
                    db.execute('INSERT INTO controls VALUES (?,?,?,?,?)', (sender, channel, delivery, fingerprint, '{}'))
                    return result
                return self.preferences.control(db, sender, op, memory_id)
            if op == 'agent.lookup':
                required(delivery, 128)
                row = db.execute('SELECT * FROM messages WHERE sender=? AND channel=? AND delivery=?',
                                 (sender, channel, delivery)).fetchone()
                if row is None:
                    raise AgentError('NOT_FOUND', '原投递尚无受理记录，不会自动新建')
                return self._result(db, row, reused=True)
            if op == 'agent.status' and message_id:
                row = db.execute('SELECT * FROM messages WHERE id=? AND sender=? AND channel=?',
                                 (message_id, sender, channel)).fetchone()
                if row is None:
                    raise AgentError('NOT_FOUND', '消息不存在或来源不匹配')
                return self._result(db, row)
            if op == 'agent.status':
                rows = db.execute('SELECT * FROM messages WHERE sender=? AND channel=? ORDER BY seq DESC LIMIT 10',
                                  (sender, channel)).fetchall()
                return {'ok': True, 'messages': [self._result(db, row) for row in rows]}
            if op != 'agent.new':
                raise AgentError('INVALID_MESSAGE', '不支持的对话控制操作')
            required(delivery, 128)
            fingerprint = hashlib.sha256(json.dumps([op, project]).encode()).hexdigest()
            old = db.execute('SELECT * FROM controls WHERE sender=? AND channel=? AND delivery=?',
                             (sender, channel, delivery)).fetchone()
            if old:
                if old['digest'] != fingerprint:
                    raise AgentError('DELIVERY_CONFLICT', '投递 ID 已绑定其他操作')
                return {**json.loads(old['response']), 'reused': True}
            cwd = os.path.realpath(self.project(project))
            session = self._session(db, sender, channel, project, cwd, new=True)
            result = {'ok': True, 'session_id': session['id'], 'project': project,
                      'message': '已开新会话，不导入旧聊天；原任务和记录保留。'}
            db.execute('INSERT INTO controls VALUES (?,?,?,?,?)',
                       (sender, channel, delivery, fingerprint, json.dumps(result, ensure_ascii=False)))
            return result

    def _loop(self):
        while not self.stop.is_set():
            with self.db() as db:
                row = db.execute("SELECT * FROM messages WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
                if row:
                    session = dict(db.execute('SELECT * FROM sessions WHERE id=?', (row['session'],)).fetchone())
                    cancel = self.cancels.setdefault(row['id'], threading.Event())
                    db.execute("UPDATE messages SET state='running' WHERE id=? AND state='queued'", (row['id'],))
                    turn = {**dict(row), 'subject': {k: session[k] for k in ('sender', 'channel', 'project', 'cwd')},
                            'resume': db.execute("SELECT 1 FROM messages WHERE session=? AND state='done' LIMIT 1", (row['session'],)).fetchone() is not None}
                    turn['subject'].update(conversation=session['id'], session=session['id'], attempt=row['id'])
                    turn['persona'] = self.preferences.view(db, session['sender'])
            if not row:
                self.wake.wait(.25); self.wake.clear()
                continue
            state, reply = 'uncertain', ''
            try:
                cwd = os.path.realpath(self.project(session['project']))
                stat = os.stat(cwd)
                if cwd != session['cwd'] or (stat.st_dev, stat.st_ino) != (session['device'], session['inode']):
                    raise AgentError('PROJECT_CHANGED', '项目目录已改变')
                turn['recent_tasks'] = self.evidence(session['sender'], session['channel'], session['project'])
                reply = self.runner(turn, cancel)
                if not isinstance(reply, str) or not reply.strip():
                    raise AgentError('EMPTY_REPLY', '对话没有完整返回')
                state = 'done'
            except AgentError as exc:
                if exc.code == 'CANCELLED':
                    state = 'cancelled'
                elif exc.code == 'PROJECT_CHANGED':
                    state = 'failed'
            except Exception:
                pass
            with self.db() as db:
                db.execute('UPDATE messages SET state=?,response=?,ended=? WHERE id=?', (state, reply, time.time(), row['id']))
                if state in ('uncertain', 'cancelled'):
                    db.execute('UPDATE sessions SET blocked=1 WHERE id=?', (session['id'],))
                    db.execute("UPDATE messages SET state='uncertain',ended=? WHERE session=? AND state='queued'", (time.time(), session['id']))
                self.cancels.pop(row['id'], None)

    def close(self):
        self.stop.set()
        with self.lock:
            for cancel in self.cancels.values():
                cancel.set()
        self.wake.set()
        self.thread.join(15)
        if self.thread.is_alive():
            raise RuntimeError('chat executor did not quiesce; lease retained')
        self.lease.close()


class NativeExecutor:
    def __init__(self, root, run_task, redact):
        self.root, self.run_task, self.redact = Path(root), run_task, redact
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def recover(self, turn):
        if not re.fullmatch(r'[a-f0-9]{32}', turn['id']):
            return None
        receipt = self.root / 'native' / 'receipts' / (turn['id'] + '.json')
        try:
            descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 32768:
                    return None
                data = json.loads(source.read(32769))
            if (data.get('id') != turn['id'] or data.get('session') != turn['session']
                    or data.get('digest') != hashlib.sha256(turn['text'].encode()).hexdigest()
                    or data.get('completed') is not True or not isinstance(data.get('reply'), str) or not data['reply'].strip()):
                return None
            return self.redact(data['reply'])[:8000]
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def __call__(self, turn, cancel):
        payload = {k: turn[k] for k in ('id', 'session', 'text', 'subject', 'resume', 'recent_tasks', 'persona')}
        payload['root'] = str(self.root / 'native')
        payload['python'] = sys.executable
        source = Path(__file__).with_name('agent_driver.mjs')
        node = os.environ.get('AGENTBRIDGE_AGENT_NODE', str(Path.home() / '.deepseek-harness/bin/node'))
        request = self.root / (turn['id'] + '.request.json')
        fd = os.open(request, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(payload, output, ensure_ascii=False)
        # Electron's audited Node entry point needs the same flag as the DSH launcher.
        command = ['/usr/bin/env', 'ELECTRON_RUN_AS_NODE=1', node, str(source), str(request)]
        try:
            result = self.run_task(command, cwd=turn['subject']['cwd'], timeout=120,
                                   cancel_check=cancel.is_set)
            if result.get('cancelled') and result.get('group_stopped'):
                raise AgentError('CANCELLED', '执行器已停止')
            recovered = self.recover(turn)
            if recovered is not None and result.get('group_stopped', True):
                return recovered
            if result.get('exit_code') != 0 or result.get('timed_out') or result.get('out_truncated'):
                raise AgentError('DRIVER_UNKNOWN', '对话进程未完整返回，不会自动重试')
            data = json.loads(result['stdout'])
            if data.get('id') != turn['id'] or data.get('session') != turn['session'] or data.get('completed') is not True:
                raise AgentError('DRIVER_UNKNOWN', '对话收据不匹配')
            raise AgentError('DRIVER_UNKNOWN', '缺少持久对话收据，不会自动重试')
        finally:
            request.unlink(missing_ok=True)
