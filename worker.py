#!/usr/bin/env python3
"""AgentBridge Mac 侧执行器。

职责：接收 shim 经 SSH 反向隧道发来的任务，在 Mac 上调度并执行 DSH。

安全边界（本文件负责的）：
  * 仅监听 127.0.0.1，任何非回环来源都不可达
  * 所有请求需 X-AgentBridge-Secret 共享密钥；内部协议只认 JSON body，
    不读取任何转发头（shim 负责把元数据放进 JSON，见 shim.py）
  * 需提权的任务以只读沙箱执行，只产出计划；权限在「实际执行时」
    按最新上下文重新判定（接纳快照 + 运行时复检，宁严勿松）；
    正文分类只是粗筛，不是硬审批边界
  * 敏感任务必须持有已签发、未过期、绑定来源与目录的确认令牌才能执行；
    未登记的 job_id 携带任何 confirm 一律拒绝；队列满不消耗令牌
  * 任务接纳时固化目录/资源键快照并分配执行序号，同资源严格按
    接纳顺序执行；确认只授权该快照
  * DSH settings 与 permission 默认隔离到桥接自有的配置文件，
    日常 UI 的权限预设不会反向覆盖只读计划任务
  * 超时对整个进程组先 TERM 后无条件 KILL；主动脱离进程组
    （setsid）的后代无法回收，这是已知限制
  * 数据库与审计文件 0600/0700；任务原文与结果按保留期清理

本文件刻意不记录任务正文到 stderr/标准日志。
"""

from __future__ import annotations

import collections
import contextlib
import ctypes
import hashlib
import hmac
import itertools
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

HOME = os.path.expanduser("~")
BASE = os.environ.get("AGENTBRIDGE_BASE", os.path.join(HOME, "agentbridge"))

SECRET = os.environ.get("AGENTBRIDGE_SECRET", "")
DSH = os.environ.get("AGENTBRIDGE_DSH", os.path.join(HOME, ".deepseek-harness/bin/dsh"))
PATCH_FULL = os.environ.get("AGENTBRIDGE_PATCH_FULL", os.path.join(BASE, "fullaccess.patch.yml"))
PATCH_RO = os.environ.get("AGENTBRIDGE_PATCH_RO", os.path.join(BASE, "readonly.patch.yml"))
DB_PATH = os.environ.get("AGENTBRIDGE_DB", os.path.join(BASE, "agentbridge.db"))
AUDIT_DIR = os.environ.get("AGENTBRIDGE_AUDIT_DIR", os.path.join(BASE, "audit"))
# 桥接隔离的 DSH settings：patch 会把 settings 服务的 path 指到这里、
# 并固定 permission.defaultPreset，避免日常 UI 的权限预设反向覆盖只读任务。
# 启动时写入环境变量 AGENTBRIDGE_DSH_SETTINGS 供 !!js 表达式与子进程使用。
DSH_SETTINGS_PATH = os.environ.get(
    "AGENTBRIDGE_DSH_SETTINGS", os.path.join(BASE, "dsh-settings.yaml"))

PORT = int(os.environ.get("AGENTBRIDGE_PORT", "9000"))
CONCURRENCY = int(os.environ.get("AGENTBRIDGE_CONCURRENCY", "3"))
QUEUE_MAX = int(os.environ.get("AGENTBRIDGE_QUEUE_MAX", "8"))
TASK_TIMEOUT = int(os.environ.get("AGENTBRIDGE_TASK_TIMEOUT", "120"))
OUTPUT_MAX = int(os.environ.get("AGENTBRIDGE_OUTPUT_MAX", "8192"))
# 采集硬上限：读取子进程输出时只保留这么多字节，超出部分继续排空但丢弃。
# 单独配置以便测试调小；默认给截断留 4 倍余量（含多字节字符与 stderr）。
CAPTURE_MAX = int(os.environ.get("AGENTBRIDGE_CAPTURE_MAX", str(OUTPUT_MAX * 4)))
STDERR_MAX = int(os.environ.get("AGENTBRIDGE_STDERR_MAX", str(OUTPUT_MAX)))
BODY_MAX = int(os.environ.get("AGENTBRIDGE_BODY_MAX", str(64 * 1024)))
CONTEXT_MAX = int(os.environ.get("AGENTBRIDGE_CONTEXT_MAX", "6000"))
CONTEXT_RECENT = int(os.environ.get("AGENTBRIDGE_CONTEXT_RECENT", "8"))
AUDIT_DAYS = int(os.environ.get("AGENTBRIDGE_AUDIT_DAYS", "7"))
AUDIT_MAX_BYTES = int(os.environ.get("AGENTBRIDGE_AUDIT_MAX_BYTES", str(200 * 1024 * 1024)))
CONFIRM_TTL = int(os.environ.get("AGENTBRIDGE_CONFIRM_TTL", "300"))
# 提权计划的影响窗口：该窗口内同项目的后续任务一律降级为只读执行
PRIV_PLAN_TTL = int(os.environ.get("AGENTBRIDGE_PRIV_PLAN_TTL", "1800"))
# HTTP 连接读写时限（秒）：防止慢速客户端长期占用服务线程
HTTP_TIMEOUT = int(os.environ.get("AGENTBRIDGE_HTTP_TIMEOUT", "30"))

# 允许的来源通道。非空时请求必须携带白名单内的 channel，缺失同样拒绝，
# 不能用「缺字段」表示无限制。
ALLOWED_SENDERS = {
    s.strip() for s in os.environ.get("AGENTBRIDGE_ALLOWED_SENDERS", "").split(",") if s.strip()
}

DEFAULT_PROJECT = "default"

# --------------------------------------------------------------------------
# 错误码
# --------------------------------------------------------------------------


class AgentBridgeError(Exception):
    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


# --------------------------------------------------------------------------
# 任务分类（执行层软控制）
# --------------------------------------------------------------------------

# 正文匹配只是粗筛与提示，不是硬审批边界：真正的边界是
# effective_kind 的降级策略 + 敏感任务确认令牌 + 沙箱组合。
_PRIV_PATTERNS = [
    r"\bsudo\b", r"\bsu\b\s", r"\bdscl\b", r"\bsystemsetup\b",
    r"\blaunchctl\s+(load|unload|bootstrap|bootout|enable|disable)\b",
    r"\bsoftwareupdate\b", r"\bpfctl\b", r"\bchflags\b",
    r"/etc/", r"/Library/(LaunchDaemons|LaunchAgents|PrivilegedHelperTools)",
    r"/System/", r"\bcsrutil\b", r"\bdiskutil\s+erase", r"\bnewfs\b", r"\bmkfs",
    r"(系统设置|系统偏好|开机启动项|启动项|防火墙|钥匙串|账号密码|管理员权限)",
]

# 敏感 → 需二次确认
_SENSITIVE_PATTERNS = [
    r"\brm\s+-[a-zA-Z]*[rf]", r"\brm\s+.*\*", r"\bshred\b", r"\bmv\s+.*\*",
    r"\btruncate\b", r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+--force)",
    r"\bdrop\s+(table|database)\b", r"\btrash\b", r"\bunlink\b",
    r"(删除|清空|格式化|抹掉|覆盖掉|批量改|全部删)",
]

# 需脱敏的凭据模式（回传给用户前处理）
_SECRET_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 [REDACTED]"),
]


