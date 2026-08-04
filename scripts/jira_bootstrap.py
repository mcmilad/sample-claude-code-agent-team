#!/usr/bin/env python3
"""Jira admin plane for the agent team.

The MCP's OAuth grant carries only read:jira-work and write:jira-work, so it
cannot create a project, add a workflow status, or run the sprint lifecycle.
This script does those things with a Jira API token, and discovers the per-site
IDs everything else reads from .claude/jira-config.json.

SECURITY: the API token acts with the operator's full Jira permissions. It is
deliberately confined to this one scripted surface. Teammates never hold it and
never invoke this script -- the team lead does, at group boundaries.

Non-interactive by contract (see .claude/rules/execution-hygiene.md): every
input arrives via argument or environment variable, nothing reads stdin, and a
missing input exits non-zero rather than prompting.

Usage:
    export JIRA_SITE=your-site.atlassian.net
    export JIRA_EMAIL=you@example.com
    export JIRA_API_TOKEN=...            # id.atlassian.com > Security > API tokens

    python3 scripts/jira_bootstrap.py ensure-project --key AGENT --name "Agent Team"
    python3 scripts/jira_bootstrap.py discover --key AGENT
    python3 scripts/jira_bootstrap.py sprint-open --name "Group 1 - interfaces"
    python3 scripts/jira_bootstrap.py sprint-close --id 42
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

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


def _http(method, url, body, headers):
    """Default transport. Tests replace JiraAdmin.transport with a stub."""
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode() or "{}"
    return json.loads(raw) if raw.strip() else {}


class JiraAdmin:
    def __init__(self, site, email, token):
        self.site = site.replace("https://", "").rstrip("/")
        self.auth = base64.b64encode("{}:{}".format(email, token).encode()).decode()
        self.transport = _http

    def request(self, method, path, body=None):
        url = "https://{}{}".format(self.site, path)
        headers = {
            "Authorization": "Basic " + self.auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = json.dumps(body) if body is not None else None
        return self.transport(method, url, payload, headers)

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

    def discover_ids(self, project_key):
        """Resolve per-site field, status, transition and board IDs.

        Nothing here may be hardcoded: these IDs differ per site, and this repo
        is a public template.
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

        # Transition ids are only readable from a real issue. Absent one, the
        # map stays empty and the verify gate fails open until re-run.
        transitions = {}
        probe = self.request(
            "GET", "/rest/api/3/search?jql=project%3D{}&maxResults=1".format(project_key))
        issues = probe.get("issues", [])
        if issues:
            key = issues[0]["key"]
            for t in self.request(
                    "GET", "/rest/api/3/issue/{}/transitions".format(key)).get("transitions", []):
                target = (t.get("to") or {}).get("name")
                if target:
                    transitions[str(t["id"])] = target

        board_id = None
        for board in self.request("GET", "/rest/agile/1.0/board").get("values", []):
            if (board.get("location") or {}).get("projectKey") == project_key:
                board_id = board.get("id")
                break

        # Gate only on statuses that exist. Gating on an absent 'In Review'
        # would make every transition resolve to unknown and fail open --
        # a guardrail that silently does nothing is worse than none.
        gated = [s for s in PREFERRED_GATED if s in statuses]

        return {
            "site": self.site,
            "projectKey": project_key,
            "boardId": board_id,
            "fields": fields,
            "statuses": statuses,
            "transitions": transitions,
            "gatedStatuses": gated,
            # 'To Do' is a contract, not a discovery: "unclaimed" is encoded as a
            # status because JQL cannot wildcard labels, so the journaller and the
            # idle check both treat it as a literal. A board lacking it must fail
            # at setup -- otherwise every create-event journals a status no issue
            # ever holds, the idle check finds nothing claimable, and the guardrail
            # looks installed while doing nothing.
            "missingRequiredStatus": None if REQUIRED_STATUS in statuses else REQUIRED_STATUS,
        }

    # -- sprints ---------------------------------------------------------

    def open_sprint(self, board_id, name):
        sprint = self.request("POST", "/rest/agile/1.0/sprint",
                              {"name": name, "originBoardId": board_id})
        self.request("POST", "/rest/agile/1.0/sprint/{}".format(sprint["id"]),
                     {"state": "active"})
        return sprint

    def close_sprint(self, sprint_id):
        return self.request("POST", "/rest/agile/1.0/sprint/{}".format(sprint_id),
                            {"state": "closed"})


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

    p_close = sub.add_parser("sprint-close")
    p_close.add_argument("--id", required=True)

    args = parser.parse_args(argv)
    admin = _admin_from_env()
    if admin is None:
        return 1

    try:
        if args.command == "ensure-project":
            project = admin.ensure_project(args.key, args.name)
            print("project {} ready (id {})".format(project.get("key"), project.get("id")))
            config = admin.discover_ids(args.key)
            config["cloudId"] = read_config(CONFIG_PATH).get("cloudId", "")
            write_config(CONFIG_PATH, config)
            if _fail_if_required_status_missing(config):
                return 3
            _warn_if_no_in_review(config)
            return 0

        if args.command == "discover":
            config = admin.discover_ids(args.key)
            config["cloudId"] = read_config(CONFIG_PATH).get("cloudId", "")
            write_config(CONFIG_PATH, config)
            print("wrote {}".format(CONFIG_PATH))
            if _fail_if_required_status_missing(config):
                return 3
            _warn_if_no_in_review(config)
            return 0

        config = read_config(CONFIG_PATH)
        if args.command == "sprint-open":
            board_id = config.get("boardId")
            if not board_id:
                print("no boardId in {} -- run `discover` first".format(CONFIG_PATH),
                      file=sys.stderr)
                return 1
            sprint = admin.open_sprint(board_id, args.name)
            print(json.dumps({"id": sprint["id"], "name": sprint.get("name")}))
            return 0

        if args.command == "sprint-close":
            admin.close_sprint(args.id)
            print("sprint {} closed".format(args.id))
            return 0
    except urllib.error.HTTPError as e:
        print("Jira API error {} on {}: {}".format(e.code, args.command, e.read().decode()[:500]),
              file=sys.stderr)
        return 2

    return 1


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
            ", ".join(sorted(config.get("statuses") or {})) or "(none)",
            config.get("projectKey", "AGENT")),
        file=sys.stderr)
    return True


def _warn_if_no_in_review(config):
    if "In Review" in (config.get("statuses") or {}):
        return
    print(
        "\nNOTE: the project has no 'In Review' status, so only 'Done' is gated.\n"
        "Adding it needs a board edit the API cannot reliably perform on a\n"
        "team-managed project. One-time, ~30 seconds:\n"
        "  1. Open the board > Board settings (or the '...' menu) > Columns\n"
        "  2. Add a column named 'In Review' between 'In Progress' and 'Done'\n"
        "  3. Re-run: python3 scripts/jira_bootstrap.py discover --key {}\n".format(
            config.get("projectKey", "AGENT")),
        file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
