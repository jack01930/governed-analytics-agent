from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from governed_analytics.config import ModelSettings
from governed_analytics.evals import cli


def test_fixture_never_constructs_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(cli, "_run", lambda **kwargs: None)
    assert cli.main(["baseline", "--dataset", "tiny", "--mode", "fixture"]) == 0
    assert calls == []


def test_full_and_unapproved_live_never_construct_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["baseline", "--dataset", "full", "--mode", "fixture"]) == 2
    assert capsys.readouterr().err.strip() == "Week 1 baseline supports only dataset tiny"
    assert cli.main(["baseline", "--dataset", "tiny", "--mode", "live"]) == 2
    assert capsys.readouterr().err.strip() == "Live model calls require --live"
    assert calls == []


@pytest.mark.parametrize("key", (None, "", " \t "))
def test_live_requires_nonblank_key_before_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], key: str | None
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **kwargs: calls.append(kwargs))
    settings = ModelSettings(model_api_key=key, _env_file=None)  # type: ignore[call-arg,arg-type]
    monkeypatch.setattr(cli, "ModelSettings", lambda: settings)
    assert cli.main(["baseline", "--dataset", "tiny", "--mode", "live", "--live"]) == 2
    assert capsys.readouterr().err.strip() == "MODEL_API_KEY is not configured"
    assert calls == []


@pytest.mark.parametrize(
    "argv",
    (
        ("baseline", "--dataset", "unique-dataset-marker", "--mode", "fixture"),
        ("baseline", "--dataset", "tiny", "--mode", "unique-mode-marker"),
        ("baseline", "--dataset", "tiny", "--mode", "fixture", "--unique-unknown-marker"),
    ),
)
def test_argparse_errors_are_stable_and_do_not_echo_raw_input(
    capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    assert cli.main(argv) == 2
    stderr = capsys.readouterr().err.strip()
    assert stderr == "Invalid governed-eval arguments"
    assert "unique-" not in stderr


def test_live_constructs_once_only_after_all_preflights(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    received: list[object] = []
    settings = SimpleNamespace(
        model_api_key=SecretStr("permitted-test-key"),
        model_base_url="https://workspace.example/v1",
        model_name="configured-alias",
        eval_model_name="configured-snapshot",
    )
    monkeypatch.setattr(cli, "ModelSettings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "load_model_pricing",
        lambda _path: SimpleNamespace(
            requested_model="configured-alias", resolved_model="configured-snapshot"
        ),
    )

    def fake_client(**kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(cli, "AsyncOpenAI", fake_client)
    monkeypatch.setattr(cli, "_run", lambda **kwargs: received.append(kwargs["generator"]))
    assert cli.main(["baseline", "--dataset", "tiny", "--mode", "live", "--live"]) == 0
    assert calls == [
        {
            "api_key": "permitted-test-key",
            "base_url": "https://workspace.example/v1",
            "max_retries": 0,
        }
    ]
    assert len(received) == 1


def test_mismatched_pricing_never_constructs_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    settings = SimpleNamespace(
        model_api_key=SecretStr("permitted-test-key"),
        model_base_url="https://workspace.example/v1",
        model_name="configured-alias",
        eval_model_name="different-snapshot",
    )
    monkeypatch.setattr(cli, "ModelSettings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "load_model_pricing",
        lambda _path: SimpleNamespace(
            requested_model="configured-alias", resolved_model="provider-snapshot"
        ),
    )
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["baseline", "--dataset", "tiny", "--mode", "live", "--live"]) == 2
    assert calls == []


def test_generator_construction_failure_closes_already_created_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()
    settings = SimpleNamespace(
        model_api_key=SecretStr("permitted-test-key"),
        model_base_url="https://workspace.example/v1",
        model_name="configured-alias",
        eval_model_name="configured-snapshot",
    )
    monkeypatch.setattr(cli, "ModelSettings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "load_model_pricing",
        lambda _path: SimpleNamespace(
            requested_model="configured-alias", resolved_model="configured-snapshot"
        ),
    )
    monkeypatch.setattr(cli, "AsyncOpenAI", lambda **_kwargs: client)
    monkeypatch.setattr(
        cli,
        "OpenAICompatibleSqlGenerator",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("construction failure")),
    )
    assert cli.main(["baseline", "--dataset", "tiny", "--mode", "live", "--live"]) == 2
    assert client.closed
