"""Bounded, user-owned character memories. No model, file or tool extraction."""
import hashlib
import re
import time
import unicodedata
import uuid


class CharacterMemory:
    LIMIT = 12
    MAX_TEXT = 120
    PRIVATE = re.compile(r'sk-|password|passwd|token|secret|api.?key|密钥|密码|验证码|身份证|银行卡|手机号|健康|疾病|医院|病情|\d{5,}', re.I)
    UNSAFE = re.compile(r'https?://|[\n\r\x00-\x1f<>`]|系统(?:提示|指令)|(?:忽略|绕过|跳过|取消|关闭).{0,8}'
                        r'(?:规则|权限|审批|确认|安全|限制)|(?:读取|写入|删除|执行|运行|上传|下载|发送|转账|提醒|授权|密钥|shell|sudo)'
                        r'|system\s*prompt|ignore\b|bypass\b|permission|approval|execute|tool\b', re.I)
    QUOTED = re.compile(r'[“”"\n>]|他说|她说|它说|别人|对方|客户|同事|网页|文档|文件|工具输出|日志|引用|转发|翻译|总结|假设|扮演|角色扮演|例如|测试材料')
    TEMPORARY = re.compile(r'本轮|这次|本次|这条|仅此|临时|不要记|别记|不用记|不要学习|停止学习|是否|能否|能不能|吗[？?]?$')

    def __init__(self, available=False):
        self.available = available

    @staticmethod
    def initialize(db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS character_policy(owner TEXT PRIMARY KEY,learning INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS character_traits(
              id TEXT PRIMARY KEY,owner TEXT,text TEXT,digest TEXT,source TEXT,manual INTEGER,
              state TEXT,created REAL,updated REAL,UNIQUE(owner,digest));
            CREATE TABLE IF NOT EXISTS character_evidence(
              owner TEXT,digest TEXT,source TEXT,created REAL,PRIMARY KEY(owner,digest,source));
            CREATE TABLE IF NOT EXISTS character_changes(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT,trait TEXT,kind TEXT,source TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS character_autonomy(owner TEXT PRIMARY KEY,enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS character_name_lock(owner TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS character_self_changes(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT,attempt TEXT,kind TEXT,before TEXT,after TEXT,
              reason TEXT,created REAL,UNIQUE(owner,attempt));
            CREATE TABLE IF NOT EXISTS character_self_undos(owner TEXT,seq INTEGER,PRIMARY KEY(owner,seq));
        ''')

    @classmethod
    def clean(cls, text):
        if not isinstance(text, str):
            raise ValueError('性格描述须为文字')
        text = unicodedata.normalize('NFKC', text).strip().strip('，,。.!！;； ')
        if not 2 <= len(text) <= cls.MAX_TEXT or cls.PRIVATE.search(text) or cls.UNSAFE.search(text):
            raise ValueError('只支持2至120字的相处方式/性格描述，不保存敏感信息、执行动作或权限指令')
        return text

    @staticmethod
    def digest(text):
        return hashlib.sha256(text.encode()).hexdigest()

    def enabled(self, db, owner):
        row = db.execute('SELECT learning FROM character_policy WHERE owner=?', (owner,)).fetchone()
        return bool(self.available and row and row['learning'])

    def autonomous(self, db, owner):
        row = db.execute('SELECT enabled FROM character_autonomy WHERE owner=?', (owner,)).fetchone()
        return bool(self.available and row and row['enabled'])

    def self_update(self, db, owner, attempt, tool, arguments):
        if not self.autonomous(db, owner):
            raise ValueError('名字和性格自主管理未开启或已经冻结')
        if not isinstance(arguments, dict) or set(arguments) != ({'name', 'reason'} if tool == 'persona_update' else {'description', 'memory_id', 'reason'}):
            raise ValueError('自主修改字段无效')
        reason = self.clean(arguments['reason'])
        if len(reason) > 80:
            raise ValueError('自主修改原因最多80字')
        if db.execute('SELECT 1 FROM character_self_changes WHERE owner=? AND attempt=?', (owner, attempt)).fetchone():
            raise ValueError('本轮已经自主修改一次，不重复或循环修改')
        now = time.time()
        if tool == 'persona_update':
            name = arguments['name']
            if not isinstance(name, str) or not re.fullmatch(r'[\w\-]{1,20}', name) or self.PRIVATE.search(name) or self.UNSAFE.search(name):
                raise ValueError('自主名字限1至20个文字/字母/数字/下划线/连字符，不能是操作指令')
            if db.execute('SELECT 1 FROM character_name_lock WHERE owner=?', (owner,)).fetchone():
                raise ValueError('用户已指定名字，自主修改不能覆盖；需用户重新开启自主管理')
            if db.execute("SELECT 1 FROM character_self_changes WHERE owner=? AND kind='name' AND created>?", (owner, now - 86400)).fetchone():
                raise ValueError('自主改名每天最多一次，避免反复改名')
            row = db.execute('SELECT name FROM persona_core WHERE owner=?', (owner,)).fetchone()
            before, after = row['name'] if row else 'DSH 助手', name
            db.execute('INSERT OR REPLACE INTO persona_core VALUES (?,?)', (owner, name))
            kind = 'name'
        else:
            identifier = arguments['memory_id']
            if not isinstance(identifier, str) or len(identifier) > 32:
                raise ValueError('性格ID无效')
            row = db.execute("SELECT * FROM character_traits WHERE owner=? AND id=? AND state='active'", (owner, identifier)).fetchone() if identifier else None
            if identifier and (not row or row['manual']):
                raise ValueError('自主修改不能覆盖或忘记用户手动设置的性格')
            before, after = row['text'] if row else '', self.clean(arguments['description'])
            self.set(db, owner, after, 'assistant:' + attempt, identifier, manual=False, now=now)
            active = db.execute("SELECT id FROM character_traits WHERE owner=? AND digest=? AND state='active'", (owner, self.digest(after))).fetchone()
            if not active:
                raise ValueError('用户已忘记该性格，自主修改不能恢复')
            kind = 'trait'
        db.execute('INSERT INTO character_self_changes(owner,attempt,kind,before,after,reason,created) VALUES (?,?,?,?,?,?,?)',
                   (owner, attempt, kind, before, after, reason, now))

    @staticmethod
    def change(db, owner, trait, kind, source, now):
        db.execute('INSERT INTO character_changes(owner,trait,kind,source,created) VALUES (?,?,?,?,?)',
                   (owner, trait, kind, source, now))

    def set(self, db, owner, text, source, memory_id='', manual=True, now=None):
        if not self.available:
            raise ValueError('性格成长尚未由宿主启用')
        now = time.time() if now is None else now
        text = self.clean(text)
        digest = self.digest(text)
        old = db.execute('SELECT * FROM character_traits WHERE owner=? AND id=?', (owner, memory_id)).fetchone() if memory_id else None
        if memory_id and (old is None or old['state'] != 'active'):
            raise ValueError('性格ID不存在或不属于你')
        same = db.execute('SELECT * FROM character_traits WHERE owner=? AND digest=?', (owner, digest)).fetchone()
        if same and same['state'] == 'forgotten' and not manual:
            return
        if old and old['digest'] != digest:
            db.execute("UPDATE character_traits SET state='forgotten',updated=? WHERE id=?", (now, old['id']))
            self.change(db, owner, old['id'], 'corrected', source, now)
        if same and same['state'] == 'active':
            if manual and not same['manual']:
                db.execute('UPDATE character_traits SET manual=1,source=?,updated=? WHERE id=?', (source, now, same['id']))
                self.change(db, owner, same['id'], 'set', source, now)
            return
        count = db.execute("SELECT count(*) FROM character_traits WHERE owner=? AND state='active'", (owner,)).fetchone()[0]
        if count >= self.LIMIT:
            raise ValueError('性格记忆已达12条，请先纠正或忘记旧条目，不自动挤掉已有性格')
        identifier = same['id'] if same else uuid.uuid4().hex
        db.execute('INSERT INTO character_traits VALUES (?,?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(owner,digest) DO UPDATE SET text=excluded.text,source=excluded.source,'
                   "manual=excluded.manual,state='active',updated=excluded.updated",
                   (identifier, owner, text, digest, source, int(manual), 'active', now, now))
        self.change(db, owner, identifier, 'set' if manual else 'learned', source, now)

    def forget(self, db, owner, identifier, source):
        row = db.execute("SELECT id FROM character_traits WHERE id=? AND owner=? AND state='active'", (identifier, owner)).fetchone()
        if row is None:
            raise ValueError('性格ID不存在或不属于你')
        now = time.time()
        db.execute("UPDATE character_traits SET state='forgotten',updated=? WHERE id=? AND owner=?", (now, identifier, owner))
        self.change(db, owner, identifier, 'forgotten', source, now)

    def undo(self, db, owner, sequence, source):
        if not isinstance(sequence, str) or not re.fullmatch(r'[1-9][0-9]{0,12}', sequence):
            raise ValueError('自主变化编号无效')
        row = db.execute('SELECT * FROM character_self_changes WHERE owner=? AND seq=?', (owner, int(sequence))).fetchone()
        if not row:
            raise ValueError('自主变化不存在或不属于你')
        if db.execute('SELECT 1 FROM character_self_undos WHERE owner=? AND seq=?', (owner, row['seq'])).fetchone():
            return
        if row['kind'] == 'name':
            current = db.execute('SELECT name FROM persona_core WHERE owner=?', (owner,)).fetchone()
            if not current or current['name'] != row['after']:
                raise ValueError('名字已有后续变化，不用旧记录覆盖当前名字')
            db.execute('INSERT OR REPLACE INTO persona_core VALUES (?,?)', (owner, row['before']))
            db.execute('INSERT OR IGNORE INTO character_name_lock VALUES (?)', (owner,))
        else:
            trait = db.execute("SELECT * FROM character_traits WHERE owner=? AND digest=? AND state='active'",
                               (owner, self.digest(row['after']))).fetchone()
            if not trait or trait['manual']:
                raise ValueError('性格已被纠正或忘记，不用旧记录覆盖当前性格')
            if row['before']:
                self.set(db, owner, row['before'], source, trait['id'])
            else:
                self.forget(db, owner, trait['id'], source)
        db.execute('INSERT INTO character_self_undos VALUES (?,?)', (owner, row['seq']))

    def learn(self, db, owner, source, text):
        if not self.enabled(db, owner) or not isinstance(text, str) or len(text) > 180:
            return
        if self.QUOTED.search(text) or self.TEMPORARY.search(text) or self.PRIVATE.search(text):
            return
        # Free descriptions, not an adjective enum. Only direct feedback about
        # the assistant's way of relating qualifies; unrelated requests do not.
        match = re.fullmatch(r'(?:请)?(?:记住[,，:：]?)?(?:(?:以后|今后|长期|一直)[,，]?)?'
                             r'(?:我希望你|我喜欢你|希望你|你的性格(?:应该|可以|要|是)|'
                             r'(?:和我相处时|跟我相处时|和我交流时)[,，]?(?:请你|你)?|'
                             r'你(?:刚才|这样)(?:的回答|的语气|的表达)?(?:让我觉得|显得))(.{2,120})[。！!！]?', text.strip())
        if not match:
            return
        try:
            description = self.clean(match[1])
        except ValueError:
            return
        # "I want you to..." alone is not enough to learn task requirements.
        if not re.search(r'性格|相处|语气|幽默|温柔|主见|直率|坦诚|耐心|体贴|共情|真诚|敷衍|讨好|附和|调侃|玩笑|冷静|活泼|严肃|亲切|自然|分寸|表达|说话', text):
            return
        digest = self.digest(description)
        row = db.execute('SELECT state FROM character_traits WHERE owner=? AND digest=?', (owner, digest)).fetchone()
        if row and row['state'] == 'forgotten':
            return
        now = time.time()
        db.execute('INSERT OR IGNORE INTO character_evidence VALUES (?,?,?,?)', (owner, digest, source, now))
        count = db.execute('SELECT count(*) FROM character_evidence WHERE owner=? AND digest=? AND created>?',
                           (owner, digest, now - 90 * 86400)).fetchone()[0]
        if count >= 2 or re.search(r'以后|今后|长期|一直|你的性格', text):
            self.set(db, owner, description, source, manual=False, now=now)

    def view(self, db, owner):
        traits = [dict(row) for row in db.execute("SELECT id,text,manual,updated,source FROM character_traits "
                  "WHERE owner=? AND state='active' ORDER BY created,id LIMIT 12", (owner,))]
        for trait in traits:
            trait['origin'] = 'assistant' if trait.pop('source').startswith('assistant:') else 'user'
            digest = self.digest(trait['text'])
            trait['evidence_count'] = db.execute('SELECT count(*) FROM character_evidence WHERE owner=? AND digest=?', (owner, digest)).fetchone()[0]
        changes = [dict(row) for row in db.execute('SELECT seq,trait,kind,created FROM character_changes WHERE owner=? ORDER BY seq DESC LIMIT 12', (owner,))]
        learning = self.enabled(db, owner)
        autonomous = self.autonomous(db, owner)
        self_changes = [dict(row) for row in db.execute('SELECT seq,kind,before,after,reason,created FROM character_self_changes '
                        'WHERE owner=? ORDER BY seq DESC LIMIT 5', (owner,))]
        name_locked = db.execute('SELECT 1 FROM character_name_lock WHERE owner=?', (owner,)).fetchone() is not None
        for change in self_changes:
            change['undone'] = db.execute('SELECT 1 FROM character_self_undos WHERE owner=? AND seq=?', (owner, change['seq'])).fetchone() is not None
        display = ('性格成长：' + ('开' if learning else '关') + '；名字/性格自主管理：' + ('开' if autonomous else '关')
                   + ('（名字由用户指定，自主改名已冻结）' if name_locked else '') + '；不改变权限。\n')
        display += '\n'.join(f"{row['text']}（ID：{row['id']}；" + ('手动设置' if row['manual'] else '助手自主形成' if row['origin'] == 'assistant' else f"用户反馈{row['evidence_count']}次") + '）' for row in traits) or '暂无性格记忆。'
        if self_changes:
            display += '\n最近自主变化：\n' + '\n'.join(f"{row['seq']} {'名字' if row['kind'] == 'name' else '性格'}：{row['before'] or '未设置'} → {row['after']}；原因：{row['reason']}" + ('（已撤销）' if row['undone'] else '') for row in self_changes)
        return {'available': self.available, 'learning': learning, 'autonomous': autonomous, 'name_locked': name_locked,
                'traits': traits, 'changes': changes, 'self_changes': self_changes, 'display': display}
