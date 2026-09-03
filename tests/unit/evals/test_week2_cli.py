from __future__ import annotations

import pytest

from governed_analytics.evals import cli
from governed_analytics.evals.week2_runner import Week2RunError


def test_week2_fixture_never_constructs_api_client_or_requires_an_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls: list[object] = []
    run_calls: list[object] = []
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **kwargs: client_calls.append(kwargs))
    monkeypatch.setattr(cli, "_run_week2", lambda **kwargs: run_calls.append(kwargs))

    assert cli.main(["week2", "--dataset", "tiny", "--mode", "fixture"]) == 0
    assert client_calls == []
    assert len(run_calls) == 1


def test_week2_live_without_explicit_confirmation_is_rejected_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_calls: list[object] = []
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **kwargs: client_calls.append(kwargs))

    assert cli.main(["week2", "--dataset", "tiny", "--mode", "live"]) == 2
    assert capsys.readouterr().err.strip() == "Live model calls require --live"
    assert client_calls == []


def test_week2_success_is_not_relabelled_failed_when_client_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[str] = []

    async def successful_run(**_kwargs: object) -> None:
        runs.append("published")

    class FailingCloseClient:
        async def close(self) -> None:
            raise RuntimeError("provider cleanup detail")

    monkeypatch.setattr(cli, "run_week2_evaluation", successful_run)

    cli._run_week2(mode="live", client=FailingCloseClient())  # type: ignore[arg-type]

    assert runs == ["published"]


def test_week2_run_failure_survives_a_second_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_run(**_kwargs: object) -> None:
        raise Week2RunError("sanitized run failure")

    class FailingCloseClient:
        async def close(self) -> None:
            raise RuntimeError("provider cleanup detail")

    monkeypatch.setattr(cli, "run_week2_evaluation", failed_run)

    with pytest.raises(Week2RunError, match="sanitized run failure"):
        cli._run_week2(mode="live", client=FailingCloseClient())  # type: ignore[arg-type]
