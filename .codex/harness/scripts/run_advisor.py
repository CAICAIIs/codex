#!/usr/bin/env python3
"""Create a read-only multi-model advisor artifact.

The advisor is deliberately narrow: it reads local evidence, asks one external
chat-capable provider for a Chinese review, redacts secrets, and writes an
auditable JSON artifact. It never edits files, runs shell commands, or performs
remote writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import mm_chat_smoke


ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / ".codex" / "harness" / "runs"
KIMI_READONLY_AGENT = ROOT / ".codex" / "harness" / "advisors" / "kimi-readonly-agent" / "agent.yaml"
KIMI_EMPTY_SKILLS = ROOT / ".codex" / "harness" / "advisors" / "empty-skills"
KIMI_WORK_DIR = Path(tempfile.gettempdir()) / "caicaiis-kimi-readonly-advisor"
KIMI_CLI_CONFIG = {
    "default_model": "kimi-for-coding",
    "merge_all_available_skills": False,
    "telemetry": False,
    "providers": {
        "kimi-for-coding": {
            "type": "kimi",
            "base_url": "https://api.kimi.com/coding/v1",
            "api_key": "",
        }
    },
    "models": {
        "kimi-for-coding": {
            "provider": "kimi-for-coding",
            "model": "kimi-for-coding",
            "max_context_size": 262144,
        }
    },
}
KIMI_RESUME_PATTERN = re.compile(r"\n?To resume this session: kimi -r [0-9a-f-]+\s*$")
FENCED_JSON_PATTERN = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
RETRYABLE_KIMI_CLI_PATTERN = re.compile(
    r"timed?\s*out|timeout|connection|network|HTTP\s+5\d\d|exit(?:ed)?\s+75",
    re.IGNORECASE,
)
SENSITIVE_EVIDENCE_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"
    ),
]

ROLE_INSTRUCTIONS = {
    "critic": "你是只读反方评审。重点寻找错误假设、缺失验证、范围膨胀、安全边界和更简单的替代方案。",
    "request-decoder": "你是中文需求解码顾问。重点把模糊需求翻译成用户能看懂的目标、验收、风险和确认点。",
    "benchmark-auditor": "你是评测审计顾问。重点检查 baseline、variant、pass/fail、成本、失败分类和结论是否匹配证据。",
    "attempt-grader": "你是独立尝试评分员。重点比较两个 agent attempt 的需求契合度、安全边界、最小改动、验证质量和成本合理性。",
}
ROLE_OUTPUT_FIELDS = {
    "attempt-grader": (
        "winner、baseline_score、challenger_score、score_rationale、"
        "safety_findings、cost_assessment、confidence"
    ),
    "default": "verdict、strongest_case、risks、missing_evidence、recommended_action、confidence",
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def read_evidence(paths: list[Path], max_chars_per_file: int, max_total_chars: int) -> tuple[list[dict], str]:
    evidence = []
    prompt_parts = []
    remaining = max_total_chars
    for path in paths:
        resolved = path.resolve()
        text = resolved.read_text(encoding="utf-8")
        findings = sensitive_evidence_findings(text)
        if findings:
            raise ValueError(
                f"refusing to send possible secret evidence from {display_path(resolved)}: "
                + ", ".join(findings[:3])
            )
        clipped = text[: min(max_chars_per_file, remaining)]
        redacted = mm_chat_smoke.redact(clipped)
        remaining -= len(clipped)
        evidence.append(
            {
                "path": display_path(resolved),
                "sha256": sha256_text(text),
                "chars": len(text),
                "included_chars": len(redacted),
                "redacted": redacted != clipped,
            }
        )
        prompt_parts.append(
            f"\n--- evidence: {display_path(resolved)} ---\n{redacted}\n"
        )
        if remaining <= 0:
            break
    return evidence, "".join(prompt_parts)


def sensitive_evidence_findings(text: str) -> list[str]:
    findings = []
    for pattern in SENSITIVE_EVIDENCE_PATTERNS:
        if pattern.search(text):
            findings.append(pattern.pattern)
    return findings


def build_prompt(args: argparse.Namespace, evidence_text: str) -> str:
    role_instruction = ROLE_INSTRUCTIONS[args.role]
    output_fields = ROLE_OUTPUT_FIELDS.get(args.role, ROLE_OUTPUT_FIELDS["default"])
    return f"""请使用中文输出，并且只输出最终答案，不要输出推理过程。

{role_instruction}

约束：
- 你是只读顾问，只能基于给出的 evidence 提建议。
- 不要要求执行 shell、修改文件、push、评论 GitHub 或读取密钥。
- 如果证据不足，明确写“证据不足”，不要装作确定。
- 不要迎合需求；先指出最可能错在哪里。
- 输出必须是一个紧凑 JSON object，不要 markdown fence，不要额外说明。
- 字段固定为 {output_fields}。
- 如果输出 score，使用 0-10 数字；10 表示明显优于另一方且无安全/验证问题，5 表示基本可用但无优势，0 表示失败或有严重安全问题。
- 列表字段最多各 3 条，每条不超过 40 个中文字；整段输出尽量控制在 1000 个中文字以内。

