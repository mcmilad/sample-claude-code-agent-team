#!/usr/bin/env python3
"""Jira admin plane for the agent team.

The MCP's OAuth grant carries only read:jira-work and write:jira-work, so it
cannot create a project, add a workflow status, or run the sprint lifecycle.
This script does those things with a Jira API token, and discovers the per-site
IDs everything else reads from .claude/jira-config.json.

SECURITY: the API token acts with the operator's full Jira permissions -- far
beyond the MCP's read/write:jira-work. This script is the only surface that
uses it, and agents are *instructed* not to hold or echo it. That instruction
is not a mechanism: a token exported into the shell that launches Claude Code
is inherited by every agent Bash subprocess and readable by any teammate.

The one real confinement is where you export it. Run these commands in a
terminal SEPARATE from the one running Claude Code, so the token is never in
the agent session's environment at all. If you run them from inside the session
(or via the team lead), treat the token as exposed to every agent in it.

Non-interactive by contract (see .claude/rules/execution-hygiene.md): every
input arrives via argument or environment variable, nothing reads stdin, and a
missing input exits non-zero rather than prompting.

Usage (in a separate terminal, not the Claude Code session's shell):
    export JIRA_SITE=your-site.atlassian.net
    export JIRA_EMAIL=you@example.com
    export JIRA_API_TOKEN=...            # id.atlassian.com > Security > API tokens

    python3 scripts/jira_bootstrap.py ensure-project --key AGENT --name "Agent Team"
    # Create the first few issues now, at least one per workflow status. `discover`
    # unions the transition map from real issues' current-status transitions, so on
    # a brand-new project with none yet, that map is necessarily empty -- and
    # `discover` will fail by design (exit 4) until issues exist to sample. This
    # ordering is intentional; don't try to work around it.
    python3 scripts/jira_bootstrap.py discover --key AGENT
    python3 scripts/jira_bootstrap.py sprint-open --name "Group 1 - interfaces"
    python3 scripts/jira_bootstrap.py sprint-close --id 42

    # Cleanup for smoke-test issues (the Atlassian MCP has no delete tool).
    # Omitting --confirm is a dry run: it reports the plan and deletes nothing.
    python3 scripts/jira_bootstrap.py delete-issues --keys AGENT-2,AGENT-3,AGENT-1
    python3 scripts/jira_bootstrap.py delete-issues --keys AGENT-2,AGENT-3,AGENT-1 --confirm
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO, ".claude", "jira-config.json")

# Team-managed ("next-gen") Scrum. Team-managed matters: board columns and
# statuses can then be changed without a site admin.
SCRUM_TEMPLATE = "com.pyxis.greenhopper.jira:gh-simplified-agility-scrum"

FIELD_ALIASES = {"sprint": "Sprint", "rank": "Rank", "flagged": "Flagged"}
PREFERRED_GATED = ("In Review", "Done")

# The one status name the system treats as a literal rather than discovering.
# See "To Do is a required status" in the plan's Global Constraints.
REQUIRED_STATUS = "To Do"


def _as_dict(value):
    """Coalesce a value to a dict before .get()/`in`, guarding on *type* rather
    than truthiness.

    `value or {}` only guards falsy values (None, {}, [], ""). A truthy
    non-dict -- a JSON array, a bare string, a number, exactly what a
    maintenance-mode or proxy error body often is -- passes through unchanged
    and crashes on the next .get(). Every value here originates from a
    parsed HTTP response body (or a config dict built from one), so the type
    is never guaranteed.
    """
    return value if isinstance(value, dict) else {}


def _as_list(value):
    """Same idea as _as_dict, for list-shaped values."""
    return value if isinstance(value, list) else []


def _jira_iso(dt):
    """Format a UTC datetime as the ISO-8601 shape Jira's sprint API expects:
    milliseconds and a literal 'Z' zone, e.g. 2026-08-05T09:00:00.000Z.

    `dt.isoformat()` does NOT produce this on its own -- it emits microsecond
    precision (6 digits, or none at all when they're zero) and a '+00:00'
    offset rather than 'Z', so it must be formatted explicitly rather than
    trusted as-is.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(dt.microsecond // 1000)


def _http(method, url, body, headers):
    """Default transport. Tests replace JiraAdmin.transport with a stub."""
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode() or "{}"
    return json.loads(raw) if raw.strip() else {}


def _http_with_status(method, url, body, headers):
    """Status-inspecting transport, used only where a caller needs to treat a
    4xx as a *signal* rather than a fatal error -- the delete-issues route
    probe (404 means "route exists", 405/410 mean "route moved or was
    removed"), and per-key deletes (404 means "already gone", not a failure).

    `_http` above is untouched and keeps raising HTTPError for every existing
    caller; this is a separate function so that contract never changes.
    Returns (status, data). A 204/empty body decodes to {} rather than
    raising on json.loads("").  Tests replace JiraAdmin.transport_status with
    a stub of the same (status, data) shape.
    """
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
    if not raw.strip():
        return status, {}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"raw": raw}


class JiraAdmin:
    def __init__(self, site, email, token):
        self.site = site.replace("https://", "").rstrip("/")
        self.auth = base64.b64encode("{}:{}".format(email, token).encode()).decode()
        self.transport = _http
        self.transport_status = _http_with_status

    def _headers(self):
        return {
            "Authorization": "Basic " + self.auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def request(self, method, path, body=None):
        url = "https://{}{}".format(self.site, path)
        payload = json.dumps(body) if body is not None else None
        return self.transport(method, url, payload, self._headers())

    def request_status(self, method, path, body=None):
        """Like `request`, but never raises on a 4xx/5xx -- returns
        (status, data) so the caller can inspect the code itself. See
        `_http_with_status` for why this is a separate transport."""
        url = "https://{}{}".format(self.site, path)
        payload = json.dumps(body) if body is not None else None
        return self.transport_status(method, url, payload, self._headers())

    # -- project ---------------------------------------------------------

    def find_project(self, key):
        result = self.request("GET", "/rest/api/3/project/search?query=" + key)
        for project in result.get("values", []):
            if project.get("key") == key:
                return project
        return None

    def ensure_project(self, key, name):
        existing = self.find_project(key)
        if existing:
            return existing
        account_id = self.request("GET", "/rest/api/3/myself").get("accountId")
        return self.request("POST", "/rest/api/3/project", {
            "key": key,
            "name": name,
            "projectTypeKey": "software",
            "projectTemplateKey": SCRUM_TEMPLATE,
            "leadAccountId": account_id,
            "assigneeType": "UNASSIGNED",
        })

    # -- discovery -------------------------------------------------------

    def discover_ids(self, project_key, existing_cloud_id=""):
        """Resolve per-site field, status, transition, board and cloud IDs.

        Nothing here may be hardcoded: these IDs differ per site, and this repo
        is a public template. `existing_cloud_id` is the value already on disk
        (if any), used as a fallback when the tenant_info lookup can't confirm
        a value -- see the cloudId block below.
        """
        fields = {}
        by_name = {f.get("name"): f.get("id") for f in self.request("GET", "/rest/api/3/field")}
        for alias, jira_name in FIELD_ALIASES.items():
            if by_name.get(jira_name):
                fields[alias] = by_name[jira_name]

        statuses = {}
        for issue_type in self.request(
                "GET", "/rest/api/3/project/{}/statuses".format(project_key)):
            for status in issue_type.get("statuses", []):
                statuses[status["name"]] = status["id"]

        # Transition ids are only readable from a real issue, and Jira only
        # returns transitions reachable from that issue's *current* status --
        # sampling a single issue therefore only ever covers one status's
        # outbound edges. Probe one representative issue per distinct status
        # (capped at one page) and union the resulting id -> target-name maps.
        # /rest/api/3/search was removed by Atlassian (410 Gone in production
        # as of this writing); /rest/api/3/search/jql is its replacement. It
        # still returns an `issues` array, so the parsing below is unchanged.
        # It paginates via `nextPageToken` rather than `startAt`, but nothing
        # here ever used `startAt` -- a single page of up to 50 issues is the
        # deliberate cap (see the docstring above), not partial results to
        # page through.
        #
        # Verified live against a real Jira site: transitions in a team-managed
        # ("next-gen") Scrum project are isGlobal -- any status to any status,
        # with no workflow ordering restricting which targets a given status can
        # reach. So a single issue's transitions already enumerate the complete
        # id -> target-name map; the per-status union below is defensive (it
        # still holds if a site is configured with a non-global workflow) rather
        # than load-bearing. This is also why the setup instructions only say
        # "create the first few issues" and never "walk an issue through every
        # status by hand": on this project type, any one existing issue is
        # enough for `discover` to see every transition.
        transitions = {}
        probe = self.request(
            "GET", "/rest/api/3/search/jql?jql=project%3D{}&maxResults=50&fields=status".format(
                project_key))
        representative_by_status = {}
        for issue in probe.get("issues", []):
            status_name = _as_dict(_as_dict(issue.get("fields")).get("status")).get("name")
            if status_name and status_name not in representative_by_status:
                representative_by_status[status_name] = issue["key"]
        for key in representative_by_status.values():
            for t in self.request(
                    "GET", "/rest/api/3/issue/{}/transitions".format(key)).get("transitions", []):
                target = _as_dict(t.get("to")).get("name")
                if target:
                    transitions[str(t["id"])] = target

        board_id = None
        for board in self.request("GET", "/rest/agile/1.0/board").get("values", []):
            if _as_dict(board.get("location")).get("projectKey") == project_key:
                board_id = board.get("id")
                break

        # Gate only on statuses that exist. Gating on an absent 'In Review'
        # would make every transition resolve to unknown and fail open --
        # a guardrail that silently does nothing is worse than none.
        # If that leaves NOTHING gated (a To Do / In Progress / Complete board),
        # the same reasoning applies to the whole gate: setup must fail, because
        # missingGatedTransitions is then vacuously empty and every other
        # precondition passes while the verification guardrail does nothing.
        # See _fail_if_no_status_is_gated.
        gated = [s for s in PREFERRED_GATED if s in statuses]

        # A gated status with no inbound transition id in the map means the
        # verify gate resolves that transition to "unknown target" and fails
        # open -- waving through exactly the transition (e.g. into Done) it
        # exists to block, while looking correctly installed. Union-sampling
        # only covers statuses a *current* issue occupies, so on a fresh
        # project (no issues yet) this is non-empty by construction; that is
        # surfaced as a hard failure by _fail_if_transition_map_incomplete,
        # not silently retried.
        missing_gated_transitions = [s for s in gated if s not in transitions.values()]

        # Every Atlassian MCP tool call (createJiraIssue, transitionJiraIssue,
        # searchJiraIssuesUsingJql, ...) requires cloudId as a parameter, and
        # it's the first thing the jira-workflow skill tells agents to read
        # from this config -- so an empty value here breaks every agent's
        # first MCP call. The lookup needs no auth, but unlike the To Do and
        # gated-transition preconditions, a failure here is recoverable (the
        # operator can supply it by hand), so it must not abort setup: fall
        # back to whatever the existing config held rather than blanking a
        # good value with an empty string.
        cloud_id = existing_cloud_id or ""
        try:
            tenant_info = self.request("GET", "/_edge/tenant_info")
        except (urllib.error.URLError, ValueError):
            tenant_info = {}
        discovered_cloud_id = _as_dict(tenant_info).get("cloudId")
        if discovered_cloud_id:
            cloud_id = discovered_cloud_id

        return {
            "site": self.site,
            "cloudId": cloud_id,
            "projectKey": project_key,
            "boardId": board_id,
            "fields": fields,
            # An alias that resolved to nothing is a field readers still use
            # unconditionally -- config.fields.flagged drives the whole blocker
            # protocol. Surfaced as a NOTE so the operator can map or rename the
            # field rather than discover the gap as silence at runtime.
            "unresolvedFields": sorted(a for a in FIELD_ALIASES if a not in fields),
            "statuses": statuses,
            "transitions": transitions,
            "gatedStatuses": gated,
            "missingGatedTransitions": missing_gated_transitions,
            # 'To Do' is a contract, not a discovery: "unclaimed" is encoded as a
            # status because JQL cannot wildcard labels, so the journaller and the
            # idle check both treat it as a literal. A board lacking it must fail
            # at setup -- otherwise every create-event journals a status no issue
            # ever holds, the idle check finds nothing claimable, and the guardrail
            # looks installed while doing nothing.
            "missingRequiredStatus": None if REQUIRED_STATUS in statuses else REQUIRED_STATUS,
        }

    # -- sprints ---------------------------------------------------------

    def open_sprint(self, board_id, name, days=14):
        """Create a sprint, then start it.

        Creating a *future* sprint needs no dates -- only transitioning it to
        `active` does, and Jira 400s ("You must specify a start date for the
        sprint.") without both startDate and endDate on that second call. The
        two calls stay separate on purpose: the creation body must never carry
        dates.

        `days` is nominal, not a real cadence: a "sprint" here models a
        parallel work group that may last minutes or hours, and the lead
        closes it explicitly via sprint-close. The window only exists because
        Jira's API requires an endDate to activate a sprint at all.
        """
        sprint = self.request("POST", "/rest/agile/1.0/sprint",
                              {"name": name, "originBoardId": board_id})
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=days)
        self.request("POST", "/rest/agile/1.0/sprint/{}".format(sprint["id"]),
                     {"state": "active", "startDate": _jira_iso(start),
                      "endDate": _jira_iso(end)})
        return sprint

    def close_sprint(self, sprint_id):
        return self.request("POST", "/rest/agile/1.0/sprint/{}".format(sprint_id),
                            {"state": "closed"})

    # -- deletion ----------------------------------------------------------
    #
    # The delete endpoint's exact semantics were never confirmed against
    # Atlassian's docs (the pages truncated on fetch during this project) --
    # only the probe below, run live, establishes what a given site actually
    # does. Every method here is written to degrade loudly rather than
    # assume: an unexpected status aborts instead of being treated as success.

    def probe_delete_route(self, project_key):
        """Spend one throwaway DELETE, against a key that cannot exist in the
        project, to convert "does this route still exist, and are we
        authorised" from an assumption into a fact -- the same discipline
        `discover_ids` applies to /rest/api/3/search, which Atlassian removed
        (410) without changing its shape otherwise. Returns the raw status
        code; the caller decides what each one means.
        """
        probe_key = "{}-999999".format(project_key)
        status, _ = self.request_status("DELETE", "/rest/api/3/issue/{}".format(probe_key))
        return status

    def get_issue_parent(self, key):
        """Returns the parent issue's key, or None if the issue has no parent
        -- including when the issue can't be fetched at all (already gone, or
        some other error). Ordering only needs a best-effort signal: a key
        that can't be resolved just falls into the parentless group, and the
        real delete call surfaces whatever is actually wrong with it.
        """
        status, data = self.request_status(
            "GET", "/rest/api/3/issue/{}?fields=parent".format(key))
        if status != 200:
            return None
        return _as_dict(_as_dict(data.get("fields")).get("parent")).get("key")

    def delete_issue(self, key, delete_subtasks=False):
        """Returns (status, data). Never raises -- 404 (already gone) is a
        normal, expected outcome here, not an error.
        """
        path = "/rest/api/3/issue/{}".format(key)
        if delete_subtasks:
            path += "?deleteSubtasks=true"
        return self.request_status("DELETE", path)


def write_config(path, config):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(config, fh, indent=2, sort_keys=True)
        fh.write("\n")


def read_config(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _parse_delete_keys(raw):
    """Comma-separated, explicit issue keys only -- no wildcards, no "all
    issues in project", no JQL. Blank entries (from a stray comma) are
    dropped rather than treated as a key."""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _first_key_outside_project(keys, project_key):
    """Returns the first key that does not belong to project_key, or None if
    every key does. Project scoping is absolute: the site also holds the
    operator's real client work in other projects, so this check runs before
    any network call at all -- a typo here must never be able to cascade.

    Jira project keys cannot contain a hyphen, so splitting an issue key on
    its first hyphen reliably recovers the project key it belongs to.
    """
    for key in keys:
        prefix = key.split("-", 1)[0] if "-" in key else key
        if prefix != project_key:
            return key
    return None


def _leaves_first_order(admin, keys):
    """Partition keys into (has-parent, parentless), preserving each key's
    relative input order within its group, and return has-parent first.

    This is deliberately not a full topological sort by depth -- the
    requirement is only that a parent (e.g. an Epic) is never deleted before
    a child that still references it, whether or not Jira cascades an Epic
    delete on its own. Checking "does this issue currently have a parent" and
    ordering on that boolean is sufficient for that, and doesn't require the
    operator to pass keys in any particular order.
    """
    with_parent, without_parent = [], []
    for key in keys:
        target = with_parent if admin.get_issue_parent(key) else without_parent
        target.append(key)
    return with_parent + without_parent


def _delete_error_message(data):
    """Best-effort human-readable message from a Jira error body, without
    ever touching the token or auth header -- those never appear in a
    response body, so this is safe by construction, not by omission."""
    data = _as_dict(data)
    messages = _as_list(data.get("errorMessages"))
    if messages:
        return "; ".join(str(m) for m in messages)
    errors = _as_dict(data.get("errors"))
    if errors:
        return "; ".join("{}: {}".format(k, v) for k, v in errors.items())
    return json.dumps(data) if data else "(no error body)"


def _admin_from_env():
    site = os.environ.get("JIRA_SITE")
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    missing = [n for n, v in
               (("JIRA_SITE", site), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token)) if not v]
    if missing:
        print("Missing required environment: {}.\n"
              "Create a token at id.atlassian.com > Security > API tokens, then export "
              "JIRA_SITE, JIRA_EMAIL and JIRA_API_TOKEN.".format(", ".join(missing)),
              file=sys.stderr)
        return None
    return JiraAdmin(site, email, token)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jira admin plane for the agent team")
    sub = parser.add_subparsers(dest="command", required=True)

    p_project = sub.add_parser("ensure-project")
    p_project.add_argument("--key", default="AGENT")
    p_project.add_argument("--name", default="Agent Team")

    p_discover = sub.add_parser("discover")
    p_discover.add_argument("--key", default="AGENT")

    p_open = sub.add_parser("sprint-open")
    p_open.add_argument("--name", required=True)
    # Nominal window, not a real cadence: a "sprint" here models a parallel
    # work group that may last minutes, and the lead closes it explicitly via
    # sprint-close. This only exists because Jira requires an endDate to
    # activate a sprint at all -- don't read 14 as a planning decision.
    p_open.add_argument("--days", type=int, default=14)

    p_close = sub.add_parser("sprint-close")
    p_close.add_argument("--id", required=True)

    p_delete = sub.add_parser("delete-issues")
    p_delete.add_argument("--keys", required=True,
                          help="comma-separated, explicit issue keys -- no wildcards, no JQL")
    p_delete.add_argument("--confirm", action="store_true",
                          help="without this, runs as a dry run and deletes nothing")
    p_delete.add_argument("--delete-subtasks", action="store_true",
                          help="append deleteSubtasks=true; only use after a delete fails "
                               "with a message about subtasks")

    args = parser.parse_args(argv)
    admin = _admin_from_env()
    if admin is None:
        return 1

    try:
        if args.command == "ensure-project":
            project = admin.ensure_project(args.key, args.name)
            print("project {} ready (id {})".format(project.get("key"), project.get("id")))
            existing_cloud_id = read_config(CONFIG_PATH).get("cloudId", "")
            config = admin.discover_ids(args.key, existing_cloud_id=existing_cloud_id)
            write_config(CONFIG_PATH, config)
            if _fail_if_required_status_missing(config):
                return 3
            if _fail_if_no_status_is_gated(config):
                return 5
            if _fail_if_transition_map_incomplete(config):
                return 4
            _warn_if_no_in_review(config)
            _note_unresolved_fields(config)
            return 0

        if args.command == "discover":
            existing_cloud_id = read_config(CONFIG_PATH).get("cloudId", "")
            config = admin.discover_ids(args.key, existing_cloud_id=existing_cloud_id)
            write_config(CONFIG_PATH, config)
            print("wrote {}".format(CONFIG_PATH))
            if _fail_if_required_status_missing(config):
                return 3
            if _fail_if_no_status_is_gated(config):
                return 5
            if _fail_if_transition_map_incomplete(config):
                return 4
            _warn_if_no_in_review(config)
            _note_unresolved_fields(config)
            return 0

        config = read_config(CONFIG_PATH)
        if args.command == "sprint-open":
            board_id = config.get("boardId")
            if not board_id:
                print("no boardId in {} -- run `discover` first".format(CONFIG_PATH),
                      file=sys.stderr)
                return 1
            sprint = admin.open_sprint(board_id, args.name, days=args.days)
            print(json.dumps({"id": sprint["id"], "name": sprint.get("name")}))
            return 0

        if args.command == "sprint-close":
            admin.close_sprint(args.id)
            print("sprint {} closed".format(args.id))
            return 0

        if args.command == "delete-issues":
            return _run_delete_issues(admin, config, args)
    except urllib.error.HTTPError as e:
        print("Jira API error {} on {}: {}".format(e.code, args.command, e.read().decode()[:500]),
              file=sys.stderr)
        return 2

    return 1


# Exit codes for delete-issues, distinct from the discover/ensure-project
# codes above (2 HTTP error, 3 missing 'To Do', 4 incomplete transition map,
# 5 no status gated) so a caller can tell these failure modes apart.
DELETE_EXIT_KEY_OUTSIDE_PROJECT = 6
DELETE_EXIT_ROUTE_MOVED = 7
DELETE_EXIT_ROUTE_FORBIDDEN = 8
DELETE_EXIT_ROUTE_UNEXPECTED = 9
DELETE_EXIT_SOME_DELETIONS_FAILED = 10


def _run_delete_issues(admin, config, args):
    """Implements `delete-issues`. See the module docstring's Usage section
    and the class comment above JiraAdmin's "-- deletion --" methods for the
    reasoning; this function is just the CLI-level sequencing:

      1. parse + validate --keys are all in-project (no network yet)
      2. compute the leaves-first delete order (GET only, safe in dry run)
      3. dry run: report the plan and stop
      4. --confirm: probe the route once, then delete in that order
    """
    keys = _parse_delete_keys(args.keys)
    if not keys:
        print("ERROR: --keys must list at least one issue key.", file=sys.stderr)
        return 1

    project_key = config.get("projectKey")
    if not project_key:
        print("no projectKey in {} -- run `discover` first".format(CONFIG_PATH),
              file=sys.stderr)
        return 1

    bad_key = _first_key_outside_project(keys, project_key)
    if bad_key:
        print(
            "\nERROR: '{}' does not belong to project '{}' -- aborting before deleting\n"
            "anything. Project scoping here is absolute: this site also holds the\n"
            "operator's real client work in other projects, and a typo in --keys must\n"
            "never be able to reach it.\n"
            "Keys given: {}\n".format(bad_key, project_key, ", ".join(keys)),
            file=sys.stderr)
        return DELETE_EXIT_KEY_OUTSIDE_PROJECT

    order = _leaves_first_order(admin, keys)

    if not args.confirm:
        print("DRY RUN -- no issues will be deleted (pass --confirm to actually delete).")
        print("Planned order (leaves first, so a parent is never removed before its "
              "children):")
        for i, key in enumerate(order, 1):
            print("  {}. {}".format(i, key))
        return 0

    probe_status = admin.probe_delete_route(project_key)
    if probe_status == 404:
        pass  # route exists and we're authorised -- proceed
    elif probe_status in (405, 410):
        print(
            "\nERROR: the delete route probe returned {}. That means the endpoint has\n"
            "moved or been removed -- exactly like /rest/api/3/search's removal (410)\n"
            "silently broke `discover` until it was repointed at /search/jql. Aborting\n"
            "before deleting anything; re-verify the current delete endpoint before\n"
            "retrying.\n".format(probe_status),
            file=sys.stderr)
        return DELETE_EXIT_ROUTE_MOVED
    elif probe_status in (401, 403):
        print(
            "\nERROR: the delete route probe returned {} -- a permissions problem, not a\n"
            "route problem. Aborting before deleting anything.\n".format(probe_status),
            file=sys.stderr)
        return DELETE_EXIT_ROUTE_FORBIDDEN
    else:
        print(
            "\nERROR: the delete route probe returned unexpected status {} (expected 404\n"
            "for a nonexistent key). Aborting rather than assume it's safe to proceed.\n"
            .format(probe_status),
            file=sys.stderr)
        return DELETE_EXIT_ROUTE_UNEXPECTED

    failures = 0
    for key in order:
        status, data = admin.delete_issue(key, delete_subtasks=args.delete_subtasks)
        if status in (200, 202, 204):
            print("{} {}: deleted".format(key, status))
        elif status == 404:
            print("{} {}: already gone, skipped".format(key, status))
        else:
            failures += 1
            message = _delete_error_message(data)
            hint = ""
            if "subtask" in message.lower() and not args.delete_subtasks:
                hint = " Re-run with --delete-subtasks if this issue has subtasks."
            print("{} {}: FAILED -- {}{}".format(key, status, message, hint), file=sys.stderr)

    return DELETE_EXIT_SOME_DELETIONS_FAILED if failures else 0


def _fail_if_required_status_missing(config):
    """Hard precondition: the board MUST have a status named 'To Do'.

    Returns True when the setup should abort. This is deliberately an error and
    not a warning: 'unclaimed' is encoded as this status, so a board without it
    produces a mirror full of create-events claiming a status no issue holds,
    and an idle check that never finds claimable work. That failure is invisible
    at runtime, so it has to be loud here.
    """
    missing = config.get("missingRequiredStatus")
    if not missing:
        return False
    print(
        "\nERROR: project {} has no '{}' status, and the agent team requires one.\n"
        "'Unclaimed' is encoded as this status, so without it the idle work-check\n"
        "will silently never find claimable work.\n"
        "Statuses found: {}\n\n"
        "Fix it, then re-run this command:\n"
        "  1. Open the board > Board settings > Columns\n"
        "  2. Rename the first column to exactly 'To Do' (or add one)\n"
        "  3. python3 scripts/jira_bootstrap.py discover --key {}\n".format(
            config.get("projectKey", "?"), missing,
            ", ".join(sorted(_as_dict(config.get("statuses")))) or "(none)",
            config.get("projectKey", "AGENT")),
        file=sys.stderr)
    return True


def _fail_if_transition_map_incomplete(config):
    """Hard precondition: every gated status must have a mapped inbound transition id.

    Returns True when setup should abort. Same treatment as the missing-'To Do'
    case, and for the same reason: an unmapped transition id resolves to
    "unknown target" in the verify gate, which fails open -- silently waving
    through exactly the transitions (e.g. into Done) it exists to block, while
    looking correctly installed. Union-sampling only covers statuses a *current*
    issue occupies, so on a brand-new project (no issues yet) this fires on the
    very first `discover` call. That is intended: create issues covering each
    gated status, then re-run.
    """
    missing = _as_list(config.get("missingGatedTransitions"))
    if not missing:
        return False
    print(
        "\nERROR: no transition id maps to: {}.\n"
        "The verify gate resolves an unmapped transition id to 'unknown target',\n"
        "which fails open -- it would silently allow exactly the transitions into\n"
        "{} that it exists to block, while looking correctly installed.\n\n"
        "Fix it, then re-run this command:\n"
        "  1. Create (or move) at least one issue into each of these statuses\n"
        "  2. python3 scripts/jira_bootstrap.py discover --key {}\n".format(
            ", ".join(missing), ", ".join(missing), config.get("projectKey", "AGENT")),
        file=sys.stderr)
    return True


def _fail_if_no_status_is_gated(config):
    """Hard precondition: at least one status must actually be gated.

    Returns True when setup should abort. On a board whose columns are e.g.
    To Do / In Progress / Complete, no PREFERRED_GATED name exists, so `gated`
    computes to [] -- and then missingGatedTransitions is vacuously [] too, so
    the exit-4 precondition passes and setup reports success. The result is a
    verification guardrail that gates nothing at all while looking installed:
    exactly the "worse than none" case the discover comment calls out, but
    total rather than partial. Loud here, or invisible forever.
    """
    if _as_list(config.get("gatedStatuses")):
        return False
    print(
        "\nERROR: no status on project {} is gated, so the verification gate would\n"
        "gate nothing at all -- every transition, including into the board's final\n"
        "column, would be allowed with no sentinel while the hook looks installed.\n"
        "The gate needs one of: {}.\n"
        "Statuses found: {}\n\n"
        "Fix it, then re-run this command:\n"
        "  1. Open the board > Board settings > Columns\n"
        "  2. Rename/add a column so one of {} exists (a 'Complete' column is\n"
        "     usually just 'Done' under another name)\n"
        "  3. python3 scripts/jira_bootstrap.py discover --key {}\n".format(
            config.get("projectKey", "?"), ", ".join(PREFERRED_GATED),
            ", ".join(sorted(_as_dict(config.get("statuses")))) or "(none)",
            ", ".join(PREFERRED_GATED), config.get("projectKey", "AGENT")),
        file=sys.stderr)
    return True


def _note_unresolved_fields(config):
    """Advisory: a FIELD_ALIASES entry that resolved to no custom field.

    Not fatal -- only `flagged` has an unconditional reader (the blocker
    protocol), and a run can proceed without flagging. But the reader does not
    check first, so an unresolved alias must not be discovered as silence.
    """
    unresolved = _as_list(config.get("unresolvedFields"))
    if not unresolved:
        return
    print(
        "\nNOTE: these fields could not be resolved on this site: {}.\n"
        "Readers use them unconditionally -- config.fields.flagged is what the\n"
        "blocker protocol sets to raise an impediment -- so a missing one fails\n"
        "at use, not here. Expected Jira field names: {}.\n"
        "Add or rename the field on the site, then re-run discover.\n".format(
            ", ".join(unresolved),
            ", ".join("{} -> '{}'".format(a, FIELD_ALIASES[a]) for a in unresolved)),
        file=sys.stderr)


def _warn_if_no_in_review(config):
    if "In Review" in _as_dict(config.get("statuses")):
        return
    print(
        "\nNOTE: the project has no 'In Review' status, so the gated statuses are\n"
        "just: {}.\n"
        "Adding it needs a board edit this script cannot perform -- the API does\n"
        "not reliably create statuses on a team-managed project, so do it by hand.\n"
        "One-time, ~30 seconds:\n"
        "  1. Open the board > Board settings (or the '...' menu) > Columns\n"
        "  2. Add a column named 'In Review' between 'In Progress' and 'Done'\n"
        "  3. Re-run: python3 scripts/jira_bootstrap.py discover --key {}\n".format(
            ", ".join(_as_list(config.get("gatedStatuses"))) or "(none)",
            config.get("projectKey", "AGENT")),
        file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
