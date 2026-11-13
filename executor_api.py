"""Fixed authenticated host-to-Mac operations. Never accepts paths/URLs as bindings."""
import os
from pathlib import Path

from agent_protocol import AgentError, required
import agent_reads

FIELDS = {
    'health': set(), 'read': {'operation', 'value', 'range'},
    'submit': {'call_id', 'tool', 'arguments'},
    'tasks': {'task_id'}, 'cancel': {'task_id', 'delivery_id'},
    'approvals': {'approval_id'}, 'approve': {'approval_id', 'delivery_id'},
    'deny': {'approval_id', 'delivery_id'}, 'events': {'after'},
    'computer_inspect': {'operation', 'value'},
    'permissions': set(), 'revoke_permission': {'permission_id', 'delivery_id'},
    'approve_run': {'approval_id', 'delivery_id'},
}


def dispatch(app, payload, senders, channels):
    if getattr(app, 'role', '') != 'executor_only':
        raise AgentError('EXECUTOR_ONLY_REQUIRED', '执行接口只在持久executor-only角色启用')
    sender, channel, op = (payload.get(key) for key in ('sender', 'channel', 'op'))
    if not isinstance(sender, str) or sender not in senders or not isinstance(channel, str) or channel not in channels:
        raise AgentError('AUTH_FAIL', '执行端来源未授权')
    if not isinstance(op, str) or op not in FIELDS or set(payload) != FIELDS[op] | {'sender', 'channel', 'op', 'subject'}:
        raise AgentError('INVALID_MESSAGE', '执行端仅接受固定接口字段')
    if op == 'health':
        return {'ok': True, 'role': app.role, 'boot_id': app.boot_id}
    subject = payload['subject']
    if (not isinstance(subject, dict) or set(subject) != {'sender', 'channel', 'project', 'conversation', 'attempt'}
            or subject['sender'] != sender or subject['channel'] != channel):
        raise AgentError('AUTH_FAIL', '执行端工具身份不匹配')
    for value in subject.values():
        required(value, 256)
    tasks = app.agent_tasks
    if op in ('permissions', 'revoke_permission'):
        if tasks is None:
            raise AgentError('WRITES_DISABLED', '电脑助手尚未启用')
        return tasks.permissions(sender, channel, payload['permission_id'] if op == 'revoke_permission' else None)
    if op == 'computer_inspect':
        if tasks is None:
            raise AgentError('WRITES_DISABLED', '电脑助手尚未启用')
        return {'ok': True, 'value': tasks.inspect_computer(payload['operation'], payload['value']),
                'evidence_location': 'mac_live', 'boot_id': app.boot_id}
    if op == 'read':
        cwd = app.get_project(subject['project'])['dir']
        if not isinstance(payload['range'], dict) or set(payload['range']) - {'literal', 'max_matches', 'start_line', 'max_lines', 'expected_version'}:
            raise AgentError('INVALID_MESSAGE', '读取参数无效')
        info = os.stat(cwd)
        value = agent_reads.query({**payload['range'], 'root': cwd, 'value': payload['value'],
                                  'operation': payload['operation'], 'device': info.st_dev, 'inode': info.st_ino,
                                  'private_prefixes': [str(Path(app.db.path).parent / name)
                                                       for name in ('agent', 'logs', 'audit', 'control')]})
        return {'ok': True, 'value': value, 'evidence_location': 'mac_live', 'boot_id': app.boot_id}
    if tasks is None:
        raise AgentError('WRITES_DISABLED', 'Mac后台动作未启用')
    if op == 'submit':
        subject = {**subject, 'cwd': tasks.workspace(subject['project'], payload['tool'])}
        return tasks.submit(subject, payload['call_id'], payload['tool'], payload['arguments'])
    args = (sender, channel, subject['conversation'], subject['project'])
    if op == 'tasks':
        return tasks.inspect(*args, payload['task_id'])
    if op == 'cancel':
        return tasks.cancel(*args, payload['task_id'], payload['delivery_id'])
    if op == 'events':
        return tasks.events(*args, after=int(payload['after']))
    if op == 'approvals':
        result = tasks.approvals.inspect(*args, payload['approval_id'])
    else:
        result = tasks.decide(*args, payload['approval_id'], payload['delivery_id'], op in ('approve', 'approve_run'),
                              remember=op == 'approve_run')
    if 'approvals' in result:
        result['approvals'] = [{key: value for key, value in item.items() if key != 'grant'} for item in result['approvals']]
    result.pop('grant', None)
    return result
