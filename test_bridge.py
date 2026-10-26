#!/usr/bin/env python3
"""Real loopback shim -> worker -> synthetic process smoke tests; no model calls."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
import urllib.request
import urllib.error

import shim
import worker


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="agentbridge-smoke-")))
        home = self.root / "home"
        home.mkdir()
        backend = self.root / "backend.sh"
        backend.write_text("#!/bin/sh\nprintf 'bridge-smoke-ok\\n'\n", encoding="utf-8")
        backend.chmod(0o700)
        for module, name, value in (
            (worker, "HOME", str(home)), (worker, "DB_PATH", str(self.root / "jobs.db")),
            (worker, "AUDIT_DIR", str(self.root / "audit")),
            (worker, "DSH_SETTINGS_PATH", str(self.root / "settings.yaml")),
            (worker, "DSH", str(backend)), (worker, "SECRET", "synthetic-worker-key"),
            (worker, "ALLOWED_SENDERS", {"owner"}),
            (shim, "WORKER_SECRET", "synthetic-worker-key"),
            (shim, "TOKEN", "synthetic-shim-key"), (shim, "SENDER_ALLOW", {"owner"}),
            (shim, "ACCEPT_TIMEOUT", 0.4), (shim, "POLL_INTERVAL", 0.02),
            (shim, "WORKER_TIMEOUT", 3),
        ):
            self.stack.enter_context(mock.patch.object(module, name, value))
        self.app = worker.App(verify=False)
        self.stack.callback(self.app.db.close)
        self.stack.enter_context(mock.patch.object(worker, "APP", self.app))
        self.drop = {"next": None}
        drop = self.drop

        class WorkerHandler(worker.Handler):
            def _json(self, code, obj):
                if obj.get("code") == drop["next"] and drop["next"] is not None:
                    drop["next"] = None
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                return super()._json(code, obj)

        upstream = self.start_server(WorkerHandler)
        self.stack.enter_context(mock.patch.object(shim, "WORKER", f"http://127.0.0.1:{upstream.server_port}/execute"))
        frontend = self.start_server(shim.Handler)
        self.url = f"http://127.0.0.1:{frontend.server_port}/v1/chat/completions"
        self.stack.callback(self.wait_idle)

    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.stack.callback(thread.join, 3)
        self.stack.callback(server.server_close)
        self.stack.callback(server.shutdown)
        return server

    def wait_idle(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.app.sched.snapshot()
            if not state['running'] and not state['waiting']:
                return
            time.sleep(0.02)
        self.fail('Synthetic worker did not drain before test cleanup')

    def post(self, text, job_id, **headers):
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"model": "agentbridge", "messages": [{"role": "user", "content": text}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer synthetic-shim-key",
                     "X-AgentBridge-Sender": "owner", "X-AgentBridge-Channel": "smoke-channel",
                     "X-AgentBridge-Job-Id": job_id, **headers},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read())

    def control(self, payload, **headers):
        request = urllib.request.Request(self.url.replace('/chat/completions', '/agentbridge/control'),
            data=json.dumps(payload).encode(), headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer synthetic-shim-key',
                'X-AgentBridge-Sender': 'owner', 'X-AgentBridge-Channel': 'smoke-channel', **headers})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_control_status_unknown_whitelist_identity_and_no_model(self):
        self.post('synthetic task', 'control-job')
        count = len(self.app.db.query('SELECT * FROM jobs'))
        status, body = self.control({'op': 'status', 'job_id': 'control-job'})
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'done')
        for payload, headers, expected in (
            ({'op': 'status'}, {'X-AgentBridge-Sender': ''}, 403),
            ({'op': 'status'}, {'X-AgentBridge-Channel': ''}, 403),
            ({'op': 'status'}, {'X-AgentBridge-Sender': 'intruder'}, 403),
            ({'op': 'status', 'job_id': 'control-job'}, {'X-AgentBridge-Channel': 'other'}, 403),
            ({'op': 'status', 'url': 'http://example.invalid'}, {}, 400),
            ({'op': 'execute'}, {}, 400),
            ({'op': 'status', 'sender': 'owner'}, {}, 400),
            ({'op': 'status', 'job_id': 'not-yet-accepted'}, {}, 404),
        ):
            self.assertEqual(self.control(payload, **headers)[0], expected)
        self.assertEqual(len(self.app.db.query('SELECT * FROM jobs')), count)

    def test_control_force_response_loss_replay_after_second_interruption(self):
        self.post('synthetic task', 'force-job')
        self.app.db.execute("UPDATE jobs SET status='uncertain' WHERE job_id='force-job'")
        self.drop['next'] = 'ACCEPTED'
        payload = {'op': 'force', 'job_id': 'force-job', 'delivery_id': 'force-delivery'}
        status, body = self.control(payload)
        self.assertEqual(body['code'], 'ACCEPT_UNKNOWN')
        self.assertEqual(body['job_id'], 'force-job')
        self.assertEqual(self.control({'op': 'status', 'job_id': body['job_id']})[0], 200)
        self.wait_idle()
        self.app.db.execute("UPDATE jobs SET status='uncertain' WHERE job_id='force-job'")
        status, body = self.control(payload)
        self.assertEqual(body['status'], 'uncertain')
        self.assertTrue(body['reused'])
        self.assertFalse(self.app.sched.snapshot()['running'])

    def test_still_running_then_control_query_and_confirmation(self):
        with mock.patch.object(shim, 'WORKER_TIMEOUT', 0):
            initial = self.post('synthetic task', 'long-job')
        self.assertEqual(initial['x_agentbridge']['code'], 'STILL_RUNNING')
        self.wait_idle()
        self.assertEqual(self.control({'op': 'status', 'job_id': 'long-job'})[1]['status'], 'done')
        pending = self.post('删除合成文件', 'pending-control')
        token = re.search(r'确认 ([0-9a-f]{12})', pending['choices'][0]['message']['content']).group(1)
        confirm = {'op': 'confirm', 'token': token, 'delivery_id': 'confirm-control'}
        self.assertEqual(self.control(confirm, **{'X-AgentBridge-Channel': 'other'})[0], 403)
        self.assertIsNotNone(self.app.db.one('SELECT * FROM pending WHERE job_id=?', ('pending-control',)))
        self.drop['next'] = 'ACCEPTED'
        self.assertEqual(self.control(confirm)[1]['code'], 'ACCEPT_UNKNOWN')
        self.wait_idle()
        self.assertTrue(self.control(confirm)[1]['reused'])
        self.assertEqual(self.control({'op': 'status', 'job_id': 'confirm-control'})[1]['status'], 'done')

    def test_real_process_result_and_idempotent_retry(self):
        first = self.post("synthetic task", "smoke-job")
        second = self.post("synthetic task", "smoke-job")
        self.assertIn("bridge-smoke-ok", first["choices"][0]["message"]["content"])
        self.assertTrue(second["x_agentbridge"]["reused"])
        rows = self.app.db.query("SELECT * FROM jobs")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["exec_identity"])
        self.assertEqual(rows[0]["status"], "done")

    def test_lost_pending_reissue_and_lost_confirmation_reconcile(self):
        self.drop["next"] = "NEED_CONFIRM"
        pending = self.post("删除合成文件", "smoke-sensitive")
        self.assertEqual(pending["x_agentbridge"]["code"], "NEED_CONFIRM")
        reissued = self.post("删除合成文件", "smoke-sensitive")
        text = reissued["choices"][0]["message"]["content"]
        token = re.search(r"确认 ([0-9a-f]{12})", text).group(1)
        self.drop["next"] = "ACCEPTED"
        done = self.post("确认 " + token, "smoke-confirm-message", **{"X-AgentBridge-Confirm": token})
        if done['x_agentbridge']['status'] != 'done':
            # A short wait budget may legitimately expire under load. Reconcile, never re-execute.
            self.assertEqual(done['x_agentbridge']['code'], 'STILL_RUNNING')
            self.wait_idle()
            status, reconciled = self.control({'op': 'status', 'job_id': done['x_agentbridge']['job_id']})
            self.assertEqual(status, 200)
            self.assertEqual(reconciled['status'], 'done')
            done['x_agentbridge']['status'] = reconciled['status']
        self.assertEqual(done["x_agentbridge"]["status"], "done")
        self.assertEqual(done["x_agentbridge"]["job_id"], "smoke-sensitive")
        retry = self.post("确认 " + token, "smoke-confirm-message", **{"X-AgentBridge-Confirm": token})
        self.assertTrue(retry["x_agentbridge"]["reused"])
        self.assertEqual(len(self.app.db.query("SELECT * FROM jobs")), 1)
        self.assertEqual(self.app.get_job("smoke-confirm-message")["status"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
