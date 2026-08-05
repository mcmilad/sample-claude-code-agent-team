#!/usr/bin/env python3
"""TeammateIdle hook -- nudge a teammate to claim claimable work before idling.

Exit 2 keeps the teammate working and delivers the nudge via stderr.

Data source is the local mirror journal, not Jira: a hook subprocess holds no
OAuth token. Claimable means, in mirror state:
  - status 'To Do' (unclaimed is a status, not a label -- JQL cannot wildcard
    labels, so claim state is encoded in status),
  - a role-<mine> label matching this teammate's role, and
  - no agent-* label.

SCOPE: RANK, NEVER FILTER. The mirror spans every spec and group that has ever
run in this project, so the claimable set is wider than the sprint-scoped Find
JQL the teammate is supposed to use -- one teammate has been observed offered
two issues from two different specs and groups in a single nudge. The fix is to
PARTITION the message (in-scope first, out-of-scope flagged), not to filter.
Every conjunct added to the claimable set fails in the *silent allow-idle*
direction, trading a bounded, visible false positive (a nudge toward the wrong
issue, which the lead catches) for an unbounded, unlogged one: a teammate that
goes idle while real work sits unclaimed. So an issue is dropped only for a
CONTRADICTING label, never a missing one, and the partition counts are audited.

Known blind spot: the mirror only sees mutations made through the MCP, so a
card dragged by hand on the board is invisible here until an agent next touches
it. Acceptable -- this nudge is advisory and fail-open.

LOOP GUARD (critical): a naive nudge would trap a teammate forever. State at
~/.claude/logs/idle-nudges/<team>__<teammate>.json tracks how many times we have
nudged for the SAME claimable set. After MAX_NUDGES the teammate may idle. A
changed set resets the counter; an emptied set clears the state file.

FAIL OPEN: any internal error allows idle.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, role_of_teammate, nudge_dir,
    safe_path_component,
)
import jira_mirror  # noqa: E402

EVENT = "TeammateIdle"
MAX_NUDGES = 2


def _repo_root():
    """Repo root: $CLAUDE_PROJECT_DIR, else derived from this file's location.

    The TeammateIdle payload carries only team_name and teammate_name -- no cwd,
    no session, no sprint -- so the scope cannot come from the payload. The hook
    lives at <repo>/.claude/hooks/, which is a reliable anchor.

    An explicitly-set CLAUDE_PROJECT_DIR is honoured even if it does not exist:
    falling back to this file's location when it is set-but-invalid would make
    the hook silently resolve scope against a DIFFERENT checkout than the one
    the session is working in. Absent-and-unset is the only case that derives.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return env
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def current_scope():
    """Best-effort ('spec-<slug>', 'group-<n>') for the run in flight.

    The lead records the Epic key and per-group sprint ids in
    .claude/specs/<slug>/jira-run.json, so the most recently touched one names
    the spec in flight; the slug is its parent directory. Group is read from the
    same file when present under any of a few plausible keys, since the file is
    lead-authored and its shape is not contractual.

    Returns (None, None) when nothing can be resolved -- which disables
    partitioning entirely rather than guessing. This must never raise: an
    unresolvable scope has to degrade to today's unpartitioned behaviour.
    """
    spec = group = None
    try:
        specs_dir = os.path.join(_repo_root(), ".claude", "specs")
        runs = []
        for slug in os.listdir(specs_dir):
            path = os.path.join(specs_dir, slug, "jira-run.json")
            if os.path.isfile(path):
                runs.append((os.path.getmtime(path), slug, path))
        if not runs:
            return (None, None)
        _, slug, path = max(runs)
        spec = "spec-" + slug
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in ("group", "currentGroup", "activeGroup"):
                if data.get(key) is not None:
                    group = "group-" + str(data[key])
                    break
    except Exception:
        return (spec, group)
    return (spec, group)


def _out_of_scope(labels, scope_label, prefix):
    """True only when the issue CONTRADICTS the scope.

    An issue carrying no label with this prefix is never out of scope -- absence
    is unknown, not mismatch. Dropping on a missing label is exactly how a
    scoping change turns into silent work-abandonment.
    """
    if not scope_label:
        return False
    present = [l for l in labels if str(l).startswith(prefix)]
    if not present:
        return False
    return scope_label not in present


