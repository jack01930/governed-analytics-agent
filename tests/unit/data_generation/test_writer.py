"""Tests for the deterministic CSV serialization boundary."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]
import pytest

from governed_analytics.data_generation.writer import write_canonical_csv


def test_canonical_csv_is_stable_across_input_order_and_does_not_mutate_input(
    tmp_path: Path,
) -> None:
    first = pd.DataFrame([{"id": 2, "value": "b"}, {"id": 1, "value": "a"}])
    original = first.copy(deep=True)
    second = first.iloc[::-1].reset_index(drop=True)

    first_digest = write_canonical_csv(first, tmp_path / "first.csv", sort_by=("id",))
    second_digest = write_canonical_csv(second, tmp_path / "second.csv", sort_by=("id",))

    assert first_digest == second_digest
    assert (tmp_path / "first.csv").read_bytes() == (tmp_path / "second.csv").read_bytes()
    pd.testing.assert_frame_equal(first, original)


def test_canonical_csv_preserves_contractual_scalar_representations(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {
            "id": [2, 1],
            "occurred_at": [
                datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC),
                datetime(2025, 1, 1, tzinfo=UTC),
            ],
            "amount": [Decimal("12.30"), Decimal("0.10")],
            "maybe_id": pd.Series([None, 7], dtype="Int64"),
            "active": [False, True],
            "label": ["普通\"商品", "中文,商品"],
        }
    )

    write_canonical_csv(frame, tmp_path / "values.csv", sort_by=("id",))

    assert (tmp_path / "values.csv").read_bytes() == (
        "id,occurred_at,amount,maybe_id,active,label\n"
        '1,2025-01-01T00:00:00Z,0.10,7,True,"中文,商品"\n'
        '2,2025-01-01T00:00:01Z,12.30,,False,"普通""商品"\n'
    ).encode()


def test_canonical_csv_rejects_non_utc_datetime_and_unknown_sort_column(tmp_path: Path) -> None:
    naive = pd.DataFrame({"id": [1], "occurred_at": [datetime(2025, 1, 1)]})
    non_utc = pd.DataFrame(
        {"id": [1], "occurred_at": [datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=8)))]}
    )
    with pytest.raises(ValueError, match="UTC-aware"):
        write_canonical_csv(naive, tmp_path / "naive.csv", sort_by=("id",))
    with pytest.raises(ValueError, match="UTC"):
        write_canonical_csv(non_utc, tmp_path / "non-utc.csv", sort_by=("id",))
    frame = pd.DataFrame({"id": [1], "occurred_at": [datetime(2025, 1, 1, tzinfo=UTC)]})
    with pytest.raises(ValueError, match="sort_by"):
        write_canonical_csv(frame, tmp_path / "invalid.csv", sort_by=("missing",))
