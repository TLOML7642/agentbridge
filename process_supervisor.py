"""Owned process supervision shared by chat and legacy tasks. No worker startup/config."""
from __future__ import annotations
import ctypes
import os
import re
import signal
import subprocess
import sys
import threading
import time

class ProcessStartError(RuntimeError):
    pass

_SECRET_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 [REDACTED]"),
]

def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


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
    return _process_identity(pgid, pgid)


def _process_identity(pid: int, pgid: int) -> str | None:
    try:
        if sys.platform == "darwin":
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            query = libproc.proc_pidinfo
            query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]
            query.restype = ctypes.c_int
            info = _ProcBSDInfo()
            size = ctypes.sizeof(info)
            if query(pid, 3, 0, ctypes.byref(info), size) != size:
                return None
            if info.pid != pid or info.pgid != pgid:
                return None
            return f"darwin:{pid}:{info.start_sec}:{info.start_usec}"
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            if int(fields[2]) != pgid:  # field 5: process group
                return None
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
                boot = fh.read().strip()
            return f"linux:{boot}:{pid}:{fields[19]}"  # field 22: start ticks
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return None


def _group_members(pgid: int) -> dict:
    try:
        if sys.platform.startswith("linux"):
            # Minimal Linux images need no ps binary. Each /proc birth identity
            # still validates the kernel process group; races fail closed.
            members = {}
            for name in os.listdir("/proc"):
                if name.isascii() and name.isdecimal():
                    pid = int(name)
                    identity = _process_identity(pid, pgid)
                    if identity:
                        members[pid] = identity
            return members
        output = subprocess.check_output(["ps", "-axo", "pid=,pgid="], text=True, timeout=2)
        members = {}
        for line in output.splitlines():
            pid, group = map(int, line.split())
            if group == pgid:
                identity = _process_identity(pid, pgid)
                if identity:
                    members[pid] = identity
        return members
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _stop_owned_group(pgid: int, members: dict) -> bool:
    """Only the executor supervisor signals a still-identifiable owned group."""
    for sig, seconds in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        if not _group_alive(pgid):
            return True
        current = _group_members(pgid)
        if not any(current.get(pid) == identity for pid, identity in members.items()):
            return False
        members.update(current)
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return True
            time.sleep(0.05)
    return not _group_alive(pgid)


class _CancelledStart(Exception):
    pass


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


def run_task(cmd: list[str], cwd: str, timeout: int, on_start=None,
             on_release=None, cancel_check=None, *, capture_max=32768, stderr_max=8192, error_type=ProcessStartError) -> dict:
    """执行子进程，超时回收整个进程组，返回有界输出与峰值内存。

    采集上限：stdout 保留至多 CAPTURE_MAX 字节、stderr 至多 STDERR_MAX
    字节；超限后继续排空管道（避免子进程写阻塞）但丢弃内容。因此
    OUTPUT_MAX 只是「回传截断」，CAPTURE_MAX 才是内存/磁盘边界。

    已知限制：主动 setsid 脱离进程组的后代无法通过 killpg 回收。
    """
    capture_out = capture_max
    capture_err = stderr_max
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
        raise error_type("EXEC_FAIL", "无法启动后端") from exc
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
    owner_identity = _executor_identity(pgid)
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
        if on_release is None:
            os.write(gate_write, b"1")
        else:
            on_release(lambda: os.write(gate_write, b"1"))
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
    timed_out = False
    group_reaped = False
    cancelled = False
    group_stopped = True
    if cancel_check is not None:
        members = {pgid: owner_identity} if owner_identity is not None else {}
        while True:
            if cancel_check():
                cancelled = True
                group_stopped = _stop_owned_group(pgid, members)
                waiter.join(2)
                break
            if not waiter.is_alive() and not _group_alive(pgid):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                group_stopped = _stop_owned_group(pgid, members)
                waiter.join(2)
                break
            # Keep identities of descendants while the registered group is still owned.
            current = _group_members(pgid)
            if any(current.get(pid) == identity for pid, identity in members.items()):
                members.update(current)
            time.sleep(0.05)
    else:
        waiter.join(timeout)
    if cancel_check is None and waiter.is_alive():
        timed_out = True
        _killpg(pgid, signal.SIGTERM)
        waiter.join(5)
        # 无论直接父进程是否已退出，都探测并强杀整个组：
        # 父进程先死、后代忽略 SIGTERM 时，waiter 已返回但组仍存活。
        try:
            os.killpg(pgid, 0)
            _killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        waiter.join(5)
    elif cancel_check is None:
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
        "cancelled": cancelled,
        "group_stopped": group_stopped,
    }
