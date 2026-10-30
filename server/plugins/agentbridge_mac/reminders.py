"""Server-owned one-shot reminders. No model, worker, shell or network dependency."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ReminderError(ValueError):
    pass


def absolute_time(value, timezone, now):
    if (not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)?', value)):
        raise ReminderError('请提供明确的绝对日期和时间；含糊时间需要追问，不自动改到次日。')
    try:
        zone = ZoneInfo(timezone)
        local = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if local.tzinfo is None:
            one, two = local.replace(tzinfo=zone, fold=0), local.replace(tzinfo=zone, fold=1)
            if one.utcoffset() != two.utcoffset():
                raise ReminderError('本地时间重复或不存在，请明确 UTC 偏移。')
            local = one
        if local.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ReminderError('时间或 UTC 偏移与时区不匹配。')
        due = local.timestamp()
    except (ValueError, TypeError, ZoneInfoNotFoundError, OverflowError):
        raise ReminderError('日期、时间或时区无效，请明确绝对时间。') from None
    if due <= now:
        raise ReminderError('提醒时间已经过去，不会自动改到次日。')
    return due


class Reminders:
    def __init__(self, root, clock=time.time):
        self.root, self.clock = Path(root), clock
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / 'reminders.db'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.lease = None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS reminders(
                  id TEXT PRIMARY KEY,sender TEXT,channel TEXT,state TEXT,content TEXT,
                  due REAL,timezone TEXT,created REAL,expires REAL,target TEXT,
                  target_version INTEGER,version INTEGER NOT NULL DEFAULT 0,claimed REAL,ended REAL);
                CREATE INDEX IF NOT EXISTS reminder_due ON reminders(state,due);
                CREATE TABLE IF NOT EXISTS reminder_requests(
                  id TEXT PRIMARY KEY,digest TEXT,result TEXT);
            ''')

    @contextmanager
    def db(self):
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

    def start_sender(self):
        if self.lease is not None:
            return
        lease = open(self.root / 'reminder-sender.lock', 'a')
        os.chmod(lease.name, 0o600)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.db() as db:
                db.execute("UPDATE reminders SET state='unknown',ended=? WHERE state='sending'", (self.clock(),))
        except BaseException:
            lease.close()
            raise
        self.lease = lease

    def close(self):
        if self.lease is not None:
            self.lease.close()
            self.lease = None

    @staticmethod
    def _expire(db, now):
        db.execute("UPDATE reminders SET state='expired',ended=? WHERE state='draft' AND expires<=?", (now, now))
        db.execute("UPDATE reminders SET state='expired',ended=? WHERE state='active' AND due<?", (now, now - 86400))

    @staticmethod
    def _row(db, sender, channel, identifier):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ReminderError('请使用完整提醒 ID。')
        row = db.execute('SELECT * FROM reminders WHERE id=? AND sender=? AND channel=?',
                         (identifier, sender, channel)).fetchone()
        if row is None:
            raise ReminderError('提醒不存在或不属于当前来源。')
        return row

    @staticmethod
    def view(row):
        return {key: row[key] for key in ('id', 'state', 'content', 'due', 'timezone', 'expires', 'target', 'version')} | {
            'absolute_time': datetime.fromtimestamp(row['due'], ZoneInfo(row['timezone'])).isoformat(),
            'delivery_evidence': 'send_api_returned_not_phone_receipt' if row['state'] == 'api_returned' else row['state']}

    def request(self, sender, channel, request_id, tool, arguments):
        if (not sender or not channel or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
                or not isinstance(arguments, dict)):
            raise ReminderError('服务请求缺少可信身份或稳定编号。')
        digest = hashlib.sha256(json.dumps([sender, channel, tool, arguments], sort_keys=True,
                                           ensure_ascii=False).encode()).hexdigest()
        with self.db() as db:
            prior = db.execute('SELECT * FROM reminder_requests WHERE id=?', (request_id,)).fetchone()
            if prior:
                if prior['digest'] != digest:
                    raise ReminderError('请求 ID 已绑定其他来源或参数。')
                return json.loads(prior['result'])
            now = self.clock()
            self._expire(db, now)
            # Store deterministic failures too; a repeated request cannot gain
            # new meaning after a restart, cancellation or quota change.
            db.execute('SAVEPOINT request')
            try:
                result = {'ok': True, **self._execute(db, sender, channel, tool, arguments, now),
                          'observed_at': now, 'evidence': 'operation_receipt_not_live_status'}
            except ReminderError as exc:
                db.execute('ROLLBACK TO request')
                result = {'ok': False, 'error': str(exc)}
            finally:
                db.execute('RELEASE request')
            db.execute('INSERT INTO reminder_requests VALUES (?,?,?)',
                       (request_id, digest, json.dumps(result, ensure_ascii=False)))
            return result

    def _execute(self, db, sender, channel, tool, args, now):
        fields = {'reminder_draft': ({'due_at', 'content'}, {'timezone', 'reminder_id'}),
                  'reminder_set': ({'due_at', 'content'}, {'timezone', 'reminder_id'}),
                  'reminder_list': (set(), set()), 'reminder_get': ({'reminder_id'}, set()),
                  'reminder_cancel': ({'reminder_id'}, set()),
                  'reminder_confirm': ({'reminder_id'}, set())}
        if tool not in fields or not fields[tool][0] <= args.keys() or args.keys() - set.union(*fields[tool]):
            raise ReminderError('不支持的提醒操作或参数，不能指定其他联系人、会话或文件动作。')
        if tool == 'reminder_list':
            rows = db.execute('SELECT * FROM reminders WHERE sender=? AND channel=? '
                              "ORDER BY CASE WHEN state IN ('active','draft','sending','unknown') THEN 0 ELSE 1 END,"
                              'due,id LIMIT 51', (sender, channel)).fetchall()
            items = []
            for row in rows[:50]:
                full = self.view(row)
                summary = {key: full[key] for key in ('id', 'state', 'absolute_time', 'timezone')}
                summary.update(content=row['content'][:80], content_truncated=len(row['content']) > 80)
                candidate = [*items, summary]
                if len(json.dumps(candidate, ensure_ascii=False).encode()) > 12000:
                    break
                items = candidate
            return {'reminders': items, 'truncated': len(rows) > len(items)}
        if tool in ('reminder_set', 'reminder_draft'):
            content, zone = args['content'], args.get('timezone', 'Asia/Shanghai')
            if (not isinstance(content, str) or not content.strip() or len(content) > 1000 or '\0' in content
                    or not isinstance(zone, str) or len(zone) > 100):
                raise ReminderError('内容须为 1 至 1000 字符，时区须明确。')
            due = absolute_time(args['due_at'], zone, now)
            target = args.get('reminder_id', '')
            version = None
            if target:
                original = self._row(db, sender, channel, target)
                if original['state'] != 'active':
                    raise ReminderError('只有尚未发送的有效提醒可以修改。')
                version = original['version']
            elif 'reminder_id' in args:
                raise ReminderError('修改须提供完整提醒 ID。')
            if tool == 'reminder_set':
                if target:
                    db.execute('UPDATE reminders SET content=?,due=?,timezone=?,version=version+1 WHERE id=?',
                               (content, due, zone, target))
                    identifier = target
                else:
                    count = db.execute("SELECT count(*) FROM reminders WHERE sender=? AND channel=? AND state IN ('active','sending','unknown')",
                                       (sender, channel)).fetchone()[0]
                    if count >= 20:
                        raise ReminderError('当前来源最多二十个有效或发送结果未知的提醒。')
                    identifier = uuid.uuid4().hex
                    db.execute('INSERT INTO reminders(id,sender,channel,state,content,due,timezone,created,version) '
                               "VALUES (?,?,?,'active',?,?,?,?,1)",
                               (identifier, sender, channel, content, due, zone, now))
                return {'reminder': self.view(self._row(db, sender, channel, identifier)),
                        'message': '提醒已修改。' if target else '提醒已设置。'}
            count = db.execute("SELECT count(*) FROM reminders WHERE sender=? AND channel=? AND state='draft'",
                               (sender, channel)).fetchone()[0]
            if count >= 5:
                raise ReminderError('当前来源最多五个待确认草稿。')
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO reminders(id,sender,channel,state,content,due,timezone,created,expires,target,target_version) '
                       "VALUES (?,?,?,'draft',?,?,?,?,?,?,?)",
                       (identifier, sender, channel, content, due, zone, now, now + 600, target, version))
            return {'reminder': self.view(self._row(db, sender, channel, identifier)),
                    'message': '仅创建服务意图草稿，尚未生效。请核对绝对时间、时区、内容后单独发送：\n/agent remind confirm ' + identifier}
        row = self._row(db, sender, channel, args['reminder_id'])
        if tool == 'reminder_get':
            return {'reminder': self.view(row)}
        if tool == 'reminder_cancel':
            if row['state'] in ('draft', 'active'):
                db.execute("UPDATE reminders SET state='cancelled',ended=?,version=version+1 WHERE id=?", (now, row['id']))
                message = '提醒已取消，不会发送。'
            elif row['state'] in ('sending', 'unknown', 'api_returned'):
                message = '可能已经发送，无法保证撤回或取消；不会盲目补发。API 返回不等于手机收到。'
            else:
                message = '提醒状态：' + row['state'] + '。没有重新启用。'
            return {'reminder': self.view(self._row(db, sender, channel, row['id'])), 'message': message}
        if row['state'] != 'draft' or row['expires'] <= now:
            raise ReminderError('草稿已过期或已处理，请重新查询，不会重复启用。')
        if row['due'] <= now:
            raise ReminderError('确认时提醒时间已经过去，不会自动改到次日。')
        if row['target']:
            original = self._row(db, sender, channel, row['target'])
            if original['state'] != 'active' or original['version'] != row['target_version']:
                raise ReminderError('原提醒已变化或可能已经发送，请重新查询。')
            db.execute('UPDATE reminders SET content=?,due=?,timezone=?,version=version+1 WHERE id=?',
                       (row['content'], row['due'], row['timezone'], original['id']))
            db.execute("UPDATE reminders SET state='applied',ended=? WHERE id=?", (now, row['id']))
            identifier = original['id']
        else:
            count = db.execute("SELECT count(*) FROM reminders WHERE sender=? AND channel=? AND state IN ('active','sending','unknown')",
                               (sender, channel)).fetchone()[0]
            if count >= 20:
                raise ReminderError('当前来源最多二十个有效或发送结果未知的提醒。')
            db.execute("UPDATE reminders SET state='active',version=version+1 WHERE id=?", (row['id'],))
            identifier = row['id']
        return {'reminder': self.view(self._row(db, sender, channel, identifier)), 'message': '已确认生效，尚不代表已发送或手机收到。'}

    def claim_due(self):
        if self.lease is None:
            raise RuntimeError('reminder sender lease required')
        with self.db() as db:
            now = self.clock()
            self._expire(db, now)
            row = db.execute("SELECT * FROM reminders WHERE state='active' AND due<=? ORDER BY due,id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE reminders SET state='sending',claimed=?,version=version+1 WHERE id=? AND state='active'", (now, row['id']))
            return dict(row) | {'late_seconds': max(0, int(now - row['due']))}

    def finish(self, identifier, state):
        if state not in ('unknown', 'api_returned', 'blocked'):
            raise ValueError('invalid send evidence')
        with self.db() as db:
            db.execute("UPDATE reminders SET state=?,ended=? WHERE id=? AND state='sending'", (state, self.clock(), identifier))

    @staticmethod
    def notification(row):
        late = f"\n（这条消息晚到了 {row['late_seconds']} 秒。）" if row['late_seconds'] > 0 else ''
        return row['content'] + late
