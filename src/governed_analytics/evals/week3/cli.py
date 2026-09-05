"""Offline-first CLI composition for the Week 3 agent evaluation."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncEngine

from governed_analytics.agent.ports import AgentTools
from governed_analytics.agent.tool_registry import ToolRegistry
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings, ModelSettings
from governed_analytics.evals.cli import (
    _atomic_write_text,
    _lexical_absolute,
    _open_directory_nofollow,
    _verify_bound_identity,
    _verify_bound_regular_file,
)
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
        cleanup_control: BaseException | None = None
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
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                cleanup_control = error
            except Exception:
                pass
        if engine is not None:
            try:
                await engine.dispose()
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
                if cleanup_control is None:
                    cleanup_control = error
            except Exception:
                pass
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
        if cleanup_control is not None:
            raise cleanup_control
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


@dataclass
class _ValidatedReport:
    report_path: Path
    report_dir: Path
    directory_fd: int
    file_fd: int
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int, int]
    expected_bytes: bytes

    def _verify_parent(self) -> None:
        directory_status = os.fstat(self.directory_fd)
        if (directory_status.st_dev, directory_status.st_ino) != self.directory_identity:
            raise OSError
        path_fd = _open_directory_nofollow(self.report_dir, create_missing=False)
        try:
            path_status = os.fstat(path_fd)
            if (path_status.st_dev, path_status.st_ino) != self.directory_identity:
                raise OSError
        finally:
            os.close(path_fd)

    def verify(self) -> None:
        try:
            self._verify_parent()
            _verify_bound_regular_file(
                self.directory_fd,
                "report.json",
                expected_identity=self.file_identity,
                expected_bytes=self.expected_bytes,
                held_fd=self.file_fd,
            )
        except OSError:
            raise OSError("Week 3 report artifact is unavailable") from None

    def verify_identity(self) -> None:
        try:
            _verify_bound_identity(
                self.directory_fd,
                "report.json",
                held_fd=self.file_fd,
                expected_identity=self.file_identity,
                expected_parent_identity=self.directory_identity,
            )
        except OSError:
            raise OSError("Week 3 report artifact is unavailable") from None

    def close(self) -> None:
        file_fd, directory_fd = self.file_fd, self.directory_fd
        self.file_fd = -1
        self.directory_fd = -1
        if file_fd >= 0:
            with suppress(OSError):
                os.close(file_fd)
        if directory_fd >= 0:
            with suppress(OSError):
                os.close(directory_fd)


def _open_validated_report(
    artifact: Week3RunArtifact,
    *,
    mode: Literal["fixture", "live"],
) -> _ValidatedReport:
    directory_fd = -1
    file_fd = -1
    try:
        report = Week3RunReport.model_validate(
            artifact.report.model_dump(exclude_computed_fields=True), strict=True
        )
        if report.mode != mode or report.report_scope != "canonical":
            raise ValueError
        report_dir = _lexical_absolute(artifact.report_dir)
        report_json = _lexical_absolute(artifact.report_json)
        if report_json.name != "report.json" or report_json.parent != report_dir:
            raise ValueError
        directory_fd = _open_directory_nofollow(report_dir, create_missing=False)
        directory_status = os.fstat(directory_fd)
        file_fd = os.open(
            "report.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        file_identity, stored_bytes = _verify_bound_regular_file(
            directory_fd,
            "report.json",
            held_fd=file_fd,
        )
        stored = json.loads(
            stored_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if stored != report.model_dump(mode="json"):
            raise ValueError
        validated = _ValidatedReport(
            # Normalize only after descriptor traversal has rejected symlink components.
            report_path=Path(os.path.abspath(os.fspath(report_json))),
            report_dir=report_dir,
            directory_fd=directory_fd,
            file_fd=file_fd,
            directory_identity=(directory_status.st_dev, directory_status.st_ino),
            file_identity=file_identity,
            expected_bytes=stored_bytes,
        )
        directory_fd = -1
        file_fd = -1
        validated.verify()
        return validated
    except Week3RunError:
        raise
    except Exception:
        raise Week3RunError("Week 3 report artifact is unavailable") from None
    finally:
        if file_fd >= 0:
            with suppress(OSError):
                os.close(file_fd)
        if directory_fd >= 0:
            with suppress(OSError):
                os.close(directory_fd)


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
        validated: _ValidatedReport | None = None
        try:
            validated = _open_validated_report(artifact, mode=mode)
            _atomic_write_text(
                report_path_file,
                f"{validated.report_path}\n",
                publication_guard=validated.verify,
                final_publication_guard=validated.verify_identity,
            )
        except (OSError, Week3RunError):
            raise Week3CliError("Week 3 report pointer publication failed") from None
        finally:
            if validated is not None:
                validated.close()
    return artifact


__all__ = ["Week3CliError", "run_week3_command"]
