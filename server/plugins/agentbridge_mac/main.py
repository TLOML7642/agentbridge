"""Deterministic delivery IDs and authenticated control routing."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import aiohttp
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.command import GreedyStr

SHIM_URL = os.environ.get("AGENTBRIDGE_SHIM_URL", "http://127.0.0.1:8787/v1/chat/completions")
CONTROL_URL = SHIM_URL.rsplit("/v1/", 1)[0] + "/v1/agentbridge/control"
TOKEN = os.environ.get("SHIM_TOKEN", "")
TIMEOUT = int(os.environ.get("AGENTBRIDGE_MAC_TIMEOUT", "300"))
DATA_DIR = None
USAGE = ("用法：/mac [@项目] <任务>；new <编号> [@项目] <任务>\n"
         "/mac status [任务ID]；confirm <令牌>；force <任务ID>\n"
         "/mac project add <名称> <绝对目录>；project ls；project rm <名称>；use <名称>")


def _fingerprint(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class Main(star.Star):
    def __init__(self, context: star.Context):
        self.context = context

    def _delivery(self, event, text, project="", new_number=""):
        source = [event.unified_msg_origin, str(event.get_sender_id()),
                  str(getattr(event.message_obj, "self_id", ""))]
        if not source[0] or not source[1]:
            raise ValueError("缺少发送者或会话身份，未下发任务。")
        if new_number:
            return _fingerprint(["new", *source, new_number])[:32], False
        raw = getattr(event.message_obj, "raw_message", None)
        # The adapter's message_obj.message_id can be a random fallback UUID.
        mid = (raw.get("message_id") or raw.get("msg_id")) if isinstance(raw, dict) else None
        if mid:
            return _fingerprint(["delivery", *source, str(mid)])[:32], False
        fingerprint = _fingerprint(["fallback", *source, project, text])
        root = Path(DATA_DIR) if DATA_DIR is not None else Path(star.StarTools.get_data_dir("agentbridge_mac"))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        path = root / "deliveries.db"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("CREATE TABLE IF NOT EXISTS deliveries "
                       "(fingerprint TEXT PRIMARY KEY, job_id TEXT NOT NULL, created_at REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            hit = db.execute("SELECT job_id,created_at FROM deliveries WHERE fingerprint=?", (fingerprint,)).fetchone()
            if hit and 0 <= now - hit[1] < 60:
                return hit[0], True
            jid = uuid.uuid4().hex
            db.execute("DELETE FROM deliveries WHERE created_at <= ?", (now - 60,))
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?)", (fingerprint, jid, now))
            # Commit before dispatch; persistence failure must never send an execution request.
        return jid, False

    def _headers(self, event, job_id=None, confirm=None, project=None):
        headers = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
                   "X-AgentBridge-Sender": str(event.get_sender_id()),
                   "X-AgentBridge-Channel": event.unified_msg_origin}
        if job_id:
            headers.update({"X-AgentBridge-Job-Id": job_id, "X-AgentBridge-Id-Stable": "1"})
        if confirm:
            headers["X-AgentBridge-Confirm"] = confirm
        if project:
            headers["X-AgentBridge-Project"] = project
        return headers

    async def _request(self, event, url, payload, **headers):
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers(event, **headers),
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                try:
                    return resp.status, await resp.json()
                except Exception:
                    return resp.status, {"error": {"message": "网关响应格式异常，请查询原任务。"}}

    async def _call(self, event, text, job_id=None, project=None):
        return await self._request(event, SHIM_URL, {"model": "bridge",
            "messages": [{"role": "user", "content": text}]}, job_id=job_id, project=project)

    async def _control(self, event, payload):
        return await self._request(event, CONTROL_URL, payload)

    @staticmethod
    def _render(status, data):
        if data.get("choices"):
            return str(data["choices"][0]["message"]["content"])
        if data.get("error"):
            return "调用本机执行器失败：" + str(data["error"].get("message", status))
        if data.get("message"):
            return str(data["message"]) + (f"\n任务ID：{data['job_id']} 状态：{data.get('status', 'unknown')}" if data.get("job_id") else "")
        if data.get("job_id"):
            return f"任务ID：{data['job_id']} 状态：{data.get('status', 'unknown')}"
        if data.get("recent") is not None:
            rows = [f"{r['job_id']} {r['status']} @{r['project']}" for r in data["recent"]]
            return "当前项目：" + str(data.get("current")) + "\n" + ("\n".join(rows) or "暂无任务")
        if "projects" in data:
            return "项目：" + ", ".join(p["name"] for p in data["projects"])
        return "当前项目：" + str(data.get("current", data.get("project", "操作完成")))

    async def _confirm(self, event, token):
        jid, reused = self._delivery(event, "confirm " + token, "control")
        status, data = await self._control(event, {"op": "confirm", "token": token, "delivery_id": jid})
        return ("60 秒内相同指令，正在复用任务。\n" if reused else "") + self._render(status, data)

    @filter.regex(r"^\s*确认\s+[0-9a-zA-Z]{6,}\s*$")
    async def confirm_by_text(self, event: AstrMessageEvent):
        try:
            if hasattr(event, "is_private_chat") and not event.is_private_chat():
                raise ValueError("仅支持私聊。")
            yield event.plain_result(await self._confirm(event, event.get_message_str().split()[1]))
        except Exception:
            yield event.plain_result("确认结果未知，令牌未在本地清除；请重试同一确认或查询原任务。")

    @filter.command("mac")
    async def mac(self, event: AstrMessageEvent, task: GreedyStr):
        text = str(task or "").strip()
        if not text:
            yield event.plain_result(USAGE)
            return
        jid = ""
        try:
            if hasattr(event, "is_private_chat") and not event.is_private_chat():
                raise ValueError("仅支持私聊。")
            parts = text.split()
            command = parts[0]
            payload = None
            if command == "confirm":
                if len(parts) != 2:
                    raise ValueError("格式：/mac confirm <令牌>")
                yield event.plain_result(await self._confirm(event, parts[1]))
                return
            if command == "status":
                if len(parts) > 2:
                    raise ValueError("格式：/mac status [任务ID]")
                payload = {"op": "status", "job_id": parts[1] if len(parts) == 2 else ""}
            elif command in ("force", "use"):
                if len(parts) != 2:
                    raise ValueError("格式：/mac " + command + " <任务ID或项目名称>")
                payload = {"op": command, "job_id" if command == "force" else "project": parts[1]}
            elif command == "project":
                if parts[1:] == ["ls"]:
                    payload = {"op": "project.list"}
                elif len(parts) == 3 and parts[1] == "rm":
                    payload = {"op": "project.rm", "name": parts[2]}
                elif len(parts) >= 4 and parts[1] == "add":
                    payload = {"op": "project.add", "name": parts[2], "dir": text.split(None, 3)[3]}
                else:
                    raise ValueError(USAGE)
            if payload is not None:
                if payload["op"] not in ("status", "project.list"):
                    jid, _ = self._delivery(event, text, "control")
                    payload["delivery_id"] = jid
                status, data = await self._control(event, payload)
                yield event.plain_result(self._render(status, data))
                return
            number = ""
            if command == "new":
                if len(parts) < 3:
                    raise ValueError("格式：/mac new <编号> [@项目] <任务>")
                number, text = parts[1], text.split(None, 2)[2]
            project = ""
            if text.startswith("@"):
                route = text.split(None, 1)
                if len(route) != 2 or not route[0][1:]:
                    raise ValueError("格式：/mac @项目 <任务>")
                project, text = route[0][1:], route[1]
            if not project:
                status, data = await self._control(event, {"op": "status"})
                if status != 200 or not data.get("ok") or not data.get("current"):
                    yield event.plain_result(self._render(status, data))
                    return
                project = data["current"]
            jid, reused = self._delivery(event, text, project, number)
            status, data = await self._call(event, text, job_id=jid, project=project)
            yield event.plain_result(("60 秒内相同指令，正在复用任务。\n" if reused else "") + self._render(status, data))
        except ValueError as exc:
            yield event.plain_result(str(exc))
        except Exception:
            yield event.plain_result("未能确认投递结果。" + (f"请用 /mac status {jid} 查询，勿盲目新建。" if jid else "未下发执行；请稍后重试。"))
