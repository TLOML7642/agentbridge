"""Deterministic delivery IDs and authenticated control routing."""
import asyncio
import fcntl
import hashlib
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import re
import time
import uuid

import aiohttp
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.command import GreedyStr

SHIM_URL = os.environ.get("AGENTBRIDGE_SHIM_URL", "http://127.0.0.1:8787/v1/chat/completions")
CONTROL_URL = SHIM_URL.rsplit("/v1/", 1)[0] + "/v1/agentbridge/control"
MESSAGES_URL = SHIM_URL.rsplit('/v1/', 1)[0] + '/v1/agentbridge/messages'
AGENT_ENABLED = os.environ.get('AGENTBRIDGE_AGENT_MESSAGES', '0') == '1'
AGENT_TASKS = os.environ.get('AGENTBRIDGE_AGENT_TASKS', '0') == '1'
AGENT_CHANNELS = {s.strip() for s in os.environ.get('AGENTBRIDGE_AGENT_CHANNELS', '').split(',') if s.strip()}
TOKEN = os.environ.get("SHIM_TOKEN", "")
TIMEOUT = int(os.environ.get("AGENTBRIDGE_MAC_TIMEOUT", "300"))
NOTIFICATIONS_ENABLED = os.environ.get("AGENTBRIDGE_NOTIFICATIONS", "0") == "1"
DATA_DIR = None
USAGE = ("用法：/mac [@项目] <任务>；new <编号> [@项目] <任务>\n"
         "/mac help；diag；jobs；pending；deny <ID>；cancel <ID>\n"
         "/mac status [任务ID]；confirm <令牌>；force <任务ID>\n"
         "/mac project add <名称> <绝对目录>；project ls；project rm <名称>；use <名称>")


