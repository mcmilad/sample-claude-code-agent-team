"""Bootstrap uses stdlib urllib so hooks and scripts share a zero-dependency
runtime. Tests stub the transport rather than the network.
"""
import json
import os
import sys
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import jira_bootstrap  # noqa: E402

# Real cloud id for the site this will eventually run against (mcmilad.atlassian.net).
# Used only as fixture data here -- never referenced by the implementation, and no
# test makes a live call to this or any other site.
A_CLOUD_ID = "a92ccd30-64c9-4992-a6a3-fb5cc92cbeb9"


class FakeTransport:
    """Records requests and replays queued responses keyed by 'METHOD path'."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body))
        for key, value in self.responses.items():
            verb, fragment = key.split(" ", 1)
            if method == verb and fragment in url:
                return value
        raise AssertionError("unstubbed request: {} {}".format(method, url))


def admin(responses):
    a = jira_bootstrap.JiraAdmin("example.atlassian.net", "me@example.com", "tok")
    a.transport = FakeTransport(responses)
    return a


def test_find_project_returns_none_when_absent():
    a = admin({"GET /rest/api/3/project/search": {"values": []}})
    assert a.find_project("AGENT") is None


def test_find_project_returns_matching_project():
    a = admin({"GET /rest/api/3/project/search": {
        "values": [{"key": "AGENT", "id": "10001"}, {"key": "SCRUM", "id": "10000"}]}})
    assert a.find_project("AGENT")["id"] == "10001"


def test_ensure_project_is_idempotent():
    a = admin({"GET /rest/api/3/project/search": {"values": [{"key": "AGENT", "id": "10001"}]}})
    a.ensure_project("AGENT", "Agent Team")
    assert not any(m == "POST" for m, _, _ in a.transport.calls), \
        "an existing project must not be recreated"


def test_ensure_project_creates_a_team_managed_scrum_project():
    a = admin({
        "GET /rest/api/3/project/search": {"values": []},
        "GET /rest/api/3/myself": {"accountId": "5b10a2844c20165700ede21g"},
        "POST /rest/api/3/project": {"key": "AGENT", "id": "10001"},
    })
    a.ensure_project("AGENT", "Agent Team")
    posts = [(u, b) for m, u, b in a.transport.calls if m == "POST"]
    assert len(posts) == 1
    body = json.loads(posts[0][1])
    assert body["key"] == "AGENT"
    assert body["projectTypeKey"] == "software"
    assert "gh-simplified-agility-scrum" in body["projectTemplateKey"]


def test_discover_ids_maps_fields_statuses_and_transitions():
    a = admin({
        "GET /rest/api/3/field": [
            {"id": "customfield_10020", "name": "Sprint"},
            {"id": "customfield_10019", "name": "Rank"},
            {"id": "customfield_10021", "name": "Flagged"},
            {"id": "customfield_10016", "name": "Story point estimate"},
        ],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "To Do"},
                {"id": "10001", "name": "In Progress"},
                {"id": "10002", "name": "Done"},
            ]},
        ],
        "GET /rest/api/3/search": {"issues": [
            {"key": "AGENT-1", "fields": {"status": {"name": "To Do"}}},
        ]},
        "GET /rest/api/3/issue/AGENT-1/transitions": {"transitions": [
            {"id": "11", "to": {"name": "To Do"}},
            {"id": "21", "to": {"name": "In Progress"}},
            {"id": "31", "to": {"name": "Done"}},
        ]},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["fields"]["sprint"] == "customfield_10020"
    assert cfg["fields"]["rank"] == "customfield_10019"
    assert cfg["fields"]["flagged"] == "customfield_10021"
    assert cfg["statuses"]["To Do"] == "10000"
    assert cfg["transitions"]["21"] == "In Progress"
    assert cfg["boardId"] == 1


def test_discover_ids_flags_a_project_with_no_to_do_status():
    """'To Do' is a required contract, not a discovered name. A board without it
    must fail loudly at setup rather than silently producing a mirror whose
    create-events claim a status no issue ever has."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "Backlog"},
                {"id": "10002", "name": "Done"},
            ]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["missingRequiredStatus"] == "To Do"


