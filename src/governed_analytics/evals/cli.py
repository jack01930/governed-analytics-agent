"""Explicitly authorized command-line entry point for baseline evaluation."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import suppress
from typing import Literal, Never

from openai import AsyncOpenAI

from governed_analytics.config import ModelSettings
from governed_analytics.evals.pricing import ModelPricing, load_model_pricing
from governed_analytics.evals.runner import BaselineRunError, run_baseline
from governed_analytics.evals.week2_runner import Week2RunError, run_week2_evaluation
from governed_analytics.models.openai_compatible import OpenAICompatibleSqlGenerator
from governed_analytics.models.protocols import EvaluationSqlGenerator, SqlGenerator

_PRICING_PATH = "data/pricing/deepseek-v4-flash-2026-09-01.yaml"


class _CliArgumentError(ValueError):
    """Internal marker for a public, input-free argparse failure."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise _CliArgumentError


def _parser() -> argparse.ArgumentParser:
    parser = _StableArgumentParser(prog="governed-eval")
    command = parser.add_subparsers(dest="command", required=True)
    baseline = command.add_parser("baseline")
    baseline.add_argument("--dataset", required=True, choices=("tiny", "full"))
    baseline.add_argument("--mode", required=True, choices=("fixture", "live"))
    baseline.add_argument("--live", action="store_true")
    week2 = command.add_parser("week2")
    week2.add_argument("--dataset", required=True, choices=("tiny", "full"))
    week2.add_argument("--mode", required=True, choices=("fixture", "live"))
    week2.add_argument("--live", action="store_true")
    return parser


def _failure(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _run(
    *,
    mode: Literal["fixture", "live"],
    generator: SqlGenerator | None = None,
    pricing: ModelPricing | None = None,
    client: AsyncOpenAI | None = None,
) -> None:
    async def execute() -> None:
        try:
            await run_baseline(mode=mode, generator=generator, pricing=pricing)
        finally:
            if client is not None:
                await client.close()

    try:
        asyncio.run(execute())
    except BaselineRunError:
        raise
    except Exception:
        raise BaselineRunError("baseline client cleanup failed") from None


def _run_week2(
    *,
    mode: Literal["fixture", "live"],
    generator: EvaluationSqlGenerator | None = None,
    pricing: ModelPricing | None = None,
    client: AsyncOpenAI | None = None,
) -> None:
    async def execute() -> None:
        try:
            await run_week2_evaluation(mode=mode, generator=generator, pricing=pricing)
        finally:
            if client is not None:
                # The report may already contain 50 paid, immutable outcomes.
                # A best-effort transport cleanup must never relabel that run as
                # failed and encourage an accidental paid rerun.
                with suppress(Exception):
                    await client.close()

    try:
        asyncio.run(execute())
    except Week2RunError:
        raise
    except Exception:
        raise Week2RunError("Week 2 client cleanup failed") from None


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _CliArgumentError:
        return _failure("Invalid governed-eval arguments")
    is_week2 = args.command == "week2"
    if args.dataset != "tiny":
        return _failure(
            "Week 2 evaluation supports only dataset tiny"
            if is_week2
            else "Week 1 baseline supports only dataset tiny"
        )
    if args.mode == "fixture":
        try:
            if is_week2:
                _run_week2(mode="fixture")
            else:
                _run(mode="fixture")
        except (BaselineRunError, Week2RunError):
            return _failure(
                "Fixture Week 2 evaluation failed" if is_week2 else "Fixture baseline failed"
            )
        return 0
    if not args.live:
        return _failure("Live model calls require --live")
    try:
        settings = ModelSettings()
    except Exception:
        return _failure("MODEL settings are invalid")
    secret = settings.model_api_key
    if secret is None:
        return _failure("MODEL_API_KEY is not configured")
    try:
        pricing = load_model_pricing(_PRICING_PATH)
    except Exception:
        return _failure("MODEL pricing is unavailable")
    if (
        pricing.requested_model != settings.model_name
        or pricing.resolved_model != settings.eval_model_name
    ):
        return _failure("MODEL pricing is unavailable")
    client: AsyncOpenAI | None = None
    try:
        client = AsyncOpenAI(
            api_key=secret.get_secret_value(),
            base_url=settings.model_base_url,
            max_retries=0,
        )
        generator = OpenAICompatibleSqlGenerator(client, settings.model_name)
    except Exception:
        if client is not None:
            with suppress(Exception):
                asyncio.run(client.close())
        return _failure("MODEL client is unavailable")
    try:
        if is_week2:
            _run_week2(mode="live", generator=generator, pricing=pricing, client=client)
        else:
            _run(mode="live", generator=generator, pricing=pricing, client=client)
    except (BaselineRunError, Week2RunError):
        return _failure("Live Week 2 evaluation failed" if is_week2 else "Live baseline failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
