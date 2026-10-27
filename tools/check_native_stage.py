"""Fail-closed stage-A checker. Does not start or change any bridge service."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


REQUIRED_CHECKS = frozenset({
    "session_survives_process_exit",
    "create_flush_dispose_resume",
    "persona_is_agent_scoped",
    "pre_execute_denial_reaches_no_body",
    "final_guard_survives_pre_execute_allow",
    "native_mode_rejects_code_and_unregistered_bypasses",
    "nested_dispatch_reenters_guard",
    "cancel_before_dispatch_and_resume_without_replay",
    "guard_binds_actual_dispatch_parameters",
    "guard_binds_actual_dispatch_tool",
})


def gate_passed(report, returncode):
    """Missing/duplicate checks, non-booleans, or contradictory exit codes deny."""
    if not isinstance(report, dict) or returncode != 0:
        return False
    checks = report.get("results")
    if not isinstance(checks, list) or len(checks) != len(REQUIRED_CHECKS):
        return False
    if any(not isinstance(check, dict) for check in checks):
        return False
    names = [check.get("name") for check in checks]
    if any(not isinstance(name, str) for name in names) or set(names) != REQUIRED_CHECKS:
        return False
    return (report.get("stage") == "A"
            and report.get("gatePassed") is True
            and report.get("productionChanged") is False
            and type(report.get("realModelCalls")) is int
            and report["realModelCalls"] == 0
            and type(report.get("realToolActions")) is int
            and report["realToolActions"] == 0
            and all(check.get("passed") is True for check in checks))


def run_probe(node, packages):
    node = Path(node).expanduser().absolute()
    packages = Path(packages).expanduser().resolve(strict=True)
    if not node.is_file() or not os.access(node, os.X_OK):
        raise ValueError("an explicit executable Node runtime is required")
    script = Path(__file__).with_name("probe_native_dsh.mjs").resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="agentbridge-native-driver-") as scratch:
        # Deliberately do not inherit provider keys, auth sockets, proxy settings,
        # production bridge variables, the user's HOME or DSH_HOME.
        env = {
            "PATH": "/usr/bin:/bin", "HOME": scratch, "DSH_HOME": scratch,
            "TMPDIR": scratch, "ELECTRON_RUN_AS_NODE": "1",
            "AGENTBRIDGE_PROBE_PACKAGES": str(packages),
        }
        result = subprocess.run([str(node), str(script)], cwd=scratch, env=env,
                                capture_output=True, text=True, timeout=45)
        if result.stderr or result.returncode not in (0, 2):
            raise RuntimeError("native probe failed to complete; stage A is not approved")
        report = json.loads(result.stdout)
        if not isinstance(report, dict):
            raise ValueError("native probe did not return a stage report")
        passed = gate_passed(report, result.returncode)
        if report.get("gatePassed") is not passed:
            raise ValueError("native probe returned an inconsistent gate report")
        return report, 0 if passed else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, help="installed pinned Node/Electron executable")
    parser.add_argument("--packages", required=True, help="installed @deepseek-ai package directory")
    args = parser.parse_args()
    try:
        report, code = run_probe(args.node, args.packages)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print("Stage A probe incomplete; no approval or activation.", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
