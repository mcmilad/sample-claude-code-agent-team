"""A transition into a gated status requires a consumed sentinel."""
import json
import os
import subprocess
import time
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "jira_transition_verify_gate.py")
TRANSITION = "mcp__plugin_atlassian_atlassian__transitionJiraIssue"

CONFIG = {
    "projectKey": "AGENT",
    "transitions": {"11": "To Do", "21": "In Progress", "31": "In Review", "41": "Done"},
    "gatedStatuses": ["In Review", "Done"],
}


def setup_env(tmp_path, config=None):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps(config or CONFIG))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return dict(os.environ, HOME=str(home), JIRA_CONFIG_PATH=str(cfg)), home


def run_hook(env, issue="AGENT-14", transition_id="31", tool_name=TRANSITION):
    payload = {
        "tool_name": tool_name,
        "tool_input": {"issueIdOrKey": issue, "transition": {"id": transition_id}},
    }
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def sentinel(home, issue="AGENT-14", project="AGENT"):
    d = os.path.join(str(home), ".claude", "logs", "verified", project)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, issue + ".verified")
    with open(path, "w") as fh:
        fh.write("pytest tests/auth -q PASSED\n")
    return path


def journal(home, project, events):
    d = os.path.join(str(home), ".claude", "logs", "jira-mirror")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, project + ".jsonl"), "a") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def test_blocks_in_review_without_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    proc = run_hook(env, transition_id="31")
    assert proc.returncode == 2
    assert "sentinel" in proc.stderr.lower()
    assert "AGENT-14.verified" in proc.stderr


def test_blocks_done_without_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, transition_id="41").returncode == 2


def test_block_message_covers_both_the_implementer_and_reviewer_case(tmp_path):
    """Transitions are any->any (no workflow ordering in a team-managed project),
    so an implementer can go To Do -> Done directly and a reviewer can hit this
    same block while closing after a PASS verdict. The message must not assume
    either actor -- it must not tell a reviewer transitioning straight to Done
    to go run the issue's `Run:` command, which they never ran. Covers both
    routes to the same gated transition (41 = Done) since the message text does
    not depend on which transition id triggered it."""
    env, home = setup_env(tmp_path)
    proc = run_hook(env, transition_id="41")  # e.g. To Do -> Done in one call
    assert proc.returncode == 2
    lower = proc.stderr.lower()
    assert "run:` command" in lower or "run:" in lower  # implementer case
    assert "reviewer" in lower and "verdict" in lower    # reviewer case
    assert "mkdir -p" in proc.stderr
    assert "skip-verify" in proc.stderr


def test_allows_in_review_with_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0


FINALIZE = os.path.join(REPO, ".claude", "hooks", "jira_transition_sentinel_finalize.py")


