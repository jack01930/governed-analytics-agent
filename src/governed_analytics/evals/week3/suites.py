"""Strict, cwd-independent Week 3 registry, script, truth, and hash loaders."""

from __future__ import annotations

import json
import stat
import unicodedata
from collections import Counter
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from governed_analytics.agent.contracts import (
    AnalysisAction,
    BehaviorDecision,
    FinalAnswer,
    TypedMetricPlan,
)
from governed_analytics.evals.week3.models import (
    FixtureModelStep,
    FixtureScript,
    FrozenExpectedResult,
    Week3Cohort,
    Week3EvaluationCase,
)
from governed_analytics.safety.sql_policy import SqlPolicyError, validate_sql

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
WEEK3_ROOT = _REPOSITORY_ROOT / "evals/datasets/week3"
WEEK3_KNOWN_REGISTRY = WEEK3_ROOT / "known/cases.yaml"
WEEK3_HELDOUT_REGISTRY = WEEK3_ROOT / "heldout/cases.yaml"
WEEK3_SCRIPTS_REGISTRY = WEEK3_ROOT / "scripted/scripts.yaml"
WEEK3_SCRIPTED_SQL_ROOT = WEEK3_ROOT / "scripted/sql"
WEEK3_ORACLE_ROOT = WEEK3_ROOT / "oracle"
WEEK3_EXPECTED_ROOT = WEEK3_ROOT / "expected"

_METRIC_ORDER = (
    "gmv",
    "paid_gmv",
    "net_revenue",
    "valid_order_count",
    "average_order_value",
    "payment_success_rate",
    "refund_amount",
    "refund_rate",
    "active_customers",
    "new_customers",
    "repeat_purchase_rate",
    "customer_acquisition_cost",
    "conversion_rate",
    "stockout_rate",
    "campaign_roi",
)
_EXPECTED_STEMS = tuple(
    [f"W3K{number:03d}" for number in range(11, 26)]
    + [
        "W3K026-confirm_decline",
        "W3K026-region_contribution",
        "W3K026-sku_contribution",
        "W3K026-segment_contribution",
    ]
)
_KNOWN_IDS = tuple(f"W3K{number:03d}" for number in range(1, 31))
_HELDOUT_IDS = tuple(f"W3H{number:03d}" for number in range(1, 11))
_TRUTH_KEYS = frozenset(
    {
        "expected_result_path",
        "expected_rows",
        "expected_sql",
        "oracle_sql_path",
        "oracle_query_id",
        "scorer_truth",
        "ground_truth",
    }
)
class _UniqueSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


