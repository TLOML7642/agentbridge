"""P0-only native runtime probe. No listener, production state or deployment.

The parent starts a clean child with an empty HOME. Synthetic diagnostics never
approve P0; approval additionally requires Linux, a constrained non-root cgroup,
and two real requests using an explicitly supplied private credential document.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = 'v24.22.2'
CHECKS = frozenset(('linux_x64', 'non_root', 'cgroup_limits', 'node_version',
                    'locked_packages', 'directory_fsync', 'create_resume',
                    'durable_receipts', 'single_writer', 'restart_recovery',
                    'unknown_not_replayed', 'cancel_owned_process', 'resource_budget', 'real_api'))


def gate_passed(report):
    if not isinstance(report, dict) or report.get('stage') != 'P0':
        return False
    checks = report.get('checks')
    return (isinstance(checks, dict) and set(checks) == CHECKS
            and all(value is True for value in checks.values())
            and type(report.get('real_model_turns')) is int and report['real_model_turns'] == 2
            and report.get('mode') == 'real' and report.get('platform') == 'linux'
            and report.get('node') == NODE_VERSION and report.get('dsh') == '0.1.1-rc.2'
            and report.get('production_changed') is False)


def private_credential(path):
    """Reject links/devices/shared files before copying, without printing data."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= 65536):
            raise ValueError('credential document must be a private owned regular file')
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, 'rb') as source:
        data = source.read(65537)
        if len(data) > 65536:
            raise ValueError('credential document exceeds limit')
        return data


def locked_packages(packages):
    lock = json.loads((ROOT / 'server/assistant/package-lock.json').read_text())
    manifest = json.loads((ROOT / 'server/assistant/package.json').read_text())
    entries = lock['packages']
    if lock['lockfileVersion'] != 3 or entries['']['dependencies'] != manifest['dependencies']:
        return False
    for name, record in entries.items():
        if not name:
            continue
        if (not name.startswith('node_modules/') or '..' in Path(name).parts
                or not record.get('resolved', '').startswith('https://registry.npmjs.org/')
                or not re.fullmatch(r'sha512-[A-Za-z0-9+/]+={0,2}', record.get('integrity', ''))):
            return False
        installed = packages.parent.parent / name / 'package.json'
        # Optional binaries for other platforms need not be present.
        if record.get('optional') and not installed.exists():
            continue
        if json.loads(installed.read_text())['version'] != record['version']:
            return False
    return True


def linux_budget():
    try:
        memory = int(Path('/sys/fs/cgroup/memory.max').read_text())
        quota, period = map(int, Path('/sys/fs/cgroup/cpu.max').read_text().split())
        limited = 0 < memory <= 1073741824 and 0 < quota <= period
        available = next(int(line.split()[1]) / 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                         if line.startswith('MemAvailable:'))
        return limited, available
    except (OSError, ValueError, StopIteration):
        return False, None


def wait_for(runtime, identifier, timeout=140):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = runtime.control('p0-owner', 'p0-private', 'agent.status', 'p0', identifier)
        if result['state'] not in ('queued', 'running'):
            return result
        time.sleep(.02)
    raise TimeoutError('isolated native turn did not finish')


