from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from governed_analytics.api.app import create_app
from governed_analytics.api.dependencies import AgentStartupError, build_container
from governed_analytics.config import AgentRuntimeSettings, DatabaseSettings, ModelSettings
from governed_analytics.models.agent_fixtures import builtin_demo_scripts
from governed_analytics.runtime.runs import AnalysisRunner
from governed_analytics.tools.contracts import QueryResult


class FakeEngine:
    def __init__(self, cleanup_order: list[str] | None = None) -> None:
        self.cleanup_order = cleanup_order
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True
        if self.cleanup_order is not None:
            self.cleanup_order.append("engine")


class FixtureBackend:
    async def execute(self, validated: Any, parameters: tuple[object, ...]) -> QueryResult:
        del validated, parameters
        return QueryResult(
            query_id="a" * 64,
            columns=("gmv",),
            rows=(("125.00",),),
            row_count=1,
        )


class FakeOpenAIClient:
    def __init__(self, cleanup_order: list[str] | None = None) -> None:
        self.cleanup_order = cleanup_order

    async def close(self) -> None:
        if self.cleanup_order is not None:
            self.cleanup_order.append("client")


def runtime_settings(**overrides: object) -> AgentRuntimeSettings:
    return AgentRuntimeSettings(  # type: ignore[call-arg]
        _env_file=None,
        **overrides,  # type: ignore[arg-type]
    )


def database_settings() -> DatabaseSettings:
    return DatabaseSettings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://analytics_readonly:password@localhost/db",
    )


def app_for(settings: AgentRuntimeSettings, model: ModelSettings | None = None) -> FastAPI:
    return create_app(
        container_factory=partial(
            build_container,
            runtime_settings=settings,
            database_settings=database_settings(),
            model_settings=model or ModelSettings(_env_file=None),  # type: ignore[call-arg]
        )
    )


def install_fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cleanup_order: list[str] | None = None,
) -> FakeEngine:
    engine = FakeEngine(cleanup_order)
    monkeypatch.setattr(
        "governed_analytics.api.dependencies.create_async_database_engine",
        lambda _settings: engine,
    )
    monkeypatch.setattr(
        "governed_analytics.api.dependencies.AsyncEngineSqlExecutionBackend",
        lambda _engine: FixtureBackend(),
    )
    return engine


def test_fixture_lifespan_never_constructs_openai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "governed_analytics.api.dependencies.AsyncOpenAI",
        lambda **kwargs: calls.append(kwargs),
    )
    engine = install_fake_engine(monkeypatch)

    with TestClient(app_for(runtime_settings(runtime_mode="fixture"))) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ok"}

    assert calls == []
    assert engine.disposed


@pytest.mark.parametrize(
    ("model_name", "api_key"),
    [
        ("deepseek-v4-flash", None),
        ("different-model", SecretStr("not-a-real-key")),
    ],
)
def test_live_startup_fails_safely_before_client_without_key_or_matching_pricing(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    api_key: SecretStr | None,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "governed_analytics.api.dependencies.AsyncOpenAI",
        lambda **kwargs: calls.append(kwargs),
    )
    install_fake_engine(monkeypatch)
    model = ModelSettings(  # type: ignore[call-arg]
        _env_file=None,
        model_name=model_name,
        model_api_key=api_key,
    )

    with pytest.raises(AgentStartupError, match="agent startup failed") as raised, TestClient(
        app_for(
            runtime_settings(runtime_mode="live", live_enabled=True),
            model,
        )
    ):
        pass

    assert calls == []
    assert "deepseek" not in str(raised.value).casefold()


def test_live_mode_requires_server_side_double_switch() -> None:
    with pytest.raises(ValidationError, match="live mode requires live_enabled"):
        runtime_settings(runtime_mode="live", live_enabled=False)


def test_lifespan_cleanup_order_is_runner_then_client_then_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_order: list[str] = []
    install_fake_engine(monkeypatch, cleanup_order=cleanup_order)
    client = FakeOpenAIClient(cleanup_order)
    calls: list[dict[str, object]] = []

    def openai_factory(**kwargs: object) -> FakeOpenAIClient:
        calls.append(kwargs)
        return client

    async def shutdown(_runner: AnalysisRunner) -> None:
        cleanup_order.append("runner")

    monkeypatch.setattr("governed_analytics.api.dependencies.AsyncOpenAI", openai_factory)
    monkeypatch.setattr(AnalysisRunner, "shutdown", shutdown)
    model = ModelSettings(  # type: ignore[call-arg]
        _env_file=None,
        model_name="deepseek-v4-flash",
        model_api_key=SecretStr("not-a-real-key"),
    )

    with TestClient(
        app_for(runtime_settings(runtime_mode="live", live_enabled=True), model)
    ) as test_client:
        assert test_client.get("/readyz").status_code == 200

    assert calls == [
        {
            "api_key": "not-a-real-key",
            "base_url": "https://api.deepseek.com",
            "max_retries": 0,
        }
    ]
    assert cleanup_order == ["runner", "client", "engine"]


def _wait_for_terminal(client: TestClient, run_id: str) -> dict[str, object]:
    for _ in range(500):
        body = client.get(f"/v1/analyses/{run_id}").json()
        if body["lifecycle_status"] == "terminal":
            return cast(dict[str, object], body)
    raise AssertionError("run did not become terminal")


def test_fixture_sessions_restart_script_ordinals_for_sequential_and_concurrent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_engine(monkeypatch)
    query = {"query": "2026年6月GMV是多少？"}  # noqa: RUF001

    with TestClient(app_for(runtime_settings())) as client:
        sequential = [client.post("/v1/analyses", json=query) for _ in range(2)]
        for response in sequential:
            assert response.status_code == 202
            body = _wait_for_terminal(client, response.json()["run_id"])
            assert body["final_status"] == "completed"
        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent = tuple(
                pool.map(lambda _: client.post("/v1/analyses", json=query), range(2))
            )
        for response in concurrent:
            assert response.status_code == 202
            body = _wait_for_terminal(client, response.json()["run_id"])
            assert body["final_status"] == "completed"


def test_fixture_unknown_query_has_stable_unsupported_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_engine(monkeypatch)

    with TestClient(app_for(runtime_settings())) as client:
        response = client.post(
            "/v1/analyses",
            json={"query": "查询尚未支持的数据域"},
        )
        assert response.status_code == 202
        body = _wait_for_terminal(client, response.json()["run_id"])

    assert body["final_status"] == "unsupported"
    assert body["stop_reason"] == "unsupported_analysis"


def test_builtin_demo_library_has_exact_queries_and_complete_purpose_sequences() -> None:
    scripts = builtin_demo_scripts()
    assert tuple(scripts) == (
        "2026年6月gmv是多少？",  # noqa: RUF001
        # The full-width comma is part of the exact approved demo query.
        "比较 2026-06-01 至 06-08 与 06-08 至 06-15 的 "
        "gmv，并按区域、sku、客户分群解释下降。",  # noqa: RUF001
    )
    first_key, second_key = tuple(scripts)
    assert tuple(scripts[first_key]) == ("behavior", "plan", "action", "synthesis")
    attribution = scripts[second_key]
    assert tuple(action["purpose"] for action in attribution["action"]) == (
        "gmv_comparison",
        "region_contribution",
        "sku_contribution",
        "segment_contribution",
    )
    with pytest.raises(TypeError):
        scripts["new"] = {}  # type: ignore[index]