def test_discover_ids_does_not_flag_a_project_that_has_to_do():
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    assert a.discover_ids("AGENT").get("missingRequiredStatus") is None


def test_main_exits_nonzero_when_to_do_is_absent(monkeypatch, tmp_path, capsys):
    """The precondition must break the build, not warn past it."""
    monkeypatch.setenv("JIRA_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(jira_bootstrap, "CONFIG_PATH", str(tmp_path / "jira-config.json"))
    monkeypatch.setattr(
        jira_bootstrap.JiraAdmin, "discover_ids",
        lambda self, key, **kwargs: {"projectKey": key, "statuses": {"Backlog": "1"},
                           "missingRequiredStatus": "To Do"},
    )
    code = jira_bootstrap.main(["discover", "--key", "AGENT"])
    assert code != 0
    assert "To Do" in capsys.readouterr().err


def test_discover_ids_reports_missing_in_review_status():
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "To Do"},
                {"id": "10002", "name": "Done"},
            ]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert "In Review" not in cfg["statuses"]
    assert cfg["gatedStatuses"] == ["Done"], \
        "gate only on statuses that actually exist, or every transition fails open"


def test_discover_ids_unions_transitions_across_distinct_statuses():
    """Jira only returns transitions reachable from an issue's *current* status,
    so sampling a single issue only ever covers one status's outbound edges.
    Probe one representative issue per distinct status and union the results."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "To Do"},
                {"id": "10001", "name": "In Progress"},
                {"id": "10002", "name": "Done"},
            ]}],
        "GET /rest/api/3/search": {"issues": [
            {"key": "AGENT-1", "fields": {"status": {"name": "To Do"}}},
            {"key": "AGENT-2", "fields": {"status": {"name": "In Progress"}}},
        ]},
        "GET /rest/api/3/issue/AGENT-1/transitions": {"transitions": [
            {"id": "21", "to": {"name": "In Progress"}},
        ]},
        "GET /rest/api/3/issue/AGENT-2/transitions": {"transitions": [
            {"id": "31", "to": {"name": "Done"}},
        ]},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["transitions"]["21"] == "In Progress"
    assert cfg["transitions"]["31"] == "Done"
    assert cfg["missingGatedTransitions"] == []


def test_discover_ids_reports_missing_gated_transitions_with_no_issues():
    """A fresh project has no issues to sample, so the transition map is empty
    by construction. That must surface as a loud, actionable shortfall -- not
    a verify gate that quietly fails open on the very transitions it guards."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "To Do"},
                {"id": "10002", "name": "Done"},
            ]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["transitions"] == {}
    assert cfg["missingGatedTransitions"] == ["Done"]


def test_discover_ids_reports_no_missing_gated_transitions_when_complete():
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "To Do"},
                {"id": "10001", "name": "In Progress"},
                {"id": "10002", "name": "Done"},
            ]}],
        "GET /rest/api/3/search": {"issues": [
            {"key": "AGENT-1", "fields": {"status": {"name": "In Progress"}}},
        ]},
        "GET /rest/api/3/issue/AGENT-1/transitions": {"transitions": [
            {"id": "31", "to": {"name": "Done"}},
        ]},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["missingGatedTransitions"] == []


def test_discover_ids_populates_cloud_id_from_tenant_info():
    """Every Atlassian MCP tool call requires cloudId, and the jira-workflow
    skill's first instruction to agents is to read it from this config -- so
    it must be discovered, not left blank."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": []},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["cloudId"] == A_CLOUD_ID


def test_discover_ids_keeps_existing_cloud_id_when_tenant_info_is_empty():
    """An empty/unusable tenant_info response must not blank a value already
    on disk -- the fallback is the whole point of carrying it forward."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": []},
        "GET /_edge/tenant_info": {},
    })
    cfg = a.discover_ids("AGENT", existing_cloud_id="previously-known-id")
    assert cfg["cloudId"] == "previously-known-id"


