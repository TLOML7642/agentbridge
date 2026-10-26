"""Independent loopback sshd. Never uses production keys, users or listeners."""
import os
from pathlib import Path
import pwd
import select
import socket
import subprocess
import tempfile
import time
import unittest


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class TestIsolatedSSHD(unittest.TestCase):
    def setUp(self):
        # Unix domain socket names are limited to 104/108 bytes. The regression
        # runner's nested TMPDIR must not turn an authorization test into EINVAL.
        self.temp = tempfile.TemporaryDirectory(prefix='ab-sshd-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        for name in ('host', 'client'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.root / name)], check=True)
        self.port, self.remote_port = free_port(), free_port()
        user = pwd.getpwuid(os.getuid()).pw_name
        config = self.root / 'sshd_config'
        config.write_text(f'''
Port {self.port}
ListenAddress 127.0.0.1
HostKey {self.root / 'host'}
PidFile {self.root / 'sshd.pid'}
AuthorizedKeysFile {self.root / 'client.pub'}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers {user}
AllowTcpForwarding remote
PermitListen 127.0.0.1:{self.remote_port}
PermitOpen none
AllowStreamLocalForwarding no
PermitTTY no
X11Forwarding no
AllowAgentForwarding no
LogLevel ERROR
''')
        subprocess.run(['/usr/sbin/sshd', '-t', '-f', str(config)], check=True, capture_output=True)
        self.log = open(self.root / 'sshd.log', 'w+')
        self.addCleanup(self.log.close)
        self.sshd = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(config)],
                                     stdout=subprocess.DEVNULL, stderr=self.log)
        self.addCleanup(self.stop, self.sshd)
        self.wait_listener(self.port)
        self.base = ['ssh', '-F', '/dev/null', '-N', '-i', str(self.root / 'client'),
                     '-p', str(self.port), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                     '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                     '-o', 'LogLevel=VERBOSE', '-o', 'ExitOnForwardFailure=yes',
                     '-o', 'ConnectTimeout=3']
        self.target = f'{user}@127.0.0.1'

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(3)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()

    def wait_listener(self, port):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    return
            except OSError:
                if self.sshd.poll() is not None:
                    self.log.flush()
                    self.fail('Independent sshd failed to start; inspect private synthetic log')
                time.sleep(.02)
        self.fail('Independent listener did not become ready')

    def client(self, *args):
        proc = subprocess.Popen([*self.base, *args, self.target], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, proc)
        return proc

    def assert_refusal(self, proc, expected=b'administratively prohibited'):
        deadline, output = time.monotonic() + 3, b''
        while time.monotonic() < deadline:
            if expected in output:
                return
            if select.select([proc.stderr], [], [], .1)[0]:
                chunk = os.read(proc.stderr.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
        self.assertIn(expected, output)

    def test_local_dynamic_and_unix_forwarding_are_denied(self):
        for dynamic in (False, True):
            with self.subTest(dynamic=dynamic):
                port = free_port()
                proc = self.client(*(['-D', f'127.0.0.1:{port}'] if dynamic else
                                     ['-L', f'127.0.0.1:{port}:127.0.0.1:{self.port}']))
                self.wait_listener(port)
                with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
                    sock.settimeout(3)
                    if dynamic:
                        sock.sendall(b'\x05\x01\x00')
                        self.assertEqual(sock.recv(2), b'\x05\x00')
                        sock.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + self.port.to_bytes(2, 'big'))
                    try:
                        response = sock.recv(256)
                    except ConnectionResetError:
                        response = b''
                    if dynamic:
                        self.assertTrue(not response or response[1] != 0)
                    else:
                        self.assertEqual(response, b'')
                self.assert_refusal(proc)
                self.stop(proc)
        remote = self.client('-R', f'{self.root / "remote.sock"}:{self.root / "target.sock"}')
        _, stderr = remote.communicate(timeout=5)
        self.assertNotEqual(remote.returncode, 0)
        self.assertIn(b'forwarding failed', stderr)
        self.assertFalse((self.root / 'remote.sock').exists())
        # An existing target proves refusal is not just a missing socket. OpenSSH
        # reports stream-local policy refusal as "connect failed: open failed".
        with socket.socket(socket.AF_UNIX) as target:
            target.bind(str(self.root / 'target.sock'))
            target.listen()
            target.settimeout(.2)
            local_path = self.root / 'local.sock'
            proc = self.client('-L', f'{local_path}:{self.root / "target.sock"}')
            deadline = time.monotonic() + 3
            while not local_path.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(local_path.exists())
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(3)
                sock.connect(str(local_path))
                self.assertEqual(sock.recv(1), b'')
            self.assert_refusal(proc, b'open failed')
            with self.assertRaises(TimeoutError):
                target.accept()

    def test_disconnect_listener_collision_and_clean_reconnect(self):
        proc = self.client('-R', f'127.0.0.1:{self.remote_port}:127.0.0.1:{self.port}')
        self.wait_listener(self.remote_port)
        with socket.create_connection(('127.0.0.1', self.remote_port), timeout=3) as sock:
            self.assertTrue(sock.recv(256).startswith(b'SSH-'))
        self.stop(proc)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', self.remote_port), timeout=.1):
                    pass
            except OSError:
                break
            time.sleep(.02)
        else:
            self.fail('Owned test reverse listener survived disconnect')
        with socket.socket() as collision:
            collision.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            collision.bind(('127.0.0.1', self.remote_port))
            collision.listen()
            failed = self.client('-R', f'127.0.0.1:{self.remote_port}:127.0.0.1:{self.port}')
            _, stderr = failed.communicate(timeout=5)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(b'forwarding failed', stderr)
        reconnected = self.client('-R', f'127.0.0.1:{self.remote_port}:127.0.0.1:{self.port}')
        self.wait_listener(self.remote_port)
        self.assertIsNone(reconnected.poll())


if __name__ == '__main__':
    unittest.main()
