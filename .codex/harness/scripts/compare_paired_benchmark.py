#!/usr/bin/env python3
"""Compare paired benchmark records."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


DEFAULT_VARIANTS = ("baseline", "multimodel-lite")


def parse_variants(value: str) -> tuple[str, str]:
    variants = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(variants) != 2:
        raise ValueError("--variants must name exactly two variants, for example baseline,subagent-lite")
    if variants[0] == variants[1]:
        raise ValueError("--variants must name two different variants")
    return variants


def load_records(paths: list[Path], variants: tuple[str, str]) -> list[dict]:
    records = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if value.get("variant") in variants:
                records.append(value)
    return records


def mean(values: list[int | float]) -> float | None:
    return statistics.fmean(values) if values else None


def token_total(record: dict) -> int:
    usage = record.get("metrics", {}).get("token_usage") or {}
    return usage_token_total(usage) + advisor_usage_token_total(record)


def summarize_variant(records: list[dict]) -> dict:
    attempts = len(records)
    passed = sum(1 for record in records if record.get("status") == "passed")
    failed = sum(1 for record in records if record.get("status") == "failed")
    inconclusive = sum(1 for record in records if record.get("status") == "inconclusive")
    return {
        "attempts": attempts,
        "passed": passed,
        "failed": failed,
        "inconclusive": inconclusive,
        "pass_rate": passed / attempts if attempts else None,
        "mean_agent_wall_time_ms": mean(
            [
                record.get("metrics", {}).get("agent_wall_time_ms", 0)
                for record in records
                if record.get("metrics", {}).get("agent_wall_time_ms") is not None
            ]
        ),
        "mean_total_wall_time_ms": mean(
            [
                total_wall_time(record)
                for record in records
                if total_wall_time(record) is not None
            ]
        ),
        "mean_total_tokens": mean([token_total(record) for record in records if token_total(record)]),
        "mean_modified_files": mean(
            [
                record.get("metrics", {}).get("modified_files", 0)
                for record in records
                if record.get("metrics", {}).get("modified_files") is not None
            ]
        ),
    }


def compare(
    records: list[dict],
    min_complete_pairs: int,
    variants: tuple[str, str] = DEFAULT_VARIANTS,
) -> dict:
    baseline_variant, challenger_variant = variants
    groups: dict[tuple[str, int], dict[str, dict]] = {}
    for record in records:
        key = (record["task_id"], record["repeat_index"])
        groups.setdefault(key, {})[record["variant"]] = record

    complete_pairs = []
    incomplete_pairs = []
    comparable_statuses = {"passed", "failed"}
    for key, record_variants in sorted(groups.items()):
        if all(
            variant in record_variants and record_variants[variant].get("status") in comparable_statuses
            for variant in variants
        ):
            complete_pairs.append((key, record_variants))
        else:
            incomplete_pairs.append(
                {
                    "task_id": key[0],
                    "repeat_index": key[1],
                    "variants": {
                        variant: record_variants[variant].get("status")
                        for variant in sorted(record_variants)
                    },
                }
            )

    by_variant = {
        variant: summarize_variant([record for record in records if record.get("variant") == variant])
        for variant in variants
    }
    complete_records_by_variant = {variant: [] for variant in variants}
    pair_outcomes = []
    challenger_wins = 0
    baseline_wins = 0
    ties = 0
    for (task_id, repeat), record_variants in complete_pairs:
        baseline = record_variants[baseline_variant]
        challenger = record_variants[challenger_variant]
        complete_records_by_variant[baseline_variant].append(baseline)
        complete_records_by_variant[challenger_variant].append(challenger)
        baseline_pass = baseline.get("status") == "passed"
        challenger_pass = challenger.get("status") == "passed"
        if challenger_pass and not baseline_pass:
            winner = challenger_variant
            challenger_wins += 1
        elif baseline_pass and not challenger_pass:
            winner = baseline_variant
            baseline_wins += 1
        else:
            winner = "tie"
            ties += 1
        pair_outcomes.append(
            {
                "task_id": task_id,
                "repeat_index": repeat,
                "baseline_status": baseline.get("status"),
                "challenger_status": challenger.get("status"),
                "challenger_variant": challenger_variant,
                "winner": winner,
                "baseline_wall_time_ms": baseline.get("metrics", {}).get("agent_wall_time_ms"),
                "challenger_wall_time_ms": challenger.get("metrics", {}).get("agent_wall_time_ms"),
                "baseline_total_wall_time_ms": total_wall_time(baseline),
                "challenger_total_wall_time_ms": total_wall_time(challenger),
                "baseline_tokens": token_total(baseline),
                "challenger_tokens": token_total(challenger),
            }
        )

    complete_cost_stats = {
        variant: summarize_variant(complete_records_by_variant[variant])
        for variant in variants
    }
    cost = cost_comparison(complete_cost_stats, variants)
    if len(complete_pairs) < min_complete_pairs:
        conclusion = "inconclusive"
        reason = f"complete pairs {len(complete_pairs)} < required {min_complete_pairs}"
    elif challenger_wins > baseline_wins:
        conclusion = f"{challenger_variant}-leading"
        reason = f"{challenger_variant} has more paired pass/fail wins"
    elif baseline_wins > challenger_wins:
        conclusion = "baseline-leading"
        reason = f"{baseline_variant} has more paired pass/fail wins"
    elif cost["baseline_cost_leading"]:
        conclusion = "baseline-cost-leading"
        reason = cost["reason"]
    elif cost["challenger_cost_leading"]:
        conclusion = f"{challenger_variant}-cost-leading"
        reason = cost["reason"]
    else:
        conclusion = "no-clear-winner"
        reason = "paired pass/fail wins are tied"

    return {
        "complete_pair_count": len(complete_pairs),
        "incomplete_pairs": incomplete_pairs,
        "by_variant": by_variant,
        "complete_pair_cost_by_variant": complete_cost_stats,
        "pair_outcomes": pair_outcomes,
        "wins": {
            baseline_variant: baseline_wins,
            challenger_variant: challenger_wins,
            "tie": ties,
        },
        "variants": list(variants),
        "cost": cost,
        "conclusion": conclusion,
        "reason": reason,
    }


def cost_comparison(by_variant: dict, variants: tuple[str, str]) -> dict:
    baseline_variant, challenger_variant = variants
    baseline = by_variant[baseline_variant]
    challenger = by_variant[challenger_variant]
    baseline_wall = baseline.get("mean_total_wall_time_ms") or 0
    challenger_wall = challenger.get("mean_total_wall_time_ms") or 0
    baseline_tokens = baseline.get("mean_total_tokens") or 0
    challenger_tokens = challenger.get("mean_total_tokens") or 0
    wall_ratio = challenger_wall / baseline_wall if baseline_wall and challenger_wall else None
    token_ratio = challenger_tokens / baseline_tokens if baseline_tokens and challenger_tokens else None
    baseline_cost_leading = bool(
        (wall_ratio is not None and wall_ratio > 1.25)
        or (token_ratio is not None and token_ratio > 1.25)
    )
    challenger_cost_leading = bool(
        (wall_ratio is not None and wall_ratio < 0.8)
        and (token_ratio is not None and token_ratio < 0.8)
    )
    if baseline_cost_leading:
        reason = (
            f"paired pass/fail wins are tied, but {challenger_variant} costs exceed {baseline_variant} "
            f"(wall_ratio={format_ratio(wall_ratio)}, token_ratio={format_ratio(token_ratio)})"
        )
    elif challenger_cost_leading:
        reason = (
            f"paired pass/fail wins are tied, and {challenger_variant} costs are lower "
            f"(wall_ratio={format_ratio(wall_ratio)}, token_ratio={format_ratio(token_ratio)})"
        )
    else:
        reason = "paired pass/fail wins are tied and cost ratios are within threshold"
    return {
        "wall_ratio_challenger_over_baseline": wall_ratio,
        "token_ratio_challenger_over_baseline": token_ratio,
        "challenger_variant": challenger_variant,
        "baseline_cost_leading": baseline_cost_leading,
        "challenger_cost_leading": challenger_cost_leading,
        "threshold": 1.25,
        "reason": reason,
    }


def format_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def markdown(summary: dict, variants: tuple[str, str]) -> str:
    lines = [
        "# Paired Benchmark Comparison",
        "",
        f"Conclusion: `{summary['conclusion']}`",
        "",
        f"Reason: {summary['reason']}",
        "",
        f"Complete pairs: {summary['complete_pair_count']}",
        "",
        "| Variant | Attempts | Passed | Failed | Inconclusive | Pass rate | Mean total wall ms | Mean tokens |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in variants:
        stats = summary["by_variant"][variant]
        pass_rate = "" if stats["pass_rate"] is None else f"{stats['pass_rate']:.3f}"
        wall = "" if stats["mean_total_wall_time_ms"] is None else f"{stats['mean_total_wall_time_ms']:.0f}"
        tokens = "" if stats["mean_total_tokens"] is None else f"{stats['mean_total_tokens']:.0f}"
        lines.append(
            f"| {variant} | {stats['attempts']} | {stats['passed']} | {stats['failed']} | "
            f"{stats['inconclusive']} | {pass_rate} | {wall} | {tokens} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    parser.add_argument("--min-complete-pairs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    variants = parse_variants(args.variants)
    summary = compare(load_records(args.inputs, variants), args.min_complete_pairs, variants)
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown(summary, variants), encoding="utf-8")
    print(payload)

    if args.strict and summary["conclusion"] == "inconclusive":
        return 1
    return 0


def usage_token_total(usage: dict) -> int:
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if isinstance(input_tokens, int) or isinstance(output_tokens, int):
        return (input_tokens if isinstance(input_tokens, int) else 0) + (
            output_tokens if isinstance(output_tokens, int) else 0
        )

    total_tokens = usage.get("total_tokens")
    return total_tokens if isinstance(total_tokens, int) else 0


def advisor_usage_token_total(record: dict) -> int:
    usage = record.get("metrics", {}).get("advisor_token_usage") or {}
    total_tokens = usage.get("total_tokens")
    return total_tokens if isinstance(total_tokens, int) else 0


def total_wall_time(record: dict) -> int | None:
    metrics = record.get("metrics", {})
    total = metrics.get("total_wall_time_ms")
    if isinstance(total, int):
        return total
    agent = metrics.get("agent_wall_time_ms")
    advisor = metrics.get("advisor_wall_time_ms", 0)
    if isinstance(agent, int):
        return agent + (advisor if isinstance(advisor, int) else 0)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
