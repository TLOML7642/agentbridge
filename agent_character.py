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
        traits = [dict(row) for row in db.execute("SELECT id,text,manual,updated FROM character_traits "
                  "WHERE owner=? AND state='active' ORDER BY created,id LIMIT 12", (owner,))]
        for trait in traits:
            digest = self.digest(trait['text'])
            trait['evidence_count'] = db.execute('SELECT count(*) FROM character_evidence WHERE owner=? AND digest=?', (owner, digest)).fetchone()[0]
        changes = [dict(row) for row in db.execute('SELECT seq,trait,kind,created FROM character_changes WHERE owner=? ORDER BY seq DESC LIMIT 12', (owner,))]
        learning = self.enabled(db, owner)
        display = '性格成长：' + ('开' if learning else '关') + '；只影响相处和表达，不改变权限。\n'
        display += '\n'.join(f"{row['text']}（ID：{row['id']}；" + ('手动设置' if row['manual'] else f"用户反馈{row['evidence_count']}次") + '）' for row in traits) or '暂无性格记忆。'
        return {'available': self.available, 'learning': learning, 'traits': traits, 'changes': changes, 'display': display}