主题：
{args.topic}

证据：
{evidence_text}
"""


def default_output(provider: str, role: str) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return RUNS / f"{stamp}-advisor-{provider}-{role}.json"


def write_artifact(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def redact_json(value):
    if isinstance(value, str):
        return mm_chat_smoke.redact(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_json(item) for key, item in value.items()}
    return value


def parse_advisor_json(content: str) -> tuple[dict | None, str | None]:
    stripped = content.strip()
    match = FENCED_JSON_PATTERN.match(stripped)
    if match:
        stripped = match.group(1).strip()
    else:
        extracted = extract_json_object(stripped)
        if extracted:
            stripped = extracted
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, str(exc)
    if not isinstance(parsed, dict):
        return None, "advisor JSON must be an object"
    return redact_json(parsed), None


def extract_json_object(content: str) -> str | None:
    start = content.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(content)):
        char = content[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]
    return None


def kimi_tool_python(executable: str) -> Path | None:
    candidate = Path(executable).resolve().parent / "python"
    return candidate if candidate.exists() else None


def run_kimi_sdk_fallback(executable: str, prompt: str, timeout: int, max_tokens: int) -> dict:
    python = kimi_tool_python(executable)
    if python is None:
        raise RuntimeError("kimi tool python not found for SDK fallback")

    code = r'''
import asyncio
import json
import os
import sys
import time

import httpx
from openai import AsyncOpenAI
from kimi_cli.constant import USER_AGENT


async def main() -> None:
    request = json.loads(sys.stdin.read())
    api_key = os.environ.get("KIMI_API_KEY") or os.environ.get("KIMI_CODE_API_KEY")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.kimi.com/coding/v1",
        default_headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(request["timeout"], connect=min(30.0, request["timeout"])),
    )
    started = time.monotonic()
    response = await client.chat.completions.create(
        model="kimi-for-coding",
        messages=[{"role": "user", "content": request["prompt"]}],
        max_tokens=request["max_tokens"],
        stream=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
    message = response.choices[0].message
    usage = response.usage.model_dump(mode="json") if response.usage else None
    print(json.dumps({
        "latency_ms": int((time.monotonic() - started) * 1000),
        "content": message.content or "",
        "reasoning": getattr(message, "reasoning", None) or getattr(message, "reasoning_content", None) or "",
        "usage": usage,
    }, ensure_ascii=False))


asyncio.run(main())
'''
    request = json.dumps(
        {
            "prompt": prompt,
            "timeout": timeout,
            "max_tokens": max_tokens,
        },
        ensure_ascii=False,
    )
    completed = subprocess.run(
        [str(python), "-c", code],
        input=request,
        cwd=str(KIMI_WORK_DIR),
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout + 10,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"kimi SDK fallback exited {completed.returncode}: {detail[:500]}")
    payload = json.loads(completed.stdout)
    return {
        "provider": "kimi-cli",
        "model": "kimi-for-coding",
        "base_url": "https://api.kimi.com/coding/v1",
        "latency_ms": payload["latency_ms"],
        "content": payload["content"],
        "reasoning": payload["reasoning"],
        "usage": payload["usage"],
        "transport": "kimi-cli-sdk-fallback",
    }


def run_kimi_cli(prompt: str, timeout: int, max_tokens: int) -> dict:
    executable = shutil.which("kimi")
    if not executable:
        raise RuntimeError("kimi executable not found; install with: uv tool install kimi-cli")
    if not KIMI_READONLY_AGENT.is_file():
        raise RuntimeError(f"missing Kimi read-only agent file: {display_path(KIMI_READONLY_AGENT)}")
    KIMI_WORK_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if not env.get("KIMI_API_KEY") and env.get("KIMI_CODE_API_KEY"):
        env["KIMI_API_KEY"] = env["KIMI_CODE_API_KEY"]
    env.setdefault("KIMI_MODEL_MAX_TOKENS", "1200")
    env.setdefault("KIMI_MODEL_TEMPERATURE", "0")

    started = time.monotonic()
    completed = subprocess.run(
        [
            executable,
            "--work-dir",
            str(KIMI_WORK_DIR),
            "--quiet",
            "--no-thinking",
            "--max-steps-per-turn",
            "1",
            "--skills-dir",
            str(KIMI_EMPTY_SKILLS),
            "--agent-file",
            str(KIMI_READONLY_AGENT),
            "--config",
            json.dumps(KIMI_CLI_CONFIG, ensure_ascii=False),
            "-p",
            prompt,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    output = completed.stdout.strip()
    output = KIMI_RESUME_PATTERN.sub("", output).strip()
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if not os.environ.get("KIMI_ADVISOR_ENABLE_SDK_FALLBACK"):
            raise RuntimeError(f"kimi CLI exited {completed.returncode}: {detail[:500]}")
        if not is_retryable_kimi_cli_failure(completed.returncode, detail):
            raise RuntimeError(f"kimi CLI exited {completed.returncode}: {detail[:500]}")
        try:
            return run_kimi_sdk_fallback(executable, prompt, timeout, max_tokens)
        except Exception as fallback_exc:
            raise RuntimeError(
                f"kimi CLI exited {completed.returncode}: {detail[:500]}; "
                f"SDK fallback failed: {fallback_exc}"
            ) from fallback_exc
    return {
        "provider": "kimi-cli",
        "model": "kimi-for-coding",
        "base_url": "https://api.kimi.com/coding/v1",
        "latency_ms": int((time.monotonic() - started) * 1000),
        "content": output,
        "reasoning": "",
        "usage": None,
        "transport": "kimi-cli",
    }


def is_retryable_kimi_cli_failure(returncode: int, detail: str) -> bool:
    return returncode == 75 or bool(RETRYABLE_KIMI_CLI_PATTERN.search(detail))


def make_record(
    args: argparse.Namespace,
    provider: mm_chat_smoke.Provider | None,
    evidence: list[dict],
    prompt: str,
    completion: dict | None,
    status: str,
    error: str | None = None,
) -> dict:
    content = completion["content"] if completion else ""
    reasoning = completion["reasoning"] if completion else ""
    parsed_response, parse_error = parse_advisor_json(content) if content else (None, None)
    return {
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": status,
        "provider": provider.name if provider else args.provider,
        "model": completion["model"] if completion else None,
        "base_url": completion["base_url"] if completion else None,
        "role": args.role,
        "topic": args.topic,
        "evidence": evidence,
        "prompt_sha256": sha256_text(prompt),
        "latency_ms": completion["latency_ms"] if completion else None,
        "transport": completion.get("transport") if completion else None,
        "content": mm_chat_smoke.redact(content),
        "content_chars": len(content),
        "parsed_response": parsed_response,
        "parse_error": parse_error,
        "reasoning_chars": len(reasoning),
        "usage": completion["usage"] if completion else None,
        "error": mm_chat_smoke.redact(error or ""),
        "adoption": {
            "status": "pending",
            "notes": "由主 Agent 结合本地验证决定采纳或拒绝；外置 advisor 不直接改文件。",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    provider_choices = sorted([*mm_chat_smoke.PROVIDERS, "kimi-cli"])
    parser.add_argument("provider", choices=provider_choices)
    parser.add_argument("--role", choices=sorted(ROLE_INSTRUCTIONS), default="critic")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--evidence", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--max-chars-per-file", type=int, default=6000)
    parser.add_argument("--max-total-chars", type=int, default=18000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.provider == "kimi-code":
        print(
            "ERROR: kimi-code benefits credentials only support the coding endpoint smoke here; "
            "use kimi-cli for Kimi advisor artifacts.",
            file=sys.stderr,
        )
        return 2
    provider = (
        mm_chat_smoke.PROVIDERS[args.provider]
        if args.provider in mm_chat_smoke.PROVIDERS
        else None
    )

    try:
        evidence, evidence_text = read_evidence(
            args.evidence,
            args.max_chars_per_file,
            args.max_total_chars,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"ERROR: failed to read evidence: {exc}", file=sys.stderr)
        return 2

    prompt = build_prompt(args, evidence_text)
    output = args.output or default_output(args.provider, args.role)
    if args.dry_run:
        record = make_record(args, provider, evidence, prompt, None, "dry-run")
        record["would_write"] = str(output)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    started = time.monotonic()
    try:
        if args.provider == "kimi-cli":
            completion = run_kimi_cli(prompt, args.timeout, args.max_tokens)
        else:
            assert provider is not None
            completion = mm_chat_smoke.chat_completion(
                provider,
                prompt,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
            )
    except Exception as exc:
        record = make_record(args, provider, evidence, prompt, None, "failed", str(exc))
        write_artifact(output, record)
        print(json.dumps({"status": "failed", "output": str(output)}, ensure_ascii=False))
        return 1

    if not completion["content"].strip():
        status = "no-content"
    else:
        _, parse_error = parse_advisor_json(completion["content"])
        status = "invalid-json" if parse_error else "passed"
    record = make_record(args, provider, evidence, prompt, completion, status)
    record["wall_time_ms"] = int((time.monotonic() - started) * 1000)
    write_artifact(output, record)
    print(
        json.dumps(
            {
                "status": status,
                "output": str(output),
                "content_chars": record["content_chars"],
                "reasoning_chars": record["reasoning_chars"],
                "latency_ms": record["latency_ms"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
