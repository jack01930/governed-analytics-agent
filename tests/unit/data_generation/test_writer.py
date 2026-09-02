"""Tests for the deterministic CSV serialization boundary."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import numpy as np
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
        '2,2025-01-01T00:00:01Z,12.30,\\N,False,"普通""商品"\n'
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


def test_canonical_csv_distinguishes_empty_strings_from_nulls(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "text_value": pd.Series(["", pd.NA, pd.NA], dtype="string"),
            "occurred_at": [pd.NaT, pd.NaT, pd.NaT],
            "maybe_id": pd.Series([7, None, None], dtype="Int64"),
        }
    )

    write_canonical_csv(frame, tmp_path / "nulls.csv", sort_by=("id",))

    assert (tmp_path / "nulls.csv").read_bytes() == (
        b"id,text_value,occurred_at,maybe_id\n"
        b"1,,\\N,7\n"
        b"2,\\N,\\N,\\N\n"
        b"3,\\N,\\N,\\N\n"
    )


@pytest.mark.parametrize(
    "value",
    [1.0, float("nan"), float("inf"), float("-inf"), np.float64(1.0), np.float64("nan")],
)
def test_canonical_csv_rejects_all_float_values(value: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="float"):
        write_canonical_csv(
            pd.DataFrame({"id": [1], "value": [value]}),
            tmp_path / "float.csv",
            sort_by=("id",),
        )


def test_canonical_csv_rejects_nan_from_string_dtype_and_does_not_replace_artifact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "string-nan.csv"
    path.write_bytes(b"known-good\n")
    frame = pd.DataFrame(
        {
            "id": [1],
            "value": pd.Series([np.nan], dtype=pd.StringDtype(na_value=np.nan)),
        }
    )

    with pytest.raises(ValueError, match="float"):
        write_canonical_csv(frame, path, sort_by=("id",))

    assert path.read_bytes() == b"known-good\n"


def test_canonical_csv_writes_default_string_dtype_pd_na_as_null_sentinel(tmp_path: Path) -> None:
    path = tmp_path / "string-na.csv"
    frame = pd.DataFrame({"id": [1], "value": pd.Series([pd.NA], dtype="string")})

    write_canonical_csv(frame, path, sort_by=("id",))

    assert path.read_bytes() == b"id,value\n1,\\N\n"


def test_canonical_csv_rejects_reserved_null_sentinel(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved NULL sentinel"):
        write_canonical_csv(
            pd.DataFrame({"id": [1], "value": [r"\N"]}),
            tmp_path / "sentinel.csv",
            sort_by=("id",),
        )


def test_canonical_csv_hashes_large_file_without_path_read_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "large.csv"
    frame = pd.DataFrame({"id": range(700), "value": ["x" * 4096] * 700})

    def fail_read_bytes(_: Path) -> bytes:
        raise AssertionError("writer must hash CSV files incrementally")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    digest = write_canonical_csv(frame, path, sort_by=("id",))
    expected = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            expected.update(chunk)
    assert digest == expected.hexdigest()
