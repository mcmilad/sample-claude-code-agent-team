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


tool_input_of = team_hook_common.tool_input_of


def test_tool_input_of_decodes_a_stringified_object():
    """as_dict() would reduce this to {}, which names no project -- and every
    gating hook reads its scope out of tool_input, so an empty one reads as
    'somebody else's project' and the guardrail silently switches off."""
    assert tool_input_of({"tool_input": {"projectKey": "AGENT"}}) == (
        {"projectKey": "AGENT"}, None)
    assert tool_input_of({"tool_input": '{"projectKey": "AGENT"}'}) == (
        {"projectKey": "AGENT"}, None)


def test_tool_input_of_reports_a_genuinely_unreadable_input():
    """Unreadable must be distinguishable from empty. Reporting it as {} is what
    let both hooks record 'belongs to another project' for an input whose
    project they never managed to read."""
    for bad in ("AGENT-14 to In Review", "[1, 2]", 7, ["a"]):
        value, unreadable = tool_input_of({"tool_input": bad})
        assert value == {}
        assert unreadable, "a %s tool_input is unreadable, not empty" % type(bad).__name__
        assert "tool_input" in unreadable and "JSON object" in unreadable


def test_tool_input_of_treats_an_absent_input_as_empty_not_unreadable():
    assert tool_input_of({}) == ({}, None)
    assert tool_input_of({"tool_input": None}) == ({}, None)
    assert tool_input_of("not a payload") == ({}, None)