def classify(task: str) -> str:
    """返回 needs_privilege / sensitive / normal。顺序敏感：提权优先。

    这是对当前任务文本的粗筛；跨任务的提权扩散见 effective_kind。
    """
    low = task.lower()
    for p in _PRIV_PATTERNS:
        if re.search(p, task) or re.search(p, low):
            return "needs_privilege"
    for p in _SENSITIVE_PATTERNS:
        if re.search(p, task) or re.search(p, low):
            return "sensitive"
    return "normal"


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _as_bool(value) -> bool:
    """把 JSON/头部传来的布尔值规范成 bool（"0"/"false" 都是假）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def _task_digest(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 路径 / 锁键
# --------------------------------------------------------------------------

def normalize_dir(path: str) -> str:
    """规范化目录：realpath 解析符号链接 → 去尾斜杠 → 小写归一。

    小写归一的理由：macOS APFS 默认大小写不敏感，/a/Proj 与 /a/proj 是同一目录。
    归一后可能把大小写敏感卷上的两个不同目录误判为同一个——这是安全的失败方向
    （宁可过度串行，不可漏串行）。
    """
    if not path:
        raise AgentBridgeError("PROJECT_NOT_FOUND", "项目目录为空")
    expanded = os.path.expanduser(path)
    real = os.path.realpath(expanded)
    if not os.path.isdir(real):
        raise AgentBridgeError("PROJECT_NOT_FOUND", f"目录不存在：{expanded}")
    real = real.rstrip("/") or "/"
    return real.lower()


def lock_key_for(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _is_ancestor(a: str, b: str) -> bool:
    """按路径分量判断 a 是否为 b 的祖先（/a 是 /a/b 的祖先，但 /a 不是 /ab 的祖先）。"""
    if a == b:
        return True
    a = a.rstrip("/") + "/"
    return b.startswith(a)


def paths_overlap(a: str, b: str) -> bool:
    return _is_ancestor(a, b) or _is_ancestor(b, a)


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name       TEXT PRIMARY KEY,
    dir        TEXT NOT NULL,
    normalized TEXT NOT NULL,
    lock_key   TEXT NOT NULL,
    created_at REAL NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channels (
    channel        TEXT PRIMARY KEY,
    current_project TEXT,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    sender        TEXT,
    channel       TEXT,
    project       TEXT,
    cwd           TEXT,
    backend       TEXT NOT NULL DEFAULT 'dsh',
    task          TEXT NOT NULL,
    status        TEXT NOT NULL,
    error_code    TEXT,
    result        TEXT,
    result_digest TEXT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    ended_at      REAL,
    exit_code     INTEGER,
    dsh_session   TEXT,
    peak_rss_mb   REAL,
    sandbox_mode  TEXT,
    id_stable     INTEGER NOT NULL DEFAULT 1,
    timed_out     INTEGER NOT NULL DEFAULT 0,
    task_digest   TEXT,
    kind          TEXT,
    lock_key      TEXT,
    exec_pgid     INTEGER,
    exec_boot     TEXT,
    exec_identity TEXT,
    exec_state    TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE TABLE IF NOT EXISTS pending (
    job_id     TEXT PRIMARY KEY,
    token      TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases (
    alias      TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    lock_key   TEXT PRIMARY KEY,
    pgid       INTEGER,
    reason     TEXT NOT NULL,
    created_at REAL NOT NULL,
    cwd        TEXT
);
CREATE TABLE IF NOT EXISTS control_receipts (
    delivery_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    sender TEXT NOT NULL,
    channel TEXT NOT NULL,
    job_id TEXT,
    response TEXT,
    created_at REAL NOT NULL
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        # SQLite 按进程 umask 建库（常见 0644），连接后统一收紧为 0600
        self._conn = sqlite3.connect(path, check_same_thread=False)
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.chmod(path + suffix, 0o600)
            except OSError:
                pass
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """补齐执行器身份、启动状态与隔离路径；旧身份缺失记录保持保守。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        for col, decl in (("sender", "TEXT"), ("task_digest", "TEXT"), ("kind", "TEXT"), ("lock_key", "TEXT"),
                          ("exec_pgid", "INTEGER"), ("exec_boot", "TEXT"),
                          ("exec_identity", "TEXT"),
                          ("exec_state", "TEXT NOT NULL DEFAULT 'unknown'")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        for row in self._conn.execute(
                "SELECT job_id, cwd FROM jobs WHERE lock_key IS NULL AND cwd IS NOT NULL"):
            self._conn.execute("UPDATE jobs SET lock_key=? WHERE job_id=?",
                               (lock_key_for(row["cwd"]), row["job_id"]))
        pcols = {r[1] for r in self._conn.execute("PRAGMA table_info(projects)")}
        if "is_default" not in pcols:
            self._conn.execute(
                "ALTER TABLE projects ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
        self._conn.execute("UPDATE projects SET is_default=1 WHERE name=?",
                           (DEFAULT_PROJECT,))
        qcols = {r[1] for r in self._conn.execute("PRAGMA table_info(quarantine)")}
        if "cwd" not in qcols:
            self._conn.execute("ALTER TABLE quarantine ADD COLUMN cwd TEXT")
        self._conn.execute(
            "UPDATE quarantine SET cwd=COALESCE("
            "(SELECT cwd FROM jobs WHERE jobs.lock_key=quarantine.lock_key AND cwd IS NOT NULL LIMIT 1),"
            "(SELECT normalized FROM projects WHERE projects.lock_key=quarantine.lock_key LIMIT 1)) "
            "WHERE cwd IS NULL")

    def execute(self, sql: str, args: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    @contextlib.contextmanager
    def transaction(self):
        """多条语句一个事务：同一把应用锁内执行，成功才提交，异常回滚。

        确认创建（任务+令牌）、确认消费（删令牌+转 queued）、清理等
        多语句状态迁移必须整体原子——逐条提交会在语句之间暴露给
        prune_data 等并发清理，把有效任务误判为过期。
        """
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def query(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------

class Audit:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    def _file(self) -> str:
        return os.path.join(self.dir, time.strftime("audit-%Y%m%d.jsonl"))

    def write(self, record: dict) -> None:
        record = dict(record)
        record["ts"] = time.time()
        # 只记脱敏后的任务原文，绝不记认证头/密钥/环境变量
        if "task" in record and record["task"]:
            record["task"] = redact(str(record["task"]))[:2000]
        line = json.dumps(record, ensure_ascii=False) + "\n"
        path = self._file()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        self.prune()

    def prune(self) -> None:
        try:
            entries = sorted(os.listdir(self.dir))
        except OSError:
            return
        cutoff = time.time() - AUDIT_DAYS * 86400
        total = 0
        kept = []
        for name in entries:
            path = os.path.join(self.dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            total += st.st_size
            kept.append((st.st_mtime, path))
        # 超总量上限时从最旧的开始删
        kept.sort()
        idx = 0
        while total > AUDIT_MAX_BYTES and idx < len(kept):
            _, path = kept[idx]
            try:
                total -= os.stat(path).st_size
                os.unlink(path)
            except OSError:
                pass
            idx += 1


# --------------------------------------------------------------------------
# 沙箱模式校验
# --------------------------------------------------------------------------

def _dump_service_value(stdout: str, section: str, key: str) -> str | None:
    """从 dump-config 文本中提取某服务配置键的字面量值。

    值是 !!js 表达式（未被 patch 覆盖）时返回 None——视为未确定，
    由调用方按与预期不符处理。
    """
    in_section = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("- id:"):
            in_section = stripped == f"- id: {section}"
            continue
        if not in_section:
            continue
        m = re.match(rf"{key}:\s*(.*)$", stripped)
        if m:
            value = m.group(1).split("#")[0].strip()
            if not value or value.startswith("!!"):
                return None
            return value
    return None


def _verify_patch_isolation(label: str, patch: str, preset: str) -> None:
    """校验 patch 是否隔离 settings 并固定 permission 默认。

    dump-config 不求值 !!js，settings.path 只能按 patch 文本核对；
    固定后的最终会话权限由 verify_policy_isolation.mjs（真实模块）
    独立验证。
    """
    try:
        text = open(patch, encoding="utf-8").read()
    except OSError as exc:
        raise AgentBridgeError("EXEC_FAIL", f"无法读取 patch({label})：{exc}") from exc
    if "id: settings" not in text or "AGENTBRIDGE_DSH_SETTINGS" not in text:
        raise AgentBridgeError(
            "EXEC_FAIL",
            f"patch({label}) 缺少 settings 隔离段：日常 DSH 权限预设可能反向覆盖",
        )
    if f"defaultPreset: {preset}" not in text:
        raise AgentBridgeError(
            "EXEC_FAIL",
            f"patch({label}) 未固定 permission.defaultPreset={preset}",
        )


def _verify_isolated_settings_clean() -> None:
    """隔离的 settings 文件必须存在且不含 permission 段。

    文件里一旦出现 permission 配置，就等于把权限默认交还给持久化
    设置——桥接任务的权限只能来自 patch。
    """
    if not os.path.exists(DSH_SETTINGS_PATH):
        raise AgentBridgeError(
            "EXEC_FAIL",
            f"隔离 settings 不存在：{DSH_SETTINGS_PATH}（应由 worker 启动时创建）",
        )
    try:
        with open(DSH_SETTINGS_PATH, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise AgentBridgeError("EXEC_FAIL", f"无法读取隔离 settings：{exc}") from exc
    if "permission" in raw:
        raise AgentBridgeError(
            "EXEC_FAIL",
            f"隔离 settings（{DSH_SETTINGS_PATH}）包含 permission 段，拒绝启动；"
            "桥接任务的权限由 patch 固定，不读取持久化权限默认",
        )


def verify_sandbox_modes() -> dict:
    """用 --dump-config 读取实际生效的 sandbox/approval/permission 组合。

    只校验 sandbox 字符串是不够的：
    1. 内置权限预设按 (sandbox, approval) 二元组匹配，例如
       danger-full-access 必须搭配 approval=never，否则服务初始化直接抛
       "composed sandbox and approval defaults match no preset"；
    2. DSH 持久化的 permission.defaultPreset 会为新会话写入 sandbox/mode
       事件、优先于组合默认——因此 settings 必须隔离到桥接自有文件，
       且 permission 默认在 patch 中固定。
    """
    result: dict[str, dict] = {}
    expected = {
        "fullaccess": ("danger-full-access", "never", "danger-full-access"),
        "readonly": ("read-only", "ask", "read-only"),
    }
    _verify_isolated_settings_clean()
    for label, patch in (("fullaccess", PATCH_FULL), ("readonly", PATCH_RO)):
        if not os.path.exists(patch):
            raise AgentBridgeError("EXEC_FAIL", f"沙箱 patch 文件缺失：{patch}")
        cmd = [DSH, "--profile", "headless", "--dump-config", "--patch", patch]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            raise AgentBridgeError("EXEC_FAIL", f"无法校验沙箱配置({label})：{exc}") from exc
        if proc.returncode != 0:
            raise AgentBridgeError("EXEC_FAIL", f"dump-config 失败({label})：{proc.stderr[:200]}")
        mode = _dump_service_value(proc.stdout, "sandbox-policy", "mode")
        approval = _dump_service_value(proc.stdout, "approval", "policy")
        default_preset = _dump_service_value(proc.stdout, "permission", "defaultPreset")
        result[label] = {"sandbox": mode, "approval": approval,
                         "default_preset": default_preset}
        want_mode, want_approval, want_preset = expected[label]
        if (mode != want_mode or approval != want_approval
                or default_preset != want_preset):
            raise AgentBridgeError(
                "EXEC_FAIL",
                f"沙箱/审批/权限默认与预期不符：{label} 期望 sandbox={want_mode} "
                f"approval={want_approval} preset={want_preset}，"
                f"实际 sandbox={mode} approval={approval} preset={default_preset}",
            )
        _verify_patch_isolation(label, patch, want_preset)
    return result


# --------------------------------------------------------------------------
# 调度器
# --------------------------------------------------------------------------

class _Ticket:
    __slots__ = ("seq",)

    def __init__(self, seq: int):
        self.seq = seq


class ResourceGate:
    """单资源闸：按「接纳序号」放行，同一时刻至多一个持有者。

    票据在任务接纳的请求线程里 enqueue（非阻塞，带全局单调序号），
    执行线程 enter。这样执行顺序 = 接纳顺序，消除「已入库但线程
    还没走到 acquire」的空档——后接纳的任务无法插队。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._waiters: list[_Ticket] = []
        self._owner: _Ticket | None = None

    def enqueue(self, seq: int) -> _Ticket:
        """按序号把票据插入等待队列（非阻塞）。seq 全局唯一单调递增。"""
        ticket = _Ticket(seq)
        with self._cv:
            pos = len(self._waiters)
            for i, w in enumerate(self._waiters):
                if ticket.seq < w.seq:
                    pos = i
                    break
            self._waiters.insert(pos, ticket)
            self._cv.notify_all()
            return ticket

    def enter(self, ticket: _Ticket) -> None:
        """阻塞直到轮到该票据并获得持有权。"""
        with self._cv:
            try:
                # 双条件：无人持有，且自己是队首（按序 FIFO + 互斥）
                while self._owner is not None or self._waiters[0] is not ticket:
                    self._cv.wait()
            except BaseException:
                self.abandon(ticket)
                raise
            self._waiters.pop(0)
            self._owner = ticket

    def release(self, ticket: _Ticket) -> None:
        with self._cv:
            if self._owner is ticket:
                self._owner = None
            else:
                self.abandon(ticket)
                return
            self._cv.notify_all()

    def abandon(self, ticket: _Ticket) -> None:
        """放弃排队中的票据（未获得持有权时），唤醒后继。"""
        with self._cv:
            try:
                self._waiters.remove(ticket)
            except ValueError:
                pass
            self._cv.notify_all()


class Scheduler:
    """每资源 FIFO 闸 + 全局并发闸。

    锁粒度是「规范化真实目录」，不是项目名 —— 两个项目名指向同一目录时共享闸。
    祖先/后代目录之间的重叠在「项目注册时」拒绝（default 泛目录除外），
    运行时不加前缀锁，避免 $HOME 下的任务退化为全串行。
    """

    def __init__(self, concurrency: int, queue_max: int):
        self._sem = threading.Semaphore(concurrency)
        self._queue_max = queue_max
        self._lock = threading.Lock()
        self._gates: dict[str, ResourceGate] = {}
        self._waiting = 0
        self._running: dict[str, dict] = {}

    def reserve(self) -> None:
        with self._lock:
            if self._waiting >= self._queue_max:
                raise AgentBridgeError("QUEUE_FULL", f"排队已满（{self._queue_max}）")
            self._waiting += 1

    def release_reservation(self) -> None:
        with self._lock:
            self._waiting = max(0, self._waiting - 1)

    def resource_gate(self, key: str) -> ResourceGate:
        with self._lock:
            if key not in self._gates:
                self._gates[key] = ResourceGate()
            return self._gates[key]

    def begin_running(self) -> None:
        """取得全局运行名额，并立即归还等待位。

        释放等待位是关键：排队上限只统计「等待中」的任务，不含运行中的，
        否则长任务会把排队名额长期占用，使队列上限失去意义。
        """
        self._sem.acquire()
        self.release_reservation()

    def end_running(self) -> None:
        self._sem.release()

    def mark_running(self, job_id: str, info: dict) -> None:
        with self._lock:
            self._running[job_id] = info

    def clear_running(self, job_id: str) -> None:
        with self._lock:
            self._running.pop(job_id, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": list(self._running.values()),
                "waiting": self._waiting,
                "queue_max": self._queue_max,
            }


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _ProcBSDInfo(ctypes.Structure):
    # macOS sys/proc_info.h: PROC_PIDTBSDINFO, including microsecond birth time.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        "nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64)]


def _executor_identity(pgid: int) -> str | None:
    """Read the group leader's OS birth identity; uncertainty never authorizes kill."""
    try:
        if sys.platform == "darwin":
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            query = libproc.proc_pidinfo
            query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]
            query.restype = ctypes.c_int
            info = _ProcBSDInfo()
            size = ctypes.sizeof(info)
            if query(pgid, 3, 0, ctypes.byref(info), size) != size:
                return None
            if info.pid != pgid or info.pgid != pgid:
                return None
            return f"darwin:{pgid}:{info.start_sec}:{info.start_usec}"
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pgid}/stat", encoding="ascii") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            if int(fields[2]) != pgid:  # field 5: process group
                return None
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
                boot = fh.read().strip()
            return f"linux:{boot}:{pgid}:{fields[19]}"  # field 22: start ticks
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return None


_EXEC_GATE = (
    "import os,sys; fd=int(sys.argv[1]); "
    "go=os.read(fd,1); os.close(fd); "
    "os.execvpe(sys.argv[2],sys.argv[2:],os.environ) if go==b'1' else sys.exit(125)"
)


def _supervise_group(pgid: int, deadline: float) -> bool:
    """父进程退出后监督同组后代至期限；逾期回收整组。

    返回是否实际执行了回收。期限前后代自然退出则不打扰。
    """
    if not _group_alive(pgid):
        return False
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if not _group_alive(pgid):
            return False
    _killpg(pgid, signal.SIGTERM)
    time.sleep(1.0)
    if _group_alive(pgid):
        _killpg(pgid, signal.SIGKILL)
    return True


def run_task(cmd: list[str], cwd: str, timeout: int, on_start=None) -> dict:
    """执行子进程，超时回收整个进程组，返回有界输出与峰值内存。

    采集上限：stdout 保留至多 CAPTURE_MAX 字节、stderr 至多 STDERR_MAX
    字节；超限后继续排空管道（避免子进程写阻塞）但丢弃内容。因此
    OUTPUT_MAX 只是「回传截断」，CAPTURE_MAX 才是内存/磁盘边界。

    已知限制：主动 setsid 脱离进程组的后代无法通过 killpg 回收。
    """
    capture_out = CAPTURE_MAX
    capture_err = STDERR_MAX
    m_out, s_out = os.pipe()
    m_err, s_err = os.pipe()
    gate_read, gate_write = os.pipe()
    try:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-I", "-c", _EXEC_GATE, str(gate_read), *cmd],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=s_out,
            stderr=s_err,
            start_new_session=True,  # 独立进程组，便于整组回收
            close_fds=True,
            pass_fds=(gate_read,),
        )
    except Exception as exc:  # noqa: BLE001
        for fd in (m_out, s_out, m_err, s_err, gate_read, gate_write):
            try:
                os.close(fd)
            except OSError:
                pass
        raise AgentBridgeError("EXEC_FAIL", f"无法启动后端：{exc}") from exc
    finally:
        # 从站描述符已被子进程继承，父进程侧必须关闭
        for fd in (s_out, s_err, gate_read):
            try:
                os.close(fd)
            except OSError:
                pass

    # start_new_session 使子进程成为组长，pgid == 子进程 pid。
    # 先记下初始 PGID：父进程被回收后 os.getpgid(pid) 会失败，
    # 而组可能仍有忽略 SIGTERM 的后代存活。
    pgid = proc.pid
    try:
        if on_start is not None:
            on_start(pgid)
    except BaseException:
        # The backend has not been exec'd: no work may precede durable registration.
        os.close(gate_write)
        _killpg(pgid, signal.SIGKILL)
        proc.wait()
        os.close(m_out)
        os.close(m_err)
        raise

    holder: dict = {}
    chunks_out: list[bytes] = []
    chunks_err: list[bytes] = []
    drained = {"out": 0, "err": 0}

    def _drain(fd: int, chunks: list[bytes], stat_key: str, cap: int) -> None:
        kept = 0
        try:
            while True:
                data = os.read(fd, 65536)
                if not data:
                    break
                drained[stat_key] += len(data)
                if kept < cap:
                    take = data[: cap - kept]
                    chunks.append(take)
                    kept += len(take)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    readers = [
        threading.Thread(target=_drain, args=(m_out, chunks_out, "out", capture_out), daemon=True),
        threading.Thread(target=_drain, args=(m_err, chunks_err, "err", capture_err), daemon=True),
    ]
    def _wait() -> None:
        try:
            pid, status, ru = os.wait4(proc.pid, 0)
            holder["status"] = status
            holder["ru"] = ru
        except ChildProcessError:
            holder["status"] = 0

    waiter = threading.Thread(target=_wait, daemon=True)
    try:
        for t in readers:
            t.start()
        waiter.start()
        os.write(gate_write, b"1")
    except BaseException:
        _killpg(pgid, signal.SIGKILL)
        if waiter.ident is not None:
            waiter.join(5)
            if "status" in holder:
                proc.returncode = os.waitstatus_to_exitcode(holder["status"])
        else:
            proc.wait()
        for t, fd in zip(readers, (m_out, m_err)):
            if t.ident is not None:
                t.join(2)
            else:
                os.close(fd)
        raise
    finally:
        os.close(gate_write)
    deadline = time.monotonic() + timeout
    waiter.join(timeout)

    timed_out = False
    group_reaped = False
    if waiter.is_alive():
        timed_out = True
        _killpg(pgid, signal.SIGTERM)
        waiter.join(5)
        # 无论直接父进程是否已退出，都探测并强杀整个组：
        # 父进程先死、后代忽略 SIGTERM 时，waiter 已返回但组仍存活。
        # （组探测+kill 之间存在极小的 PID 复用窗口，此处选择宁可误杀
        # 复用组也不放过残留进程。）
        try:
            os.killpg(pgid, 0)
            _killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        waiter.join(5)
    else:
        # 父进程正常退出 ≠ 任务结束：同组的普通后台后代仍在期限预算内
        # 运行、持有输出管道、可能继续改文件。一次性任务语义下监督整组
        # 到期限；逾期先 TERM 后 KILL。有意常驻的服务需等显式登记机制，
        # 当前一律按一次性任务回收。
        group_reaped = _supervise_group(pgid, deadline)

    status = holder.get("status", -signal.SIGKILL)
    try:
        proc.returncode = os.waitstatus_to_exitcode(status) if not timed_out else -9
    except (ValueError, OverflowError):
        proc.returncode = -9

    # 组已回收，管道写端全部关闭后读者线程自然结束；
    # 若有脱离组的后代仍持有写端，给 2 秒后放弃（守护线程）。
    for t in readers:
        t.join(2)

    stdout = b"".join(chunks_out).decode("utf-8", "replace")
    stderr = b"".join(chunks_err).decode("utf-8", "replace")

    ru = holder.get("ru")
    peak_mb = None
    if ru is not None:
        raw = float(ru.ru_maxrss)
        # macOS 报字节，Linux 报 KB
        peak_mb = raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "peak_rss_mb": peak_mb,
        "out_truncated": drained["out"] > capture_out,
        "err_truncated": drained["err"] > capture_err,
        "group_reaped": group_reaped,
    }


# --------------------------------------------------------------------------
# 上下文（桥接层维护，非 DSH 原生会话）
# --------------------------------------------------------------------------

_UNRESOLVED = ("failed", "cancelled", "uncertain")


def build_context(db: DB, project: str) -> str:
    # 注意：不注入 pending-confirm 条目——它们尚未执行，
    # 且防止「确认回环」内容扩散进后续任务的上下文。
    rows = db.query(
        "SELECT job_id, task, status, result_digest, created_at FROM jobs "
        "WHERE project = ? AND status IN ('done','failed','cancelled','uncertain') "
        "ORDER BY created_at DESC LIMIT 100",
        (project,),
    )
    recent = rows[:CONTEXT_RECENT]
    unresolved_ids = {r["job_id"] for r in rows if r["status"] in _UNRESOLVED}
    # 未完成项即使超出最近窗口也必须注入
    merged = {r["job_id"]: r for r in recent}
    for r in rows:
        if r["job_id"] in unresolved_ids:
            merged.setdefault(r["job_id"], r)
    ordered = sorted(merged.values(), key=lambda r: r["created_at"], reverse=True)

    lines = []
    for r in ordered:
        task = redact(str(r["task"] or ""))[:120]
        digest = redact(str(r["result_digest"] or ""))[:200]
        lines.append(f"- [{r['status']}] {task} → {digest}")
        if len("\n".join(lines)) > CONTEXT_MAX:
            lines.pop()
            break
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > CONTEXT_MAX:
        body = body[:CONTEXT_MAX] + "…"
    return (
        "【本会话此前任务记录（桥接层维护，非 DSH 原生会话）】\n"
        f"{body}\n"
        "【记录结束】以上仅供参考；未完成或失败的条目尤其需要注意。\n\n"
    )


def effective_kind(db: DB, project: str, task: str) -> tuple[str, bool]:
    """返回 (执行类别, 是否降级)。

    正文只看当前任务的分类不够：前一条 sudo 任务产出的计划会进入
    后续任务的上下文，「照刚才的方案执行」这类文本本身不含提权词。
    因此在 PRIV_PLAN_TTL 窗口内，同项目只要出现过 needs_privilege
    任务，后续任务一律按 needs_privilege 只读执行。
    宁可过度限制，不可让提权计划顺流进全权限执行。

    关键细节：只有「原始分类」确实是 needs_privilege 的任务才算数。
    被降级的任务 kind 同样是 needs_privilege，若拿它们续期窗口，
    持续对话会让只读锁定永久不解除——因此这里用 classify(原始任务) 复核。
    """
    kind = classify(task)
    if kind == "needs_privilege":
        return kind, False
    rows = db.query(
        "SELECT task FROM jobs WHERE project = ? AND kind = 'needs_privilege' "
        "AND created_at > ? LIMIT 50",
        (project, time.time() - PRIV_PLAN_TTL),
    )
    for row in rows:
        if classify(row["task"] or "") == "needs_privilege":
            return "needs_privilege", True
    return kind, False


# --------------------------------------------------------------------------
# 应用主体
# --------------------------------------------------------------------------

class App:
    def __init__(self, verify: bool = True) -> None:
        self._seq = itertools.count(1)
        # 本次 worker 的审计标签，不代替执行器的 OS 启动身份。
        self.boot_id = uuid.uuid4().hex[:16]
        # 接纳线性化锁：数据库认领 + 项目复核 + 取号 + 入队 + 粘性写
        # 在同一临界区内完成，保证「接纳顺序 = 执行顺序 = 路由生效顺序」
        # （同资源）。锁只持有到入队完成，不跨实际执行。
        self._admit_lock = threading.Lock()
        # 项目注册/改绑/删除的检查与写入同锁（内部再取 admit 锁与任务
        # 接纳协调，锁序固定 projects → admit），防止并发绕过重叠检查
        self._projects_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._ensure_dsh_settings()
        self.db = DB(DB_PATH)
        self.audit = Audit(AUDIT_DIR)
        self.sched = Scheduler(CONCURRENCY, QUEUE_MAX)
        # verify=False 仅供单元测试使用；生产路径 main() 始终为 True。
        self.sandbox_modes = verify_sandbox_modes() if verify else {}
        self._last_prune = 0.0
        self._ensure_default_project()
        self._recover_stale_jobs()
        self.prune_data()

    def next_seq(self) -> int:
        """任务接纳序号：全局单调递增，驱动同资源按接纳顺序执行。"""
        return next(self._seq)

    def _ensure_dsh_settings(self) -> None:
        """准备桥接隔离的 DSH settings 并固定到子进程环境。

        patch 将 settings 服务指向此文件、并固定 permission.defaultPreset；
        文件内不含 permission 段，日常 UI 的权限预设因此无法进入
        桥接发起的会话（只读计划不会被反向放开）。
        """
        try:
            os.makedirs(os.path.dirname(DSH_SETTINGS_PATH) or ".", mode=0o700,
                        exist_ok=True)
            if not os.path.exists(DSH_SETTINGS_PATH):
                fd = os.open(DSH_SETTINGS_PATH,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, b"{}\n")
                os.close(fd)
            else:
                os.chmod(DSH_SETTINGS_PATH, 0o600)
        except OSError:
            pass
        os.environ["AGENTBRIDGE_DSH_SETTINGS"] = DSH_SETTINGS_PATH

    def _ensure_default_project(self) -> None:
        row = self.db.one("SELECT name FROM projects WHERE name = ?", (DEFAULT_PROJECT,))
        if not row:
            norm = normalize_dir(HOME)
            self.db.execute(
                "INSERT INTO projects (name, dir, normalized, lock_key, created_at, is_default) "
                "VALUES (?,?,?,?,?,1)",
                (DEFAULT_PROJECT, HOME, norm, lock_key_for(norm), time.time()),
            )
        else:
            self.db.execute("UPDATE projects SET is_default=1 WHERE name=?", (DEFAULT_PROJECT,))

    def _recover_stale_jobs(self) -> None:
        """崩溃恢复：上一进程留下的 queued/running 先结算遗留执行器再转 uncertain。

        worker 被 SIGKILL 时，start_new_session 的后端进程不会随主进程退出，
        期限监督也随之消失。恢复必须先对账：能验证归属的遗留组直接回收；
        无法安全判定（PID 复用嫌疑 / 杀不掉）则隔离该资源，禁止新任务
        与旧执行器并发写同一目录。uncertain 仍禁止自动重放（force=1 才重跑）。
        """
        now = time.time()
        rows = self.db.query(
            "SELECT job_id, project, cwd, lock_key, exec_pgid, exec_identity, exec_state "
            "FROM jobs WHERE status IN ('queued','running')")
        for r in rows:
            verdict = self._settle_stale_executor(r)
            self.db.execute(
                "UPDATE jobs SET status='uncertain', error_code='INTERRUPTED', "
                "ended_at=? WHERE job_id=? AND status IN ('queued','running')",
                (now, r["job_id"]),
            )
            self.audit.write({
                "job_id": r["job_id"], "project": r["project"],
                "event": "recovered-stale", "settlement": verdict,
            })

    def _settle_stale_executor(self, row) -> str:
        """结算一个遗留执行器：dead / killed / quarantined。"""
        pgid = row["exec_pgid"]
        # With the startup gate, a crash before registration cannot start backend work.
        if not pgid and row["exec_state"] in ("idle", "starting"):
            return "dead"
        if pgid and not _group_alive(pgid):
            return "dead"
        identity = row["exec_identity"]
        if pgid and identity and _executor_identity(pgid) == identity:
            _killpg(pgid, signal.SIGKILL)
            time.sleep(0.2)
            if not _group_alive(pgid):
                return "killed"
        # Missing legacy identity, absent leader, or a reused PID is never kill authority.
        self.db.execute(
            "INSERT OR REPLACE INTO quarantine (lock_key, pgid, reason, created_at, cwd) "
            "VALUES (?,?,?,?,?)",
            (row["lock_key"] or f"unknown:{row['job_id']}", pgid,
             f"遗留执行器 pgid={pgid}（job {row['job_id']}）归属未确认",
             time.time(), row["cwd"]),
        )
        return "quarantined"

    def _check_quarantine(self, lock_key: str, cwd: str | None = None) -> None:
        """Reject overlapping quarantined paths; unknown executor identity stays fail-closed."""
        if cwd is None:
            project = self.db.one("SELECT normalized FROM projects WHERE lock_key=?", (lock_key,))
            cwd = project["normalized"] if project else None
        for row in self.db.query("SELECT * FROM quarantine"):
            if row["pgid"] is not None and not _group_alive(row["pgid"]):
                self.db.execute("DELETE FROM quarantine WHERE lock_key=?", (row["lock_key"],))
                continue
            path = row["cwd"]
            if not path:
                project = self.db.one("SELECT normalized FROM projects WHERE lock_key=?", (row["lock_key"],))
                path = project["normalized"] if project else None
            if not cwd or not path or paths_overlap(cwd, path):
                raise AgentBridgeError(
                    "BUSY", f"资源已隔离：{row['reason']}。重叠目录暂不接纳任务或项目变更；"
                    "已知进程组退出后自动解除；身份缺失的旧记录需人工核实并清理隔离记录。")

    def prune_data(self) -> None:
        """按保留期清理 jobs/pending 中的任务原文与结果。

        审计 prune 只管 JSONL；数据库里的 task/result 是同一批隐私数据，
        必须有同样的保留期限，否则长期增长且无法收回。
        过期/被清掉的待确认任务在此明确落终态 cancelled——
        不能让无令牌的 pending-confirm 永远占住项目（BUSY）。
        全程单事务：与确认创建/消费的多语句迁移互斥，不会把
        语句间隙里的正常迁移误判为过期。
        """
        now = time.time()
        cutoff = now - AUDIT_DAYS * 86400
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status='cancelled', error_code='CONFIRM_EXPIRED', "
                "ended_at=?, result='确认超时未确认，任务已取消；如仍需执行请重新发起。' "
                "WHERE status='pending-confirm' AND job_id NOT IN "
                "(SELECT job_id FROM pending WHERE expires_at > ?)",
                (now, now),
            )
            conn.execute(
                "DELETE FROM jobs WHERE (ended_at IS NOT NULL AND ended_at < ?) "
                "OR (ended_at IS NULL AND created_at < ?)", (cutoff, cutoff))
            conn.execute("DELETE FROM pending WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM aliases WHERE created_at < ?", (cutoff,))

    def get_job(self, job_id: str) -> dict | None:
        """按 job_id 查询任务状态/结果（供 shim 轮询）。

        查不到时尝试确认别名（确认消息 ID → 目标任务），让响应丢失
        后的对账也能解析到真实任务。
        """
        row = self.db.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if row is not None:
            return _job_response(self, row)
        arow = self.db.one("SELECT job_id FROM aliases WHERE alias = ?", (job_id,))
        if arow:
            row = self.db.one("SELECT * FROM jobs WHERE job_id = ?", (arow["job_id"],))
            if row is not None:
                resp = _job_response(self, row, reused=True)
                resp["requested_id"] = job_id
                return resp
        return None

    def maybe_prune_data(self) -> None:
        now = time.time()
        if now - self._last_prune > 300:
            self._last_prune = now
            self.prune_data()

    # ---- 项目 / 通道 ----

    def add_project(self, name: str, directory: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", name or ""):
            raise AgentBridgeError("EXEC_FAIL", "项目名只能包含字母、数字、点、下划线、连字符")
        if name == DEFAULT_PROJECT:
            raise AgentBridgeError("EXEC_FAIL", "default 是保留的泛目录会话，不能覆盖注册")
        norm = normalize_dir(directory)
        # 检查与写入同锁：否则父/子目录并发注册时各自都看不到对方，
        # 重叠拒绝形同虚设。内部再取 admit 锁：任务接纳在 admit 锁内
        # 复核项目存在性（锁序固定 projects → admit，无死锁），
        # 「注册/改绑/删除」与「快照接纳」因此无法交错。
        with self._projects_lock, self._admit_lock:
            self._check_quarantine(lock_key_for(norm), norm)
            same = self.db.one(
                "SELECT name, normalized FROM projects WHERE name = ?", (name,))
            if same and same["normalized"] == norm:
                return {"ok": True, "project": name, "dir": directory, "normalized": norm}

            # 改绑检查：有未终态任务时禁止换目录——待确认任务尤其不允许
            # 批准后被换到另一个目录执行。已过期/无令牌的待确认不算忙碌。
            if same:
                busy = self.db.one(
                    "SELECT job_id FROM jobs WHERE project = ? "
                    "AND (status IN ('queued','running') "
                    "     OR (status='pending-confirm' AND job_id IN "
                    "         (SELECT job_id FROM pending WHERE expires_at > ?))) "
                    "LIMIT 1", (name, time.time()))
                if busy:
                    raise AgentBridgeError(
                        "BUSY", f"项目 {name} 有排队/运行/待确认任务，拒绝改绑目录")

            # 重叠检查只针对「专属项目」。default 绑定 HOME 属于泛目录会话，
            # 不参与互斥——否则 HOME 下所有真实项目都无法注册。
            for row in self.db.query(
                    "SELECT name, normalized FROM projects WHERE is_default = 0 AND name != ?",
                    (name,)):
                if paths_overlap(norm, row["normalized"]):
                    raise AgentBridgeError(
                        "EXEC_FAIL",
                        f"目录与项目 {row['name']} 重叠（祖先或后代关系），拒绝注册",
                    )
            self.db.execute(
                "INSERT INTO projects (name, dir, normalized, lock_key, created_at, is_default) "
                "VALUES (?,?,?,?,?,0) "
                "ON CONFLICT(name) DO UPDATE SET dir=excluded.dir, normalized=excluded.normalized, "
                "lock_key=excluded.lock_key",
                (name, directory, norm, lock_key_for(norm), time.time()),
            )
        return {"ok": True, "project": name, "dir": directory, "normalized": norm}

    def list_projects(self, channel: str | None = None) -> dict:
        rows = self.db.query(
            "SELECT name, dir, normalized, is_default FROM projects ORDER BY name")
        current = self.current_project(channel) if channel else None
        return {
            "ok": True,
            "current": current,
            "projects": [
                {"name": r["name"], "dir": r["dir"], "current": r["name"] == current,
                 "default": bool(r["is_default"])}
                for r in rows
            ],
        }

    def rm_project(self, name: str) -> dict:
        if name == DEFAULT_PROJECT:
            raise AgentBridgeError("EXEC_FAIL", "不能删除 default 项目")
        # 同 add_project：projects → admit 双锁，删除与任务快照接纳互斥
        with self._projects_lock, self._admit_lock:
            project = self.db.one("SELECT normalized,lock_key FROM projects WHERE name=?", (name,))
            if project:
                self._check_quarantine(project["lock_key"], project["normalized"])
            busy = self.db.one(
                "SELECT job_id FROM jobs WHERE project = ? "
                "AND (status IN ('queued','running') "
                "     OR (status='pending-confirm' AND job_id IN "
                "         (SELECT job_id FROM pending WHERE expires_at > ?))) "
                "LIMIT 1",
                (name, time.time()),
            )
            if busy:
                raise AgentBridgeError("BUSY", f"项目 {name} 有运行中/排队/待确认任务，拒绝删除")
            self.db.execute("DELETE FROM projects WHERE name = ?", (name,))
            self.db.execute("UPDATE channels SET current_project = ? WHERE current_project = ?",
                            (DEFAULT_PROJECT, name))
        return {"ok": True, "removed": name}

    def current_project(self, channel: str | None) -> str:
        if not channel:
            return DEFAULT_PROJECT
        row = self.db.one("SELECT current_project FROM channels WHERE channel = ?", (channel,))
        if not row or not row["current_project"]:
            return DEFAULT_PROJECT
        exists = self.db.one("SELECT name FROM projects WHERE name = ?", (row["current_project"],))
        return row["current_project"] if exists else DEFAULT_PROJECT

    def set_current_project(self, channel: str, project: str) -> None:
        self.db.execute(
            "INSERT INTO channels (channel, current_project, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(channel) DO UPDATE SET current_project=excluded.current_project, "
            "updated_at=excluded.updated_at",
            (channel, project, time.time()),
        )

    def get_project(self, name: str):
        row = self.db.one("SELECT * FROM projects WHERE name = ?", (name,))
        if not row:
            raise AgentBridgeError("PROJECT_NOT_FOUND", f"项目不存在：{name}")
        return row


APP: App | None = None  # noqa: N816


# --------------------------------------------------------------------------
# 任务执行流程
# --------------------------------------------------------------------------

def build_cmd(task: str, kind: str) -> tuple[list[str], str, str]:
    """返回 (命令, 沙箱模式标签, patch 路径)。需提权 → 只读沙箱只产计划。"""
    if kind == "needs_privilege":
        prefix = (
            "你只有只读权限：本机文件不可写。注意这只约束本机沙箱，"
            "不构成对远端免密 sudo 等主机侧操作的隔离。\n"
            "不要尝试修改任何文件或提权。\n"
            "请输出：(1) 你打算做什么 (2) 需要用户在自己的终端执行的完整命令。\n"
            "任务：" + task
        )
        return [DSH, "--profile", "headless", "--patch", PATCH_RO, prefix], "read-only", PATCH_RO
    return [DSH, "--profile", "headless", "--patch", PATCH_FULL, task], "danger-full-access", PATCH_FULL


def _task_snapshot(job_id: str, channel: str, project: str, task: str,
                   kind: str, downgraded: bool, proj_row) -> dict:
    """任务接纳快照：目录、资源键、任务内容与执行类别在此固化。

    之后的项目改绑不影响已接纳任务；确认令牌只授权这份快照。
    """
    return {
        "job_id": job_id,
        "channel": channel,
        "project": project,
        "cwd": proj_row["normalized"],
        "lock_key": proj_row["lock_key"],
        "task": task,
        "task_digest": _task_digest(task),
        "kind": kind,
        "downgraded": downgraded,
    }


def _snap_from_row(row) -> dict:
    return {
        "job_id": row["job_id"],
        "channel": row["channel"] or "",
        "sender": row["sender"] or "",
        "project": row["project"],
        "cwd": row["cwd"],
        "lock_key": row["lock_key"] or (lock_key_for(row["cwd"]) if row["cwd"] else ""),
        "task": row["task"],
        "task_digest": row["task_digest"] or _task_digest(row["task"]),
        "kind": row["kind"] or classify(row["task"]),
        "downgraded": (row["kind"] or classify(row["task"])) == "needs_privilege"
                      and classify(row["task"]) != "needs_privilege",
    }


def _busy_response(row, message: str | None = None) -> dict:
    return {
        "ok": False, "code": "BUSY", "reused": True,
        "message": message or f"该任务仍在执行（job_id={row['job_id'][:8]}）",
        "job_id": row["job_id"], "project": row["project"],
        "backend": row["backend"], "status": row["status"],
    }


def _check_source(row, sender: str, channel: str) -> None:
    if ((row["channel"] or "") != channel
            or (row["sender"] and row["sender"] != sender)):
        raise AgentBridgeError("AUTH_FAIL", "来源与任务发起者或会话不一致")


def _control_job(app: App, job_id: str, sender: str, channel: str):
    row = app.db.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
    if row is None:
        alias = app.db.one("SELECT job_id FROM aliases WHERE alias=?", (job_id,))
        if alias:
            row = app.db.one("SELECT * FROM jobs WHERE job_id=?", (alias["job_id"],))
    if row is None:
        raise AgentBridgeError("NOT_FOUND", "任务不存在")
    _check_source(row, sender, channel)
    return row


CONTROL_OPS = frozenset(("status", "confirm", "force", "project.add", "project.rm", "project.list", "use"))


def do_control(app: App, payload: dict) -> dict:
    sender = str(payload.get("sender") or "").strip()
    channel = str(payload.get("channel") or "").strip()
    if not sender or not channel or (ALLOWED_SENDERS and sender not in ALLOWED_SENDERS):
        raise AgentBridgeError("AUTH_FAIL", "控制操作必须提供允许的发送者与会话身份")
    op = payload.get("op")
    if op not in CONTROL_OPS:
        raise AgentBridgeError("EXEC_FAIL", "不支持的控制操作")
    job_id = str(payload.get("job_id") or "").strip()
    if op == "status":
        if job_id:
            return _job_response(app, _control_job(app, job_id, sender, channel))
        recent = app.db.query(
            "SELECT job_id,project,status FROM jobs WHERE channel=? AND (sender=? OR sender IS NULL OR sender='') "
            "ORDER BY created_at DESC LIMIT 10", (channel, sender))
        return {"ok": True, "current": app.current_project(channel),
                "recent": [dict(row) for row in recent]}
    if op == "project.list":
        return app.list_projects(channel)
    delivery = str(payload.get("delivery_id") or "").strip()
    if not delivery or len(delivery) > 128:
        raise AgentBridgeError("EXEC_FAIL", "修改类控制操作必须携带投递 ID")
    # Store only a digest, never a confirmation token or command body.
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with app._control_lock:
        receipt = app.db.one("SELECT * FROM control_receipts WHERE delivery_id=?", (delivery,))
        if receipt:
            if receipt["sender"] != sender or receipt["channel"] != channel or receipt["digest"] != digest:
                raise AgentBridgeError("AUTH_FAIL", "投递 ID 已绑定其他控制请求")
            if receipt["job_id"]:
                return _job_response(app, _control_job(app, receipt["job_id"], sender, channel), reused=True)
            if receipt["response"]:
                return {**json.loads(receipt["response"]), "reused": True}
            alias = app.db.one("SELECT job_id FROM aliases WHERE alias=?", (delivery,))
            if alias:
                return _job_response(app, _control_job(app, alias["job_id"], sender, channel), reused=True)
            return {"ok": False, "code": "CONTROL_UNKNOWN", "reused": True,
                    "message": "原控制操作结果未知，不会重复授权；请先查询状态。"}
        if op == "force":
            row = _control_job(app, job_id, sender, channel)
            if row["status"] != "uncertain":
                raise AgentBridgeError("EXEC_FAIL", "仅可显式重跑 uncertain 任务")
        if op == "confirm" and not str(payload.get("token") or "").strip():
            raise AgentBridgeError("AUTH_FAIL", "缺少确认令牌")
        # Durable intent before side effects. After a crash, an incomplete receipt fails closed.
        app.db.execute("INSERT INTO control_receipts VALUES (?,?,?,?,?,?,?)",
                       (delivery, digest, sender, channel, row["job_id"] if op == "force" else None, None, time.time()))
        try:
            if op == "force":
                result = _rerun_uncertain(app, row, row["task"], wait=False)
            elif op == "confirm":
                result = do_execute(app, {"task": "confirm", "sender": sender, "channel": channel,
                    "confirm": str(payload["token"]), "job_id": delivery, "wait": False})
            elif op == "project.add":
                directory = str(payload.get("dir") or "")
                if not os.path.isabs(directory):
                    raise AgentBridgeError("EXEC_FAIL", "项目目录必须是绝对路径")
                result = app.add_project(payload.get("name", ""), directory)
            elif op == "project.rm":
                result = app.rm_project(payload.get("name", ""))
            else:
                name = str(payload.get("project") or "")
                with app._projects_lock:
                    app.get_project(name)
                    app.set_current_project(channel, name)
                result = {"ok": True, "current": name}
        except AgentBridgeError:
            # Rejected requests have no authorization effect; retain pending tokens for retry.
            app.db.execute("DELETE FROM control_receipts WHERE delivery_id=?", (delivery,))
            raise
        app.db.execute("UPDATE control_receipts SET job_id=?,response=? WHERE delivery_id=?",
                       (result.get("job_id"), json.dumps(result, ensure_ascii=False), delivery))
        return result


def do_execute(app: App, payload: dict) -> dict:
    task = (payload.get("task") or "").strip()
    if not task:
        raise AgentBridgeError("EXEC_FAIL", "任务为空")
    if len(task) > BODY_MAX:
        raise AgentBridgeError("EXEC_FAIL", "任务过长")

    channel = (payload.get("channel") or "").strip()
    sender = (payload.get("sender") or "").strip()
    # 白名单校验的是「发送者身份」（actor）；channel 是会话路由键，
    # 两者分离——同一发送者的不同会话不应共享粘性项目绑定。
    # 白名单配置后，缺失身份字段同样拒绝——缺字段不代表无限制。
    if ALLOWED_SENDERS:
        actor = sender or channel
        if not actor or actor not in ALLOWED_SENDERS:
            raise AgentBridgeError(
                "AUTH_FAIL", "发送者不在允许列表内（白名单模式下必须提供受信任身份）")

    # wait=false：接纳后立即返回 ACCEPTED，由后台线程执行（shim 轮询取结果）。
    # 排队/执行时间不计入客户端等待，超时不再被误报为离线。
    wait = _as_bool(payload.get("wait", True))

    # 项目解析：显式指定优先，否则沿用该通道当前项目
    explicit = (payload.get("project") or "").strip()
    if explicit:
        app.get_project(explicit)
        target_project = explicit
    else:
        target_project = app.current_project(channel)

    # job_id：显式提供则复用，否则派生
    job_id = (payload.get("job_id") or "").strip()
    id_stable = _as_bool(payload.get("id_stable", True))
    if not job_id:
        seed = f"{channel}|{target_project}|{time.time_ns()}|{uuid.uuid4().hex}"
        job_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
        id_stable = False

    digest = _task_digest(task)
    confirm = str(payload.get("confirm") or "").strip()
    force = _as_bool(payload.get("force"))

    # 确认消息 ID → 目标任务的持久别名（U2）：确认消费成功时写入，
    # 响应丢失后的对账与令牌耗尽后的重投都靠它解析回目标任务
    alias_target = ""
    if job_id:
        arow = app.db.one("SELECT job_id FROM aliases WHERE alias = ?", (job_id,))
        if arow:
            alias_target = arow["job_id"]

    existing = app.db.one(
        "SELECT * FROM jobs WHERE job_id = ?", (alias_target or job_id,))
    if existing is not None:
        _check_source(existing, sender, channel)
    if alias_target and existing is not None and confirm:
        # 同一确认投递的重放：别名只在首次消费成功的事务里写入，
        # 此时令牌已耗尽——幂等返回目标任务当前结果，而不是 AUTH_FAIL
        return _job_response(app, existing, reused=True)
    if existing is None and confirm:
        # 确认是条新消息：它自己的消息 ID（job_id）不等于原任务 ID。
        # 无论调用方是否透传新 ID，令牌都是定位目标任务的授权凭据。
        hit = app.db.one(
            "SELECT p.job_id FROM pending p JOIN jobs j ON j.job_id = p.job_id "
            "WHERE p.token = ? AND p.expires_at > ? AND j.status = 'pending-confirm'",
            (confirm, time.time()),
        )
        if hit:
            existing = app.db.one("SELECT * FROM jobs WHERE job_id = ?", (hit["job_id"],))

    if existing is not None:
        _check_source(existing, sender, channel)
        status = existing["status"]
        if status == "pending-confirm" and confirm:
            # 确认回复的正文必然与原任务不同（如「确认 <token>」），
            # 授权凭据是令牌而非文本；先于内容摘要检查处理。
            # alias_id 记录确认消息自身的 ID，供对账/重投解析目标。
            return _consume_confirm(app, existing, channel, confirm, wait,
                                    alias_id=(payload.get("job_id") or "").strip())
        if status == "uncertain" and force:
            # force 重跑同理：重跑请求是新消息、正文不同，
            # 执行的是已存快照，先于内容摘要检查处理
            return _rerun_uncertain(app, existing, task, wait)
        # job_id 绑定任务内容：同 ID 换内容不是复用，是冲突
        if existing["task_digest"] and existing["task_digest"] != digest:
            raise AgentBridgeError(
                "EXEC_FAIL", "该 job_id 已绑定其他任务内容，拒绝复用；请更换 job_id")
        if status in ("done", "failed", "cancelled"):
            return _job_response(app, existing, reused=True)
        if status in ("queued", "running"):
            return _busy_response(existing)
        if status == "uncertain":
            if not force:
                return {
                    "ok": False, "code": "UNCERTAIN", "reused": True,
                    "message": ("该任务上次执行时进程中断，无法确定是否已产生副作用。"
                                "不会自动重跑；确认要重跑请显式带 force=1。"),
                    "job_id": job_id, "project": existing["project"],
                    "backend": existing["backend"], "status": "uncertain",
                }
            return _rerun_uncertain(app, existing, task, wait)
        if status == "pending-confirm":
            return _reissue_confirm(app, existing["job_id"], channel, digest)
        return _busy_response(existing)

    # 未登记的 job_id 不接受任何 confirm：确认必须来自已签发的 pending 记录
    if confirm:
        raise AgentBridgeError(
            "AUTH_FAIL", "确认令牌未签发或已失效；请先不带 confirm 发起任务获取令牌")

    kind, downgraded = effective_kind(app.db, target_project, task)
    proj = app.get_project(target_project)
    snap = _task_snapshot(job_id, channel, target_project, task, kind, downgraded, proj)
    snap["sender"] = sender

    if kind == "sensitive":
        return _await_confirm(app, snap, id_stable)
    return _start_fresh(app, snap, id_stable, wait)


class _ClaimAbort(Exception):
    """事务内认领失败的控制流信号（触发回滚），kind 区分原因。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _validate_admission(app: App, snap: dict) -> None:
    app._check_quarantine(snap["lock_key"], snap["cwd"])
    proj = app.db.one("SELECT normalized FROM projects WHERE name=?", (snap["project"],))
    if not proj or proj["normalized"] != snap["cwd"]:
        raise AgentBridgeError("PROJECT_NOT_FOUND", "项目已被删除或改绑，任务未接纳；请重新发起")


def _admit_and_enqueue(app: App, snap: dict, claim, sticky: bool = True) -> None:
    """接纳临界区：admit 锁内完成「隔离检查 → 项目复核 → 数据库认领 →
    取号 → 入队 → 粘性写」。

    claim(conn) 在数据库事务内执行认领语句；抛 _ClaimAbort 触发回滚，
    抛 sqlite3.IntegrityError 由调用方按主键冲突处理。项目注册/改绑/
    删除同样在 admit 锁内改 projects 表（锁序 projects → admit），
    因此「复核项目存在 + 认领」之间管理操作无法插入；粘性写也在锁内，
    后接纳的任务不会把路由覆盖回旧任务。
    """
    with app._admit_lock:
        _validate_admission(app, snap)
        with app.db.transaction() as conn:
            claim(conn)
        try:
            snap["seq"] = app.next_seq()
            snap["ticket"] = app.sched.resource_gate(snap["lock_key"]).enqueue(snap["seq"])
            if sticky and snap["channel"]:
                app.set_current_project(snap["channel"], snap["project"])
        except BaseException as exc:
            # 入队之后、派发之前的异常必须弃票并落终态，
            # 否则队首票据无人认领，同资源后继任务永久阻塞
            if snap.get("ticket") is not None:
                app.sched.resource_gate(snap["lock_key"]).abandon(snap["ticket"])
            # The caller still owns the reservation until _finish_acceptance.
            _fail_job(app, snap, f"接纳中断：{type(exc).__name__}")
            raise


def _claim_failure_response(app: App, job_id: str):
    """认领失败后按真实终态回报——cancelled/done/failed 不是 BUSY。"""
    row = app.db.one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if row and row["status"] in ("done", "failed", "cancelled", "uncertain"):
        return _job_response(app, row, reused=True)
    return _busy_response(row)


def _finish_acceptance(app: App, snap: dict, wait: bool,
                       audit_record: dict | None = None) -> dict:
    """派发收尾：审计 → 同步执行或后台线程。

    进入时票据已入队、等待位已预留（粘性写已在 admit 锁内完成）。
    任何一步失败（审计写、后台线程启动）都必须归还票据与等待位并
    落终态——否则同资源的后续任务会被无人持有的队首票据永久挡住。
    同步执行路径在调用 _run_claimed 前移交生命周期，避免双重释放。
    """
    handed_off = False
    try:
        if audit_record:
            app.audit.write(audit_record)
        if wait:
            handed_off = True  # 票据/等待位自此由 _run_claimed 负责
            return _run_claimed(app, snap)
        threading.Thread(target=_run_claimed, args=(app, snap), daemon=True).start()
        handed_off = True
        return {
            "ok": True, "code": "ACCEPTED", "job_id": snap["job_id"],
            "project": snap["project"], "backend": "dsh", "status": "queued",
            "message": "任务已接纳，正在排队执行；可随时以相同 job_id 查询结果。",
        }
    except BaseException as exc:
        if not handed_off:
            app.sched.resource_gate(snap["lock_key"]).abandon(snap["ticket"])
            app.sched.release_reservation()
            _fail_job(app, snap, f"派发失败：{type(exc).__name__}")
        raise


def _await_confirm(app: App, snap: dict, id_stable: bool) -> dict:
    """敏感任务：登记 pending-confirm（含快照）并签发一次性令牌。

    任务与令牌同一事务插入——分两次提交会在间隙暴露给 prune_data，
    出现「令牌有效但任务已被清理取消」的错配。
    """
    # 12 位十六进制（48 bit）：令牌直接出现在聊天正文里，留足猜测余量
    token = uuid.uuid4().hex[:12]
    try:
        with app._admit_lock:
            _validate_admission(app, snap)
            with app.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO jobs (job_id, channel, project, cwd, backend, task, status, "
                    "created_at, id_stable, task_digest, kind, lock_key, sandbox_mode,sender,exec_state) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'idle')",
                    (snap["job_id"], snap["channel"], snap["project"], snap["cwd"], "dsh",
                     snap["task"], "pending-confirm", time.time(), 1 if id_stable else 0,
                     snap["task_digest"], snap["kind"], snap["lock_key"], "danger-full-access", snap.get("sender", "")),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO pending (job_id, token, expires_at) VALUES (?,?,?)",
                    (snap["job_id"], token, time.time() + CONFIRM_TTL),
                )
            try:
                if snap["channel"]:
                    app.set_current_project(snap["channel"], snap["project"])
            except BaseException:
                with app.db.transaction() as conn:
                    conn.execute("DELETE FROM pending WHERE job_id=?", (snap["job_id"],))
                    conn.execute(
                        "UPDATE jobs SET status='failed',error_code='EXEC_FAIL',ended_at=? WHERE job_id=?",
                        (time.time(), snap["job_id"]))
                raise
    except sqlite3.IntegrityError:
        # 并发同 job_id：先核对内容绑定，再按在途处理
        row = app.db.one("SELECT * FROM jobs WHERE job_id = ?", (snap["job_id"],))
        if row and row["task_digest"] and row["task_digest"] != snap["task_digest"]:
            raise AgentBridgeError(
                "EXEC_FAIL", "该 job_id 已绑定其他任务内容，拒绝复用；请更换 job_id")
        return _busy_response(row, "该任务正被其他请求处理")
    app.audit.write({
        "job_id": snap["job_id"], "project": snap["project"], "channel": snap["channel"],
        "event": "pending-confirm", "kind": snap["kind"], "task": snap["task"],
    })
    return {
        "ok": False, "code": "NEED_CONFIRM", "token": token,
        "job_id": snap["job_id"], "project": snap["project"], "backend": "dsh",
        "message": (f"该操作可能造成较大影响，需要确认。\n"
                    f"回复「确认 {token}」继续，{CONFIRM_TTL // 60} 分钟内有效。"),
    }


def _reissue_confirm(app: App, job_id: str, channel: str, digest: str) -> dict:
    with app._admit_lock:
        with app.db.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise AgentBridgeError("EXEC_FAIL", "任务已清理，请重新发起")
            if row["task_digest"] and row["task_digest"] != digest:
                raise AgentBridgeError("EXEC_FAIL", "任务内容不匹配")
            if row["status"] == "pending-confirm":
                if (row["channel"] or "") != channel:
                    raise AgentBridgeError("AUTH_FAIL", "确认令牌只发给原始通道")
                token = uuid.uuid4().hex[:12]
                conn.execute(
                    "INSERT OR REPLACE INTO pending(job_id,token,expires_at) VALUES(?,?,?)",
                    (job_id, token, time.time() + CONFIRM_TTL))
            else:
                token = None
    if token is None:
        if row["status"] in ("queued", "running"):
            return _busy_response(row)
        return _job_response(app, row, reused=True)
    app.audit.write({"job_id": job_id, "project": row["project"],
                     "channel": channel, "event": "pending-reissue"})
    return {"ok": False, "code": "NEED_CONFIRM", "token": token,
            "job_id": job_id, "project": row["project"], "backend": "dsh",
            "message": (f"该操作仍在等待确认。\n回复「确认 {token}」继续，"
                        f"{CONFIRM_TTL // 60} 分钟内有效（重发原任务会刷新令牌）。")}


def _consume_confirm(app: App, existing, channel: str, confirm: str,
                     wait: bool = True, alias_id: str = "") -> dict:
    """消费确认令牌并执行。

    顺序至关重要：先占队列名额（满了抛 QUEUE_FULL，令牌完好可重试）；
    再在 admit 锁内的单个事务里「删令牌 + pending-confirm→queued +
    记录确认消息 ID→目标任务别名」——同锁同事务，清理线程无法插入，
    失败时回滚令牌也不被半消费。别名让确认响应丢失后的对账与
    幂等重投都能解析回目标任务。
    """
    if existing["channel"] and channel and existing["channel"] != channel:
        raise AgentBridgeError("AUTH_FAIL", "确认来源与任务发起来源不一致")
    app.sched.reserve()
    snap = _snap_from_row(existing)
    try:
        def claim(conn) -> None:
            cur = conn.execute(
                "DELETE FROM pending WHERE job_id = ? AND token = ? AND expires_at > ?",
                (existing["job_id"], confirm, time.time()),
            )
            if cur.rowcount != 1:
                raise _ClaimAbort("bad_token")
            cur = conn.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, ended_at=NULL, "
                "exit_code=NULL, result=NULL, result_digest=NULL, error_code=NULL, "
                "exec_pgid=NULL,exec_boot=NULL,exec_identity=NULL,exec_state='idle' "
                "WHERE job_id = ? AND status = 'pending-confirm'",
                (existing["job_id"],),
            )
            if cur.rowcount != 1:
                raise _ClaimAbort("state_changed")
            if alias_id and alias_id != existing["job_id"]:
                conn.execute(
                    "INSERT OR REPLACE INTO aliases (alias, job_id, created_at) "
                    "VALUES (?,?,?)",
                    (alias_id, existing["job_id"], time.time()),
                )

        _admit_and_enqueue(app, snap, claim, sticky=True)
    except _ClaimAbort as abort:
        app.sched.release_reservation()
        if abort.kind == "bad_token":
            raise AgentBridgeError("AUTH_FAIL", "确认令牌不匹配或已过期，请重新发起") from None
        if abort.kind == "project_changed":
            raise AgentBridgeError(
                "PROJECT_NOT_FOUND",
                "项目已被删除或改绑，确认未执行；请重新发起任务") from None
        # 状态已被并发改变：按真实终态回报，不包成 BUSY
        return _claim_failure_response(app, existing["job_id"])
    except BaseException:
        app.sched.release_reservation()
        raise
    return _finish_acceptance(app, snap, wait, audit_record={
        "job_id": snap["job_id"], "project": snap["project"], "channel": channel,
        "event": "confirm-accepted", "kind": snap["kind"], "task": snap["task"],
    })


def _start_fresh(app: App, snap: dict, id_stable: bool, wait: bool = True) -> dict:
    """新任务：INSERT 即原子接纳，主键冲突说明并发请求已接纳同 job_id。

    认领与取号入队在 admit 锁内完成（接纳顺序 = 执行顺序）。
    """
    app.sched.reserve()

    def claim(conn) -> None:
        conn.execute(
            "INSERT INTO jobs (job_id, channel, project, cwd, backend, task, status, "
            "created_at, sandbox_mode, id_stable, task_digest, kind, lock_key,sender,exec_state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'idle')",
            (snap["job_id"], snap["channel"], snap["project"], snap["cwd"], "dsh",
             snap["task"], "queued", time.time(), build_cmd(snap["task"], snap["kind"])[1],
             1 if id_stable else 0, snap["task_digest"], snap["kind"], snap["lock_key"], snap.get("sender", "")),
        )

    try:
        _admit_and_enqueue(app, snap, claim)
    except sqlite3.IntegrityError:
        app.sched.release_reservation()
        row = app.db.one("SELECT * FROM jobs WHERE job_id = ?", (snap["job_id"],))
        if row is not None:
            _check_source(row, snap.get("sender", ""), snap["channel"])
        # 冲突恢复分支同样执行内容绑定校验：同 ID 不同内容是冲突，不是复用
        if row and row["task_digest"] and row["task_digest"] != snap["task_digest"]:
            raise AgentBridgeError(
                "EXEC_FAIL", "该 job_id 已绑定其他任务内容，拒绝复用；请更换 job_id")
        if row and row["status"] in ("done", "failed", "cancelled"):
            return _job_response(app, row, reused=True)
        return _busy_response(row, "该任务正被其他请求处理")
    except _ClaimAbort as abort:
        app.sched.release_reservation()
        if abort.kind == "project_changed":
            raise AgentBridgeError(
                "PROJECT_NOT_FOUND",
                "项目已被删除或改绑，任务未接纳；请重新发起") from None
        raise
    except BaseException:
        app.sched.release_reservation()
        raise
    return _finish_acceptance(app, snap, wait)


def _rerun_uncertain(app: App, existing, task: str, wait: bool = True) -> dict:
    """force=1 重跑 uncertain 任务：原子认领 uncertain → queued。"""
    snap = _snap_from_row(existing)
    app.sched.reserve()
    try:
        def claim(conn) -> None:
            cur = conn.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, ended_at=NULL, "
                "exit_code=NULL, result=NULL, result_digest=NULL, error_code=NULL, "
                "exec_pgid=NULL,exec_boot=NULL,exec_identity=NULL,exec_state='idle' "
                "WHERE job_id = ? AND status = 'uncertain'",
                (snap["job_id"],),
            )
            if cur.rowcount != 1:
                raise _ClaimAbort("state_changed")

        _admit_and_enqueue(app, snap, claim, sticky=False)
    except _ClaimAbort as abort:
        app.sched.release_reservation()
        if abort.kind == "project_changed":
            raise AgentBridgeError(
                "PROJECT_NOT_FOUND",
                "项目已被删除或改绑，重跑未执行；请重新发起") from None
        return _claim_failure_response(app, snap["job_id"])
    except BaseException:
        app.sched.release_reservation()
        raise
    return _finish_acceptance(app, snap, wait, audit_record={
        "job_id": snap["job_id"], "project": snap["project"],
        "event": "uncertain-force-rerun", "task": task,
    })


def _run_claimed(app: App, snap: dict) -> dict:
    """执行已认领（status=queued）的任务。

    调用前必须已 sched.reserve() 且票据已入队；本函数负责等待位生命周期：
    要么由 begin_running 转为运行名额，要么在异常路径归还。
    """
    gate = app.sched.resource_gate(snap["lock_key"])
    ticket = snap.get("ticket")
    if ticket is None:
        ticket = gate.enqueue(snap.get("seq") or app.next_seq())
    entered = False
    began = False
    try:
        gate.enter(ticket)  # 同资源按接纳序号 FIFO
        entered = True
        app.sched.begin_running()
        began = True
        app.sched.mark_running(snap["job_id"], {
            "job_id": snap["job_id"], "project": snap["project"], "started": time.time(),
        })
        try:
            context = build_context(app.db, snap["project"])
            final_task = context + snap["task"] if context else snap["task"]
            # 运行时权限复检：接纳后同项目可能新增提权计划，它此刻会进入
            # 上下文。权限按「执行时的最新上下文」判定，比接纳快照更严格
            # 时立即降级——批准的只是当时的任务，不是后来混入的提权计划。
            kind = snap["kind"]
            downgraded = bool(snap.get("downgraded"))
            if kind != "needs_privilege":
                runtime_kind, _rd = effective_kind(app.db, snap["project"], snap["task"])
                if runtime_kind == "needs_privilege" or classify(context) == "needs_privilege":
                    kind = "needs_privilege"
                    downgraded = True
            if downgraded and kind == "needs_privilege":
                final_task = (
                    "（系统注：本会话近期包含提权任务，本任务按只读模式执行，"
                    "只产出计划与命令，不执行。）\n" + final_task
                )
            cmd, mode, _patch = build_cmd(final_task, kind)
            app.db.execute(
                "UPDATE jobs SET status='running', started_at=?, kind=?, sandbox_mode=?, "
                "exec_pgid=NULL,exec_identity=NULL,exec_boot=?,exec_state='starting' "
                "WHERE job_id=?",
                (time.time(), kind, mode, app.boot_id, snap["job_id"]),
            )
            boot = app.boot_id

            def _persist_executor(pgid: int) -> None:
                # 启动即持久化执行器身份：worker 崩溃后恢复据此对账
                identity = _executor_identity(pgid)
                if identity is None:
                    raise AgentBridgeError("EXEC_FAIL", "无法读取执行器启动身份，任务未启动")
                cur = app.db.execute(
                    "UPDATE jobs SET exec_pgid=?, exec_boot=?,exec_identity=?,exec_state='registered' "
                    "WHERE job_id=? AND status='running'",
                    (pgid, boot, identity, snap["job_id"]),
                )
                if cur.rowcount != 1:
                    raise AgentBridgeError("EXEC_FAIL", "执行器登记失败，任务未启动")

            res = run_task(cmd, snap["cwd"], TASK_TIMEOUT, on_start=_persist_executor)
            return _finalize(app, snap, res, mode)
        except Exception as exc:  # noqa: BLE001
            # 执行异常必须落终态，否则该 job_id 永远 BUSY
            _fail_job(app, snap, f"执行异常：{type(exc).__name__}")
            raise
        finally:
            app.sched.clear_running(snap["job_id"])
    finally:
        if entered:
            gate.release(ticket)
        else:
            gate.abandon(ticket)
        if began:
            app.sched.end_running()
        else:
            app.sched.release_reservation()


def _finalize(app: App, snap: dict, res: dict, mode: str) -> dict:
    out = (res.get("stdout") or "").strip()
    truncated = False
    if len(out) > OUTPUT_MAX:
        out = out[:OUTPUT_MAX]
        truncated = True
    out = redact(out)

    if res.get("timed_out"):
        status, code = "failed", "TIMEOUT"
        message = f"任务超时（超过 {TASK_TIMEOUT} 秒），已终止整个进程组。"
    elif res.get("exit_code") == 0 and out:
        status, code, message = "done", None, out
    elif res.get("exit_code") == 0 and not out:
        status, code, message = "done", None, "（没有产出内容）"
    else:
        status, code = "failed", "EXEC_FAIL"
        message = out or f"执行失败（退出码 {res.get('exit_code')}）"

    if truncated:
        # OUTPUT_MAX 按字符计（采集字节上限是另一档 CAPTURE_MAX）
        message += f"\n…（已截断，原长超过 {OUTPUT_MAX} 字符）"
    if res.get("out_truncated"):
        message += f"\n…（输出超过采集上限 {CAPTURE_MAX} 字节，超限部分未保留）"
    if res.get("group_reaped"):
        message += "\n（任务主进程退出后仍有同组后台进程存活，已在期限处回收整组。）"

    app.db.execute(
        "UPDATE jobs SET status=?, error_code=?, result=?, result_digest=?, ended_at=?, "
        "exit_code=?, peak_rss_mb=? WHERE job_id=?",
        (status, code, message, message[:200], time.time(), res.get("exit_code"),
         res.get("peak_rss_mb"), snap["job_id"]),
    )
    app.audit.write({
        "job_id": snap["job_id"], "project": snap["project"], "channel": snap["channel"],
        "backend": "dsh", "status": status, "error_code": code,
        "exit_code": res.get("exit_code"), "timed_out": bool(res.get("timed_out")),
        "peak_rss_mb": res.get("peak_rss_mb"), "sandbox_mode": mode,
        "task": snap["task"],
    })
    app.maybe_prune_data()
    return {
        "ok": status == "done", "code": code, "job_id": snap["job_id"],
        "project": snap["project"], "backend": "dsh", "status": status,
        "exit_code": res.get("exit_code"), "peak_rss_mb": res.get("peak_rss_mb"),
        "message": message,
    }


def _fail_job(app: App, snap: dict, message: str) -> None:
    try:
        app.db.execute(
            "UPDATE jobs SET status='failed', error_code='EXEC_FAIL', result=?, "
            "result_digest=?, ended_at=? WHERE job_id=?",
            (message[:OUTPUT_MAX], message[:200], time.time(), snap["job_id"]),
        )
        app.audit.write({
            "job_id": snap["job_id"], "project": snap["project"],
            "channel": snap["channel"], "status": "failed",
            "error_code": "EXEC_FAIL", "task": snap["task"],
        })
    except Exception:  # noqa: BLE001
        pass


def _job_response(app: App, row, reused: bool = False) -> dict:
    resp = {
        "ok": row["status"] == "done", "code": row["error_code"], "reused": reused,
        "job_id": row["job_id"], "project": row["project"], "backend": row["backend"],
        "status": row["status"], "exit_code": row["exit_code"],
        "peak_rss_mb": row["peak_rss_mb"], "message": row["result"] or "",
    }
    if row["status"] == "pending-confirm":
        # 查询/对账也必须给出完整业务状态：待确认任务带明确的
        # NEED_CONFIRM 码与取回令牌的指引，而不是空正文
        resp["ok"] = False
        resp["code"] = "NEED_CONFIRM"
        resp["message"] = ("任务等待确认；若未收到令牌，用相同 job_id 与正文"
                           "重发原任务将重新签发确认令牌。")
    return resp


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "agentbridge-worker"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:  # noqa: D102
        super().setup()
        # 读写时限：防慢速客户端长期占用线程；空闲 keep-alive 超时后自动断开
        try:
            self.connection.settimeout(HTTP_TIMEOUT)
        except OSError:
            pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not SECRET:
            return False
        got = self.headers.get("X-AgentBridge-Secret") or ""
        ok = hmac.compare_digest(got.encode("utf-8"), SECRET.encode("utf-8"))
        if not ok:
            # 认证失败时请求体未读，必须关闭连接而非复用
            self.close_connection = True
        return ok

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise AgentBridgeError("EXEC_FAIL", "Content-Length 非法") from None
        if n <= 0 or n > BODY_MAX:
            raise AgentBridgeError("EXEC_FAIL", "请求体为空或过大")
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
        except Exception:
            raise AgentBridgeError("EXEC_FAIL", "请求体不是合法 JSON") from None
        if not isinstance(data, dict):
            raise AgentBridgeError("EXEC_FAIL", "请求体必须是对象")
        return data

    def do_GET(self) -> None:  # noqa: N802
        if not self._authed():
            return self._json(401, {"ok": False, "code": "AUTH_FAIL", "message": "unauthorized"})
        if self.path == "/health":
            return self._json(200, {"ok": True, "sandbox_modes": APP.sandbox_modes})
        if self.path.startswith("/job"):
            # 单任务查询：shim 接纳后轮询取结果（wait=false 协作模式）
            from urllib.parse import parse_qs
            query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            job_id = (query.get("id") or [""])[0].strip()
            if not job_id:
                return self._json(404, {"ok": False, "code": "EXEC_FAIL",
                                        "message": "缺少 id 参数"})
            result = APP.get_job(job_id)
            if result is None:
                return self._json(404, {"ok": False, "code": "EXEC_FAIL",
                                        "message": "任务不存在"})
            return self._json(200, result)
        if self.path.startswith("/status"):
            channel = ""
            if "?" in self.path:
                from urllib.parse import parse_qs
                channel = (parse_qs(self.path.split("?", 1)[1]).get("channel") or [""])[0]
            return self._json(200, {
                "ok": True,
                "current": APP.current_project(channel),
                "scheduler": APP.sched.snapshot(),
                "projects": APP.list_projects(channel)["projects"],
            })
        return self._json(404, {"ok": False, "code": "EXEC_FAIL", "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authed():
            return self._json(401, {"ok": False, "code": "AUTH_FAIL", "message": "unauthorized"})
        try:
            # 内部协议：一切元数据（channel/project/job_id/confirm/id_stable）
            # 都在 JSON body 里，不读取任何 X-AgentBridge-* 转发头
            payload = self._body()
            route = self.path.rstrip("/")
            if route == "/execute":
                if _as_bool(payload.get("force")):
                    return self._json(200, do_control(APP, {
                        "op": "force", "sender": payload.get("sender"), "channel": payload.get("channel"),
                        "job_id": payload.get("job_id"), "delivery_id": payload.get("control_delivery_id")}))
                return self._json(200, do_execute(APP, payload))
            if route == "/control":
                return self._json(200, do_control(APP, payload))
            legacy_controls = {"/project/add": "project.add", "/project/rm": "project.rm",
                               "/project/list": "project.list", "/channel/use": "use"}
            if route in legacy_controls:
                return self._json(200, do_control(APP, {**payload, "op": legacy_controls[route]}))
            self.close_connection = True
            return self._json(404, {"ok": False, "code": "EXEC_FAIL", "message": "not found"})
        except AgentBridgeError as exc:
            # 出错路径可能未读完请求体，统一关闭连接，避免复用残留字节
            self.close_connection = True
            status = 401 if exc.code == "AUTH_FAIL" else 200
            return self._json(status, {"ok": False, "code": exc.code,
                                       "message": exc.message, **exc.extra})
        except Exception as exc:  # noqa: BLE001
            self.close_connection = True
            return self._json(500, {"ok": False, "code": "EXEC_FAIL",
                                    "message": f"内部错误：{type(exc).__name__}"})

    def log_message(self, fmt, *args):  # noqa: D102
        pass  # 不记录请求内容


def main() -> None:
    global APP  # noqa: PLW0603
    if not SECRET:
        print("AGENTBRIDGE_SECRET 未设置，拒绝启动", file=sys.stderr)
        raise SystemExit(2)
    try:
        APP = App()
    except AgentBridgeError as exc:
        print(f"启动失败：{exc.code} {exc.message}", file=sys.stderr)
        raise SystemExit(3) from exc
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"agentbridge-worker 监听 127.0.0.1:{PORT}，沙箱={APP.sandbox_modes}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
