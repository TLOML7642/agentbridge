#!/usr/bin/env python3
"""AgentBridge 统一 API 与路由层（shim）。

职责：对客户端暴露 OpenAI 兼容接口，把「本机执行」路由到 Mac worker。

设计约束：
  * 仅监听 127.0.0.1；回环仍校验 Bearer token
  * 支持 models/chat 与固定白名单 control；/v1/responses 明确返回 404
  * 与 worker 的内部协议是 JSON body：AgentBridge 专用头（job/channel/project/
    confirm/force/id_stable）由本层取出、经请求体转发；worker 不读转发头
  * 白名单（AGENTBRIDGE_ALLOWED_SENDERS）验证的是发送者身份（X-AgentBridge-Sender）；
    X-AgentBridge-Channel 是会话路由键，不被身份覆盖——同一发送者的不同会话
    各自维护粘性项目。白名单配置后缺失身份直接拒绝
  * 接纳与等待分离：任务以 wait=false 交给 worker（立即返回 ACCEPTED），
    本层轮询 /job 取结果。job_id 在发送前确定并随响应暴露。排队/执行
    超时不算「离线」（STILL_RUNNING 交互态）；POST 已发出后的读超时/
    断连属于「接纳结果未知」（ACCEPT_UNKNOWN，带 job_id 可对账），
    只有连接未建立才报 OFFLINE——避免误导重发造成重复执行
  * stream=true 走「缓冲型 SSE」；失败返回 error 事件，不伪装成功
  * 非流式：执行/认证/离线等失败返回对应 HTTP 错误与标准 error 对象，
    不再包装成 200 + finish_reason=stop 的成功回复；
    NEED_CONFIRM/BUSY/UNCERTAIN/STILL_RUNNING 属于交互态，仍以 200 正文返回
  * 不记录任何请求正文
  * 不新增公网入口
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("SHIM_TOKEN", "")          # 客户端用的 Bearer
WORKER_SECRET = os.environ.get("AGENTBRIDGE_SECRET", "")  # 与 Mac worker 共享
WORKER = os.environ.get("AGENTBRIDGE_WORKER", "http://127.0.0.1:9000/execute")
SENDER_ALLOW = {
    s.strip() for s in os.environ.get("AGENTBRIDGE_ALLOWED_SENDERS", "").split(",") if s.strip()
}

PORT = int(os.environ.get("SHIM_PORT", "8787"))
# 非流式等待任务结果的总预算（接纳 + 轮询）。超时返回 STILL_RUNNING，
# 不是 OFFLINE——任务仍在 worker 侧排队/执行，没有被取消。
WORKER_TIMEOUT = int(os.environ.get("SHIM_WORKER_TIMEOUT", "130"))
# SSE 有客户端可见的心跳，等待预算可以更长
STREAM_WAIT_SEC = int(os.environ.get("SHIM_STREAM_TIMEOUT", "600"))
ACCEPT_TIMEOUT = int(os.environ.get("SHIM_ACCEPT_TIMEOUT", "10"))
POLL_INTERVAL = float(os.environ.get("SHIM_POLL_INTERVAL", "1"))
KEEPALIVE_SEC = int(os.environ.get("SHIM_KEEPALIVE_SEC", "10"))
BODY_MAX = int(os.environ.get("SHIM_BODY_MAX", str(64 * 1024)))
HTTP_TIMEOUT = int(os.environ.get("SHIM_HTTP_TIMEOUT", "60"))

MODEL_ID = "agentbridge"

# worker 返回这些业务码时视为「执行层失败」：非流式必须回 HTTP 错误，
# SSE 必须发 error 事件。NEED_CONFIRM/BUSY/UNCERTAIN/STILL_RUNNING/ACCEPTED
# 是交互态，不算失败。
FAILURE_CODES = {"OFFLINE", "AUTH_FAIL", "TIMEOUT", "EXEC_FAIL", "QUEUE_FULL"}
FAILURE_STATUS = {
    "OFFLINE": 503, "AUTH_FAIL": 502, "TIMEOUT": 504,
    "QUEUE_FULL": 429, "EXEC_FAIL": 500,
}

PREAMBLE = (
    "你在即时通讯（微信）的私聊里回答用户。规则：\n"
    "1. 中文口语化，尽量简短（一般不超过 200 字）\n"
    "2. 不要输出本机绝对路径、完整命令行、内部推理过程\n"
    "3. 只给结论与必要步骤；确实执行过动作时直接给结果\n"
)


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_request_error"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def flatten(messages) -> str:
    lines = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if not isinstance(content, str) or not content.strip():
            continue
        tag = {"user": "用户", "assistant": "助手", "system": "系统"}.get(role, role)
        lines.append(f"{tag}：{content.strip()}")
    return "\n".join(lines)


def _worker_base() -> str:
    """从 /execute URL 推导 worker 根地址（轮询 /job 用）。"""
    if WORKER.endswith("/execute"):
        return WORKER[: -len("/execute")]
    return WORKER.rstrip("/")


CONTROL_FIELDS = {
    "status": {"job_id"}, "confirm": {"token", "delivery_id"},
    "status.batch": {"job_ids"}, "diag": set(), "jobs": set(), "pending": set(),
    "deny": {"job_id", "delivery_id"}, "cancel": {"job_id", "delivery_id"},
    "force": {"job_id", "delivery_id"}, "project.add": {"name", "dir", "delivery_id"},
    "project.rm": {"name", "delivery_id"}, "project.list": set(),
    "use": {"project", "delivery_id"},
}
QUERY_OPS = frozenset(("status", "project.list", "diag", "jobs", "pending", "status.batch"))
_HEALTH_LOCK = threading.Lock()
_LAST_WORKER_HEALTH = None


def control_worker(payload: dict, meta: dict) -> dict:
    global _LAST_WORKER_HEALTH
    op = payload.get("op")
    if not isinstance(op, str) or op not in CONTROL_FIELDS:
        raise ApiError(400, "不支持的控制操作", "invalid_control")
    if set(payload) - CONTROL_FIELDS[op] - {"op"}:
        raise ApiError(400, "控制请求含非白名单字段", "invalid_control")
    if any(not isinstance(value, str) for key, value in payload.items() if key != "job_ids"):
        raise ApiError(400, "控制字段必须是字符串", "invalid_control")
    if op == "status.batch":
        ids = payload.get("job_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or any(not isinstance(i, str) or not i for i in ids):
            raise ApiError(400, "批量查询须提供 1 至 50 个任务 ID", "invalid_control")
    body = {**payload, "sender": meta["sender"], "channel": meta["channel"]}
    req = urllib.request.Request(
        _worker_base() + "/control", data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "X-AgentBridge-Secret": WORKER_SECRET})
    query = op in QUERY_OPS
    deadline = time.monotonic() + 10
    for attempt in range(3 if query else 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with urllib.request.urlopen(req, timeout=min(ACCEPT_TIMEOUT, remaining, 3) if query else ACCEPT_TIMEOUT) as resp:
                result = json.loads(resp.read())
            with _HEALTH_LOCK:
                _LAST_WORKER_HEALTH = time.time()
            if op == "diag":
                result["last_worker_healthy_at"] = _LAST_WORKER_HEALTH
            return result
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
                return result if isinstance(result, dict) else {"ok": False, "code": "EXEC_FAIL"}
            finally:
                exc.close()
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            if query and attempt < 2:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    if query:
        return {"ok": False, "code": "QUERY_UNAVAILABLE",
                "last_worker_healthy_at": _LAST_WORKER_HEALTH,
                "message": "隧道或 worker 不可达，查询未创建任务。请稍后查询原 ID。"}
    return {"ok": False, "code": "ACCEPT_UNKNOWN", "job_id": payload.get("job_id") or payload.get("delivery_id"),
            "message": "修改结果未知；不会自动重试，请先查询原任务，不要新建授权。"}


def _accept_worker(task: str, meta: dict) -> dict:
    """把任务交给 worker（wait=false）：接纳即返回，不等执行完成。

    job_id 在发送前确定（沿用调用方提供的，否则本地生成）——POST
    已发出后的读超时/断连属于「接纳结果未知」，不是「未接纳」：
    worker 可能已受理。此时带上 job_id 返回 ACCEPT_UNKNOWN 并做
    短暂对账（查询 /job），绝不误报 OFFLINE 诱导盲目重发造成重复执行。
    只有连接根本没建立（拒绝/连接超时）才返回普通 OFFLINE。
    """
    if not meta.get("job_id"):
        meta = dict(meta)
        meta["job_id"] = uuid.uuid4().hex[:32]
    job_id = meta["job_id"]
    body = json.dumps(
        {"task": task, "wait": False,
         **{k: v for k, v in meta.items() if v not in (None, "")}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        WORKER, data=body,
        headers={"Content-Type": "application/json", "X-AgentBridge-Secret": WORKER_SECRET},
    )
    try:
        with urllib.request.urlopen(req, timeout=ACCEPT_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "code": "AUTH_FAIL",
                    "message": "网关与执行节点之间的密钥不匹配（AUTH_FAIL）。"}
        return {"ok": False, "code": "EXEC_FAIL", "job_id": job_id,
                "message": f"执行节点返回 HTTP {exc.code}（EXEC_FAIL）。"}
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (ConnectionRefusedError, socket.timeout, TimeoutError)):
            # 连接未建立：请求没有到达 worker，可以确定未被接纳
            return {"ok": False, "code": "OFFLINE", "job_id": job_id,
                    "message": "隧道或 worker 不可达，连接未建立，任务未被接纳。"}
        return _accept_unknown(job_id)
    except (socket.timeout, TimeoutError):
        # 请求已发出、读响应超时：接纳结果未知
        return _accept_unknown(job_id)
    except (http.client.HTTPException, ConnectionResetError, BrokenPipeError, OSError):
        # 响应途中断开（含 IncompleteRead 半截正文）、协议/传输异常：
        # worker 可能已受理并继续执行，一律按接纳结果未知对账
        return _accept_unknown(job_id)
    except json.JSONDecodeError:
        # 200 但正文非法：不能据此断定未接纳
        return _accept_unknown(job_id)


def _accept_unknown(job_id: str) -> dict:
    """接纳结果未知：短暂对账，能查到就当作已接纳继续等待。"""
    deadline = time.monotonic() + min(5.0, ACCEPT_TIMEOUT * 2)
    while time.monotonic() < deadline:
        time.sleep(min(0.2, POLL_INTERVAL))
        status = _poll_worker(job_id)
        if status is not None:
            status = dict(status)
            status.setdefault("job_id", job_id)
            return status
    return {
        "ok": False, "code": "ACCEPT_UNKNOWN", "job_id": job_id, "status": "unknown",
        "message": (f"已把任务发给执行器，但没能确认受理结果（job_id={job_id}）。"
                    "任务可能已在执行，请勿直接重发；稍后带此 job_id 查询结果。"),
    }


def _poll_worker(job_id: str) -> dict | None:
    """查询任务状态；网络/传输/解析失败返回 None（调用方稍后重试）。"""
    req = urllib.request.Request(
        f"{_worker_base()}/job?id={urllib.parse.quote(job_id)}",
        headers={"X-AgentBridge-Secret": WORKER_SECRET},
    )
    try:
        with urllib.request.urlopen(req, timeout=ACCEPT_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404=任务不存在（尚未受理/无此别名）；其余状态码也按本轮失败重试
        try:
            exc.read()
        except Exception:  # noqa: BLE001
            pass
        exc.close()
        return None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
            OSError, json.JSONDecodeError):
        return None


_TERMINAL = ("done", "failed", "cancelled", "uncertain")


def call_worker(task: str, meta: dict, wait_sec: int | None = None) -> dict:
    """交给 worker 并等待结果（接纳 → 轮询 → 终态/超时）。

    超时返回 STILL_RUNNING（带 job_id）：任务没有取消，用户重发相同
    job_id 可取回结果，而不是收到「Mac 离线」后盲目重试造成重复执行。
    """
    deadline = time.monotonic() + (wait_sec if wait_sec is not None else WORKER_TIMEOUT)
    result = _accept_worker(task, meta)
    job_id = str(result.get("job_id") or "")
    accepted = (result.get("code") == "ACCEPTED"
                or result.get("status") in ("queued", "running"))
    if not accepted or not job_id:
        return result
    polled = result.get("status") in ("queued", "running")
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        status = _poll_worker(job_id)
        if status is not None:
            polled = True
            if status.get("status") in _TERMINAL:
                return status
    suffix = "" if polled else "（当前状态暂时无法确认，执行器无响应）"
    return {
        "ok": False, "code": "STILL_RUNNING", "job_id": job_id, "status": "running",
        "message": (f"任务仍在排队/执行（job_id={job_id}），没有被取消。"
                    f"稍后带原 job_id 重发即可取回结果。{suffix}"),
    }


def compose_content(payload: dict) -> str:
    """拼装 [项目 · 后端 · 短ID] 前缀 + 正文。"""
    project = payload.get("project") or "?"
    backend = (payload.get("backend") or "dsh").upper()
    job_id = str(payload.get("job_id") or "")
    short = job_id[:8] if job_id else "????????"
    body = payload.get("message") or ""
    return f"[{project} · {backend} · {short}] {body}".strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "agentbridge-shim"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:  # noqa: D102
        super().setup()
        try:
            self.connection.settimeout(HTTP_TIMEOUT)
        except OSError:
            pass

    # ---- 基础工具 ----

    def _json(self, status: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-Id", self._rid)
        self.end_headers()
        self.wfile.write(raw)

    def _oai_error(self, status: int, message: str, code: str,
                   x_agentbridge: dict | None = None) -> None:
        err: dict = {"message": message,
                     "type": "server_error" if status >= 500 else "invalid_request_error",
                     "param": None, "code": code}
        if x_agentbridge is not None:
            err["x_agentbridge"] = x_agentbridge
        self._json(status, {"error": err})

    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        ok = bool(TOKEN) and header == f"Bearer {TOKEN}"
        if not ok:
            self.close_connection = True
        return ok

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 非法") from None
        if n <= 0 or n > BODY_MAX:
            raise ApiError(413, "请求体为空或过大")
        try:
            data = json.loads(self.rfile.read(n))
        except Exception:
            raise ApiError(400, "请求体不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _worker_meta(self) -> dict:
        """从 AgentBridge 专用头提取元数据，交给 worker 的 JSON 协议。

        身份（X-AgentBridge-Sender，用于白名单鉴权）与会话（X-AgentBridge-Channel，
        用于粘性项目路由）是两个概念：sender 不得覆盖 channel，
        否则同一发送者的多个会话会共享同一条粘性绑定。
        白名单配置后，缺失发送者身份直接拒绝——缺字段不是「无限制」。
        """
        meta: dict = {}
        mapping = {
            "X-AgentBridge-Job-Id": "job_id",
            "X-AgentBridge-Channel": "channel",
            "X-AgentBridge-Project": "project",
            "X-AgentBridge-Confirm": "confirm",
            "X-AgentBridge-Id-Stable": "id_stable",
            "X-AgentBridge-Force": "force",
            "X-AgentBridge-Delivery-Id": "control_delivery_id",
        }
        for header, field in mapping.items():
            value = self.headers.get(header)
            if value is not None and value.strip():
                meta[field] = value.strip()
        sender = self.headers.get("X-AgentBridge-Sender") or ""
        if SENDER_ALLOW:
            principal = sender or meta.get("channel") or ""
            if not principal:
                raise ApiError(
                    403,
                    "已配置发送者白名单，但请求缺少发送者身份"
                    "（X-AgentBridge-Sender 或 X-AgentBridge-Channel），拒绝转发",
                    "permission_error",
                )
            if principal not in SENDER_ALLOW:
                raise ApiError(403, "发送者不在白名单内（AUTH_FAIL）", "permission_error")
        if sender:
            meta["sender"] = sender
        # 无显式会话标识时，以发送者充当会话路由键（向后兼容）
        if not meta.get("channel") and sender:
            meta["channel"] = sender
        return meta

    def _xbot(self, result: dict) -> dict:
        return {
            "request_id": self._rid,
            "job_id": result.get("job_id"),
            "project": result.get("project"),
            "backend": result.get("backend"),
            "status": result.get("status"),
            "code": result.get("code"),
            "reused": result.get("reused", False),
            "attempt_id": result.get("attempt_id"),
            "phase": result.get("phase"),
            "elapsed_seconds": result.get("elapsed_seconds"),
        }

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        self._rid = self.headers.get("X-Request-Id") or f"req-{uuid.uuid4().hex[:12]}"
        if not self._authed():
            return self._oai_error(401, "缺少或错误的 Bearer token", "auth_failed")
        if self.path.rstrip("/") == "/v1/models":
            return self._json(200, {
                "object": "list",
                "data": [{"id": MODEL_ID, "object": "model",
                          "created": int(time.time()), "owned_by": "local"}],
                "x_agentbridge": {
                    "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/agentbridge/control"],
                    "unsupported": ["/v1/responses"],
                    "streaming": "buffered",
                    "note": "兼容格式仅为通信协议，不等价于等价的 Agent 能力。",
                },
            })
        if self.path.rstrip("/") == "/v1/responses":
            return self._oai_error(
                404, "/v1/responses 未实现；请使用 /v1/chat/completions", "unsupported_endpoint")
        if self.path.rstrip("/") == "/health":
            return self._json(200, {"ok": True, "model": MODEL_ID})
        return self._oai_error(404, "not found", "unknown_endpoint")

    def do_POST(self) -> None:  # noqa: N802
        self._rid = self.headers.get("X-Request-Id") or f"req-{uuid.uuid4().hex[:12]}"
        if not self._authed():
            return self._oai_error(401, "缺少或错误的 Bearer token", "auth_failed")
        route = self.path.rstrip("/")
        if route == "/v1/responses":
            return self._oai_error(
                404, "/v1/responses 未实现；请使用 /v1/chat/completions", "unsupported_endpoint")
        if route == "/v1/agentbridge/control":
            try:
                payload = self._read_body()
                # Unlike legacy chat clients, control callers must supply both identities.
                if not self.headers.get("X-AgentBridge-Sender", "").strip() or not self.headers.get("X-AgentBridge-Channel", "").strip():
                    raise ApiError(403, "控制操作缺少发送者或会话身份", "permission_error")
                meta = self._worker_meta()
                result = control_worker(payload, meta)
                code = result.get("code")
                status = (403 if code == "AUTH_FAIL" else 404 if code == "NOT_FOUND" else
                          409 if code == "AMBIGUOUS_ID" else 503 if code == "QUERY_UNAVAILABLE" else FAILURE_STATUS.get(code, 200))
                return self._json(status, result)
            except ApiError as exc:
                self.close_connection = True
                return self._oai_error(exc.status, exc.message, exc.code)
            except Exception:
                return self._oai_error(502, "控制节点响应异常，请查询原投递状态", "control_error")
        if route != "/v1/chat/completions":
            self.close_connection = True
            return self._oai_error(404, "not found", "unknown_endpoint")

        try:
            payload = self._read_body()
            meta = self._worker_meta()
        except ApiError as exc:
            self.close_connection = True
            return self._oai_error(exc.status, exc.message, exc.code)

        conversation = flatten(payload.get("messages"))
        if not conversation:
            return self._oai_error(400, "messages 为空", "invalid_messages")
        task = PREAMBLE + "\n对话如下：\n" + conversation

        stream = bool(payload.get("stream"))
        if stream and self.headers.get("Prefer", "").lower() != "respond-async":
            return self._stream(task, meta)
        return self._blocking(task, meta)

    # ---- 非流式 ----

    def _blocking(self, task: str, meta: dict) -> None:
        try:
            result = (_accept_worker(task, meta) if self.headers.get("Prefer", "").lower() == "respond-async"
                      else call_worker(task, meta, wait_sec=WORKER_TIMEOUT))
        except Exception as exc:  # noqa: BLE001
            # 传输层之外的意外异常也要结构化返回，不能裸断客户端连接
            result = {"ok": False, "code": "EXEC_FAIL",
                      "job_id": meta.get("job_id"),
                      "message": f"网关内部错误：{type(exc).__name__}"}
        code = result.get("code")
        if result.get("ok") is False and code in FAILURE_CODES:
            # 执行/认证/离线失败不能伪装成成功回复——标准客户端只看
            # HTTP 状态与 finish_reason，包装成 200 会被当成正常输出
            return self._oai_error(
                FAILURE_STATUS.get(code, 500), compose_content(result), code,
                x_agentbridge=self._xbot(result),
            )
        self._json(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": compose_content(result)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "x_agentbridge": self._xbot(result),
        })

    # ---- 缓冲型 SSE ----

    def _chunk(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.wfile.write(f"{len(raw):X}\r\n".encode() + raw + b"\r\n")
        self.wfile.flush()

    def _event(self, obj: dict) -> None:
        self._chunk("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")

    def _chunk_payload(self, delta: dict, finish: str | None = None) -> dict:
        return {
            "id": self._chunk_id, "object": "chat.completion.chunk",
            "created": self._created, "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _stream(self, task: str, meta: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Request-Id", self._rid)
        self.end_headers()

        self._chunk_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        self._created = int(time.time())

        holder: dict = {}

        def _work() -> None:
            # SSE 有客户端可见的心跳，等待预算比非流式长得多；
            # 超时仍返回 STILL_RUNNING 交互态而不是 error
            try:
                holder["result"] = call_worker(task, meta, wait_sec=STREAM_WAIT_SEC)
            except Exception as exc:  # noqa: BLE001
                holder["result"] = {
                    "ok": False, "code": "EXEC_FAIL", "job_id": meta.get("job_id"),
                    "message": f"网关内部错误：{type(exc).__name__}",
                }

        worker_thread = threading.Thread(target=_work, daemon=True)
        worker_thread.start()

        # 先发 assistant 角色块，确认流已建立
        self._event(self._chunk_payload({"role": "assistant"}))

        # 等待期间发心跳注释（不是模型输出，不保证绕过客户端总超时）
        while worker_thread.is_alive():
            worker_thread.join(KEEPALIVE_SEC)
            if worker_thread.is_alive():
                self._chunk(": keepalive\n\n")

        result = holder.get("result") or {
            "ok": False, "code": "EXEC_FAIL", "message": "执行节点没有返回结果。"}

        code = result.get("code")
        if result.get("ok") is False and code in FAILURE_CODES:
            # 失败：发 error 事件，绝不伪装成正常回答
            self._event({"error": {
                "message": result.get("message", "执行失败"),
                "type": "server_error", "code": code,
            }})
        else:
            self._event(self._chunk_payload({"content": compose_content(result)}))
            self._event(self._chunk_payload({}, finish="stop"))

        self._chunk("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, fmt, *args):  # noqa: D102
        pass  # 不记录任何请求内容


def main() -> None:
    if not TOKEN:
        print("SHIM_TOKEN 未设置，拒绝启动", file=sys.stderr)
        raise SystemExit(2)
    if not WORKER_SECRET:
        print("AGENTBRIDGE_SECRET 未设置，拒绝启动", file=sys.stderr)
        raise SystemExit(2)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"agentbridge-shim 监听 127.0.0.1:{PORT} → {WORKER}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
