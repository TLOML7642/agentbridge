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
    'reminder_set': ({'due_at', 'content'}, {'timezone', 'reminder_id'}),
    'reminder_list': (set(), set()), 'reminder_get': ({'reminder_id'}, set()),
    'reminder_cancel': ({'reminder_id'}, set()),
}
REMINDER_ERRORS = frozenset(('REMINDER_INTENT_REQUIRED', 'REMINDER_RECURRING_UNSUPPORTED', 'REMINDER_FOLLOWUP_CONFLICT'))


def reminder_retry(text):
    if not isinstance(text, str) or len(text) > 80:
        return False
    text = text.strip()
    maintenance = re.match(r'^我(?:刚刚|刚才|已经)?(?:修复|修改|调整|更新|处理|改动|优化|检查|完善|修|改)'
                           r'(?:了)?(?:一下)?[^\W\d_]{1,8}(?:了)?[，,。\s]+', text, re.I)
    if maintenance and not re.search(r'审批|权限|授权|提醒|闹钟|文件|指令|工具|密码|密钥', maintenance[0]):
        text = text[maintenance.end():]
    return ('试' in text and re.fullmatch(r'(?:你|请|麻烦|帮我|现在|再|重新|试试|试一下|试下|试一次|试试看|试看|重试|一下|一次|看看|看|吧|呢|啊|呀|好|了|[。！!？?，,\s])+', text) is not None)


def reminder_context(db, message, now=None):
    """Only host-owned direct user messages in this exact recent conversation."""
    if not reminder_retry(message['text']):
        return None
    now = time.time() if now is None else now
    rows = db.execute('SELECT id,text,state,created FROM messages WHERE sender=? AND channel=? AND session=? AND seq<? '
                      'ORDER BY seq DESC LIMIT 6', (message['sender'], message['channel'], message['session'], message['seq'])).fetchall()
    for row in rows:
        if row['state'] != 'done' or not 0 <= now - row['created'] <= 7200:
            return None
        direct, recurring = reminder_set_intent(row['text'])
        if direct:
            return {'attempt': row['id'], 'direct': True, 'recurring': recurring}
        # Only fixed retry/acknowledgement and a narrow workday-definition
        # question may intervene. Unrelated turns, cancellations and quoted
        # instructions break the chain rather than revive an old request.
        if not (reminder_retry(row['text']) or re.fullmatch(r'(?:好|好的|行|嗯|收到|知道了|(?:我)?先看看怎么回事吧?)[。！!]?|'
                r'(?:你)?(?:知道|明白|懂)(?:什么叫|什么是)?工作日(?:吧|吗|么)?[？?。]?', row['text'].strip())):
            return None
    return None


def reminder_set_intent(text):
    """Recognize direct one-shot requests; never silently expand recurrence."""
    if (not isinstance(text, str) or not 1 <= len(text) <= 1400
            or re.search(r'[\n>“”"`]|引用|转发|https?://', text)):
        return False, False
    text = text.strip()
    marker = re.search(r'提醒(?:一下|下)?我|remind me\b|提醒|闹钟', text, re.I)
    if not marker:
        return False, False
    prefix = text[:marker.start()]
    if re.search(r'不要|别|无需|不用|不必|取消|撤销|是否|怎么|如何|能否|能不能|看看|了解|知道|解释|explain|do not|don.t|'
                 r'阅读|读取|文档|文件|指令|工具|read\b|instructions?\b', prefix, re.I):
        return False, False
    direct = bool(re.match(
        r'^(?:请|请你|帮我|麻烦)?(?:[^。；！？?]{0,60}(?:提醒(?:一下|下)?我|remind me\b)|'
        r'(?:把|将|修改|更改|调整).{0,60}提醒)', text, re.I))
    alarm = bool(re.match(r'^(?:请你|请|麻烦你|麻烦)?(?:帮我|给我|为我)?(?:设|设置|定|订)(?:置)?(?:一)?(?:个|一个)?'
                         r'[^。；！？?]{0,60}(?:闹钟|提醒)', text))
    timed = bool(re.match(r'^(?:请你|请|帮我|麻烦)?(?:在)?(?:今天|明天|后天|今晚|早上|上午|下午|晚上|'
                         r'\d{1,4}(?:月|日|号|[:：点分时])|每(?:个)?工作日|工作日都|每天|每日|每周)'
                         r'[^。；！？?]{0,60}(?:提醒|闹钟)', text))
    direct = direct or alarm or timed
    cadence = r'每(?:个)?工作日|工作日都|每天|每日|每晚|每早|每周|每星期|每月|循环|重复|周一[至到-]周五|every\s+(?:day|week|weekday)'
    recurring = direct and bool(re.search(cadence, prefix, re.I) or re.search(
        r'(?:提醒(?:一下|下)?我?|remind me|闹钟)[,，:：\s]*(?:在)?(?:' + cadence + ')', text, re.I))
    return direct, recurring


