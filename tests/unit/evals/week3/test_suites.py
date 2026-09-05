from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest

from governed_analytics.agent.contracts import ActionType
from governed_analytics.evals.suites import load_active_suites
from governed_analytics.evals.week2_runner import _suite_manifest_sha256
from governed_analytics.evals.week3 import suites
from governed_analytics.evals.week3.suites import (
    load_fixture_scripts,
    load_week3_cases,
    week3_cohort_sha256,
    week3_manifest_sha256,
)
from governed_analytics.metrics.catalog import load_metric_catalog


def test_week3_registry_has_fixed_behavior_metrics_and_heldout_distribution() -> None:
    cases = load_week3_cases()
    known = tuple(case for case in cases if case.cohort == "known")
    heldout = tuple(case for case in cases if case.cohort == "heldout")
    behavior = tuple(case for case in known if case.suite == "behavior")
    simple = tuple(case for case in known if case.suite == "simple")

    assert len(known) == 30
    assert len(behavior) == 10
    assert Counter(case.expected_behavior for case in behavior) == {
        "clarify": 4,
        "execute": 2,
        "refuse": 2,
        "unsupported": 2,
    }
    assert len(simple) == 15
    assert {case.metric_id for case in simple} == set(load_metric_catalog("data/metrics/core.yaml"))
    assert len(heldout) == 10
    assert all(case.review_status == "approved" for case in cases)


def test_non_execute_cases_have_no_observations_or_execute_tool() -> None:
    for case in load_week3_cases():
        if case.expected_behavior != "execute":
            assert case.expected_observations == ()
            assert ActionType.EXECUTE_SQL in case.forbidden_tools


def test_heldout_only_reuses_one_known_script_with_compatible_intent() -> None:
    cases = load_week3_cases()
    by_id = {case.case_id: case for case in cases}
    scripts = {script.script_id: script for script in load_fixture_scripts()}
    for case in (item for item in cases if item.cohort == "heldout"):
        owner = by_id[case.expected_ref or ""]
        assert owner.cohort == "known"
        assert case.script_ref == owner.script_ref
        assert case.script_ref in scripts
        assert (case.suite, case.expected_behavior, case.metric_id) == (
            owner.suite,
            owner.expected_behavior,
            owner.metric_id,
        )


def test_week3_hashes_are_distinct_and_stable_hex_digests() -> None:
    overall = week3_manifest_sha256()
    known = week3_cohort_sha256("known")
    heldout = week3_cohort_sha256("heldout")
    assert all(
        len(value) == 64 and set(value) <= set("0123456789abcdef")
        for value in (overall, known, heldout)
    )
    assert len({overall, known, heldout}) == 3


@pytest.mark.parametrize("cohort", ["unknown", "KNOWN"])
def test_cohort_hash_rejects_unknown_values(cohort: str) -> None:
    with pytest.raises(ValueError):
        week3_cohort_sha256(cohort)  # type: ignore[arg-type]


def test_default_loaders_do_not_depend_on_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert len(load_week3_cases()) == 40
    assert len(load_fixture_scripts()) == 30


def _patch_week3_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    source_root = suites.WEEK3_ROOT
    target_root = repository / "evals/datasets/week3"
    target_root.parent.mkdir(parents=True)
    shutil.copytree(source_root, target_root)
    monkeypatch.setattr(suites, "_REPOSITORY_ROOT", repository)
    monkeypatch.setattr(suites, "WEEK3_ROOT", target_root)
    monkeypatch.setattr(suites, "WEEK3_KNOWN_REGISTRY", target_root / "known/cases.yaml")
    monkeypatch.setattr(suites, "WEEK3_HELDOUT_REGISTRY", target_root / "heldout/cases.yaml")
    monkeypatch.setattr(suites, "WEEK3_SCRIPTS_REGISTRY", target_root / "scripted/scripts.yaml")
    monkeypatch.setattr(suites, "WEEK3_SCRIPTED_SQL_ROOT", target_root / "scripted/sql")
    monkeypatch.setattr(suites, "WEEK3_ORACLE_ROOT", target_root / "oracle")
    monkeypatch.setattr(suites, "WEEK3_EXPECTED_ROOT", target_root / "expected")
    return target_root


