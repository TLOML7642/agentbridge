"""Shared validation errors without importing chat or personality runtimes."""
import json

from agent_diagnostics import failure_metadata


class AgentError(Exception):
    def __init__(self, code, message, diagnostic=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.diagnostic = failure_metadata(diagnostic)


def required(value, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise AgentError('INVALID_MESSAGE', '消息字段缺失或格式错误')
    return value


def bound_trash_progress(result, tool, arguments):
    if (not isinstance(result, dict) or result.get('completion') != 'uncertain'
            or result.get('evidence') != 'trash_stopped_with_progress'
            or set(result) != {'completion', 'evidence', 'moved', 'needs_check', 'not_attempted', 'permanently_deleted'}
            or result['permanently_deleted'] is not False or not isinstance(result['moved'], list)
            or not isinstance(result['not_attempted'], list)):
        return False
    try:
        args = json.loads(arguments)
        if tool == 'computer_trash_files':
            approved = json.loads(args['paths'])
        elif tool == 'computer_trash_file':
            approved = [args['path']]
        else:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    observed = result['moved'] + [result['needs_check']] + result['not_attempted']
    return (1 <= len(observed) <= 20 and all(isinstance(path, str) for path in observed)
            and observed == approved)
