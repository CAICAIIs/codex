#!/usr/bin/env python3
"""Validate the CAICAIIs Codex harness files.

This script intentionally avoids third-party dependencies so it can run before
the repo-specific Python environment is prepared.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / ".codex" / "harness"
TASKS = HARNESS / "evals" / "tasks"

REQUIRED_FILES = [
    HARNESS / "advisors" / "kimi-readonly-agent" / "agent.yaml",
    HARNESS / "advisors" / "kimi-readonly-agent" / "system.md",
    HARNESS / "advisors" / "empty-skills" / ".keep",
    HARNESS / "model-router.json",
    HARNESS / "model-router.schema.json",
    HARNESS / "provider-availability.json",
    HARNESS / "evals" / "task.schema.json",
    HARNESS / "scripts" / "run_advisor.py",
    HARNESS / "scripts" / "run_paired_benchmark.py",
    HARNESS / "scripts" / "compare_paired_benchmark.py",
    TASKS / "001-harness-self-check.json",
    TASKS / "002-model-router-safety.json",
    TASKS / "003-mm-smoke-dry-run.json",
    TASKS / "004-core-harness-cli.json",
    TASKS / "005-unsafe-command-denylist.json",
]

TASK_REQUIRED_FIELDS = {
    "id",
    "title",
    "category",
    "risk_level",
    "instruction",
    "setup",
    "verifier",
    "success_criteria",
    "forbidden_actions",
}

TASK_ALLOWED_FIELDS = TASK_REQUIRED_FIELDS | {
    "notes",
    "workspace_fixture",
    "requires_attempt_evidence",
}

TASK_ALLOWED_CATEGORIES = {
    "feature",
    "bugfix",
    "cli",
    "debug",
    "security",
    "workflow",
    "docs",
    "harness-contract-feature",
    "harness-contract-cli-env",
    "harness-contract-security",
    "harness-contract-workflow",
    "harness-contract-docs",
    "real-feature-bug",
    "real-cli-env",
    "real-security",
}

SECRET_PATTERNS = [
    re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
]


def error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def check_required_files() -> list[str]:
    failures: list[str] = []
    for path in REQUIRED_FILES:
        if not path.exists():
            failures.append(f"missing required file: {path.relative_to(ROOT)}")
    return failures


def check_no_secrets() -> list[str]:
    failures: list[str] = []
    for path in HARNESS.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(HARNESS).parts
        if (
            "__pycache__" in path.parts
            or ".git" in relative_parts
            or path.suffix == ".pyc"
            or relative_parts[:2] == ("runs", "paired-workspaces")
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"non-UTF-8 file under harness: {path.relative_to(ROOT)}")
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"possible secret in {path.relative_to(ROOT)}")
                break
    return failures


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_task(path: Path, task: dict) -> list[str]:
    failures: list[str] = []
    relative = path.relative_to(ROOT)

    extra = set(task) - TASK_ALLOWED_FIELDS
    if extra:
        failures.append(f"{relative} has unknown fields: {', '.join(sorted(extra))}")

    missing = TASK_REQUIRED_FIELDS - set(task)
    if missing:
        failures.append(f"{relative} missing fields: {', '.join(sorted(missing))}")
        return failures

    if not isinstance(task["id"], str) or not re.match(r"^[a-z0-9][a-z0-9_-]{2,80}$", task["id"]):
        failures.append(f"{relative} id is invalid")
    if not isinstance(task["title"], str) or len(task["title"]) < 3:
        failures.append(f"{relative} title is invalid")
    if task["category"] not in TASK_ALLOWED_CATEGORIES:
        failures.append(f"{relative} category is invalid: {task['category']}")
    if task["risk_level"] not in {"low", "medium", "high"}:
        failures.append(f"{relative} risk_level is invalid")
    if not isinstance(task["instruction"], str) or len(task["instruction"]) < 20:
        failures.append(f"{relative} instruction is too short")

    for field in ("setup", "verifier", "success_criteria", "forbidden_actions"):
        value = task[field]
        if not isinstance(value, list):
            failures.append(f"{relative} {field} must be a list")
            continue
        if field in {"verifier", "success_criteria"} and not value:
            failures.append(f"{relative} {field} must be a non-empty list")
        if not all(isinstance(item, str) for item in value):
            failures.append(f"{relative} {field} entries must be strings")

    if "notes" in task and not isinstance(task["notes"], str):
        failures.append(f"{relative} notes must be a string")

    if "requires_attempt_evidence" in task and not isinstance(task["requires_attempt_evidence"], bool):
        failures.append(f"{relative} requires_attempt_evidence must be a boolean")

    fixture = task.get("workspace_fixture")
    if fixture is not None:
        if not isinstance(fixture, str):
            failures.append(f"{relative} workspace_fixture must be a string")
        else:
            fixture_path = (ROOT / fixture).resolve()
            fixtures_root = (HARNESS / "evals" / "fixtures").resolve()
            if not is_relative_to(fixture_path, fixtures_root):
                failures.append(f"{relative} workspace_fixture must stay under .codex/harness/evals/fixtures")
            if not fixture_path.is_dir():
                failures.append(f"{relative} workspace_fixture does not exist: {fixture}")

    if task.get("requires_attempt_evidence"):
        if not fixture:
            failures.append(f"{relative} requires_attempt_evidence needs workspace_fixture")
        verifiers = task["verifier"]
        task_id = task["id"]
        if not any("assert_real_task.py" in command and task_id in command for command in verifiers):
            failures.append(
                f"{relative} requires_attempt_evidence needs a workspace-aware assert_real_task.py verifier"
            )

    return failures


def check_tasks() -> list[str]:
    failures: list[str] = []
    for path in sorted(TASKS.glob("*.json")):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{path.relative_to(ROOT)} is invalid JSON: {exc}")
            continue

        failures.extend(validate_task(path, task))

    return failures


def check_model_router() -> list[str]:
    failures: list[str] = []
    path = HARNESS / "model-router.json"
    try:
        router = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path.relative_to(ROOT)} is invalid JSON: {exc}"]

    try:
        availability = json.loads((HARNESS / "provider-availability.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        availability = {}
    provider_statuses = {
        provider_id: provider.get("status")
        for provider_id, provider in availability.get("providers", {}).items()
        if isinstance(provider, dict)
    }

    routes = router.get("routes")
    models = router.get("models")
    if not isinstance(models, dict) or not models:
        failures.append("model-router.json must define at least one model")
        models = {}
    if not isinstance(routes, list) or not routes:
        failures.append("model-router.json must define at least one route")
        return failures
    if len(routes) > 4:
        failures.append("model-router.json must keep routes <= 4 until A/B data justifies more")

    model_ids = set(models)
    route_ids: set[str] = set()
    for route in routes:
        route_id = route.get("id", "<missing>")
        enabled = route.get("enabled", True)
        if not isinstance(enabled, bool):
            failures.append(f"route {route_id} enabled must be a boolean when present")
            enabled = True
        if not enabled and not route.get("disabled_reason"):
            failures.append(f"route {route_id} disabled routes must define disabled_reason")

        if route_id in route_ids:
            failures.append(f"duplicate route id in model-router.json: {route_id}")
        route_ids.add(route_id)

        lead = route.get("lead")
        if lead not in model_ids:
            failures.append(f"route {route_id} references unknown lead model: {lead}")
        for advisor in route.get("advisors", []):
            if advisor not in model_ids:
                failures.append(f"route {route_id} references unknown advisor model: {advisor}")
            if provider_statuses.get(advisor) not in (None, "live-current-env") and enabled:
                failures.append(
                    f"route {route_id} must be disabled while advisor {advisor} is {provider_statuses[advisor]}"
                )

        budget = route.get("budget")
        if not isinstance(budget, dict):
            failures.append(f"route {route_id} missing budget object")
        else:
            for key in ("max_wall_minutes", "max_review_rounds", "max_human_interventions"):
                if not isinstance(budget.get(key), int):
                    failures.append(f"route {route_id} budget.{key} must be an integer")
        stop_loss = route.get("stop_loss")
        if not isinstance(stop_loss, list) or not stop_loss:
            failures.append(f"route {route_id} must define stop_loss")

    remote_policy = router.get("remote_write_policy", {})
    if remote_policy.get("default") != "forbid":
        failures.append("model-router.json remote_write_policy.default must be forbid")
    confirmations = remote_policy.get("requires_current_conversation_confirmation", [])
    for required in ("git push", "gh pr comment", "gh pr review"):
        if required not in confirmations:
            failures.append(f"remote write policy must list: {required}")

    return failures


def check_provider_availability() -> list[str]:
    failures: list[str] = []
    path = HARNESS / "provider-availability.json"
    try:
        availability = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path.relative_to(ROOT)} is invalid JSON: {exc}"]

    if availability.get("default_runtime_policy") != "fail-closed":
        failures.append("provider-availability.json default_runtime_policy must be fail-closed")

    providers = availability.get("providers")
    if not isinstance(providers, dict) or not providers:
        failures.append("provider-availability.json must define providers")
        return failures

    for provider_id, provider in providers.items():
        if provider.get("runtime_policy") != "requires-current-live-success":
            failures.append(
                f"provider {provider_id} runtime_policy must be requires-current-live-success"
            )
        if not provider.get("status"):
            failures.append(f"provider {provider_id} missing status")
        if not isinstance(provider.get("safe_env_vars"), list) or not provider["safe_env_vars"]:
            failures.append(f"provider {provider_id} must list safe_env_vars")
        forbidden = provider.get("forbidden_when_unavailable")
        if not isinstance(forbidden, list) or "remote writes" not in forbidden:
            failures.append(
                f"provider {provider_id} must forbid remote writes when unavailable"
            )

    return failures


def main() -> int:
    failures = []
    failures.extend(check_required_files())
    failures.extend(check_no_secrets())
    failures.extend(check_tasks())
    failures.extend(check_model_router())
    failures.extend(check_provider_availability())

    if failures:
        for failure in failures:
            error(failure)
        return 1

    print("harness validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
