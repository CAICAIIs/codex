#!/usr/bin/env python3
"""Unit tests for run_advisor.py."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import run_advisor


class AdvisorTests(unittest.TestCase):
    def test_dry_run_writes_no_secret_and_no_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / "evidence.md"
            evidence.write_text("事实：远程写入默认禁止。\n", encoding="utf-8")
            output = Path(temp) / "advisor.json"
            argv = [
                "run_advisor.py",
                "glm",
                "--topic",
                "检查多模型路由",
                "--evidence",
                str(evidence),
                "--output",
                str(output),
                "--dry-run",
            ]

            with patch.object(sys, "argv", argv):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = run_advisor.main()

            self.assertEqual(exit_code, 0)
            self.assertFalse(output.exists())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "dry-run")
            self.assertEqual(payload["provider"], "glm")
            self.assertEqual(payload["adoption"]["status"], "pending")

    def test_kimi_code_is_not_allowed_as_chat_advisor(self) -> None:
        argv = ["run_advisor.py", "kimi-code", "--topic", "检查路由"]

        with patch.object(sys, "argv", argv):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = run_advisor.main()

        self.assertEqual(exit_code, 2)
        self.assertIn("benefits credentials", stderr.getvalue())

    def test_evidence_with_secret_pattern_is_rejected_before_prompting_advisor(self) -> None:
        secret = "sk-" + "testsecret1234567890"
        with tempfile.TemporaryDirectory() as temp:
            evidence_path = Path(temp) / "evidence.md"
            evidence_path.write_text(f"secret={secret}\n", encoding="utf-8")

            with patch.dict("os.environ", {"GLM_5_1_API_KEY": secret}, clear=False):
                with self.assertRaisesRegex(ValueError, "possible secret evidence"):
                    run_advisor.read_evidence(
                        [evidence_path],
                        max_chars_per_file=1000,
                        max_total_chars=1000,
                    )

    def test_glm_advisor_artifact_redacts_and_records_content(self) -> None:
        secret = "sk-" + "testsecret1234567890"
        completion = {
            "provider": "glm",
            "model": "glm-5.1",
            "base_url": "https://glm-5-1.hiyo.top/v1",
            "latency_ms": 123,
            "content": f'{{"verdict":"PASS","risks":["{secret}"]}}',
            "reasoning": "",
            "usage": {"total_tokens": 42},
        }

        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / "evidence.md"
            evidence.write_text("事实：评测必须有 baseline。\n", encoding="utf-8")
            output = Path(temp) / "advisor.json"
            argv = [
                "run_advisor.py",
                "glm",
                "--role",
                "benchmark-auditor",
                "--topic",
                "检查评测证据",
                "--evidence",
                str(evidence),
                "--output",
                str(output),
            ]

            with patch("mm_chat_smoke.chat_completion", return_value=completion):
                with patch.object(sys, "argv", argv):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        exit_code = run_advisor.main()

            self.assertEqual(exit_code, 0)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["status"], "passed")
            artifact = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(artifact["status"], "passed")
            self.assertEqual(artifact["role"], "benchmark-auditor")
            self.assertNotIn(secret, artifact["content"])
            self.assertIn("[REDACTED]", artifact["content"])
            self.assertEqual(
                artifact["parsed_response"],
                {"verdict": "PASS", "risks": ["[REDACTED]"]},
            )
            self.assertIsNone(artifact["parse_error"])

    def test_fenced_advisor_json_is_parsed(self) -> None:
        parsed, error = run_advisor.parse_advisor_json(
            '```json\n{"verdict":"PASS","risks":[]}\n```'
        )

        self.assertEqual(parsed, {"verdict": "PASS", "risks": []})
        self.assertIsNone(error)

    def test_advisor_json_can_be_extracted_from_surrounding_text(self) -> None:
        parsed, error = run_advisor.parse_advisor_json(
            '说明：\n{"verdict":"PASS","risks":["ok"]}\n结束'
        )

        self.assertEqual(parsed, {"verdict": "PASS", "risks": ["ok"]})
        self.assertIsNone(error)

    def test_kimi_cli_run_uses_readonly_agent_and_strips_resume_footer(self) -> None:
        completed = __import__("subprocess").CompletedProcess(
            args=["kimi"],
            returncode=0,
            stdout="Kimi advisor ok\n\nTo resume this session: kimi -r 45f16795-58e8-42a1-8c19-e18bf301f7fa\n",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temp:
            agent_file = Path(temp) / "agent.yaml"
            agent_file.write_text("version: 1\n", encoding="utf-8")
            with patch("shutil.which", return_value="/usr/local/bin/kimi"):
                with patch.object(run_advisor, "KIMI_READONLY_AGENT", agent_file):
                    with patch("subprocess.run", return_value=completed) as run:
                        result = run_advisor.run_kimi_cli("hello", timeout=5, max_tokens=100)

        self.assertEqual(result["provider"], "kimi-cli")
        self.assertEqual(result["content"], "Kimi advisor ok")
        self.assertEqual(result["transport"], "kimi-cli")
        args = run.call_args.args[0]
        self.assertIn("--agent-file", args)
        self.assertIn(str(agent_file), args)

    def test_kimi_cli_sdk_fallback_is_disabled_by_default(self) -> None:
        completed = __import__("subprocess").CompletedProcess(
            args=["kimi"],
            returncode=75,
            stdout="Request timed out.\n",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temp:
            agent_file = Path(temp) / "agent.yaml"
            agent_file.write_text("version: 1\n", encoding="utf-8")
            with patch("shutil.which", return_value="/usr/local/bin/kimi"):
                with patch.object(run_advisor, "KIMI_READONLY_AGENT", agent_file):
                    with patch("subprocess.run", return_value=completed):
                        with patch.dict("os.environ", {}, clear=True):
                            with self.assertRaisesRegex(RuntimeError, "kimi CLI exited 75"):
                                run_advisor.run_kimi_cli("hello", timeout=5, max_tokens=100)


if __name__ == "__main__":
    unittest.main()
