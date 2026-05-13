#!/usr/bin/env python3
"""Unit tests for compare_paired_benchmark.py."""

from __future__ import annotations

import unittest

import compare_paired_benchmark


class ComparePairedBenchmarkTests(unittest.TestCase):
    def test_incomplete_pairs_are_inconclusive(self) -> None:
        summary = compare_paired_benchmark.compare(
            [
                {
                    "task_id": "real-001-readme-title-fix",
                    "repeat_index": 1,
                    "variant": "baseline",
                    "status": "passed",
                    "metrics": {},
                }
            ],
            min_complete_pairs=1,
        )

        self.assertEqual(summary["conclusion"], "inconclusive")
        self.assertEqual(summary["complete_pair_count"], 0)
        self.assertEqual(len(summary["incomplete_pairs"]), 1)

    def test_paired_pass_fail_win_is_counted(self) -> None:
        records = [
            {
                "task_id": "real-001-readme-title-fix",
                "repeat_index": 1,
                "variant": "baseline",
                "status": "failed",
                "metrics": {"agent_wall_time_ms": 100, "token_usage": {"input_tokens": 10}},
            },
            {
                "task_id": "real-001-readme-title-fix",
                "repeat_index": 1,
                "variant": "multimodel-lite",
                "status": "passed",
                "metrics": {"agent_wall_time_ms": 200, "token_usage": {"input_tokens": 20}},
            },
        ]

        summary = compare_paired_benchmark.compare(records, min_complete_pairs=1)

        self.assertEqual(summary["conclusion"], "multimodel-lite-leading")
        self.assertEqual(summary["wins"]["multimodel-lite"], 1)
        self.assertEqual(summary["by_variant"]["baseline"]["pass_rate"], 0.0)
        self.assertEqual(summary["by_variant"]["multimodel-lite"]["pass_rate"], 1.0)

    def test_inconclusive_variant_does_not_count_as_complete_pair(self) -> None:
        records = [
            {
                "task_id": "real-001-readme-title-fix",
                "repeat_index": 1,
                "variant": "baseline",
                "status": "passed",
                "metrics": {},
            },
            {
                "task_id": "real-001-readme-title-fix",
                "repeat_index": 1,
                "variant": "multimodel-lite",
                "status": "inconclusive",
                "metrics": {},
            },
        ]

        summary = compare_paired_benchmark.compare(records, min_complete_pairs=1)

        self.assertEqual(summary["conclusion"], "inconclusive")
        self.assertEqual(summary["complete_pair_count"], 0)
        self.assertEqual(summary["incomplete_pairs"][0]["variants"]["multimodel-lite"], "inconclusive")

    def test_tied_pass_rate_with_higher_multimodel_cost_favors_baseline(self) -> None:
        records = [
            {
                "task_id": "real-001-readme-title-fix",
                "repeat_index": 1,
                "variant": "baseline",
                "status": "passed",
                "metrics": {"agent_wall_time_ms": 100, "token_usage": {"input_tokens": 100}},
            },
            {
                "task_id": "real-001-readme-title-fix",
                "repeat_index": 1,
                "variant": "multimodel-lite",
                "status": "passed",
                "metrics": {"agent_wall_time_ms": 200, "token_usage": {"input_tokens": 200}},
            },
        ]

        summary = compare_paired_benchmark.compare(records, min_complete_pairs=1)

        self.assertEqual(summary["conclusion"], "baseline-cost-leading")
        self.assertGreater(summary["cost"]["wall_ratio_challenger_over_baseline"], 1.25)

    def test_token_total_excludes_cached_and_reasoning_buckets_and_adds_advisor_usage(self) -> None:
        record = {
            "metrics": {
                "token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 70,
                    "output_tokens": 25,
                    "reasoning_output_tokens": 10,
                },
                "advisor_token_usage": {
                    "total_tokens": 30,
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                },
            }
        }

        self.assertEqual(compare_paired_benchmark.token_total(record), 155)

    def test_can_compare_baseline_against_subagent_variant(self) -> None:
        records = [
            {
                "task_id": "real-001-readme-title-fix",
                "repeat_index": 1,
                "variant": "baseline",
                "status": "passed",
                "metrics": {"agent_wall_time_ms": 100, "token_usage": {"input_tokens": 100}},
            },
            {
                "task_id": "real-001-readme-title-fix",
                "repeat_index": 1,
                "variant": "subagent-lite",
                "status": "passed",
                "metrics": {"agent_wall_time_ms": 90, "token_usage": {"input_tokens": 90}},
            },
        ]

        summary = compare_paired_benchmark.compare(
            records,
            min_complete_pairs=1,
            variants=("baseline", "subagent-lite"),
        )

        self.assertEqual(summary["conclusion"], "no-clear-winner")
        self.assertEqual(summary["wins"]["subagent-lite"], 0)
        self.assertEqual(summary["cost"]["challenger_variant"], "subagent-lite")


if __name__ == "__main__":
    unittest.main()
