"""Fingerprint static AstrBot policy, not official WeChat transport cursors."""
import copy
import hashlib
import json
from pathlib import Path


def static_config(config):
    if not isinstance(config, dict) or not isinstance(config.get('platform'), list):
        raise ValueError('invalid platform configuration')
    detached = copy.deepcopy(config)
    for platform in detached['platform']:
        if not isinstance(platform, dict):
            raise ValueError('invalid platform entry')
        if platform.get('type') != 'weixin_oc':
            continue
        # The installed official adapter persists these after messages. Its
        # account ID, login token, base URL, identity and all policy remain bound.
        if 'weixin_oc_sync_buf' in platform:
            if not isinstance(platform['weixin_oc_sync_buf'], str):
                raise ValueError('invalid synchronization cursor')
            del platform['weixin_oc_sync_buf']
        if 'weixin_oc_context_tokens' in platform:
            values = platform['weixin_oc_context_tokens']
            if not isinstance(values, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()):
                raise ValueError('invalid reply context state')
            del platform['weixin_oc_context_tokens']
    return detached


def static_fingerprint(config):
    encoded = json.dumps(static_config(config), ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate configuration key')
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError('invalid JSON constant')


def load_config(path):
    with Path(path).open('rb') as source:
        data = source.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('configuration exceeds reviewed size')
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique_object, parse_constant=invalid_constant)
