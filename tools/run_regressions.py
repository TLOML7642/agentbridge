"""Run complete regression once, then repeat concurrent/failure paths 20 times."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
repeated = ['test_s1_s5', 'test_credentials', 'test_bridge', 'test_experience', 'test_stability', 'test_tunnel_isolation']
with tempfile.TemporaryDirectory(prefix='agentbridge-s1-s5-suite-') as scratch:
    for index in range(21):
        modules = ['test_worker', 'test_shim', *repeated] if index == 0 else repeated
        start = time.monotonic()
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', *modules], capture_output=True, text=True,
                                env={**os.environ, 'TMPDIR': scratch})
        print(f'round={index} elapsed={time.monotonic() - start:.1f}s exit={result.returncode}', flush=True)
        print(result.stderr, flush=True)
        if 'Traceback' in result.stderr or 'ResourceWarning' in result.stderr or 'Exception in thread' in result.stderr:
            sys.exit('Uncaught exception or resource warning invalidates the regression run')
        if result.returncode:
            sys.exit(result.returncode)