def test_discover_ids_keeps_existing_cloud_id_when_tenant_info_is_a_truthy_non_dict():
    """`x or {}` only guards *falsy* x -- a truthy non-dict (a JSON array, a
    bare string, exactly what a maintenance-mode or proxy error body often is)
    passes through unchanged and crashes on the next .get() unless the guard
    checks type, not just truthiness. Must fall back, not raise."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": []},
        "GET /_edge/tenant_info": ["unexpected"],
    })
    cfg = a.discover_ids("AGENT", existing_cloud_id="previously-known-id")
    assert cfg["cloudId"] == "previously-known-id"


def test_discover_ids_keeps_existing_cloud_id_when_tenant_info_lookup_fails():
    """A cloud-id lookup failure (network error) must not abort setup -- unlike
    the To Do and gated-transition preconditions, this one is recoverable: the
    operator can supply the value by hand."""
    inner = FakeTransport({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": []},
    })

    def flaky(method, url, body, headers):
        if "/_edge/tenant_info" in url:
            raise urllib.error.URLError("no route to host")
        return inner(method, url, body, headers)

    a = jira_bootstrap.JiraAdmin("example.atlassian.net", "me@example.com", "tok")
    a.transport = flaky
    cfg = a.discover_ids("AGENT", existing_cloud_id="previously-known-id")
    assert cfg["cloudId"] == "previously-known-id"


def test_write_config_round_trips_cloud_id(tmp_path):
    path = tmp_path / "jira-config.json"
    jira_bootstrap.write_config(str(path), {"cloudId": A_CLOUD_ID})
    with open(path) as fh:
        assert json.load(fh)["cloudId"] == A_CLOUD_ID


def test_main_exits_with_4_when_gated_transitions_incomplete(monkeypatch, tmp_path, capsys):
    """The transition-completeness precondition must break the build too, and
    with a code distinct from the missing-'To Do' case (3) and HTTP errors (2)."""
    monkeypatch.setenv("JIRA_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(jira_bootstrap, "CONFIG_PATH", str(tmp_path / "jira-config.json"))
    monkeypatch.setattr(
        jira_bootstrap.JiraAdmin, "discover_ids",
        lambda self, key, **kwargs: {"projectKey": key, "statuses": {"To Do": "1", "Done": "2"},
                           "missingRequiredStatus": None,
                           "gatedStatuses": ["Done"],
                           "transitions": {},
                           "missingGatedTransitions": ["Done"]},
    )
    code = jira_bootstrap.main(["discover", "--key", "AGENT"])
    assert code == 4
    assert "Done" in capsys.readouterr().err


def test_open_sprint_creates_then_starts():
    a = admin({
        "POST /rest/agile/1.0/sprint": {"id": 42, "name": "Group 1"},
        "POST /rest/agile/1.0/sprint/42": {"id": 42, "state": "active"},
    })
    sprint = a.open_sprint(board_id=1, name="Group 1")
    assert sprint["id"] == 42
    methods = [(m, u) for m, u, _ in a.transport.calls]
    assert methods[0][1].endswith("/rest/agile/1.0/sprint")
    assert "/sprint/42" in methods[1][1]


def test_close_sprint_sets_closed_state():
    a = admin({"POST /rest/agile/1.0/sprint/42": {"id": 42, "state": "closed"}})
    a.close_sprint(42)
    body = json.loads(a.transport.calls[0][2])
    assert body["state"] == "closed"


def test_write_config_is_json_and_round_trips(tmp_path):
    path = tmp_path / "jira-config.json"
    jira_bootstrap.write_config(str(path), {"projectKey": "AGENT", "transitions": {"21": "In Progress"}})
    with open(path) as fh:
        assert json.load(fh)["transitions"]["21"] == "In Progress"


def test_missing_credentials_exits_nonzero_without_prompting(monkeypatch, capsys):
    for var in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    code = jira_bootstrap.main(["discover"])
    assert code != 0
    assert "JIRA_API_TOKEN" in capsys.readouterr().err
