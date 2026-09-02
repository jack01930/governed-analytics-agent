"""Integration contracts for versioned Oracle materialization."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from decimal import Decimal
from pathlib import Path
from typing import NoReturn

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from governed_analytics.evals.golden import load_golden_cases
from governed_analytics.evals.models import GoldenCase, QueryResult


@pytest.mark.asyncio
@pytest.mark.integration
async def test_all_oracles_execute_in_one_readonly_snapshot_and_materialize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from governed_analytics.evals import oracle

    cases = load_golden_cases("evals/datasets/golden/cases.yaml")
    observed_settings: list[tuple[str, str, str, str, str]] = []
    original_verify = oracle._verify_runtime_settings

    async def capture_runtime_settings(connection: AsyncConnection) -> None:
        settings = await original_verify(connection)
        observed_settings.append(settings)

    monkeypatch.setattr(oracle, "_verify_runtime_settings", capture_runtime_settings)

    results = await oracle.materialize_oracles(cases, tmp_path)

    assert set(results) == {case.case_id for case in cases}
    assert all((tmp_path / f"{case.case_id}.json").is_file() for case in cases)
    assert observed_settings == [
        ("on", "repeatable read", "10s", "public, pg_catalog", "UTC")
    ]
    assert results["G006"].columns == ("cause_type", "previous", "current", "delta")
    assert tuple(row[0] for row in results["G006"].rows) == (
        "south_conversion",
        "SKU-000001",
        "SKU-000002",
    )
    assert results["G011"].columns == ("channel", "conversion_rate")
    assert results["G015"].rows[0][0] == Decimal("0.98989898989898989899")
    assert results["G017"].rows == ((True,),)
    assert results["G018"].rows[0][0] == Decimal("20")
    assert results["G019"].rows[0][0] == 10
    assert results["G020"].rows[0][0] == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_second_materialization_is_byte_identical(tmp_path: Path) -> None:
    from governed_analytics.evals.oracle import materialize_oracles

    cases = load_golden_cases("evals/datasets/golden/cases.yaml")
    await materialize_oracles(cases, tmp_path)
    first = {case.case_id: (tmp_path / f"{case.case_id}.json").read_bytes() for case in cases}

    await materialize_oracles(cases, tmp_path)

    assert {
        case.case_id: (tmp_path / f"{case.case_id}.json").read_bytes() for case in cases
    } == first


@pytest.mark.asyncio
@pytest.mark.integration
async def test_failure_keeps_existing_expected_files_and_unknown_files_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from governed_analytics.evals import oracle

    cases = load_golden_cases("evals/datasets/golden/cases.yaml")
    expected_file = tmp_path / "G001.json"
    expected_file.write_bytes(b"previous expected truth\n")
    unknown_file = tmp_path / "notes.json"
    unknown_file.write_bytes(b"preserve me\n")
    original_execute = oracle._execute_oracle

    async def fail_during_second_query(
        connection: AsyncConnection, case: GoldenCase
    ) -> QueryResult:
        if case.case_id == "G002":
            raise RuntimeError("injected database URL postgresql://secret@example.invalid")
        return await original_execute(connection, case)

    monkeypatch.setattr(oracle, "_execute_oracle", fail_during_second_query)

    with pytest.raises(RuntimeError, match="injected"):
        await oracle.materialize_oracles(cases, tmp_path)

    assert expected_file.read_bytes() == b"previous expected truth\n"
    assert unknown_file.read_bytes() == b"preserve me\n"
    assert not list(tmp_path.parent.glob(f".{tmp_path.name}-staging-*"))


def test_query_result_json_is_deterministic_and_uses_json_mode() -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from governed_analytics.evals.oracle import serialize_query_result

    serialized = serialize_query_result(
        QueryResult(
            columns=("amount", "occurred_at"),
            rows=((Decimal("12.50"), datetime(2026, 6, 1, tzinfo=UTC)),),
        )
    )

    assert serialized == (
        '{\n'
        '  "columns": [\n'
        '    "amount",\n'
        '    "occurred_at"\n'
        '  ],\n'
        '  "rows": [\n'
        '    [\n'
        '      "12.50",\n'
        '      "2026-06-01T00:00:00Z"\n'
        '    ]\n'
        '  ]\n'
        '}\n'
    )


def test_cli_failure_message_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from governed_analytics.evals import oracle

    def fail_without_running(coroutine: Coroutine[object, object, object]) -> NoReturn:
        coroutine.close()
        raise RuntimeError("postgresql://analytics_readonly:password@example.invalid/database")

    monkeypatch.setattr(oracle, "load_golden_cases", lambda _: ())
    monkeypatch.setattr(asyncio, "run", fail_without_running)

    assert oracle.main(["--cases", "cases.yaml", "--output", "expected"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "oracle materialization failed\n"
    assert "password" not in captured.err