def _construct_unique_mapping(
    loader: _UniqueSafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate YAML mapping key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON mapping key")
        result[key] = value
    return result


def _reject_non_finite_json_constant(_constant: str) -> None:
    raise ValueError("non-finite JSON constants are forbidden")


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError("symlink paths are forbidden")
        except FileNotFoundError:
            raise ValueError("required Week 3 file is unavailable") from None


def _regular_file(path: Path) -> Path:
    try:
        _reject_symlink_components(path)
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError:
        raise ValueError("required Week 3 file is unavailable") from None
    if not stat.S_ISREG(mode):
        raise ValueError("Week 3 paths must identify regular files")
    return path


def _repository_file(raw: object, *, directory: Path, suffix: str) -> Path:
    if type(raw) is not str or not raw or "\\" in raw:
        raise ValueError("Week 3 references must be repository-relative POSIX paths")
    relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Week 3 references cannot traverse or be absolute")
    candidate = _REPOSITORY_ROOT / relative
    try:
        candidate.relative_to(directory)
    except ValueError:
        raise ValueError("Week 3 reference is outside its allowed directory") from None
    if candidate.suffix != suffix:
        raise ValueError("Week 3 reference has an invalid suffix")
    return _regular_file(candidate)


def _read_yaml_list(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        raw = yaml.load(_regular_file(path).read_text(encoding="utf-8"), Loader=_UniqueSafeLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError, TypeError) as error:
        raise ValueError(f"invalid Week 3 {label} YAML") from error
    if not isinstance(raw, list) or not raw or not all(type(item) is dict for item in raw):
        raise ValueError(f"Week 3 {label} must be a nonempty list of mappings")
    return cast(list[dict[str, Any]], raw)


def _read_expected(path: Path) -> FrozenExpectedResult:
    try:
        raw = json.loads(
            _regular_file(path).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json_constant,
        )
        return FrozenExpectedResult.model_validate_json(
            json.dumps(raw, ensure_ascii=False), strict=True
        )
    except (OSError, UnicodeDecodeError, TypeError, ValueError, ValidationError) as error:
        raise ValueError("invalid frozen Week 3 expected result") from error


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _normalize_truth_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _truth_separator(character: str) -> bool:
    category = unicodedata.category(character)
    return (
        character == "_"
        or character.isspace()
        or category.startswith(("P", "Z"))
        or category == "Cf"
    )


_TRUTH_PATTERNS = tuple(
    "".join(character for character in _normalize_truth_text(key) if character.isalnum())
    for key in sorted(_TRUTH_KEYS)
)


def _matches_truth_pattern(text: str, pattern: str) -> bool:
    for start, character in enumerate(text):
        if character != pattern[0] or (start > 0 and text[start - 1].isalnum()):
            continue
        position = start
        pattern_position = 0
        while position < len(text) and pattern_position < len(pattern):
            if text[position] == pattern[pattern_position]:
                position += 1
                pattern_position += 1
            elif pattern_position > 0 and _truth_separator(text[position]):
                position += 1
            else:
                break
        if pattern_position == len(pattern) and (
            position == len(text) or not text[position].isalnum()
        ):
            return True
    return False


def _contains_truth_text(text: str) -> bool:
    normalized = _normalize_truth_text(text)
    return any(_matches_truth_pattern(normalized, pattern) for pattern in _TRUTH_PATTERNS)


def _key_tokens(key: str) -> tuple[str, ...]:
    normalized = _normalize_truth_text(key)
    tokens: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def _truth_key(key: str) -> bool:
    tokens = _key_tokens(key)
    return (
        _contains_truth_text(key)
        or "oracle" in tokens
        or "scorer" in tokens
        or "expected" in tokens
        or ("ground" in tokens and "truth" in tokens)
    )


def _contains_truth(value: object, *, parent_key: str | None = None) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                return True
            normalized = _normalize_truth_text(key)
            if normalized == "expected_evidence":
                if _contains_truth(item, parent_key=normalized):
                    return True
                continue
            if _truth_key(key):
                return True
            if _contains_truth(item, parent_key=normalized):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_truth(item, parent_key=parent_key) for item in value)
    if not isinstance(value, str):
        return False
    return _contains_truth_text(value)


def _strict_contract(model: type[Any], value: object) -> None:
    try:
        # Production FrozenJsonObject intentionally materializes MappingProxyType in its
        # before-validator, so Pydantic's global strict-dict switch is incompatible with
        # the real contract. JSON parsing plus the production model's extra=forbid and
        # semantic validators is the strict wire boundary used here.
        model.model_validate_json(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError(f"invalid fixture {model.__name__} output") from error


def _parse_fixture_script(raw: dict[str, Any]) -> FixtureScript:
    if _contains_truth(raw):
        raise ValueError("fixture scripts cannot contain Oracle, expected, or scorer truth")
    script_id = raw.get("script_id")
    if type(script_id) is not str:
        raise ValueError("fixture script_id must be a string")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("fixture script steps must be a list")
    parsed_steps: list[FixtureModelStep] = []
    for raw_step in raw_steps:
        if type(raw_step) is not dict:
            raise ValueError("fixture steps must be mappings")
        step_data = dict(raw_step)
        if (
            type(step_data.get("model_purpose")) is not str
            or type(step_data.get("ordinal")) is not int
            or type(step_data.get("output")) is not dict
            or (step_data.get("sql_ref") is not None and type(step_data.get("sql_ref")) is not str)
        ):
            raise ValueError("fixture step wire types are invalid")
        raw_ref = step_data.get("sql_ref")
        sql_path = None
        if raw_ref is not None:
            sql_path = _repository_file(raw_ref, directory=WEEK3_SCRIPTED_SQL_ROOT, suffix=".sql")
            step_data["sql_ref"] = sql_path
        try:
            step = FixtureModelStep.model_validate(step_data)
        except ValidationError as error:
            raise ValueError("invalid fixture model step") from error
        output = cast(dict[str, object], _thaw(step.output))
        purpose = step.model_purpose
        if purpose in {"action", "repair"}:
            arguments = output.get("arguments")
            if not isinstance(arguments, dict) or "sql" in arguments or sql_path is None:
                raise ValueError("fixture actions require one external candidate sql_ref")
            sql = sql_path.read_text(encoding="utf-8")
            if _contains_truth(sql):
                raise ValueError("candidate SQL cannot contain evaluation truth sentinels")
            expanded = {**output, "arguments": {**arguments, "sql": sql}}
            if _contains_truth(expanded):
                raise ValueError("expanded fixture actions cannot contain evaluation truth")
            _strict_contract(AnalysisAction, expanded)
            if sql_path.name == "W3K030-dangerous.sql":
                try:
                    validate_sql(sql)
                except SqlPolicyError:
                    pass
                else:
                    raise ValueError("W3K030 must remain policy-rejected dangerous SQL")
            else:
                try:
                    validated = validate_sql(sql)
                except SqlPolicyError as error:
                    raise ValueError("candidate SQL must pass the production SQL policy") from error
                parameters = arguments.get("parameters", {})
                if not isinstance(parameters, dict) or set(parameters) != set(
                    validated.parameter_names
                ):
                    raise ValueError("candidate SQL parameters must exactly match placeholders")
            output = expanded
        else:
            if sql_path is not None:
                raise ValueError("only action and repair steps may reference candidate SQL")
            contract = {
                "behavior": BehaviorDecision,
                "plan": TypedMetricPlan,
                "synthesis": FinalAnswer,
            }[purpose]
            _strict_contract(contract, output)
        parsed_steps.append(
            FixtureModelStep.model_validate(
                {
                    "model_purpose": step.model_purpose,
                    "ordinal": step.ordinal,
                    "output": output,
                    "sql_ref": sql_path,
                }
            )
        )
    try:
        return FixtureScript(script_id=script_id, steps=tuple(parsed_steps))
    except ValidationError as error:
        raise ValueError("invalid fixture script") from error


def load_fixture_scripts() -> tuple[FixtureScript, ...]:
    """Load all and only the 30 reviewed known scripts, with candidate SQL injected."""
    raw = _read_yaml_list(WEEK3_SCRIPTS_REGISTRY, label="fixture scripts")
    scripts = tuple(_parse_fixture_script(item) for item in raw)
    ids = tuple(script.script_id for script in scripts)
    if ids != _KNOWN_IDS or len(set(ids)) != len(ids):
        raise ValueError("fixture scripts require ordered W3K001 through W3K030 IDs")
    referenced = {
        step.sql_ref for script in scripts for step in script.steps if step.sql_ref is not None
    }
    actual = set(WEEK3_SCRIPTED_SQL_ROOT.rglob("*"))
    if any(path.is_symlink() or not path.is_file() for path in actual) or referenced != actual:
        raise ValueError("candidate SQL inventory has missing, symlink, or orphan files")
    return scripts


def _parse_case(raw: dict[str, Any], *, cohort: Week3Cohort) -> Week3EvaluationCase:
    data = dict(raw)
    heldout_expected_metadata = {
        "expected_ref",
        "expected_behavior",
        "expected_final_status",
        "expected_stop_reason",
        "expected_missing_fields",
        "expected_repair_count",
    }
    if cohort == "heldout" and any(
        key in {"expected_observations", "budget_overrides"}
        or (_truth_key(key) and key not in heldout_expected_metadata)
        for key in data
    ):
        raise ValueError("heldout cases cannot declare Oracle, expected paths, or budgets")
    observations = data.get("expected_observations", [])
    if not isinstance(observations, list):
        raise ValueError("expected_observations must be a list")
    parsed_observations = []
    for observation in observations:
        if type(observation) is not dict:
            raise ValueError("expected observations must be mappings")
        item = dict(observation)
        item["expected_result_path"] = _repository_file(
            item.get("expected_result_path"), directory=WEEK3_EXPECTED_ROOT, suffix=".json"
        )
        parsed_observations.append(item)
    data["expected_observations"] = parsed_observations
    try:
        return Week3EvaluationCase.model_validate_json(
            json.dumps(data, ensure_ascii=False, default=str), strict=True
        )
    except ValidationError as error:
        raise ValueError("invalid Week 3 evaluation case") from error


def _validate_expected_observations(case: Week3EvaluationCase) -> None:
    purposes = tuple(item.purpose for item in case.expected_observations)
    if len(purposes) != len(set(purposes)):
        raise ValueError("expected observation purposes must be unique")
    for observation in case.expected_observations:
        frozen = _read_expected(observation.expected_result_path)
        declared = set(observation.key_columns) | set(observation.numeric_columns)
        if declared != set(frozen.result.columns):
            raise ValueError("expected result columns do not match the observation contract")


def _validate_case_distribution(cases: tuple[Week3EvaluationCase, ...]) -> None:
    ids = tuple(case.case_id for case in cases)
    if ids != (*_KNOWN_IDS, *_HELDOUT_IDS) or len(set(ids)) != len(ids):
        raise ValueError("Week 3 case IDs, order, or count are invalid")
    questions = tuple(" ".join(case.question.split()).casefold() for case in cases)
    if len(set(questions)) != len(questions):
        raise ValueError("Week 3 questions must be unique")
    known = cases[:30]
    behavior = tuple(case for case in known if case.suite == "behavior")
    if len(behavior) != 10 or Counter(case.expected_behavior for case in behavior) != {
        "clarify": 4,
        "execute": 2,
        "refuse": 2,
        "unsupported": 2,
    }:
        raise ValueError("known behavior distribution is invalid")
    simple = tuple(case for case in known if case.suite == "simple")
    if tuple(case.metric_id for case in simple) != _METRIC_ORDER:
        raise ValueError("simple metric order must be W3K011 through W3K025")
    fixture_only = tuple(case.case_id for case in known if case.modes == ("fixture",))
    if fixture_only != ("W3K027", "W3K028", "W3K029", "W3K030"):
        raise ValueError("only the four deterministic fault cases may be fixture-only")
    budget = cases[28]
    if (
        budget.budget_overrides is None
        or budget.budget_overrides.max_tool_calls != 3
        or budget.budget_overrides.max_execute_calls is not None
    ):
        raise ValueError("W3K029 requires its fixed downward-only tool budget")


def _validate_case_script_compatibility(
    cases: tuple[Week3EvaluationCase, ...], scripts: tuple[FixtureScript, ...]
) -> None:
    by_case = {case.case_id: case for case in cases}
    by_script = {script.script_id: script for script in scripts}
    for case in cases:
        try:
            script = by_script[case.script_ref]
        except KeyError:
            raise ValueError("case references an unknown fixture script") from None
        behavior = BehaviorDecision.model_validate(dict(script.steps[0].output))
        if behavior.action.value != case.expected_behavior:
            raise ValueError("case and script behavior are incompatible")
        if behavior.missing_fields != case.expected_missing_fields:
            raise ValueError("case and script missing-field contracts are incompatible")
        purposes = tuple(step.model_purpose for step in script.steps)
        expected_purposes: tuple[str, ...]
        if case.expected_behavior != "execute":
            expected_purposes = ("behavior",)
        elif script.script_id in {"W3K006", "W3K026"}:
            expected_purposes = (
                "behavior",
                "plan",
                "action",
                "action",
                "action",
                "action",
                "synthesis",
            )
        elif script.script_id == "W3K027":
            expected_purposes = ("behavior", "plan", "action", "repair", "synthesis")
        elif script.script_id == "W3K028":
            expected_purposes = ("behavior", "plan", "action", "repair")
        elif script.script_id == "W3K029":
            expected_purposes = ("behavior", "plan", "action", "action")
        elif script.script_id == "W3K030":
            expected_purposes = ("behavior", "plan", "action")
        else:
            expected_purposes = ("behavior", "plan", "action", "synthesis")
        if purposes != expected_purposes:
            raise ValueError("fixture script purpose sequence is incomplete or out of order")
        plans = tuple(step for step in script.steps if step.model_purpose == "plan")
        if case.expected_behavior == "execute":
            if len(plans) != 1:
                raise ValueError("execute scripts require exactly one typed plan")
            plan = TypedMetricPlan.model_validate(dict(plans[0].output))
            expected_analysis = (
                "attribution" if script.script_id in {"W3K006", "W3K026", "W3K029"} else "simple"
            )
            if plan.metric_id != case.metric_id or plan.analysis_type.value != expected_analysis:
                raise ValueError("case and script typed plans are incompatible")
        if case.cohort == "heldout":
            owner = by_case.get(case.expected_ref or "")
            if owner is None or owner.cohort != "known" or owner.script_ref != case.script_ref:
                raise ValueError("heldout expected_ref must directly identify its known owner")
            compatible = (
                case.suite,
                case.expected_behavior,
                case.metric_id,
                case.expected_final_status,
                case.expected_stop_reason,
                case.expected_missing_fields,
                case.required_dimensions,
                case.required_tools,
                case.forbidden_tools,
            )
            owner_contract = (
                owner.suite,
                owner.expected_behavior,
                owner.metric_id,
                owner.expected_final_status,
                owner.expected_stop_reason,
                owner.expected_missing_fields,
                owner.required_dimensions,
                owner.required_tools,
                owner.forbidden_tools,
            )
            if compatible != owner_contract:
                raise ValueError("heldout and known owner contracts are incompatible")


def _validate_sql_inventories() -> None:
    oracle = set(WEEK3_ORACLE_ROOT.rglob("*"))
    expected = set(WEEK3_EXPECTED_ROOT.rglob("*"))
    wanted_oracle = {WEEK3_ORACLE_ROOT / f"{stem}.sql" for stem in _EXPECTED_STEMS}
    wanted_expected = {WEEK3_EXPECTED_ROOT / f"{stem}.json" for stem in _EXPECTED_STEMS}
    if oracle != wanted_oracle or expected != wanted_expected:
        raise ValueError("Week 3 Oracle and expected inventories must contain exactly 19 files")
    for path in (*oracle, *expected):
        _regular_file(path)
    for path in oracle:
        try:
            validated = validate_sql(path.read_text(encoding="utf-8"))
        except (OSError, SqlPolicyError) as error:
            raise ValueError("Week 3 Oracle SQL is not governed read-only SQL") from error
        frozen = _read_expected(WEEK3_EXPECTED_ROOT / f"{path.stem}.json")
        if frozen.oracle_query_id != validated.query_id:
            raise ValueError("frozen Oracle query identity does not match its SQL")


def load_week3_cases() -> tuple[Week3EvaluationCase, ...]:
    """Load and cross-check the complete immutable 30-known/10-heldout protocol."""
    known_raw = _read_yaml_list(WEEK3_KNOWN_REGISTRY, label="known cases")
    heldout_raw = _read_yaml_list(WEEK3_HELDOUT_REGISTRY, label="heldout cases")
    known = tuple(_parse_case(item, cohort="known") for item in known_raw)
    raw_heldout = tuple(_parse_case(item, cohort="heldout") for item in heldout_raw)
    by_known = {case.case_id: case for case in known}
    heldout = tuple(
        case.model_copy(
            update={
                "expected_observations": by_known[case.expected_ref or ""].expected_observations
            }
        )
        if case.expected_ref in by_known
        else case
        for case in raw_heldout
    )
    cases = (*known, *heldout)
    _validate_case_distribution(cases)
    _validate_sql_inventories()
    for case in known:
        _validate_expected_observations(case)
    scripts = load_fixture_scripts()
    _validate_case_script_compatibility(cases, scripts)
    return cases


def _hash_files(files: set[Path]) -> str:
    digest = sha256()
    try:
        for path in sorted(files, key=lambda item: item.relative_to(_REPOSITORY_ROOT).as_posix()):
            _regular_file(path)
            relative = path.relative_to(_REPOSITORY_ROOT).as_posix().encode("utf-8")
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except (OSError, ValueError) as error:
        raise ValueError("Week 3 manifest input is unavailable") from error
    return digest.hexdigest()


def _script_dependencies(script_ids: set[str]) -> set[Path]:
    return {
        step.sql_ref
        for script in load_fixture_scripts()
        if script.script_id in script_ids
        for step in script.steps
        if step.sql_ref is not None
    }


def week3_manifest_sha256() -> str:
    """Hash the exact reviewed Week 3 whitelist with length-framed relative paths."""
    cases = load_week3_cases()
    scripts = load_fixture_scripts()
    files = {WEEK3_KNOWN_REGISTRY, WEEK3_HELDOUT_REGISTRY, WEEK3_SCRIPTS_REGISTRY}
    files.update(
        step.sql_ref for script in scripts for step in script.steps if step.sql_ref is not None
    )
    files.update(WEEK3_ORACLE_ROOT / f"{stem}.sql" for stem in _EXPECTED_STEMS)
    files.update(WEEK3_EXPECTED_ROOT / f"{stem}.json" for stem in _EXPECTED_STEMS)
    del cases
    return _hash_files(files)


def week3_cohort_sha256(cohort: Week3Cohort) -> str:
    """Hash a cohort and all direct/transitive known-script and truth dependencies."""
    if cohort not in {"known", "heldout"}:
        raise ValueError("cohort must be known or heldout")
    cases = load_week3_cases()
    selected = tuple(case for case in cases if case.cohort == cohort)
    owners = {
        case.case_id if case.cohort == "known" else cast(str, case.expected_ref)
        for case in selected
    }
    files = {
        WEEK3_KNOWN_REGISTRY,
        WEEK3_SCRIPTS_REGISTRY,
        WEEK3_KNOWN_REGISTRY if cohort == "known" else WEEK3_HELDOUT_REGISTRY,
    }
    files.update(_script_dependencies({case.script_ref for case in selected}))
    for owner_id in owners:
        owner = next(case for case in cases if case.case_id == owner_id)
        for observation in owner.expected_observations:
            files.add(observation.expected_result_path)
            files.add(WEEK3_ORACLE_ROOT / f"{observation.expected_result_path.stem}.sql")
    return _hash_files(files)


__all__ = [
    "WEEK3_EXPECTED_ROOT",
    "WEEK3_HELDOUT_REGISTRY",
    "WEEK3_KNOWN_REGISTRY",
    "WEEK3_ORACLE_ROOT",
    "WEEK3_ROOT",
    "WEEK3_SCRIPTED_SQL_ROOT",
    "WEEK3_SCRIPTS_REGISTRY",
    "load_fixture_scripts",
    "load_week3_cases",
    "week3_cohort_sha256",
    "week3_manifest_sha256",
]
