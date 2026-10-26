"""Verify direct TCP channels are denied for the dedicated tunnel key."""
import select
import socket
import subprocess
import time

with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    port = reservation.getsockname()[1]
proc = subprocess.Popen(['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                         '-o', 'ConnectTimeout=10', '-L', f'127.0.0.1:{port}:127.0.0.1:22',
                         'agentbridge-tunnel'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    deadline = time.monotonic() + 15
    while True:
        try:
            sock = socket.create_connection(('127.0.0.1', port), timeout=1)
            break
        except OSError:
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
    with sock:
        sock.settimeout(5)
        try:
            payload = sock.recv(256)
        except ConnectionResetError:
            payload = b''
        assert not payload, 'Unexpected remote SSH banner: local forwarding remains possible'
    assert select.select([proc.stderr], [], [], 5)[0], 'Missing SSH refusal diagnostic'
    diagnostic = proc.stderr.readline()
    assert b'administratively prohibited' in diagnostic, 'Forwarding did not fail for the expected authorization reason'
    print('local TCP forwarding denied by SSH authorization; no test process retained')
finally:
    proc.terminate()
    proc.communicate(timeout=10)
