#!/usr/bin/env python3
"""Unit tests for score_paired_benchmark.py."""

from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

import score_paired_benchmark


class ScorePairedBenchmarkTests(unittest.TestCase):
    def test_build_evidence_redacts_and_includes_scores_rubric(self) -> None:
        records = [
            {
                "task_id": "real-x",
                "repeat_index": 1,
                "variant": "baseline",
                "status": "passed",
                "metrics": {"token_usage": {"input_tokens": 10, "output_tokens": 2}},
                "codex": {"trace": {"final_messages": ["ok sk-1234567890"]}},
            },
            {
                "task_id": "real-x",
                "repeat_index": 1,
                "variant": "subagent-lite",
                "status": "passed",
                "metrics": {"token_usage": {"input_tokens": 20, "output_tokens": 2}},
                "codex": {"trace": {"final_messages": ["ok"]}},
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            evidence = score_paired_benchmark.build_evidence(
                Path(directory) / "run.jsonl",
                records,
                ("baseline", "subagent-lite"),
                1,
            )

        self.assertIn("评分维度", evidence)
        self.assertIn("subagent-lite", evidence)
        self.assertNotIn("sk-1234567890", evidence)
        self.assertIn("[REDACTED]", evidence)

    def test_compact_record_includes_verifier_failure_and_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "report.md").write_text("value sk-1234567890\n", encoding="utf-8")
            verify = root / "verify.jsonl"
            verify.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "results": [
                            {
                                "command": "python3 check.py",
                                "exit_code": 1,
                                "stdout": "",
                                "stderr": "bad Bearer abcdefghij",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            compact = score_paired_benchmark.compact_record(
                {
                    "task_id": "real-x",
                    "repeat_index": 1,
                    "variant": "baseline",
                    "status": "failed",
                    "workspace": str(workspace),
                    "workspace_diff": {"modified": ["report.md"], "added": [], "deleted": []},
                    "verify": {"output": str(verify)},
                    "codex": {"trace": {}},
                }
            )

        self.assertEqual(compact["verifier_failure"]["status"], "failed")
        self.assertIn("[REDACTED]", compact["verifier_failure"]["results"][0]["stderr_excerpt"])
        self.assertEqual(compact["changed_file_summaries"][0]["path"], "report.md")
        self.assertIn("[REDACTED]", compact["changed_file_summaries"][0]["excerpt"])


if __name__ == "__main__":
    unittest.main()
