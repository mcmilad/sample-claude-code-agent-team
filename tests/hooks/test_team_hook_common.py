"""Shared hook helpers. These are small, but every hook depends on them, so a
wrong answer here misroutes or silently disables a guardrail.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))

import team_hook_common  # noqa: E402

role_of_teammate = team_hook_common.role_of_teammate


def test_exact_role_name_maps_to_its_role():
    for role in ("coding", "devops", "sa", "review"):
        assert role_of_teammate(role) == role


def test_instance_suffixed_names_map_to_their_role():
    assert role_of_teammate("coding-2") == "coding"
    assert role_of_teammate("coding-agent") == "coding"
    assert role_of_teammate("review-1") == "review"
    assert role_of_teammate("sa-agent") == "sa"
    assert role_of_teammate("DevOps-3") == "devops"


def test_a_bare_prefix_match_is_not_a_role():
    """'sample-1'.startswith('sa') is true, which used to hand a teammate named
    'sample-1' the sa pool's work. Only an exact name or a `<role>-` instance
    suffix is a real role."""
    assert role_of_teammate("sample-1") is None
    assert role_of_teammate("sandbox") is None
    assert role_of_teammate("reviewer") is None
    assert role_of_teammate("codingagent") is None


def test_unknown_and_empty_names_map_to_nothing():
    assert role_of_teammate("mystery-bot") is None
    assert role_of_teammate("") is None
    assert role_of_teammate(None) is None


def test_as_dict_coalesces_truthy_non_dicts():
    """`value or {}` only guards falsy values; a truthy non-dict -- a JSON
    string, exactly what an LLM emits for additional_fields when it stringifies
    a nested object -- passes through and raises AttributeError on .get()."""
    assert team_hook_common.as_dict({"a": 1}) == {"a": 1}
    assert team_hook_common.as_dict('{"labels": ["role-coding"]}') == {}
    assert team_hook_common.as_dict(["labels"]) == {}
    assert team_hook_common.as_dict(None) == {}
    assert team_hook_common.as_dict(0) == {}
