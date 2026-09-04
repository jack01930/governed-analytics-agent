from governed_analytics.agent.state import append_observations, new_agent_state


def test_new_state_has_no_oracle_fields_and_append_preserves_history() -> None:
    state = new_agent_state(run_id="run-1", query="2026年6月GMV是多少？")  # noqa: RUF001

    assert state["run_id"] == "run-1"
    assert state["plan_revisions"] == ()
    assert state["observations"] == ()
    assert state["observation_validations"] == ()
    assert not ({"expected_sql", "expected_rows", "oracle"} & set(state))
    assert append_observations(("first",), ("second",)) == ("first", "second")


def test_new_state_initializes_every_field_without_duplicate_final_status() -> None:
    state = new_agent_state(run_id="run-1", query="GMV")

    assert state["normalized_query"] == "GMV"
    assert state["lifecycle_status"] == "queued"
    assert state["governance"].llm_calls == 0
    assert state["final_answer"] is None
    assert "final_status" not in state