def test_loader_rejects_traversal_symlink_duplicate_keys_ids_and_questions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    known = root / "known/cases.yaml"
    original = known.read_text(encoding="utf-8")

    known.write_text(
        original.replace("  cohort: known\n", "  cohort: known\n  cohort: known\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="YAML"):
        load_week3_cases()

    known.write_text(original.replace("W3K002", "W3K001", 1), encoding="utf-8")
    with pytest.raises(ValueError):
        load_week3_cases()

    question = "GMV 是多少？"  # noqa: RUF001
    known.write_text(original.replace("帮我看看最近的表现。", question), encoding="utf-8")
    with pytest.raises(ValueError, match="questions"):
        load_week3_cases()

    with pytest.raises(ValueError, match="traverse"):
        suites._repository_file("../oracle.sql", directory=root / "oracle", suffix=".sql")

    symlink = tmp_path / "registry-link.yaml"
    symlink.symlink_to(known)
    monkeypatch.setattr(suites, "WEEK3_KNOWN_REGISTRY", symlink)
    with pytest.raises(ValueError, match="YAML"):
        load_week3_cases()


def test_loader_rejects_wrong_cohort_prefix_missing_expected_or_outside_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    known = root / "known/cases.yaml"
    original = known.read_text(encoding="utf-8")

    known.write_text(original.replace("W3K001", "W3H001", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="evaluation case"):
        load_week3_cases()

    known.write_text(
        original.replace("expected/W3K011.json", "expected/missing.json", 1), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unavailable"):
        load_week3_cases()

    known.write_text(
        original.replace("evals/datasets/week3/expected/W3K011.json", "data/metrics/core.yaml", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside"):
        load_week3_cases()


def test_scripts_cannot_reference_oracle_or_expected_truth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    scripts_path = root / "scripted/scripts.yaml"
    original = scripts_path.read_text(encoding="utf-8")
    scripts_path.write_text(
        original.replace("output: {action:", "output: {oracle_query_id: secret, action:", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="truth"):
        load_fixture_scripts()

    scripts_path.write_text(original, encoding="utf-8")
    candidate = root / "scripted/sql/W3K011.sql"
    candidate.write_text(
        candidate.read_text(encoding="utf-8") + "\n-- scorer_truth\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="truth"):
        load_fixture_scripts()


@pytest.mark.parametrize("surface", ["mapping_key", "mapping_value", "purpose", "raw_sql"])
@pytest.mark.parametrize("truth_key", sorted(suites._TRUTH_KEYS))
def test_truth_isolation_rejects_every_key_on_every_raw_surface(
    surface: str, truth_key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    if surface == "raw_sql":
        candidate = root / "scripted/sql/W3K011.sql"
        candidate.write_text(
            candidate.read_text(encoding="utf-8") + f"\n-- {truth_key}\n",
            encoding="utf-8",
        )
        error = r"^candidate SQL cannot contain evaluation truth sentinels$"
    else:
        scripts_path = root / "scripted/scripts.yaml"
        original = scripts_path.read_text(encoding="utf-8")
        if surface == "mapping_key":
            mutated = original.replace(
                "output: {action:", f"output: {{{truth_key}: secret, action:", 1
            )
        elif surface == "mapping_value":
            mutated = original.replace("script_id: W3K001", f"script_id: {truth_key}", 1)
        else:
            mutated = original.replace(
                "model_purpose: behavior", f"model_purpose: {truth_key}", 1
            )
        scripts_path.write_text(mutated, encoding="utf-8")
        error = r"^fixture scripts cannot contain Oracle, expected, or scorer truth$"

    with pytest.raises(ValueError, match=error):
        load_fixture_scripts()


@pytest.mark.parametrize(
    "disguised",
    [
        "oracle_query_id",
        "oracle query id",
        "oracle-query-id",
        "oracleQueryId",
        "\uff4f\uff52\uff41\uff43\uff4c\uff45\uff3f\uff51\uff55\uff45\uff52\uff59"
        "\uff3f\uff49\uff44",
        "oracle\u200bquery\u200cid",
        "oracle q-uery id",
    ],
)
def test_truth_isolation_rejects_separator_and_unicode_disguises(
    disguised: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    candidate = root / "scripted/sql/W3K011.sql"
    candidate.write_text(
        candidate.read_text(encoding="utf-8") + f"\n-- {disguised}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match=r"^candidate SQL cannot contain evaluation truth sentinels$"
    ):
        load_fixture_scripts()


@pytest.mark.parametrize(
    "separator",
    ["\u034f", "\ufe0f", "\u2060\u034f\ufe0f\u200b"],
    ids=["combining-grapheme-joiner", "variation-selector", "mixed-default-ignorable"],
)
@pytest.mark.parametrize(
    "surface", ["mapping_key", "mapping_value", "purpose", "raw_sql", "expanded_action"]
)
@pytest.mark.parametrize("truth_key", sorted(suites._TRUTH_KEYS))
def test_truth_isolation_skips_all_non_alnum_inside_every_pattern_and_surface(
    truth_key: str, surface: str, separator: str
) -> None:
    compact = "".join(character for character in truth_key if character.isalnum())
    disguised = separator.join(compact)
    if surface == "mapping_key":
        value: object = {disguised: "safe"}
    elif surface == "mapping_value":
        value = {"safe": disguised}
    elif surface == "purpose":
        value = {"model_purpose": disguised}
    elif surface == "raw_sql":
        value = f"select 1 -- {disguised}"
    else:
        value = {"arguments": {"sql": f"select 1 -- {disguised}"}}

    assert suites._contains_truth(value)


@pytest.mark.parametrize("separator", ["\u034f", "\ufe0f", "\u2060\u034f\ufe0f\u200b"])
def test_candidate_sql_unicode_marks_fail_with_fixed_redacted_error(
    separator: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    candidate = root / "scripted/sql/W3K011.sql"
    disguised = separator.join("oraclequeryid")
    candidate.write_text(
        candidate.read_text(encoding="utf-8") + f"\n-- {disguised}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match=r"^candidate SQL cannot contain evaluation truth sentinels$"
    ):
        load_fixture_scripts()


def test_truth_isolation_uses_alphanumeric_boundaries_without_false_positive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert not suites._contains_truth("unexpected rows")
    assert not suites._contains_truth({"unexpected_rows": "safe"})
    root = _patch_week3_root(monkeypatch, tmp_path)
    candidate = root / "scripted/sql/W3K011.sql"
    candidate.write_text(
        candidate.read_text(encoding="utf-8") + "\n-- unexpected rows\n",
        encoding="utf-8",
    )

    assert len(load_fixture_scripts()) == 30


def test_expanded_action_is_scanned_after_candidate_sql_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = suites._contains_truth

    def reject_expanded_action(value: object, *, parent_key: str | None = None) -> bool:
        if isinstance(value, dict):
            arguments = value.get("arguments")
            if isinstance(arguments, dict) and "sql" in arguments:
                return True
        return original(value, parent_key=parent_key)

    monkeypatch.setattr(suites, "_contains_truth", reject_expanded_action)
    with pytest.raises(
        ValueError, match=r"^expanded fixture actions cannot contain evaluation truth$"
    ):
        load_fixture_scripts()


def test_exact_expected_evidence_field_remains_allowed_in_expanded_request() -> None:
    script = next(item for item in load_fixture_scripts() if item.script_id == "W3K011")
    action = next(step for step in script.steps if step.model_purpose == "action")

    assert action.output["expected_evidence"] == "contracted numeric result"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_expected_json_rejects_non_finite_constants_with_redacted_error(
    constant: str, tmp_path: Path
) -> None:
    expected = tmp_path / "expected.json"
    expected.write_text(
        f"""{{
  "oracle_query_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "result": {{"columns": ["gmv"], "rows": [[{constant}]]}}
}}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"^invalid frozen Week 3 expected result$"):
        suites._read_expected(expected)


def test_heldout_cannot_declare_oracle_expected_or_budget_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    heldout = root / "heldout/cases.yaml"
    original = heldout.read_text(encoding="utf-8")
    heldout.write_text(
        original.replace(
            "expected_ref: W3K011",
            "expected_ref: W3K011, budget_overrides: {max_tool_calls: 3, max_execute_calls: 3}",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="heldout"):
        load_week3_cases()


@pytest.mark.parametrize(
    "relative_path",
    [
        "known/cases.yaml",
        "heldout/cases.yaml",
        "scripted/scripts.yaml",
        "scripted/sql/W3K011.sql",
        "oracle/W3K011.sql",
        "expected/W3K011.json",
    ],
)
def test_overall_hash_covers_every_whitelisted_input_class(
    relative_path: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    before = week3_manifest_sha256()
    target = root / relative_path
    target.write_bytes(target.read_bytes() + b"\n")
    assert week3_manifest_sha256() != before


def test_heldout_hash_covers_reused_known_dependencies_without_changing_week2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _patch_week3_root(monkeypatch, tmp_path)
    week2_before = _suite_manifest_sha256(load_active_suites())
    heldout_before = week3_cohort_sha256("heldout")
    reused_candidate = root / "scripted/sql/W3K011.sql"
    reused_candidate.write_bytes(reused_candidate.read_bytes() + b"\n")

    assert week3_cohort_sha256("heldout") != heldout_before
    assert _suite_manifest_sha256(load_active_suites()) == week2_before
