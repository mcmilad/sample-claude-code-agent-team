"""Phase two of the sentinel consume, pinned directly.

This file did not exist until the third review round. The finalizer was only ever
exercised indirectly, through the gate's tests, and three separate defects lived
in it undetected -- each one able to authorize a second gated transition on a
single attestation, which is the one property the sentinel exists to guarantee.

The recurring shape is worth naming: every defect here came from the two phases
of ONE consume disagreeing. PreToolUse and PostToolUse receive the identical
model-authored payload, so any rule the gate applies and the finalizer does not
(or vice versa) desynchronises them, and the .inflight created by one is
adjudicated by the other under different assumptions.
"""
import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOKS = os.path.join(REPO, ".claude", "hooks")
GATE = os.path.join(HOOKS, "jira_transition_verify_gate.py")
FINALIZE = os.path.join(HOOKS, "jira_transition_sentinel_finalize.py")
TRANSITION = "mcp__plugin_atlassian_atlassian__transitionJiraIssue"

CONFIG = {
    "projectKey": "AGENT",
    "transitions": {"11": "To Do", "21": "In Progress", "31": "In Review", "41": "Done"},
    "gatedStatuses": ["In Review", "Done"],
}


def finalize_module():
    spec = importlib.util.spec_from_file_location("finalize_under_test", FINALIZE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["finalize_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def setup_env(tmp_path):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps(CONFIG))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return dict(os.environ, HOME=str(home), JIRA_CONFIG_PATH=str(cfg)), home


def sentinel(home, issue="AGENT-14"):
    d = os.path.join(str(home), ".claude", "logs", "verified", "AGENT")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, issue + ".verified")
    with open(path, "w") as fh:
        fh.write("pytest -q PASSED\n")
    return path


def drive(hook, env, payload):
    return subprocess.run([sys.executable, hook], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def gate_payload(issue="AGENT-14", tid="31", as_string=False):
    ti = {"issueIdOrKey": issue, "transition": {"id": tid}}
    return {"tool_name": TRANSITION, "tool_input": json.dumps(ti) if as_string else ti}


def final_payload(issue="AGENT-14", tid="31", response=None, as_string=False):
    p = gate_payload(issue, tid, as_string)
    p["tool_response"] = {"success": True} if response is None else response
    return p


def inflight_of(path):
    return path[: -len(".verified")] + ".inflight"


# --- the combination defect -------------------------------------------------

def test_both_phases_read_a_stringified_tool_input_the_same_way(tmp_path):
    """The gate decodes a JSON-string tool_input; the finalizer must too.

    When only the gate was converted, it created an .inflight the finalizer then
    failed to recognise as its own -- it bailed at its project-scope check
    logging "belongs to another project", leaving an orphaned .inflight that
    authorized a SECOND gated transition once the stale window elapsed. Neither
    patch was wrong alone; the defect existed only in the combination.
    """
    env, home = setup_env(tmp_path)
    path = sentinel(home)

    assert drive(GATE, env, gate_payload(as_string=True)).returncode == 0
    assert os.path.exists(inflight_of(path)), "the gate consumes a stringified payload"

    assert drive(FINALIZE, env, final_payload(as_string=True)).returncode == 0
    assert not os.path.exists(inflight_of(path)), \
        "the finalizer must recognise the same payload the gate acted on"
    assert not os.path.exists(path), "a successful transition spends the sentinel"
    assert drive(GATE, env, gate_payload(as_string=True)).returncode == 2, \
        "one attestation must not authorize a second gated transition"


def test_a_stringified_failure_restores_rather_than_orphans(tmp_path):
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert drive(GATE, env, gate_payload(as_string=True)).returncode == 0
    assert drive(FINALIZE, env, final_payload(
        as_string=True, response='{"errorMessages": ["boom"]}')).returncode == 0
    assert os.path.exists(path), "a failed transition must leave the sentinel usable"
    assert drive(GATE, env, gate_payload(as_string=True)).returncode == 0


# --- gated scope ------------------------------------------------------------

def test_an_ungated_transition_never_touches_a_sentinel(tmp_path):
    """In Progress is the documented claim step, run on every issue. It consumes
    no sentinel, so it must finalize none."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert drive(GATE, env, gate_payload(tid="31")).returncode == 0
    inflight = inflight_of(path)
    assert os.path.exists(inflight)

    assert drive(FINALIZE, env, final_payload(tid="21")).returncode == 0
    assert os.path.exists(inflight), \
        "an ungated finalize must leave another transition's .inflight alone"

    assert drive(FINALIZE, env, final_payload(tid="21", response="Error: 401")).returncode == 0
    assert os.path.exists(inflight) and not os.path.exists(path), \
        "an ungated FAILURE must not restore a sentinel it did not consume"


def test_an_unknown_transition_id_finalizes_nothing(tmp_path):
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert drive(GATE, env, gate_payload(tid="31")).returncode == 0
    assert drive(FINALIZE, env, final_payload(tid="99")).returncode == 0
    assert os.path.exists(inflight_of(path))


# --- the classifier ---------------------------------------------------------

def test_an_issue_number_is_not_an_http_status():
    """`AGENT-401 transitioned` is a SUCCESS message. The bare 4xx/5xx pattern
    matched the issue NUMBER, so every issue numbered 400-599 read as an HTTP
    error -- roughly one in five, scaling with the project."""
    m = finalize_module()
    assert m.transition_succeeded("Issue AGENT-401 transitioned to In Review") is True
    assert m.transition_succeeded("AGENT-512 moved to Done") is True
    assert m.transition_succeeded("PROJ-404 transitioned") is True


def test_a_real_http_status_is_still_a_failure():
    m = finalize_module()
    for text in ("Error: 401 Unauthorized", "Request failed with 403",
                 "HTTP 500", "status 503", "500 Internal Server Error"):
        assert m.transition_succeeded(text) is False, text


def test_empty_error_containers_are_not_errors():
    """Jira and MCP wrappers return `{"errorMessages": [], "errors": {}}` on
    success. Reading key PRESENCE restored a legitimately spent sentinel."""
    m = finalize_module()
    assert m._structured_verdict({"errorMessages": [], "errors": {}}) is True
    assert m._structured_verdict({"errorMessages": ["real"]}) is False
    assert m._structured_verdict({"errors": {"field": "bad"}}) is False


def test_the_finalizer_fails_open_on_hostile_input(tmp_path):
    """House rule: exit 0 or 2, never crash."""
    env, _ = setup_env(tmp_path)
    for payload in ("not json", "", "[]", "null",
                    json.dumps({"tool_name": TRANSITION, "tool_input": "{"}),
                    json.dumps({"tool_name": TRANSITION, "tool_input": 42}),
                    json.dumps({"tool_name": TRANSITION,
                                "tool_input": {"issueIdOrKey": "AGENT-../../etc/passwd",
                                               "transition": {"id": "31"}}})):
        proc = subprocess.run([sys.executable, FINALIZE], input=payload,
                              capture_output=True, text=True, env=env)
        assert proc.returncode in (0, 2), (payload, proc.returncode, proc.stderr)


def test_bare_prose_http_errors_are_failures_without_needing_a_cue_word():
    """Regression guard. An attempt to stop the 4xx/5xx arm matching issue
    NUMBERS narrowed it to require an explicit HTTP cue -- and let six realistic
    failures read as SUCCESS, deleting the sentinel for transitions that never
    landed. Bare prose is the live tool_response shape, so this is the primary
    path. The narrowing was redundant too: stripping issue keys before the scan
    already solves the issue-number problem."""
    m = finalize_module()
    for text in ("Received 403 from Jira",
                 "The request returned 400.",
                 "Transition rejected (409)",
                 "Jira responded 502",
                 "429 - slow down",
                 "Issue AGENT-14 could not be transitioned: 400"):
        assert m.transition_succeeded(text) is False, text


def test_issue_key_stripping_is_what_protects_the_issue_number():
    """Pins the stripping itself, not a side effect of some other guard.

    Previously this property passed off the narrowed regex, so deleting
    _ISSUE_KEY.sub left the whole suite green and the sub read as dead code --
    a future cleanup would have silently removed CODE-404/STATUS-500 protection.
    With the broad arm restored the sub is the only thing standing between an
    issue number and a false failure."""
    m = finalize_module()
    # Every one of these is a SUCCESS message whose only 4xx/5xx digits live
    # inside an issue key. Without the strip, the broad arm matches all of them.
    for text in ("Issue AGENT-401 transitioned to In Review",
                 "AGENT-512 moved to Done",
                 "CODE-404 transitioned",
                 "STATUS-500 transitioned",
                 "HTTP-503 transitioned"):
        assert m.transition_succeeded(text) is True, text


def test_mcp_is_error_flag_is_a_failure():
    """MCP's own failure flag. Omitting it from _ERROR_KEYS meant an errored
    call classified as SUCCESS and SPENT the sentinel."""
    m = finalize_module()
    assert m._structured_verdict({"isError": True, "content": []}) is False
    assert m._structured_verdict({"isError": False, "ok": 1}) is True
    assert m._structured_verdict({"ok": 1}) is True


def test_an_error_in_any_content_block_is_a_failure():
    """First-block-wins let a response whose leading block is clean and whose
    second reports the failure classify as SUCCESS -- against this module's
    documented restore bias."""
    m = finalize_module()
    assert m.transition_succeeded(
        [{"text": '{"ok": 1}'}, {"text": '{"errorMessages": ["boom"]}'}]) is False
    assert m.transition_succeeded(
        [{"text": '{"ok": 1}'}, {"text": '{"also": "fine"}'}]) is True
