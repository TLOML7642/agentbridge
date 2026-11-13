"""Compare a read-only baseline PID with an isolated synthetic Mac executor."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def cpu_seconds(value):
    fields = value.replace('-', ':').split(':')
    return sum(float(part) * factor for part, factor in zip(reversed(fields), (1, 60, 3600, 86400)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-pid', type=int, required=True)
    parser.add_argument('--seconds', type=int, default=600)
    args = parser.parse_args()
    if args.seconds < 600 or args.baseline_pid <= 1:
        parser.error('explicit baseline PID and at least 600 seconds required')
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix='ab-idle-', dir='/private/tmp'))
    source, home, runtime = (root / name for name in ('source', 'home', 'runtime'))
    for directory in (source, home, runtime):
        directory.mkdir(mode=0o700)
    repo = Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=repo).decode().split('\0')
    hashes = {}
    for name in filter(None, paths):
        file = repo / name
        if file.is_symlink() or not file.is_file():
            raise ValueError('unsafe source')
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, dest)
        hashes[name] = hashlib.sha256(dest.read_bytes()).hexdigest()
    (root / 'source-hashes.json').write_text(json.dumps(hashes, sort_keys=True))
    env = dict(PATH='/usr/bin:/bin', HOME=str(home), AGENTBRIDGE_BASE=str(runtime),
               AGENTBRIDGE_WORKER_ROLE='executor_only', AGENTBRIDGE_COMPUTER_ROOT=str(home),
               AGENTBRIDGE_SECRET='synthetic-secret', AGENTBRIDGE_ALLOWED_SENDERS='synthetic-owner',
               AGENTBRIDGE_AGENT_CHANNELS='synthetic-private', AGENTBRIDGE_PORT='0')
    print(root, flush=True)
    process = subprocess.Popen([sys.executable, '-B', 'executor_service.py'], cwd=source, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=(root / 'service.stderr').open('w'))
    samples = []
    children = set()
    try:
        time.sleep(1)
        started = time.monotonic()
        while True:
            if process.poll() is not None:
                raise RuntimeError('candidate exited')
            output = subprocess.check_output(['ps', '-p', str(args.baseline_pid) + ',' + str(process.pid),
                                              '-o', 'pid=,rss=,time='], text=True)
            row = {'elapsed': time.monotonic() - started}
            for line in output.splitlines():
                pid, rss, cpu = line.split()
                row['candidate' if int(pid) == process.pid else 'baseline'] = {'rss_kib': int(rss), 'cpu_seconds': cpu_seconds(cpu)}
            if set(row) != {'elapsed', 'candidate', 'baseline'}:
                raise RuntimeError('baseline or candidate disappeared')
            samples.append(row)
            processes = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,comm='], text=True)
            for line in processes.splitlines():
                pid, parent, command = line.strip().split(None, 2)
                if int(parent) == process.pid:
                    children.add(command)
            if row['elapsed'] >= args.seconds:
                break
            time.sleep(min(5, args.seconds - row['elapsed']))
        summary = {'duration_seconds': samples[-1]['elapsed'] - samples[0]['elapsed'], 'samples': len(samples),
                   'candidate_child_commands_observed': sorted(children), 'boundary': 'idle process metrics, not workflow acceptance'}
        for key in ('baseline', 'candidate'):
            cpu = (samples[-1][key]['cpu_seconds'] - samples[0][key]['cpu_seconds']) / summary['duration_seconds'] * 100
            rss = max(row[key]['rss_kib'] for row in samples)
            summary[key] = {'average_single_core_cpu_percent': cpu, 'peak_rss_mib': rss / 1024}
        summary['candidate_passed'] = (summary['candidate']['average_single_core_cpu_percent'] <= 1
                                       and summary['candidate']['peak_rss_mib'] <= 100 and not children)
        (root / 'samples.json').write_text(json.dumps(samples))
        (root / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        return 0 if summary['candidate_passed'] else 1
    finally:
        process.terminate()
        process.wait(timeout=25)


if __name__ == '__main__':
    sys.exit(main())
