"""Unit tests: rule loading, validation, versioning, hashing."""

from __future__ import annotations

import json

import pytest
import yaml

from autograder.app.rules import (
    RulesError,
    bump_version,
    hash_rules,
    load_rules,
    validate_rules,
)

from .conftest import base_rules_dict


def test_valid_rules_validate():
    rules = validate_rules(base_rules_dict())
    assert rules.problem_id == "p1"
    assert abs(sum(m.weight for m in rules.metrics) - 1.0) < 1e-12


def test_missing_required_field_rejected():
    data = base_rules_dict()
    del data["problem_id"]
    with pytest.raises(RulesError, match="problem_id"):
        validate_rules(data)


def test_unknown_analyzer_rejected():
    data = base_rules_dict()
    data["metrics"][0]["analyzer"] = "mind_reader"
    with pytest.raises(RulesError, match="unknown analyzer"):
        validate_rules(data)


@pytest.mark.parametrize("field", ["time_limit_s", "memory_limit_mb"])
@pytest.mark.parametrize("bad", [-1.0, 0, "fast"])
def test_bad_limits_rejected(field, bad):
    data = base_rules_dict()
    data["limits"][field] = bad
    with pytest.raises(RulesError):
        validate_rules(data)


def test_negative_process_limit_rejected():
    data = base_rules_dict()
    data["limits"]["process_limit"] = -4
    with pytest.raises(RulesError):
        validate_rules(data)


def test_bad_threshold_type_rejected():
    data = base_rules_dict()
    data["metrics"][0]["thresholds"] = {"min": "high"}
    with pytest.raises(RulesError):
        validate_rules(data)


def test_negative_weight_rejected():
    data = base_rules_dict()
    data["metrics"][0]["weight"] = -0.2
    data["metrics"][1]["weight"] = 1.2
    with pytest.raises(RulesError):
        validate_rules(data)


def test_weights_not_summing_to_one_rejected():
    data = base_rules_dict()
    data["metrics"][0]["weight"] = 0.5
    data["metrics"][1]["weight"] = 0.4  # sums to 0.9
    with pytest.raises(RulesError, match="sum to"):
        validate_rules(data)


def test_normalize_weights_opt_in():
    data = base_rules_dict()
    data["metrics"][0]["weight"] = 3.0
    data["metrics"][1]["weight"] = 1.0
    data["normalize_weights"] = True
    rules = validate_rules(data)
    assert rules.weights_normalized is True
    assert abs(sum(m.weight for m in rules.metrics) - 1.0) < 1e-12
    assert abs(rules.metrics[0].weight - 0.75) < 1e-12


def test_duplicate_metric_names_rejected():
    data = base_rules_dict()
    data["metrics"].append(
        {"name": "correctness", "analyzer": "compiler", "weight": 0.0}
    )
    # weights now sum to 1.0 still (0.6+0.4+0.0); the dupe check must fire
    with pytest.raises(RulesError, match="duplicate metric"):
        validate_rules(data)


def test_max_rounds_bound_rejected():
    data = base_rules_dict()
    data["test_generation"] = {"max_rounds": 4, "max_candidates": 10,
                               "token_budget": 100, "exec_budget": 10}
    with pytest.raises(RulesError):
        validate_rules(data)


def test_bad_literal_rejected():
    data = base_rules_dict()
    data["comparison_policy"] = {"whitespace": "whatever", "case_sensitive": True}
    with pytest.raises(RulesError):
        validate_rules(data)


def test_extra_field_rejected():
    data = base_rules_dict()
    data["nonsense"] = 1
    with pytest.raises(RulesError):
        validate_rules(data)


def test_comment_keys_ignored():
    data = base_rules_dict()
    data["_comment"] = "instructor note"
    rules = validate_rules(data)
    assert rules.problem_id == "p1"


def test_non_mapping_rejected():
    with pytest.raises(RulesError):
        validate_rules([1, 2, 3])


# -- loader ---------------------------------------------------------------
def test_load_json_file(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(base_rules_dict()))
    assert load_rules(p).problem_id == "p1"


def test_load_yaml_file(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump(base_rules_dict()))
    assert load_rules(p).problem_id == "p1"


def test_load_missing_file(tmp_path):
    with pytest.raises(RulesError, match="cannot read"):
        load_rules(tmp_path / "nope.json")


def test_load_bad_json(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text("{not json")
    with pytest.raises(RulesError, match="not valid JSON"):
        load_rules(p)


def test_load_unsupported_extension(tmp_path):
    p = tmp_path / "rules.toml"
    p.write_text("x = 1")
    with pytest.raises(RulesError, match="unsupported"):
        load_rules(p)


# -- versioning / hashing --------------------------------------------------
def test_hash_stable_and_sensitive():
    a = validate_rules(base_rules_dict())
    b = validate_rules(base_rules_dict())
    assert hash_rules(a) == hash_rules(b)
    assert hash_rules(a).startswith("sha256:")
    c = validate_rules(base_rules_dict(version="2.0.0"))
    assert hash_rules(c) != hash_rules(a)


def test_bump_version():
    assert bump_version("1.0.0") == "1.0.1"
    assert bump_version("2.3.9") == "2.3.10"
    assert bump_version("draft") == "draft-r2"
    assert bump_version("v3-r2") == "v3-r3"
