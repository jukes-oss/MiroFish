"""Deterministic eval layer for the two execution profiles.

Contract mode checks local fixtures and prints the frozen route table.
Dev and holdout model repeats stay 待验. This command does not call a
subscription CLI, Ollama, or a paid API.
"""

from __future__ import annotations

import argparse
import sys

from app.providers.profile import _ROUTES
from contracts.check_contracts import run_expectations


def _route_line(profile: str) -> str:
    routes = _ROUTES[profile]
    return (
        f"profile={profile} "
        f"persona={routes['persona']} "
        f"agent={routes['agent']} "
        f"report={routes['report']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="推文模拟评估的确定性层")
    parser.add_argument("--profile", required=True, choices=("mixed", "subscription-only"))
    parser.add_argument("--suite", required=True, choices=("contract", "dev", "holdout"))
    parser.add_argument("--manifest", default="")
    parser.add_argument("--max-calls-per-run", type=int, default=10)
    parser.add_argument("--max-wall-seconds-per-run", type=int, default=600)
    parser.add_argument("--max-calls-total", type=int, default=0)
    parser.add_argument("--max-wall-seconds-total", type=int, default=0)
    args = parser.parse_args(argv)
    print(_route_line(args.profile))
    print("未校准输出标记为模拟备忘")
    if args.suite in ("dev", "holdout"):
        print(f"suite={args.suite} N=5 待验")
        print("未调用模型，没有把样例写成预测。")
        return 0
    failures = run_expectations()
    if failures:
        for name, expected, errors in failures:
            print(f"fixture={name} {expected} errors={len(errors)}")
        print("契约样例：失败")
        return 1
    print("契约样例：通过")
    print("N=5 模型实验：待验")
    print(
        "caps "
        f"max_calls_per_run={args.max_calls_per_run} "
        f"max_wall_seconds_per_run={args.max_wall_seconds_per_run}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
