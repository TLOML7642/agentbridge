#!/usr/bin/env python3
"""worker 核心逻辑单元测试。

原有覆盖（方案第 2 步验收四项）：
  1. 超时后进程组被完整回收（不是只杀父进程）
  2. 重复 job_id 不重复执行
  3. 锁键等价性（符号链接、大小写、祖先/后代、非祖先前缀）
  4. 各错误码可触发

新增回归测试对应 2026-10-24 审计的 15 项缺陷（F1-F15），
每项缺陷修复后都应有对应的防回归用例。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="agentbridge-test-")
os.environ["AGENTBRIDGE_BASE"] = TMP
os.environ["AGENTBRIDGE_DB"] = os.path.join(TMP, "test.db")
os.environ["AGENTBRIDGE_AUDIT_DIR"] = os.path.join(TMP, "audit")
os.environ["AGENTBRIDGE_SECRET"] = "test-secret"
os.environ["AGENTBRIDGE_PATCH_FULL"] = os.path.join(TMP, "full.yml")
os.environ["AGENTBRIDGE_PATCH_RO"] = os.path.join(TMP, "ro.yml")
os.environ["AGENTBRIDGE_TASK_TIMEOUT"] = "3"
os.environ["AGENTBRIDGE_CONCURRENCY"] = "1"
os.environ["AGENTBRIDGE_QUEUE_MAX"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import worker  # noqa: E402

COUNTER = os.path.join(TMP, "calls.txt")


def _fake_dsh(path: str, body: str) -> str:
    p = Path(path)
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_result(*args, **kwargs):
    return {"stdout": "synthetic-ok", "stderr": "", "exit_code": 0,
            "timed_out": False, "peak_rss_mb": 1}


class TestPathAndLock(unittest.TestCase):
    def test_symlink_shares_lock_key(self):
        real = os.path.join(TMP, "projA")
        os.makedirs(real, exist_ok=True)
        link = os.path.join(TMP, "projA-link")
        if not os.path.islink(link):
            os.symlink(real, link)
        a = worker.normalize_dir(real)
        b = worker.normalize_dir(link)
        self.assertEqual(a, b, "符号链接应解析到同一真实目录")
        self.assertEqual(worker.lock_key_for(a), worker.lock_key_for(b))

    def test_case_insensitive_normalization(self):
        d = os.path.join(TMP, "ProjCase")
        os.makedirs(d, exist_ok=True)
        a = worker.normalize_dir(d)
        self.assertEqual(a, a.lower(), "规范化结果应小写归一")

    def test_ancestor_overlap(self):
        a = os.path.join(TMP, "x").lower()
        b = os.path.join(TMP, "x", "y").lower()
        self.assertTrue(worker.paths_overlap(a, b), "/a 与 /a/b 应判定重叠")

    def test_sibling_prefix_not_overlap(self):
        a = os.path.join(TMP, "x").lower()
        b = os.path.join(TMP, "xy").lower()
        self.assertFalse(worker.paths_overlap(a, b), "/a 与 /ab 不应判定重叠")

    def test_missing_dir_raises(self):
        with self.assertRaises(worker.AgentBridgeError) as ctx:
            worker.normalize_dir(os.path.join(TMP, "nope-not-here"))
        self.assertEqual(ctx.exception.code, "PROJECT_NOT_FOUND")


class TestClassifyAndRedact(unittest.TestCase):
    def test_privilege(self):
        for t in ("sudo rm -rf /tmp/x", "launchctl unload a.plist",
                  "改一下系统设置的防火墙", "写入 /etc/hosts"):
            self.assertEqual(worker.classify(t), "needs_privilege", t)

    def test_sensitive(self):
        for t in ("rm -rf ./build", "git reset --hard", "把这个目录全部删掉"):
            self.assertEqual(worker.classify(t), "sensitive", t)

    def test_normal(self):
        for t in ("看一下当前目录有什么", "磁盘还剩多少", "df -h"):
            self.assertEqual(worker.classify(t), "normal", t)

    def test_redact_api_key(self):
        out = worker.redact("key=sk-abcdefghijklmnop1234")
        self.assertNotIn("sk-abcdefghijklmnop1234", out)
        self.assertIn("[REDACTED_API_KEY]", out)

    def test_redact_password(self):
        out = worker.redact("password: hunter2")
        self.assertNotIn("hunter2", out)

    def test_redact_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        self.assertIn("[REDACTED_PRIVATE_KEY]", worker.redact(text))


class TestProcessGroupKill(unittest.TestCase):
    def test_timeout_kills_group_not_only_parent(self):
        """父进程被 SIGTERM 后，后台子进程也必须一起死。

        子进程 5 秒后创建标记文件；父进程 sleep 30。
        超时 3 秒回收整组后，等待 6 秒，标记文件不应出现。
        """
        marker = os.path.join(TMP, "child_survived.flag")
        script = _fake_dsh(
            os.path.join(TMP, "fake_dsh_slow.sh"),
            f"( sleep 5; touch {marker} ) &\nsleep 30\n",
        )
        started = time.time()
        res = worker.run_task([script], TMP, timeout=2)
        elapsed = time.time() - started

        self.assertTrue(res["timed_out"], "应报告超时")
        self.assertLess(elapsed, 12, "回收应迅速完成")

        time.sleep(6)
        self.assertFalse(
            os.path.exists(marker),
            "后台子进程存活并创建了标记文件 → 进程组未被完整回收",
        )

    def test_timeout_kills_term_ignoring_descendant(self):
        """F7：父进程先退出、后代忽略 SIGTERM 时，整组仍必须被强杀。"""
        ready = os.path.join(TMP, "term_ready")
        survived = os.path.join(TMP, "term_survived")
        descendant = (
            "import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"pathlib.Path({ready!r}).write_text('ready'); time.sleep(1); "
            f"pathlib.Path({survived!r}).write_text('survived')"
        )
        parent = (
            "import subprocess,sys,pathlib,time; "
            f"subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
            f"p=pathlib.Path({ready!r}); "
            "\nwhile not p.exists(): time.sleep(0.01)\n"
            "time.sleep(5)"
        )
        res = worker.run_task([sys.executable, "-c", parent], TMP, timeout=0.4)
        time.sleep(1.2)
        self.assertTrue(res["timed_out"])
        self.assertFalse(
            os.path.exists(survived),
            "忽略 SIGTERM 的后代在父进程退出后仍存活并写了标记 → 回收不完整",
        )

    def test_normal_completion(self):
        script = _fake_dsh(os.path.join(TMP, "fake_dsh_ok.sh"), "echo hello-agentbridge\n")
        res = worker.run_task([script], TMP, timeout=10)
        self.assertFalse(res["timed_out"])
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("hello-agentbridge", res["stdout"])

    def test_output_capture_is_bounded(self):
        """F10：采集必须有硬上限，不能先无限收再截断。"""
        with mock.patch.object(worker, "CAPTURE_MAX", 1024), \
                mock.patch.object(worker, "STDERR_MAX", 256):
            res = worker.run_task(
                [sys.executable, "-c", 'print("x" * 2_000_000); '
                 'import sys; sys.stderr.write("e" * 500)'],
                TMP, timeout=10)
        self.assertLessEqual(len(res["stdout"].encode()), 1024,
                             "stdout 采集超过 CAPTURE_MAX")
        self.assertLessEqual(len(res["stderr"].encode()), 256,
                             "stderr 采集超过 STDERR_MAX")
        self.assertTrue(res["out_truncated"], "超限应标记 out_truncated")
        self.assertTrue(res["err_truncated"], "超限应标记 err_truncated")


class TestDedupeAndErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 假后端：每次被调用就往计数器追加一行
        cls.dsh = _fake_dsh(
            os.path.join(TMP, "fake_dsh_count.sh"),
            f'echo call >> {COUNTER}\necho "done-ok"\n',
        )
        worker.DSH = cls.dsh
        worker.TASK_TIMEOUT = 10
        cls.app = worker.App(verify=False)

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()

    def setUp(self):
        # 保证每个用例的排队计数是干净的，避免用例间相互污染
        self.app.sched._waiting = 0

    def test_duplicate_job_id_executes_once(self):
        if os.path.exists(COUNTER):
            os.unlink(COUNTER)
        payload = {"task": "看一下当前目录", "job_id": "job-dup-1",
                   "channel": "wx:me", "project": "default", "id_stable": True}
        r1 = worker.do_execute(self.app, dict(payload))
        r2 = worker.do_execute(self.app, dict(payload))

        self.assertTrue(r1["ok"], r1)
        self.assertTrue(r2.get("reused"), "第二次应标记为复用")
        calls = Path(COUNTER).read_text().count("call")
        self.assertEqual(calls, 1, f"重复 job_id 被执行了 {calls} 次，应为 1 次")

    def test_sensitive_needs_confirm_then_runs(self):
        token_res = worker.do_execute(self.app, {
            "task": "rm -rf ./somedir", "job_id": "job-conf-1",
            "channel": "wx:me", "project": "default", "id_stable": True})
        self.assertEqual(token_res["code"], "NEED_CONFIRM")
        token = token_res["token"]

        # 错误令牌
        with self.assertRaises(worker.AgentBridgeError) as ctx:
            worker.do_execute(self.app, {
                "task": "rm -rf ./somedir", "job_id": "job-conf-1",
                "channel": "wx:me", "confirm": "wrong0"})
        self.assertEqual(ctx.exception.code, "AUTH_FAIL")

        ok_res = worker.do_execute(self.app, {
            "task": "rm -rf ./somedir", "job_id": "job-conf-1",
            "channel": "wx:me", "confirm": token})
        self.assertEqual(ok_res["status"], "done", ok_res)

    def test_privilege_task_uses_readonly_patch(self):
        cmd, mode, patch = worker.build_cmd("sudo reboot", "needs_privilege")
        self.assertEqual(mode, "read-only")
        self.assertIn(patch, cmd)
        self.assertIn("只读权限", cmd[-1])

    def test_unknown_project(self):
        with self.assertRaises(worker.AgentBridgeError) as ctx:
            worker.do_execute(self.app, {
                "task": "hi", "project": "ghost", "channel": "wx:me"})
        self.assertEqual(ctx.exception.code, "PROJECT_NOT_FOUND")

    def test_queue_full(self):
        """并发 1、排队 1 时，等待位占满后新任务应收到 QUEUE_FULL。"""
        self.app.sched.reserve()  # 占满唯一等待位
        try:
            with self.assertRaises(worker.AgentBridgeError) as ctx:
                worker.do_execute(self.app, {"task": "hi", "channel": "wx:me"})
            self.assertEqual(ctx.exception.code, "QUEUE_FULL")
        finally:
            self.app.sched.release_reservation()

    def test_reservation_released_when_running(self):
        """进入运行态后必须归还等待位，否则长任务会长期占用排队额度。"""
        slow = _fake_dsh(os.path.join(TMP, "fake_dsh_slow2.sh"), "sleep 2\necho slow-ok\n")
        old = worker.DSH
        worker.DSH = slow
        try:
            t = threading.Thread(target=lambda: worker.do_execute(
                self.app, {"task": "看一下磁盘占用", "channel": "wx:rel", "id_stable": True}))
            t.start()
            time.sleep(0.8)  # 等它进入 running
            self.assertEqual(self.app.sched.snapshot()["waiting"], 0,
                             "运行中的任务不应占用等待位")
            # 等待位空闲，说明还能再接纳一个排队任务
            self.app.sched.reserve()
            self.app.sched.release_reservation()
            t.join(timeout=20)
            self.assertFalse(t.is_alive(), "任务未在预期时间内结束")
        finally:
            worker.DSH = old
            self.app.sched._waiting = 0

    def test_project_overlap_rejected(self):
        d1 = os.path.join(TMP, "overlap1")
        d2 = os.path.join(TMP, "overlap1", "sub")
        os.makedirs(d2, exist_ok=True)
        self.app.add_project("ov1", d1)
        with self.assertRaises(worker.AgentBridgeError) as ctx:
            self.app.add_project("ov2", d2)
        self.assertEqual(ctx.exception.code, "EXEC_FAIL")

    def test_unknown_project_rejected(self):
        with self.assertRaises(worker.AgentBridgeError) as ctx:
            self.app.add_project("ghostproj", os.path.join(TMP, "does-not-exist"))
        self.assertEqual(ctx.exception.code, "PROJECT_NOT_FOUND")

    def test_sticky_project_switches_on_accept(self):
        d = os.path.join(TMP, "sticky")
        os.makedirs(d, exist_ok=True)
        self.app.add_project("sticky", d)
        worker.do_execute(self.app, {"task": "看一下", "channel": "wx:sticky",
                                     "project": "sticky", "id_stable": True})
        self.assertEqual(self.app.current_project("wx:sticky"), "sticky")


class TestAuditRegressions(unittest.TestCase):
    """对应审计 REVIEW.md F3-F14 的防回归用例（每项一个可复现行为）。"""

    def setUp(self):
        self.app = worker.App(verify=False)
        self.app.sched._waiting = 0

    def tearDown(self):
        self.app.sched._waiting = 0
        self.app.db.close()

    # ---- F3：任意非空 confirm 不能绕过首次确认 ----

    def test_fresh_job_with_unissued_confirm_rejected(self):
        with mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            with self.assertRaises(worker.AgentBridgeError) as ctx:
                worker.do_execute(self.app, {
                    "task": "删除临时测试文件", "job_id": "confirm-bypass",
                    "channel": "owner", "confirm": "not-issued"})
        self.assertEqual(ctx.exception.code, "AUTH_FAIL")
        self.assertEqual(execute.call_count, 0, "未签发的确认不得触发执行")

        # 普通任务带 confirm 同样拒绝
        with self.assertRaises(worker.AgentBridgeError) as ctx2:
            worker.do_execute(self.app, {
                "task": "看一下磁盘", "job_id": "confirm-bypass-2",
                "confirm": "anything"})
        self.assertEqual(ctx2.exception.code, "AUTH_FAIL")

    # ---- F4：并发同 job_id 只执行一次；pending 重放不炸库；内容绑定 ----

    def test_concurrent_same_job_executes_once(self):
        self.app.sched._queue_max = 8
        barrier = threading.Barrier(2)
        seen: list = []
        original = self.app.db.one

        def both_observe_missing(sql, args=()):
            result = original(sql, args)
            # 只让两条线程的首次 existing 检查在此会合（之后如插入冲突
            # 后的重读直接放行），制造「同时读到不存在」的竞争窗口
            if sql == "SELECT * FROM jobs WHERE job_id = ?" and args == ("racing-job",):
                seen.append(1)
                if len(seen) <= 2:
                    barrier.wait(timeout=10)
            return result

        payload = {"task": "concurrent-task", "job_id": "racing-job", "channel": "owner"}
        with mock.patch.object(self.app.db, "one", side_effect=both_observe_missing), \
                mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker.do_execute, self.app, dict(payload))
                           for _ in range(2)]
                results = [f.result(timeout=15) for f in futures]
        self.assertEqual(execute.call_count, 1,
                         f"并发请求执行了 {execute.call_count} 次，应为 1 次")
        self.assertTrue(all(r["job_id"] == "racing-job" for r in results))
        self.assertTrue(all(r.get("ok") or r.get("code") == "BUSY" for r in results))

    def test_pending_confirm_replay_returns_need_confirm(self):
        payload = {"task": "删除临时测试文件", "job_id": "repeat-pending",
                   "channel": "owner"}
        first = worker.do_execute(self.app, dict(payload))
        self.assertEqual(first["code"], "NEED_CONFIRM")
        # 原样重放（不带 confirm）不得抛 IntegrityError，应返回交互态
        second = worker.do_execute(self.app, dict(payload))
        self.assertEqual(second["code"], "NEED_CONFIRM")

    def test_job_id_binds_task_content(self):
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            worker.do_execute(self.app, {"task": "original-task", "job_id": "same-id"})
            with self.assertRaises(worker.AgentBridgeError) as ctx:
                worker.do_execute(self.app, {"task": "different-task", "job_id": "same-id"})
        self.assertEqual(ctx.exception.code, "EXEC_FAIL",
                         "同 job_id 换任务内容必须拒绝，不得静默复用旧结果")

    def test_confirm_token_single_use(self):
        first = worker.do_execute(self.app, {
            "task": "rm -rf ./x1", "job_id": "single-use", "channel": "owner"})
        token = first["token"]
        # 两条确认线程都要能通过队列接纳，竞争才发生在令牌消费上
        self.app.sched._queue_max = 8
        # 两条线程同时确认：DELETE rowcount 决定唯一赢家
        barrier = threading.Barrier(2)
        seen: list = []
        original = self.app.db.one

        def sync_first(sql, args=()):
            result = original(sql, args)
            if sql == "SELECT * FROM jobs WHERE job_id = ?" and args == ("single-use",):
                seen.append(1)
                if len(seen) <= 2:
                    barrier.wait(timeout=10)
            return result

        def attempt():
            try:
                return ("ok", worker.do_execute(self.app, {
                    "task": "rm -rf ./x1", "job_id": "single-use",
                    "channel": "owner", "confirm": token}))
            except worker.AgentBridgeError as exc:
                return ("err", exc)

        with mock.patch.object(self.app.db, "one", side_effect=sync_first), \
                mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(attempt) for _ in range(2)]
                outcomes = [f.result(timeout=15) for f in futures]
        errs = [o for o in outcomes if o[0] == "err"]
        oks = [o for o in outcomes if o[0] == "ok"]
        self.assertEqual(len(errs), 1, f"应恰有一次确认被拒绝：{outcomes}")
        self.assertEqual(errs[0][1].code, "AUTH_FAIL", "令牌必须一次性消费")
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(oks, "赢家应正常执行")

    # ---- F5：default=HOME 不再堵死子目录项目 ----

    def test_default_home_allows_subdir_project(self):
        home = os.path.join(TMP, "fake-home")
        sub = os.path.join(home, "project-a")
        os.makedirs(sub, exist_ok=True)
        with mock.patch.object(worker, "HOME", home), \
                mock.patch.object(worker, "DB_PATH", os.path.join(TMP, "home.db")), \
                mock.patch.object(worker, "AUDIT_DIR", os.path.join(TMP, "home-audit")):
            app = worker.App(verify=False)
            try:
                res = app.add_project("project-a", sub)
                self.assertTrue(res["ok"], "HOME 子目录项目应可注册")
                sibling = os.path.join(home, "project-b")
                os.makedirs(sibling, exist_ok=True)
                self.assertTrue(app.add_project("project-b", sibling)["ok"])
            finally:
                app.db.close()

    def test_default_project_cannot_be_overwritten(self):
        with self.assertRaises(worker.AgentBridgeError):
            self.app.add_project("default", TMP)

    # ---- F6：确认只授权接纳时的目录快照；忙碌时禁止改绑 ----

    def test_confirm_executes_in_pinned_cwd(self):
        before = os.path.join(TMP, "before")
        after = os.path.join(TMP, "after")
        os.makedirs(before, exist_ok=True)
        os.makedirs(after, exist_ok=True)
        self.app.add_project("rebind-proj", before)
        pending = worker.do_execute(self.app, {
            "task": "删除临时测试文件", "job_id": "rebound-confirm",
            "channel": "owner", "project": "rebind-proj"})
        pinned = self.app.db.one(
            "SELECT cwd FROM jobs WHERE job_id=?", ("rebound-confirm",))["cwd"]
        # 待确认期间禁止改绑
        with self.assertRaises(worker.AgentBridgeError) as ctx:
            self.app.add_project("rebind-proj", after)
        self.assertEqual(ctx.exception.code, "BUSY")
        with mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            worker.do_execute(self.app, {
                "task": "删除临时测试文件", "job_id": "rebound-confirm",
                "channel": "owner", "confirm": pending["token"]})
        actual = execute.call_args.args[1]
        self.assertEqual(actual, pinned, "确认执行必须使用接纳时固化的目录")

    # ---- F8：重启恢复 + 执行异常落终态 + force 重跑 ----

    def test_restart_marks_stale_running_uncertain(self):
        restart_db = os.path.join(TMP, "restart.db")
        with mock.patch.object(worker, "DB_PATH", restart_db), \
                mock.patch.object(worker, "AUDIT_DIR", os.path.join(TMP, "restart-audit")):
            first = worker.App(verify=False)
            row = first.db.one("SELECT normalized, lock_key FROM projects WHERE name='default'")
            first.db.execute(
                "INSERT INTO jobs(job_id,task,status,created_at,cwd,lock_key,kind,project) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("old-running", "stale-task", "running", time.time() - 3600,
                 row["normalized"], row["lock_key"], "normal", "default"))
            # The gated launch has not yet registered/released a backend.
            first.db.execute("UPDATE jobs SET exec_state='starting' WHERE job_id='old-running'")
            first.db.close()
            restarted = worker.App(verify=False)
            try:
                status = restarted.db.one(
                    "SELECT status, error_code FROM jobs WHERE job_id=?",
                    ("old-running",))
                self.assertEqual(status["status"], "uncertain",
                                 "重启后无执行者的任务应转为 uncertain，不能永久 BUSY")
                self.assertEqual(status["error_code"], "INTERRUPTED")
                res = worker.do_execute(restarted, {
                    "task": "stale-task", "job_id": "old-running"})
                self.assertEqual(res["code"], "UNCERTAIN")
                with mock.patch.object(worker, "run_task", side_effect=_fake_result):
                    forced = worker.do_execute(restarted, {
                        "task": "stale-task", "job_id": "old-running", "force": 1})
                self.assertEqual(forced["status"], "done", "force=1 应可重跑 uncertain")
            finally:
                restarted.db.close()

    def test_execute_exception_writes_terminal_state(self):
        with mock.patch.object(worker, "run_task", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                worker.do_execute(self.app, {
                    "task": "boom-task", "job_id": "boom-job", "channel": "owner"})
        row = self.app.db.one(
            "SELECT status, error_code FROM jobs WHERE job_id=?", ("boom-job",))
        self.assertEqual(row["status"], "failed", "执行异常必须写终态，避免永久 BUSY")
        self.assertEqual(row["error_code"], "EXEC_FAIL")
        self.assertEqual(self.app.sched.snapshot()["waiting"], 0,
                         "异常路径必须归还等待位")

    # ---- F9：提权计划不得顺流进全权限执行 ----

    def test_privileged_history_downgrades_followup(self):
        with mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            worker.do_execute(self.app, {"task": "sudo synthetic-command",
                                         "job_id": "plan", "channel": "owner"})
            worker.do_execute(self.app, {"task": "照刚才的方案执行",
                                         "job_id": "followup", "channel": "owner"})
        command = execute.call_args.args[0]
        self.assertIn(worker.PATCH_RO, command,
                      "含提权计划会话的后续任务必须走只读 patch")
        self.assertNotIn(worker.PATCH_FULL, command)
        row = self.app.db.one(
            "SELECT kind FROM jobs WHERE job_id=?", ("followup",))
        self.assertEqual(row["kind"], "needs_privilege")

    # ---- F11：数据库文件权限私有 ----

    def test_database_file_created_private(self):
        previous = os.umask(0o022)
        database = os.path.join(TMP, "private-dir", "private.db")
        try:
            db = worker.DB(database)
            db.close()
        finally:
            os.umask(previous)
        self.assertEqual(os.stat(database).st_mode & 0o777, 0o600,
                         "数据库文件必须 0600，不随 umask 变成 0644")
        parent = os.path.dirname(database)
        self.assertEqual(os.stat(parent).st_mode & 0o777, 0o700)

    # ---- F12：白名单模式下缺失通道必须拒绝 ----

    def test_allowlist_rejects_missing_channel(self):
        with mock.patch.object(worker, "ALLOWED_SENDERS", {"trusted-channel"}):
            with self.assertRaises(worker.AgentBridgeError) as ctx:
                worker.do_execute(self.app, {"task": "synthetic-task",
                                             "job_id": "no-channel"})
        self.assertEqual(ctx.exception.code, "AUTH_FAIL",
                         "配置白名单后缺失 channel 不得放行")

    # ---- F14/R5：同资源按接纳序号 FIFO ----

    def test_resource_gate_is_fifo(self):
        gate = worker.ResourceGate()
        holder = gate.enqueue(0)
        order = []

        def run(tag, seq):
            ticket = gate.enqueue(seq)
            gate.enter(ticket)
            try:
                order.append(tag)
            finally:
                gate.release(ticket)

        t1 = threading.Thread(target=run, args=(1, 1))
        t1.start()
        # 确保 1 号先入队（持有者未释放，它会阻塞在 enter）
        deadline = time.time() + 2
        while time.time() < deadline:
            with gate._cv:
                if len(gate._waiters) == 1:
                    break
            time.sleep(0.01)
        t2 = threading.Thread(target=run, args=(2, 2))
        t2.start()
        time.sleep(0.2)  # 2 号也入队
        gate.release(holder)
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertEqual(order, [1, 2], "同资源的任务必须按接纳顺序执行")

    def test_resource_gate_orders_by_seq_not_arrival(self):
        """晚到但序号更小的票据必须插队（接纳顺序优先于线程到达顺序）。"""
        gate = worker.ResourceGate()
        holder = gate.enqueue(0)
        gate.enter(holder)  # 占住持有权
        gate.enqueue(5)     # 先到的等待者，序号大
        later = gate.enqueue(3)  # 后到但接纳更早
        entered = []

        def try_enter():
            gate.enter(later)
            entered.append(1)
            gate.release(later)

        t = threading.Thread(target=try_enter, daemon=True)
        t.start()
        time.sleep(0.2)
        self.assertFalse(entered, "序号 5 还在前面，3 不应先进入")
        with gate._cv:
            first_is_3 = gate._waiters[0].seq == 3
        self.assertTrue(first_is_3, "等待队列应按序号排序")
        gate.release(holder)
        t.join(timeout=5)
        self.assertTrue(entered)
        # 清理：释放剩余等待票据
        with gate._cv:
            leftover = list(gate._waiters)
        for ticket in leftover:
            gate.abandon(ticket)

    def test_resource_gate_waiter_abandon_releases_successor(self):
        """队首等待者放弃时，必须清理票据并唤醒后继。"""
        gate = worker.ResourceGate()
        holder = gate.enqueue(0)
        stuck = gate.enqueue(1)
        gate.abandon(stuck)  # 执行失败路径的清理入口

        got = []
        ticket2 = gate.enqueue(2)

        def successor():
            gate.enter(ticket2)
            got.append(1)
            gate.release(ticket2)

        th = threading.Thread(target=successor)
        th.start()
        time.sleep(0.2)
        self.assertFalse(got, "持有者未释放，后继不应提前获得")
        gate.release(holder)
        th.join(timeout=5)
        self.assertFalse(th.is_alive(), "后继被永久阻塞 → 放弃路径未清理票据")


class TestRound2Regressions(unittest.TestCase):
    """对应二轮审计 R1-R8 的防回归用例。"""

    def setUp(self):
        self.app = worker.App(verify=False)
        self.app.sched._waiting = 0

    def tearDown(self):
        self.app.sched._waiting = 0
        self.app.db.close()

    # ---- R1：确认执行时按最新上下文重检权限 ----

    def test_confirmed_job_downgraded_by_new_privileged_context(self):
        # 专属项目，避免共享测试库中其他用例的提权任务污染 effective_kind
        d = os.path.join(TMP, "r1proj")
        os.makedirs(d, exist_ok=True)
        self.app.add_project("r1proj", d)
        pending = worker.do_execute(self.app, {
            "task": "删除临时测试文件", "job_id": "r1-old",
            "channel": "owner", "project": "r1proj"})
        self.assertEqual(pending["code"], "NEED_CONFIRM")
        with mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            worker.do_execute(self.app, {"task": "sudo synthetic-command",
                                         "job_id": "r1-new", "channel": "owner",
                                         "project": "r1proj"})
            worker.do_execute(self.app, {"task": "确认", "job_id": "r1-old",
                                         "channel": "owner", "project": "r1proj",
                                         "confirm": pending["token"]})
        command = execute.call_args.args[0]
        self.assertIn(worker.PATCH_RO, command,
                      "确认执行必须按最新上下文重检：新注入的提权计划要求只读")
        self.assertNotIn(worker.PATCH_FULL, command)
        row = self.app.db.one("SELECT kind FROM jobs WHERE job_id=?", ("r1-old",))
        self.assertEqual(row["kind"], "needs_privilege", "重检结果应落库")

    def test_context_history_downgrades_even_beyond_ttl(self):
        """TTL 窗口外的提权计划仍在上下文窗口内 → 同样降级（classify(context)）。"""
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            worker.do_execute(self.app, {"task": "sudo old-plan",
                                         "job_id": "r1b-plan", "channel": "owner"})
        with mock.patch.object(worker, "PRIV_PLAN_TTL", 0), \
                mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            r = worker.do_execute(self.app, {"task": "照刚才的方案执行",
                                             "job_id": "r1b-follow", "channel": "owner"})
        command = execute.call_args.args[0]
        self.assertIn(worker.PATCH_RO, command,
                      "上下文里仍可见的提权文本必须触发只读降级")
        self.assertNotIn(worker.PATCH_FULL, command)

    # ---- R3：worker 侧接纳/执行分离 + 查询 ----

    def test_async_accept_returns_then_completes(self):
        slow = _fake_dsh(os.path.join(TMP, "fake_dsh_r3.sh"), "sleep 1\necho r3-ok\n")
        old = worker.DSH
        worker.DSH = slow
        try:
            r = worker.do_execute(self.app, {"task": "看一下磁盘", "job_id": "r3-job",
                                             "channel": "owner", "wait": False})
            self.assertEqual(r["code"], "ACCEPTED")
            self.assertEqual(r["status"], "queued")
            deadline = time.time() + 15
            status = None
            while time.time() < deadline:
                status = self.app.get_job("r3-job")
                if status and status["status"] == "done":
                    break
                time.sleep(0.1)
            self.assertEqual(status["status"], "done", "异步任务应最终完成")
            self.assertIn("r3-ok", status["message"])
        finally:
            worker.DSH = old
        self.assertEqual(self.app.sched.snapshot()["waiting"], 0)

    def test_get_job_missing_returns_none(self):
        self.assertIsNone(self.app.get_job("no-such-job"))

    def test_force_rerun_allows_different_request_text(self):
        """R8：聊天里的重跑消息正文不同，force 仍应触发已存任务的重跑。"""
        d = os.path.join(TMP, "r8proj")
        os.makedirs(d, exist_ok=True)
        self.app.add_project("r8proj", d)
        with mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            worker.do_execute(self.app, {"task": "原始任务内容",
                                         "job_id": "r8-job", "channel": "owner",
                                         "project": "r8proj"})
            self.app.db.execute("UPDATE jobs SET status='uncertain', "
                                "error_code='INTERRUPTED' WHERE job_id='r8-job'")
            r = worker.do_execute(self.app, {"task": "重跑刚才那个",
                                             "job_id": "r8-job", "channel": "owner",
                                             "force": 1})
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(execute.call_count, 2)
        # 执行的是已存任务快照，不是新正文
        self.assertIn("原始任务内容", execute.call_args.args[0][-1])

    # ---- R4：队列满不消耗令牌；过期确认落终态 ----

    def test_queue_full_keeps_token_usable(self):
        payload = {"task": "rm -rf ./x", "job_id": "r4-job", "channel": "owner"}
        first = worker.do_execute(self.app, dict(payload))
        token = first["token"]
        self.app.sched._waiting = self.app.sched._queue_max
        try:
            with self.assertRaises(worker.AgentBridgeError) as ctx:
                worker.do_execute(self.app, {**payload, "confirm": token})
            self.assertEqual(ctx.exception.code, "QUEUE_FULL")
        finally:
            self.app.sched._waiting = 0
        row = self.app.db.one("SELECT status FROM jobs WHERE job_id=?", ("r4-job",))
        self.assertEqual(row["status"], "pending-confirm",
                         "队列满时任务应留在待确认，不得消耗令牌")
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            ok = worker.do_execute(self.app, {**payload, "confirm": token})
        self.assertEqual(ok["status"], "done", "同一令牌在队列空闲后应仍可用")

    def test_expired_confirm_cancelled_and_project_released(self):
        d = os.path.join(TMP, "r4proj")
        os.makedirs(d, exist_ok=True)
        self.app.add_project("r4proj", d)
        worker.do_execute(self.app, {"task": "rm -rf ./x", "job_id": "r4-exp",
                                     "channel": "owner", "project": "r4proj"})
        self.app.db.execute("UPDATE pending SET expires_at=? WHERE job_id=?",
                            (time.time() - 1, "r4-exp"))
        self.app.prune_data()
        row = self.app.db.one("SELECT status, error_code FROM jobs WHERE job_id=?",
                              ("r4-exp",))
        self.assertEqual(row["status"], "cancelled",
                         "过期确认应明确落终态，不得停在 pending-confirm")
        self.assertEqual(row["error_code"], "CONFIRM_EXPIRED")
        self.assertTrue(self.app.rm_project("r4proj")["ok"],
                        "确认过期后项目应可解绑/删除")

    # ---- R5：执行顺序 = 接纳顺序 ----

    def test_acceptance_order_is_execution_order(self):
        admitted = threading.Event()
        release = threading.Event()
        order = []
        original = self.app.set_current_project

        def pause_after_acceptance(channel, project):
            original(channel, project)
            if channel == "first-channel":
                admitted.set()
                if not release.wait(5):
                    raise RuntimeError("test sync timeout")

        def execute(command, *args, **kwargs):
            order.append("first" if command[-1].endswith("synthetic-first") else "second")
            return _fake_result()

        self.app.sched._queue_max = 8
        with mock.patch.object(self.app, "set_current_project",
                               side_effect=pause_after_acceptance), \
                mock.patch.object(worker, "run_task", side_effect=execute):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = pool.submit(worker.do_execute, self.app,
                                    {"task": "synthetic-first", "job_id": "r5-a",
                                     "channel": "first-channel"})
                try:
                    self.assertTrue(admitted.wait(5))
                    # A 已接纳并停住；此刻在另一线程接纳 B——B 不得先执行
                    fut_b = pool.submit(worker.do_execute, self.app,
                                        {"task": "synthetic-second", "job_id": "r5-b",
                                         "channel": "second-channel"})
                    # 等两个任务都完成接纳（等待位计到 2）
                    deadline = time.time() + 5
                    while time.time() < deadline:
                        if self.app.sched.snapshot()["waiting"] >= 2:
                            break
                        time.sleep(0.02)
                finally:
                    release.set()
                ra = fut_a.result(timeout=10)
                rb = fut_b.result(timeout=10)
        self.assertTrue(ra.get("ok") and rb.get("ok"), (ra, rb))
        self.assertEqual(order, ["first", "second"],
                         "执行顺序必须等于接纳顺序，不得被线程调度重排")

    # ---- R6：并发主键冲突也要校验内容绑定 ----

    def test_concurrent_id_collision_reports_conflict(self):
        barrier = threading.Barrier(2)
        seen: list = []
        original = self.app.db.one

        def both_observe_missing(sql, args=()):
            result = original(sql, args)
            if sql == "SELECT * FROM jobs WHERE job_id = ?" and args == ("r6-id",):
                seen.append(1)
                if len(seen) <= 2:
                    barrier.wait(timeout=10)
            return result

        def attempt(task):
            try:
                return ("ok", worker.do_execute(self.app, {
                    "task": task, "job_id": "r6-id", "channel": "owner"}))
            except worker.AgentBridgeError as exc:
                return ("err", exc)

        self.app.sched._queue_max = 8
        with mock.patch.object(self.app.db, "one", side_effect=both_observe_missing), \
                mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(attempt, t)
                           for t in ("r6-content-one", "r6-content-two")]
                outcomes = [f.result(timeout=15) for f in futures]
        self.assertEqual(execute.call_count, 1)
        conflicts = [o for o in outcomes
                     if o[0] == "err" and "已绑定其他任务内容" in o[1].message]
        self.assertEqual(len(conflicts), 1,
                         f"不同内容撞同一 job_id 必须报内容冲突：{outcomes}")

    # ---- R7：sender（身份）与 channel（会话路由）分离 ----

    def test_sender_channel_separation(self):
        d = os.path.join(TMP, "r7proj")
        os.makedirs(d, exist_ok=True)
        self.app.add_project("r7proj", d)
        with mock.patch.object(worker, "ALLOWED_SENDERS", {"owner"}), \
                mock.patch.object(worker, "run_task", side_effect=_fake_result):
            r = worker.do_execute(self.app, {
                "task": "看一下", "job_id": "r7a", "channel": "conv-a",
                "sender": "owner", "project": "r7proj"})
            self.assertTrue(r["ok"])
            # 会话 conv-a 粘到 r7proj；同一 sender 的另一会话不受影响
            self.assertEqual(self.app.current_project("conv-a"), "r7proj")
            self.assertEqual(self.app.current_project("conv-b"), "default")
            # 白名单验证的是身份：sender 合法即可，channel 无须在名单内
            r2 = worker.do_execute(self.app, {
                "task": "再看一下", "job_id": "r7b", "channel": "conv-b",
                "sender": "owner"})
            self.assertTrue(r2["ok"])
            with self.assertRaises(worker.AgentBridgeError) as ctx:
                worker.do_execute(self.app, {
                    "task": "hi", "job_id": "r7c", "channel": "conv-a",
                    "sender": "intruder"})
            self.assertEqual(ctx.exception.code, "AUTH_FAIL")


class TestRound3Regressions(unittest.TestCase):
    """对应三轮审计 T2-T7 的防回归用例。"""

    def setUp(self):
        self.app = worker.App(verify=False)
        self.app.sched._waiting = 0

    def tearDown(self):
        self.app.sched._waiting = 0
        self.app.db.close()

    def _hammer_prune(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.app.prune_data()
            time.sleep(0.002)

    # ---- T2：多语句迁移与 prune 互斥 ----

    def test_confirm_consumption_survives_concurrent_prune(self):
        payload = {"task": "rm -rf ./x", "job_id": "t2-consume",
                   "channel": "owner"}
        first = worker.do_execute(self.app, dict(payload))
        stop = threading.Event()
        hammer = threading.Thread(target=self._hammer_prune, args=(stop,), daemon=True)
        hammer.start()
        try:
            with mock.patch.object(worker, "run_task", side_effect=_fake_result):
                r = worker.do_execute(self.app, {**payload, "confirm": first["token"]})
        finally:
            stop.set()
            hammer.join(timeout=5)
        self.assertEqual(r["status"], "done", r)
        row = self.app.db.one("SELECT status, error_code FROM jobs WHERE job_id=?",
                              ("t2-consume",))
        self.assertEqual(row["status"], "done",
                         "并发清理不得把有效确认误判过期")
        self.assertIsNone(row["error_code"])

    def test_pending_creation_survives_concurrent_prune(self):
        stop = threading.Event()
        hammer = threading.Thread(target=self._hammer_prune, args=(stop,), daemon=True)
        hammer.start()
        try:
            r = worker.do_execute(self.app, {
                "task": "rm -rf ./y", "job_id": "t2-create", "channel": "owner"})
        finally:
            stop.set()
            hammer.join(timeout=5)
        self.assertEqual(r["code"], "NEED_CONFIRM")
        row = self.app.db.one("SELECT status FROM jobs WHERE job_id=?", ("t2-create",))
        self.assertEqual(row["status"], "pending-confirm",
                         "任务+令牌必须同事务创建，不得被清理误取消")
        # 签发的令牌必须可用
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            ok = worker.do_execute(self.app, {"task": "rm -rf ./y",
                                              "job_id": "t2-create",
                                              "channel": "owner",
                                              "confirm": r["token"]})
        self.assertEqual(ok["status"], "done")

    def test_state_changed_returns_real_terminal(self):
        """认领失败按真实终态回报，cancelled 不包成 BUSY。"""
        payload = {"task": "rm -rf ./z", "job_id": "t2-terminal", "channel": "owner"}
        first = worker.do_execute(self.app, dict(payload))
        # 令牌仍有效时把任务改成 cancelled，模拟认领窗口外被取消
        self.app.db.execute(
            "UPDATE jobs SET status='cancelled', error_code='CONFIRM_EXPIRED' "
            "WHERE job_id='t2-terminal'")
        r = worker.do_execute(self.app, {**payload, "confirm": first["token"]})
        self.assertEqual(r["status"], "cancelled", "应如实回报终态而非 BUSY")
        self.assertEqual(r.get("reused"), True)

    # ---- T3：接纳线性化（认领+取号+入队同锁）----

    def test_pause_inside_critical_section_preserves_order(self):
        """在临界区内（认领后、入队前）暂停 A，B 不得先执行。"""
        allocated = threading.Event()
        release = threading.Event()
        order = []
        original = self.app.next_seq

        def pause_at_first_seq():
            seq = original()
            if seq == 1:
                allocated.set()
                if not release.wait(5):
                    raise RuntimeError("sync timeout")
            return seq

        def backend(command, *args, **kwargs):
            order.append("a" if command[-1].endswith("synthetic-a") else "b")
            return _fake_result()

        self.app.sched._queue_max = 8
        with mock.patch.object(self.app, "next_seq", side_effect=pause_at_first_seq), \
                mock.patch.object(worker, "run_task", side_effect=backend):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = pool.submit(worker.do_execute, self.app,
                                    {"task": "synthetic-a", "job_id": "t3-a",
                                     "channel": "chan-a"})
                self.assertTrue(allocated.wait(5))
                fut_b = pool.submit(worker.do_execute, self.app,
                                    {"task": "synthetic-b", "job_id": "t3-b",
                                     "channel": "chan-b"})
                time.sleep(0.3)  # B 只能阻塞在 admit 锁上
                self.assertEqual(order, [], "B 不得在 A 入队前执行")
                release.set()
                ra = fut_a.result(timeout=10)
                rb = fut_b.result(timeout=10)
        self.assertTrue(ra.get("ok") and rb.get("ok"), (ra, rb))
        self.assertEqual(order, ["a", "b"])

    # ---- T4：派发失败收尾 ----

    def test_thread_start_failure_cleaned_up(self):
        with mock.patch.object(worker.threading.Thread, "start",
                               side_effect=RuntimeError("synthetic thread limit")):
            with self.assertRaises(RuntimeError):
                worker.do_execute(self.app, {"task": "synthetic", "job_id": "t4-job",
                                             "channel": "owner", "wait": False})
        row = self.app.db.one(
            "SELECT status, error_code, lock_key FROM jobs WHERE job_id=?",
            ("t4-job",))
        self.assertEqual(row["status"], "failed", "派发失败必须落终态")
        self.assertEqual(self.app.sched.snapshot()["waiting"], 0, "等待位必须归还")
        gate = self.app.sched.resource_gate(row["lock_key"])
        with gate._cv:
            leftover = len(gate._waiters)
        self.assertEqual(leftover, 0, "不得遗留无人持有的队首票据")
        retry = worker.do_execute(self.app, {"task": "synthetic", "job_id": "t4-job",
                                             "channel": "owner"})
        self.assertEqual(retry["status"], "failed", "重试应得到终态而非永久 BUSY")

    # ---- T5：父进程退出后监督同组后代 ----

    def test_parent_success_still_reaps_same_group_descendant(self):
        marker = os.path.join(TMP, "t5_marker")
        child_pid_file = os.path.join(TMP, "t5_child.pid")
        for stale in (marker, child_pid_file):
            if os.path.exists(stale):
                os.unlink(stale)
        child = (f"import pathlib,time; time.sleep(5.5); "
                 f"pathlib.Path({marker!r}).write_text('survived'); time.sleep(20)")
        parent = ("import subprocess,sys,pathlib; "
                  f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                  f"pathlib.Path({child_pid_file!r}).write_text(str(p.pid))")
        try:
            res = worker.run_task([sys.executable, "-c", parent], TMP, timeout=1)
            self.assertEqual(res["exit_code"], 0)
            self.assertFalse(res["timed_out"])
            self.assertTrue(res.get("group_reaped"),
                            "同组后代存活时必须在期限处回收整组")
            deadline = time.time() + 3
            while time.time() < deadline and not os.path.exists(marker):
                time.sleep(0.05)
            self.assertFalse(os.path.exists(marker),
                             "后台后代越过任务期限写入标记 → 未被监督回收")
        finally:
            if os.path.exists(child_pid_file):
                try:
                    with open(child_pid_file) as fh:
                        pid = int(fh.read().strip())
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, ValueError, PermissionError):
                    pass

    # ---- T6：并发项目注册不得绕过重叠检查 ----

    def test_concurrent_registration_rejects_overlap(self):
        """父/子目录并发注册：项目锁串行化后，后进入者必须看到前者并被拒。"""
        parent = os.path.join(TMP, "t6proj")
        child = os.path.join(parent, "child")
        os.makedirs(child, exist_ok=True)
        outcomes = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.app.add_project, name, path)
                       for name, path in (("t6parent", parent), ("t6child", child))]
            for f in futures:
                try:
                    outcomes.append(("ok", f.result(timeout=10)))
                except worker.AgentBridgeError as exc:
                    outcomes.append(("err", exc))
        errs = [o for o in outcomes if o[0] == "err"]
        self.assertEqual(len(errs), 1,
                         f"父/子并发注册必须恰好一个被拒：{outcomes}")
        self.assertIn("重叠", errs[0][1].message)
        kept = [o for o in outcomes if o[0] == "ok"][0][1]["project"]
        dropped_name = "t6child" if kept == "t6parent" else "t6parent"
        dropped = self.app.db.one(
            "SELECT name FROM projects WHERE name=?", (dropped_name,))
        self.assertIsNone(dropped, "被拒方不得入库")

    # ---- T7：确认消息携带自己的新消息 ID ----

    def test_confirmation_with_new_message_id_executes(self):
        pending = worker.do_execute(self.app, {
            "task": "rm -rf ./w", "job_id": "t7-original", "channel": "owner"})
        self.assertEqual(pending["code"], "NEED_CONFIRM")
        with mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            r = worker.do_execute(self.app, {
                "task": "确认 " + pending["token"],
                "job_id": "t7-confirm-msg",  # 确认消息自己的稳定 ID
                "id_stable": True, "channel": "owner",
                "confirm": pending["token"]})
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.app.get_job("t7-original")["status"], "done")


class TestRound4Regressions(unittest.TestCase):
    """对应四轮审计 U1-U6 的防回归用例。"""

    def setUp(self):
        self.app = worker.App(verify=False)
        self.app.sched._waiting = 0

    def tearDown(self):
        self.app.sched._waiting = 0
        self.app.db.close()

    # ---- U1：崩溃恢复先结算遗留执行器；隔离资源拒绝新任务 ----

    def test_crash_recovery_kills_verified_orphan(self):
        """独立 worker 进程被 SIGKILL 后，恢复精确核对 OS 启动身份并回收
        遗留后端；同资源不再与旧执行器并发。"""
        db_path = os.path.join(TMP, "u1.db")
        audit_dir = os.path.join(TMP, "u1-audit")
        home = os.path.join(TMP, "u1-home")
        os.makedirs(home, exist_ok=True)
        for stale in (db_path,):
            if os.path.exists(stale):
                os.unlink(stale)
        pid_file = os.path.join(TMP, "u1-orphan.pid")
        if os.path.exists(pid_file):
            os.unlink(pid_file)
        child = (f"import pathlib,os,time; pathlib.Path({pid_file!r}).write_text(str(os.getpid())); "
                 "time.sleep(30)")
        wrapper = (
            "import importlib.util,sys; "
            f"s=importlib.util.spec_from_file_location('w',{worker.__file__!r}); "
            "w=importlib.util.module_from_spec(s);s.loader.exec_module(w); "
            f"w.HOME={home!r};w.DB_PATH={db_path!r};w.AUDIT_DIR={audit_dir!r}; "
            "app=w.App(verify=False); real=w.run_task; "
            f"w.run_task=lambda cmd,cwd,timeout,on_start=None: real("
            f"[sys.executable,'-c',{child!r}],{TMP!r},20,on_start=on_start); "
            "w.do_execute(app,{'task':'u1-old','job_id':'u1-old','channel':'owner'})"
        )
        proc = subprocess.Popen([sys.executable, "-B", "-c", wrapper],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        orphan_pid = None
        try:
            deadline = time.time() + 8
            while time.time() < deadline and not os.path.exists(pid_file):
                time.sleep(0.05)
            self.assertTrue(os.path.exists(pid_file), "合成后端未启动")
            with open(pid_file) as fh:
                orphan_pid = int(fh.read().strip())
            proc.kill()
            proc.wait(timeout=5)
            os.kill(orphan_pid, 0)  # 后端仍在运行（worker 死了）
            # 新 App 恢复：应校验并击杀遗留执行器
            with mock.patch.object(worker, "DB_PATH", db_path), \
                    mock.patch.object(worker, "AUDIT_DIR", audit_dir):
                recovered = worker.App(verify=False)
                try:
                    self.assertEqual(recovered.get_job("u1-old")["status"], "uncertain")
                    with self.assertRaises(ProcessLookupError):
                        os.kill(orphan_pid, 0)
                finally:
                    recovered.db.close()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if orphan_pid is not None:
                try:
                    os.killpg(orphan_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def test_quarantine_blocks_and_lifts(self):
        lock = self.app.db.one(
            "SELECT lock_key FROM projects WHERE name='default'")["lock_key"]
        # 用当前进程组充当「存活的遗留执行器」：只探测不击杀
        self.app.db.execute(
            "INSERT INTO quarantine (lock_key, pgid, reason, created_at) VALUES (?,?,?,?)",
            (lock, os.getpgid(0), "synthetic leftover", time.time()))
        with self.assertRaises(worker.AgentBridgeError) as ctx:
            worker.do_execute(self.app, {"task": "hi", "channel": "owner",
                                         "project": "default", "job_id": "u1-q-job"})
        self.assertEqual(ctx.exception.code, "BUSY")
        self.assertIn("隔离", ctx.exception.message)
        # 遗留进程退出后自动解除
        self.app.db.execute("UPDATE quarantine SET pgid=? WHERE lock_key=?",
                            (99999999, lock))
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            r = worker.do_execute(self.app, {"task": "hi", "channel": "owner",
                                             "project": "default",
                                             "job_id": "u1-q-job2"})
        self.assertEqual(r["status"], "done")

    # ---- U2：确认消息 ID 与目标任务的持久别名 ----

    def test_confirmation_alias_replay_and_query(self):
        pending = worker.do_execute(self.app, {
            "task": "rm -rf ./u2", "job_id": "u2-target", "channel": "owner"})
        payload = {"task": "确认 " + pending["token"], "job_id": "u2-confirm-msg",
                   "channel": "owner", "confirm": pending["token"]}
        with mock.patch.object(worker, "run_task", side_effect=_fake_result) as execute:
            first = worker.do_execute(self.app, dict(payload))
            self.assertEqual(first["status"], "done")
            # 完全相同的确认投递重试：幂等取回目标任务结果，不报 AUTH_FAIL
            retry = worker.do_execute(self.app, dict(payload))
            self.assertEqual(retry["status"], "done", retry)
            self.assertTrue(retry.get("reused"))
        self.assertEqual(execute.call_count, 1)
        # 对账查询：用确认消息 ID 也能解析到目标任务
        via_alias = self.app.get_job("u2-confirm-msg")
        self.assertIsNotNone(via_alias)
        self.assertEqual(via_alias["status"], "done")
        self.assertEqual(via_alias["requested_id"], "u2-confirm-msg")

    # ---- U3：待确认查询完整状态；重发重新签发令牌 ----

    def test_pending_query_and_resend_reissue(self):
        first = worker.do_execute(self.app, {
            "task": "rm -rf ./u3", "job_id": "u3-job", "channel": "owner"})
        old_token = first["token"]
        # 查询（对账）得到完整业务状态与指引，而非空正文
        q = self.app.get_job("u3-job")
        self.assertEqual(q["code"], "NEED_CONFIRM")
        self.assertIn("重新签发", q["message"])
        self.assertNotIn("token", q)
        # 响应丢失后用户重发原任务：签发新令牌（旧令牌作废）
        second = worker.do_execute(self.app, {
            "task": "rm -rf ./u3", "job_id": "u3-job", "channel": "owner"})
        self.assertEqual(second["code"], "NEED_CONFIRM")
        self.assertNotEqual(second["token"], old_token)
        with self.assertRaises(worker.AgentBridgeError):
            worker.do_execute(self.app, {
                "task": "rm -rf ./u3", "job_id": "u3-job", "channel": "owner",
                "confirm": old_token})
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            done = worker.do_execute(self.app, {
                "task": "rm -rf ./u3", "job_id": "u3-job", "channel": "owner",
                "confirm": second["token"]})
        self.assertEqual(done["status"], "done")

    # ---- U5：粘性路由按接纳顺序生效 ----

    def test_sticky_order_follows_admission_order(self):
        for name in ("u5-a", "u5-b"):
            d = os.path.join(TMP, name)
            os.makedirs(d, exist_ok=True)
            self.app.add_project(name, d)
        original = self.app.set_current_project

        def slow_first_write(channel, project):
            if project == "u5-a":
                time.sleep(0.4)  # 旧任务接纳后延迟写路由（在 admit 锁内）
            return original(channel, project)

        self.app.sched._queue_max = 8
        with mock.patch.object(self.app, "set_current_project",
                               side_effect=slow_first_write), \
                mock.patch.object(worker, "run_task", side_effect=_fake_result):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fa = pool.submit(worker.do_execute, self.app,
                                 {"task": "a", "job_id": "u5-a", "channel": "owner",
                                  "project": "u5-a"})
                time.sleep(0.05)
                fb = pool.submit(worker.do_execute, self.app,
                                 {"task": "b", "job_id": "u5-b", "channel": "owner",
                                  "project": "u5-b"})
                fa.result(timeout=10)
                fb.result(timeout=10)
        self.assertEqual(self.app.current_project("owner"), "u5-b",
                         "后接纳任务的路由不得被旧任务的延迟写覆盖")
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            following = worker.do_execute(self.app, {"task": "next",
                                                     "job_id": "u5-next",
                                                     "channel": "owner"})
        self.assertEqual(following["project"], "u5-b")

    # ---- U6：项目删除与快照接纳互斥 ----

    def test_removed_project_rejects_stale_snapshot(self):
        parent = os.path.join(TMP, "u6parent")
        child = os.path.join(parent, "child")
        os.makedirs(child, exist_ok=True)
        self.app.add_project("u6parent", parent)
        snap_ready = threading.Event()
        resume = threading.Event()
        original = worker._start_fresh

        def pause_before_admit(app, snap, *args, **kwargs):
            if snap["job_id"] == "u6-old":
                snap_ready.set()
                if not resume.wait(5):
                    raise RuntimeError("sync timeout")
            return original(app, snap, *args, **kwargs)

        with mock.patch.object(worker, "_start_fresh",
                               side_effect=pause_before_admit), \
                mock.patch.object(worker, "run_task", side_effect=_fake_result):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(worker.do_execute, self.app,
                                  {"task": "u6-old-task", "job_id": "u6-old",
                                   "project": "u6parent", "channel": "owner"})
                try:
                    self.assertTrue(snap_ready.wait(5))
                    # 快照已生成、尚未入库：此刻删除项目并注册子目录
                    self.assertTrue(self.app.rm_project("u6parent")["ok"])
                    self.assertTrue(self.app.add_project("u6child", child)["ok"])
                finally:
                    resume.set()
                with self.assertRaises(worker.AgentBridgeError) as ctx:
                    fut.result(timeout=10)
        self.assertEqual(ctx.exception.code, "PROJECT_NOT_FOUND",
                         "已删除项目的陈旧快照必须被拒绝接纳")
        # 子目录项目正常工作，与被拒的旧任务无并发
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            r = worker.do_execute(self.app, {"task": "child-task", "job_id": "u6-child",
                                             "project": "u6child", "channel": "owner"})
        self.assertEqual(r["status"], "done")

    # ---- 入队后的异常不得泄漏队首票据（U5 重构引入的边界）----

    def test_sticky_failure_after_enqueue_abandons_ticket(self):
        with mock.patch.object(self.app, "set_current_project",
                               side_effect=RuntimeError("sticky boom")):
            with self.assertRaises(RuntimeError):
                worker.do_execute(self.app, {"task": "u5x-a", "job_id": "u5x-a",
                                             "channel": "owner", "project": "default"})
        row = self.app.db.one("SELECT status FROM jobs WHERE job_id=?", ("u5x-a",))
        self.assertEqual(row["status"], "failed", "接纳中断必须落终态")
        self.assertEqual(self.app.sched.snapshot()["waiting"], 0)
        gate = self.app.sched.resource_gate(
            self.app.db.one("SELECT lock_key FROM projects WHERE name='default'")["lock_key"])
        with gate._cv:
            self.assertEqual(len(gate._waiters), 0, "不得遗留队首票据")
        # 同资源后续任务不被阻塞
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            r = worker.do_execute(self.app, {"task": "u5x-b", "job_id": "u5x-b",
                                             "channel": "owner", "project": "default"})
        self.assertEqual(r["status"], "done")

    # ---- run_task 暴露 pgid（供崩溃恢复对账）----

    def test_run_task_reports_pgid_on_start(self):
        seen = []
        script = _fake_dsh(os.path.join(TMP, "fake_dsh_u4.sh"), "echo pgid-ok\n")
        res = worker.run_task([script], TMP, 10, on_start=seen.append)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 1)


class TestRound5Fixes(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="agentbridge-r5-")))
        home = self.root / "home"
        home.mkdir()
        for name, value in (
            ("HOME", str(home)), ("DB_PATH", str(self.root / "jobs.db")),
            ("AUDIT_DIR", str(self.root / "audit")),
            ("DSH_SETTINGS_PATH", str(self.root / "settings.yaml")),
            ("QUEUE_MAX", 8), ("CONCURRENCY", 3),
        ):
            stack.enter_context(mock.patch.object(worker, name, value))
        self.app = worker.App(verify=False)
        self.addCleanup(lambda: self.app.db.close())

    def project(self, name):
        directory = self.root / name
        directory.mkdir()
        self.app.add_project(name, str(directory))
        return directory

    def test_recovery_never_kills_mismatched_birth_identity(self):
        row = {"job_id": "mismatch", "exec_pgid": 99999999,
               "exec_identity": "recorded-birth", "exec_state": "registered",
               "cwd": str(self.root), "lock_key": "mismatch-lock"}
        with mock.patch.object(worker, "_group_alive", return_value=True), \
                mock.patch.object(worker, "_executor_identity", return_value="different-birth"), \
                mock.patch.object(worker, "_killpg") as kill:
            self.assertEqual(self.app._settle_stale_executor(row), "quarantined")
        kill.assert_not_called()

    def test_legacy_missing_identity_requires_manual_quarantine(self):
        project = self.app.get_project("default")
        self.app.db.execute(
            "INSERT INTO jobs(job_id,task,status,created_at,cwd,lock_key,project) VALUES(?,?,?,?,?,?,?)",
            ("legacy", "synthetic", "running", time.time(), project["normalized"],
             project["lock_key"], "default"))
        self.app._recover_stale_jobs()
        with self.assertRaises(worker.AgentBridgeError) as error:
            self.app._check_quarantine(project["lock_key"], project["normalized"])
        self.assertEqual(error.exception.code, "BUSY")
        self.assertIsNotNone(self.app.db.one("SELECT * FROM quarantine"))

    def test_missing_group_leader_does_not_authorize_group_kill(self):
        row = {"job_id": "leader-gone", "exec_pgid": 99999999,
               "exec_identity": "recorded", "exec_state": "registered",
               "cwd": str(self.root), "lock_key": "leader-gone"}
        with mock.patch.object(worker, "_group_alive", return_value=True), \
                mock.patch.object(worker, "_executor_identity", return_value=None), \
                mock.patch.object(worker, "_killpg") as kill:
            self.assertEqual(self.app._settle_stale_executor(row), "quarantined")
        kill.assert_not_called()

    def test_registration_failure_never_executes_backend(self):
        marker = self.root / "must-not-execute"
        script = _fake_dsh(str(self.root / "backend.sh"), f"touch '{marker}'\nsleep 10\n")
        pids = []

        def reject(pgid):
            pids.append(pgid)
            raise RuntimeError("synthetic registration failure")

        with self.assertRaises(RuntimeError):
            worker.run_task([script], str(self.root), 1, on_start=reject)
        self.assertFalse(marker.exists())
        self.assertFalse(worker._group_alive(pids[0]))

    def test_reader_thread_failure_never_releases_start_gate(self):
        marker = self.root / "thread-must-not-execute"
        script = _fake_dsh(str(self.root / "thread-backend.sh"), f"touch '{marker}'\n")
        seen = []
        with mock.patch.object(worker.threading.Thread, "start", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                worker.run_task([script], str(self.root), 1, on_start=seen.append)
        self.assertFalse(marker.exists())
        self.assertFalse(worker._group_alive(seen[0]))

    def test_persistence_failure_fails_job_without_backend_side_effects(self):
        marker = self.root / "never-written"
        script = _fake_dsh(str(self.root / "dsh.sh"), f"touch '{marker}'\n")
        original = self.app.db.execute

        def reject_identity(sql, args=()):
            if sql.startswith("UPDATE jobs SET exec_pgid="):
                raise worker.sqlite3.OperationalError("synthetic persistence failure")
            return original(sql, args)

        with mock.patch.object(worker, "DSH", script), \
                mock.patch.object(self.app.db, "execute", side_effect=reject_identity):
            with self.assertRaises(worker.sqlite3.OperationalError):
                worker.do_execute(self.app, {"task": "synthetic", "job_id": "persist-fail", "channel": "owner"})
        self.assertFalse(marker.exists())
        self.assertEqual(self.app.get_job("persist-fail")["status"], "failed")
        self.assertEqual(self.app.sched.snapshot()["waiting"], 0)

    def test_crash_before_registration_keeps_backend_gated(self):
        paused = self.root / "gate.pid"
        marker = self.root / "backend-ran"
        child = f"import pathlib;pathlib.Path({str(marker)!r}).write_text('ran')"
        wrapper = (
            "import importlib.util,sys,time,pathlib; "
            f"s=importlib.util.spec_from_file_location('w',{worker.__file__!r}); "
            "w=importlib.util.module_from_spec(s);s.loader.exec_module(w); "
            f"w.HOME={worker.HOME!r};w.DB_PATH={worker.DB_PATH!r}; "
            f"w.AUDIT_DIR={worker.AUDIT_DIR!r};w.DSH_SETTINGS_PATH={worker.DSH_SETTINGS_PATH!r}; "
            "app=w.App(verify=False);real=w.run_task; "
            "\ndef blocked(pgid,callback):\n"
            f" pathlib.Path({str(paused)!r}).write_text(str(pgid))\n"
            " time.sleep(30)\n callback(pgid)\n"
            f"w.run_task=lambda cmd,cwd,timeout,on_start=None:real([sys.executable,'-c',{child!r}],"
            "cwd,10,on_start=lambda pgid:blocked(pgid,on_start)); "
            "w.do_execute(app,{'task':'synthetic','job_id':'gate-crash','channel':'owner'})"
        )
        parent = subprocess.Popen([sys.executable, "-B", "-c", wrapper],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pgid = None
        try:
            deadline = time.monotonic() + 5
            while not paused.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(paused.exists())
            pgid = int(paused.read_text())
            parent.kill()
            parent.wait(5)
            deadline = time.monotonic() + 3
            while worker._group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(worker._group_alive(pgid))
            self.assertFalse(marker.exists())
            self.app.db.close()
            self.app = worker.App(verify=False)
            self.assertEqual(self.app.get_job("gate-crash")["status"], "uncertain")
            self.assertEqual(self.app.db.query("SELECT * FROM quarantine"), [])
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(5)
            if pgid is not None:
                worker._killpg(pgid, signal.SIGKILL)

    def test_force_clears_previous_execution_identity_before_start(self):
        with mock.patch.object(worker, "run_task", side_effect=_fake_result):
            worker.do_execute(self.app, {"task": "synthetic", "job_id": "force-reset"})
        self.app.db.execute(
            "UPDATE jobs SET status='uncertain',exec_pgid=12345,exec_identity='old',exec_boot='old' WHERE job_id=?",
            ("force-reset",))

        def inspect(*args, **kwargs):
            row = self.app.db.one("SELECT * FROM jobs WHERE job_id=?", ("force-reset",))
            self.assertIsNone(row["exec_pgid"])
            self.assertIsNone(row["exec_identity"])
            self.assertEqual(row["exec_state"], "starting")
            return _fake_result()

        with mock.patch.object(worker, "run_task", side_effect=inspect):
            self.assertTrue(worker.do_execute(self.app,
                {"task": "force", "job_id": "force-reset", "force": True})["ok"])

    def test_sensitive_sticky_commit_is_ordered_with_normal_admission(self):
        self.project("a")
        self.project("b")
        entered, release = threading.Event(), threading.Event()
        original = self.app.set_current_project

        def pause(channel, project):
            if project == "a":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test synchronization timeout")
            original(channel, project)

        with mock.patch.object(self.app, "set_current_project", side_effect=pause), \
                mock.patch.object(worker, "run_task", side_effect=_fake_result), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker.do_execute, self.app,
                {"task": "删除合成文件", "job_id": "sensitive-a", "project": "a", "channel": "owner"})
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(worker.do_execute, self.app,
                    {"task": "synthetic", "job_id": "normal-b", "project": "b", "channel": "owner"})
                self.assertFalse(second.done())
            finally:
                release.set()
            self.assertEqual(first.result(5)["code"], "NEED_CONFIRM")
            self.assertTrue(second.result(5)["ok"])
        self.assertEqual(self.app.current_project("owner"), "b")

    def test_sensitive_stale_snapshot_is_rejected(self):
        self.project("gone")
        original = worker._await_confirm

        def remove_first(app, snap, stable):
            app.rm_project("gone")
            return original(app, snap, stable)

        with mock.patch.object(worker, "_await_confirm", side_effect=remove_first):
            with self.assertRaises(worker.AgentBridgeError) as error:
                worker.do_execute(self.app,
                    {"task": "删除合成文件", "job_id": "stale-sensitive", "project": "gone"})
        self.assertEqual(error.exception.code, "PROJECT_NOT_FOUND")
        self.assertIsNone(self.app.get_job("stale-sensitive"))

    def test_reissue_rechecks_terminal_state_after_prune(self):
        payload = {"task": "删除合成文件", "job_id": "expired-reissue", "channel": "owner"}
        worker.do_execute(self.app, payload)
        self.app.db.execute("UPDATE pending SET expires_at=0 WHERE job_id=?", (payload["job_id"],))
        original = worker._reissue_confirm

        def prune_first(*args):
            self.app.prune_data()
            return original(*args)

        with mock.patch.object(worker, "_reissue_confirm", side_effect=prune_first):
            result = worker.do_execute(self.app, payload)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("token", result)
        self.assertIsNone(self.app.db.one("SELECT * FROM pending WHERE job_id=?", (payload["job_id"],)))

    def test_reissue_rechecks_state_after_concurrent_confirmation(self):
        payload = {"task": "删除合成文件", "job_id": "confirmed-reissue", "channel": "owner"}
        pending = worker.do_execute(self.app, payload)
        original = worker._reissue_confirm

        def confirm_first(*args):
            with mock.patch.object(worker, "run_task", side_effect=_fake_result):
                worker.do_execute(self.app, {**payload, "confirm": pending["token"]})
            return original(*args)

        with mock.patch.object(worker, "_reissue_confirm", side_effect=confirm_first):
            response = worker.do_execute(self.app, payload)
        self.assertEqual(response["status"], "done")
        self.assertNotIn("token", response)
        self.assertIsNone(self.app.db.one("SELECT * FROM pending WHERE job_id=?", (payload["job_id"],)))

    def test_old_quarantine_schema_gets_path_backfilled(self):
        path = self.root / "legacy.db"
        project = self.app.get_project("default")
        conn = worker.sqlite3.connect(path)
        conn.execute("CREATE TABLE quarantine(lock_key TEXT PRIMARY KEY,pgid INTEGER,reason TEXT,created_at REAL)")
        conn.execute("INSERT INTO quarantine VALUES(?,?,?,?)",
                     (project["lock_key"], os.getpgrp(), "legacy", time.time()))
        conn.commit()
        conn.close()
        migrated = worker.DB(str(path))
        try:
            migrated.execute(
                "INSERT INTO projects(name,dir,normalized,lock_key,created_at) VALUES(?,?,?,?,?)",
                ("legacy", project["dir"], project["normalized"], project["lock_key"], time.time()))
        finally:
            migrated.close()
        migrated = worker.DB(str(path))
        try:
            self.assertEqual(migrated.one("SELECT cwd FROM quarantine")["cwd"], project["normalized"])
            cols = {row[1] for row in migrated.query("PRAGMA table_info(jobs)")}
            self.assertTrue({"exec_identity", "exec_state"} <= cols)
        finally:
            migrated.close()

    def test_quarantine_blocks_remove_and_overlapping_registration(self):
        parent = self.project("isolated")
        child = parent / "child"
        child.mkdir()
        project = self.app.get_project("isolated")
        self.app.db.execute(
            "INSERT INTO quarantine(lock_key,pgid,reason,created_at,cwd) VALUES(?,?,?,?,?)",
            (project["lock_key"], os.getpgrp(), "synthetic live group", time.time(), project["normalized"]))
        for change in (lambda: self.app.rm_project("isolated"),
                       lambda: self.app.add_project("child", str(child))):
            with self.assertRaises(worker.AgentBridgeError) as error:
                change()
            self.assertEqual(error.exception.code, "BUSY")
        self.project("unrelated")
        # Even if a historical parent binding is absent, its quarantine path survives.
        self.app.db.execute("DELETE FROM projects WHERE name='isolated'")
        with self.assertRaises(worker.AgentBridgeError):
            self.app.add_project("child", str(child))

    def test_sticky_failure_preserves_other_task_reservation(self):
        self.app.sched.reserve()
        with mock.patch.object(self.app, "set_current_project", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                worker.do_execute(self.app, {"task": "synthetic", "job_id": "failed-sticky", "channel": "owner"})
        self.assertEqual(self.app.sched.snapshot()["waiting"], 1)
        row = self.app.db.one("SELECT * FROM jobs WHERE job_id='failed-sticky'")
        self.assertEqual(row["status"], "failed")
        gate = self.app.sched.resource_gate(row["lock_key"])
        self.assertEqual(len(gate._waiters), 0)
        self.app.sched.release_reservation()


if __name__ == "__main__":
    unittest.main(verbosity=2)
