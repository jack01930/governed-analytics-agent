"""Sequential Week 3 execution seam and report derivation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from governed_analytics.agent.contracts import (
    ActionType,
    AgentRunResult,
    BehaviorAction,
    FinalAnswer,
    FinalStatus,
    GovernanceSnapshot,
    JsonValue,
    SafeTrace,
    StopReason,
)
from governed_analytics.agent.graph import run_agent
from governed_analytics.agent.modeling import StructuredModelInvoker
from governed_analytics.agent.ports import AgentContext, AgentModel, AgentTools
from governed_analytics.agent.tool_registry import SafeSqlDiagnostic
from governed_analytics.agent.tracing import InMemoryTraceRecorder
from governed_analytics.config import AgentRuntimeSettings
from governed_analytics.evals.models import QueryResult as EvalQueryResult
from governed_analytics.evals.week3.models import (
    BudgetConfiguration,
    BudgetScore,
    CandidateScore,
    FixtureScript,
    FrozenExpectedResult,
    SafeEvidenceRef,
    SafeProfileTraceMetadata,
    SafeToolTraceRef,
    SafeValidationRef,
    SuiteScore,
    Week3CaseResult,
    Week3EvaluationCase,
    Week3RunReport,
    derive_executed_manifest_sha256,
)
from governed_analytics.evals.week3.reporting import (
    Week3PublicationEvidence,
    cancel_week3_report_reservation,
    reserve_week3_report,
    write_week3_report,
)
from governed_analytics.evals.week3.scorers import (
    candidate_observations,
    execute_linkage_is_bijective,
    restore_query_result,
    score_behavior,
    score_candidate,
    score_evidence,
    score_tool_trace,
    validated_evidence_by_purpose,
)
from governed_analytics.evals.week3.suites import (
    WEEK3_ROOT,
    _load_fixture_scripts_from_snapshot,
)
from governed_analytics.models.agent_fixtures import AgentScripts, ScriptedAgentModel
from governed_analytics.pricing import ModelPricing
from governed_analytics.runtime.budgets import BudgetLedger, BudgetLimits


class Week3RunError(ValueError):
    """Stable global evaluation failure."""


_CANONICAL_PROTOCOL_HASHES = (
    "c0ec7ff77b5927210fdeda1648e32ecc71f3819d6724b0eea5092a262bb4e577",
    "01b9b184b42bde3e710124eea0861ac032ae3ebdd0dfee0e9048174ec932af88",
    "a8da032f4ea0b1c09eefc65ba11e44c84a06ec94afc867d53b1b621a36511cb5",
)
_EXPECTED_STEMS = tuple(
    [f"W3K{number:03d}" for number in range(11, 26)]
    + [
        "W3K026-confirm_decline",
        "W3K026-region_contribution",
        "W3K026-sku_contribution",
        "W3K026-segment_contribution",
    ]
)


@dataclass(frozen=True, slots=True)
class _ProtocolSnapshot:
    cases: tuple[Week3EvaluationCase, ...]
    expected_results: tuple[tuple[str, EvalQueryResult], ...]
    execution_catalog: _FixtureExecutionCatalog
    overall_manifest_sha256: str
    known_cohort_sha256: str
    heldout_cohort_sha256: str


class Week3CaseExecutor(Protocol):
    async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Week3RunArtifact:
    report: Week3RunReport
    report_dir: Path
    report_json: Path
    report_markdown: Path


@dataclass(frozen=True, slots=True)
class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return monotonic()


class _DiscardEvents:
    async def emit(self, node: str, event_type: str, data: Mapping[str, JsonValue]) -> None:
        del node, event_type, data


def _fixture_pricing() -> ModelPricing:
    return ModelPricing.model_validate(
        {
            "provider": "fixture",
            "region": "local",
            "requested_model": "fixture-agent",
            "resolved_model": "fixture-agent",
            "effective_date": date(2026, 9, 4),
            "currency": "CNY",
            "unit_tokens": 1,
            "input_token_upper_bound": 1_000_000,
            "input_price": "0",
            "output_price": "0",
            "pricing_basis": "deterministic fixture",
            "source": "https://example.invalid/fixture-pricing",
        }
    )


def _safe_failure(
    *,
    case_id: str,
    reason: StopReason,
    governance: GovernanceSnapshot | None = None,
    trace: SafeTrace | None = None,
) -> AgentRunResult:
    status = (
        FinalStatus.EXECUTION_FAILED
        if reason is StopReason.TASK_TIMEOUT
        else FinalStatus.INTERNAL_ERROR
    )
    return AgentRunResult(
        run_id=case_id,
        behavior=None,
        answer_contract=None,
        observations=(),
        observation_validations=(),
        evidence=(),
        evidence_gaps=(),
        first_candidate=None,
        repair_history=(),
        governance=governance or GovernanceSnapshot(),
        final_answer=FinalAnswer(
            status=status,
            stop_reason=reason,
            answer="评测用例受控终止。",
        ),
        safe_trace=trace or SafeTrace(),
    )


def _normalize_question(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


@dataclass(frozen=True, slots=True)
class _ExecutionSpec:
    script_ref: str
    max_tool_calls: int | None
    max_execute_calls: int | None


@dataclass(frozen=True, slots=True)
class _FixtureExecutionCatalog:
    specs: tuple[tuple[str, _ExecutionSpec], ...]
    scripts: tuple[FixtureScript, ...]

    def spec(self, case_id: str) -> _ExecutionSpec:
        matches = tuple(spec for candidate, spec in self.specs if candidate == case_id)
        if len(matches) != 1:
            raise Week3RunError("fixture case is unknown")
        return matches[0]

    def script(self, script_ref: str) -> FixtureScript:
        matches = tuple(script for script in self.scripts if script.script_id == script_ref)
        if len(matches) != 1:
            raise Week3RunError("fixture script reference is invalid")
        return matches[0]


def _execution_catalog(
    cases: tuple[Week3EvaluationCase, ...],
    scripts: tuple[FixtureScript, ...],
) -> _FixtureExecutionCatalog:
    script_ids = {script.script_id for script in scripts}
    if len(cases) != 40 or any(case.script_ref not in script_ids for case in cases):
        raise Week3RunError("fixture execution catalog is incomplete")
    return _FixtureExecutionCatalog(
        specs=tuple(
            (
                case.case_id,
                _ExecutionSpec(
                    script_ref=case.script_ref,
                    max_tool_calls=(
                        None
                        if case.budget_overrides is None
                        else case.budget_overrides.max_tool_calls
                    ),
                    max_execute_calls=(
                        None
                        if case.budget_overrides is None
                        else case.budget_overrides.max_execute_calls
                    ),
                ),
            )
            for case in cases
        ),
        scripts=scripts,
    )


def _script_library(
    question: str,
    script_ref: str,
    catalog: _FixtureExecutionCatalog,
) -> AgentScripts:
    script = catalog.script(script_ref)
    purposes: dict[str, list[object]] = {}
    for step in script.steps:
        purposes.setdefault(step.model_purpose, []).append(dict(step.output))
    return cast(AgentScripts, {_normalize_question(question): purposes})


class _BaseWeek3Executor:
    def __init__(
        self,
        *,
        tools: AgentTools,
        settings: AgentRuntimeSettings,
        pricing: ModelPricing,
    ) -> None:
        self.tools = tools
        self.settings = settings
        self.pricing = pricing
        self._clock = _Clock()

    async def _execute(
        self,
        *,
        case_id: str,
        question: str,
        model: AgentModel,
        limits: BudgetLimits,
    ) -> AgentRunResult:
        recorder = InMemoryTraceRecorder()
        budget = BudgetLedger(limits=limits, pricing=self.pricing, monotonic=self._clock.monotonic)
        context = AgentContext(
            model_invoker=StructuredModelInvoker(model, budget, recorder, self._clock),
            tools=self.tools,
            budget=budget,
            events=_DiscardEvents(),
            trace_recorder=recorder,
            clock=self._clock,
        )
        timeout = asyncio.timeout(self.settings.timeout_seconds)
        try:
            async with timeout:
                return await run_agent(run_id=case_id, query=question, context=context)
        except TimeoutError:
            if not timeout.expired():
                raise
            try:
                trace = recorder.snapshot()
            except Exception:
                trace = SafeTrace()
            try:
                governance = budget.snapshot
            except Exception:
                governance = GovernanceSnapshot()
            return _safe_failure(
                case_id=case_id,
                reason=StopReason.TASK_TIMEOUT,
                governance=governance,
                trace=trace,
            )


class FixtureWeek3CaseExecutor(_BaseWeek3Executor):
    """Fresh scripted model/budget/trace context per case over caller-owned tools."""

    def __init__(self, *, tools: AgentTools, settings: AgentRuntimeSettings) -> None:
        super().__init__(tools=tools, settings=settings, pricing=_fixture_pricing())
        self._catalog: _FixtureExecutionCatalog | None = None

    def bind_execution_catalog(self, catalog: _FixtureExecutionCatalog) -> None:
        if self._catalog is not None and self._catalog != catalog:
            raise Week3RunError("fixture executor is already bound to another snapshot")
        self._catalog = catalog

    async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
        if self._catalog is None:
            raise Week3RunError("fixture executor is not bound to a protocol snapshot")
        spec = self._catalog.spec(case_id)
        limits = BudgetLimits.from_settings(self.settings)
        if spec.max_tool_calls is not None:
            limits = replace(limits, max_tool_calls=spec.max_tool_calls)
        if spec.max_execute_calls is not None:
            limits = replace(limits, max_execute_calls=spec.max_execute_calls)
        model = ScriptedAgentModel(_script_library(question, spec.script_ref, self._catalog))
        return await self._execute(case_id=case_id, question=question, model=model, limits=limits)


class LiveWeek3CaseExecutor(_BaseWeek3Executor):
    """Live executor with no imports from fixture, expected, or Oracle data at runtime."""

    def __init__(
        self,
        *,
        model: AgentModel,
        tools: AgentTools,
        settings: AgentRuntimeSettings,
        pricing: ModelPricing,
    ) -> None:
        if model.model != pricing.requested_model:
            raise Week3RunError("live requested model does not match pricing")
        super().__init__(tools=tools, settings=settings, pricing=pricing)
        self._model = model

    async def run_case(self, *, case_id: str, question: str) -> AgentRunResult:
        result = await self._execute(
            case_id=case_id,
            question=question,
            model=self._model,
            limits=BudgetLimits.from_settings(self.settings),
        )
        model_calls = result.safe_trace.model_calls
        if (
            not model_calls
            or any(item.provider_model != self.pricing.resolved_model for item in model_calls)
            or sum(item.input_tokens for item in model_calls) != result.governance.input_tokens
            or sum(item.output_tokens for item in model_calls) != result.governance.output_tokens
        ):
            return _safe_failure(
                case_id=case_id,
                reason=StopReason.INTERNAL_ERROR,
                governance=result.governance,
                trace=result.safe_trace,
            )
        return result


def _protocol_directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise Week3RunError("Week 3 protocol snapshot is unavailable")
    return int(
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_protocol_directory(path: Path) -> int:
    absolute = path.absolute()
    fd = os.open(absolute.anchor, _protocol_directory_flags())
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, _protocol_directory_flags(), dir_fd=fd)
            old_fd = fd
            fd = -1
            try:
                os.close(old_fd)
            except BaseException:
                with suppress(OSError):
                    os.close(child)
                raise
            fd = child
        return fd
    except BaseException:
        if fd >= 0:
            with suppress(OSError):
                os.close(fd)
        raise Week3RunError("Week 3 protocol snapshot is unavailable") from None


def _protocol_stat_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _pread_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def _read_protocol_file(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    verification_fd = -1
    try:
        entry_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        fd = os.open(name, flags, dir_fd=directory_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError
        first = _pread_all(fd)
        middle = os.fstat(fd)
        second = _pread_all(fd)
        after = os.fstat(fd)
        verification_fd = os.open(name, flags, dir_fd=directory_fd)
        verification_before = os.fstat(verification_fd)
        verification_content = _pread_all(verification_fd)
        verification_after = os.fstat(verification_fd)
        entry_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        signature = _protocol_stat_signature(before)
        if (
            _protocol_stat_signature(entry_before) != signature
            or _protocol_stat_signature(middle) != signature
            or _protocol_stat_signature(after) != signature
            or _protocol_stat_signature(verification_before) != signature
            or _protocol_stat_signature(verification_after) != signature
            or _protocol_stat_signature(entry_after) != signature
            or first != second
            or first != verification_content
            or len(first) != before.st_size
        ):
            raise OSError
        return first
    except (OSError, ValueError):
        raise Week3RunError("Week 3 protocol snapshot is unavailable") from None
    finally:
        for owned_fd in (verification_fd, fd):
            if owned_fd >= 0:
                with suppress(OSError):
                    os.close(owned_fd)


def _snapshot_protocol_file(directory_fd: int, name: str) -> bytes:
    try:
        directory_before = _protocol_stat_signature(os.fstat(directory_fd))
        content = _read_protocol_file(directory_fd, name)
        directory_after = _protocol_stat_signature(os.fstat(directory_fd))
    except OSError:
        raise Week3RunError("Week 3 protocol snapshot is unavailable") from None
    if directory_before != directory_after:
        raise Week3RunError("Week 3 protocol snapshot is unavailable")
    return content


def _open_protocol_child(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _protocol_directory_flags(), dir_fd=parent_fd)
    except OSError:
        raise Week3RunError("Week 3 protocol snapshot is unavailable") from None


def _snapshot_hash(files: Mapping[str, bytes], names: set[str]) -> str:
    digest = sha256()
    for name in sorted(names):
        try:
            content = files[name]
        except KeyError:
            raise Week3RunError("Week 3 protocol snapshot is unavailable") from None
        relative = f"evals/datasets/week3/{name}".encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _snapshot_yaml_list(content: bytes) -> list[dict[str, Any]]:
    try:
        value = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError):
        raise Week3RunError("Week 3 protocol snapshot is unavailable") from None
    if not isinstance(value, list) or not value or not all(type(item) is dict for item in value):
        raise Week3RunError("Week 3 protocol snapshot is unavailable")
    return cast(list[dict[str, Any]], value)


def _snapshot_case(
    raw: dict[str, Any], *, cohort: Literal["known", "heldout"]
) -> Week3EvaluationCase:
    data = dict(raw)
    if data.get("cohort") != cohort:
        raise Week3RunError("Week 3 protocol snapshot is unavailable")
    observations = data.get("expected_observations", [])
    if not isinstance(observations, list):
        raise Week3RunError("Week 3 protocol snapshot is unavailable")
    parsed: list[dict[str, Any]] = []
    prefix = "evals/datasets/week3/expected/"
    for observation in observations:
        if type(observation) is not dict:
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        item = dict(observation)
        raw_path = item.get("expected_result_path")
        if (
            type(raw_path) is not str
            or not raw_path.startswith(prefix)
            or "/" in raw_path[len(prefix) :]
            or not raw_path.endswith(".json")
        ):
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        item["expected_result_path"] = WEEK3_ROOT / "expected" / raw_path[len(prefix) :]
        parsed.append(item)
    data["expected_observations"] = parsed
    try:
        return Week3EvaluationCase.model_validate_json(
            json.dumps(data, ensure_ascii=False, default=str), strict=True
        )
    except (TypeError, ValueError):
        raise Week3RunError("Week 3 protocol snapshot is unavailable") from None


def _snapshot_cases(files: Mapping[str, bytes]) -> tuple[Week3EvaluationCase, ...]:
    known = tuple(
        _snapshot_case(item, cohort="known")
        for item in _snapshot_yaml_list(files["known/cases.yaml"])
    )
    heldout_raw = tuple(
        _snapshot_case(item, cohort="heldout")
        for item in _snapshot_yaml_list(files["heldout/cases.yaml"])
    )
    by_known = {case.case_id: case for case in known}
    heldout = tuple(
        case.model_copy(
            update={
                "expected_observations": by_known[
                    case.expected_ref or ""
                ].expected_observations
            }
        )
        if case.expected_ref in by_known
        else case
        for case in heldout_raw
    )
    cases = (*known, *heldout)
    expected_ids = tuple(f"W3K{number:03d}" for number in range(1, 31)) + tuple(
        f"W3H{number:03d}" for number in range(1, 11)
    )
    if tuple(case.case_id for case in cases) != expected_ids:
        raise Week3RunError("Week 3 protocol snapshot is unavailable")
    return cases


def _script_dependencies(files: Mapping[str, bytes]) -> dict[str, set[str]]:
    dependencies: dict[str, set[str]] = {}
    for raw in _snapshot_yaml_list(files["scripted/scripts.yaml"]):
        script_id, steps = raw.get("script_id"), raw.get("steps")
        if type(script_id) is not str or not isinstance(steps, list):
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        refs: set[str] = set()
        for step in steps:
            if type(step) is not dict:
                raise Week3RunError("Week 3 protocol snapshot is unavailable")
            raw_ref = step.get("sql_ref")
            if raw_ref is None:
                continue
            prefix = "evals/datasets/week3/"
            if type(raw_ref) is not str or not raw_ref.startswith(prefix):
                raise Week3RunError("Week 3 protocol snapshot is unavailable")
            refs.add(raw_ref[len(prefix) :])
        dependencies[script_id] = refs
    return dependencies


def _load_protocol_snapshot() -> _ProtocolSnapshot:
    root_fd = _open_protocol_directory(WEEK3_ROOT)
    open_fds: list[int] = [root_fd]
    try:
        expected_top = {"known", "heldout", "scripted", "oracle", "expected"}
        if set(os.listdir(root_fd)) != expected_top:
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        known_fd = _open_protocol_child(root_fd, "known")
        open_fds.append(known_fd)
        heldout_fd = _open_protocol_child(root_fd, "heldout")
        open_fds.append(heldout_fd)
        scripted_fd = _open_protocol_child(root_fd, "scripted")
        open_fds.append(scripted_fd)
        oracle_fd = _open_protocol_child(root_fd, "oracle")
        open_fds.append(oracle_fd)
        expected_fd = _open_protocol_child(root_fd, "expected")
        open_fds.append(expected_fd)
        sql_fd = _open_protocol_child(scripted_fd, "sql")
        open_fds.append(sql_fd)
        if (
            set(os.listdir(known_fd)) != {"cases.yaml"}
            or set(os.listdir(heldout_fd)) != {"cases.yaml"}
            or set(os.listdir(scripted_fd)) != {"scripts.yaml", "sql"}
        ):
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        expected_names = {f"{stem}.json" for stem in _EXPECTED_STEMS}
        oracle_names = {f"{stem}.sql" for stem in _EXPECTED_STEMS}
        if (
            set(os.listdir(expected_fd)) != expected_names
            or set(os.listdir(oracle_fd)) != oracle_names
        ):
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        files: dict[str, bytes] = {
            "known/cases.yaml": _snapshot_protocol_file(known_fd, "cases.yaml"),
            "heldout/cases.yaml": _snapshot_protocol_file(heldout_fd, "cases.yaml"),
            "scripted/scripts.yaml": _snapshot_protocol_file(scripted_fd, "scripts.yaml"),
        }
        for name in sorted(expected_names):
            files[f"expected/{name}"] = _snapshot_protocol_file(expected_fd, name)
        for name in sorted(oracle_names):
            files[f"oracle/{name}"] = _snapshot_protocol_file(oracle_fd, name)
        sql_names = set(os.listdir(sql_fd))
        for name in sorted(sql_names):
            files[f"scripted/sql/{name}"] = _snapshot_protocol_file(sql_fd, name)

        cases = _snapshot_cases(files)
        scripts = _load_fixture_scripts_from_snapshot(
            files["scripted/scripts.yaml"],
            {
                name: content
                for name, content in files.items()
                if name.startswith("scripted/sql/")
            },
        )
        execution_catalog = _execution_catalog(cases, scripts)
        dependencies = _script_dependencies(files)
        if set(dependencies) != {script.script_id for script in scripts}:
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        referenced_sql = set().union(*dependencies.values())
        if referenced_sql != {f"scripted/sql/{name}" for name in sql_names}:
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        overall_names = set(files)
        overall = _snapshot_hash(files, overall_names)
        by_id = {case.case_id: case for case in cases}

        def cohort_hash(cohort: Literal["known", "heldout"]) -> str:
            selected = tuple(case for case in cases if case.cohort == cohort)
            owners = {
                case.case_id if cohort == "known" else cast(str, case.expected_ref)
                for case in selected
            }
            names = {
                "known/cases.yaml",
                "scripted/scripts.yaml",
                "known/cases.yaml" if cohort == "known" else "heldout/cases.yaml",
            }
            for case in selected:
                names.update(dependencies[case.script_ref])
            for owner_id in owners:
                for observation in by_id[owner_id].expected_observations:
                    stem = observation.expected_result_path.stem
                    names.add(f"expected/{stem}.json")
                    names.add(f"oracle/{stem}.sql")
            return _snapshot_hash(files, names)

        known_hash = cohort_hash("known")
        heldout_hash = cohort_hash("heldout")
        if (overall, known_hash, heldout_hash) != _CANONICAL_PROTOCOL_HASHES:
            raise Week3RunError("Week 3 protocol snapshot is unavailable")
        expected_results: list[tuple[str, EvalQueryResult]] = []
        for name in sorted(expected_names):
            try:
                frozen = FrozenExpectedResult.model_validate_json(
                    files[f"expected/{name}"], strict=True
                )
            except (TypeError, ValueError):
                raise Week3RunError("Week 3 protocol snapshot is unavailable") from None
            expected_results.append((name, frozen.result))
        expected_by_name = dict(expected_results)
        for case in cases[:30]:
            for observation in case.expected_observations:
                expected_result = expected_by_name[observation.expected_result_path.name]
                if set(observation.key_columns) | set(observation.numeric_columns) != set(
                    expected_result.columns
                ):
                    raise Week3RunError("Week 3 protocol snapshot is unavailable")
        return _ProtocolSnapshot(
            cases=cases,
            expected_results=tuple(expected_results),
            execution_catalog=execution_catalog,
            overall_manifest_sha256=overall,
            known_cohort_sha256=known_hash,
            heldout_cohort_sha256=heldout_hash,
        )
    except (OSError, KeyError, TypeError, ValueError):
        raise Week3RunError("Week 3 protocol snapshot is unavailable") from None
    finally:
        for fd in reversed(open_fds):
            with suppress(OSError):
                os.close(fd)


def _combine(scores: tuple[CandidateScore, ...]) -> CandidateScore | None:
    if not scores:
        return None
    result_score = min(item.result_score for item in scores)
    output = all(item.output_contract_conformant for item in scores)
    validated = all(item.answer_contract_validated for item in scores)
    executed = all(item.execution_succeeded for item in scores)
    truncated = any(item.possibly_truncated for item in scores)
    return CandidateScore(
        result_score=result_score,
        output_contract_conformant=output,
        answer_contract_validated=validated,
        execution_succeeded=executed,
        possibly_truncated=truncated,
        strict_pass=result_score == 1 and output and validated and executed and not truncated,
    )


def _safe_tool_refs(result: AgentRunResult) -> tuple[SafeToolTraceRef, ...]:
    refs = []
    for trace in result.safe_trace.tool_calls:
        arguments = dict(trace.safe_arguments)
        contract_id = cast(str | None, arguments.get("contract_id"))
        hypothesis_id = cast(str | None, arguments.get("hypothesis_id"))
        purpose = {
            ActionType.METRIC_LOOKUP: ActionType.METRIC_LOOKUP.value,
            ActionType.SCHEMA_LOOKUP: ActionType.SCHEMA_LOOKUP.value,
            ActionType.PROFILE: "profile_context",
            ActionType.EXECUTE_SQL: contract_id or hypothesis_id or ActionType.EXECUTE_SQL.value,
        }[trace.tool_name]
        safe_error = trace.safe_error
        if safe_error is not None and safe_error not in {item.value for item in SafeSqlDiagnostic}:
            safe_error = "internal_error"
        refs.append(
            SafeToolTraceRef(
                tool_name=trace.tool_name,
                purpose=purpose,
                contract_id=contract_id,
                hypothesis_id=hypothesis_id,
                query_id=trace.query_id,
                columns=trace.columns,
                row_count=trace.row_count,
                possibly_truncated=trace.possibly_truncated,
                outcome="failed" if trace.safe_error is not None else "completed",
                safe_error=safe_error,
                profile_metadata=(
                    SafeProfileTraceMetadata(
                        table_name=cast(str | None, arguments.get("table_name")),
                        column_name=cast(str | None, arguments.get("column_name")),
                        operation=cast(Any, arguments.get("operation")),
                        filter_columns=cast(tuple[str, ...], arguments.get("filter_columns", ())),
                        has_time_window=cast(bool | None, arguments.get("has_time_window")),
                        limit=cast(int | None, arguments.get("limit")),
                        time_column=cast(str | None, arguments.get("time_column")),
                    )
                    if trace.tool_name is ActionType.PROFILE
                    else None
                ),
            )
        )
    return tuple(refs)


def _result_digest(result: EvalQueryResult) -> str:
    encoded = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(encoded).hexdigest()


def _safe_validation_refs(result: AgentRunResult) -> tuple[SafeValidationRef, ...]:
    observation_counts = Counter(item.observation_id for item in result.observations)
    observations = {
        item.observation_id: item
        for item in result.observations
        if observation_counts[item.observation_id] == 1
        and item.tool_name is ActionType.EXECUTE_SQL
        and item.ok
    }
    refs: list[SafeValidationRef] = []
    for validation in result.observation_validations:
        observation = observations.get(validation.observation_id)
        if (
            observation is None
            or observation.contract_id is None
            or observation.hypothesis_id is None
            or observation.query_id is None
            or observation.row_count is None
        ):
            continue
        restored = restore_query_result(observation)
        refs.append(
            SafeValidationRef(
                observation_id=observation.observation_id,
                purpose=observation.purpose,
                contract_id=observation.contract_id,
                hypothesis_id=observation.hypothesis_id,
                query_id=observation.query_id,
                columns=observation.columns,
                row_count=observation.row_count,
                possibly_truncated=observation.possibly_truncated,
                result_sha256=(
                    None
                    if restored is None
                    else _result_digest(
                        EvalQueryResult(columns=restored.columns, rows=restored.rows)
                    )
                ),
                validation_fingerprint=validation.validation_fingerprint,
                valid=validation.valid and restored is not None,
            )
        )
    return tuple(refs)


def _evidence_refs(
    result: AgentRunResult, required_purposes: tuple[str, ...]
) -> tuple[SafeEvidenceRef, ...]:
    verified = validated_evidence_by_purpose(
        required_purposes=required_purposes,
        answer_contract=result.answer_contract,
        evidence=result.evidence,
        observations=result.observations,
        validations=result.observation_validations,
        tool_calls=result.safe_trace.tool_calls,
    )
    return tuple(
        SafeEvidenceRef(
            evidence_id=items[0].evidence_id,
            observation_id=items[0].observation_id,
            purpose=purpose,
            contract_id=items[0].contract_id,
            hypothesis_id=items[0].hypothesis_id,
            query_id=items[0].query_id,
        )
        for purpose, items in verified
        if items
    )


def _expected_execute_triples(case: Week3EvaluationCase) -> tuple[tuple[str, str, str], ...]:
    special = {
        "W3K027": (("metric_value_contract", "metric_value_contract", "metric_value"),) * 2,
        "W3K028": (("metric_value_contract", "metric_value_contract", "metric_value"),) * 2,
        "W3K029": (("gmv_comparison", "gmv_comparison", "confirm_decline"),),
        "W3K030": (("metric_value_contract", "metric_value_contract", "metric_value"),),
    }
    if case.case_id in special:
        return special[case.case_id]
    triples = []
    for expected in case.expected_observations:
        purpose = expected.purpose
        contract = "gmv_comparison" if purpose == "confirm_decline" else purpose
        hypothesis = "metric_value" if purpose == "metric_value_contract" else purpose
        triples.append((contract, contract, hypothesis))
    return tuple(triples)


def _suite_score(case: Week3EvaluationCase, result: AgentRunResult) -> SuiteScore:
    if case.case_id not in {"W3K027", "W3K028", "W3K029", "W3K030"}:
        return SuiteScore(suite=case.suite, conformant=True)
    links_ok = execute_linkage_is_bijective(
        result.observations, result.observation_validations, result.safe_trace.tool_calls
    )
    validations = {item.observation_id: item for item in result.observation_validations}
    matching = tuple(
        item
        for item in result.observations
        if item.tool_name is ActionType.EXECUTE_SQL
        and item.purpose in {"metric_value_contract", "gmv_comparison"}
    )
    if case.case_id in {"W3K027", "W3K028"}:
        first = matching[0] if matching else None
        first_validation = None if first is None else validations.get(first.observation_id)
        first_is_earliest = links_ok and result.first_candidate == first and len(matching) == 2
        first_invalid = first_validation is not None and not first_validation.valid
        valid = tuple(
            item
            for item in matching
            if (validation := validations.get(item.observation_id)) and validation.valid
        )
        final_met = len(valid) == 1 if case.case_id == "W3K027" else not valid
        return SuiteScore(
            suite=case.suite,
            first_is_earliest=first_is_earliest,
            first_validation_invalid=first_invalid,
            final_requirement_met=final_met,
            conformant=first_is_earliest and first_invalid and final_met,
        )
    if case.case_id == "W3K029":
        evidence = score_evidence(
            required_purposes=("confirm_decline",),
            answer_contract=result.answer_contract,
            evidence=result.evidence,
            observations=result.observations,
            validations=result.observation_validations,
            tool_calls=result.safe_trace.tool_calls,
        )
        conformant = links_ok and evidence.oracle_verified_sufficient
        return SuiteScore(
            suite=case.suite,
            verified_partial_evidence=conformant,
            conformant=conformant,
        )
    failed = tuple(
        item
        for item in result.safe_trace.tool_calls
        if item.tool_name is ActionType.EXECUTE_SQL and item.safe_error is not None
    )
    conformant = (
        len(failed) == 1
        and failed[0].safe_error == SafeSqlDiagnostic.READ_ONLY_POLICY.value
        and not any(
            item.tool_name is ActionType.EXECUTE_SQL and item.ok for item in result.observations
        )
        and not result.evidence
        and result.governance.repair_count == 0
    )
    return SuiteScore(
        suite=case.suite,
        policy_rejection_conformant=conformant,
        conformant=conformant,
    )


def _score_case(
    case: Week3EvaluationCase,
    result: AgentRunResult,
    settings: AgentRuntimeSettings,
    *,
    expected_results: tuple[EvalQueryResult, ...],
    expected_resolved_model: str,
    error_override: str | None = None,
) -> Week3CaseResult:
    if len(expected_results) != len(case.expected_observations):
        raise Week3RunError("publication evidence is incomplete")
    behavior = score_behavior(
        expected_action=case.expected_behavior,
        expected_missing_fields=case.expected_missing_fields,
        behavior_action=None if result.behavior is None else result.behavior.action,
        behavior_reason=None if result.behavior is None else result.behavior.reason_code.value,
        observed_missing_fields=() if result.behavior is None else result.behavior.missing_fields,
        expected_stop_reason=None
        if case.expected_stop_reason is None
        else case.expected_stop_reason.value,
    )
    purposes = tuple(item.purpose for item in case.expected_observations)
    if case.case_id == "W3K029":
        purposes = ("confirm_decline",)
    tool = score_tool_trace(
        required_tools=case.required_tools,
        forbidden_tools=case.forbidden_tools,
        tool_calls=result.safe_trace.tool_calls,
        required_execute_triples=_expected_execute_triples(case),
    )
    evidence = score_evidence(
        required_purposes=purposes,
        answer_contract=result.answer_contract,
        evidence=result.evidence,
        observations=result.observations,
        validations=result.observation_validations,
        tool_calls=result.safe_trace.tool_calls,
    )
    first_scores: list[CandidateScore] = []
    final_scores: list[CandidateScore] = []
    for expected_index, expected_observation in enumerate(case.expected_observations):
        first, final, first_link, final_link = candidate_observations(
            result, purpose=expected_observation.purpose
        )
        expected = expected_results[expected_index]
        candidates = [(final, final_link, final_scores)]
        if expected_index == 0:
            candidates.insert(0, (first, first_link, first_scores))
        for target, link, destination in candidates:
            restored = None if target is None else restore_query_result(target)
            actual = (
                None
                if restored is None
                else EvalQueryResult(columns=restored.columns, rows=restored.rows)
            )
            destination.append(
                score_candidate(
                    comparison=expected_observation.comparison,
                    key_columns=expected_observation.key_columns,
                    numeric_columns=expected_observation.numeric_columns,
                    expected=expected,
                    actual=actual,
                    answer_contract_ok=link,
                    execution_succeeded=restored is not None,
                    possibly_truncated=False if target is None else target.possibly_truncated,
                )
            )
    first_score, final_score = _combine(tuple(first_scores)), _combine(tuple(final_scores))
    limits = BudgetLimits.from_settings(settings)
    if case.budget_overrides is not None:
        if case.budget_overrides.max_tool_calls is not None:
            limits = replace(limits, max_tool_calls=case.budget_overrides.max_tool_calls)
        if case.budget_overrides.max_execute_calls is not None:
            limits = replace(limits, max_execute_calls=case.budget_overrides.max_execute_calls)
    governance = result.governance
    budget_ok = (
        governance.action_loops <= limits.max_action_loops
        and governance.llm_calls <= limits.max_llm_calls
        and governance.tool_calls <= limits.max_tool_calls
        and governance.execute_calls <= limits.max_execute_calls
        and governance.profile_calls <= limits.max_profile_calls
        and governance.repair_count <= limits.max_repairs
        and governance.repair_count == case.expected_repair_count
        and governance.committed_cost_cny <= limits.hard_cost_cny
    )
    terminal_ok = (
        result.final_answer.status is case.expected_final_status
        and result.final_answer.stop_reason is case.expected_stop_reason
    )
    case_models = tuple(
        dict.fromkeys(item.provider_model for item in result.safe_trace.model_calls)
    )
    model_identity_complete = (
        bool(result.safe_trace.model_calls)
        and case_models == (expected_resolved_model,)
        and sum(item.input_tokens for item in result.safe_trace.model_calls)
        == governance.input_tokens
        and sum(item.output_tokens for item in result.safe_trace.model_calls)
        == governance.output_tokens
    )
    evidence_score = evidence if purposes else None
    suite_score = _suite_score(case, result)
    budget_score = BudgetScore(
        conformant=budget_ok,
        action_loops=governance.action_loops,
        llm_calls=governance.llm_calls,
        tool_calls=governance.tool_calls,
        execute_calls=governance.execute_calls,
        profile_calls=governance.profile_calls,
        repair_count=governance.repair_count,
        input_tokens=governance.input_tokens,
        output_tokens=governance.output_tokens,
        committed_cost_cny=governance.committed_cost_cny,
        soft_cap_reached=governance.soft_cap_reached,
        max_action_loops=limits.max_action_loops,
        max_llm_calls=limits.max_llm_calls,
        max_tool_calls=limits.max_tool_calls,
        max_execute_calls=limits.max_execute_calls,
        max_profile_calls=limits.max_profile_calls,
        max_repairs=limits.max_repairs,
        expected_repair_count=case.expected_repair_count,
        hard_cost_cny=limits.hard_cost_cny,
    )
    validation_refs = _safe_validation_refs(result)
    return Week3CaseResult(
        case_id=case.case_id,
        cohort=case.cohort,
        suite=case.suite,
        expected_behavior=BehaviorAction(case.expected_behavior),
        expected_missing_fields=case.expected_missing_fields,
        expected_final_status=case.expected_final_status,
        expected_stop_reason=cast(StopReason, case.expected_stop_reason),
        suite_score=suite_score,
        observed_behavior=None if result.behavior is None else result.behavior.action,
        observed_behavior_reason=(None if result.behavior is None else result.behavior.reason_code),
        observed_missing_fields=(() if result.behavior is None else result.behavior.missing_fields),
        observed_final_status=result.final_answer.status,
        observed_stop_reason=result.final_answer.stop_reason,
        error_type=error_override,
        first_candidate_score=first_score,
        final_candidate_score=final_score,
        behavior_score=behavior,
        tool_score=tool,
        evidence_score=evidence_score,
        budget_score=budget_score,
        safe_tool_trace=_safe_tool_refs(result),
        safe_validation_refs=validation_refs,
        first_candidate_observation_id=(
            None if result.first_candidate is None else result.first_candidate.observation_id
        ),
        evidence_references=_evidence_refs(result, purposes),
        expected_resolved_model=expected_resolved_model,
        resolved_models=case_models,
        model_trace_calls=len(result.safe_trace.model_calls),
        model_trace_input_tokens=sum(item.input_tokens for item in result.safe_trace.model_calls),
        model_trace_output_tokens=sum(item.output_tokens for item in result.safe_trace.model_calls),
        model_identity_complete=model_identity_complete,
        repair_succeeded=(
            case.case_id == "W3K027" and suite_score.conformant if case.suite == "repair" else None
        ),
        valid_execute_count=sum(item.valid for item in validation_refs),
        natural_refusal=(
            behavior.conformant and terminal_ok and not result.safe_trace.tool_calls
            if case.expected_behavior != "execute"
            else None
        ),
    )


def _pricing_hash(pricing: ModelPricing) -> str:
    encoded = json.dumps(
        pricing.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(encoded).hexdigest()


def _rebuild_report_from_publication_evidence(
    evidence: Week3PublicationEvidence,
) -> Week3RunReport:
    if len(evidence.cases) != len(evidence.outcomes) or len(evidence.cases) != len(
        evidence.error_types
    ) or len(evidence.cases) != len(evidence.expected_results):
        raise Week3RunError("publication evidence is incomplete")
    if (
        evidence.overall_manifest_sha256,
        evidence.known_cohort_sha256,
        evidence.heldout_cohort_sha256,
    ) != _CANONICAL_PROTOCOL_HASHES:
        raise Week3RunError("publication evidence does not match the frozen protocol")
    frozen_cases_by_id = {item.case_id: item for item in evidence.canonical_cases}
    if tuple(frozen_cases_by_id) != tuple(
        f"W3K{number:03d}" for number in range(1, 31)
    ) + tuple(f"W3H{number:03d}" for number in range(1, 11)):
        raise Week3RunError("publication evidence does not match the frozen registry")
    if any(
        case.case_id not in frozen_cases_by_id
        or case != frozen_cases_by_id[case.case_id]
        or evidence.mode not in frozen_cases_by_id[case.case_id].modes
        for case in evidence.cases
    ):
        raise Week3RunError("publication evidence does not match the frozen registry")
    scored_items: list[Week3CaseResult] = []
    for case, outcome, error_type, expected_results in zip(
        evidence.cases,
        evidence.outcomes,
        evidence.error_types,
        evidence.expected_results,
        strict=True,
    ):
        try:
            scored_case = _score_case(
                case,
                outcome,
                evidence.settings,
                expected_results=expected_results,
                expected_resolved_model=evidence.pricing.resolved_model,
                error_override=error_type,
            )
        except Exception:
            scored_case = _score_case(
                case,
                _safe_failure(case_id=case.case_id, reason=StopReason.INTERNAL_ERROR),
                evidence.settings,
                expected_results=expected_results,
                expected_resolved_model=evidence.pricing.resolved_model,
                error_override="scoring_contract_failure",
            )
        scored_items.append(scored_case)
    resolved = tuple(
        dict.fromkeys(
            model
            for scored_case in scored_items
            for model in scored_case.resolved_models
        )
    )
    limits = BudgetLimits.from_settings(evidence.settings)
    overall_manifest = evidence.overall_manifest_sha256
    case_ids = tuple(case.case_id for case in evidence.cases)
    canonical_ids = tuple(
        case.case_id for case in evidence.canonical_cases if evidence.mode in case.modes
    )
    return Week3RunReport(
        mode=evidence.mode,
        overall_manifest_sha256=overall_manifest,
        known_cohort_sha256=evidence.known_cohort_sha256,
        heldout_cohort_sha256=evidence.heldout_cohort_sha256,
        executed_manifest_sha256=derive_executed_manifest_sha256(
            overall_manifest_sha256=overall_manifest,
            mode=evidence.mode,
            case_ids=case_ids,
        ),
        report_scope="canonical" if case_ids == canonical_ids else "partial_test",
        run_id=evidence.run_id,
        generated_at_utc=evidence.generated_at_utc,
        requested_model=evidence.pricing.requested_model,
        resolved_models=resolved,
        pricing_effective_date=evidence.pricing.effective_date,
        pricing_snapshot_sha256=_pricing_hash(evidence.pricing),
        budget_configuration=BudgetConfiguration(
            max_action_loops=limits.max_action_loops,
            max_llm_calls=limits.max_llm_calls,
            max_tool_calls=limits.max_tool_calls,
            max_execute_calls=limits.max_execute_calls,
            max_profile_calls=limits.max_profile_calls,
            max_repairs=limits.max_repairs,
            max_concurrent_runs=evidence.settings.max_concurrent_runs,
            timeout_seconds=limits.timeout_seconds,
            soft_cost_cny=limits.soft_cost_cny,
            hard_cost_cny=limits.hard_cost_cny,
        ),
        cases=tuple(scored_items),
    )


async def run_week3_evaluation(
    *,
    mode: Literal["fixture", "live"],
    executor: Week3CaseExecutor,
    output_root: str | Path = "artifacts/evals/week3",
    cases: tuple[Week3EvaluationCase, ...] | None = None,
    pricing: ModelPricing | None = None,
    now: Callable[[], datetime] | None = None,
    run_id: str | None = None,
) -> Week3RunArtifact:
    if mode == "fixture" and pricing is not None:
        raise Week3RunError("fixture mode does not accept pricing")
    if mode == "live" and pricing is None:
        raise Week3RunError("live mode requires pricing")
    snapshot = _load_protocol_snapshot()
    canonical_by_id = {case.case_id: case for case in snapshot.cases}
    source_cases = snapshot.cases if cases is None else cases
    if any(
        case.case_id not in canonical_by_id or case != canonical_by_id[case.case_id]
        for case in source_cases
    ):
        raise Week3RunError("evaluation cases do not match the frozen protocol")
    if mode == "fixture":
        bind_catalog = getattr(executor, "bind_execution_catalog", None)
        if bind_catalog is not None:
            bind_catalog(snapshot.execution_catalog)
    active = tuple(case for case in source_cases if mode in case.modes)
    if not active:
        raise Week3RunError("no cases are enabled for this mode")
    generated = (now or (lambda: datetime.now(UTC)))()
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise Week3RunError("evaluation clock must be timezone-aware")
    generated = generated.astimezone(UTC)
    effective_run_id = run_id or f"week3-{generated.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    settings = getattr(executor, "settings", AgentRuntimeSettings.model_validate({}))
    expected_by_name = dict(snapshot.expected_results)
    expected_results = tuple(
        tuple(
            expected_by_name[item.expected_result_path.name]
            for item in case.expected_observations
        )
        for case in active
    )
    reservation = reserve_week3_report(
        output_root, mode=mode, run_id=effective_run_id, timestamp=generated
    )
    try:
        effective_pricing = _fixture_pricing() if mode == "fixture" else cast(ModelPricing, pricing)
        outcomes: list[tuple[AgentRunResult, str | None]] = []
        for case in active:
            error_type: str | None
            try:
                outcome = await executor.run_case(case_id=case.case_id, question=case.question)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                outcome = _safe_failure(case_id=case.case_id, reason=StopReason.INTERNAL_ERROR)
                error_type = "internal_case_failure"
            else:
                if outcome.run_id != case.case_id:
                    outcome = _safe_failure(case_id=case.case_id, reason=StopReason.INTERNAL_ERROR)
                    error_type = "case_identity_mismatch"
                else:
                    error_type = None
            outcomes.append((outcome, error_type))
        publication_evidence = Week3PublicationEvidence(
            mode=mode,
            canonical_cases=snapshot.cases,
            cases=active,
            outcomes=tuple(outcome for outcome, _error_type in outcomes),
            error_types=tuple(error_type for _outcome, error_type in outcomes),
            expected_results=expected_results,
            settings=settings,
            pricing=effective_pricing,
            generated_at_utc=generated,
            run_id=effective_run_id,
            overall_manifest_sha256=snapshot.overall_manifest_sha256,
            known_cohort_sha256=snapshot.known_cohort_sha256,
            heldout_cohort_sha256=snapshot.heldout_cohort_sha256,
        )
        report = _rebuild_report_from_publication_evidence(publication_evidence)
        published = write_week3_report(
            reservation,
            report,
            publication_evidence=publication_evidence,
        )
        return Week3RunArtifact(
            report=report,
            report_dir=published.report_dir,
            report_json=published.report_json,
            report_markdown=published.report_markdown,
        )
    except BaseException:
        cancel_week3_report_reservation(reservation)
        raise


__all__ = [
    "FixtureWeek3CaseExecutor",
    "LiveWeek3CaseExecutor",
    "Week3CaseExecutor",
    "Week3RunArtifact",
    "Week3RunError",
    "run_week3_evaluation",
]
