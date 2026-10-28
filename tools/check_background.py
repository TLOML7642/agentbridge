"""Isolated discovery plus repeated background/chat/permission fault regression."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True, type=Path)
    parser.add_argument('--packages', required=True, type=Path)
    parser.add_argument('--report-dir', required=True, type=Path)
    parser.add_argument('--rounds', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 20 or not args.node.is_absolute() or not args.packages.is_absolute():
        parser.error('explicit absolute native paths and 1..20 rounds required')
    root = Path(__file__).resolve().parents[1]
    report = args.report_dir.resolve()
    if report == root or root in report.parents:
        parser.error('private reports must be outside the source repository')
    os.umask(0o077)
    report.mkdir(parents=True, exist_ok=False)
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root).decode().split('\0')
    repeated = ['test_agent_tasks', 'test_agent_runtime', 'test_agent_reads', 'test_agent_approvals', 'test_config_invariants']
    results = []
    with tempfile.TemporaryDirectory(prefix='ab-background-', dir='/private/tmp') as scratch:
        scratch = Path(scratch)
        source, home, runtime = (scratch / name for name in ('source', 'home', 'runtime'))
        for directory in (source, home, runtime):
            directory.mkdir(mode=0o700)
        hashes = {}
        for name in names:
            if not name:
                continue
            file = root / name
            if file.is_symlink() or not file.is_file() or root not in file.resolve().parents:
                raise ValueError('unsafe source path')
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        (report / 'source-hashes.json').write_text(json.dumps(hashes, indent=2, sort_keys=True) + '\n')
        # Keep only the small environment required for local synthetic tests.
        env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home), 'TMPDIR': str(scratch),
               'AGENTBRIDGE_BASE': str(runtime), 'AGENTBRIDGE_AGENT_MESSAGES': '0', 'AGENTBRIDGE_AGENT_TASKS': '0',
               'AGENTBRIDGE_AGENT_LEARNING': '0', 'AGENTBRIDGE_TEST_NATIVE_NODE': str(args.node),
               'AGENTBRIDGE_TEST_NATIVE_PACKAGES': str(args.packages), 'ELECTRON_RUN_AS_NODE': '1'}
        for index in range(args.rounds + 1):
            modules = ['discover', '-p', 'test_*.py'] if index == 0 else repeated
            command = [sys.executable, '-B', '-m', 'unittest', *modules]
            started = time.monotonic()
            result = subprocess.run(command, cwd=source, env=env, capture_output=True, text=True, timeout=300)
            output = result.stdout + result.stderr
            (report / f'round-{index:02d}.log').write_text(output)
            count = re.search(r'Ran (\d+) tests? in ', output)
            clean = result.returncode == 0 and count is not None and not any(
                marker in output for marker in ('Traceback', 'ResourceWarning', 'Exception in thread', 'skipped='))
            results.append({'round': index, 'tests': int(count[1]) if count else None, 'exit': result.returncode,
                            'clean': clean, 'seconds': round(time.monotonic() - started, 3)})
            (report / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            print(json.dumps(results[-1]), flush=True)
            if not clean:
                return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
