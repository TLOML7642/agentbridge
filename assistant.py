#!/usr/bin/env python3
"""Loopback server conversation host with host-bound read/executor targets.

Existing AstrBot owns reminders.db, scheduling and message delivery.
"""
import hmac
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_runtime import AgentError, AgentRuntime, NativeExecutor, required
from agent_services import ServiceQueue
from assistant_projects import Projects
from agent_diagnostics import Diagnostics


CONTROL_FIELDS = {
    'agent.projects': set(), 'agent.use': {'project', 'delivery_id'},
    'agent.status': {'message_id'}, 'agent.lookup': {'delivery_id'},
    'agent.new': {'delivery_id'}, 'agent.cancel': {'message_id', 'delivery_id'},
    'agent.memory': set(), 'agent.persona': set(),
    'agent.memory.on': {'delivery_id'}, 'agent.memory.off': {'delivery_id'},
    'agent.forget': {'memory_id', 'delivery_id'}, 'agent.persona.set': {'name', 'delivery_id'},
    'agent.persona.reset': {'delivery_id'},
    'agent.preference.set': {'category', 'value', 'delivery_id'},
    'agent.preference.confirm': {'draft_id', 'delivery_id'},
    'agent.service.requests': set(), 'agent.service.resolve': {'request_id', 'result'},
}
MAC_CONTROLS = frozenset(('agent.tasks', 'agent.task.cancel', 'agent.approvals',
                          'agent.approve', 'agent.deny', 'agent.events'))


