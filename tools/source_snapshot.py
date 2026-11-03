"""Export a named reviewed source scope from an independently verified Git commit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_reads import LEGACY_SCOPE, SOURCE_SCOPES


def export(repo, commit, destination, scope=LEGACY_SCOPE):
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('full independently verified GitHub commit required')
    if scope not in SOURCE_SCOPES:
        raise ValueError('unknown source scope')
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    hashes = {}
    for name in sorted(SOURCE_SCOPES[scope]):
        mode = subprocess.check_output(['git', 'ls-tree', commit, '--', name], cwd=repo).decode().split()[0]
        if mode != '100644':
            raise ValueError('reviewed source must be a regular Git blob')
        data = subprocess.check_output(['git', 'show', commit + ':' + name], cwd=repo)
        if len(data) > 1048576 or b'\0' in data:
            raise ValueError('source file is not bounded text')
        data.decode('utf-8')
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o444)
        hashes[name] = hashlib.sha256(data).hexdigest()
    manifest = {'format': 1, 'scope': scope, 'commit': commit, 'created': time.time(), 'files': hashes,
                'evidence_location': 'github_source_snapshot', 'runtime_or_mac_state': False}
    (destination / 'snapshot.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (destination / 'snapshot.json').chmod(0o444)
    for path in sorted(destination.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
    destination.chmod(0o555)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--verified-commit', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--scope', choices=sorted(SOURCE_SCOPES), default=LEGACY_SCOPE)
    args = parser.parse_args()
    result = export(args.repo, args.verified_commit, args.destination, args.scope)
    print(json.dumps({'commit': result['commit'], 'files': len(result['files']), 'runtime_or_mac_state': False}))
