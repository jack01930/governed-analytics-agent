from __future__ import annotations

from pathlib import Path

import pytest

from governed_analytics.evals.week3 import fixtures
from governed_analytics.evals.week3.fixtures import freeze_week3_expected


def test_freezer_accepts_only_tiny_before_database_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tiny"):
        freeze_week3_expected(dataset="large", output_root=tmp_path / "expected")  # type: ignore[arg-type]


def test_existing_output_is_rejected_before_database_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    output.mkdir()

    async def database_must_not_be_touched(_staging: Path) -> tuple[str, ...]:
        raise AssertionError("database was touched")

    monkeypatch.setattr(fixtures, "_freeze_to_staging", database_must_not_be_touched)
    with pytest.raises(FileExistsError):
        freeze_week3_expected(dataset="tiny", output_root=output)
    assert tuple(output.iterdir()) == ()


def test_freeze_failure_removes_owned_staging_and_leaves_no_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"

    async def fail_after_partial_write(staging: Path) -> tuple[str, ...]:
        (staging / "partial.json").write_text("{}", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(fixtures, "_freeze_to_staging", fail_after_partial_write)
    with pytest.raises(KeyboardInterrupt):
        freeze_week3_expected(dataset="tiny", output_root=output)
    assert not output.exists()
    assert not tuple(tmp_path.glob(".week3-expected-*"))


def test_successful_freeze_publishes_exactly_once_as_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "expected"
    names = tuple(f"W3K{index:03d}.json" for index in range(1, 20))

    async def write_complete_staging(staging: Path) -> tuple[str, ...]:
        for name in names:
            (staging / name).write_text("{}", encoding="utf-8")
        return names

    monkeypatch.setattr(fixtures, "_freeze_to_staging", write_complete_staging)
    assert freeze_week3_expected(dataset="tiny", output_root=output) == tuple(
        output / name for name in names
    )
    assert {path.name for path in output.iterdir()} == set(names)
    with pytest.raises(FileExistsError):
        freeze_week3_expected(dataset="tiny", output_root=output)


def test_freezer_module_has_no_live_model_client_dependency() -> None:
    source = Path(fixtures.__file__ or "").read_text(encoding="utf-8")
    assert "AsyncOpenAI" not in source
    assert "deepseek" not in source.casefold()