class App:
    def __init__(self, root, secret, senders, channels, services=False, runner=None,
                 executor_url=None, executor_secret=None, task_projects=()):
        required(secret, 512)
        if not senders or not channels or any(not isinstance(v, str) or not v.strip() for v in (*senders, *channels)):
            raise AgentError('AUTH_FAIL', '服务器助手须显式配置发送者和会话允许列表')
        root = Path(root)
        if not root.is_absolute() or root.is_symlink():
            raise AgentError('INVALID_CONFIG', '助手状态须使用独立绝对目录，不接受链接')
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.diagnostics = Diagnostics(self.root / 'diagnostics')
        self.secret, self.senders, self.channels = secret, frozenset(senders), frozenset(channels)
        self.boot_id = uuid.uuid4().hex
        # Captured once from the image build, never inferred from read documents.
        version = os.environ.get('AGENTBRIDGE_BUILD_SOURCE_VERSION', '')
        self.source_version = version if re.fullmatch('[a-f0-9]{40}', version) else None
        self.closed = False
        self.workspace = self.root / 'workspace'
        if self.workspace.is_symlink():
            raise AgentError('INVALID_CONFIG', '会话工作空间不能是链接')
        self.workspace.mkdir(mode=0o700, exist_ok=True)
        self.workspace.chmod(0o700)
        self.project = 'assistant'
        self.projects = Projects(self.root)
        with self.projects.connect() as db:
            if not db.execute('SELECT 1 FROM bindings WHERE name=?', (self.project,)).fetchone():
                self.projects.bind(self.project)
        self.workspace = Path(self.projects.workspace(self.project))
        self.executor = None
        if executor_url:
            from assistant_executor import Executor
            required(executor_secret, 512)
            self.executor = Executor(self.root / 'tools', executor_url, executor_secret, self.projects, task_projects)
        self.agent = None
        queue = ServiceQueue(self.root / 'agent/services', profile=self.profile_intent) if services else None
        try:
            if runner is None:
                # Definitions only. Never construct worker.App, Scheduler or its
                # config/preset setup; use the already-verified owned supervisor.
                from process_supervisor import run_task, redact
                runner = NativeExecutor(self.root / 'agent', run_task, redact, services=queue, file_reads=False,
                                        tasks=self.executor, remote=self.executor, snapshots=self.projects.snapshot)
            ready = threading.Event()

            def run(turn, cancel):
                # A recovered queued turn must not call profile services before
                # the owning runtime has been assigned to this host.
                ready.wait()
                return runner(turn, cancel)

            self.agent = AgentRuntime(self.root / 'agent', run, self.projects.workspace,
                                      recover=getattr(runner, 'recover', lambda _: None),
                                      allow_learning=False, services=queue, runtime_info=self.runtime_info,
                                      logical_projects=True, tasks=self.executor)
            ready.set()
        except BaseException:
            if queue:
                queue.close()
            if self.executor:
                self.executor.close()
            raise

    def runtime_info(self):
        diagnostic_status = self.diagnostics.maintain()
        return {'host_role': 'server_assistant', 'assistant_boot_id': self.boot_id,
                'running_source_version': self.source_version,
                'running_source_version_evidence': 'image build metadata' if self.source_version else 'unknown; not supplied at build',
                'diagnostics': diagnostic_status,
                'assistant_runtime': 'running', 'mac_status': 'not_probed',
                'file_reads': 'host-bound snapshot or fixed executor only; current project determines tools',
                'mac_actions': ('fixed executor; availability not asserted; no offline queue' if self.executor
                                else 'unavailable; requests are not queued'),
                'profile_projection': 'server only; no desktop preset installed or synchronized',
                'deployment_status': 'process running; production rollout and independent acceptance not asserted',
                'independent_acceptance': 'not supplied; no phone receipt evidence'}

    @staticmethod
    def profile_result(result):
        if 'desktop_profile_synced' in result:
            result['server_profile_synced'] = result.pop('desktop_profile_synced')
            result['profile_projection'] = 'server_only'
            if not result['server_profile_synced'] and 'message' in result:
                result['message'] = '偏好变更已记录，但服务器偏好快照同步未确认；不表示桌面同步。'
        return result

    def profile_intent(self, *args):
        return self.profile_result(self.agent.profile_intent(*args))

    def source(self, payload):
        sender, channel = payload.get('sender'), payload.get('channel')
        if not isinstance(sender, str) or sender not in self.senders or not isinstance(channel, str) or channel not in self.channels:
            raise AgentError('AUTH_FAIL', '消息来源未获服务器助手授权')
        return sender, channel

    def message(self, payload):
        sender, channel = self.source(payload)
        if set(payload) != {'sender', 'channel', 'delivery_id', 'text'}:
            raise AgentError('INVALID_MESSAGE', '消息字段无效；项目和执行端由宿主绑定')
        return self.agent.submit(sender, channel, payload['delivery_id'], payload['text'], self.projects.current(sender, channel))

    def control(self, payload):
        sender, channel = self.source(payload)
        op = payload.get('op')
        if isinstance(op, str) and op in MAC_CONTROLS:
            if self.executor is None:
                raise AgentError('MAC_TOOLS_UNAVAILABLE', 'Mac工具尚未连接，未登记或排队执行；旧/mac仍走原链路')
            fields = {'agent.tasks': {'task_id'}, 'agent.task.cancel': {'task_id', 'delivery_id'},
                      'agent.approvals': {'approval_id'}, 'agent.approve': {'approval_id', 'delivery_id'},
                      'agent.deny': {'approval_id', 'delivery_id'}, 'agent.events': {'after'}}
            if set(payload) - fields[op] - {'op', 'sender', 'channel'} or any(not isinstance(v, str) for v in payload.values()):
                raise AgentError('INVALID_MESSAGE', '工具控制字段无效')
            project = self.projects.current(sender, channel)
            conversation = self.agent.current_conversation(sender, channel, project)
            if op == 'agent.events':
                if not re.fullmatch('[0-9]{1,18}', payload.get('after', '0')):
                    raise AgentError('INVALID_MESSAGE', '事件游标格式错误')
                return self.executor.events(sender, channel, conversation, project, int(payload.get('after', '0')))
            subject = dict(sender=sender, channel=channel, conversation=conversation, project=project,
                           attempt='control')
            return self.executor.call(subject, {'agent.tasks': 'tasks', 'agent.task.cancel': 'cancel',
                                      'agent.approvals': 'approvals', 'agent.approve': 'approve', 'agent.deny': 'deny'}[op],
                                      {field: payload.get(field, '') for field in fields[op]})
        if not isinstance(op, str) or op not in CONTROL_FIELDS:
            raise AgentError('INVALID_MESSAGE', '服务器助手不支持该控制操作')
        if set(payload) - CONTROL_FIELDS[op] - {'op', 'sender', 'channel'} or any(not isinstance(v, str) for v in payload.values()):
            raise AgentError('INVALID_MESSAGE', '控制字段格式错误或包含非白名单字段')
        if op == 'agent.projects':
            return self.projects.listing(sender, channel)
        if op == 'agent.use':
            return self.projects.use(sender, channel, payload.get('project', ''), payload.get('delivery_id', ''))
        if op.startswith('agent.service.'):
            if self.agent.services is None:
                raise AgentError('SERVICE_DISABLED', '个人服务未启用')
            if op == 'agent.service.requests':
                return self.agent.services.pending(sender, channel)
            try:
                return self.agent.services.resolve(sender, channel, payload.get('request_id', ''), payload.get('result', ''))
            except (ValueError, TypeError):
                raise AgentError('INVALID_MESSAGE', '服务结果须为明确状态的JSON对象') from None
        return self.profile_result(self.agent.control(sender, channel, op, self.projects.current(sender, channel), payload.get('message_id', ''),
                                   payload.get('delivery_id', ''), payload.get('memory_id', ''),
                                   payload.get('name', ''), payload.get('category', ''),
                                   payload.get('value', ''), payload.get('draft_id', '')))

    def close(self):
        if not self.closed:
            self.agent.close()
            if self.executor:
                self.executor.close()
            self.closed = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'agentbridge-assistant'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def _json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Durable acceptance is queried using the original delivery ID.

    def _authed(self):
        supplied = self.headers.get('X-AgentBridge-Secret', '')
        if not hmac.compare_digest(supplied.encode(), self.server.app.secret.encode()):
            self.close_connection = True
            self._json(401, {'ok': False, 'code': 'AUTH_FAIL'})
            return False
        return True

    def _body(self):
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            raise AgentError('INVALID_MESSAGE', '须使用单一Content-Length，不接受分块消息')
        try:
            size = int(self.headers['Content-Length'])
            if not 0 < size <= 65536:
                raise ValueError
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, TypeError):
            raise AgentError('INVALID_MESSAGE', '请求体为空、过大或不是JSON对象') from None

    def do_GET(self):
        if self._authed():
            if self.path == '/health':
                self._json(200, {'ok': True, **self.server.app.runtime_info()})
            else:
                self._json(404, {'ok': False, 'code': 'NOT_FOUND'})

    def do_POST(self):
        if not self._authed():
            return
        try:
            if self.path not in ('/agent/messages', '/control'):
                self.close_connection = True
                return self._json(404, {'ok': False, 'code': 'NOT_FOUND'})
            payload = self._body()
            result = self.server.app.message(payload) if self.path == '/agent/messages' else self.server.app.control(payload)
            self._json(200, result)
        except AgentError as error:
            self.close_connection = True
            self._json(403 if error.code == 'AUTH_FAIL' else 400, {'ok': False, 'code': error.code, 'message': error.message})
        except Exception:
            self.close_connection = True
            self._json(500, {'ok': False, 'code': 'RESULT_UNKNOWN', 'message': '结果未确认，不自动重试；请查询原投递ID'})


