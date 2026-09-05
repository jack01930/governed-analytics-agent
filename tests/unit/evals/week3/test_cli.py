from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from governed_analytics.config import AgentRuntimeSettings, ModelSettings
from governed_analytics.evals import cli
from governed_analytics.evals.pricing import ModelPricing
from governed_analytics.evals.week3 import cli as week3_cli
from governed_analytics.evals.week3.runner import Week3RunArtifact, Week3RunError


def _artifact(path: Path) -> Week3RunArtifact:
    return cast(
        Week3RunArtifact,
        SimpleNamespace(
            report=SimpleNamespace(mode="fixture", report_scope="canonical"),
            report_dir=path.parent,
            report_json=path,
            report_markdown=path.with_suffix(".md"),
        ),
    )


def test_week3_fixture_never_constructs_api_client_or_reads_live_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls: list[dict[str, object]] = []
    run_calls: list[dict[str, object]] = []

    def forbidden_live_setting(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("fixture entered a live-only seam")

    monkeypatch.setenv("MODEL_BASE_URL", "not-a-valid-provider-url")

    def record_client(**kwargs: object) -> None:
        client_calls.append(kwargs)

    monkeypatch.setattr(week3_cli, "AsyncOpenAI", record_client)
    monkeypatch.setattr(week3_cli, "ModelSettings", forbidden_live_setting)
    monkeypatch.setattr(week3_cli, "load_model_pricing", forbidden_live_setting)

    def record_run(**kwargs: object) -> Week3RunArtifact:
        run_calls.append(kwargs)
        return _artifact(Path("report.json"))

    monkeypatch.setattr(week3_cli, "_run_week3", record_run)

    assert cli.main(["week3", "--dataset", "tiny", "--mode", "fixture"]) == 0
    assert client_calls == []
    assert len(run_calls) == 1
    assert run_calls[0]["mode"] == "fixture"


def test_week3_live_requires_cli_authorization_before_all_other_gates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    downstream: list[str] = []

    def record_runtime() -> AgentRuntimeSettings:
        downstream.append("runtime")
        return AgentRuntimeSettings()

    monkeypatch.setattr(week3_cli, "AgentRuntimeSettings", record_runtime)
    monkeypatch.setattr(
        week3_cli,
        "AsyncOpenAI",
        lambda **_kwargs: downstream.append("client"),
    )

    assert cli.main(["week3", "--dataset", "tiny", "--mode", "live"]) == 2
    assert capsys.readouterr().err.strip() == "Live model calls require --live"
    assert downstream == []


@pytest.mark.parametrize(
    ("runtime_mode", "live_enabled"),
    (("fixture", "false"), ("fixture", "true"), ("live", "false")),
)
def test_week3_live_flag_still_requires_both_server_switches(
    runtime_mode: str,
    live_enabled: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client_calls: list[object] = []
    model_calls: list[object] = []
    monkeypatch.setenv("AGENT_RUNTIME_MODE", runtime_mode)
    monkeypatch.setenv("AGENT_LIVE_ENABLED", live_enabled)
    monkeypatch.setattr(
        week3_cli,
        "AsyncOpenAI",
        lambda **kwargs: client_calls.append(kwargs),
    )
    monkeypatch.setattr(
        week3_cli,
        "ModelSettings",
        lambda: model_calls.append(object()),
    )

    assert cli.main(["week3", "--dataset", "tiny", "--mode", "live", "--live"]) == 2
    assert capsys.readouterr().err.strip() == "Agent live runtime is not authorized"
    assert model_calls == []
    assert client_calls == []


def test_week3_rejects_full_before_live_authorization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        week3_cli,
        "AgentRuntimeSettings",
        lambda: calls.append("runtime"),
    )

    assert cli.main(["week3", "--dataset", "full", "--mode", "live", "--live"]) == 2
    assert capsys.readouterr().err.strip() == "Week 3 evaluation supports only dataset tiny"
    assert calls == []


@pytest.mark.parametrize("failure", ("model", "missing_key", "pricing", "mismatch"))
def test_week3_live_gate_failures_do_not_reach_later_resources(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "live")
    monkeypatch.setenv("AGENT_LIVE_ENABLED", "true")

    def model_settings() -> object:
        events.append("model")
        if failure == "model":
            raise ValueError("private model configuration detail")
        return SimpleNamespace(
            model_api_key=(
                None
                if failure == "missing_key"
                else SimpleNamespace(get_secret_value=lambda: "opaque")
            ),
            model_name="requested-model",
            model_base_url="https://example.invalid",
        )

    def pricing() -> object:
        events.append("pricing")
        if failure == "pricing":
            raise ValueError("private pricing detail")
        return SimpleNamespace(
            requested_model="other-model" if failure == "mismatch" else "requested-model",
            resolved_model="resolved-model",
        )

    monkeypatch.setattr(week3_cli, "ModelSettings", model_settings)
    monkeypatch.setattr(week3_cli, "load_model_pricing", lambda _path: pricing())
    monkeypatch.setattr(
        week3_cli,
        "_run_week3",
        lambda **_kwargs: events.append("run"),
    )
    monkeypatch.setattr(
        week3_cli,
        "AsyncOpenAI",
        lambda **_kwargs: events.append("client"),
    )

    assert cli.main(["week3", "--dataset", "tiny", "--mode", "live", "--live"]) == 2
    error = capsys.readouterr().err.strip()
    if failure == "model":
        assert error == "MODEL settings are invalid"
        assert events == ["model"]
    elif failure == "missing_key":
        assert error == "MODEL_API_KEY is not configured"
        assert events == ["model"]
    else:
        assert error == "MODEL pricing is unavailable"
        assert events == ["model", "pricing"]


def test_week3_live_constructs_engine_before_zero_retry_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Engine:
        async def dispose(self) -> None:
            events.append("engine.dispose")

    class Client:
        async def close(self) -> None:
            events.append("client.close")

    async def successful_run(**_kwargs: object) -> Week3RunArtifact:
        events.append("run")
        return _artifact(Path("report.json"))

    def client_factory(**kwargs: object) -> Client:
        events.append(("client", kwargs["max_retries"]))
        return Client()

    def engine_factory(_settings: object) -> Engine:
        events.append("engine")
        return Engine()

    def tools_factory(_engine: object) -> object:
        events.append("tools")
        return object()

    monkeypatch.setattr(week3_cli, "DatabaseSettings", lambda: object())
    monkeypatch.setattr(week3_cli, "create_async_database_engine", engine_factory)
    monkeypatch.setattr(week3_cli, "_build_tools", tools_factory)
    monkeypatch.setattr(week3_cli, "AsyncOpenAI", client_factory)
    monkeypatch.setattr(week3_cli, "OpenAICompatibleAgentModel", lambda *_args: object())
    monkeypatch.setattr(week3_cli, "LiveWeek3CaseExecutor", lambda **_kwargs: object())
    monkeypatch.setattr(week3_cli, "run_week3_evaluation", successful_run)

    week3_cli._run_week3(
        mode="live",
        runtime_settings=AgentRuntimeSettings(),
        model_settings=cast(
            ModelSettings,
            SimpleNamespace(
                model_api_key=SimpleNamespace(get_secret_value=lambda: "opaque"),
                model_base_url="https://example.invalid",
                model_name="requested-model",
            ),
        ),
        pricing=cast(
            ModelPricing,
            SimpleNamespace(
                requested_model="requested-model",
                resolved_model="resolved-model",
            ),
        ),
    )

    assert events == [
        "engine",
        "tools",
        ("client", 0),
        "run",
        "client.close",
        "engine.dispose",
    ]


@pytest.mark.parametrize("mode", ("fixture", "live"))
def test_week3_run_failure_preserves_primary_and_cleans_client_then_engine(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Engine:
        async def dispose(self) -> None:
            events.append("engine.dispose")
            raise RuntimeError("dispose detail")

    class Client:
        async def close(self) -> None:
            events.append("client.close")
            raise RuntimeError("close detail")

    async def failed_run(**_kwargs: object) -> Week3RunArtifact:
        events.append("run")
        raise Week3RunError("primary run failure")

    monkeypatch.setattr(week3_cli, "create_async_database_engine", lambda _settings: Engine())
    monkeypatch.setattr(week3_cli, "DatabaseSettings", lambda: object())
    monkeypatch.setattr(week3_cli, "_build_tools", lambda _engine: object())
    monkeypatch.setattr(week3_cli, "run_week3_evaluation", failed_run)
    monkeypatch.setattr(week3_cli, "FixtureWeek3CaseExecutor", lambda **_kwargs: object())
    monkeypatch.setattr(week3_cli, "LiveWeek3CaseExecutor", lambda **_kwargs: object())
    monkeypatch.setattr(week3_cli, "OpenAICompatibleAgentModel", lambda *_args: object())
    monkeypatch.setattr(week3_cli, "AsyncOpenAI", lambda **_kwargs: Client())

    kwargs: dict[str, object] = {
        "mode": mode,
        "runtime_settings": AgentRuntimeSettings(),
    }
    if mode == "live":
        kwargs.update(
            model_settings=cast(
                ModelSettings,
                SimpleNamespace(
                    model_api_key=SimpleNamespace(get_secret_value=lambda: "opaque"),
                    model_base_url="https://example.invalid",
                    model_name="requested-model",
                ),
            ),
            pricing=cast(
                ModelPricing,
                SimpleNamespace(
                    requested_model="requested-model",
                    resolved_model="resolved-model",
                ),
            ),
        )

    with pytest.raises(Week3RunError, match="primary run failure"):
        week3_cli._run_week3(**kwargs)  # type: ignore[arg-type]

    expected = ["run", "engine.dispose"]
    if mode == "live":
        expected = ["run", "client.close", "engine.dispose"]
    assert events == expected


def test_week3_partial_client_initialization_still_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Engine:
        async def dispose(self) -> None:
            events.append("engine.dispose")

    monkeypatch.setattr(week3_cli, "create_async_database_engine", lambda _settings: Engine())
    monkeypatch.setattr(week3_cli, "DatabaseSettings", lambda: object())
    monkeypatch.setattr(week3_cli, "_build_tools", lambda _engine: object())
    monkeypatch.setattr(
        week3_cli,
        "AsyncOpenAI",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("client init detail")),
    )

    with pytest.raises(Week3RunError, match="Week 3 runtime initialization failed"):
        week3_cli._run_week3(
            mode="live",
            runtime_settings=AgentRuntimeSettings(),
            model_settings=cast(
                ModelSettings,
                SimpleNamespace(
                    model_api_key=SimpleNamespace(get_secret_value=lambda: "opaque"),
                    model_base_url="https://example.invalid",
                    model_name="requested-model",
                ),
            ),
            pricing=cast(
                ModelPricing,
                SimpleNamespace(
                    requested_model="requested-model",
                    resolved_model="resolved-model",
                ),
            ),
        )

    assert events == ["engine.dispose"]


def test_week3_report_pointer_uses_exact_returned_artifact_not_decoys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    returned_report = tmp_path / "returned" / "report.json"
    returned_report.parent.mkdir()
    returned_report.write_text("{}", encoding="utf-8")
    decoy = tmp_path / "newer-decoy" / "report.json"
    decoy.parent.mkdir()
    decoy.write_text("{}", encoding="utf-8")
    pointer = tmp_path / "pointer.txt"
    artifact = _artifact(returned_report)
    monkeypatch.setattr(week3_cli, "_run_week3", lambda **_kwargs: artifact)
    monkeypatch.setattr(
        week3_cli,
        "_validated_report_path",
        lambda returned, *, mode: returned.report_json.resolve(),
    )

    assert (
        cli.main(
            [
                "week3",
                "--dataset",
                "tiny",
                "--mode",
                "fixture",
                "--report-path-file",
                str(pointer),
            ]
        )
        == 0
    )
    assert pointer.read_text(encoding="utf-8") == f"{returned_report.resolve()}\n"
    assert decoy.resolve().as_posix() not in pointer.read_text(encoding="utf-8")


def test_atomic_pointer_concurrent_writers_publish_one_complete_value(tmp_path: Path) -> None:
    target = tmp_path / "pointer.txt"
    values = ("first-value\n", "second-value\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cli._atomic_write_text, target, value) for value in values]
        for future in futures:
            future.result(timeout=5)

    assert target.read_text(encoding="utf-8") in values
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_atomic_pointer_rejects_symlink_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError, match="atomic evaluation pointer unavailable"):
        cli._atomic_write_text(linked / "pointer.txt", "unsafe")

    assert not (real / "pointer.txt").exists()


def test_run_week3_keeps_engine_and_client_on_one_asyncio_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loops: list[asyncio.AbstractEventLoop] = []

    class Engine:
        async def dispose(self) -> None:
            loops.append(asyncio.get_running_loop())

    class Client:
        async def close(self) -> None:
            loops.append(asyncio.get_running_loop())

    async def successful_run(**_kwargs: object) -> Week3RunArtifact:
        loops.append(asyncio.get_running_loop())
        return _artifact(Path("report.json"))

    monkeypatch.setattr(week3_cli, "create_async_database_engine", lambda _settings: Engine())
    monkeypatch.setattr(week3_cli, "DatabaseSettings", lambda: object())
    monkeypatch.setattr(week3_cli, "_build_tools", lambda _engine: object())
    monkeypatch.setattr(week3_cli, "run_week3_evaluation", successful_run)
    monkeypatch.setattr(week3_cli, "LiveWeek3CaseExecutor", lambda **_kwargs: object())
    monkeypatch.setattr(week3_cli, "OpenAICompatibleAgentModel", lambda *_args: object())
    monkeypatch.setattr(week3_cli, "AsyncOpenAI", lambda **_kwargs: Client())

    week3_cli._run_week3(
        mode="live",
        runtime_settings=AgentRuntimeSettings(),
        model_settings=cast(
            ModelSettings,
            SimpleNamespace(
                model_api_key=SimpleNamespace(get_secret_value=lambda: "opaque"),
                model_base_url="https://example.invalid",
                model_name="requested-model",
            ),
        ),
        pricing=cast(
            ModelPricing,
            SimpleNamespace(
                requested_model="requested-model",
                resolved_model="resolved-model",
            ),
        ),
    )

    assert len(loops) == 3
    assert len({id(loop) for loop in loops}) == 1
