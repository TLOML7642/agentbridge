"""Supervise the one-action native driver in the existing process-group gate."""
import json
import os
from pathlib import Path
import stat
import sys

from agent_runtime import AgentError


class NativeTaskExecutor:
    def __init__(self, root, run_task, identity, settle, redact=lambda value: value):
        self.root = Path(root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.run_task, self.identity, self.settle, self.redact = run_task, identity, settle, redact

    def receipt(self, task):
        try:
            descriptor = os.open(self.root / 'receipts' / (task['id'] + '.json'), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
                    return None
                data = json.loads(source.read(65537))
            subject = {key: task[key] for key in ('sender', 'channel', 'conversation', 'project', 'cwd', 'session')}
            subject['attempt'] = task['id']
            if (data.get('id') != task['id'] or data.get('session') != task['session'] or data.get('digest') != task['digest']
                    or data.get('subject') != subject or not isinstance(data.get('result'), dict)
                    or data['result'].get('completion') not in ('verified', 'process-ended', 'denied')):
                return None
            def sanitized(value):
                if isinstance(value, str):
                    return self.redact(value)
                if isinstance(value, list):
                    return [sanitized(item) for item in value]
                if isinstance(value, dict):
                    return {key: sanitized(item) for key, item in value.items()}
                return value
            return sanitized(data['result'])
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def recover(self, task):
        # Even a valid receipt is not permission to ignore a surviving process.
        if self.settle(task) not in ('dead', 'killed'):
            return None
        return self.receipt(task)

    def __call__(self, task, cancel):
        private = self.root.parent.parent
        payload = {key: task[key] for key in ('id', 'session', 'subject', 'digest', 'tool', 'broker')}
        payload.update(root=str(self.root), python=sys.executable, arguments=json.loads(task['arguments']),
                       private_prefixes=[str(private), *[str(private.parent / name) for name in ('control', 'logs', 'audit')]])
        request = self.root / (task['id'] + '.request.json')
        descriptor = os.open(request, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            json.dump(payload, output, ensure_ascii=False)
        node = os.environ.get('AGENTBRIDGE_AGENT_NODE', str(Path.home() / '.deepseek-harness/bin/node'))
        command = ['/usr/bin/env', 'ELECTRON_RUN_AS_NODE=1', node, str(Path(__file__).with_name('agent_task_driver.mjs')), str(request)]

        def registered(pgid):
            identity = self.identity(pgid)
            if not identity:
                raise AgentError('TASK_UNCERTAIN', '无法确认后台执行器身份')
            task['register_executor'](pgid, identity)

        try:
            result = self.run_task(command, cwd=task['cwd'], timeout=300, on_start=registered, cancel_check=cancel.is_set)
            if result.get('cancelled') and result.get('group_stopped'):
                raise AgentError('CANCELLED', '后台执行器已停止；已发生的副作用不撤销')
            if not result.get('group_stopped', False):
                raise AgentError('TASK_UNCERTAIN', '后台执行器停止状态未知')
            receipt = self.receipt(task)
            if receipt is None:
                raise AgentError('TASK_UNCERTAIN', '后台动作未返回匹配的持久完成收据')
            return receipt
        finally:
            request.unlink(missing_ok=True)
