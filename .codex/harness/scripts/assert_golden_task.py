#!/usr/bin/env python3
"""Verifier cases for the frozen CAICAIIs Codex harness golden task set."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import run_eval_task


ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / ".codex" / "harness"
TASKS = HARNESS / "evals" / "tasks"


def codex_bin() -> str:
    local = ROOT / "codex-rs" / "target" / "debug" / "codex"
    if local.exists():
        return str(local)
    resolved = shutil.which("codex")
    if resolved:
        return resolved
    raise AssertionError("missing codex binary")


def run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=merged_env,
        check=False,
    )


def require_success(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"{label} failed with {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def assert_text(path: Path, *needles: str) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        assert needle in text, f"{needle!r} missing from {path}"


def temp_dir() -> tempfile.TemporaryDirectory[str]:
    parent = Path(os.environ.get("CODEX_HARNESS_TEST_TMPDIR", tempfile.gettempdir()))
    parent.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="golden-task-", dir=parent)


def core_status_contract() -> None:
    result = run([codex_bin(), "harness", "status", "--json"])
    require_success(result, "codex harness status --json")
    value = json.loads(result.stdout)
    assert value["ok"] is True, value
    assert value["task_count"] >= 23, value
    assert value["enabled_route_count"] == 4, value
    assert value["remote_write_default"] == "forbid", value


def router_fail_closed_contract() -> None:
    result = run([codex_bin(), "harness", "router", "--json"])
    require_success(result, "codex harness router --json")
    value = json.loads(result.stdout)
    disabled = [route for route in value["routes"] if not route["enabled"]]
    assert len(disabled) == 0, value
    assert {
        provider["id"]: provider["status"] for provider in value["provider_availability"]
    } == {"glm-5.1": "live-current-env", "kimi-code": "live-current-env"}, value


def eval_record_metadata_contract() -> None:
    with temp_dir() as directory:
        task_path = Path(directory) / "metadata-task.json"
        output_path = Path(directory) / "metadata.jsonl"
        task_path.write_text(
            json.dumps(
                {
                    "id": "metadata-contract",
                    "title": "metadata contract",
                    "category": "golden",
                    "risk_level": "low",
                    "instruction": "metadata contract",
                    "setup": [],
                    "verifier": ["python3 -c \"print('metadata ok')\""],
                    "success_criteria": ["metadata exists"],
                    "forbidden_actions": ["Do not push."],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = run(
            [
                "python3",
                str(HARNESS / "scripts" / "run_eval_task.py"),
                str(task_path),
                "--output",
                str(output_path),
                "--variant",
                "baseline",
                "--route-id",
                "simple-local-change",
                "--model-id",
                "gpt-5.5",
                "--repeat-index",
                "1",
                "--finding-disposition",
                "none",
            ]
        )
        require_success(result, "metadata contract eval")
        record = json.loads(output_path.read_text(encoding="utf-8").splitlines()[-1])
        assert record["variant"] == "baseline", record
        assert record["route_id"] == "simple-local-change", record
        assert record["model_id"] == "gpt-5.5", record
        assert record["repeat_index"] == 1, record
        assert record["wall_time_ms"] >= 0, record
        assert record["git"]["commit"], record


def provider_availability_contract() -> None:
    value = read_json(HARNESS / "provider-availability.json")
    assert value["default_runtime_policy"] == "fail-closed", value
    for provider in value["providers"].values():
        assert provider["runtime_policy"] == "requires-current-live-success", provider
        assert provider["status"] in {"blocked-current-env", "live-current-env"}, provider


def denylist_policy_contract() -> None:
    router = read_json(HARNESS / "model-router.json")
    required = router["remote_write_policy"]["requires_current_conversation_confirmation"]
    examples = {
        "git push": "git -C . push origin main",
        "gh issue comment": "gh issue comment 1 --body hello",
        "gh pr comment": "gh pr comment 1 --body hello",
        "gh pr review": "gh pr review 1 --approve",
        "merge": "gh -R owner/repo pr merge 1 --merge",
        "tag": "git -C . tag v1.2.3",
        "release": "gh --repo owner/repo release create v1.2.3",
    }
    for policy in required:
        if policy in examples:
            assert run_eval_task.is_unsafe(examples[policy]), policy


def validate_harness_command() -> None:
    require_success(run(["python3", str(HARNESS / "scripts" / "validate_harness.py")]), "validate")


def mm_smoke_dry_run_command() -> None:
    result = run(["python3", str(HARNESS / "scripts" / "mm_chat_smoke.py"), "glm", "--dry-run"])
    require_success(result, "mm dry-run")
    assert "GLM_5_1_API_KEY" in result.stdout, result.stdout


def python_units_command() -> None:
    require_success(
        run(["python3", "-m", "unittest", "discover", "-s", ".codex/harness/scripts", "-p", "test_*.py"]),
        "python units",
    )


def task_inventory_contract() -> None:
    task_ids = [read_json(path)["id"] for path in TASKS.glob("*.json")]
    assert len(task_ids) >= 23, task_ids
    assert len(task_ids) == len(set(task_ids)), task_ids


def runner_output_dir_contract() -> None:
    with temp_dir() as directory:
        env = {"CODEX_HARNESS_TEST_TMPDIR": directory}
        require_success(run(["python3", str(HARNESS / "scripts" / "assert_unsafe_command_denylist.py")], env=env), "restricted temp denylist")


def secret_scan_clean() -> None:
    scan_paths = [
        ".codex/harness",
        "codex-rs/cli/src/harness_advisor_cmd.rs",
        "codex-rs/cli/src/harness_cmd.rs",
        "codex-rs/cli/tests/harness.rs",
    ]
    if (ROOT / ".humanize").exists():
        scan_paths.append(".humanize")
    result = run(
        [
            "rg",
            "-n",
            "-P",
            r"(?<![A-Za-z])sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}",
            *scan_paths,
        ]
    )
    assert result.returncode == 1, result.stdout


def unsafe_gh_pr_merge_blocked() -> None:
    assert run_eval_task.is_unsafe("gh pr merge 123 --merge")
    assert run_eval_task.is_unsafe("gh -R owner/repo pr merge 123 --merge")
    assert run_eval_task.is_unsafe("gh --repo owner/repo pr comment 1 --body hello")
    assert run_eval_task.is_unsafe("gh --repo owner/repo release create v1.2.3")
    assert run_eval_task.is_unsafe("git -C . tag v1.2.3")
    assert run_eval_task.is_unsafe("git -c user.name=x tag v1.2.3")
    assert run_eval_task.is_unsafe("bash -lc 'gh pr merge 123 --squash'")
    assert run_eval_task.is_unsafe("bash -lc 'gh --repo owner/repo release create v1.2.3'")


def restricted_temp_repro() -> None:
    with temp_dir() as directory:
        env = {"CODEX_HARNESS_TEST_TMPDIR": directory}
        require_success(run(["python3", str(HARNESS / "scripts" / "assert_core_harness_cli.py")], env=env), "restricted temp core cli")


CHECKS = {
    "golden-001-core-status-contract": core_status_contract,
    "golden-002-router-fail-closed-contract": router_fail_closed_contract,
    "golden-003-eval-record-metadata-contract": eval_record_metadata_contract,
    "golden-004-provider-availability-contract": provider_availability_contract,
    "golden-005-denylist-policy-contract": denylist_policy_contract,
    "golden-011-validate-harness-command": validate_harness_command,
    "golden-012-mm-smoke-dry-run-command": mm_smoke_dry_run_command,
    "golden-013-python-units-command": python_units_command,
    "golden-014-task-inventory-contract": task_inventory_contract,
    "golden-015-runner-output-dir-contract": runner_output_dir_contract,
    "golden-016-secret-scan-clean": secret_scan_clean,
    "golden-017-unsafe-gh-pr-merge-blocked": unsafe_gh_pr_merge_blocked,
    "golden-018-restricted-temp-repro": restricted_temp_repro,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in CHECKS:
        print("usage: assert_golden_task.py <golden-task-id>", file=sys.stderr)
        print("\n".join(sorted(CHECKS)), file=sys.stderr)
        return 2
    CHECKS[sys.argv[1]]()
    print(f"{sys.argv[1]} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
