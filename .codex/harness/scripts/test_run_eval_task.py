#!/usr/bin/env python3
"""Unit tests for run_eval_task.py."""

from __future__ import annotations

import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import run_eval_task


class RunEvalTaskTests(unittest.TestCase):
    def test_load_task_by_id(self) -> None:
        path, task = run_eval_task.load_task("harness-self-check")

        self.assertTrue(path.name.endswith("001-harness-self-check.json"))
        self.assertEqual(task["id"], "harness-self-check")

    def test_blocks_remote_write_commands(self) -> None:
        unsafe = [
            "git push origin main",
            "git -C . push origin main",
            "git -c protocol.version=2 push origin main",
            "git -C . tag v1.2.3",
            "git -C '/tmp/space dir' tag v1.2.3",
            "git -c user.name=x tag v1.2.3",
            "git -C . reset --hard HEAD",
            "gh pr comment 1 --body hello",
            "gh --repo owner/repo pr comment 1 --body hello",
            "gh pr review 1 --approve",
            "gh -R owner/repo pr review 1 --approve",
            "gh pr merge 123 --merge",
            "gh -R owner/repo pr merge 123 --merge",
            "gh issue edit 1 --title hello",
            "gh --repo owner/repo issue comment 1 --body hello",
            "gh repo sync owner/repo",
            "gh -R owner/repo repo sync",
            "gh api repos/o/r/issues/1/comments -f body=hello",
            "gh --repo owner/repo api repos/o/r/issues/1/comments -f body=hello",
            "gh release create v1.2.3",
            "gh --repo owner/repo release create v1.2.3",
            "git tag v1.2.3",
            "git reset --hard HEAD",
            "bash -lc 'git -C . push origin main'",
            "bash -lc 'git -C . tag v1.2.3'",
            "bash -lc 'git -C \"/tmp/space dir\" tag v1.2.3'",
            "bash -lc 'gh pr merge 123 --squash'",
            "bash -lc 'gh --repo owner/repo release create v1.2.3'",
        ]
        for command in unsafe:
            with self.subTest(command=command):
                self.assertTrue(run_eval_task.is_unsafe(command))

    def test_allows_local_verifier_commands(self) -> None:
        safe = [
            "python3 .codex/harness/scripts/validate_harness.py",
            "cargo test -p codex-core config_tests",
            "rg -n model-router .codex/harness",
        ]
        for command in safe:
            with self.subTest(command=command):
                self.assertFalse(run_eval_task.is_unsafe(command))

    def test_prepare_workspace_creates_independent_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            task = {
                "id": "real-001-readme-title-fix",
                "workspace_fixture": ".codex/harness/evals/fixtures/real-001-readme-title-fix",
            }

            run_eval_task.prepare_workspace(task, workspace)
            git_root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(git_root.returncode, 0, git_root.stderr)
        self.assertEqual(Path(git_root.stdout.strip()).resolve(), workspace.resolve())
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout, "")

    def test_real_benchmark_rejects_missing_attempt_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_path = root / "real-task.json"
            workspace = root / "workspace"
            output = root / "out.jsonl"
            workspace.mkdir()
            task_path.write_text(
                json.dumps(
                    {
                        "id": "real-test",
                        "title": "real test",
                        "category": "real-feature-bug",
                        "risk_level": "low",
                        "instruction": "Verify strict attempt evidence for real benchmark tasks.",
                        "workspace_fixture": ".codex/harness/evals/fixtures/real-001-readme-title-fix",
                        "requires_attempt_evidence": True,
                        "setup": [],
                        "verifier": ["python3 -c \"print('would pass')\""],
                        "success_criteria": ["Verifier passes."],
                        "forbidden_actions": ["Do not push."],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(run_eval_task.HARNESS / "scripts" / "run_eval_task.py"),
                    str(task_path),
                    "--variant",
                    "proposed",
                    "--verify-only",
                    "--workspace",
                    str(workspace),
                    "--attempt-log",
                    str(root / "missing.log"),
                    "--output",
                    str(output),
                ],
                cwd=run_eval_task.ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse(output.exists())

    def test_real_benchmark_rejects_attempt_note_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_path = root / "real-task.json"
            workspace = root / "workspace"
            output = root / "out.jsonl"
            workspace.mkdir()
            task_path.write_text(
                json.dumps(
                    {
                        "id": "real-test",
                        "title": "real test",
                        "category": "real-feature-bug",
                        "risk_level": "low",
                        "instruction": "Verify note-only evidence cannot satisfy benchmark attempts.",
                        "workspace_fixture": ".codex/harness/evals/fixtures/real-001-readme-title-fix",
                        "requires_attempt_evidence": True,
                        "setup": [],
                        "verifier": ["python3 -c \"print('would pass')\""],
                        "success_criteria": ["Verifier passes."],
                        "forbidden_actions": ["Do not push."],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(run_eval_task.HARNESS / "scripts" / "run_eval_task.py"),
                    str(task_path),
                    "--variant",
                    "proposed",
                    "--verify-only",
                    "--workspace",
                    str(workspace),
                    "--attempt-note",
                    "manual note",
                    "--output",
                    str(output),
                ],
                cwd=run_eval_task.ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