def run_finalize(env, issue="AGENT-14", response="Issue transitioned successfully",
                 transition_id="31"):
    payload = {
        "tool_name": TRANSITION,
        "tool_input": {"issueIdOrKey": issue, "transition": {"id": transition_id}},
        "tool_response": response,
    }
    return subprocess.run([sys.executable, FINALIZE], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def inflight_of(path):
    return path[: -len(".verified")] + ".inflight"


def test_consumes_the_sentinel_so_it_cannot_be_reused(tmp_path):
    """One sentinel authorizes one SUCCESSFUL transition. The PreToolUse rename
    is the consume -- it takes effect before the MCP call, so a second
    concurrent transition is blocked -- and PostToolUse spends it on success."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert not os.path.exists(path), "the .verified must be consumed immediately"
    assert os.path.exists(inflight_of(path)), "consumed means in-flight, not gone"

    # A second transition while the first is in flight is blocked.
    assert run_hook(env, transition_id="31").returncode == 2

    assert run_finalize(env).returncode == 0
    assert not os.path.exists(inflight_of(path)), "success spends the sentinel"
    assert run_hook(env, transition_id="31").returncode == 2


def test_a_failed_transition_restores_the_sentinel(tmp_path):
    """The regression this whole two-phase design exists for. Observed live on
    AGENT-11: the gate allowed and destroyed the sentinel, the MCP call never
    landed, and the retry was blocked with 'no sentinel' -- which reads to an
    agent as a verification failure, not an API failure."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0

    assert run_finalize(env, response="Error: 401 Unauthorized").returncode == 0
    assert os.path.exists(path), "a failed transition must leave the sentinel usable"
    assert not os.path.exists(inflight_of(path))
    assert run_hook(env, transition_id="31").returncode == 0, "the retry must pass"


def test_an_error_string_is_not_treated_as_success(tmp_path):
    """jira_mirror_journal._succeeded() counts any non-empty string as success,
    and a bare string is the live tool_response shape. Reusing it here would
    make the finalizer a no-op for exactly the case it targets."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("finalize", FINALIZE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["finalize"] = mod
    spec.loader.exec_module(mod)

    assert mod.transition_succeeded("Issue transitioned") is True
    assert mod.transition_succeeded({"status": "ok"}) is True
    assert mod.transition_succeeded("Error: could not transition") is False
    assert mod.transition_succeeded("Request failed with 403") is False
    assert mod.transition_succeeded({"errorMessages": ["nope"]}) is False
    assert mod.transition_succeeded("") is False
    assert mod.transition_succeeded(None) is False


def test_evidence_free_response_restores_rather_than_spends(tmp_path):
    """A response carrying no evidence either way must resolve to restore: a
    wrongly-restored sentinel costs at most one extra authorized transition, a
    wrongly-spent one wedges the issue -- which is the failure actually observed.

    Note the deliberate limit: a STRUCTURED response with no error key still
    counts as success, matching jira_mirror_journal's live-verified convention.
    Demanding a positive success key there would restore genuinely-spent
    sentinels and break single-use, since the response shape is not contractual."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert run_finalize(env, response=[]).returncode == 0
    assert os.path.exists(path), "an evidence-free response must restore the sentinel"


def test_stale_inflight_is_reclaimable_when_the_tool_never_ran(tmp_path):
    """If the call is denied or cancelled, PostToolUse never fires and the
    .inflight would otherwise wedge the issue forever."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    inflight = inflight_of(path)
    assert os.path.exists(inflight)

    assert run_hook(env, transition_id="31").returncode == 2, "fresh in-flight blocks"
    old = time.time() - 3600
    os.utime(inflight, (old, old))
    assert run_hook(env, transition_id="31").returncode == 0, "stale in-flight is reclaimable"


def test_an_ungated_transition_never_spends_an_in_flight_sentinel(tmp_path):
    """The gate never consumes for an ungated transition, so that transition's
    PostToolUse is not the other half of any consume and must leave the
    sentinel alone. It used to delete a lingering .inflight on success --
    In Progress (21) is the documented claim step, run on every issue -- which
    destroyed an attestation that had already passed verification. The gated
    retry then read "no sentinel", which to an agent looks like a verification
    failure rather than a stuck call, and nothing on disk could recover it."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    inflight = inflight_of(path)
    assert os.path.exists(inflight)

    # That In Review call was denied/cancelled, so no PostToolUse fired for it.
    # The same issue is now moved back to In Progress, and that call succeeds.
    assert run_hook(env, transition_id="21").returncode == 0
    assert run_finalize(env, transition_id="21").returncode == 0

    assert os.path.exists(inflight), \
        "an ungated transition must not spend a sentinel it never consumed"
    assert not os.path.exists(path), "nor forge one back into existence"

    # Because the attestation survived, the documented recovery still works.
    old = time.time() - 3600
    os.utime(inflight, (old, old))
    assert run_hook(env, transition_id="31").returncode == 0, \
        "the stale in-flight reclaim must still be able to recover it"


def test_an_ungated_failure_never_resurrects_an_in_flight_sentinel(tmp_path):
    """The half that defeats the review gate itself. A FAILING ungated
    transition used to restore an .inflight belonging to a gated call that was
    still in flight. That gated call then landed, found no .inflight to spend,
    and left a live .verified behind -- so one attestation authorized both
    In Review and Done with no second verification."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0

    assert run_hook(env, transition_id="21").returncode == 0
    assert run_finalize(env, transition_id="21",
                        response="Error: 401 Unauthorized").returncode == 0
    assert not os.path.exists(path), \
        "an ungated failure must not resurrect another transition's sentinel"

    # The gated In Review lands and spends its own sentinel, as it should.
    assert run_finalize(env, transition_id="31").returncode == 0
    assert not os.path.exists(inflight_of(path))
    assert run_hook(env, transition_id="41").returncode == 2, \
        "one attestation must never authorize In Review and then Done"


def test_an_unknown_transition_id_leaves_the_in_flight_sentinel_alone(tmp_path):
    """The gate fails open on an id outside the discovered map, consuming
    nothing for it. Fail-open in the finalizer must mean the same thing:
    touch nothing. Deleting or restoring here would act on a sentinel whose
    real transition is still unresolved."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert run_finalize(env, transition_id="99").returncode == 0
    assert os.path.exists(inflight_of(path)), "an unknown id must finalize nothing"
    assert not os.path.exists(path)

    # The explicit `not target` guard is not redundant with is_gated(): a
    # malformed discovered gate set containing null makes `None in gated` true,
    # so an unresolvable id would read as GATED and the finalizer would
    # adjudicate a sentinel whose transition it never identified.
    other = tmp_path / "malformed"
    other.mkdir()
    env2, home2 = setup_env(other, dict(CONFIG, gatedStatuses=["In Review", None]))
    path2 = sentinel(home2)
    assert run_hook(env2, transition_id="31").returncode == 0
    assert run_finalize(env2, transition_id="99").returncode == 0
    assert os.path.exists(inflight_of(path2)), \
        "unknown must stay unknown even when the gate set itself is malformed"


def test_the_finalizer_takes_its_gated_set_from_config_not_a_local_copy(tmp_path):
    """Drift pin. Both phases resolve gated-ness through the same two helpers
    (target_status / is_gated in jira_transition_verify_gate); a second copy of
    the rule inside the finalizer would desynchronise them the moment a board
    gates different statuses. On a board that gates only In Progress, the
    finalizer must spend on 21 and keep its hands off 31."""
    config = dict(CONFIG, gatedStatuses=["In Progress"])
    env, home = setup_env(tmp_path, config)
    path = sentinel(home)

    assert run_hook(env, transition_id="21").returncode == 0
    inflight = inflight_of(path)
    assert os.path.exists(inflight), "the gate consumes for the CONFIGURED status"

    assert run_finalize(env, transition_id="31").returncode == 0
    assert os.path.exists(inflight), \
        "In Review is not gated on this board -- the finalizer must not touch it"

    assert run_finalize(env, transition_id="21").returncode == 0
    assert not os.path.exists(inflight), "the configured gated status spends it"


def test_gatedness_resolution_is_total_even_on_a_malformed_transition_field():
    """Both hooks now route their decision through these two helpers, so the
    helpers must never raise: a raise resolves the call through the outer
    fail-open handler instead, which exits 0 with an EMPTY audit payload and is
    indistinguishable from a deliberate "not gated". `transition` arriving as
    the bare id string is the documented model failure mode `as_dict` exists
    for -- it is truthy, so `or {}` lets it through to .get()."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("gate_mod", HOOK)
    gate = importlib.util.module_from_spec(spec)
    sys.modules["gate_mod"] = gate
    spec.loader.exec_module(gate)

    cfg = {"transitions": {"31": "In Review"}, "gatedStatuses": ["In Review"]}
    assert gate.target_status(cfg, {"transition": {"id": "31"}}) == "In Review"
    assert gate.target_status(cfg, {"transition": {"id": 31}}) == "In Review", \
        "a numeric id must resolve, not fall through as unknown"
    for junk in ("not-a-dict", {"transition": "31"}, {"transition": []}, {}, None, 7):
        assert gate.target_status(cfg, junk) is None
    assert gate.is_gated(cfg, None) is False


def test_a_malformed_transition_field_is_decided_not_crashed_through(tmp_path):
    """End-to-end companion: both hooks must REACH a decision on junk rather
    than land in the outer handler. Return code alone cannot tell the two
    apart -- both are 0 -- so this reads the audit trail, which is the whole
    value of a fail-open guardrail."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0  # consume -> .inflight

    for script in (HOOK, FINALIZE):
        payload = {
            "tool_name": TRANSITION,
            "tool_input": {"issueIdOrKey": "AGENT-14", "transition": "31"},
            "tool_response": "Issue transitioned successfully",
        }
        proc = subprocess.run([sys.executable, script], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)
        assert proc.returncode == 0
        records = [json.loads(l) for l in open(
            os.path.join(str(home), ".claude", "logs", "team-hooks.jsonl")) if l.strip()]
        assert "hook error" not in (records[-1].get("reason") or ""), \
            "{} crashed into fail-open instead of deciding: {}".format(
                os.path.basename(script), records[-1].get("reason"))

    assert os.path.exists(inflight_of(path)), \
        "an unresolvable transition must finalize nothing"


def test_allows_ungated_transitions_without_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, transition_id="21").returncode == 0, "In Progress is not gated"