def create_server(app, port=9010):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.app = app
    return server


def main():
    os.umask(0o077)
    if os.environ.get('AGENTBRIDGE_AGENT_MESSAGES') != '1' or os.environ.get('AGENTBRIDGE_AGENT_LEARNING', '0') != '0':
        raise SystemExit('assistant requires explicit enablement and automatic learning disabled')
    from tools.check_server_runtime import NODE_VERSION, linux_budget, locked_packages, private_credential
    try:
        node = Path(os.environ['AGENTBRIDGE_AGENT_NODE'])
        packages = Path(os.environ['AGENTBRIDGE_AGENT_PACKAGES'])
        credential = Path(os.environ['AGENTBRIDGE_AGENT_CREDENTIALS'])
        required(os.environ['AGENTBRIDGE_AGENT_CREDENTIAL_REF'], 128)
        if (not sys.platform.startswith('linux') or os.getuid() == 0 or not linux_budget()[0]
                or not node.is_absolute() or not packages.is_absolute() or not credential.is_absolute()
                or subprocess.check_output([str(node), '--version'], text=True, timeout=5).strip() != NODE_VERSION
                or not locked_packages(packages)):
            raise ValueError
        private_credential(credential)
        secret = private_credential(os.environ['AGENTBRIDGE_ASSISTANT_SECRET_FILE']).decode().strip()
        if len(secret) < 32:
            raise ValueError
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, AgentError):
        raise SystemExit('assistant requires verified Linux/non-root/cgroup runtime and explicit private credentials') from None
    senders = {v.strip() for v in os.environ.get('AGENTBRIDGE_ALLOWED_SENDERS', '').split(',') if v.strip()}
    channels = {v.strip() for v in os.environ.get('AGENTBRIDGE_AGENT_CHANNELS', '').split(',') if v.strip()}
    app = App(os.environ['AGENTBRIDGE_ASSISTANT_ROOT'], secret, senders, channels,
              services=os.environ.get('AGENTBRIDGE_PERSONAL_SERVICES', '0') == '1',
              executor_url=os.environ.get('AGENTBRIDGE_EXECUTOR_URL'),
              executor_secret=(private_credential(os.environ['AGENTBRIDGE_EXECUTOR_SECRET_FILE']).decode().strip()
                               if os.environ.get('AGENTBRIDGE_EXECUTOR_URL') else None),
              task_projects={v for v in os.environ.get('AGENTBRIDGE_AGENT_TASK_PROJECTS', '').split(',') if v})
    server = None

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server = create_server(app)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        app.close()


if __name__ == '__main__':
    main()
