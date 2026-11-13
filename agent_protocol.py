"""Shared validation errors without importing chat or personality runtimes."""
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