def exercise(node, packages, real):
    # Importing worker defines the existing supervisor but does not instantiate
    # App, the old scheduler, project registry or HTTP listener.
    sys.path.insert(0, str(ROOT))
    from agent_runtime import AgentRuntime, NativeExecutor
    from worker import run_task

    checks = dict.fromkeys(CHECKS, False)
    checks['linux_x64'] = sys.platform.startswith('linux') and platform.machine() == 'x86_64'
    checks['non_root'] = os.getuid() != 0
    checks['cgroup_limits'], available = linux_budget()
    actual_node = subprocess.check_output([str(node), '--version'], text=True).strip()
    checks['node_version'] = actual_node == NODE_VERSION
    checks['locked_packages'] = locked_packages(packages)
    report = {'stage': 'P0', 'checks': checks, 'production_changed': False, 'real_model_turns': 0,
              'mode': 'real' if real else 'synthetic', 'platform': sys.platform,
              'node': actual_node, 'dsh': '0.1.1-rc.2', 'host_available_mb': available}
    if not checks['node_version'] or not checks['locked_packages']:
        return report
    if real and not all(checks[key] for key in ('linux_x64', 'non_root', 'cgroup_limits')):
        return report
    home = Path.home()
    workspace = home / 'workspace'
    workspace.mkdir(mode=0o700)
    root = home / 'state/agent'
    root.mkdir(mode=0o700, parents=True)
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
        checks['directory_fsync'] = True
    finally:
        os.close(descriptor)
    executions = []
    nonce = 'P0-' + uuid.uuid4().hex
    synthetic_mode = True
    credentials = {key: os.environ.pop(key) for key in
                   ('AGENTBRIDGE_AGENT_CREDENTIALS', 'AGENTBRIDGE_AGENT_CREDENTIAL_REF') if key in os.environ}

    def supervised(command, **kwargs):
        request = Path(command[-1])
        if synthetic_mode:
            payload = json.loads(request.read_text())
            payload['synthetic'] = True
            request.write_text(json.dumps(payload))
        result = run_task(command, **kwargs)
        executions.append({key: result.get(key) for key in
                           ('exit_code', 'timed_out', 'peak_rss_mb', 'group_stopped', 'err_truncated')})
        return result

    executor = NativeExecutor(root, supervised, lambda text: text)

    def make():
        return AgentRuntime(root, executor, lambda _: str(workspace), recover=executor.recover)

    runtime = make()
    try:
        first = runtime.submit('p0-owner', 'p0-private', 'p0-1', '请记住这个测试标识，只回复此标识：' + nonce, 'p0')
        one = wait_for(runtime, first['message_id'])
        try:
            duplicate = make()
        except BlockingIOError:
            checks['single_writer'] = True
        else:
            duplicate.close()
        runtime.close()
        runtime = make()
        second = runtime.submit('p0-owner', 'p0-private', 'p0-2', '上一条用户消息中的测试标识是什么？只回复标识。', 'p0')
        two = wait_for(runtime, second['message_id'])
        checks['create_resume'] = (one['state'] == two['state'] == 'done'
                                   and first['session_id'] == second['session_id'])
        if checks['create_resume']:
            response = json.loads(two['reply'])
            checks['create_resume'] = response['turns'] == 2 and nonce in response['first']
        with runtime.db() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM messages ORDER BY seq')]
        checks['durable_receipts'] = all(executor.recover(row) == row['response'] for row in rows) and len(rows) == 2
        # Simulate the crash window after native fsync but before SQLite settlement.
        runtime.close()
        with sqlite3.connect(root / 'conversations.db') as db:
            db.execute("UPDATE messages SET state='running',response=NULL WHERE id=?", (second['message_id'],))
        before = len(executions)
        runtime = make()
        recovered = wait_for(runtime, second['message_id'])
        checks['restart_recovery'] = recovered == two and len(executions) == before
        runtime.close()
        # A missing native receipt must block the session, not resend the input.
        (root / 'native/receipts' / (second['message_id'] + '.json')).unlink()
        with sqlite3.connect(root / 'conversations.db') as db:
            db.execute("UPDATE messages SET state='running',response=NULL WHERE id=?", (second['message_id'],))
        runtime = make()
        unknown = wait_for(runtime, second['message_id'])
        with runtime.db() as db:
            blocked = db.execute('SELECT blocked FROM sessions WHERE id=?', (first['session_id'],)).fetchone()[0]
        checks['unknown_not_replayed'] = unknown['state'] == 'uncertain' and blocked == 1 and len(executions) == before
    finally:
        runtime.close()
    entered = workspace / 'cancel-entered'
    result = run_task([sys.executable, '-I', '-c',
                       'import pathlib,time; pathlib.Path("cancel-entered").write_text("synthetic"); time.sleep(60)'],
                      cwd=str(workspace), timeout=5, cancel_check=entered.exists)
    checks['cancel_owned_process'] = (entered.is_file() and result['cancelled'] is True
                                      and result['group_stopped'] is True)
    if real and all(checks[key] for key in CHECKS - {'real_api', 'resource_budget'}):
        # Real smoke uses a separate fresh store, never fault-injected state or
        # a production credential during the synthetic crash/cancel exercises.
        os.environ.update(credentials)
        synthetic_mode = False
        real_root = home / 'real-state/agent'
        real_executor = NativeExecutor(real_root, supervised, lambda text: text)

        def real_runtime():
            return AgentRuntime(real_root, real_executor, lambda _: str(workspace), recover=real_executor.recover)

        runtime = real_runtime()
        try:
            first = runtime.submit('p0-owner', 'p0-private', 'real-1', '请记住这个测试标识，只回复此标识：' + nonce, 'p0')
            one = wait_for(runtime, first['message_id'])
            runtime.close()
            runtime = real_runtime()
            second = runtime.submit('p0-owner', 'p0-private', 'real-2', '上一条用户消息中的测试标识是什么？只回复标识。', 'p0')
            two = wait_for(runtime, second['message_id'])
            report['real_model_turns'] = sum(row['state'] == 'done' for row in (one, two))
            checks['real_api'] = (one['state'] == two['state'] == 'done'
                                  and first['session_id'] == second['session_id']
                                  and nonce in one['reply'] and nonce in two['reply'])
        finally:
            runtime.close()
    peaks = [item['peak_rss_mb'] for item in executions]
    report['native_process_peaks_mb'] = peaks
    _, final_available = linux_budget()
    report['host_available_after_mb'] = final_available
    try:
        cgroup_peak = int(Path('/sys/fs/cgroup/memory.peak').read_text()) / 1048576
    except (OSError, ValueError):
        cgroup_peak = None
    report['cgroup_peak_mb'] = cgroup_peak
    healthy = all(item['exit_code'] == 0 and item['timed_out'] is False
                  and item['group_stopped'] is True and item['err_truncated'] is False for item in executions)
    checks['resource_budget'] = (bool(peaks) and all(type(peak) in (float, int) and 0 < peak <= 768 for peak in peaks)
                                 and available is not None and available >= 1024
                                 and final_available is not None and final_available >= 1024
                                 and cgroup_peak is not None and 0 < cgroup_peak <= 768 and healthy)
    return report


