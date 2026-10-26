"""Transport failures must remain evidence, not abort or masquerade as acceptance."""
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import shim
from tools import observe_release as observer


class TestObservation(unittest.TestCase):
    def test_failure_is_retained_and_sampling_continues(self):
        samples = iter([
            {"ok": True, "tunnel_runs": 1},
            OSError("synthetic disconnect"),
            {"ok": False, "tunnel_runs": 2},
            {"ok": True, "tunnel_runs": 2},
        ])

        def probe():
            value = next(samples)
            if isinstance(value, Exception):
                raise value
            return value

        with tempfile.TemporaryDirectory() as root:
            rows, summary = observer.observe(Path(root) / "samples.jsonl", 0.03, 0.01, probe)
            self.assertEqual(len(rows), 4)
            self.assertEqual(summary["failures"], 2)
            self.assertFalse(summary["passed"])
            self.assertGreater(summary["longest_unreachable_seconds"], 0)
            self.assertEqual(len(summary["recoveries"]), 1)
            self.assertEqual(len((Path(root) / "samples.jsonl").read_text().splitlines()), 5)

    def test_restart_invalidates_healthy_window(self):
        with tempfile.TemporaryDirectory() as root:
            probe = mock.Mock(side_effect=[{"ok": True, "tunnel_runs": 1},
                                           {"ok": True, "tunnel_runs": 2}])
            _, summary = observer.observe(Path(root) / "samples.jsonl", 0.01, 0.01, probe)
            self.assertFalse(summary["passed"])


class TestQueryTransport(unittest.TestCase):
    def test_query_failure_does_not_claim_unknown_acceptance(self):
        with mock.patch.object(shim.urllib.request, "urlopen", side_effect=OSError("offline")) as call, \
                mock.patch.object(shim.time, "sleep"):
            result = shim.control_worker({"op": "status"}, {"sender": "owner", "channel": "chat"})
        self.assertEqual(result["code"], "QUERY_UNAVAILABLE")
        self.assertIn("查询未创建任务", result["message"])
        self.assertEqual(call.call_count, 3)

    def test_mutations_are_never_retried(self):
        with mock.patch.object(shim.urllib.request, "urlopen", side_effect=OSError("lost")) as call:
            result = shim.control_worker({"op": "force", "job_id": "job", "delivery_id": "delivery"},
                                         {"sender": "owner", "channel": "chat"})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["code"], "ACCEPT_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
