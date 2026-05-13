#!/usr/bin/env python3
"""Unit tests for run_paired_benchmark.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_paired_benchmark


class PairedBenchmarkTests(unittest.TestCase):
    def test_schedule_pairs_variants_for_each_task_repeat(self) -> None:
        schedule = run_paired_benchmark.build_schedule(
            ["real-001-readme-title-fix", "real-004-redaction-bearer-token"],
            ["baseline", "multimodel-lite"],
            repeats=2,
            seed=7,
        )

        self.assertEqual(len(schedule), 8)
        pairs = {}
        for item in schedule:
            pairs.setdefault((item["task_id"], item["repeat"]), set()).add(item["variant"])
        self.assertTrue(all(value == {"baseline", "multimodel-lite"} for value in pairs.values()))

    def test_trace_usage_parser_counts_turn_completed_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            root_path = str(run_paired_benchmark.ROOT)
            trace.write_text(
                "\n".join(
                    [
                        '{"type":"turn.started"}',
                        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3,"reasoning_output_tokens":2}}',
                        '{"type":"item.completed","item":{"type":"agent_message","text":"See '
                        + root_path
                        + '/README.md"}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = run_paired_benchmark.parse_trace(trace)

        self.assertEqual(parsed["usage"]["input_tokens"], 10)
        self.assertEqual(parsed["usage"]["output_tokens"], 3)
        self.assertEqual(parsed["usage"]["reasoning_output_tokens"], 2)
        self.assertNotIn(root_path, parsed["final_messages"][0])
        self.assertFalse(parsed["turn_failed"])

    def test_trace_parser_detects_spawn_agent_collab_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            trace.write_text(
                "\n".join(
                    [
                        '{"type":"item.completed","item":{"type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"p","receiver_thread_ids":["c"],"prompt":"Reply with READY.","agents_states":{"c":{"status":"pending_init","message":null}},"status":"completed"}}',
                        '{"type":"item.completed","item":{"type":"collab_tool_call","tool":"wait","sender_thread_id":"p","receiver_thread_ids":[],"prompt":null,"agents_states":{},"status":"completed"}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = run_paired_benchmark.parse_trace(trace)

        self.assertTrue(parsed["subagent_spawned"])
        self.assertEqual(parsed["collab_tool_counts"]["spawn_agent"], 1)
        self.assertEqual(parsed["collab_tool_counts"]["wait"], 1)
        self.assertEqual(parsed["collab_tool_attempt_counts"], {})

    def test_trace_parser_does_not_count_started_spawn_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            trace.write_text(
                '{"type":"item.started","item":{"id":"item_1","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"p","receiver_thread_ids":[],"prompt":"Reply with READY.","agents_states":{},"status":"in_progress"}}\n',
                encoding="utf-8",
            )

            parsed = run_paired_benchmark.parse_trace(trace)

        self.assertFalse(parsed["subagent_spawned"])
        self.assertEqual(parsed["collab_tool_attempt_counts"]["spawn_agent"], 1)
        self.assertEqual(parsed["collab_tool_counts"], {})

    def test_snapshot_diff_reports_added_modified_deleted(self) -> None:
        before = {"a.txt": "1", "b.txt": "2"}
        after = {"b.txt": "3", "c.txt": "4"}

        diff = run_paired_benchmark.diff_snapshot(before, after)

        self.assertEqual(
            diff,
            {
                "modified": ["b.txt"],
                "added": ["c.txt"],
                "deleted": ["a.txt"],
                "changed_files": 3,
            },
        )

    def test_required_advisors_need_named_provider_success(self) -> None:
        advisors = [
            {"provider": "kimi-cli", "status": "failed"},
            {"provider": "glm", "status": "passed"},
        ]

        self.assertFalse(run_paired_benchmark.required_advisors_satisfied(advisors, ["kimi-cli"]))
        self.assertTrue(run_paired_benchmark.required_advisors_satisfied(advisors, ["glm"]))
        self.assertTrue(run_paired_benchmark.required_advisors_satisfied(advisors, []))

    def test_run_advisor_retries_until_success(self) -> None:
        calls = []

        def fake_run_advisor_once(provider, role, topic, evidence, output, timeout, provider_env):
            calls.append(output.name)
            status = "passed" if len(calls) == 2 else "failed"
            return {
                "provider": provider,
                "role": role,
                "status": status,
                "path": str(output),
                "latency_ms": 1,
                "content_chars": 2 if status == "passed" else 0,
                "content": "{}" if status == "passed" else "",
                "error": "" if status == "passed" else "timeout",
            }

        original = run_paired_benchmark.run_advisor_once
        run_paired_benchmark.run_advisor_once = fake_run_advisor_once
        try:
            with tempfile.TemporaryDirectory() as directory:
                result = run_paired_benchmark.run_advisor(
                    "kimi-cli",
                    "request-decoder",
                    "topic",
                    [],
                    Path(directory) / "advisor-kimi-cli.json",
                    timeout=1,
                    attempts=2,
                    provider_env={},
                )
        finally:
            run_paired_benchmark.run_advisor_once = original

        self.assertEqual(result["status"], "passed")
        self.assertEqual(calls, ["advisor-kimi-cli-attempt-1.json", "advisor-kimi-cli-attempt-2.json"])
        self.assertEqual([attempt["status"] for attempt in result["attempts"]], ["failed", "passed"])

    def test_advisor_evidence_includes_small_workspace_text_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            task = workspace / "TASK.md"
            readme = workspace / "README.md"
            binary = workspace / "image.png"
            task.write_text("Do the task", encoding="utf-8")
            readme.write_text("# TODO", encoding="utf-8")
            binary.write_bytes(b"\x89PNG")

            evidence = run_paired_benchmark.advisor_evidence_paths(workspace)

        self.assertEqual([path.name for path in evidence], ["TASK.md", "README.md"])

    def test_provider_env_is_scrubbed_from_agent_subprocess_env(self) -> None:
        env = {
            "PATH": "/bin",
            "KIMI_API_KEY": "secret",
            "KIMI_CODE_API_KEY": "secret",
            "MOONSHOT_API_KEY": "secret",
            "GLM_5_1_API_KEY": "secret",
            "KIMI_MODEL": "kimi-for-coding",
        }

        scrubbed = run_paired_benchmark.scrub_provider_env(env)

        self.assertEqual(scrubbed, {"PATH": "/bin"})

    def test_agent_subprocess_receives_scrubbed_provider_env(self) -> None:
        seen_env = {}

        def fake_run(*args, **kwargs):
            seen_env.update(kwargs["env"])
            return __import__("subprocess").CompletedProcess(args=args[0], returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            stdout = Path(directory) / "stdout.log"
            stderr = Path(directory) / "stderr.log"
            with patch.dict(
                "os.environ",
                {
                    "PATH": "/bin",
                    "KIMI_API_KEY": "secret",
                    "MOONSHOT_API_KEY": "secret",
                    "GLM_5_1_API_KEY": "secret",
                },
                clear=True,
            ):
                with patch("subprocess.run", side_effect=fake_run):
                    result = run_paired_benchmark.run_subprocess(
                        ["codex", "exec"],
                        Path(directory),
                        timeout=1,
                        stdout_path=stdout,
                        stderr_path=stderr,
                    )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(seen_env, {"PATH": "/bin"})

    def test_advisor_evidence_skips_sensitive_file_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "TASK.md").write_text("Do the task", encoding="utf-8")
            (workspace / ".env").write_text("API_KEY=secret", encoding="utf-8")
            (workspace / "credentials.json").write_text("{}", encoding="utf-8")
            (workspace / "README.md").write_text("# ok", encoding="utf-8")

            evidence = run_paired_benchmark.advisor_evidence_paths(workspace)

        self.assertEqual([path.name for path in evidence], ["TASK.md", "README.md"])


if __name__ == "__main__":
    unittest.main()
