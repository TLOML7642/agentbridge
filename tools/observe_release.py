"""Bounded 30-minute release observation. Read-only; exits without a daemon."""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

os.umask(0o077)
evidence = Path.home() / '.local/share/agentbridge-releases/20261026-s1-s5/post-deploy.jsonl'
start = time.monotonic()
started_at = time.time()
baseline_runs = None
for seconds in range(0, 1801, 300):
    time.sleep(max(0, start + seconds - time.monotonic()))
    check = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'agentbridge',
                            'sudo python3 -B /var/tmp/agentbridge-s1-s5-validation/release/server_release.py probe'],
                           capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(check.stdout)
    tunnel = subprocess.check_output(['launchctl', 'print', 'gui/501/com.agentbridge.tunnel'], text=True)
    state = re.search(r'^\s*state = (\S+)', tunnel, re.M).group(1)
    runs = int(re.search(r'^\s*runs = (\d+)', tunnel, re.M).group(1))
    if baseline_runs is None:
        baseline_runs = runs
    assert state == 'running' and runs == baseline_runs, 'Tunnel restarted during observation'
    with sqlite3.connect(f'file:{Path.home()}/agentbridge/agentbridge.db?mode=ro', uri=True) as db:
        result['new_uncertain_jobs'] = db.execute(
            "SELECT count(*) FROM jobs WHERE status='uncertain' AND created_at>=?", (started_at,)).fetchone()[0]
        result['active_jobs'] = dict(db.execute(
            "SELECT status,count(*) FROM jobs WHERE status IN ('running','queued','pending-confirm') GROUP BY status"))
    assert not result['new_uncertain_jobs']
    result.update({'elapsed_seconds': seconds, 'tunnel_state': state, 'tunnel_runs': runs})
    with evidence.open('a') as output:
        output.write(json.dumps(result) + '\n')
    print(json.dumps(result), flush=True)
print('30-minute post-release observation passed', flush=True)
