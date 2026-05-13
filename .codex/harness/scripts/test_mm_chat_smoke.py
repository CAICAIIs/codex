#!/usr/bin/env python3
"""Unit tests for mm_chat_smoke.py."""

from __future__ import annotations

import io
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import mm_chat_smoke


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


class SmokeTests(unittest.TestCase):
    def test_http_error_redacts_env_key_and_bearer_token(self) -> None:
        secret = "sk-" + "testsecret1234567890"
        body = f'{{"error":"bad key {secret} Bearer {secret}"}}'.encode()
        error = urllib.error.HTTPError(
            url="https://example.invalid/v1/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(body),
        )

        with patch.dict(os.environ, {"GLM_5_1_API_KEY": secret}, clear=False):
            with patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as ctx:
                    mm_chat_smoke.post_chat(
                        mm_chat_smoke.PROVIDERS["glm"],
                        "hello",
                        timeout=1,
                        max_tokens=5,
                    )

        message = str(ctx.exception)
        self.assertNotIn(secret, message)
        self.assertIn("[REDACTED]", message)

    def test_kimi_code_missing_expected_model_exits_nonzero(self) -> None:
        fake = FakeResponse('{"data":[{"id":"other-model"}]}')
        argv = ["mm_chat_smoke.py", "kimi-code", "--check", "models", "--timeout", "1"]

        with patch.dict(os.environ, {"KIMI_CODE_API_KEY": "sk-" + "testsecret1234567890"}):
            with patch("urllib.request.urlopen", return_value=fake):
                with patch.object(sys, "argv", argv):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = mm_chat_smoke.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("missing expected model", stderr.getvalue())
        self.assertNotIn("sk-" + "testsecret", stdout.getvalue() + stderr.getvalue())

    def test_dry_run_does_not_require_or_print_key(self) -> None:
        argv = ["mm_chat_smoke.py", "glm", "--dry-run"]
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sys, "argv", argv):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = mm_chat_smoke.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("GLM_5_1_API_KEY", stdout.getvalue())
        self.assertNotIn("sk-", stdout.getvalue())

    def test_chat_allows_null_content_with_reasoning(self) -> None:
        fake = FakeResponse(
            '{"choices":[{"message":{"content":null,"reasoning":"thinking"}}]}'
        )

        with patch.dict(os.environ, {"GLM_5_1_API_KEY": "sk-" + "testsecret1234567890"}):
            with patch("urllib.request.urlopen", return_value=fake):
                result = mm_chat_smoke.post_chat(
                    mm_chat_smoke.PROVIDERS["glm"],
                    "hello",
                    timeout=1,
                    max_tokens=5,
                )

        self.assertEqual(result["content_chars"], 0)
        self.assertEqual(result["content_preview"], "")
        self.assertEqual(result["reasoning_chars"], len("thinking"))
        self.assertTrue(result["has_reasoning"])

    def test_glm_chat_body_disables_thinking_for_advisor_content(self) -> None:
        body = mm_chat_smoke.build_chat_body(
            mm_chat_smoke.PROVIDERS["glm"],
            "hello",
            max_tokens=5,
        )

        self.assertEqual(
            body,
            {
                "model": "glm-5.1",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 5,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )


if __name__ == "__main__":
    unittest.main()
