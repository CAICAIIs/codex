#!/usr/bin/env python3
"""Assert the Rust `codex harness` CLI is wired to this repository harness."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
LOCAL_CODEX = ROOT / "codex-rs" / "target" / "debug" / "codex"
RUNS = ROOT / ".codex" / "harness" / "runs"


def codex_bin() -> str:
    if LOCAL_CODEX.exists():
        return str(LOCAL_CODEX)
    resolved = shutil.which("codex")
    if resolved:
        return resolved
    raise RuntimeError("missing codex binary; build with cargo test -p codex-cli or cargo build -p codex-cli")


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [codex_bin(), *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def require_success(result: subprocess.CompletedProcess[str], command: str) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"{command} failed with {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def temp_parent() -> Path:
    configured = os.environ.get("CODEX_HARNESS_TEST_TMPDIR", "").strip()
    parent = Path(configured) if configured else RUNS
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="write-check-", dir=parent):
            pass
        return parent
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "codex-harness-tests"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def main() -> int:
    status = run(["harness", "status", "--json"])
    require_success(status, "codex harness status --json")
    status_json = json.loads(status.stdout)
    assert status_json["ok"] is True
    assert status_json["task_count"] >= 5, status_json
    assert status_json["model_count"] >= 4, status_json
    assert status_json["route_count"] == 4, status_json
    assert status_json["enabled_route_count"] == 4, status_json
    assert status_json["remote_write_default"] == "forbid", status_json

    tasks = run(["harness", "tasks"])
    require_success(tasks, "codex harness tasks")
    for task_id in (
        "harness-self-check",
        "core-harness-cli",
        "unsafe-command-denylist",
        "mm-smoke-dry-run",
        "model-router-safety",
    ):
        assert task_id in tasks.stdout, tasks.stdout

    router = run(["harness", "router"])
    require_success(router, "codex harness router")
    for expected in (
        "gpt-5.5",
        "kimi-code",
        "glm-5.1",
        "live-current-env",
        "Remote writes: forbid",
    ):
        assert expected in router.stdout, router.stdout

    router_json = run(["harness", "router", "--json"])
    require_success(router_json, "codex harness router --json")
    router_value = json.loads(router_json.stdout)
    disabled = [route for route in router_value["routes"] if not route["enabled"]]
    assert len(disabled) == 0, router_value
    assert all(
        provider["status"] == "live-current-env"
        for provider in router_value["provider_availability"]
    ), router_value

    validate = run(["harness", "validate"])
    require_success(validate, "codex harness validate")
    assert "harness validation passed" in validate.stdout, validate.stdout

    with tempfile.TemporaryDirectory(prefix="core-cli-e2e-", dir=temp_parent()) as directory:
        output_path = Path(directory) / "run.jsonl"
        run_task = run(
            [
                "harness",
                "run",
                "mm-smoke-dry-run",
                "--variant",
                "proposed",
                "--human-interventions",
                "0",
                "--modified-files",
                "0",
                "--review-findings",
                "0",
                "--notes",
                "core cli metadata e2e",
                "--route-id",
                "simple-local-change",
                "--model-id",
                "gpt-5.5",
                "--repeat-index",
                "1",
                "--finding-disposition",
                "none",
                "--output",
                str(output_path),
            ]
        )
        require_success(run_task, "codex harness run mm-smoke-dry-run --variant proposed")
        records = [
            json.loads(line)
            for line in output_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert records, output_path
        record = records[-1]
        assert record["status"] == "passed", record
        assert record["variant"] == "proposed", record
        assert record["metrics"]["human_interventions"] == 0, record
        assert record["metrics"]["modified_files"] == 0, record
        assert record["metrics"]["review_findings"] == 0, record
        assert record["route_id"] == "simple-local-change", record
        assert record["model_id"] == "gpt-5.5", record
        assert record["repeat_index"] == 1, record
        assert record["metrics"]["finding_disposition"] == "none", record
        assert record["git"]["commit"], record
        assert isinstance(record["git"]["dirty"], bool), record
        assert record["notes"] == "core cli metadata e2e", record

    print("core harness CLI assertions passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
