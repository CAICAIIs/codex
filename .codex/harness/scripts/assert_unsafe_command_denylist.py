#!/usr/bin/env python3
"""Assert the eval runner blocks unsafe commands before they reach a shell."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / ".codex" / "harness" / "scripts" / "run_eval_task.py"
RUNS = ROOT / ".codex" / "harness" / "runs"


def write_task(directory: Path, task_id: str, verifier: list[str]) -> Path:
    path = directory / f"{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": task_id,
                "title": task_id,
                "category": "security",
                "risk_level": "high",
                "instruction": "temporary denylist assertion task",
                "setup": [],
                "verifier": verifier,
                "success_criteria": ["temporary assertion"],
                "forbidden_actions": ["Do not run remote or destructive commands."],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def run_task(
    task_path: Path,
    output_path: Path,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = ["python3", str(RUNNER), str(task_path), "--output", str(output_path)]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def load_last_record(output_path: Path) -> dict:
    lines = [line for line in output_path.read_text(encoding="utf-8").splitlines() if line]
    if not lines:
        raise AssertionError(f"no JSONL records written to {output_path}")
    return json.loads(lines[-1])


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


def assert_blocked_command(temp_dir: Path) -> None:
    task_path = write_task(
        temp_dir,
        "tmp-unsafe-command-denylist",
        ["git push origin main"],
    )
    output_path = temp_dir / "unsafe.jsonl"

    result = run_task(task_path, output_path)

    if result.returncode != 1:
        raise AssertionError(
            "unsafe command task should fail before shell execution\n"
            f"returncode={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    record = load_last_record(output_path)
    command_result = record["results"][0]
    assert record["status"] == "failed", record
    assert command_result["exit_code"] == 125, command_result
    assert command_result["elapsed_ms"] == 0, command_result
    assert "blocked by harness unsafe-command denylist" in command_result["stderr"], command_result


def assert_safe_command_still_runs(temp_dir: Path) -> None:
    task_path = write_task(
        temp_dir,
        "tmp-safe-command",
        ["python3 -c \"print('safe verifier executed')\""],
    )
    output_path = temp_dir / "safe.jsonl"

    result = run_task(
        task_path,
        output_path,
        [
            "--variant",
            "proposed",
            "--human-interventions",
            "0",
            "--modified-files",
            "0",
            "--review-findings",
            "0",
            "--notes",
            "denylist e2e safe command",
            "--route-id",
            "simple-local-change",
            "--model-id",
            "gpt-5.5",
            "--repeat-index",
            "1",
            "--finding-disposition",
            "none",
        ],
    )

    if result.returncode != 0:
        raise AssertionError(
            "safe command task should pass\n"
            f"returncode={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    record = load_last_record(output_path)
    command_result = record["results"][0]
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
    assert record["notes"] == "denylist e2e safe command", record
    assert command_result["exit_code"] == 0, command_result
    assert "safe verifier executed" in command_result["stdout"], command_result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="denylist-e2e-", dir=temp_parent()) as directory:
        temp_dir = Path(directory)
        assert_blocked_command(temp_dir)
        assert_safe_command_still_runs(temp_dir)
    print("unsafe command denylist e2e assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
