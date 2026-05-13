#!/usr/bin/env python3
"""Unit tests for validate_harness.py."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

import validate_harness


class ValidateHarnessTests(unittest.TestCase):
    def valid_real_task(self) -> dict:
        return {
            "id": "real-001-readme-title-fix",
            "title": "Real 001 README title fix",
            "category": "real-feature-bug",
            "risk_level": "low",
            "instruction": "Replace the placeholder README title in an isolated workspace.",
            "workspace_fixture": ".codex/harness/evals/fixtures/real-001-readme-title-fix",
            "requires_attempt_evidence": True,
            "setup": [],
            "verifier": [
                "python3 .codex/harness/scripts/assert_real_task.py real-001-readme-title-fix"
            ],
            "success_criteria": ["README title is updated."],
            "forbidden_actions": ["Do not push."],
        }

    def test_valid_real_task_category_and_fields(self) -> None:
        path = validate_harness.TASKS / "024-real-001-readme-title-fix.json"

        self.assertEqual(validate_harness.validate_task(path, self.valid_real_task()), [])

    def test_rejects_unknown_category_and_field(self) -> None:
        task = copy.deepcopy(self.valid_real_task())
        task["category"] = "surprise-category"
        task["extra_field"] = True

        failures = validate_harness.validate_task(
            validate_harness.TASKS / "024-real-001-readme-title-fix.json",
            task,
        )

        joined = "\n".join(failures)
        self.assertIn("category is invalid", joined)
        self.assertIn("unknown fields", joined)


if __name__ == "__main__":
    unittest.main()
