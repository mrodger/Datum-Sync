import json

import pytest

from datum_sync.manifest import (
    Manifest,
    ManifestError,
    ParameterError,
    load_manifest,
    validate_params,
)

MINIMAL = {"name": "ws", "parameters": [], "outputs": []}


def m(**overrides) -> Manifest:
    return Manifest.model_validate({**MINIMAL, **overrides})


# --- manifest structure ----------------------------------------------------

def test_minimal_manifest_is_valid():
    assert m().name == "ws"


def test_unknown_top_level_key_rejected():
    with pytest.raises(Exception):
        Manifest.model_validate({**MINIMAL, "widgets": 3})


def test_lookup_choice_requires_choices():
    with pytest.raises(Exception, match="requires 'choices'"):
        m(parameters=[{"name": "F", "type": "LOOKUP_CHOICE"}])


def test_lookup_choice_default_must_be_a_choice():
    with pytest.raises(Exception, match="not one of"):
        m(parameters=[
            {"name": "F", "type": "LOOKUP_CHOICE", "choices": ["A"], "default": "B"}
        ])


def test_choices_rejected_on_non_lookup():
    with pytest.raises(Exception, match="only valid for LOOKUP_CHOICE"):
        m(parameters=[{"name": "F", "type": "STRING", "choices": ["A"]}])


def test_required_with_default_rejected():
    with pytest.raises(Exception, match="cannot also declare a default"):
        m(parameters=[{"name": "F", "type": "STRING", "required": True, "default": "x"}])


def test_duplicate_parameter_names_rejected():
    with pytest.raises(Exception, match="duplicate parameter"):
        m(parameters=[
            {"name": "F", "type": "STRING"},
            {"name": "F", "type": "INTEGER"},
        ])


def test_unknown_service_rejected():
    with pytest.raises(Exception, match="unknown service"):
        m(services=["job_submitter", "teleporter"])


def test_non_positive_timeout_rejected():
    with pytest.raises(Exception, match="timeout_seconds must be positive"):
        m(timeout_seconds=0)


def test_multiple_outputs_need_exactly_one_primary():
    with pytest.raises(Exception, match="exactly one must be marked primary"):
        m(outputs=[
            {"name": "a", "type": "text/html"},
            {"name": "b", "type": "application/json"},
        ])


def test_two_primaries_rejected():
    with pytest.raises(Exception, match="only one output may be primary"):
        m(outputs=[
            {"name": "a", "type": "text/html", "primary": True},
            {"name": "b", "type": "application/json", "primary": True},
        ])


def test_single_output_is_implicitly_primary():
    assert m(outputs=[{"name": "a", "type": "text/html"}]).primary_output.name == "a"


# --- load_manifest ---------------------------------------------------------

def test_name_must_match_directory(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(MINIMAL))
    with pytest.raises(ManifestError, match="does not match directory name"):
        load_manifest(p, expected_name="other")


def test_missing_file_reports_cleanly(tmp_path):
    with pytest.raises(ManifestError, match="no manifest.json"):
        load_manifest(tmp_path / "manifest.json")


def test_invalid_json_reports_cleanly(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text("{not json")
    with pytest.raises(ManifestError, match="invalid JSON"):
        load_manifest(p)


# --- parameter validation --------------------------------------------------

TYPED = m(parameters=[
    {"name": "S", "type": "STRING", "required": True},
    {"name": "I", "type": "INTEGER", "default": 7},
    {"name": "F", "type": "FLOAT", "default": 1.5},
    {"name": "B", "type": "BOOLEAN", "default": False},
    {"name": "C", "type": "LOOKUP_CHOICE", "choices": ["A", "B"], "default": "A"},
])


def test_defaults_applied_when_absent():
    out = validate_params(TYPED, {"S": "x"})
    assert out == {"S": "x", "I": 7, "F": 1.5, "B": False, "C": "A"}


def test_unknown_parameter_is_rejected_not_ignored():
    with pytest.raises(ParameterError, match="unknown parameter"):
        validate_params(TYPED, {"S": "x", "JOB_iD": "70023"})


def test_missing_required_rejected():
    with pytest.raises(ParameterError, match="missing required parameter"):
        validate_params(TYPED, {})


@pytest.mark.parametrize("raw,expected", [("true", True), ("0", False), ("YES", True)])
def test_boolean_coerced_from_string(raw, expected):
    assert validate_params(TYPED, {"S": "x", "B": raw})["B"] is expected


def test_boolean_rejects_nonsense():
    with pytest.raises(ParameterError, match="not a boolean"):
        validate_params(TYPED, {"S": "x", "B": "maybe"})


def test_integer_coerced_from_string():
    assert validate_params(TYPED, {"S": "x", "I": "42"})["I"] == 42


def test_integer_rejects_boolean():
    with pytest.raises(ParameterError, match="got a boolean"):
        validate_params(TYPED, {"S": "x", "I": True})


def test_integer_rejects_float_string():
    with pytest.raises(ParameterError, match="not an integer"):
        validate_params(TYPED, {"S": "x", "I": "1.5"})


def test_lookup_choice_rejects_value_outside_choices():
    with pytest.raises(ParameterError, match="is not one of"):
        validate_params(TYPED, {"S": "x", "C": "Z"})


def test_explicit_none_falls_back_to_default():
    assert validate_params(TYPED, {"S": "x", "I": None})["I"] == 7
