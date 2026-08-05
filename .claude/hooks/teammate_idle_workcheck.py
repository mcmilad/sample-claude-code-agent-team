#!/usr/bin/env python3
"""TeammateIdle hook -- nudge a teammate to claim claimable work before idling.

Exit 2 keeps the teammate working and delivers the nudge via stderr.

Data source is the local mirror journal, not Jira: a hook subprocess holds no
OAuth token. Claimable means, in mirror state:
  - status 'To Do' (unclaimed is a status, not a label -- JQL cannot wildcard
    labels, so claim state is encoded in status),
  - a role-<mine> label matching this teammate's role, and
  - no agent-* label.

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
    claimable = []
    for key, issue in jira_mirror.load_state(project).items():
        labels = issue.get("labels") or []
        if issue.get("status") != "To Do":
            continue
        if role_label not in labels:
            continue
        if jira_mirror.agent_label(labels):
            continue
        claimable.append(key)
    claimable.sort()

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

    block(EVENT, p, (
        "Before idling: {} unclaimed {} issue(s) are available -- {}.\n"
        "Claim one:\n"
        "  1. getJiraIssue to read its current labels\n"
        "  2. editJiraIssue fields.labels = <existing labels> + ['agent-{}']\n"
        "  3. transitionJiraIssue to In Progress\n"
        "  4. re-read; if another agent-* label appeared, lowest instance name "
        "wins and the loser drops its label and picks another issue\n"
        "Or send the lead a one-line note that you are genuinely done. (nudge {}/{})"
    ).format(len(claimable), role_label, ", ".join(claimable), teammate,
             count + 1, MAX_NUDGES))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
