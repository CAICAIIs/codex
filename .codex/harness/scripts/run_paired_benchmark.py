#!/usr/bin/env python3
"""Run paired agent benchmarks for GPT-5.5 baseline vs read-only advisors.

This runner follows the same shape as public agent eval practice: prepare an
isolated task workspace, run the agent while capturing a JSONL trace, run the
same verifier, then record a machine-readable attempt artifact. The paired
design changes one thing at a time: the lead model stays GPT-5.5 and only the
multimodel variant receives read-only advisor artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import run_eval_task


ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / ".codex" / "harness"
RUNS = HARNESS / "runs"
DEFAULT_TASKS = [
    "real-001-readme-title-fix",
    "real-004-redaction-bearer-token",
    "real-018-unrelated-file-deletion-guard",
]
DEFAULT_VARIANTS = ["baseline", "multimodel-lite"]
SUPPORTED_VARIANTS = {"baseline", "multimodel-lite", "subagent-lite"}
PROVIDER_ENV_NAMES = ["KIMI_CODE_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY", "GLM_5_1_API_KEY"]
PROVIDER_ENV_PREFIXES = ("KIMI_", "MOONSHOT_", "GLM_5_1_")
ADVISOR_EVIDENCE_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".py",
    ".rs",
    ".js",
    ".ts",
    ".tsx",
}
SENSITIVE_EVIDENCE_NAMES = {
    ".env",
    ".npmrc",
    ".netrc",
}
SENSITIVE_EVIDENCE_PARTS = ("secret", "credential", "credentials", "token", "private-key")


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def codex_bin() -> str:
    local = ROOT / "codex-rs" / "target" / "debug" / "codex"
    if local.exists():
        return str(local)
    resolved = shutil.which("codex")
    if resolved:
        return resolved
    raise RuntimeError("missing codex binary; build with: cargo build -p codex-cli")


def parse_list(value: str | None, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    parsed = [item.strip() for item in value.split(",") if item.strip()]
    if not parsed:
        raise ValueError("list option cannot be empty")
    return parsed


def load_login_shell_env(names: list[str]) -> dict[str, str]:
    code = (
        "import json, os; "
        f"names={names!r}; "
        "print(json.dumps({name: os.environ.get(name, '') for name in names}))"
    )
    completed = subprocess.run(
        ["zsh", "-lic", f"python3 -c {json.dumps(code)}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    loaded = {}
    if completed.returncode != 0:
        return loaded
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return loaded
    for name, value in values.items():
        if value:
            loaded[name] = value
    return loaded


def scrub_provider_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if key not in PROVIDER_ENV_NAMES and not key.startswith(PROVIDER_ENV_PREFIXES)
    }


def advisor_env(provider_env: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(provider_env)
    return env


def redact_runtime(text: str, provider_env: dict[str, str] | None = None) -> str:
    redacted = run_eval_task.redact(text)
    redacted = redacted.replace(str(ROOT), ".")
    for value in (provider_env or {}).values():
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def workspace_snapshot(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        if ".git" in path.relative_to(workspace).parts:
            continue
        snapshot[str(path.relative_to(workspace))] = sha256_file(path)
    return snapshot


def diff_snapshot(before: dict[str, str], after: dict[str, str]) -> dict:
    before_keys = set(before)
    after_keys = set(after)
    modified = sorted(key for key in before_keys & after_keys if before[key] != after[key])
    added = sorted(after_keys - before_keys)
    deleted = sorted(before_keys - after_keys)
    return {
        "modified": modified,
        "added": added,
        "deleted": deleted,
        "changed_files": len(modified) + len(added) + len(deleted),
    }


def task_prompt(task: dict, workspace: Path, advisors: list[dict], variant: str) -> str:
    task_file = workspace / "TASK.md"
    task_text = read_text(task_file) if task_file.exists() else task["instruction"]
    advisor_block = ""
    if advisors:
        advisor_parts = []
        for advisor in advisors:
            if advisor["status"] != "passed":
                continue
            content = advisor.get("content", "")
            advisor_parts.append(
                f"\n--- advisor {advisor['provider']} / {advisor['role']} ---\n{content}\n"
            )
        if advisor_parts:
            advisor_block = (
                "\n只读顾问建议如下。你可以采纳或拒绝，但最终必须以 workspace "
                "里的实际验证为准，并在最终回答中简短说明采纳/拒绝：\n"
                + "".join(advisor_parts)
            )

    subagent_block = ""
    if variant == "subagent-lite":
        subagent_block = """
