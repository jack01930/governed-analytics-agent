import numpy as np

from governed_analytics.data_generation.randomness import named_rng


def test_named_stream_is_repeatable_and_namespace_isolated() -> None:
    first = named_rng(20260901, "orders").integers(0, 10_000, size=10)
    second = named_rng(20260901, "orders").integers(0, 10_000, size=10)
    customers = named_rng(20260901, "customers").integers(0, 10_000, size=10)

    np.testing.assert_array_equal(first, second)
    assert not np.array_equal(first, customers)


def test_named_stream_does_not_depend_on_global_numpy_state() -> None:
    np.random.seed(1)
    first = named_rng(20260901, "orders").integers(0, 10_000, size=10)
    np.random.seed(999)
    second = named_rng(20260901, "orders").integers(0, 10_000, size=10)

    np.testing.assert_array_equal(first, second)