def test_skip_verify_label_bypasses_the_gate(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, "AGENT", [{"op": "create", "key": "AGENT-14",
                             "labels": ["role-sa", "skip-verify"], "status": "To Do"}])
    assert run_hook(env, transition_id="31").returncode == 0


def test_ignores_other_projects(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, issue="SCRUM-2", transition_id="41").returncode == 0


def test_empty_gated_statuses_is_honoured_not_treated_as_unconfigured(tmp_path):
    """`cfg.get(...) or DEFAULT` cannot tell "no gated statuses exist on this
    board" from "nobody configured this". Falling back to the defaults there
    gates two status names the board does not have, so nothing is really gated
    while the audit log claims otherwise. An explicit [] means [] -- and
    bootstrap is where an empty gate set must fail loudly (exit 5).
    """
    config = dict(CONFIG, gatedStatuses=[])
    env, home = setup_env(tmp_path, config)
    assert run_hook(env, transition_id="41").returncode == 0, \
        "an explicitly empty gate set gates nothing -- it is not a fallback trigger"


def test_missing_gated_statuses_key_still_falls_back_to_the_defaults(tmp_path):
    config = {"projectKey": "AGENT", "transitions": {"41": "Done"}}
    env, home = setup_env(tmp_path, config)
    assert run_hook(env, transition_id="41").returncode == 2, \
        "genuinely unconfigured must keep gating the default statuses"