SubAgent 协作要求：
- 你必须先 spawn exactly one explorer subagent，让它只读检查任务理解、风险和最窄验证。
- 调用 spawn_agent 时设置 agent_type="explorer"，不要设置 fork_context、model 或 reasoning_effort。
- subagent 不能实现、不能修改文件、不能运行破坏性命令；它只返回建议。
- 等待 subagent 完成后再修改文件。
- 你可以采纳或拒绝 subagent 建议，但最终必须以 workspace 的实际验证为准。
- 如果 subagent 工具不可用或只读边界无法满足，停止并在最终回答中说明，不要降级为 worker/default，也不要假装用了 subagent。
"""

    return f"""你正在参加 CAICAIIs Codex paired benchmark。

任务：
{task_text}

工作区：
{workspace}

规则：
- 只修改这个 isolated workspace 里的文件。
- 不要 push，不要发 GitHub 评论，不要读取或输出密钥。
- 不要读取 benchmark verifier 源码；按任务文本和本地文件完成需求。
- 完成后尽量运行你认为最窄的本地检查。
- 最终回答用中文，包含：改了什么、运行了什么检查、还有什么风险。
{advisor_block}
{subagent_block}
"""


def parse_trace(path: Path) -> dict:
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    event_counts: dict[str, int] = {}
    item_type_counts: dict[str, int] = {}
    collab_tool_counts: dict[str, int] = {}
    errors = []
    final_messages = []
    turn_failed = False
    seen_collab_started_ids: set[str] = set()
    seen_collab_completed_ids: set[str] = set()
    collab_tool_attempt_counts: dict[str, int] = {}
    if not path.exists():
        return {
            "usage": usage,
            "event_counts": event_counts,
            "item_type_counts": item_type_counts,
            "collab_tool_counts": {},
            "collab_tool_attempt_counts": {},
            "errors": ["missing trace"],
            "final_messages": [],
            "turn_failed": True,
            "subagent_spawned": False,
        }

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            errors.append("invalid jsonl event")
            continue
        event_type = event.get("type", "unknown")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        if event_type == "turn.completed":
            for key, value in event.get("usage", {}).items():
                if key in usage and isinstance(value, int):
                    usage[key] += value
        elif event_type == "turn.failed":
            turn_failed = True
            errors.append(redact_runtime(str(event.get("error", {}).get("message", "turn failed")))[:500])
        elif event_type == "error":
            errors.append(redact_runtime(str(event.get("message", "error")))[:500])
        elif event_type in {"item.started", "item.completed"}:
            item = event.get("item", {})
            item_type = item.get("type", "unknown")
            if event_type == "item.completed":
                item_type_counts[item_type] = item_type_counts.get(item_type, 0) + 1
            if item.get("type") == "agent_message" and item.get("text") and event_type == "item.completed":
                final_messages.append(redact_runtime(item["text"]))
            elif item.get("type") == "collab_tool_call":
                tool = item.get("tool", "unknown")
                item_id = item.get("id")
                if event_type == "item.started":
                    if isinstance(item_id, str) and item_id in seen_collab_started_ids:
                        continue
                    if isinstance(item_id, str):
                        seen_collab_started_ids.add(item_id)
                    collab_tool_attempt_counts[tool] = collab_tool_attempt_counts.get(tool, 0) + 1
                else:
                    if isinstance(item_id, str) and item_id in seen_collab_completed_ids:
                        continue
                    if isinstance(item_id, str):
                        seen_collab_completed_ids.add(item_id)
                    collab_tool_counts[tool] = collab_tool_counts.get(tool, 0) + 1
    return {
        "usage": usage,
        "event_counts": event_counts,
        "item_type_counts": item_type_counts,
        "collab_tool_counts": collab_tool_counts,
        "collab_tool_attempt_counts": collab_tool_attempt_counts,
        "errors": errors[-5:],
        "final_messages": final_messages[-3:],
        "turn_failed": turn_failed,
        "subagent_spawned": collab_tool_counts.get("spawn_agent", 0) > 0,
    }


def write_redacted(path: Path, text: str, provider_env: dict[str, str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_runtime(text, provider_env), encoding="utf-8")


def run_subprocess(args: list[str], cwd: Path, timeout: int, stdout_path: Path, stderr_path: Path) -> dict:
    started = time.monotonic()
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                text=True,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
                env=scrub_provider_env(os.environ.copy()),
                check=False,
            )
            exit_code = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = 124
            timed_out = True
    # Rewrite through redaction in case a provider printed a token-like value.
    write_redacted(stdout_path, stdout_path.read_text(encoding="utf-8", errors="replace"))
    write_redacted(stderr_path, stderr_path.read_text(encoding="utf-8", errors="replace"))
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def run_advisor_once(
    provider: str,
    role: str,
    topic: str,
    evidence: list[Path],
    output: Path,
    timeout: int,
    provider_env: dict[str, str],
) -> dict:
    command = [
        sys.executable,
        str(HARNESS / "scripts" / "run_advisor.py"),
        provider,
        "--role",
        role,
        "--topic",
        topic,
        "--output",
        str(output),
        "--timeout",
        str(timeout),
        "--max-tokens",
        "900",
    ]
    for item in evidence:
        command.extend(["--evidence", str(item)])
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout + 10,
        env=advisor_env(provider_env),
        check=False,
    )
    if output.exists():
        artifact = json.loads(output.read_text(encoding="utf-8"))
    else:
        artifact = {
            "status": "failed",
            "provider": provider,
            "role": role,
            "content": "",
            "error": completed.stderr or completed.stdout,
        }
    return {
        "provider": provider,
        "role": role,
        "status": artifact.get("status"),
        "path": display_path(output),
        "latency_ms": artifact.get("latency_ms") or int((time.monotonic() - started) * 1000),
        "transport": artifact.get("transport"),
        "usage": artifact.get("usage"),
        "content_chars": artifact.get("content_chars", 0),
        "content": artifact.get("content", "")[:4000],
        "error": redact_runtime(artifact.get("error", ""), provider_env),
    }


def attempt_output_path(output: Path, attempt: int, attempts: int) -> Path:
    if attempts <= 1:
        return output
    return output.with_name(f"{output.stem}-attempt-{attempt}{output.suffix}")


def run_advisor(
    provider: str,
    role: str,
    topic: str,
    evidence: list[Path],
    output: Path,
    timeout: int,
    attempts: int,
    provider_env: dict[str, str],
) -> dict:
    attempt_records = []
    last_result = None
    for attempt in range(1, max(1, attempts) + 1):
        result = run_advisor_once(
            provider,
            role,
            topic,
            evidence,
            attempt_output_path(output, attempt, attempts),
            timeout,
            provider_env,
        )
        result["attempt_index"] = attempt
        attempt_records.append({key: value for key, value in result.items() if key != "content"})
        last_result = result
        if result["status"] == "passed":
            result["attempts"] = attempt_records
            return result

    assert last_result is not None
    last_result["attempts"] = attempt_records
    return last_result


def advisor_plan(
    task: dict,
    workspace: Path,
    run_dir: Path,
    timeout: int,
    include_glm: bool,
    required_attempts: int,
    optional_attempts: int,
    provider_env: dict[str, str],
) -> list[dict]:
    evidence = advisor_evidence_paths(workspace)
    topic = f"为 paired benchmark 任务 {task['id']} 提供只读建议，重点指出需求、风险和最窄验证。"
    advisors = [
        run_advisor(
            "kimi-cli",
            "request-decoder",
            topic,
            evidence,
            run_dir / "advisor-kimi-cli.json",
            timeout,
            required_attempts,
            provider_env,
        )
    ]
    if include_glm:
        advisors.append(
            run_advisor(
                "glm",
                "critic",
                topic,
                evidence,
                run_dir / "advisor-glm.json",
                timeout,
                optional_attempts,
                provider_env,
            )
        )
    return advisors


def advisor_evidence_paths(workspace: Path, limit: int = 6) -> list[Path]:
    evidence = []
    task_file = workspace / "TASK.md"
    if task_file.exists():
        evidence.append(task_file)
    for path in sorted(workspace.rglob("*")):
        if len(evidence) >= limit:
            break
        if not path.is_file() or path == task_file:
            continue
        if ".git" in path.relative_to(workspace).parts:
            continue
        if path.name == ".codex-harness-workspace.json":
            continue
        lowered_parts = {part.lower() for part in path.parts}
        if lowered_parts & SENSITIVE_EVIDENCE_NAMES:
            continue
        lowered_name = path.name.lower()
        if any(part in lowered_name for part in SENSITIVE_EVIDENCE_PARTS):
            continue
        if path.suffix.lower() not in ADVISOR_EVIDENCE_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 20_000:
            continue
        evidence.append(path)
    return evidence


def summarize_advisor(advisor: dict) -> dict:
    return {key: value for key, value in advisor.items() if key != "content"}


def advisor_token_usage(advisors: list[dict]) -> dict[str, int]:
    totals = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for advisor in advisors:
        usage = advisor.get("usage") or {}
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] += value
    return totals


def model_id_for_variant(variant: str, advisors: list[dict]) -> str:
    if variant == "baseline":
        return "gpt-5.5"
    if variant == "subagent-lite":
        return "gpt-5.5+subagent-lite"
    passed = [advisor["provider"] for advisor in advisors if advisor.get("status") == "passed"]
    if not passed:
        return "gpt-5.5+no-advisor"
    return "gpt-5.5+" + "+".join(passed)


def required_advisors_satisfied(advisors: list[dict], required: list[str]) -> bool:
    if not required:
        return any(advisor["status"] == "passed" for advisor in advisors)
    for provider in required:
        if not any(
            advisor["provider"] == provider and advisor["status"] == "passed"
            for advisor in advisors
        ):
            return False
    return True


def verify_attempt(
    task: dict,
    workspace: Path,
    trace_path: Path,
    output_path: Path,
    variant: str,
    repeat: int,
    diff: dict,
    usage: dict,
    model_id: str,
) -> tuple[int, dict | None]:
    token_usage = json.dumps(usage, ensure_ascii=False, sort_keys=True)
    command = [
        sys.executable,
        str(HARNESS / "scripts" / "run_eval_task.py"),
        task["id"],
        "--verify-only",
        "--workspace",
        str(workspace),
        "--attempt-log",
        str(trace_path),
        "--variant",
        variant,
        "--route-id",
        "workflow-evaluation",
        "--model-id",
        model_id,
        "--repeat-index",
        str(repeat),
        "--human-interventions",
        "0",
        "--modified-files",
        str(diff["changed_files"]),
        "--review-findings",
        "0",
        "--finding-disposition",
        "none",
        "--token-usage",
        token_usage,
        "--output",
        str(output_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=scrub_provider_env(os.environ.copy()),
        check=False,
    )
    record = None
    if output_path.exists():
        lines = [line for line in output_path.read_text(encoding="utf-8").splitlines() if line]
        if lines:
            record = json.loads(lines[-1])
    return completed.returncode, record


def build_schedule(tasks: list[str], variants: list[str], repeats: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    schedule = []
    for repeat in range(1, repeats + 1):
        for task_id in tasks:
            pair = list(variants)
            rng.shuffle(pair)
            for variant in pair:
                schedule.append({"task_id": task_id, "variant": variant, "repeat": repeat})
    return schedule


def run_attempt(args: argparse.Namespace, run_id: str, output_path: Path, item: dict) -> dict:
    task_path, task = run_eval_task.load_task(item["task_id"])
    variant = item["variant"]
    repeat = item["repeat"]
    attempt_id = f"{repeat:02d}-{task['id']}-{variant}"
    run_dir = RUNS / "paired-workspaces" / run_id / attempt_id
    workspace = run_dir / "workspace"
    trace_path = run_dir / "codex-trace.jsonl"
    stderr_path = run_dir / "codex-stderr.log"
    final_path = run_dir / "final.md"
    verify_output = run_dir / "verify.jsonl"
    if args.dry_run:
        return {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "task_id": task["id"],
            "task_path": display_path(task_path),
            "variant": variant,
            "repeat_index": repeat,
            "workspace": display_path(workspace),
            "status": "dry-run",
        }

    run_dir.mkdir(parents=True, exist_ok=True)
    run_eval_task.prepare_workspace(task, workspace)
    before = workspace_snapshot(workspace)
    advisors: list[dict] = []
    if variant == "multimodel-lite" and not args.dry_run:
        advisors = advisor_plan(
            task,
            workspace,
            run_dir,
            args.advisor_timeout,
            not args.no_glm,
            args.required_advisor_attempts,
            args.optional_advisor_attempts,
            args.provider_env,
        )

    prompt = task_prompt(task, workspace, advisors, variant)
    prompt_path = run_dir / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    record = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "task_id": task["id"],
        "task_path": display_path(task_path),
        "variant": variant,
        "repeat_index": repeat,
        "workspace": display_path(workspace),
        "prompt_path": display_path(prompt_path),
        "prompt_sha256": sha256_text(prompt),
        "advisor_artifacts": [summarize_advisor(advisor) for advisor in advisors],
        "status": "running",
    }

    required_advisors = parse_list(args.required_advisors, ["kimi-cli"])
    if (
        variant == "multimodel-lite"
        and args.require_advisor_success
        and not required_advisors_satisfied(advisors, required_advisors)
    ):
        record["status"] = "inconclusive"
        record["failure_category"] = "required_advisor_unavailable"
        record["required_advisors"] = required_advisors
        return record

    codex_command = [
        args.codex_bin,
        "exec",
        "--json",
        "-C",
        str(workspace),
        "--skip-git-repo-check",
        "-s",
        args.sandbox,
        "-m",
        args.model,
        "--output-last-message",
        str(final_path),
        prompt,
    ]
    if variant == "subagent-lite":
        codex_command[2:2] = ["--enable", "multi_agent_v2"]
    codex_result = run_subprocess(codex_command, workspace, args.timeout, trace_path, stderr_path)
    after = workspace_snapshot(workspace)
    diff = diff_snapshot(before, after)
    trace = parse_trace(trace_path)
    verify_exit = None
    verify_record = None
    spawn_completed = trace["collab_tool_counts"].get("spawn_agent", 0)
    spawn_attempted = trace["collab_tool_attempt_counts"].get("spawn_agent", 0)
    subagent_missing = variant == "subagent-lite" and not trace["subagent_spawned"]
    subagent_not_exactly_one = variant == "subagent-lite" and (
        spawn_completed != 1 or spawn_attempted != 1
    )
    subagent_write_boundary_unverified = (
        variant == "subagent-lite"
        and diff["changed_files"] > 0
        and trace["item_type_counts"].get("file_change", 0) == 0
    )
    if (
        codex_result["exit_code"] == 0
        and not trace["turn_failed"]
        and not subagent_missing
        and not subagent_not_exactly_one
    ):
        verify_exit, verify_record = verify_attempt(
            task,
            workspace,
            trace_path,
            verify_output,
            variant,
            repeat,
            diff,
            trace["usage"],
            model_id_for_variant(variant, advisors),
        )
    if subagent_missing:
        status = "inconclusive"
        failure_category = "subagent_not_spawned"
    elif subagent_not_exactly_one:
        status = "inconclusive"
        failure_category = "subagent_not_exactly_one"
    elif subagent_write_boundary_unverified:
        status = "inconclusive"
        failure_category = "subagent_write_boundary_unverified"
    else:
        status = "passed" if verify_record and verify_record.get("status") == "passed" else "failed"
        failure_category = None
    record.update(
        {
            "status": status,
            "failure_category": failure_category,
            "codex": {
                **codex_result,
                "trace_path": display_path(trace_path),
                "stderr_path": display_path(stderr_path),
                "final_path": display_path(final_path),
                "trace": trace,
            },
            "workspace_diff": diff,
            "verify": {
                "exit_code": verify_exit,
                "output": display_path(verify_output),
                "status": verify_record.get("status") if verify_record else None,
            },
            "metrics": {
                "agent_wall_time_ms": codex_result["elapsed_ms"],
                "advisor_wall_time_ms": sum(advisor.get("latency_ms") or 0 for advisor in advisors),
                "total_wall_time_ms": codex_result["elapsed_ms"]
                + sum(advisor.get("latency_ms") or 0 for advisor in advisors),
                "token_usage": trace["usage"],
                "advisor_token_usage": advisor_token_usage(advisors),
                "modified_files": diff["changed_files"],
                "human_interventions": 0,
                "advisor_passed": sum(1 for advisor in advisors if advisor["status"] == "passed"),
                "advisor_failed": sum(1 for advisor in advisors if advisor["status"] != "passed"),
                "subagent_spawned": trace["subagent_spawned"],
                "subagent_spawn_completed": spawn_completed,
                "subagent_spawn_attempted": spawn_attempted,
                "subagent_exactly_one": variant != "subagent-lite" or not subagent_not_exactly_one,
                "subagent_readonly_boundary_enforced": False if variant == "subagent-lite" else None,
                "subagent_tool_calls": trace["collab_tool_counts"],
                "subagent_tool_attempts": trace["collab_tool_attempt_counts"],
                "subagent_write_boundary_unverified": subagent_write_boundary_unverified,
            },
        }
    )
    return record


def write_record(output_path: Path, record: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", help="Comma-separated task ids. Defaults to a 3-task smoke matrix.")
    parser.add_argument("--variants", default="baseline,multimodel-lite")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260512)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--codex-bin")
    parser.add_argument("--sandbox", default="workspace-write")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--advisor-timeout", type=int, default=150)
    parser.add_argument("--required-advisors", default="kimi-cli")
    parser.add_argument("--required-advisor-attempts", type=int, default=2)
    parser.add_argument("--optional-advisor-attempts", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-glm", action="store_true")
    parser.add_argument("--no-load-login-env", action="store_true")
    parser.add_argument("--require-advisor-success", action="store_true", default=True)
    parser.add_argument("--no-require-advisor-success", dest="require_advisor_success", action="store_false")
    args = parser.parse_args()

    tasks = parse_list(args.tasks, DEFAULT_TASKS)
    variants = parse_list(args.variants, DEFAULT_VARIANTS)
    if args.repeats < 1:
        print("ERROR: --repeats must be >= 1", file=sys.stderr)
        return 2
    if args.required_advisor_attempts < 1 or args.optional_advisor_attempts < 1:
        print("ERROR: advisor attempt counts must be >= 1", file=sys.stderr)
        return 2
    if not args.codex_bin:
        args.codex_bin = "" if args.dry_run else codex_bin()
    unknown_variants = set(variants) - SUPPORTED_VARIANTS
    if unknown_variants:
        print(f"ERROR: unsupported variants: {', '.join(sorted(unknown_variants))}", file=sys.stderr)
        return 2
    args.provider_env = {} if args.no_load_login_env else load_login_shell_env(PROVIDER_ENV_NAMES)
    loaded_env_names = sorted(args.provider_env)

    run_id = f"{now_stamp()}-paired-benchmark"
    output_path = args.output or RUNS / f"{run_id}.jsonl"
    schedule = build_schedule(tasks, variants, args.repeats, args.seed)
    header = {
        "type": "benchmark-started",
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "tasks": tasks,
        "variants": variants,
        "repeats": args.repeats,
        "seed": args.seed,
        "model": args.model,
        "dry_run": args.dry_run,
        "method": "paired-randomized-order",
        "loaded_env_names": loaded_env_names,
        "config": {
            "codex_bin": display_path(Path(args.codex_bin)) if args.codex_bin else "",
            "sandbox": args.sandbox,
            "timeout": args.timeout,
            "advisor_timeout": args.advisor_timeout,
            "no_glm": args.no_glm,
            "kimi_sdk_fallback_enabled": bool(os.environ.get("KIMI_ADVISOR_ENABLE_SDK_FALLBACK")),
            "required_advisors": args.required_advisors,
            "required_advisor_attempts": args.required_advisor_attempts,
            "optional_advisor_attempts": args.optional_advisor_attempts,
            "require_advisor_success": args.require_advisor_success,
        },
        "benchmark_kind": "smoke" if args.repeats == 1 else "matrix",
    }
    write_record(output_path, header)

    exit_code = 0
    for item in schedule:
        record = run_attempt(args, run_id, output_path, item)
        write_record(output_path, record)
        if record["status"] in {"failed", "inconclusive"}:
            exit_code = 1

    footer = {
        "type": "benchmark-completed",
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "output": display_path(output_path),
        "exit_code": exit_code,
    }
    write_record(output_path, footer)
    print(json.dumps(footer, ensure_ascii=False, indent=2))
    return 0 if args.dry_run else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
