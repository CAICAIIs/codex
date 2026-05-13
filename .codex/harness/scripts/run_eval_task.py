#!/usr/bin/env python3
"""Run one CAICAIIs Codex harness eval task and record machine-readable evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / ".codex" / "harness"
TASKS = HARNESS / "evals" / "tasks"
RUNS = HARNESS / "runs"

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{10,}", re.IGNORECASE),
]

SHELL_ARG = r"(?:'[^']*'|\"[^\"]*\"|\S+)"
GIT_PREFIX = rf"\bgit(?:\s+(?:-C|-c)\s+{SHELL_ARG})*\s+"
GH_PREFIX = rf"\bgh(?:\s+(?:-R|--repo)\s+{SHELL_ARG})*\s+"

UNSAFE_COMMAND_PATTERNS = [
    re.compile(GIT_PREFIX + r"push\b"),
    re.compile(GIT_PREFIX + r"tag\b"),
    re.compile(GIT_PREFIX + r"reset\b.*\s--hard\b"),
    re.compile(GH_PREFIX + r"(issue|pr)\s+(comment|edit|close|reopen)\b"),
    re.compile(GH_PREFIX + r"pr\s+(merge|review)\b"),
    re.compile(GH_PREFIX + r"repo\s+sync\b"),
    re.compile(GH_PREFIX + r"api\b.*\b(comments|reviews|releases|git/refs|issues)\b"),
    re.compile(GH_PREFIX + r"release\b"),
]


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def load_task(task_arg: str) -> tuple[Path, dict]:
    path = Path(task_arg)
    if not path.exists():
        path = TASKS / task_arg
    if not path.exists() and not task_arg.endswith(".json"):
        path = TASKS / f"{task_arg}.json"
    if not path.exists():
        for candidate in sorted(TASKS.glob("*.json")):
            try:
                task = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if task.get("id") == task_arg:
                return candidate, task
        raise FileNotFoundError(f"task not found: {task_arg}")
    return path, json.loads(path.read_text(encoding="utf-8"))


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def is_unsafe(command: str) -> bool:
    return any(pattern.search(command) for pattern in UNSAFE_COMMAND_PATTERNS)


def run_command(command: str, timeout: int, workspace: Path | None = None) -> dict:
    started = time.monotonic()
    env = os.environ.copy()
    if workspace is not None:
        env["CODEX_HARNESS_WORKSPACE"] = str(workspace.resolve())
    completed = subprocess.run(
        command,
        cwd=ROOT,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
        check=False,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "command": command,
        "exit_code": completed.returncode,
        "elapsed_ms": elapsed_ms,
        "stdout": redact(completed.stdout[-4000:]),
        "stderr": redact(completed.stderr[-4000:]),
    }


def git_capture(args: list[str]) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_metadata() -> dict:
    status = git_capture(["status", "--short"]) or ""
    numstat = git_capture(["diff", "--numstat", "HEAD"]) or ""
    insertions = 0
    deletions = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or parts[0] == "-" or parts[1] == "-":
            continue
        insertions += int(parts[0])
        deletions += int(parts[1])

    return {
        "commit": git_capture(["rev-parse", "HEAD"]),
        "branch": git_capture(["branch", "--show-current"]),
        "dirty": bool(status),
        "changed_files": len([line for line in status.splitlines() if line]),
        "short_status": status.splitlines()[:50],
        "line_delta": {
            "insertions": insertions,
            "deletions": deletions,
        },
    }


def prepare_workspace(task: dict, workspace: Path) -> None:
    fixture = task.get("workspace_fixture")
    workspace.mkdir(parents=True, exist_ok=True)
    if fixture:
        fixture_path = (ROOT / fixture).resolve()
        if not fixture_path.is_dir():
            raise FileNotFoundError(f"workspace fixture not found: {fixture}")
        shutil.copytree(fixture_path, workspace, dirs_exist_ok=True)
    manifest = {
        "task_id": task["id"],
        "prepared_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "fixture": fixture,
    }
    (workspace / ".codex-harness-workspace.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    initialize_workspace_git(workspace)


def initialize_workspace_git(workspace: Path) -> None:
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Codex Harness"],
        ["git", "config", "user.email", "codex-harness@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "Initial fixture"],
    ]
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = redact(completed.stderr or completed.stdout)
            raise RuntimeError(f"failed to initialize workspace git: {detail[:500]}")


def validate_attempt_evidence(args: argparse.Namespace, task: dict, workspace: Path | None) -> tuple[bool, str]:
    requires_attempt = bool(task.get("requires_attempt_evidence"))
    benchmark_variant = args.variant in {"baseline", "multimodel-lite", "proposed"}
    if not (requires_attempt and benchmark_variant):
        return True, ""

    if not args.verify_only or workspace is None:
        return False, "benchmark task requires --verify-only --workspace"
    if args.skip_reason:
        return True, ""
    if not args.attempt_log:
        return False, "benchmark task requires --skip-reason or readable --attempt-log"

    attempt_log = Path(args.attempt_log)
    if not attempt_log.is_file():
        return False, f"attempt log does not exist or is not a file: {args.attempt_log}"
    try:
        with attempt_log.open("r", encoding="utf-8") as handle:
            handle.read(1)
    except OSError as exc:
        return False, f"attempt log is not readable: {args.attempt_log}: {exc}"
    except UnicodeDecodeError:
        return False, f"attempt log must be UTF-8 text: {args.attempt_log}"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task", help="Eval task id or JSON path")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--run-setup",
        action="store_true",
        help="Run task setup commands before verifier commands.",
    )
    parser.add_argument(
        "--allow-unsafe",
        action="store_true",
        help="Allow commands that match the remote/destructive write denylist.",
    )
    parser.add_argument(
        "--output",
        help="Optional JSONL output path. Defaults to .codex/harness/runs/<date>-<task>.jsonl.",
    )
    parser.add_argument(
        "--variant",
        default="unspecified",
        help="Evaluation variant name, for example baseline, multimodel-lite, or proposed.",
    )
    parser.add_argument("--baseline-run-id", help="Run id this result should be compared against.")
    parser.add_argument("--human-interventions", type=int, help="Human intervention count.")
    parser.add_argument("--token-usage", help="Token/API usage summary, if available.")
    parser.add_argument("--modified-files", type=int, help="Modified file count for this run.")
    parser.add_argument("--review-findings", type=int, help="Reviewer finding count for this run.")
    parser.add_argument("--failure-category", help="Failure category when the task fails.")
    parser.add_argument("--notes", help="Short run note.")
    parser.add_argument("--route-id", help="Harness route id used for this run.")
    parser.add_argument("--model-id", help="Primary model id used for this run.")
    parser.add_argument("--repeat-index", type=int, help="Repeat index for frozen task comparisons.")
    parser.add_argument("--finding-disposition", help="Reviewer finding disposition summary.")
    parser.add_argument("--prepare-only", action="store_true", help="Prepare an isolated task workspace and run setup commands only.")
    parser.add_argument("--verify-only", action="store_true", help="Run verifier commands against an already attempted workspace.")
    parser.add_argument("--workspace", help="Workspace path for prepare/verify benchmark tasks.")
    parser.add_argument("--attempt-log", help="Path to an agent attempt log or transcript.")
    parser.add_argument("--attempt-note", help="Short note describing the agent attempt evidence.")
    parser.add_argument("--skip-reason", help="Record this task as skipped without running commands.")
    args = parser.parse_args()
    if args.prepare_only and args.verify_only:
        print("ERROR: --prepare-only and --verify-only are mutually exclusive", file=sys.stderr)
        return 2

    task_path, task = load_task(args.task)
    workspace = Path(args.workspace) if args.workspace else None
    if (args.prepare_only or args.verify_only) and workspace is None:
        print("ERROR: --workspace is required with --prepare-only or --verify-only", file=sys.stderr)
        return 2
    evidence_ok, evidence_error = validate_attempt_evidence(args, task, workspace)
    if not evidence_ok:
        print(f"ERROR: {evidence_error}", file=sys.stderr)
        return 2

    commands: list[tuple[str, str]] = []
    if not args.skip_reason:
        if args.prepare_only:
            assert workspace is not None
            prepare_workspace(task, workspace)
            commands.extend(("setup", command) for command in task.get("setup", []))
        elif args.verify_only:
            commands.extend(("verifier", command) for command in task.get("verifier", []))
        else:
            if args.run_setup:
                commands.extend(("setup", command) for command in task.get("setup", []))
            commands.extend(("verifier", command) for command in task.get("verifier", []))

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    run_id = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}-{task['id']}"
    output_path = Path(args.output) if args.output else RUNS / f"{run_id}.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    run_started = time.monotonic()
    results = []
    status = "skipped" if args.skip_reason else "passed"
    for phase, command in commands:
        if is_unsafe(command) and not args.allow_unsafe:
            result = {
                "command": command,
                "exit_code": 125,
                "elapsed_ms": 0,
                "stdout": "",
                "stderr": "blocked by harness unsafe-command denylist",
            }
        else:
            try:
                result = run_command(command, args.timeout, workspace)
            except subprocess.TimeoutExpired as exc:
                result = {
                    "command": command,
                    "exit_code": 124,
                    "elapsed_ms": args.timeout * 1000,
                    "stdout": redact((exc.stdout or "")[-4000:]),
                    "stderr": redact((exc.stderr or "")[-4000:]) or "command timed out",
                }
        result["phase"] = phase
        results.append(result)
        if result["exit_code"] != 0:
            status = "failed"
            break

    record = {
        "run_id": run_id,
        "timestamp": now,
        "task_id": task["id"],
        "task_path": display_path(task_path),
        "variant": args.variant,
        "baseline_run_id": args.baseline_run_id,
        "route_id": args.route_id,
        "model_id": args.model_id,
        "repeat_index": args.repeat_index,
        "wall_time_ms": int((time.monotonic() - run_started) * 1000),
        "workspace": str(workspace) if workspace else None,
        "attempt_log": args.attempt_log,
        "attempt_note": redact(args.attempt_note or ""),
        "skip_reason": args.skip_reason,
        "git": git_metadata(),
        "metrics": {
            "human_interventions": args.human_interventions,
            "token_usage": args.token_usage,
            "modified_files": args.modified_files,
            "review_findings": args.review_findings,
            "finding_disposition": args.finding_disposition,
            "failure_category": args.failure_category if status == "failed" else None,
        },
        "notes": redact(args.notes or ""),
        "status": status,
        "results": results,
    }
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if status in {"passed", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