def test_non_list_gated_statuses_falls_back_to_the_defaults(tmp_path):
    config = dict(CONFIG, gatedStatuses="Done")
    env, home = setup_env(tmp_path, config)
    assert run_hook(env, transition_id="41").returncode == 2, \
        "a malformed value is unconfigured, not a licence to gate nothing"


def test_fails_open_on_unknown_transition_id(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, transition_id="99").returncode == 0, \
        "an unmapped id means unknown target, and unknown must not block"


def test_fails_open_when_config_missing(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / "home"),
               JIRA_CONFIG_PATH=str(tmp_path / "absent.json"))
    assert run_hook(env, transition_id="41").returncode == 0


def test_ignores_unrelated_tools(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, tool_name="Bash").returncode == 0


def test_fails_open_on_garbage_payload(tmp_path):
    env, home = setup_env(tmp_path)
    proc = subprocess.run([sys.executable, HOOK], input="not json",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0


def test_sentinel_path_cannot_traverse_out_of_verified_dir(tmp_path):
    env, home = setup_env(tmp_path)
    proc = run_hook(env, issue="AGENT-../../../../etc/passwd", transition_id="31")
    # Project prefix no longer matches AGENT, so it is ignored; either way it
    # must not touch anything outside the verified dir.
    assert proc.returncode == 0
    assert os.path.exists("/etc/passwd"), "a traversal must never reach a real path"


def _finalize_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("finalize_mod", FINALIZE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["finalize_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_a_json_object_serialized_as_a_string_is_decoded_not_keyword_scanned():
    """The live wire carries JSON-as-a-string. Falling through to the prose
    regex keyword-scans the issue's OWN embedded text, and both directions of
    that mistake are damaging -- verified against real recorded responses."""
    mod = _finalize_module()

    # FAILURE misread as success -> the sentinel is deleted for a transition
    # that never landed. This is the AGENT-11 wedge. Note "errorMessages"
    # contains no standalone word "error", so the prose regex misses it.
    assert mod.transition_succeeded('{"errorMessages": ["Issue does not exist"]}') is False
    assert mod.transition_succeeded('{"success": false}') is False
    assert mod.transition_succeeded({"success": False}) is False

    # SUCCESS misread as failure -> a spent sentinel is resurrected and a second
    # gated transition is authorized with no re-verification. "512" here comes
    # from an issue description ("memory 512 MB"), not an HTTP status.
    assert mod.transition_succeeded(
        '{"success": true, "fields": {"description": "memory 512 MB, timeout 10 s"}}'
    ) is True
    assert mod.transition_succeeded('{"success": true}') is True


def test_prose_screening_still_applies_to_genuinely_unstructured_text():
    mod = _finalize_module()
    assert mod.transition_succeeded("Issue transitioned") is True
    assert mod.transition_succeeded("Error: could not transition") is False
    assert mod.transition_succeeded("not valid json {") is True


def run_hook_raw(env, tool_input):
    """Send tool_input verbatim, so a test can hand the hook a non-dict."""
    payload = {"tool_name": TRANSITION, "tool_input": tool_input}
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def read_audit(home):
    path = os.path.join(str(home), ".claude", "logs", "team-hooks.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


STRINGIFIED = json.dumps({"issueIdOrKey": "AGENT-14", "transition": {"id": "31"}})


def test_a_json_string_tool_input_does_not_bypass_the_gate(tmp_path):
    """A model that stringifies tool_input used to walk straight through: the
    string reduces to {}, issueIdOrKey comes back empty, the project-scope check
    calls the issue somebody else's and the gate exits 0. An unverified
    transition lands. The same call as a dict is blocked, so the shape of the
    payload -- not the state of the work -- decided whether the gate applied."""
    env, home = setup_env(tmp_path)
    proc = run_hook_raw(env, STRINGIFIED)
    assert proc.returncode == 2, "a stringified payload must be gated like a dict"
    assert "AGENT-14" in proc.stderr


def test_a_json_string_tool_input_still_passes_when_verified(tmp_path):
    """Decoding must not turn the gate into a trap: with the sentinel written,
    the stringified payload is allowed and consumed exactly like a dict one."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook_raw(env, STRINGIFIED).returncode == 0
    assert not os.path.exists(path), "the sentinel is consumed, as for a dict payload"


def test_a_json_string_tool_input_for_another_project_is_still_ignored(tmp_path):
    env, home = setup_env(tmp_path)
    other = json.dumps({"issueIdOrKey": "SCRUM-2", "transition": {"id": "41"}})
    assert run_hook_raw(env, other).returncode == 0


def test_an_unreadable_tool_input_fails_open_with_a_truthful_reason(tmp_path):
    """Fail-open is right -- without an issue key the hook cannot tell its own
    project from the operator's real client work, and blocking there would be a
    serious defect. But the audit reason must name the real cause: recording
    'issue belongs to another project' for an issue whose project was never
    read is a false trail, and worse for diagnosis than the raw exception it
    replaced."""
    env, home = setup_env(tmp_path)
    assert run_hook_raw(env, "AGENT-14 to In Review").returncode == 0
    reason = read_audit(home)[-1]["reason"]
    assert "tool_input" in reason
    assert "another project" not in reason, "the hook never read a project here"


def test_a_failure_encoded_as_a_json_string_restores_the_sentinel(tmp_path):
    """End-to-end on the shape that matters, through both real hooks."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert run_finalize(
        env, response='{"errorMessages": ["Issue does not exist"]}').returncode == 0
    assert os.path.exists(path), "a failed transition must leave the sentinel usable"
    assert run_hook(env, transition_id="31").returncode == 0, "the retry must pass"
