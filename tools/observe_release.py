"""Bounded read-only observation. Every failure is recorded; never a daemon."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time


def utc():
    return datetime.now(timezone.utc).isoformat()


# No secrets, message bodies, environment or process command lines leave the server.
REMOTE = r'''
import hashlib, importlib.util, json, pathlib, subprocess, time
s=importlib.util.spec_from_file_location('release','/var/tmp/agentbridge-s1-s5-validation/release/server_release.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
r={'time': time.time(), 'ok': True}
try:
 rows, env, headers=m.runtime()
 r['containers']=[{'name': x['Name'], 'running': x['State']['Running'], 'oom': x['State']['OOMKilled'], 'restarts': x['RestartCount']} for x in rows]
 r['ok']=all(x['running'] and not x['oom'] for x in r['containers'])
 for name,url,key in [('shim','http://127.0.0.1:8787/health','Authorization'),('worker','http://127.0.0.1:9000/health','X-AgentBridge-Secret')]:
  try:
   req=m.urllib.request.Request(url,headers={key: 'Bearer '+env['SHIM_TOKEN'] if name=='shim' else env['AGENTBRIDGE_SECRET']})
   with m.urllib.request.urlopen(req,timeout=4) as res: r[name+'_healthy']=res.status==200 and json.load(res).get('ok',True)
  except Exception as e: r[name+'_healthy']=False; r[name+'_error']=type(e).__name__
  r['ok']=r['ok'] and r[name+'_healthy']
 r['source_hashes']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [pathlib.Path('/opt/agentbridge/shim/shim.py'),pathlib.Path('/opt/agentbridge/astrbot/data/plugins/agentbridge_mac/main.py')]}
except Exception as e: r['ok']=False; r['runtime_error']=type(e).__name__
p=subprocess.run(['ss','-H','-ltnp','sport = :9000'],capture_output=True,text=True,timeout=3)
r['listener_count']=len(p.stdout.splitlines()); r['listener_probe_exit']=p.returncode
print(json.dumps(r))
'''


def probe():
    result = {"ok": True, "utc": utc()}
    for name in ("tunnel", "worker"):
        try:
            check = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.agentbridge.{name}"],
                                   capture_output=True, text=True, timeout=3, check=True)
            for key in ("state", "runs", "pid"):
                hit = re.search(r"^\s*" + key + r" = (\S+)", check.stdout, re.M)
                result[name + "_" + key] = int(hit[1]) if hit and key != "state" else hit[1] if hit else None
            result["ok"] &= result[name + "_state"] == "running"
        except Exception as exc:
            result[name + "_error"] = type(exc).__name__
            result["ok"] = False
    try:
        with socket.create_connection(("127.0.0.1", 9000), timeout=2):
            result["local_worker_listener"] = True
    except OSError as exc:
        result.update(ok=False, local_worker_listener=False, local_worker_error=type(exc).__name__)
    try:
        check = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=5",
                                "-o", "ServerAliveCountMax=1", "agentbridge",
                                "sudo python3 -B -c " + shlex.quote(REMOTE)],
                               capture_output=True, text=True, timeout=23)
        result["management_ssh_exit"] = check.returncode
        if check.returncode:
            result.update(ok=False, management_ssh_error="ssh_failed")
        else:
            result["remote"] = json.loads(check.stdout)
            result["ok"] &= bool(result["remote"]["ok"])
    except Exception as exc:
        result.update(ok=False, management_ssh_error=type(exc).__name__)
    result["local_source_hashes"] = {
        name: hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
        for name in ("worker.py", "shim.py", "server/plugins/agentbridge_mac/main.py")}
    return result


def observe(evidence, duration=1800, interval=30, sampler=probe):
    if duration < 0 or interval <= 0:
        raise ValueError("Invalid observation window")
    evidence.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    rows, recoveries, baseline = [], [], {}
    start, outage, longest = time.monotonic(), None, 0.0
    with os.fdopen(fd, "w") as output:
        for index in range(int(duration / interval) + 1):
            time.sleep(max(0, start + index * interval - time.monotonic()))
            sampled_at = time.monotonic()
            try:
                row = sampler()
            except Exception as exc:
                row = {"ok": False, "probe_error": type(exc).__name__}
            row.update(index=index, utc=utc(), elapsed_seconds=sampled_at - start)
            for key in ("tunnel_runs", "worker_runs"):
                if row.get(key) is not None:
                    baseline.setdefault(key, row[key])
                    if baseline[key] != row[key]:
                        row["unplanned_restart"] = True
            if not row.get("ok"):
                if outage is None:
                    outage = sampled_at
                longest = max(longest, sampled_at - outage)
            elif outage is not None:
                elapsed = sampled_at - outage
                longest = max(longest, elapsed)
                recoveries.append({"utc": row["utc"], "unreachable_seconds": elapsed})
                outage = None
            rows.append(row)
            output.write(json.dumps(row) + "\n")
            output.flush()
            os.fsync(output.fileno())
            print(json.dumps(row), flush=True)
        summary = {"type": "summary", "utc": utc(), "samples": len(rows),
                   "failures": sum(not r.get("ok") for r in rows),
                   "unplanned_restart": any(r.get("unplanned_restart") for r in rows),
                   "longest_unreachable_seconds": longest, "recoveries": recoveries,
                   "unrecovered": outage is not None}
        summary["passed"] = not summary["failures"] and not summary["unplanned_restart"]
        summary["release_gate_passed"] = summary["passed"] and duration >= 1800 and interval <= 30 and len(rows) >= 61
        output.write(json.dumps(summary) + "\n")
        output.flush()
        os.fsync(output.fileno())
        print(json.dumps(summary), flush=True)
    return rows, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=1800)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    _, summary = observe(args.evidence, args.duration, args.interval)
    raise SystemExit(0 if summary["release_gate_passed"] else 1)
