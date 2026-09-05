"""Offline-first CLI composition for the Week 3 agent evaluation."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncEngine

from governed_analytics.agent.ports import AgentTools
from governed_analytics.agent.tool_registry import ToolRegistry
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings, ModelSettings
from governed_analytics.evals.cli import _atomic_write_text
from governed_analytics.evals.pricing import ModelPricing, load_model_pricing
from governed_analytics.evals.week3.models import Week3RunReport
from governed_analytics.evals.week3.runner import (
    FixtureWeek3CaseExecutor,
    LiveWeek3CaseExecutor,
    Week3CaseExecutor,
    Week3RunArtifact,
    Week3RunError,
    run_week3_evaluation,
)
from governed_analytics.models.agent_openai_compatible import OpenAICompatibleAgentModel
from governed_analytics.persistence.database import create_async_database_engine
from governed_analytics.tools.execution import AsyncEngineSqlExecutionBackend
from governed_analytics.tools.tools import ExecuteSqlTool, MetricTool, ProfileTool, SchemaTool

_PRICING_PATH = "data/pricing/deepseek-v4-flash-2026-09-01.yaml"


class Week3CliError(ValueError):
    """Stable, public command failure."""


def _build_tools(engine: AsyncEngine) -> AgentTools:
    backend = AsyncEngineSqlExecutionBackend(engine)
    metric_tool = MetricTool()
    if not metric_tool.list().ok:
        raise Week3RunError("Week 3 tool registry is unavailable")
    return ToolRegistry.default(
        SchemaTool(),
        metric_tool,
        ProfileTool(backend=backend),
        ExecuteSqlTool(backend=backend),
    )


def _run_week3(
    *,
    mode: Literal["fixture", "live"],
    runtime_settings: AgentRuntimeSettings,
    model_settings: ModelSettings | None = None,
    pricing: ModelPricing | None = None,
) -> Week3RunArtifact:
    async def execute() -> Week3RunArtifact:
        engine: AsyncEngine | None = None
        client: AsyncOpenAI | None = None
        artifact: Week3RunArtifact | None = None
        primary: BaseException | None = None
        runner_started = False
        cleanup_failed = False
        try:
            database = DatabaseSettings()  # type: ignore[call-arg]
            engine = create_async_database_engine(database)
            tools = _build_tools(engine)
            if mode == "fixture":
                executor: Week3CaseExecutor = FixtureWeek3CaseExecutor(
                    tools=tools, settings=runtime_settings
                )
            else:
                if model_settings is None or pricing is None:
                    raise Week3RunError("Week 3 live configuration is unavailable")
                secret = model_settings.model_api_key
                if secret is None:
                    raise Week3RunError("Week 3 live configuration is unavailable")
                client = AsyncOpenAI(
                    api_key=secret.get_secret_value(),
                    base_url=model_settings.model_base_url,
                    max_retries=0,
                )
                model = OpenAICompatibleAgentModel(client, model_settings.model_name)
                executor = LiveWeek3CaseExecutor(
                    model=model,
                    tools=tools,
                    settings=runtime_settings,
                    pricing=pricing,
                )
            runner_started = True
            artifact = await run_week3_evaluation(
                mode=mode,
                executor=executor,
                pricing=pricing if mode == "live" else None,
            )
        except BaseException as error:
            primary = error
        if client is not None:
            try:
                await client.close()
            except BaseException:
                cleanup_failed = True
        if engine is not None:
            try:
                await engine.dispose()
            except BaseException:
                cleanup_failed = True
        if primary is not None:
            if isinstance(primary, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise primary
            if isinstance(primary, Week3RunError):
                raise primary
            raise Week3RunError(
                "Week 3 evaluation failed"
                if runner_started
                else "Week 3 runtime initialization failed"
            ) from None
        if cleanup_failed:
            raise Week3RunError("Week 3 resource cleanup failed")
        if artifact is None:
            raise Week3RunError("Week 3 evaluation failed")
        return artifact

    return asyncio.run(execute())


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError:
            raise Week3RunError("Week 3 report artifact is unavailable") from None
        if stat.S_ISLNK(mode):
            raise Week3RunError("Week 3 report artifact is unavailable")


def _validated_report_path(
    artifact: Week3RunArtifact,
    *,
    mode: Literal["fixture", "live"],
) -> Path:
    try:
        report = Week3RunReport.model_validate(
            artifact.report.model_dump(exclude_computed_fields=True), strict=True
        )
        if report.mode != mode or report.report_scope != "canonical":
            raise ValueError
        _reject_symlink_components(artifact.report_dir)
        _reject_symlink_components(artifact.report_json)
        report_dir = artifact.report_dir.resolve(strict=True)
        report_json = artifact.report_json.resolve(strict=True)
        if report_json.name != "report.json" or report_json.parent != report_dir:
            raise ValueError
        status = report_json.stat()
        if not stat.S_ISREG(status.st_mode):
            raise ValueError
        stored = json.loads(
            report_json.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if stored != report.model_dump(mode="json"):
            raise ValueError
        return report_json
    except Week3RunError:
        raise
    except Exception:
        raise Week3RunError("Week 3 report artifact is unavailable") from None


def run_week3_command(
    *,
    dataset: Literal["tiny", "full"],
    mode: Literal["fixture", "live"],
    live: bool,
    report_path_file: Path | None,
) -> Week3RunArtifact:
    if dataset != "tiny":
        raise Week3CliError("Week 3 evaluation supports only dataset tiny")
    if mode == "fixture":
        try:
            runtime = AgentRuntimeSettings(runtime_mode="fixture", live_enabled=False)
            artifact = _run_week3(mode="fixture", runtime_settings=runtime)
        except Week3RunError:
            raise Week3CliError("Fixture Week 3 evaluation failed") from None
    else:
        if not live:
            raise Week3CliError("Live model calls require --live")
        try:
            runtime = AgentRuntimeSettings()
        except Exception:
            raise Week3CliError("Agent live runtime is not authorized") from None
        if runtime.runtime_mode != "live" or not runtime.live_enabled:
            raise Week3CliError("Agent live runtime is not authorized")
        try:
            model_settings = ModelSettings()
        except Exception:
            raise Week3CliError("MODEL settings are invalid") from None
        if model_settings.model_api_key is None:
            raise Week3CliError("MODEL_API_KEY is not configured")
        try:
            pricing = load_model_pricing(_PRICING_PATH)
        except Exception:
            raise Week3CliError("MODEL pricing is unavailable") from None
        if pricing.requested_model != model_settings.model_name:
            raise Week3CliError("MODEL pricing is unavailable")
        try:
            artifact = _run_week3(
                mode="live",
                runtime_settings=runtime,
                model_settings=model_settings,
                pricing=pricing,
            )
        except Week3RunError:
            raise Week3CliError("Live Week 3 evaluation failed") from None
    if report_path_file is not None:
        try:
            report_path = _validated_report_path(artifact, mode=mode)
            _atomic_write_text(report_path_file, f"{report_path}\n")
        except (OSError, Week3RunError):
            raise Week3CliError("Week 3 report pointer publication failed") from None
    return artifact


__all__ = ["Week3CliError", "run_week3_command"]
