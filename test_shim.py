#!/usr/bin/env python3
"""shim 端到端验证：用桩 worker 验证 OpenAI 兼容层与缓冲型 SSE。

不触发真实 DSH/agent，只验证协议层。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(HERE, "shim.py")

STUB_PORT = 9101
SHIM_PORT = 9102
TOKEN = "test-exec-token"
SECRET = "test-worker-secret"

STUB_REPLY = {
    "ok": True, "job_id": "abcdef1234567890", "project": "projA",
    "backend": "dsh", "status": "done", "message": "磁盘还剩 32G",
}
STUB_MODE = {"value": "ok"}  # ok | offline | slow | stream-error | queue-full | async | async-stuck | drop-accept | truncate
STUB_LAST = {"body": None}   # 桩 worker 收到的最后一个 JSON body
STUB_JOBS = set()            # 桩已「受理」的 job_id（drop-accept 对账用）


class StubWorker(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        STUB_LAST["body"] = json.loads(self.rfile.read(n))
        mode = STUB_MODE["value"]
        if mode == "offline":
            # 模拟 Mac 不在线：直接断开连接
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if mode == "slow":
            time.sleep(1.2)
        if mode == "truncate":
            # 已受理但响应只写一半即断开（触发 IncompleteRead）
            STUB_JOBS.add(STUB_LAST["body"].get("job_id") or "")
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"ok":')
            self.wfile.flush()
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if mode == "drop-accept":
            # 模拟「已受理但响应丢失」：记录任务后不发响应直接断开
            STUB_JOBS.add(STUB_LAST["body"].get("job_id") or "")
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if mode == "stream-error":
            payload = {"ok": False, "code": "TIMEOUT",
                       "job_id": "deadbeef", "project": "projA",
                       "backend": "dsh", "status": "failed",
                       "message": "任务超时"}
        elif mode == "queue-full":
            payload = {"ok": False, "code": "QUEUE_FULL",
                       "job_id": None, "project": None,
                       "backend": "dsh", "status": None,
                       "message": "排队已满（8）"}
        elif mode in ("async", "async-stuck"):
            # 模拟 wait=false 协作：接纳即返回，结果走 GET /job
            payload = {"ok": True, "code": "ACCEPTED", "job_id": "asyncjob12345",
                       "project": "projA", "backend": "dsh", "status": "queued",
                       "message": "任务已接纳，正在排队执行。"}
        else:
            payload = STUB_REPLY
        self._send(payload)

    def do_GET(self):  # noqa: N802
        # 只实现 /job?id=...（shim 的轮询端点）。
        # 只有真正「受理」过任务的模式才应答；查询不存在的任务必须 404——
        # 否则 shim 的接纳对账会被误命中。
        job_id = self.path.split("id=", 1)[-1] if "id=" in self.path else ""
        mode = STUB_MODE["value"]
        known = (mode in ("async", "async-stuck")
                 or (mode in ("drop-accept", "truncate") and job_id in STUB_JOBS))
        if "/job" not in self.path or not known:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "async-stuck":
            self._send({"ok": False, "code": "BUSY", "job_id": job_id,
                        "project": "projA", "backend": "dsh",
                        "status": "running", "message": "仍在执行"})
        else:
            self._send({**STUB_REPLY, "job_id": job_id})

    def log_message(self, *a):
        pass


def start_shim() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "SHIM_TOKEN": TOKEN,
        "AGENTBRIDGE_SECRET": SECRET,
        "AGENTBRIDGE_WORKER": f"http://127.0.0.1:{STUB_PORT}/execute",
        "SHIM_PORT": str(SHIM_PORT),
        "SHIM_KEEPALIVE_SEC": "1",
        # 接纳快超时、短轮询；等待预算足够覆盖 slow(1.2s) 用例
        "SHIM_ACCEPT_TIMEOUT": "2",
        "SHIM_POLL_INTERVAL": "0.1",
        "SHIM_WORKER_TIMEOUT": "3",
        "SHIM_STREAM_TIMEOUT": "6",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen([sys.executable, SHIM], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(50):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{SHIM_PORT}/health",
                                       headers={"Authorization": f"Bearer {TOKEN}"}),
                timeout=0.5,
            ) as resp:
                resp.read()
            return proc
        except Exception:
            if proc.poll() is not None:
                err = proc.stderr.read().decode()
                raise RuntimeError(f"shim 启动失败: {err}")
            time.sleep(0.1)
    raise RuntimeError("shim 未在预期时间内就绪")


class TestShim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), StubWorker)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        cls.shim = start_shim()

    @classmethod
    def tearDownClass(cls):
        cls.shim.terminate()
        try:
            cls.shim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.shim.kill()
        for pipe in (cls.shim.stdout, cls.shim.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        STUB_MODE["value"] = "ok"
        STUB_LAST["body"] = None
        STUB_JOBS.clear()

    def _post(self, body: dict, token: str = TOKEN, headers: dict | None = None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{SHIM_PORT}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}", **(headers or {})},
        )
        return urllib.request.urlopen(req, timeout=30)

    # ---- 认证 ----

    def test_missing_token_rejected(self):
        try:
            self._post({"messages": [{"role": "user", "content": "hi"}]}, token="wrong")
            self.fail("应返回 401")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
            body = json.loads(exc.read().decode())
            exc.close()
            self.assertIn("error", body)

    # ---- 模型列表 ----

    def test_models_lists_unsupported_responses(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{SHIM_PORT}/v1/models",
            headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            self.assertIsNotNone(resp.headers.get("X-Request-Id"))
        self.assertEqual(body["object"], "list")
        self.assertIn("/v1/responses", body["x_agentbridge"]["unsupported"])

    def test_v1_responses_returns_404(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{SHIM_PORT}/v1/responses",
            data=b"{}",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {TOKEN}"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应返回 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)
            body = json.loads(exc.read().decode())
            exc.close()
            self.assertIn("unsupported_endpoint", json.dumps(body))

    # ---- 元数据协议（F1 / R7 / R8）----

    def test_metadata_forwarded_in_json_body(self):
        """AgentBridge 专用头必须转成 JSON body 字段——worker 只读 JSON。"""
        with self._post(
            {"model": "agentbridge", "messages": [{"role": "user", "content": "看下磁盘"}]},
            headers={
                "X-AgentBridge-Job-Id": "stub-job-0001",
                "X-AgentBridge-Sender": "owner",
                "X-AgentBridge-Channel": "wx:bot:conversation-a",
                "X-AgentBridge-Project": "projA",
                "X-AgentBridge-Confirm": "abc12345",
                "X-AgentBridge-Id-Stable": "1",
                "X-AgentBridge-Force": "1",
            },
        ) as resp:
            resp.read()
        body = STUB_LAST["body"]
        self.assertIsNotNone(body, "桩 worker 未收到请求")
        self.assertEqual(body.get("job_id"), "stub-job-0001")
        self.assertEqual(body.get("channel"), "wx:bot:conversation-a",
                         "sender 不得覆盖会话路由键（R7）")
        self.assertEqual(body.get("sender"), "owner")
        self.assertEqual(body.get("project"), "projA")
        self.assertEqual(body.get("confirm"), "abc12345")
        self.assertEqual(body.get("id_stable"), "1")
        self.assertEqual(body.get("force"), "1", "force 必须经 shim 转发（R8）")
        self.assertIs(body.get("wait"), False, "shim 应以 wait=false 接纳（R3）")
        self.assertIn("task", body)

    def test_sender_used_as_channel_only_when_channel_absent(self):
        """未显式提供会话标识时，sender 充当会话路由键（向后兼容）。"""
        with self._post(
            {"model": "agentbridge", "messages": [{"role": "user", "content": "看下磁盘"}]},
            headers={"X-AgentBridge-Sender": "owner"},
        ) as resp:
            resp.read()
        body = STUB_LAST["body"]
        self.assertEqual(body.get("channel"), "owner")
        self.assertEqual(body.get("sender"), "owner")

    def test_allowlist_requires_sender_identity(self):
        """白名单模式下缺失发送者身份直接拒绝（R7/F12）。"""
        port = 9103
        env = dict(os.environ)
        env.update({
            "SHIM_TOKEN": TOKEN, "AGENTBRIDGE_SECRET": SECRET,
            "AGENTBRIDGE_WORKER": f"http://127.0.0.1:{STUB_PORT}/execute",
            "SHIM_PORT": str(port), "AGENTBRIDGE_ALLOWED_SENDERS": "owner",
            "PYTHONUNBUFFERED": "1",
        })
        proc = subprocess.Popen([sys.executable, SHIM], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                               headers={"Authorization": f"Bearer {TOKEN}"}),
                        timeout=0.5,
                    ) as resp:
                        resp.read()
                    break
                except Exception:
                    time.sleep(0.1)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {TOKEN}"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("应返回 403")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 403)
                exc.read()
                exc.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ---- 非流式 ----

    def test_blocking_response_shape(self):
        with self._post({"model": "agentbridge", "messages": [
                {"role": "user", "content": "看下磁盘"}]}) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(body["object"], "chat.completion")
        content = body["choices"][0]["message"]["content"]
        self.assertTrue(content.startswith("[projA · DSH · abcdef12]"),
                        f"缺少统一回复头：{content}")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["x_agentbridge"]["job_id"], "abcdef1234567890")

    def test_queue_full_maps_to_http_429(self):
        """F13：QUEUE_FULL 是执行失败，非流式必须回真实 HTTP 错误。"""
        STUB_MODE["value"] = "queue-full"
        try:
            self._post({"model": "agentbridge",
                        "messages": [{"role": "user", "content": "hi"}]})
            self.fail("应返回 429")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 429)
            body = json.loads(exc.read().decode())
            exc.close()
            self.assertEqual(body["error"]["code"], "QUEUE_FULL")
            self.assertIn("排队已满", body["error"]["message"])
            self.assertFalse(body.get("choices"), "失败不得伪装成 choices 回复")

    # ---- 缓冲型 SSE ----

    def test_stream_uses_chunk_objects(self):
        with self._post({"model": "agentbridge", "stream": True,
                         "messages": [{"role": "user", "content": "看下磁盘"}]}) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            raw = resp.read().decode()
        events = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
        self.assertTrue(events, "没有收到任何 data 事件")
        self.assertEqual(events[-1], "[DONE]")

        objs = [json.loads(e) for e in events[:-1]]
        self.assertEqual(objs[0]["object"], "chat.completion.chunk")
        self.assertEqual(objs[0]["choices"][0]["delta"].get("role"), "assistant")
        contents = [o["choices"][0]["delta"].get("content") for o in objs
                    if o["choices"][0]["delta"].get("content")]
        self.assertTrue(contents, "缺少内容 delta")
        self.assertTrue(contents[0].startswith("[projA · DSH ·"))
        stops = [o for o in objs if o["choices"][0].get("finish_reason") == "stop"]
        self.assertEqual(len(stops), 1, "应恰好有一个 finish_reason=stop")

    def test_stream_sends_keepalive_when_slow(self):
        STUB_MODE["value"] = "slow"
        with self._post({"model": "agentbridge", "stream": True,
                         "messages": [{"role": "user", "content": "慢任务"}]}) as resp:
            raw = resp.read().decode()
        self.assertIn(": keepalive", raw, "长任务期间应发出心跳注释")

    def test_stream_error_event_not_fake_success(self):
        STUB_MODE["value"] = "stream-error"
        with self._post({"model": "agentbridge", "stream": True,
                         "messages": [{"role": "user", "content": "x"}]}) as resp:
            raw = resp.read().decode()
        events = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
        objs = [json.loads(e) for e in events[:-1]]
        self.assertTrue(any("error" in o for o in objs),
                        "失败时必须发 error 事件，不得伪装成正常回答")
        self.assertFalse(any(o.get("choices", [{}])[0].get("finish_reason") == "stop"
                             for o in objs if "error" not in o),
                         "失败时不应出现正常结束标记")

    def test_stream_queue_full_is_error_event(self):
        """F13：SSE 的错误分支也要覆盖 QUEUE_FULL。"""
        STUB_MODE["value"] = "queue-full"
        with self._post({"model": "agentbridge", "stream": True,
                         "messages": [{"role": "user", "content": "x"}]}) as resp:
            raw = resp.read().decode()
        events = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
        objs = [json.loads(e) for e in events[:-1]]
        errors = [o for o in objs if "error" in o]
        self.assertTrue(errors, "QUEUE_FULL 必须以 error 事件返回")
        self.assertEqual(errors[0]["error"]["code"], "QUEUE_FULL")

    # ---- 离线 / 接纳结果未知（T1）----

    def test_unreachable_worker_reports_offline(self):
        """连接根本没建立（无监听端口）→ 真 OFFLINE，503 + error 对象。"""
        port = 9104
        env = dict(os.environ)
        env.update({
            "SHIM_TOKEN": TOKEN, "AGENTBRIDGE_SECRET": SECRET,
            "AGENTBRIDGE_WORKER": f"http://127.0.0.1:{port}/execute",
            "SHIM_PORT": "9105",
            "SHIM_ACCEPT_TIMEOUT": "1", "SHIM_POLL_INTERVAL": "0.1",
            "PYTHONUNBUFFERED": "1",
        })
        proc = subprocess.Popen([sys.executable, SHIM], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request("http://127.0.0.1:9105/health",
                                               headers={"Authorization": f"Bearer {TOKEN}"}),
                        timeout=0.5,
                    ) as resp:
                        resp.read()
                    break
                except Exception:
                    time.sleep(0.1)
            req = urllib.request.Request(
                "http://127.0.0.1:9105/v1/chat/completions",
                data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {TOKEN}"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("应返回 503")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 503)
                body = json.loads(exc.read().decode())
                exc.close()
                self.assertEqual(body["error"]["code"], "OFFLINE")
                self.assertIn("没开机", body["error"]["message"])
                self.assertNotIn("choices", body, "失败不得伪装成 assistant 回复")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_accept_drop_reports_accept_unknown_with_job_id(self):
        """POST 已发出后连接被断（读不到响应）→ 接纳结果未知：
        返回 200 交互态 ACCEPT_UNKNOWN + 完整 job_id，绝不报 OFFLINE。"""
        STUB_MODE["value"] = "offline"
        with self._post({"model": "agentbridge", "messages": [
                {"role": "user", "content": "hi"}]}) as resp:
            self.assertEqual(resp.status, 200, "未知接纳是交互态，不是失败")
            body = json.loads(resp.read().decode())
        self.assertEqual(body["x_agentbridge"]["code"], "ACCEPT_UNKNOWN")
        job_id = body["x_agentbridge"].get("job_id") or ""
        self.assertTrue(job_id, "必须携带发送前确定的 job_id")
        content = body["choices"][0]["message"]["content"]
        self.assertIn(job_id, content, "正文应包含可对账的完整 job_id")
        self.assertIn("请勿直接重发", content)
        self.assertNotIn("没开机", content, "接纳结果未知不得误报为离线")

    def test_accept_drop_reconciles_via_job_query(self):
        """响应丢失但任务已受理：对账轮询应恢复结果，不丢任务。"""
        STUB_MODE["value"] = "drop-accept"
        with self._post({"model": "agentbridge", "messages": [
                {"role": "user", "content": "看下磁盘"}]}) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
        # 对账成功 → 拿到最终结果而不是 ACCEPT_UNKNOWN
        content = body["choices"][0]["message"]["content"]
        self.assertIn("磁盘还剩", content, f"应通过对账取回结果：{content}")
        self.assertTrue(STUB_JOBS, "桩应已受理任务")

    def test_truncated_accept_response_reconciles(self):
        """U4：半截响应（IncompleteRead）不得抛异常或断开——
        按接纳结果未知对账，恢复结构化结果与 job_id。"""
        STUB_MODE["value"] = "truncate"
        with self._post({"model": "agentbridge", "messages": [
                {"role": "user", "content": "看下磁盘"}]}) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
        content = body["choices"][0]["message"]["content"]
        self.assertIn("磁盘还剩", content,
                      f"半截响应后应通过对账恢复结果：{content}")
        self.assertTrue(body["x_agentbridge"].get("job_id"),
                        "必须保留 job_id 供对账")

    # ---- 接纳/轮询分离（R3）----

    def test_async_flow_polls_to_final_result(self):
        """wait=false 接纳 + /job 轮询：最终取回完整结果，不依赖长连接。"""
        STUB_MODE["value"] = "async"
        with self._post({"model": "agentbridge", "messages": [
                {"role": "user", "content": "慢任务"}]}) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
        self.assertEqual(body["x_agentbridge"]["job_id"], "asyncjob12345")
        content = body["choices"][0]["message"]["content"]
        self.assertIn("磁盘还剩", content, f"应轮询到最终结果：{content}")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")

    def test_wait_deadline_reports_still_running_not_offline(self):
        """R3：排队/执行超过等待预算时，绝不能误报 OFFLINE——
        任务未被取消，应返回带 job_id 的 STILL_RUNNING 交互态。"""
        STUB_MODE["value"] = "async-stuck"
        with self._post({"model": "agentbridge", "messages": [
                {"role": "user", "content": "很慢的任务"}]}) as resp:
            self.assertEqual(resp.status, 200, "STILL_RUNNING 是交互态，不是失败")
            body = json.loads(resp.read().decode())
        self.assertEqual(body["x_agentbridge"]["code"], "STILL_RUNNING")
        self.assertEqual(body["x_agentbridge"]["job_id"], "asyncjob12345")
        content = body["choices"][0]["message"]["content"]
        self.assertIn("仍在", content)
        self.assertIn("asyncjob12345", content, "超时响应必须保留完整 job_id")
        self.assertNotIn("没开机", content, "排队超时不得误报为离线")


if __name__ == "__main__":
    unittest.main(verbosity=2)
