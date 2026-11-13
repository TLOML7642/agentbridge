#!/usr/bin/env python3
"""Lightweight loopback Mac executor. No legacy App, chat writer or scheduler."""
import hmac
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from agent_runtime import AgentError
from agent_tasks import TaskRuntime
from agent_task_executor import NativeTaskExecutor
from executor_api import dispatch
from process_supervisor import run_task, redact, _executor_identity, _group_alive, _stop_owned_group
from worker_role import role


DISABLED = {'ok': False, 'code': 'LEGACY_DISABLED',
            'message': '旧/mac和/execute执行入口已停用。请直接在微信描述电脑任务；历史记录保留，不会转交其他模型。'}


class App:
    def __init__(self, root, computer_root, runner=None):
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.resolve() != root:
            raise ValueError('executor state must be canonical')
        # Preserve the existing persistent role fence and private task history.
        if role(root) != 'executor_only':
            raise ValueError('persisted executor_only role required')
        self.role, self.boot_id = 'executor_only', uuid.uuid4().hex
        self.legacy = root / 'agentbridge.db'
        self.agent = None
        task_root = root / 'agent/tasks'
        executor = NativeTaskExecutor(task_root / 'native', run_task, _executor_identity, self.settle, redact)
        self.agent_tasks = TaskRuntime(task_root, runner or executor, lambda name: self.get_project(name)['dir'],
                                      enabled=True, recover=executor.recover, check_external=self.check_legacy,
                                      allowed_projects=(), computer_root=computer_root, preflight=True)

    def legacy_rows(self, query, args=()):
        if not self.legacy.exists():
            return []
        if self.legacy.is_symlink():
            raise AgentError('TASK_UNCERTAIN', '旧任务库路径异常')
        with sqlite3.connect(self.legacy.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(query, args)]

    def check_legacy(self):
        if self.legacy_rows('SELECT 1 FROM quarantine LIMIT 1') or self.legacy_rows(
                "SELECT 1 FROM jobs WHERE status IN ('running','queued','uncertain') LIMIT 1"):
            raise AgentError('TASK_UNCERTAIN', '旧任务仍在途或结果未知，请先本机核查；没有重跑旧任务')

    @staticmethod
    def settle(row):
        pgid, identity = row.get('exec_pgid'), row.get('exec_identity')
        if not pgid and row.get('exec_state') in (None, 'idle', 'starting'):
            return 'dead'
        if pgid and not _group_alive(pgid):
            return 'dead'
        if pgid and identity and _executor_identity(pgid) == identity and _stop_owned_group(pgid, {pgid: identity}):
            return 'killed'
        return 'quarantined'

    def get_project(self, name):
        rows = self.legacy_rows('SELECT dir FROM projects WHERE name=?', (name,))
        if not rows:
            raise AgentError('NOT_FOUND', '旧只读项目绑定不存在')
        return rows[0]

    def close(self):
        self.agent_tasks.close()


def server(app, secret, senders, channels, port):
    if not secret or not senders or not channels:
        raise ValueError('explicit authentication and sources required')

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def reply(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authenticated(self):
            return hmac.compare_digest(self.headers.get('X-AgentBridge-Secret', '').encode(), secret.encode())

        def do_GET(self):
            if not self.authenticated():
                return self.reply(401, {'ok': False, 'code': 'AUTH_FAIL'})
            route = urlsplit(self.path)
            if route.path == '/health':
                return self.reply(200, {'ok': True, 'role': app.role, 'boot_id': app.boot_id, 'legacy_execution': 'disabled'})
            if route.path == '/job':
                identifier = parse_qs(route.query).get('id', [''])[0]
                rows = app.legacy_rows('SELECT job_id,status,error_code FROM jobs WHERE job_id=?', (identifier,))
                return self.reply(200 if rows else 404, {'ok': bool(rows), 'job': rows[0] if rows else None})
            return self.reply(404, {'ok': False, 'code': 'NOT_FOUND'})

        def do_POST(self):
            self.close_connection = True
            if not self.authenticated():
                return self.reply(401, {'ok': False, 'code': 'AUTH_FAIL'})
            route = urlsplit(self.path).path.rstrip('/')
            if route != '/executor':
                return self.reply(410, DISABLED)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 < size <= 65536:
                    raise ValueError('invalid body length')
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError('incomplete body')
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError('invalid body')
                return self.reply(200, dispatch(app, payload, senders, channels))
            except AgentError as error:
                return self.reply(200, {'ok': False, 'code': error.code, 'message': error.message})
            except (ValueError, TypeError):
                return self.reply(400, {'ok': False, 'code': 'INVALID_MESSAGE'})
            except Exception:
                return self.reply(500, {'ok': False, 'code': 'EXECUTOR_UNKNOWN'})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    os.umask(0o077)
    root = Path(os.environ.get('AGENTBRIDGE_BASE', str(Path.home() / 'agentbridge')))
    secret = os.environ.get('AGENTBRIDGE_SECRET', '')
    senders = {value.strip() for value in os.environ.get('AGENTBRIDGE_ALLOWED_SENDERS', '').split(',') if value.strip()}
    channels = {value.strip() for value in os.environ.get('AGENTBRIDGE_AGENT_CHANNELS', '').split(',') if value.strip()}
    computer_root = os.environ.get('AGENTBRIDGE_COMPUTER_ROOT', '')
    if not secret or not senders or not channels or not computer_root:
        raise SystemExit('executor authentication, sources and computer root required')
    app = App(root, computer_root)
    try:
        http = server(app, secret, senders, channels, int(os.environ.get('AGENTBRIDGE_PORT', '9000')))
        def stop(*_args):
            threading.Thread(target=http.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            http.serve_forever()
        finally:
            http.server_close()
    finally:
        app.close()


if __name__ == '__main__':
    main()