def _fingerprint(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class Main(star.Star):
    def __init__(self, context: star.Context):
        self.context = context
        self._sender_task = None
        self._sender_stop = None
        self._sender_lease = None
        self._agent_task = None
        self._agent_stop = None

    def _data_root(self):
        root = Path(DATA_DIR) if DATA_DIR is not None else Path(star.StarTools.get_data_dir("agentbridge_mac"))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        return root

    @contextmanager
    def _notification_db(self):
        path = self._data_root() / "notifications.db"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.executescript('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    sender TEXT NOT NULL, channel TEXT NOT NULL, job_id TEXT NOT NULL,
                    subscribed_at REAL NOT NULL, finished_at REAL, state TEXT NOT NULL DEFAULT 'active',
                    after_seq INTEGER,
                    PRIMARY KEY(sender,channel,job_id));
                CREATE TABLE IF NOT EXISTS notifications (
                    event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    sender TEXT NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    first_seen_at REAL,
                    tries INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                    claimed_at REAL, api_returned_at REAL, reason TEXT);
            ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(subscriptions)')}
            if 'after_seq' not in columns:
                db.execute('ALTER TABLE subscriptions ADD COLUMN after_seq INTEGER')
                db.execute("UPDATE subscriptions SET state='blocked' WHERE after_seq IS NULL")
            columns = {r[1] for r in db.execute('PRAGMA table_info(notifications)')}
            if 'first_seen_at' not in columns:
                db.execute('ALTER TABLE notifications ADD COLUMN first_seen_at REAL')
                db.execute('UPDATE notifications SET first_seen_at=? WHERE first_seen_at IS NULL', (time.time(),))
            yield db

    def _subscribe(self, event, job_id, after_seq=0):
        sender, channel = str(event.get_sender_id()), event.unified_msg_origin
        if not sender or not channel or not job_id:
            raise ValueError("通知订阅缺少可信来源，未下发执行。")
        with self._notification_db() as db:
            db.execute("INSERT INTO subscriptions(sender,channel,job_id,subscribed_at,after_seq) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(sender,channel,job_id) DO UPDATE SET finished_at=NULL,state='active',"
                       "after_seq=COALESCE(subscriptions.after_seq,excluded.after_seq)",
                       (sender, channel, job_id, time.time(), after_seq))

    async def _prepare_subscription(self, event, job_id):
        status, data = await self._control(event, {'op': 'diag'})
        seq = data.get('event_seq')
        if status != 200 or not data.get('ok') or type(seq) is not int or seq < 0:
            raise ValueError('通知订阅对账失败，未下发执行；请稍后重试。')
        self._subscribe(event, job_id, after_seq=seq)

    def _recover_notifications(self):
        with self._notification_db() as db:
            db.execute("UPDATE notifications SET state='unknown',reason='sender_restarted' WHERE state='sending'")

    async def initialize(self):
        if AGENT_ENABLED and self._agent_task is None:
            self._agent_stop = asyncio.Event()
            with self._agent_db() as db:
                db.execute("UPDATE agent_outbox SET state='unknown' WHERE state='sending'")
                db.execute("UPDATE agent_task_outbox SET state='unknown' WHERE state='sending'")
            self._agent_task = asyncio.create_task(self._agent_loop())
        if not NOTIFICATIONS_ENABLED or self._sender_task is not None:
            return
        lease = open(self._data_root() / "notification-sender.lock", "a")
        os.chmod(lease.name, 0o600)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._recover_notifications()
        except BaseException:
            lease.close()
            raise
        self._sender_lease = lease
        self._sender_stop = asyncio.Event()
        self._sender_task = asyncio.create_task(self._notification_loop())

    async def terminate(self):
        try:
            if self._agent_task is not None:
                self._agent_stop.set()
                await self._agent_task
                self._agent_task = None
            if self._sender_task is not None:
                self._sender_stop.set()
                await self._sender_task
        finally:
            self._sender_task = None
            if self._sender_lease is not None:
                self._sender_lease.close()
                self._sender_lease = None

    def _notification_allowed(self, sender, channel):
        # Re-read both policies before every API call. Missing context fails closed.
        allowed = {v.strip() for v in os.environ.get("AGENTBRIDGE_ALLOWED_SENDERS", "").split(",") if v.strip()}
        if not sender or sender not in allowed or not channel or not TOKEN or self.context is None:
            return False
        try:
            config = self.context.get_config(umo=channel)
            whitelist = config.get("platform_settings", {}).get("id_whitelist", [])
            return channel in whitelist and callable(getattr(self.context, "send_message", None))
        except Exception:
            return False

    @contextmanager
    def _agent_db(self):
        path = self._data_root() / 'agent-outbox.db'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE IF NOT EXISTS agent_outbox('
                       'sender TEXT,channel TEXT,delivery TEXT,message_id TEXT,state TEXT,created REAL,'
                       'PRIMARY KEY(sender,channel,delivery))')
            db.execute('CREATE TABLE IF NOT EXISTS agent_task_sources(sender TEXT,channel TEXT,cursor INTEGER,'
                       'PRIMARY KEY(sender,channel))')
            db.execute('CREATE TABLE IF NOT EXISTS agent_task_outbox(id TEXT PRIMARY KEY,sender TEXT,channel TEXT,'
                       'payload TEXT,state TEXT)')
            yield db

    def _agent_allowed(self, sender, channel):
        return AGENT_ENABLED and channel in AGENT_CHANNELS and self._notification_allowed(sender, channel)

    def _agent_delivery(self, event, text, project):
        raw = getattr(event.message_obj, 'raw_message', None)
        if not isinstance(raw, dict) or not (raw.get('message_id') or raw.get('msg_id')):
            raise ValueError('stable adapter delivery id is required')
        return self._delivery(event, text, project)

    async def _agent_tick(self):
        with self._agent_db() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM agent_outbox WHERE state='watch' ORDER BY created LIMIT 50")]
        for row in rows:
            if not self._agent_allowed(row['sender'], row['channel']):
                with self._agent_db() as db:
                    db.execute("UPDATE agent_outbox SET state='blocked' WHERE sender=? AND channel=? AND delivery=?", (row['sender'], row['channel'], row['delivery']))
                continue
            source = type('AgentSource', (), {'get_sender_id': lambda self, value=row['sender']: value,
                                            'unified_msg_origin': row['channel']})()
            payload = ({'op': 'agent.status', 'message_id': row['message_id']} if row['message_id']
                       else {'op': 'agent.lookup', 'delivery_id': row['delivery']})
            try:
                status, data = await self._control(source, payload)
            except Exception:
                continue
            if status != 200 or not data.get('ok'):
                continue
            if data.get('state') not in ('done', 'failed', 'cancelled', 'uncertain'):
                continue
            text = data.get('reply') if data['state'] == 'done' else (
                '这轮对话未完成，状态：' + data['state'] + '。不会自动重跑；可用 /agent status 查询，/agent new 开新会话。')
            key = (row['sender'], row['channel'], row['delivery'])
            with self._agent_db() as db:
                claim = db.execute("UPDATE agent_outbox SET state='sending',message_id=? "
                                   "WHERE sender=? AND channel=? AND delivery=? AND state='watch'", (data['message_id'], *key))
                if claim.rowcount != 1:
                    continue
            state = 'blocked'
            if self._agent_allowed(row['sender'], row['channel']):
                try:
                    result = await asyncio.wait_for(self._send_notification(row['channel'], str(text)), timeout=15)
                    state = 'api_returned' if result else 'unknown'
                except BaseException as exc:
                    with self._agent_db() as db:
                        db.execute("UPDATE agent_outbox SET state='unknown' WHERE sender=? AND channel=? AND delivery=?", key)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    continue
            with self._agent_db() as db:
                db.execute('UPDATE agent_outbox SET state=? WHERE sender=? AND channel=? AND delivery=?', (state, *key))

    async def _agent_loop(self):
        while not self._agent_stop.is_set():
            try:
                await self._agent_tick()
                if AGENT_TASKS:
                    await self._agent_task_tick()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._agent_stop.wait(), timeout=2)
            except TimeoutError:
                pass

    @staticmethod
    def _task_event_text(event):
        body = event['body']
        origin = event['conversation'][:8]
        prefix = f"后台任务 {event['task']} @{event['project']}，原会话 {origin}。\n"
        if event['kind'] == 'approval':
            arguments = body['arguments']
            visible = {key: value for key, value in arguments.items() if key not in ('text', 'binding')}
            if 'text' in arguments:
                visible['text_bytes'] = len(arguments['text'].encode())
                visible['text_sha256'] = hashlib.sha256(arguments['text'].encode()).hexdigest()
                visible['text_preview'] = arguments['text'][:300]
            return (prefix + '实际动作需要确认，四分钟内仅授权本次调用。\n工具：' + body['tool']
                    + '\n参数：' + json.dumps(visible, ensure_ascii=False)
                    + '\n动作摘要：' + body['digest'] + '\n审批ID：' + body['approval_id']
                    + '\n同意：/agent approve ' + body['approval_id']
                    + '\n拒绝：/agent deny ' + body['approval_id'])
        state = body.get('state', 'uncertain')
        descriptions = {'verified': '已完成并有检查证据', 'process-ended': '执行进程已结束，尚不等于目标已验证',
                        'denied': '工具未获授权，未进入实际动作', 'cancelled': '执行器已停止，不撤销已发生的副作用',
                        'uncertain': '结果未知，不会自动重跑，执行通道已隔离', 'failed': '执行前检查未通过'}
        result = body.get('result') or {}
        evidence = {key: result[key] for key in ('evidence', 'path', 'sha256', 'exit_code') if key in result}
        return prefix + descriptions.get(state, state) + '\n' + json.dumps(evidence, ensure_ascii=False)

    async def _agent_task_tick(self):
        with self._agent_db() as db:
            sources = [dict(row) for row in db.execute('SELECT * FROM agent_task_sources LIMIT 50')]
        for source in sources:
            sender, channel = source['sender'], source['channel']
            if not self._agent_allowed(sender, channel):
                continue
            event_source = type('AgentSource', (), {'get_sender_id': lambda self, value=sender: value,
                                                   'unified_msg_origin': channel})()
            try:
                status, data = await self._control(event_source, {'op': 'agent.events', 'after': str(source['cursor'])})
            except Exception:
                continue
            if status != 200 or data.get('ok') is not True or not isinstance(data.get('events'), list):
                continue
            events = data['events']
            if any(event.get('sender') != sender or event.get('channel') != channel
                   or type(event.get('seq')) is not int or event['seq'] <= source['cursor'] for event in events):
                continue
            with self._agent_db() as db:
                for event in events:
                    state = 'pending' if event['kind'] in ('approval', 'terminal') else 'skipped'
                    db.execute('INSERT OR IGNORE INTO agent_task_outbox VALUES (?,?,?,?,?)',
                               (event['id'], sender, channel, json.dumps(event, ensure_ascii=False), state))
                if events:
                    db.execute('UPDATE agent_task_sources SET cursor=max(cursor,?) WHERE sender=? AND channel=?',
                               (max(event['seq'] for event in events), sender, channel))
        with self._agent_db() as db:
            pending = [dict(row) for row in db.execute("SELECT * FROM agent_task_outbox WHERE state='pending' LIMIT 50")]
        for row in pending:
            if not self._agent_allowed(row['sender'], row['channel']):
                continue
            with self._agent_db() as db:
                claim = db.execute("UPDATE agent_task_outbox SET state='sending' WHERE id=? AND state='pending'", (row['id'],))
                if claim.rowcount != 1:
                    continue
            state = 'unknown'
            try:
                if self._agent_allowed(row['sender'], row['channel']):
                    returned = await asyncio.wait_for(self._send_notification(row['channel'], self._task_event_text(json.loads(row['payload']))), timeout=15)
                    state = 'api_returned' if returned else 'unknown'
            except BaseException as error:
                with self._agent_db() as db:
                    db.execute("UPDATE agent_task_outbox SET state='unknown' WHERE id=?", (row['id'],))
                if isinstance(error, asyncio.CancelledError):
                    raise
                continue
            with self._agent_db() as db:
                db.execute('UPDATE agent_task_outbox SET state=? WHERE id=?', (state, row['id']))

    async def _agent_local(self, event, text):
        sender, channel = str(event.get_sender_id()), event.unified_msg_origin
        if not self._agent_allowed(sender, channel):
            return
        raw = getattr(event.message_obj, 'raw_message', None)
        mid = (raw.get('message_id') or raw.get('msg_id')) if isinstance(raw, dict) else None
        mid = mid or getattr(event.message_obj, 'message_id', '')
        delivery = 'local-' + _fingerprint([sender, channel, str(mid)])[:32]
        key = (sender, channel, delivery)
        with self._agent_db() as db:
            db.execute("INSERT OR IGNORE INTO agent_outbox VALUES (?,?,?,NULL,'pending',?)", (*key, time.time()))
            claim = db.execute("UPDATE agent_outbox SET state='sending' WHERE sender=? AND channel=? AND delivery=? AND state='pending'", key)
            if claim.rowcount != 1:
                return
        state = 'blocked'
        if self._agent_allowed(sender, channel):
            try:
                returned = await asyncio.wait_for(self._send_notification(channel, str(text)), timeout=15)
                state = 'api_returned' if returned else 'unknown'
            except BaseException as exc:
                with self._agent_db() as db:
                    db.execute("UPDATE agent_outbox SET state='unknown' WHERE sender=? AND channel=? AND delivery=?", key)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
        with self._agent_db() as db:
            db.execute('UPDATE agent_outbox SET state=? WHERE sender=? AND channel=? AND delivery=?', (state, *key))

    @filter.regex(r'(?s)^.*$', priority=100)
    async def agent_message(self, event: AstrMessageEvent):
        sender, channel = str(event.get_sender_id()), event.unified_msg_origin
        if not self._agent_allowed(sender, channel):
            return
        if not callable(getattr(event, 'is_private_chat', None)) or not event.is_private_chat():
            return
        text = event.get_message_str().strip()
        if text.startswith('/mac') or re.fullmatch(r'确认\s+[0-9a-zA-Z]{6,}', text):
            return
        # Stop AstrBot's ordinary model path BEFORE network calls, including failures.
        event.stop_event()
        try:
            components = getattr(event.message_obj, 'message', [])
            if not text or not components or any(type(part).__name__ != 'Plain' for part in components):
                await self._agent_local(event, '当前统一入口仅支持文字，图片和语音暂不处理。')
                return
            if text.startswith('/agent'):
                parts = text.split()
                if parts == ['/agent', 'new']:
                    delivery, _ = self._agent_delivery(event, text, 'agent-control')
                    status, data = await self._control(event, {'op': 'agent.new', 'delivery_id': delivery})
                elif parts in (['/agent', 'memory'], ['/agent', 'persona']):
                    status, data = await self._control(event, {'op': 'agent.' + parts[1]})
                elif parts[:3] == ['/agent', 'persona', 'name'] and len(parts) >= 4:
                    delivery, _ = self._agent_delivery(event, text, 'agent-control')
                    status, data = await self._control(event, {'op': 'agent.persona.set', 'name': ' '.join(parts[3:]), 'delivery_id': delivery})
                elif parts[:2] == ['/agent', 'memory'] and len(parts) == 3 and parts[2] in ('on', 'off'):
                    delivery, _ = self._agent_delivery(event, text, 'agent-control')
                    status, data = await self._control(event, {'op': 'agent.memory.' + parts[2], 'delivery_id': delivery})
                elif parts[:2] == ['/agent', 'forget'] and len(parts) == 3:
                    delivery, _ = self._agent_delivery(event, text, 'agent-control')
                    status, data = await self._control(event, {'op': 'agent.forget', 'memory_id': parts[2], 'delivery_id': delivery})
                elif parts[:2] == ['/agent', 'status'] and len(parts) <= 3:
                    status, data = await self._control(event, {'op': 'agent.status', **({'message_id': parts[2]} if len(parts) == 3 else {})})
                elif parts[:2] == ['/agent', 'cancel'] and len(parts) == 3:
                    delivery, _ = self._agent_delivery(event, text, 'agent-control')
                    status, data = await self._control(event, {'op': 'agent.cancel', 'message_id': parts[2], 'delivery_id': delivery})
                elif parts[:2] in (['/agent', 'tasks'], ['/agent', 'approvals']) and len(parts) <= 3:
                    field = 'task_id' if parts[1] == 'tasks' else 'approval_id'
                    status, data = await self._control(event, {'op': 'agent.' + parts[1], **({field: parts[2]} if len(parts) == 3 else {})})
                elif parts[:2] in (['/agent', 'approve'], ['/agent', 'deny'], ['/agent', 'task-cancel']) and len(parts) == 3:
                    delivery, _ = self._agent_delivery(event, text, 'agent-control')
                    cancel_task = parts[1] == 'task-cancel'
                    status, data = await self._control(event, {'op': 'agent.task.cancel' if cancel_task else 'agent.' + parts[1],
                                                              'task_id' if cancel_task else 'approval_id': parts[2], 'delivery_id': delivery})
                else:
                    await self._agent_local(event, '用法：/agent new；/agent status [消息ID]；/agent cancel <消息ID>；/agent memory [on|off]；/agent forget <偏好ID>；/agent persona。'
                                            + ('后台：/agent tasks [ID]；/agent task-cancel <ID>；/agent approvals [ID]；/agent approve <ID>；/agent deny <ID>。'
                                               if AGENT_TASKS else '当前后台动作未启用。'))
                    return
                if data.get('tasks') is not None:
                    reply = '\n'.join(f"{item['task_id']} {item['state']} @{item['project']}" for item in data['tasks']) or '当前会话没有后台任务。'
                elif data.get('approvals') is not None:
                    reply = '\n'.join(f"{item['approval_id']} {item['state']} {item['tool']}" for item in data['approvals']) or '当前会话没有工具审批。'
                elif data.get('approval_id'):
                    reply = self._task_event_text({'task': data['subject']['attempt'], 'project': data['subject']['project'],
                                                  'conversation': data['subject']['conversation'], 'kind': 'approval', 'body': data})
                    if data['state'] != 'pending':
                        reply = f"审批 {data['approval_id']}：{data['state']}，仅限该动作；执行结果请查 /agent tasks {data['subject']['attempt']}。"
                elif data.get('task_id'):
                    reply = self._task_event_text({'task': data['task_id'], 'project': data['project'],
                                                  'conversation': data['conversation'], 'kind': 'terminal', 'body': data})
                elif data.get('preferences') is not None:
                    reply = (data['core']['name'] + '；自动学习：' + ('开' if data['learning'] else '关')
                             + '\n' + ('\n'.join(f"{item['id']} {item['category']}={item['value']} 来源:{item['source']}" for item in data['preferences']) or '暂无有效长期偏好。'))
                elif data.get('messages') is not None:
                    reply = '\n'.join(f"{item['message_id']} {item['state']} @{item['project']}" for item in data['messages']) or '暂无对话记录。'
                else:
                    reply = data.get('reply') or data.get('message') or data.get('state') or str(data.get('error', {}).get('message', status))
                await self._agent_local(event, reply)
                return
            raw = getattr(event.message_obj, 'raw_message', None)
            if not isinstance(raw, dict) or not (raw.get('message_id') or raw.get('msg_id')):
                await self._agent_local(event, '缺少稳定消息编号，本次未调用模型；请检查适配器消息标识。')
                return
            delivery, _ = self._agent_delivery(event, text, 'agent-chat')
            with self._agent_db() as db:
                db.execute("INSERT OR IGNORE INTO agent_outbox VALUES (?,?,?,NULL,'watch',?)", (sender, channel, delivery, time.time()))
                if AGENT_TASKS:
                    db.execute('INSERT OR IGNORE INTO agent_task_sources VALUES (?,?,0)', (sender, channel))
            status, data = await self._request(event, MESSAGES_URL, {'delivery_id': delivery, 'text': text})
            if data.get('message_id'):
                with self._agent_db() as db:
                    db.execute('UPDATE agent_outbox SET message_id=? WHERE sender=? AND channel=? AND delivery=?', (data['message_id'], sender, channel, delivery))
                await self._agent_tick()
            else:
                await self._agent_local(event, '消息受理未确认，不会改用另一个聊天模型。可用 /agent status 查询；重复投递会复用原编号。')
        except Exception:
            await self._agent_local(event, 'DSH 对话入口暂不可用，不会自动切换模型或新建任务。请查询原消息状态。')
    @staticmethod
    def _excerpt(text):
        text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", "[密钥已隐藏]", str(text))
        text = re.sub(r"```[\s\S]*?(?:```|$)", "[代码已省略]", text)
        text = re.sub(r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)\S+", r"\1[已隐藏]", text)
        text = re.sub(r"(?i)\bBearer\s+\S+|sk-[A-Za-z0-9_-]{12,}", "[凭据已隐藏]", text)
        text = re.sub(r"(?<!\w)/(?:[^\s，。；<>]+)", "[路径已隐藏]", text)
        lines = [line for line in text.splitlines() if not re.search(
            r"(?i)(?:^\s*[$>#]|\b(?:sudo|ssh|scp|curl|wget|rm|mv|cp|chmod|chown|git|python\d*|node|bash|sh|launchctl|docker|npm|pip)\s|[|;&`])", line)]
        return "\n".join(lines).strip()[:300] or "结果摘要已省略，请查询详情。"

    @staticmethod
    def _record_event(db, event):
        db.execute("INSERT OR IGNORE INTO notifications(event_id,job_id,attempt_id,sender,channel,status,created_at,first_seen_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", tuple(event[k] for k in (
                       "event_id", "job_id", "attempt_id", "sender", "channel", "status", "created_at")) + (time.time(),))

    async def _send_notification(self, channel, text):
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.api.message_components import Plain
        return await self.context.send_message(channel, MessageChain(chain=[Plain(text)]))

    async def _deliver_event(self, event, result):
        with self._notification_db() as db:
            self._record_event(db, event)
            row = db.execute("SELECT * FROM notifications WHERE event_id=?", (event['event_id'],)).fetchone()
        if row['state'] not in ('pending', 'retry') or row['next_at'] > time.time():
            return
        if time.time() - row['first_seen_at'] >= 86400:
            with self._notification_db() as db:
                db.execute("UPDATE notifications SET state='expired',reason='retry_budget_expired' WHERE event_id=? AND state IN ('pending','retry')", (event['event_id'],))
            return
        if not self._notification_allowed(event['sender'], event['channel']):
            with self._notification_db() as db:
                db.execute("UPDATE notifications SET state='blocked',reason='source_or_context_unavailable' WHERE event_id=? AND state IN ('pending','retry')", (event['event_id'],))
            return
        try:
            text = (f"事件：{event['event_id']}\n任务：{event['job_id']} 状态：{event['status']}\n"
                    f"耗时：{event.get('elapsed_seconds') or 0:.1f} 秒\n{self._excerpt(result)}\n"
                    "发送 API 状态不代表手机已收到；详情用 /mac status 查询。")
        except Exception:
            # This path is provably before the send API, so delayed retry is safe.
            tries = row['tries'] + 1
            delay = (5, 15, 60)[tries - 1] if tries <= 3 else 300
            with self._notification_db() as db:
                db.execute("UPDATE notifications SET state=?,tries=?,next_at=?,reason='pre_api_failure' WHERE event_id=?",
                           ('expired' if time.time() - row['first_seen_at'] >= 86400 else 'retry', tries, time.time() + delay, event['event_id']))
            return
        # A committed claim precedes any potentially sending call. A crash fails closed.
        with self._notification_db() as db:
            cur = db.execute("UPDATE notifications SET state='sending',claimed_at=? WHERE event_id=? AND state IN ('pending','retry') AND next_at<=?",
                             (time.time(), event['event_id'], time.time()))
            if cur.rowcount != 1:
                return
        if not self._notification_allowed(event['sender'], event['channel']):
            state, reason = 'blocked', 'source_revoked_before_api'
        else:
            try:
                returned = await asyncio.wait_for(self._send_notification(event['channel'], text), timeout=15)
                state, reason = 'api_returned', 'platform_matched' if returned else 'platform_not_found'
            except BaseException as exc:
                with self._notification_db() as db:
                    db.execute("UPDATE notifications SET state='unknown',reason='send_api_exception' WHERE event_id=?", (event['event_id'],))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
        with self._notification_db() as db:
            db.execute("UPDATE notifications SET state=?,reason=?,api_returned_at=? WHERE event_id=?",
                       (state, reason, time.time() if state == 'api_returned' else None, event['event_id']))

    async def _notification_tick(self):
        with self._notification_db() as db:
            subscriptions = [dict(r) for r in db.execute("SELECT * FROM subscriptions WHERE state='active'")]
        groups = {}
        for sub in subscriptions:
            groups.setdefault((sub['sender'], sub['channel']), []).append(sub)
        for (sender, channel), subs in groups.items():
            if self._sender_stop is not None and self._sender_stop.is_set():
                return
            if not self._notification_allowed(sender, channel):
                with self._notification_db() as db:
                    db.execute("UPDATE subscriptions SET state='blocked' WHERE sender=? AND channel=?", (sender, channel))
                continue
            class Source:
                unified_msg_origin = channel

                def get_sender_id(self):
                    return sender
            for offset in range(0, len(subs), 50):
                batch = subs[offset:offset + 50]
                status, data = await self._control(Source(), {'op': 'status.batch', 'job_ids': [s['job_id'] for s in batch]})
                jobs = data.get('jobs', []) if status == 200 and data.get('ok') else []
                if data.get('code') == 'NOT_FOUND':
                    # A not-yet-accepted subscription must not hide other accepted jobs.
                    for sub in batch:
                        status, item = await self._control(Source(), {'op': 'status', 'job_id': sub['job_id']})
                        if status == 200 and item.get('job_id'):
                            jobs.append({**item, 'requested_id': sub['job_id']})
                        elif time.time() - sub['subscribed_at'] >= 86400 and item.get('code') == 'NOT_FOUND':
                            with self._notification_db() as db:
                                db.execute("UPDATE subscriptions SET state='unresolved',finished_at=? WHERE sender=? AND channel=? AND job_id=?",
                                           (time.time(), sender, channel, sub['job_id']))
                for job in jobs:
                    sub = next((s for s in batch if s['job_id'] == job.get('requested_id')), None)
                    if sub is None:
                        continue
                    for event in job.get('events', []):
                        if (event.get('sender') != sender or event.get('channel') != channel
                                or event.get('job_id') != job.get('job_id') or sub.get('after_seq') is None
                                or type(event.get('seq')) is not int or event['seq'] <= sub['after_seq']):
                            continue
                        current = (event['attempt_id'] == job.get('attempt_id') or
                                   job.get('attempt_id') is None and event['attempt_id'] == 'pending:' + job['job_id'])
                        result = job.get('message', '') if current else '此尝试结果已由后续执行更新，请本机核查。'
                        await self._deliver_event(event, result)
                    if job.get('status') in ('done', 'failed', 'uncertain', 'cancelled'):
                        with self._notification_db() as db:
                            db.execute("UPDATE subscriptions SET finished_at=COALESCE(finished_at,?) WHERE sender=? AND channel=? AND job_id=?",
                                       (time.time(), sender, channel, sub['job_id']))
        with self._notification_db() as db:
            cutoff = time.time() - 7 * 86400
            db.execute("DELETE FROM notifications WHERE first_seen_at<? AND state NOT IN ('pending','retry','sending')", (cutoff,))
            db.execute("DELETE FROM subscriptions WHERE finished_at<?", (cutoff,))

    async def _notification_loop(self):
        while not self._sender_stop.is_set():
            try:
                await self._notification_tick()
            except Exception:
                # No raw exception/config/result logging. Claimed events remain fail-closed.
                pass
            try:
                await asyncio.wait_for(self._sender_stop.wait(), timeout=5)
            except TimeoutError:
                pass

    def _delivery(self, event, text, project="", new_number=""):
        source = [event.unified_msg_origin, str(event.get_sender_id()),
                  str(getattr(event.message_obj, "self_id", ""))]
        if not source[0] or not source[1]:
            raise ValueError("缺少发送者或会话身份，未下发任务。")
        if new_number:
            return _fingerprint(["new", *source, new_number])[:32], False
        raw = getattr(event.message_obj, "raw_message", None)
        # The adapter's message_obj.message_id can be a random fallback UUID.
        mid = (raw.get("message_id") or raw.get("msg_id")) if isinstance(raw, dict) else None
        if mid:
            return _fingerprint(["delivery", *source, str(mid)])[:32], False
        fingerprint = _fingerprint(["fallback", *source, project, text])
        root = Path(DATA_DIR) if DATA_DIR is not None else Path(star.StarTools.get_data_dir("agentbridge_mac"))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        path = root / "deliveries.db"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("CREATE TABLE IF NOT EXISTS deliveries "
                       "(fingerprint TEXT PRIMARY KEY, job_id TEXT NOT NULL, created_at REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            hit = db.execute("SELECT job_id,created_at FROM deliveries WHERE fingerprint=?", (fingerprint,)).fetchone()
            if hit and 0 <= now - hit[1] < 60:
                return hit[0], True
            jid = uuid.uuid4().hex
            db.execute("DELETE FROM deliveries WHERE created_at <= ?", (now - 60,))
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?)", (fingerprint, jid, now))
            # Commit before dispatch; persistence failure must never send an execution request.
        return jid, False

    def _headers(self, event, job_id=None, confirm=None, project=None):
        headers = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
                   "X-AgentBridge-Sender": str(event.get_sender_id()),
                   "X-AgentBridge-Channel": event.unified_msg_origin}
        if job_id:
            headers.update({"X-AgentBridge-Job-Id": job_id, "X-AgentBridge-Id-Stable": "1"})
            headers["Prefer"] = "respond-async"
        if confirm:
            headers["X-AgentBridge-Confirm"] = confirm
        if project:
            headers["X-AgentBridge-Project"] = project
        return headers

    async def _request(self, event, url, payload, **headers):
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers(event, **headers),
                                    timeout=aiohttp.ClientTimeout(total=10 if payload.get('op') in ('status', 'diag', 'jobs', 'pending', 'project.list', 'status.batch') else TIMEOUT)) as resp:
                try:
                    return resp.status, await resp.json()
                except Exception:
                    return resp.status, {"error": {"message": "网关响应格式异常，请查询原任务。"}}

    async def _call(self, event, text, job_id=None, project=None):
        if NOTIFICATIONS_ENABLED:
            await self._prepare_subscription(event, job_id)
        return await self._request(event, SHIM_URL, {"model": "bridge",
            "messages": [{"role": "user", "content": text}]}, job_id=job_id, project=project)

    async def _control(self, event, payload):
        return await self._request(event, CONTROL_URL, payload)

    def _render(self, status, data):
        if data.get("choices"):
            return str(data["choices"][0]["message"]["content"])
        if data.get("error"):
            return "调用本机执行器失败：" + str(data["error"].get("message", status))
        if data.get("job_id"):
            notify = "未订阅或未启用"
            if (self._data_root() / 'notifications.db').exists():
                with self._notification_db() as db:
                    rows = db.execute("SELECT state FROM notifications WHERE job_id=? ORDER BY created_at DESC LIMIT 3", (data['job_id'],)).fetchall()
                    labels = {'pending': '待尝试', 'retry': '待重试', 'sending': '发送中', 'api_returned': 'API 返回', 'unknown': '结果未知', 'blocked': '来源或上下文失效', 'expired': '重试已到期'}
                    notify = '、'.join(labels.get(r['state'], r['state']) for r in rows) or "尚无完成通知"
            return (str(data.get('message') or '') + f"\n任务ID：{data['job_id']} 状态：{data.get('phase', data.get('status', 'unknown'))}"
                    f"\n耗时：{data.get('elapsed_seconds', 0):.1f} 秒；通知：{notify}（不代表手机收到）")
        if data.get("message"):
            return str(data["message"]) + (f"\n最近 worker 健康时间：{data.get('last_worker_healthy_at', data.get('healthy_at'))}" if 'healthy_at' in data or 'last_worker_healthy_at' in data else '')
        if "pending" in data:
            return "待确认：\n" + ("\n".join(f"{r['job_id']} @{r['project']} 剩余 {r['remaining_seconds']} 秒" for r in data["pending"]) or "暂无")
        if data.get("recent") is not None:
            rows = [f"{r['job_id']} {r['status']} @{r['project']}" for r in data["recent"]]
            return "当前项目：" + str(data.get("current")) + "\n" + ("\n".join(rows) or "暂无任务")
        if "projects" in data:
            return "项目：" + ", ".join(p["name"] for p in data["projects"])
        return "当前项目：" + str(data.get("current", data.get("project", "操作完成")))

    async def _confirm(self, event, token):
        jid, reused = self._delivery(event, "confirm " + token, "control")
        if NOTIFICATIONS_ENABLED:
            await self._prepare_subscription(event, jid)
        status, data = await self._control(event, {"op": "confirm", "token": token, "delivery_id": jid})
        return ("60 秒内相同指令，正在复用任务。\n" if reused else "") + self._render(status, data)

    @filter.regex(r"^\s*确认\s+[0-9a-zA-Z]{6,}\s*$")
    async def confirm_by_text(self, event: AstrMessageEvent):
        try:
            if hasattr(event, "is_private_chat") and not event.is_private_chat():
                raise ValueError("仅支持私聊。")
            yield event.plain_result(await self._confirm(event, event.get_message_str().split()[1]))
        except Exception:
            yield event.plain_result("确认结果未知，令牌未在本地清除；请重试同一确认或查询原任务。")

    @filter.command("mac")
    async def mac(self, event: AstrMessageEvent, task: GreedyStr):
        text = str(task or "").strip()
        if not text:
            yield event.plain_result(USAGE)
            return
        jid = ""
        try:
            if hasattr(event, "is_private_chat") and not event.is_private_chat():
                raise ValueError("仅支持私聊。")
            parts = text.split()
            command = parts[0]
            if command == "help":
                yield event.plain_result(USAGE)
                return
            payload = None
            if command == "confirm":
                if len(parts) != 2:
                    raise ValueError("格式：/mac confirm <令牌>")
                yield event.plain_result(await self._confirm(event, parts[1]))
                return
            if command in ("diag", "jobs", "pending"):
                if len(parts) != 1:
                    raise ValueError("格式：/mac " + command)
                payload = {"op": command}
            elif command == "status":
                if len(parts) > 2:
                    raise ValueError("格式：/mac status [任务ID]")
                payload = {"op": "status", "job_id": parts[1] if len(parts) == 2 else ""}
            elif command in ("force", "use", "deny", "cancel"):
                if len(parts) != 2:
                    raise ValueError("格式：/mac " + command + " <任务ID或项目名称>")
                payload = {"op": command, "project" if command == "use" else "job_id": parts[1]}
            elif command == "project":
                if parts[1:] == ["ls"]:
                    payload = {"op": "project.list"}
                elif len(parts) == 3 and parts[1] == "rm":
                    payload = {"op": "project.rm", "name": parts[2]}
                elif len(parts) >= 4 and parts[1] == "add":
                    payload = {"op": "project.add", "name": parts[2], "dir": text.split(None, 3)[3]}
                else:
                    raise ValueError(USAGE)
            if payload is not None:
                if payload["op"] not in ("status", "project.list", "diag", "jobs", "pending"):
                    jid, _ = self._delivery(event, text, "control")
                    payload["delivery_id"] = jid
                    if payload['op'] == 'force' and NOTIFICATIONS_ENABLED:
                        check_status, job = await self._control(event, {'op': 'status', 'job_id': payload['job_id']})
                        if check_status != 200 or not job.get('job_id'):
                            yield event.plain_result(self._render(check_status, job))
                            return
                        payload['job_id'] = job['job_id']
                        await self._prepare_subscription(event, job['job_id'])
                status, data = await self._control(event, payload)
                yield event.plain_result(self._render(status, data))
                return
            number = ""
            if command == "new":
                if len(parts) < 3:
                    raise ValueError("格式：/mac new <编号> [@项目] <任务>")
                number, text = parts[1], text.split(None, 2)[2]
            project = ""
            if text.startswith("@"):
                route = text.split(None, 1)
                if len(route) != 2 or not route[0][1:]:
                    raise ValueError("格式：/mac @项目 <任务>")
                project, text = route[0][1:], route[1]
            if not project:
                status, data = await self._control(event, {"op": "status"})
                if status != 200 or not data.get("ok") or not data.get("current"):
                    yield event.plain_result(self._render(status, data))
                    return
                project = data["current"]
            jid, reused = self._delivery(event, text, project, number)
            status, data = await self._call(event, text, job_id=jid, project=project)
            yield event.plain_result(("60 秒内相同指令，正在复用任务。\n" if reused else "") + self._render(status, data))
        except ValueError as exc:
            yield event.plain_result(str(exc))
        except Exception:
            if text.split()[0] in ("status", "jobs", "pending", "diag"):
                yield event.plain_result("查询失败，查询未创建任务；请稍后查询原 ID。")
            else:
                yield event.plain_result("未能确认投递结果。" + (f"请用 /mac status {jid} 查询，勿盲目新建。" if jid else "未下发执行；请稍后重试。"))
