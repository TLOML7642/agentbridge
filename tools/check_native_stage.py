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
    "boundary_allows_approved_action_once",
    "boundary_reads_need_no_approval",
    "boundary_writes_default_off",
    "boundary_denies_missing_or_foreign_identity",
    "boundary_denies_unwrapped_registration",
    "boundary_registry_replacement_revokes_inflight",
    "boundary_scoped_shadow_revokes_inflight",
    "boundary_rechecks_after_async_consumption",
    "boundary_cancel_cannot_be_replaced",
    "boundary_denies_stale_changed_or_missing_grants",
    "boundary_pre_hook_cannot_discard_denial",
    "boundary_nested_dispatch_cannot_inherit_grant",
    "boundary_seals_all_execution_identity_fields",
    "boundary_pre_hook_parameter_change_denied",
    "boundary_pending_approval_cancel_discards_late_allow",
    "boundary_consumption_failure_spends_execution",
    "boundary_expiry_rechecked_before_action",
    "boundary_one_approval_id_cannot_authorize_two_calls",
    "boundary_action_can_only_enter_once",
    "boundary_disabled_services_and_command_bypasses_denied",
    "boundary_pins_and_rechecks_native_subject",
    "boundary_directory_replacement_denied",
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
            and report.get("boundary") == "protected"
            and report.get("gatePassed") is True
            and report.get("productionChanged") is False
            and type(report.get("realModelCalls")) is int
            and report["realModelCalls"] == 0
            and type(report.get("realToolActions")) is int
            and report["realToolActions"] == 0
            and all(check.get("passed") is True for check in checks))


def run_probe(node, packages, native_only=False):
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
        command = [str(node), str(script)] + (["--native-only"] if native_only else [])
        result = subprocess.run(command, cwd=scratch, env=env,
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
    parser.add_argument("--native-only", action="store_true", help="unprotected diagnostic control; never release approval")
    args = parser.parse_args()
    try:
        report, code = run_probe(args.node, args.packages, args.native_only)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print("Stage A probe incomplete; no approval or activation.", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
