#!/usr/bin/env python3
"""Smoke-test OpenAI-compatible Chat Completions providers.

Use this for Kimi/GLM as external read-only advisors. The script reads API keys
from environment variables and never prints them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    base_url_env: str
    default_base_url: str
    api_key_envs: tuple[str, ...]
    model_env: str
    default_model: str
    expected_models: tuple[str, ...] = ()


PROVIDERS = {
    "kimi": Provider(
        name="kimi",
        base_url_env="KIMI_BASE_URL",
        default_base_url="https://api.moonshot.ai/v1",
        api_key_envs=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        model_env="KIMI_MODEL",
        default_model="kimi-k2.6",
    ),
    "glm": Provider(
        name="glm",
        base_url_env="GLM_5_1_BASE_URL",
        default_base_url="https://glm-5-1.hiyo.top/v1",
        api_key_envs=("GLM_5_1_API_KEY",),
        model_env="GLM_5_1_MODEL",
        default_model="glm-5.1",
    ),
    "kimi-code": Provider(
        name="kimi-code",
        base_url_env="KIMI_CODE_BASE_URL",
        default_base_url="https://api.kimi.com/coding/v1",
        api_key_envs=("KIMI_CODE_API_KEY",),
        model_env="KIMI_CODE_MODEL",
        default_model="kimi-for-coding",
        expected_models=("kimi-for-coding",),
    ),
}


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{10,}", re.IGNORECASE),
]


def redact(text: str) -> str:
    redacted = text
    for env_name in {
        env_name for provider in PROVIDERS.values() for env_name in provider.api_key_envs
    }:
        value = os.environ.get(env_name, "").strip()
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def read_api_key(provider: Provider) -> str:
    for env_name in provider.api_key_envs:
        api_key = os.environ.get(env_name, "").strip()
        if api_key:
            return api_key
    raise RuntimeError(f"missing env var: {' or '.join(provider.api_key_envs)}")


def build_chat_body(provider: Provider, prompt: str, max_tokens: int) -> dict:
    body = {
        "model": os.environ.get(provider.model_env, provider.default_model),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if provider.name == "glm":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


def chat_completion(provider: Provider, prompt: str, timeout: int, max_tokens: int) -> dict:
    api_key = read_api_key(provider)
    base_url = os.environ.get(provider.base_url_env, provider.default_base_url).rstrip("/")
    url = f"{base_url}/chat/completions"
    body = build_chat_body(provider, prompt, max_tokens)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(f"request timed out after {timeout}s") from exc
        raise RuntimeError(f"request failed: {redact(str(reason))}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    choice = payload.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content") or ""
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""

    return {
        "provider": provider.name,
        "model": body["model"],
        "base_url": base_url,
        "latency_ms": elapsed_ms,
        "content": content,
        "reasoning": reasoning,
        "usage": payload.get("usage"),
    }


def post_chat(provider: Provider, prompt: str, timeout: int, max_tokens: int) -> dict:
    completion = chat_completion(provider, prompt, timeout, max_tokens)
    content = completion["content"]
    reasoning = completion["reasoning"]

    return {
        "provider": completion["provider"],
        "model": completion["model"],
        "base_url": completion["base_url"],
        "latency_ms": completion["latency_ms"],
        "content_chars": len(content),
        "content_preview": content[:300],
        "reasoning_chars": len(reasoning),
        "has_reasoning": bool(reasoning),
        "usage": completion["usage"],
    }


def list_models(provider: Provider, timeout: int) -> dict:
    api_key = read_api_key(provider)
    base_url = os.environ.get(provider.base_url_env, provider.default_base_url).rstrip("/")
    url = f"{base_url}/models"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="GET",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(f"request timed out after {timeout}s") from exc
        raise RuntimeError(f"request failed: {redact(str(reason))}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    model_ids = [
        item.get("id", "")
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]

    return {
        "provider": provider.name,
        "base_url": base_url,
        "latency_ms": elapsed_ms,
        "model_count": len(model_ids),
        "expected_models_present": {
            model_id: model_id in model_ids for model_id in provider.expected_models
        },
        "model_ids_preview": model_ids[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=sorted(PROVIDERS))
    parser.add_argument(
        "--prompt",
        default="用一句中文说明：你将作为只读评审，不执行文件修改。",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--max-tokens", type=int, default=300)
    parser.add_argument(
        "--check",
        choices=("chat", "models"),
        help="Check type. Defaults to models for kimi-code and chat for other providers.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    provider = PROVIDERS[args.provider]
    check = args.check or ("models" if provider.name == "kimi-code" else "chat")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "provider": provider.name,
                    "check": check,
                    "base_url_env": provider.base_url_env,
                    "api_key_envs": provider.api_key_envs,
                    "model_env": provider.model_env,
                    "default_model": provider.default_model,
                    "would_call": "/models" if check == "models" else "/chat/completions",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    try:
        if check == "models":
            result = list_models(provider, args.timeout)
        else:
            result = post_chat(provider, args.prompt, args.timeout, args.max_tokens)
    except Exception as exc:
        print(f"ERROR: {redact(str(exc))}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if check == "models" and provider.expected_models:
        missing = [
            model_id
            for model_id, present in result["expected_models_present"].items()
            if not present
        ]
        if missing:
            print(f"ERROR: missing expected model(s): {', '.join(missing)}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