def run_probe(node, packages, report_dir, credential=None, credential_ref=None):
    report_dir = Path(report_dir).resolve()
    if report_dir == ROOT or ROOT in report_dir.parents:
        raise ValueError('private reports must be outside the repository')
    if (credential is None) != (credential_ref is None):
        raise ValueError('credential document and explicit reference must be supplied together')
    if credential and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', credential_ref):
        raise ValueError('invalid credential reference')
    if not Path(node).is_absolute() or not Path(packages).is_absolute():
        raise ValueError('explicit absolute runtime paths required')
    if credential and not Path(credential).is_absolute():
        raise ValueError('explicit absolute credential path required')
    report_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Retain the private synthetic state and failure evidence for later diagnosis.
    home = report_dir / 'home'
    home.mkdir(mode=0o700)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': str(home),
           'AGENTBRIDGE_BASE': str(home / 'unused-worker'), 'AGENTBRIDGE_AGENT_MESSAGES': '0',
           'AGENTBRIDGE_AGENT_TASKS': '0', 'AGENTBRIDGE_AGENT_LEARNING': '0',
           'AGENTBRIDGE_AGENT_NODE': str(Path(node).resolve(strict=True)),
           'AGENTBRIDGE_AGENT_PACKAGES': str(Path(packages).resolve(strict=True))}
    document = home / 'credentials.yaml'
    try:
        if credential:
            data = private_credential(credential)
            fd = os.open(document, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as target:
                target.write(data)
            env['AGENTBRIDGE_AGENT_CREDENTIALS'] = str(document)
            env['AGENTBRIDGE_AGENT_CREDENTIAL_REF'] = credential_ref
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--child']
        result = subprocess.run(command, env=env, cwd=home, capture_output=True, text=True, timeout=330)
        if result.returncode or result.stderr:
            # Do not expose raw exceptions or credential-bearing subprocess output.
            raise RuntimeError('isolated probe failed; P0 not approved')
        report = json.loads(result.stdout)
        report['gate_passed'] = gate_passed(report)
        file = report_dir / 'report.json'
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as target:
            target.write(json.dumps(report, indent=2) + '\n')
        return report
    finally:
        document.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    if sys.argv[1:] == ['--child']:
        try:
            report = exercise(Path(os.environ['AGENTBRIDGE_AGENT_NODE']),
                              Path(os.environ['AGENTBRIDGE_AGENT_PACKAGES']),
                              'AGENTBRIDGE_AGENT_CREDENTIALS' in os.environ)
            print(json.dumps(report))
            return 0
        except Exception as error:
            # Error class only: never log prompts, responses, credentials or raw errors.
            print(json.dumps({'stage': 'P0', 'checks': {}, 'production_changed': False,
                              'real_model_turns': 0, 'failure_type': type(error).__name__}))
            return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--packages', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--credential', type=Path)
    parser.add_argument('--credential-ref')
    args = parser.parse_args()
    try:
        report = run_probe(args.node, args.packages, args.report_dir, args.credential, args.credential_ref)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print('P0 incomplete; no deployment or approval.', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report['gate_passed'] else (1 if 'failure_type' in report else 2)


if __name__ == '__main__':
    sys.exit(main())
