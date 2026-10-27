"""The stage-A reporting gate must not turn diagnostic failures into approval."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.check_native_stage import REQUIRED_CHECKS, gate_passed, run_probe


def report():
    return {"stage": "A", "boundary": "protected", "gatePassed": True, "productionChanged": False,
            "realModelCalls": 0, "realToolActions": 0,
            "results": [{"name": name, "passed": True} for name in sorted(REQUIRED_CHECKS)]}


class NativeStageTests(unittest.TestCase):
    def test_all_checks_and_success_exit_required(self):
        self.assertTrue(gate_passed(report(), 0))
        for code in (1, 2, -9):
            self.assertFalse(gate_passed(report(), code))

    def test_known_security_failure_is_not_approval(self):
        data = report()
        data["gatePassed"] = False
        data["results"][0]["passed"] = False
        self.assertFalse(gate_passed(data, 2))
        self.assertFalse(gate_passed(data, 0))

    def test_each_required_check_can_block_gate(self):
        for index in range(len(REQUIRED_CHECKS)):
            for field in (False, None, "true", 1):
                data = report()
                data["results"][index]["passed"] = field
                self.assertFalse(gate_passed(data, 0))

    def test_missing_duplicate_and_unknown_checks_fail(self):
        data = report()
        data["results"].pop()
        self.assertFalse(gate_passed(data, 0))
        data = report()
        data["results"][-1] = copy.deepcopy(data["results"][0])
        self.assertFalse(gate_passed(data, 0))
        data = report()
        data["results"][0]["name"] = "unreviewed check"
        self.assertFalse(gate_passed(data, 0))

    def test_malformed_and_coerced_results_fail(self):
        for data in (None, [], {}, {"results": [None]}, {"results": {}}):
            self.assertFalse(gate_passed(data, 0))
        for key, value in (("gatePassed", 1), ("productionChanged", 0),
                           ("realModelCalls", False), ("realToolActions", False),
                           ("realToolActions", 1), ("stage", "B"), ("boundary", "native-only")):
            data = report()
            data[key] = value
            self.assertFalse(gate_passed(data, 0))

    def test_runner_isolates_environment_and_returns_gate_failure(self):
        data = report()
        data["gatePassed"] = False
        data["results"][0]["passed"] = False
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("tools.check_native_stage.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 2, json.dumps(data), "")
                result, code = run_probe("/bin/sh", root)
                self.assertEqual(code, 2)
                self.assertFalse(result["gatePassed"])
                args, kwargs = run.call_args
                self.assertEqual(args[0][0], "/bin/sh")
                self.assertEqual(kwargs["env"]["HOME"], kwargs["cwd"])
                self.assertEqual(kwargs["env"]["DSH_HOME"], kwargs["cwd"])
                self.assertEqual(set(kwargs["env"]), {
                    "HOME", "DSH_HOME", "TMPDIR", "PATH", "ELECTRON_RUN_AS_NODE",
                    "AGENTBRIDGE_PROBE_PACKAGES"})
                self.assertFalse(Path(kwargs["cwd"]).exists())

    def test_runner_rejects_inconsistent_report(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("tools.check_native_stage.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 2, json.dumps(report()), "")
                with self.assertRaises(ValueError):
                    run_probe("/bin/sh", root)

    def test_runner_refuses_partial_execution_and_stderr(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("tools.check_native_stage.subprocess.run") as run:
                for code, stderr in ((0, "unexpected warning"), (1, ""), (-9, "")):
                    run.return_value = subprocess.CompletedProcess([], code, json.dumps(report()), stderr)
                    with self.assertRaises(RuntimeError):
                        run_probe("/bin/sh", root)

    def test_native_only_diagnostic_is_not_approval(self):
        data = report()
        data["boundary"] = "native-only"
        data["gatePassed"] = False
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("tools.check_native_stage.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 2, json.dumps(data), "")
                _data, code = run_probe("/bin/sh", root, native_only=True)
                self.assertEqual(code, 2)
                self.assertIn("--native-only", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