def reminder_dialogue_allowed(text):
    """Source gate, not an NLP command grammar. The existing model interprets
    the user's dialogue and asks about ambiguity before calling reminder_set.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 12000 or '\0' in text:
        return False
    marker = re.search(r'提醒|闹钟|remind me\b', text, re.I)
    prefix = text[:marker.start()] if marker else text
    head = text[:marker.end()] if marker else text
    if re.search(r'^\s*[>“"「]|(?:^|\n)\s*>|```|工具输出|引用|转发', text):
        return False
    if re.search(r'他说|她说|它说|阅读|读取|(?:文档|文件|网页).{0,8}(?:里|中|内|指令|要求|说|写)|'
                 r'https?://|read\b|instructions?\b|执行.{0,8}命令|取消|撤销|是否|怎么|如何|看看|了解|知道|解释|什么|explain', prefix, re.I):
        return False
    if re.search(r'(?:关闭|取消|跳过|绕过|修改).{0,8}(?:审批|权限|授权)', prefix):
        return False
    if re.search(r'(?:不要|无需|不用|不必|别)(?:再|给我|帮我|设置|任何|这个|这条|定|设){0,3}(?:提醒|闹钟|设|定)|do not|don.t', head, re.I):
        return False
    if re.search(r'(?:不要|别|不用|无需|不必).{0,6}(?:试|重试)', head):
        return False
    return True


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
                except AgentError as error:
                    result = {'ok': False, 'code': error.code if error.code in REMINDER_ERRORS else 'SERVICE_DENIED'}
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

    def issue(self, subject, cancel, direct_text='', reminder_context=None):
        with self.lock:
            if self.closed:
                raise AgentError('UNAVAILABLE', '个人服务已停止')
            for key in ('sender', 'channel', 'conversation', 'project', 'attempt'):
                required(subject.get(key))
            token = secrets.token_hex(32)
            # Immediate service effects require this turn's direct user intent,
            # not a model decision based on a file or tool output.
            cancel_allowed = (isinstance(direct_text, str) and len(direct_text) <= 300
                              and not re.search(r'[\n>“”"`]|文档|文件|引用|转发|https?://', direct_text)
                              and bool(re.search(r'(?:取消|撤销|删除|cancel).*(?:提醒|remind)|(?:提醒|remind).*(?:取消|撤销|删除|cancel)', direct_text, re.I)))
            explicit, recurring = reminder_set_intent(direct_text)
            set_allowed = reminder_dialogue_allowed(direct_text)
            origin = None
            if reminder_retry(direct_text):
                set_allowed = False
                if reminder_context is not None:
                    required(reminder_context.get('attempt'), 128)
                    set_allowed = reminder_context.get('direct') is True
                    recurring = reminder_context.get('recurring') is True
                    origin = reminder_context['attempt'] if set_allowed else None
            from agent_runtime import Preferences
            self.capabilities[token] = (dict(subject), cancel, time.monotonic() + 120, cancel_allowed, set_allowed,
                                        Preferences.direct_change(direct_text), recurring, origin)
            return {'socket': self.socket_path, 'capability': token, 'purpose': 'personal-service',
                    'reminder_intent': 'recurring_unsupported' if recurring else 'followup_one_shot' if origin else
                                       'direct_one_shot' if set_allowed and explicit else 'dialogue' if set_allowed else 'missing'}

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
                if payload['tool'] not in ('preference_view', 'persona_update', 'personality_update') and not cap[5]:
                    raise AgentError('AUTH_FAIL', '请直接说明要长期设置或忘记的偏好；临时要求、引用或文件指令不能生成偏好草稿')
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
            if tool == 'reminder_set':
                if cap[6]:
                    raise AgentError('REMINDER_RECURRING_UNSUPPORTED', '目前只支持单次微信消息提醒，不支持工作日/每天循环；不能擅自拆成多个日期。')
                if not cap[4]:
                    raise AgentError('REMINDER_INTENT_REQUIRED', '当前来源或接续范围未能安全确定；只追问缺少的信息，不要求固定口令。')
            if (not isinstance(tool, str) or tool not in SERVICE_FIELDS or not isinstance(args, dict)
                    or not SERVICE_FIELDS[tool][0] <= args.keys() or args.keys() - set.union(*SERVICE_FIELDS[tool])
                    or any(not isinstance(v, str) or len(v) > 1000 or '\0' in v for v in args.values())):
                raise AgentError('INVALID_MESSAGE', '个人服务参数无效，不允许指定来源/接收者或执行动作')
            encoded = json.dumps(args, sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(json.dumps([subject, tool, encoded], sort_keys=True).encode()).hexdigest()
            identifier = hashlib.sha256(json.dumps([subject['attempt'], call]).encode()).hexdigest()
            with self.db() as db:
                if tool == 'reminder_set' and cap[7]:
                    previous = db.execute('SELECT * FROM service_requests WHERE attempt=? AND sender=? AND channel=? '
                                          'AND conversation=? AND project=? AND tool=?',
                                          (cap[7], subject['sender'], subject['channel'], subject['conversation'], subject['project'], tool)).fetchall()
                    if not previous:
                        raise AgentError('REMINDER_FOLLOWUP_CONFLICT', '原单次参数尚未持久绑定，不能猜测原时间或内容；请明确这一次的时间和内容。')
                    if len(previous) != 1 or previous[0]['arguments'] != encoded:
                        raise AgentError('REMINDER_FOLLOWUP_CONFLICT', '原单次请求已绑定参数或不唯一；请查询，不自动换日期、改内容或新建。')
                    self._expire(db, self.clock())
                    old = db.execute('SELECT * FROM service_requests WHERE id=?', (previous[0]['id'],)).fetchone()
                    return {'ok': True, 'request_id': old['id'], 'state': old['state'], 'reused': True,
                            'result': json.loads(old['result']) if old['result'] else None}
                if tool == 'reminder_set' and not args.get('reminder_id'):
                    same = db.execute('SELECT * FROM service_requests WHERE sender=? AND channel=? AND conversation=? '
                                      'AND project=? AND tool=? AND arguments=? AND expires>? ORDER BY seq DESC LIMIT 2',
                                      (subject['sender'], subject['channel'], subject['conversation'], subject['project'],
                                       tool, encoded, self.clock() - 7200)).fetchall()
                    if len(same) > 1:
                        raise AgentError('REMINDER_FOLLOWUP_CONFLICT', '存在多条相同原请求，请先查询，不自动增加副本。')
                    if same:
                        self._expire(db, self.clock())
                        old = db.execute('SELECT * FROM service_requests WHERE id=?', (same[0]['id'],)).fetchone()
                        return {'ok': True, 'request_id': old['id'], 'state': old['state'], 'reused': True,
                                'result': json.loads(old['result']) if old['result'] else None}
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
