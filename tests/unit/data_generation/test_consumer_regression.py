"""Independent consumer-facing signal regressions for the fixed-seed producer."""

from datetime import UTC, datetime

from governed_analytics.data_generation.anomalies import inject_anomalies
from governed_analytics.data_generation.dimensions import generate_campaigns, generate_customers
from governed_analytics.data_generation.facts import _anchor_order_ids, generate_base_facts
from governed_analytics.data_generation.models import load_generator_config
from governed_analytics.data_generation.vocabulary import SOUTH_REGIONS

_PREVIOUS_START = datetime(2026, 6, 1, tzinfo=UTC)
_CURRENT_START = datetime(2026, 6, 8, tzinfo=UTC)
_CURRENT_END = datetime(2026, 6, 15, tzinfo=UTC)
_Q2_START = datetime(2026, 4, 1, tzinfo=UTC)
_Q2_END = datetime(2026, 7, 1, tzinfo=UTC)
_VALID_ORDER_STATUSES = ("paid", "completed", "refunded")


def _in_window(frame, timestamp_column: str, start: datetime, end: datetime):  # type: ignore[no-untyped-def]
    """Use the consumer half-open UTC windows without producer selection metadata."""
    return frame[frame[timestamp_column].between(start, end, inclusive="left")]


def test_tiny_declared_consumer_decline_signals_are_scoreable() -> None:
    """G002/G006 facts, calculated from output frames, all express a real decline."""
    config = load_generator_config("data/generator/tiny.yaml")
    result = inject_anomalies(config, generate_base_facts(config))
    customers = generate_customers(config).set_index("customer_id")

    def valid_items(start: datetime, end: datetime):  # type: ignore[no-untyped-def]
        orders = _in_window(result.orders, "ordered_at", start, end)
        order_ids = orders.loc[
            orders["status"].isin(_VALID_ORDER_STATUSES), "order_id"
        ]
        return result.order_items[result.order_items["order_id"].isin(order_ids)]

    previous_items = valid_items(_PREVIOUS_START, _CURRENT_START)
    current_items = valid_items(_CURRENT_START, _CURRENT_END)
    assert current_items["net_amount"].sum() < previous_items["net_amount"].sum()

    for product_id in (1, 2):
        assert (
            current_items.loc[current_items["product_id"].eq(product_id), "net_amount"].sum()
            < previous_items.loc[previous_items["product_id"].eq(product_id), "net_amount"].sum()
        )

    def south_conversion(start: datetime, end: datetime) -> float:
        sessions = _in_window(result.web_sessions, "occurred_at", start, end)
        south_sessions = sessions[
            sessions["customer_id"].map(customers["region"]).isin(SOUTH_REGIONS)
        ]
        return float(south_sessions["converted"].mean())

    assert south_conversion(_CURRENT_START, _CURRENT_END) < south_conversion(
        _PREVIOUS_START, _CURRENT_START
    )


def test_tiny_q2_campaigns_have_scoreable_attribution_rows() -> None:
    """G016 receives at least five campaign rows from generated, valid time windows."""
    config = load_generator_config("data/generator/tiny.yaml")
    facts = generate_base_facts(config)
    campaigns = generate_campaigns(config)
    q2_campaign_ids = set(
        campaigns.loc[
            (campaigns["start_at"] < _Q2_END) & (campaigns["end_at"] > _Q2_START),
            "campaign_id",
        ]
    )
    q2_attribution_campaign_ids = set(
        _in_window(facts.campaign_attributions, "attributed_at", _Q2_START, _Q2_END)[
            "campaign_id"
        ]
    )

    assert len(q2_campaign_ids & q2_attribution_campaign_ids) >= 5


def test_full_config_reserves_consumer_anchor_capacity_without_full_frames() -> None:
    """The full configuration has the same bounded anchor shape without a large DataFrame."""
    config = load_generator_config("data/generator/full.yaml")
    anchors = _anchor_order_ids(config)
    campaigns = generate_campaigns(config)
    q2_campaigns = campaigns[
        (campaigns["start_at"] < _Q2_END) & (campaigns["end_at"] > _Q2_START)
    ]

    assert len(anchors["consumer_previous"]) == 10_000
    assert len(anchors["campaign_q2"]) == 5
    assert len(q2_campaigns) >= 5
