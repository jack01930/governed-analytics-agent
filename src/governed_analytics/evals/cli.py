"""Explicitly authorized command-line entry point for baseline evaluation."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import suppress
from typing import Literal

from openai import AsyncOpenAI

from governed_analytics.config import ModelSettings
from governed_analytics.evals.pricing import ModelPricing, load_model_pricing
from governed_analytics.evals.runner import BaselineRunError, run_baseline
from governed_analytics.models.openai_compatible import OpenAICompatibleSqlGenerator
from governed_analytics.models.protocols import SqlGenerator

_PRICING_PATH = "data/pricing/qwen3.7-plus-2026-09-01.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="governed-eval")
    command = parser.add_subparsers(dest="command", required=True)
    baseline = command.add_parser("baseline")
    baseline.add_argument("--dataset", required=True, choices=("tiny", "full"))
    baseline.add_argument("--mode", required=True, choices=("fixture", "live"))
    baseline.add_argument("--live", action="store_true")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.dataset != "tiny":
        return _failure("Week 1 baseline supports only dataset tiny")
    if args.mode == "fixture":
        try:
            _run(mode="fixture")
        except BaselineRunError:
            return _failure("Fixture baseline failed")
        return 0
    if not args.live:
        return _failure("Live model calls require --live")
    try:
        settings = ModelSettings()
    except Exception:
        return _failure("MODEL settings are invalid")
    secret = settings.model_api_key
    if secret is None or not secret.get_secret_value().strip():
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
        _run(mode="live", generator=generator, pricing=pricing, client=client)
    except BaselineRunError:
        return _failure("Live baseline failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
