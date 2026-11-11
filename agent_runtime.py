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
from agent_character import CharacterMemory


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
    VALUES = {'verbosity': {'brief', 'detailed'}, 'language': {'zh', 'en'},
              'format': {'bullets', 'natural'}, 'workflow': {'test_first', 'backup_first'}}
    LABELS = {'name': '名字', 'language': '语言', 'verbosity': '详略', 'format': '格式', 'workflow': '工作偏好'}
    VALUE_LABELS = {'zh': '中文', 'en': '英文', 'brief': '简短', 'detailed': '详细',
                    'bullets': '分点', 'natural': '自然聊天', 'test_first': '先测试', 'backup_first': '修改前备份'}

    @classmethod
    def summary(cls, category, value):
        return cls.LABELS[category] + '：' + (value if category == 'name' else cls.VALUE_LABELS[value])

    @classmethod
    def direct_change(cls, text):
        # Drafts are not effective preferences, but even a draft must originate
        # in this user's direct long-term request, never quoted/file instructions.
        if (not isinstance(text, str) or not 1 <= len(text) <= 300 or cls.QUOTED.search(text)
                or cls.PRIVATE.search(text) or re.search(r"['‘’「」『』]|读取|阅读|指令|文件.{0,10}(?:说|要求|内容|写着)|instructions?|read\b|he said|she said|quote|document|tool output|translate|summar", text, re.I)
                or re.search(r'(?:不要|别|无需|不用|不必).{0,8}(?:记|设置|修改|保存|改变)|do not|don.t', text, re.I)
                or re.search(r'本轮|这次|本次|这条|仅此|临时|this time|this reply|for now|temporar', text, re.I)):
            return False
        return bool(re.search(r'记住|以后|今后|长期|一直|设置|偏好|忘记|恢复默认|重置名字|叫你|你的名字|给你起名|我喜欢|我希望'
                              r'|remember|from now on|always|preferences?|forget|call you|your name', text, re.I))

    @classmethod
    def validate(cls, category, value):
        if not isinstance(category, str) or not isinstance(value, str):
            raise AgentError('INVALID_MESSAGE', '偏好类别和值必须为文字')
        if category == 'name':
            if isinstance(value, str) and value.strip() and re.fullmatch(r'[\w \-]{1,40}', value, re.UNICODE):
                return
        elif category in cls.VALUES and value in cls.VALUES[category]:
            return
        raise AgentError('INVALID_MESSAGE', '不支持的偏好类别或值。仅支持名字（1至40文字/数字/空格/连字符）、'
                         '语言（zh/en）、详略（brief/detailed）、格式（bullets/natural）、'
                         '工作偏好（test_first/backup_first）。请指定支持的类别和值，不保存自由人设或任意指令。')

    def manual(self, db, owner, category, value, source):
        self.validate(category, value)
        if category == 'name':
            db.execute('INSERT OR REPLACE INTO persona_core VALUES (?,?)', (owner, value))
            db.execute('INSERT OR IGNORE INTO character_name_lock VALUES (?)', (owner,))
            return
        prior = db.execute('SELECT id FROM preferences WHERE owner=? AND category=?', (owner, category)).fetchone()
        db.execute('INSERT INTO preferences VALUES (?,?,?,?,?,?,?,NULL) '
                   'ON CONFLICT(owner,category) DO UPDATE SET value=excluded.value,explicit=1,updated=excluded.updated,'
                   'source=excluded.source,expires=NULL',
                   (prior['id'] if prior else uuid.uuid4().hex, owner, category, value, 1, time.time(), 'manual:' + source))
        db.execute('DELETE FROM preference_forgotten WHERE owner=? AND category=? AND value=?', (owner, category, value))

    def __init__(self, allow_learning=False, allow_personality=False):
        self.allow_learning = allow_learning
        self.character = CharacterMemory(allow_personality)

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
            if old and (old['source'] or '').startswith('manual:'):
                continue
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
        name = core['name'] if core else self.CORE['name']
        learning = self.allow_learning and configured_learning
        items = [{**dict(row), 'summary': self.summary(row['category'], row['value'])} for row in rows]
        display = ('名字：' + name + '；自动学习：' + ('开' if learning else '关')
                   + '。当前仅服务器使用，未同步桌面。\n'
                   + ('\n'.join(item['summary'] + '（ID：' + item['id'] + '）' for item in items) or '暂无有效长期偏好。'))
        personality = self.character.view(db, owner)
        if personality['available'] or personality['traits']:
            display += '\n' + personality['display']
        return {'core': {**self.CORE, 'name': name}, 'learning': learning, 'display': display,
                'personality': personality,
                'learning_available': self.allow_learning, 'preferences': items,
                'forgotten_categories': [row[0] for row in db.execute('SELECT DISTINCT category FROM preference_forgotten WHERE owner=?', (owner,))]}

    def control(self, db, owner, op, memory_id='', name=''):
        if op == 'agent.persona.set':
            self.manual(db, owner, 'name', name, 'persona-command')
        if op == 'agent.persona.reset':
            db.execute('DELETE FROM persona_core WHERE owner=?', (owner,))
            db.execute('INSERT OR IGNORE INTO character_name_lock VALUES (?)', (owner,))
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
    def __init__(self, root, runner, project, evidence=lambda *_: [], recover=lambda *_: None, allow_learning=False, tasks=None,
                 services=None, runtime_info=None, logical_projects=False, allow_personality=False):
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
        self.tasks = tasks
        self.services, self.runtime_info = services, runtime_info
        self.logical_projects = logical_projects
        self.preferences = Preferences(allow_learning, allow_personality)
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
                CREATE TABLE IF NOT EXISTS task_cursors(session TEXT PRIMARY KEY,seq INTEGER);
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
                CREATE TABLE IF NOT EXISTS preference_drafts(
                  id TEXT PRIMARY KEY,owner TEXT,channel TEXT,category TEXT,value TEXT,memory_id TEXT,
                  basis TEXT,expires REAL,state TEXT);
                CREATE TABLE IF NOT EXISTS logical_heads(
                  sender TEXT,channel TEXT,project TEXT,session TEXT,
                  PRIMARY KEY(sender,channel,project));
                CREATE TABLE IF NOT EXISTS message_evidence(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,message TEXT,state TEXT,code TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS session_recoveries(
                  message TEXT PRIMARY KEY,session TEXT,created REAL);
            ''')
            CharacterMemory.initialize(db)
            if self.logical_projects:
                if db.execute('SELECT 1 FROM heads GROUP BY sender,channel,project HAVING count(DISTINCT session)>1 LIMIT 1').fetchone():
                    raise AgentError('MIGRATION_AMBIGUOUS', '旧项目会话头不唯一，停止迁移')
                db.execute('INSERT OR IGNORE INTO logical_heads SELECT sender,channel,project,session FROM heads')
            for row in db.execute("SELECT * FROM messages WHERE state='running'").fetchall():
                restored = recover(dict(row))
                if restored is not None:
                    db.execute("UPDATE messages SET state='done',response=?,ended=? WHERE id=?",
                               (restored, time.time(), row['id']))
                else:
                    db.execute("UPDATE messages SET state='uncertain',ended=? WHERE id=?", (time.time(), row['id']))
                    db.execute('UPDATE sessions SET blocked=1 WHERE id=?', (row['session'],))
                db.execute('INSERT INTO message_evidence(message,state,code,created) VALUES (?,?,?,?)',
                           (row['id'], 'done' if restored is not None else 'uncertain',
                            'RECEIPT_RECOVERED' if restored is not None else 'RESTART_UNKNOWN', time.time()))
            db.execute("UPDATE messages SET state='uncertain',ended=? WHERE state='queued' "
                       "AND session IN (SELECT id FROM sessions WHERE blocked=1)", (time.time(),))
            for session in db.execute('SELECT * FROM sessions WHERE blocked=1').fetchall():
                self._recover_failed(db, session['id'])
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
        if self.logical_projects:
            row = db.execute('SELECT session FROM logical_heads WHERE sender=? AND channel=? AND project=?',
                             (sender, channel, project)).fetchone()
            if row is None:
                # Preserve legacy heads/history. Ambiguous mappings require an
                # explicit migration decision, never silently pick the newest.
                candidates = db.execute('SELECT DISTINCT session FROM heads WHERE sender=? AND channel=? AND project=?',
                                        (sender, channel, project)).fetchall()
                if len(candidates) > 1:
                    raise AgentError('MIGRATION_AMBIGUOUS', '同一逻辑项目有多个旧会话头，须先核对迁移绑定')
                if candidates:
                    row = candidates[0]
                    db.execute('INSERT INTO logical_heads VALUES (?,?,?,?)', (sender, channel, project, row['session']))
        else:
            row = db.execute('SELECT session FROM heads WHERE sender=? AND channel=? AND project=? AND cwd=?',
                             (sender, channel, project, cwd)).fetchone()
        if row and not new:
            return db.execute('SELECT * FROM sessions WHERE id=?', (row['session'],)).fetchone()
        sid = uuid.uuid4().hex
        stat = os.stat(cwd)
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,0)',
                   (sid, sender, channel, project, cwd, stat.st_dev, stat.st_ino))
        db.execute('INSERT OR REPLACE INTO heads VALUES (?,?,?,?,?)', (sender, channel, project, cwd, sid))
        if self.logical_projects:
            db.execute('INSERT OR REPLACE INTO logical_heads VALUES (?,?,?,?)', (sender, channel, project, sid))
        return db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()

    @staticmethod
    def _record_failure(db, row):
        db.execute("UPDATE messages SET state='failed',ended=? WHERE id=?", (time.time(), row['id']))
        db.execute('INSERT INTO message_evidence(message,state,code,created) VALUES (?,?,?,?)',
                   (row['id'], 'failed', 'MODEL_FAILED', time.time()))
        db.execute('INSERT OR IGNORE INTO session_recoveries VALUES (?,?,?)',
                   (row['id'], row['session'], time.time()))

    def _recover_failed(self, db, session):
        recover = getattr(type(self.runner), 'recover_failure', None)
        if not recover:
            return
        rows = db.execute("SELECT * FROM messages WHERE session=? AND state='uncertain'", (session,)).fetchall()
        for row in rows:
            evidence = db.execute('SELECT code FROM message_evidence WHERE message=? ORDER BY seq DESC LIMIT 1',
                                  (row['id'],)).fetchone()
            # Queued inputs quarantined behind an unknown turn were never run.
            if evidence and evidence['code'] in ('DRIVER_UNKNOWN', 'RESTART_UNKNOWN', 'CALL_UNKNOWN'):
                if recover(self.runner, dict(row), legacy=True) is True:
                    self._record_failure(db, row)
        if not db.execute("SELECT 1 FROM messages WHERE session=? AND state IN ('uncertain','cancelled','running')",
                          (session,)).fetchone():
            db.execute('UPDATE sessions SET blocked=0 WHERE id=?', (session,))

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
                self._recover_failed(db, session['id'])
                if db.execute('SELECT blocked FROM sessions WHERE id=?', (session['id'],)).fetchone()[0]:
                    raise AgentError('SESSION_UNCERTAIN', '上一轮结果仍不能确认；请先查询，或用 /agent new 开新会话')
            mid = uuid.uuid4().hex
            db.execute('INSERT INTO messages(id,sender,channel,delivery,digest,session,text,state,created) '
                       "VALUES (?,?,?,?,?,?,?,'queued',?)", (mid, sender, channel, delivery, fingerprint, session['id'], text, time.time()))
            db.execute('INSERT INTO message_evidence(message,state,code,created) VALUES (?,?,?,?)',
                       (mid, 'queued', 'ACCEPTED', time.time()))
            # Learning failure must not roll back the chat admission itself.
            db.execute('SAVEPOINT preference_learning')
            try:
                self.preferences.learn(db, sender, mid, text)
                self.preferences.character.learn(db, sender, mid, text)
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
        evidence = db.execute('SELECT state,code,created FROM message_evidence WHERE message=? ORDER BY seq DESC LIMIT 1',
                              (row['id'],)).fetchone()
        return {'ok': True, 'message_id': row['id'], 'session_id': row['session'],
                'project': session['project'], 'state': row['state'],
                'reply': row['response'] or '', 'reused': reused,
                'completion': 'model_turn_completed' if row['state'] == 'done' else row['state'],
                'evidence': dict(evidence) if evidence else None}

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

    def control(self, sender, channel, op, project, message_id='', delivery='', memory_id='', name='',
                category='', value='', draft_id=''):
        with self.lock:
            result = self._control(sender, channel, op, project, message_id, delivery, memory_id, name, category, value, draft_id)
            if op in ('agent.memory', 'agent.memory.on', 'agent.memory.off', 'agent.forget', 'agent.persona', 'agent.persona.set',
                      'agent.persona.reset', 'agent.preference.set', 'agent.preference.confirm',
                      'agent.personality.on', 'agent.personality.off', 'agent.personality.set', 'agent.personality.forget',
                      'agent.personality.autonomy.on', 'agent.personality.autonomy.off', 'agent.personality.undo'):
                synced = self.export_profile(sender)
                result['desktop_profile_synced'] = synced  # Legacy API field; no desktop installation implied.
                result['profile_snapshot_synced'] = synced
                result['partial_success'] = not synced
                if not synced:
                    result['message'] = (result.get('message', '') + ' ' if result.get('message') else '') + (
                        '服务器偏好读取成功' if op in ('agent.memory', 'agent.persona') else '偏好数据已保存') + (
                        '，但偏好快照同步失败，不能确认全部生效；下一轮从数据库读取最新偏好，不使用旧快照。未同步桌面。')
        return result

    def profile_intent(self, subject, tool, arguments, call_id):
        if tool in ('persona_update', 'personality_update'):
            delivery = 'self-profile-' + hashlib.sha256(json.dumps([subject['attempt'], call_id]).encode()).hexdigest()
            digest = hashlib.sha256(json.dumps([tool, arguments], sort_keys=True).encode()).hexdigest()
            with self.db() as db:
                if not self.preferences.character.autonomous(db, subject['sender']):
                    raise AgentError('AUTH_FAIL', '名字和性格自主管理未开启或已经冻结')
                old = db.execute('SELECT * FROM controls WHERE sender=? AND channel=? AND delivery=?',
                                 (subject['sender'], subject['channel'], delivery)).fetchone()
                if old and old['digest'] != digest:
                    raise AgentError('DELIVERY_CONFLICT', '自主修改调用已经绑定其他内容')
                changed = False
                if old is None:
                    try:
                        changed = self.preferences.character.self_update(db, subject['sender'], subject['attempt'], tool, arguments)
                    except ValueError as error:
                        raise AgentError('INVALID_MESSAGE', str(error)) from None
                    db.execute('INSERT INTO controls VALUES (?,?,?,?,?)', (subject['sender'], subject['channel'], delivery, digest, '{}'))
                result = {'ok': True, 'reused': old is not None, 'profile_changed': changed,
                          **self.preferences.view(db, subject['sender'])}
            result['profile_snapshot_synced'] = self.export_profile(subject['sender'])
            result['partial_success'] = not result['profile_snapshot_synced']
            return result
        allowed = {'preference_view': set(), 'preference_draft': {'category', 'value'},
                   'preference_forget_draft': {'memory_id'}}
        if tool not in allowed or not isinstance(arguments, dict) or set(arguments) != allowed[tool]:
            raise AgentError('INVALID_MESSAGE', '偏好只支持查看或创建确认草稿')
        if any(not isinstance(v, str) or len(v) > 128 for v in arguments.values()):
            raise AgentError('INVALID_MESSAGE', '偏好参数无效')
        delivery = 'profile-' + hashlib.sha256(json.dumps([subject['attempt'], call_id]).encode()).hexdigest()
        return self.control(subject['sender'], subject['channel'],
                            'agent.memory' if tool == 'preference_view' else 'agent.preference.draft', subject['project'],
                            delivery=delivery, **arguments)

    def _preference_basis(self, db, sender, category):
        if category == 'name':
            row = db.execute('SELECT name FROM persona_core WHERE owner=?', (sender,)).fetchone()
        else:
            row = db.execute('SELECT id,value,updated,source FROM preferences WHERE owner=? AND category=?', (sender, category)).fetchone()
        return json.dumps(dict(row) if row else None, sort_keys=True, ensure_ascii=False)

    def _preference_control(self, db, sender, channel, op, delivery, memory_id, category, value, draft_id):
        required(delivery, 128)
        fingerprint = hashlib.sha256(json.dumps([op, memory_id, category, value, draft_id]).encode()).hexdigest()
        old = db.execute('SELECT * FROM controls WHERE sender=? AND channel=? AND delivery=?', (sender, channel, delivery)).fetchone()
        if old:
            if old['digest'] != fingerprint:
                raise AgentError('DELIVERY_CONFLICT', '偏好投递已绑定其他变更')
            previous = json.loads(old['response'])
            if 'preference_draft' not in previous:
                return {'ok': True, 'reused': True, **self.preferences.view(db, sender),
                        'message': '此前变更已处理，没有重新应用；此处为当前偏好。'}
            row = db.execute('SELECT state,expires FROM preference_drafts WHERE id=?', (previous['preference_draft']['id'],)).fetchone()
            previous['preference_draft']['state'] = ('expired' if row and row['state'] == 'pending' and row['expires'] <= time.time()
                                                      else row['state'] if row else 'unknown')
            previous['message'] = '原草稿收据，当前状态：' + previous['preference_draft']['state'] + '；没有重新应用变更。'
            return {**previous, 'reused': True}
        if op == 'agent.preference.confirm':
            row = db.execute('SELECT * FROM preference_drafts WHERE id=? AND owner=? AND channel=?', (draft_id, sender, channel)).fetchone()
            if row is None or row['state'] != 'pending' or row['expires'] <= time.time():
                raise AgentError('INVALID_MESSAGE', '偏好草稿不存在、来源不匹配或已过期')
            if self._preference_basis(db, sender, row['category']) != row['basis']:
                raise AgentError('DELIVERY_CONFLICT', '原偏好已变化，请重新生成确认摘要')
            if row['memory_id']:
                self.preferences.control(db, sender, 'agent.forget', row['memory_id'])
            else:
                self.preferences.manual(db, sender, row['category'], row['value'], delivery)
            db.execute("UPDATE preference_drafts SET state='applied' WHERE id=?", (draft_id,))
            result = {'ok': True, **self.preferences.view(db, sender),
                      'message': ('已停止后续注入该偏好，没有删除原始聊天记录。' if row['memory_id'] else '偏好已确认。')
                                 + '当前仅服务器使用，不共享完整聊天历史或同步桌面。'}
        elif op == 'agent.preference.set':
            self.preferences.manual(db, sender, category, value, delivery)
            result = {'ok': True, **self.preferences.view(db, sender)}
        elif op == 'agent.preference.draft':
            if memory_id:
                row = db.execute('SELECT * FROM preferences WHERE id=? AND owner=?', (memory_id, sender)).fetchone()
                if row is None:
                    raise AgentError('NOT_FOUND', '偏好 ID 不存在或不属于当前发送者')
                category, value = row['category'], row['value']
            else:
                self.preferences.validate(category, value)
            count = db.execute("SELECT count(*) FROM preference_drafts WHERE owner=? AND channel=? AND state='pending' AND expires>?",
                               (sender, channel, time.time())).fetchone()[0]
            if count >= 5:
                raise AgentError('BUSY', '最多五个待确认偏好草稿')
            identifier, expires = uuid.uuid4().hex, time.time() + 600
            db.execute("INSERT INTO preference_drafts VALUES (?,?,?,?,?,?,?,?,'pending')",
                       (identifier, sender, channel, category, value, memory_id, self._preference_basis(db, sender, category), expires))
            result = {'ok': True, 'preference_draft': {'id': identifier, 'category': category, 'value': value, 'state': 'pending',
                                                     'action': 'forget' if memory_id else 'set', 'expires': expires,
                                                     'summary': ('忘记' if memory_id else '设置') + self.preferences.summary(category, value)},
                      'message': ('忘记' if memory_id else '设置') + self.preferences.summary(category, value)
                                 + '。偏好尚未变更，核对后单独发送：\n/agent preference confirm ' + identifier}
        else:
            raise AgentError('INVALID_MESSAGE', '不支持的偏好操作')
        db.execute('INSERT INTO controls VALUES (?,?,?,?,?)', (sender, channel, delivery, fingerprint, json.dumps(result, ensure_ascii=False)))
        return result

    def current_conversation(self, sender, channel, project):
        cwd = os.path.realpath(self.project(project))
        with self.db() as db:
            row = (db.execute('SELECT session FROM logical_heads WHERE sender=? AND channel=? AND project=?',
                              (sender, channel, project)).fetchone() if self.logical_projects else
                   db.execute('SELECT session FROM heads WHERE sender=? AND channel=? AND project=? AND cwd=?',
                              (sender, channel, project, cwd)).fetchone())
            if row is None:
                raise AgentError('NOT_FOUND', '当前项目尚无对话会话')
            return row['session']

    def _control(self, sender, channel, op, project, message_id='', delivery='', memory_id='', name='',
                 category='', value='', draft_id=''):
        required(sender); required(channel)
        if op in ('agent.personality', 'agent.personality.on', 'agent.personality.off', 'agent.personality.set', 'agent.personality.forget',
                  'agent.personality.autonomy.on', 'agent.personality.autonomy.off', 'agent.personality.undo'):
            with self.db() as db:
                character = self.preferences.character
                if op == 'agent.personality':
                    return {'ok': True, 'personality': character.view(db, sender), 'display': character.view(db, sender)['display']}
                required(delivery, 128)
                digest = hashlib.sha256(json.dumps([op, memory_id, value]).encode()).hexdigest()
                old = db.execute('SELECT digest FROM controls WHERE sender=? AND channel=? AND delivery=?', (sender, channel, delivery)).fetchone()
                if old and old['digest'] != digest:
                    raise AgentError('DELIVERY_CONFLICT', '性格控制投递已绑定其他变更')
                if old is None:
                    try:
                        if op == 'agent.personality.undo':
                            character.undo(db, sender, memory_id, 'control:' + delivery)
                        elif op in ('agent.personality.autonomy.on', 'agent.personality.autonomy.off'):
                            if op.endswith('.on') and not character.available:
                                raise ValueError('性格成长尚未由宿主启用')
                            db.execute('INSERT OR REPLACE INTO character_autonomy VALUES (?,?)', (sender, int(op.endswith('.on'))))
                            if op.endswith('.on'):
                                db.execute('DELETE FROM character_name_lock WHERE owner=?', (sender,))
                        elif op.endswith('.on'):
                            if not character.available:
                                raise ValueError('性格成长尚未由宿主启用')
                            db.execute('INSERT OR REPLACE INTO character_policy VALUES (?,1)', (sender,))
                        elif op.endswith('.off'):
                            db.execute('INSERT OR REPLACE INTO character_policy VALUES (?,0)', (sender,))
                            db.execute('INSERT OR REPLACE INTO character_autonomy VALUES (?,0)', (sender,))
                        elif op.endswith('.set'):
                            character.set(db, sender, value, 'control:' + delivery, memory_id)
                        else:
                            character.forget(db, sender, memory_id, 'control:' + delivery)
                    except ValueError as error:
                        raise AgentError('INVALID_MESSAGE', str(error)) from None
                    db.execute('INSERT INTO controls VALUES (?,?,?,?,?)', (sender, channel, delivery, digest, '{}'))
                result = character.view(db, sender)
                return {'ok': True, 'reused': old is not None, 'personality': result, 'display': result['display']}
        if op in ('agent.preference.set', 'agent.preference.draft', 'agent.preference.confirm'):
            with self.db() as db:
                return self._preference_control(db, sender, channel, op, delivery, memory_id, category, value, draft_id)
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
            if op in ('agent.memory', 'agent.memory.on', 'agent.memory.off', 'agent.forget', 'agent.persona', 'agent.persona.set', 'agent.persona.reset'):
                if op in ('agent.memory.on', 'agent.memory.off', 'agent.forget', 'agent.persona.set', 'agent.persona.reset'):
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
                    db.execute('INSERT INTO message_evidence(message,state,code,created) VALUES (?,?,?,?)',
                               (row['id'], 'running', 'CALL_INTENT', time.time()))
                    turn = {**dict(row), 'subject': {k: session[k] for k in ('sender', 'channel', 'project', 'cwd')},
                            'resume': db.execute("SELECT 1 FROM messages WHERE session=? AND state='done' LIMIT 1", (row['session'],)).fetchone() is not None}
                    turn['subject'].update(conversation=session['id'], session=session['id'], attempt=row['id'])
                    turn['recoveries'] = [item[0] for item in db.execute(
                        'SELECT message FROM session_recoveries WHERE session=? ORDER BY created,message', (session['id'],))]
                    turn['resume'] = turn['resume'] or bool(turn['recoveries'])
                    from agent_services import reminder_context
                    turn['reminder_context'] = reminder_context(db, dict(row))
                    turn['persona'] = self.preferences.view(db, session['sender'])
                    cursor = db.execute('SELECT seq FROM task_cursors WHERE session=?', (session['id'],)).fetchone()
                    turn['task_cursor'] = cursor['seq'] if cursor else 0
            if not row:
                self.wake.wait(.25); self.wake.clear()
                continue
            state, reply, code = 'uncertain', '', 'CALL_UNKNOWN'
            try:
                try:
                    turn['runtime_info'] = self.runtime_info() if self.runtime_info else {'evidence': 'runtime release not supplied'}
                except Exception:
                    turn['runtime_info'] = {'evidence': 'host snapshot unavailable; do not infer release from documents'}
                cwd = os.path.realpath(self.project(session['project']))
                stat = os.stat(cwd)
                if not self.logical_projects and (cwd != session['cwd'] or (stat.st_dev, stat.st_ino) != (session['device'], session['inode'])):
                    raise AgentError('PROJECT_CHANGED', '项目目录已改变')
                if self.logical_projects:
                    turn['subject'].update(cwd=cwd, origin_cwd=session['cwd'])
                turn['recent_tasks'] = self.evidence(session['sender'], session['channel'], session['project'])
                if self.tasks is not None:
                    turn['task_events'] = self.tasks.events(session['sender'], session['channel'], session['id'], session['project'],
                                                           after=turn['task_cursor'])['events']
                reply = self.runner(turn, cancel)
                if not isinstance(reply, str) or not reply.strip():
                    raise AgentError('EMPTY_REPLY', '对话没有完整返回')
                state = 'done'
                code = 'RECEIPT_COMPLETED'
            except AgentError as exc:
                code = exc.code if re.fullmatch('[A-Z_]{1,64}', exc.code) else 'CALL_UNKNOWN'
                if exc.code == 'CANCELLED':
                    state = 'cancelled'
                elif exc.code in ('PROJECT_CHANGED', 'NOT_STARTED', 'MODEL_FAILED'):
                    state = 'failed'
            except Exception:
                pass
            with self.db() as db:
                db.execute('UPDATE messages SET state=?,response=?,ended=? WHERE id=?', (state, reply, time.time(), row['id']))
                db.execute('INSERT INTO message_evidence(message,state,code,created) VALUES (?,?,?,?)',
                           (row['id'], state, code, time.time()))
                if state == 'failed' and code == 'MODEL_FAILED':
                    db.execute('INSERT OR IGNORE INTO session_recoveries VALUES (?,?,?)',
                               (row['id'], session['id'], time.time()))
                if state == 'done' and turn.get('task_events'):
                    db.execute('INSERT OR REPLACE INTO task_cursors VALUES (?,?)',
                               (session['id'], max(event['seq'] for event in turn['task_events'])))
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
        if self.services is not None:
            self.services.close()


class NativeExecutor:
    def __init__(self, root, run_task, redact, tasks=None, services=None, file_reads=True, remote=None, snapshots=None):
        if type(file_reads) is not bool:
            raise ValueError('file_reads must be an explicit boolean')
        self.root, self.run_task, self.redact = Path(root).resolve(), run_task, redact
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.tasks = tasks
        self.services = services
        self.file_reads = file_reads
        self.remote = remote
        self.snapshots = snapshots
        from agent_diagnostics import Diagnostics
        self.diagnostics = Diagnostics(self.root.parent / 'diagnostics')

    @staticmethod
    def _private_bytes(path, limit):
        # Refuse symlinks in every component, not only the final file.
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError('symlink in evidence path')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > limit):
                raise ValueError('unsafe evidence file')
            return source.read(limit + 1)

    def _persist_process_record(self, turn, kind, result=None):
        directory = self.root / 'native' / kind
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = {key: turn[key] for key in ('id', 'session')}
        value.update(digest=hashlib.sha256(turn['text'].encode()).hexdigest())
        if result is not None:
            value.update({key: result.get(key) for key in ('group_stopped', 'timed_out', 'cancelled', 'exit_code')})
        temporary = directory / (turn['id'] + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, directory / (turn['id'] + '.json'))
        temporary.unlink()
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _native_failure(self, turn):
        paths = list((self.root / 'native' / 'sessions').glob('*/' + turn['session'] + '/session.jsonl'))
        if len(paths) != 1:
            return None
        raw = self._private_bytes(paths[0], 16 * 1024 * 1024)
        if not raw.endswith(b'\n'):
            return None
        records = [json.loads(line) for line in raw.splitlines()]
        header, events = records[0], records[1:]
        if (header.get('type') != 'session' or header.get('version') != 0
                or header.get('id') != turn['session'] or header.get('seedLength', 0) != 0):
            return None
        # Require a complete, ordered native log, including packed chunk runs.
        seq = 0
        inbox = {'next-turn': [], 'next-step': []}
        for event in events:
            if event['type'] in ('text-chunks', 'reasoning-chunks', 'tool-call-chunks'):
                if event['seq0'] != seq:
                    return None
                key = 'args' if event['type'] == 'tool-call-chunks' else 'texts'
                seq += len(event['data'][key])
            else:
                if event['seq'] != seq:
                    return None
                seq += 1
            if event['type'] == 'agent/inbox/spliced':
                data = event['data']
                pending = inbox[data['target']]
                start, count = data['start'], data.get('removedCount', 0)
                if (type(start) is not int or type(count) is not int or start < 0 or count < 0
                        or start + count > len(pending) or not isinstance(data['inserted'], list)):
                    return None
                pending[start:start + count] = data['inserted']
        if any(inbox.values()) or not events or events[-1]['type'] != 'turn/end':
            return None
        end = events[-1]
        reason = end['data']['reason']
        error = reason.get('error', {})
        status, code = error.get('status'), error.get('code')
        expected = {'SERVER': range(500, 600), 'AUTH': (401, 403), 'RATE_LIMIT': (429,), 'INVALID_REQUEST': (400, 413)}
        if reason.get('kind') != 'error' or code not in expected or type(status) is not int or status not in expected[code]:
            return None
        starts = [index for index, event in enumerate(events) if event['type'] == 'turn/start']
        if not starts:
            return None
        tail = events[starts[-1]:]
        if (tail[0]['data']['turn'] != end['data']['turn'] or tail[-2]['type'] != 'step/end'
                or tail[-2]['data']['turn'] != end['data']['turn']):
            return None
        users = []
        for event in tail:
            if event['type'].startswith('tool/') or event['type'] == 'tool-call-chunks':
                return None
            if event['type'] == 'llm/chunk' and ('tool-call' in json.dumps(event['data'].get('chunk', {}))):
                return None
            if event['type'] == 'assistant/message' and any(
                    block['type'] == 'tool-call' for block in event['data']['message']['content']):
                return None
            if event['type'] == 'user/message' and event['data']['source']['kind'] == 'user':
                users.append(event['data'])
        if len(users) != 1 or users[0]['content'] != [{'type': 'text', 'text': turn['text']}]:
            return None
        user_event = next(event for event in tail if event['type'] == 'user/message'
                          and event['data']['source']['kind'] == 'user')
        if (turn.get('created') is not None and user_event['time'] < turn['created'] * 1000
                or turn.get('ended') is not None and end['time'] > turn['ended'] * 1000):
            return None
        return {'turn': end['data']['turn'], 'end_seq': end['seq'], 'error_code': code, 'http_status': status}

    def recover_failure(self, turn, legacy=False):
        """Read-only reconciliation. Never resume a native agent to inspect it."""
        try:
            if not all(re.fullmatch('[a-f0-9]{32}', turn[key]) for key in ('id', 'session')):
                return False
            digest = hashlib.sha256(turn['text'].encode()).hexdigest()
            if turn.get('digest', digest) != digest:
                return False
            native = self._native_failure(turn)
            if not native:
                return False
            success = self.root / 'native/receipts' / (turn['id'] + '.json')
            if os.path.lexists(success):
                return False
            failure = self.root / 'native/failures' / (turn['id'] + '.json')
            if os.path.lexists(failure):
                data = json.loads(self._private_bytes(failure, 32768))
                if (any(data.get(key) != value for key, value in native.items())
                        or data.get('id') != turn['id'] or data.get('session') != turn['session']
                        or data.get('digest') != digest or data.get('failed') is not True
                        or data.get('side_effect_attempted') is not False or data.get('inbox_pending') is not False
                        or data.get('stage') != 'turn/end'):
                    return False
            elif not legacy or os.path.lexists(self.root / 'native/attempts' / (turn['id'] + '.json')):
                return False
            stopped = self.root / 'native/stopped' / (turn['id'] + '.json')
            if os.path.lexists(stopped):
                data = json.loads(self._private_bytes(stopped, 32768))
                return (data.get('id') == turn['id'] and data.get('session') == turn['session']
                        and data.get('digest') == digest and data.get('group_stopped') is True
                        and data.get('cancelled') is False and data.get('timed_out') is False
                        and type(data.get('exit_code')) is int and data['exit_code'] == 1)
            if not legacy:
                return False
            # Old executors have no stopped receipt. Require their exact
            # supervisor terminal evidence, with no later start/conflict.
            matches = []
            for path in sorted(self.diagnostics.root.glob('diagnostic-????????.jsonl')):
                raw = self._private_bytes(path, 16 * 1024 * 1024)
                if not raw.endswith(b'\n'):
                    return False
                matches.extend(event for event in map(json.loads, raw.splitlines())
                               if event.get('correlation') == turn['id'] and event.get('stage') == 'executor')
            return (len(matches) == 2 and matches[0]['code'] == 'START' and matches[1]['code'] == 'UNKNOWN'
                    and matches[1].get('process') == {'exit_code': 1, 'group_stopped': True,
                                                     'cancelled': False, 'timed_out': False})
        except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError):
            return False

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
        payload['runtime_info'] = turn.get('runtime_info', {'evidence': 'runtime release not supplied'})
        payload['recoveries'] = turn.get('recoveries', [])
        from agent_reads import LEGACY_SCOPE, SOURCE_FILES
        payload['read_scope'] = {'scope': LEGACY_SCOPE, 'files': sorted(SOURCE_FILES)}
        if not self.file_reads:
            payload['read_mode'] = 'none'
        if self.remote is not None and self.remote.projects.target(turn['subject']['project']) == 'mac':
            payload['read_mode'] = 'remote'
            payload['remote_reads'] = self.remote.issue('chat', turn['subject'], cancel=cancel)
        snapshot = self.snapshots(turn['subject']['project']) if self.snapshots else None
        if snapshot:
            payload.update(read_mode='snapshot', read_root=snapshot[0], source_snapshot=snapshot[1])
            payload['read_scope'] = {'scope': snapshot[1]['scope'], 'files': sorted(snapshot[1]['files'])}
        if self.services is not None:
            payload['services'] = self.services.issue(turn['subject'], cancel, turn['text'],
                                                     **({'reminder_context': turn['reminder_context']} if turn.get('reminder_context') else {}))
        if self.tasks is not None:
            payload['task_events'] = turn.get('task_events', [])
            if self.tasks.can_submit(turn['subject']['project']):
                payload['broker'] = self.tasks.issue('chat', turn['subject'], cancel=cancel)
        for key in ('broker', 'remote_reads', 'services'):
            if payload.get(key):
                payload[key] = {**payload[key], 'diagnostics': str(self.diagnostics.root), 'correlation': turn['id']}
        source = Path(__file__).with_name('agent_driver.mjs')
        node = os.environ.get('AGENTBRIDGE_AGENT_NODE', str(Path.home() / '.deepseek-harness/bin/node'))
        request = self.root / (turn['id'] + '.request.json')
        fd = os.open(request, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(payload, output, ensure_ascii=False)
        # Electron's audited Node entry point needs the same flag as the DSH launcher.
        command = ['/usr/bin/env', 'ELECTRON_RUN_AS_NODE=1', node, str(source), str(request)]
        try:
            from process_supervisor import ProcessStartError
            try:
                self._persist_process_record(turn, 'attempts')
                self.diagnostics.emit('executor', turn['id'], 'START')
                result = self.run_task(command, cwd=turn['subject']['cwd'], timeout=120,
                                       cancel_check=cancel.is_set)
                code = ('CANCELLED' if result.get('cancelled') else 'TIMEOUT' if result.get('timed_out') else
                        'OK' if result.get('exit_code') == 0 else 'UNKNOWN')
                self.diagnostics.emit('executor', turn['id'], code, process=result)
            except ProcessStartError:
                raise AgentError('NOT_STARTED', '后端进程未启动，无自动重试') from None
            if result.get('cancelled') and result.get('group_stopped'):
                raise AgentError('CANCELLED', '执行器已停止')
            recovered = self.recover(turn)
            self.diagnostics.emit('receipt', turn['id'], 'OK' if recovered is not None else 'RECEIPT_MISSING')
            if recovered is not None and result.get('group_stopped', True):
                return recovered
            try:
                self._persist_process_record(turn, 'stopped', result)
            except (OSError, ValueError):
                raise AgentError('DRIVER_UNKNOWN', '缺少持久进程停止证据') from None
            if (result.get('group_stopped') is True and result.get('cancelled') is False
                    and result.get('timed_out') is False and result.get('exit_code') == 1
                    and self.recover_failure(turn)):
                raise AgentError('MODEL_FAILED', '刚才模型服务出了点问题，那条没完成，你可以继续说')
            if result.get('exit_code') != 0 or result.get('timed_out') or result.get('out_truncated'):
                raise AgentError('DRIVER_UNKNOWN', '对话进程未完整返回，不会自动重试')
            data = json.loads(result['stdout'])
            if data.get('id') != turn['id'] or data.get('session') != turn['session'] or data.get('completed') is not True:
                raise AgentError('DRIVER_UNKNOWN', '对话收据不匹配')
            raise AgentError('DRIVER_UNKNOWN', '缺少持久对话收据，不会自动重试')
        finally:
            if payload.get('broker'):
                self.tasks.revoke_capability(payload['broker']['capability'])
            if payload.get('services'):
                self.services.revoke_capability(payload['services']['capability'])
            if payload.get('remote_reads'):
                self.remote.revoke_capability(payload['remote_reads']['capability'])
            request.unlink(missing_ok=True)
