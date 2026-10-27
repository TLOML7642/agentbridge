"""Add a dedicated chat-only desktop preset; never change defaults or sessions."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def create(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = content.encode()
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError('existing file differs; preserve it and review a separate upgrade')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def install(home, profile):
    home = Path(home).expanduser().resolve()
    profile = Path(profile).expanduser().absolute()
    if not profile.is_absolute():
        raise ValueError('private absolute profile path required')
    source = Path(__file__).resolve().parents[1] / 'agent_persona.mjs'
    content = source.read_text()
    sha = hashlib.sha256(content.encode()).hexdigest()
    fixed = home / 'plugins/sources' / ('agentbridge-persona-' + sha[:16] + '.mjs')
    wrapper = home / 'profiles/web/agentbridge-shared-persona.mjs'
    preset = home / '.agent-presets/dsh-shared'
    # Exclusive, content-checked authoring. No rename/delete/update of any existing preset.
    create(fixed, content)
    create(wrapper, 'export * from ' + json.dumps(str(fixed)) + ';\n')
    create(preset / 'agent.cordis.yml', json.dumps([{'name': str(wrapper), 'config': {'profile': str(profile)}}], ensure_ascii=False, indent=2) + '\n')
    create(preset / 'preset.yml', json.dumps({'name': 'DSH 共享人格', 'description': '共享有效偏好，独立聊天历史；仅聊天，不提供电脑工具。', 'order': 20}, ensure_ascii=False, indent=2) + '\n')
    create(home / 'plugins/manifests/agentbridge-persona.json', json.dumps({
        'source_sha256': sha, 'preset': 'dsh-shared', 'default_changed': False,
        'existing_sessions_read': False, 'tools': [], 'installed_entry': str(wrapper),
    }, ensure_ascii=False, indent=2) + '\n')
    return {'preset': 'dsh-shared', 'source_sha256': sha, 'default_changed': False,
            'existing_sessions_read': False, 'tools': []}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.deepseek-harness')
    parser.add_argument('--profile', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(install(args.home, args.profile), ensure_ascii=False))