def _state_path(team, teammate):
    # Both fields are attacker-influenceable (Threat Model SS6) and this path is
    # passed to os.remove() below, so each component is independently reduced
    # to a single safe path component -- same discipline as the verify gate's
    # sentinel_path() -- rather than a bare .replace("/", "_") on the joined
    # string, which left every other unsafe character (e.g. "..") untouched.
    safe = safe_path_component(team) + "__" + safe_path_component(teammate)
    return os.path.join(nudge_dir(), safe + ".json")


def main():
    p = read_payload()
    team = p.get("team_name")
    teammate = p.get("teammate_name")
    role = role_of_teammate(teammate)
    if not team or not role:
        allow(EVENT, p, reason="no team or unmapped role -- not nudging")

    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- not nudging")

    role_label = "role-" + role
    spec_scope, group_scope = current_scope()
    in_scope, out_scope = [], []
    for key, issue in jira_mirror.load_state(project).items():
        labels = issue.get("labels") or []
        if issue.get("status") != "To Do":
            continue
        if role_label not in labels:
            continue
        if jira_mirror.agent_label(labels):
            continue
        # Partition, never drop: an out-of-scope issue is still surfaced, just
        # flagged, so a wrong or stale scope can never withhold work.
        if (_out_of_scope(labels, spec_scope, "spec-")
                or _out_of_scope(labels, group_scope, "group-")):
            out_scope.append(key)
        else:
            in_scope.append(key)
    in_scope.sort()
    out_scope.sort()
    claimable = in_scope + out_scope

    state_path = _state_path(team, teammate)
    if not claimable:
        try:
            os.remove(state_path)
        except Exception:
            pass
        allow(EVENT, p, reason="no claimable {} work".format(role_label))

    sig = hashlib.sha1(",".join(claimable).encode()).hexdigest()[:12]
    state = {}
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except Exception:
        state = {}
    count = state.get("count", 0) if state.get("sig") == sig else 0

    if count >= MAX_NUDGES:
        allow(EVENT, p, reason="nudge cap reached for set {} -- allowing idle".format(sig))

    try:
        os.makedirs(nudge_dir(), exist_ok=True)
        with open(state_path, "w") as fh:
            json.dump({"sig": sig, "count": count + 1}, fh)
    except Exception:
        pass

    lines = ["Before idling: {} unclaimed {} issue(s) may be available.".format(
        len(claimable), role_label)]
    if in_scope:
        lines.append("  In the current scope: " + ", ".join(in_scope))
    if out_scope:
        lines.append("  Outside it (other spec/group -- confirm with the lead "
                     "before claiming): " + ", ".join(out_scope))
    lines.append(
        "These are candidates from the LOCAL MIRROR, which may be stale and is not "
        "sprint-scoped. Re-run the sprint-scoped Find JQL from `jira-workflow` and "
        "claim from that result, not from this list.")

    if role == "review":
        # Reviewers are partitioned by the lead's handoff, not by claiming, and
        # there is exactly one role-review card per sprint. Telling four
        # reviewers to self-claim it would manufacture the collision.
        lines.append(
            "You are a reviewer: do NOT self-claim. Your slice and your "
            "synthesizer/analyst role come from the lead's handoff, and the sprint's "
            "role-review card belongs to the synthesizer. If you have no slice, ask "
            "the lead.")
    else:
        lines.append(
            "To claim one -- the mkdir lock decides ownership, not the label:\n"
            "  1. mkdir ~/.claude/logs/claims/{}/<ISSUE-KEY>  -- succeeds for exactly "
            "one agent; if it fails you lost, pick another issue\n"
            "  2. getJiraIssue(..., fields=[\"labels\",\"status\",\"summary\","
            "\"description\",\"issuelinks\"])\n"
            "     -- an explicit list replaces the defaults, so it must include labels\n"
            "  3. editJiraIssue fields.labels = <those labels> + ['agent-{}'], then "
            "transitionJiraIssue to In Progress, then comment 'Claimed by {}.'\n"
            "  4. re-read to confirm -- FAIL OPEN: an absent label with no competing "
            "agent-* is an unconfirmed write, not a loss. You hold the lock; re-apply "
            "it. Never count agent-* labels to detect a race -- editJiraIssue replaces "
            "the array, so only one ever survives.".format(project, teammate, teammate))
    lines.append("Or send the lead a one-line note that you are genuinely done. "
                 "(nudge {}/{})".format(count + 1, MAX_NUDGES))

    block(EVENT, p, "\n".join(lines),
          extra={"in_scope": len(in_scope), "out_of_scope": len(out_scope),
                 "scope": {"spec": spec_scope, "group": group_scope}})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
