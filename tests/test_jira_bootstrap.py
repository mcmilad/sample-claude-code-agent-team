"""Bootstrap uses stdlib urllib so hooks and scripts share a zero-dependency
runtime. Tests stub the transport rather than the network.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import jira_bootstrap  # noqa: E402

# Real cloud id for the site this will eventually run against (mcmilad.atlassian.net).
# Used only as fixture data here -- never referenced by the implementation, and no
# test makes a live call to this or any other site.
A_CLOUD_ID = "a92ccd30-64c9-4992-a6a3-fb5cc92cbeb9"


class FakeTransport:
    """Records requests and replays queued responses keyed by 'METHOD path'.

    Matching is on the exact request path (query string ignored), not
    substring containment -- a stub for ".../sprint" must not also answer a
    request aimed at ".../sprint/42", and a stub for ".../search/jql" must
    not answer a request aimed at the retired ".../search".
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body))
        path = urllib.parse.urlsplit(url).path
        for key, value in self.responses.items():
            verb, stub_path = key.split(" ", 1)
            if method == verb and path == stub_path:
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
        "GET /rest/api/3/search/jql": {"issues": [
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


def test_discover_ids_probes_the_current_search_endpoint():
    """/rest/api/3/search was removed by Atlassian (410 Gone in production);
    the replacement is /rest/api/3/search/jql. Only the new path is stubbed
    here, so a regression back to the retired path must fail as an
    unstubbed request rather than silently succeed."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search/jql": {"issues": [
            {"key": "AGENT-1", "fields": {"status": {"name": "To Do"}}},
        ]},
        "GET /rest/api/3/issue/AGENT-1/transitions": {"transitions": [
            {"id": "11", "to": {"name": "To Do"}},
        ]},
        "GET /rest/agile/1.0/board": {"values": []},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    a.discover_ids("AGENT")
    probe_requests = [
        (m, urllib.parse.urlsplit(u).path) for m, u, _ in a.transport.calls if m == "GET"
    ]
    assert ("GET", "/rest/api/3/search/jql") in probe_requests
    assert ("GET", "/rest/api/3/search") not in probe_requests


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
        "GET /rest/api/3/search/jql": {"issues": []},
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
        "GET /rest/api/3/search/jql": {"issues": []},
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
        "GET /rest/api/3/search/jql": {"issues": []},
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
        "GET /rest/api/3/search/jql": {"issues": [
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
        "GET /rest/api/3/search/jql": {"issues": []},
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
        "GET /rest/api/3/search/jql": {"issues": [
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
        "GET /rest/api/3/search/jql": {"issues": []},
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
        "GET /rest/api/3/search/jql": {"issues": []},
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
        "GET /rest/api/3/search/jql": {"issues": []},
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
        "GET /rest/api/3/search/jql": {"issues": []},
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


def test_discover_ids_reports_an_empty_gate_set():
    """A board whose columns are To Do / In Progress / Complete yields gated ==
    [], and then missingGatedTransitions == [] too -- so every existing
    precondition passes while the verification guardrail gates nothing at all.
    That has to be visible in the config and fatal at setup."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "To Do"},
                {"id": "10001", "name": "In Progress"},
                {"id": "10002", "name": "Complete"},
            ]}],
        "GET /rest/api/3/search/jql": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["gatedStatuses"] == []
    assert cfg["missingGatedTransitions"] == [], \
        "nothing is missing when nothing is gated -- which is the trap"


def test_main_exits_with_5_when_no_status_is_gated(monkeypatch, tmp_path, capsys):
    """Distinct from missing-'To Do' (3), the incomplete map (4) and HTTP (2)."""
    monkeypatch.setenv("JIRA_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(jira_bootstrap, "CONFIG_PATH", str(tmp_path / "jira-config.json"))
    monkeypatch.setattr(
        jira_bootstrap.JiraAdmin, "discover_ids",
        lambda self, key, **kwargs: {"projectKey": key,
                                     "statuses": {"To Do": "1", "Complete": "2"},
                                     "missingRequiredStatus": None,
                                     "gatedStatuses": [],
                                     "transitions": {"11": "To Do"},
                                     "missingGatedTransitions": []},
    )
    code = jira_bootstrap.main(["discover", "--key", "AGENT"])
    assert code == 5
    err = capsys.readouterr().err
    assert "no status" in err.lower() and "gated" in err.lower()


def test_ensure_project_also_exits_with_5_when_no_status_is_gated(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JIRA_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(jira_bootstrap, "CONFIG_PATH", str(tmp_path / "jira-config.json"))
    monkeypatch.setattr(jira_bootstrap.JiraAdmin, "ensure_project",
                        lambda self, key, name: {"key": key, "id": "10001"})
    monkeypatch.setattr(
        jira_bootstrap.JiraAdmin, "discover_ids",
        lambda self, key, **kwargs: {"projectKey": key,
                                     "statuses": {"To Do": "1", "Complete": "2"},
                                     "missingRequiredStatus": None,
                                     "gatedStatuses": [],
                                     "transitions": {"11": "To Do"},
                                     "missingGatedTransitions": []},
    )
    assert jira_bootstrap.main(["ensure-project", "--key", "AGENT"]) == 5


def test_discover_ids_reports_unresolved_field_aliases():
    """fields.flagged is written only when a field named exactly 'Flagged'
    exists, but the blocker protocol reads it unconditionally. An unresolved
    alias must be surfaced, not discovered as silence."""
    a = admin({
        "GET /rest/api/3/field": [
            {"id": "customfield_10020", "name": "Sprint"},
            {"id": "customfield_10019", "name": "Rank"},
        ],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "To Do"},
                {"id": "10002", "name": "Done"},
            ]}],
        "GET /rest/api/3/search/jql": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["unresolvedFields"] == ["flagged"]


def test_discover_ids_reports_no_unresolved_fields_when_all_resolve():
    a = admin({
        "GET /rest/api/3/field": [
            {"id": "customfield_10020", "name": "Sprint"},
            {"id": "customfield_10019", "name": "Rank"},
            {"id": "customfield_10021", "name": "Flagged"},
        ],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search/jql": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": []},
        "GET /_edge/tenant_info": {"cloudId": A_CLOUD_ID},
    })
    assert a.discover_ids("AGENT")["unresolvedFields"] == []


def test_main_notes_unresolved_field_aliases_without_failing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JIRA_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(jira_bootstrap, "CONFIG_PATH", str(tmp_path / "jira-config.json"))
    monkeypatch.setattr(
        jira_bootstrap.JiraAdmin, "discover_ids",
        lambda self, key, **kwargs: {"projectKey": key,
                                     "statuses": {"To Do": "1", "In Review": "2", "Done": "3"},
                                     "missingRequiredStatus": None,
                                     "gatedStatuses": ["In Review", "Done"],
                                     "transitions": {"31": "In Review", "41": "Done"},
                                     "missingGatedTransitions": [],
                                     "unresolvedFields": ["flagged"]},
    )
    assert jira_bootstrap.main(["discover", "--key", "AGENT"]) == 0
    err = capsys.readouterr().err
    assert "NOTE:" in err and "flagged" in err


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


def _parse_jira_iso(value):
    """Jira emits/accepts ISO-8601 with milliseconds and a 'Z' zone, e.g.
    2026-08-05T09:00:00.000Z. datetime.fromisoformat before 3.11 chokes on a
    trailing 'Z', so swap it for an explicit UTC offset before parsing."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_open_sprint_activation_call_carries_state_start_and_end_dates():
    """This is the bug: starting a sprint requires startDate and endDate on
    the activation call, or Jira 400s with 'You must specify a start date'."""
    a = admin({
        "POST /rest/agile/1.0/sprint": {"id": 42, "name": "Group 1"},
        "POST /rest/agile/1.0/sprint/42": {"id": 42, "state": "active"},
    })
    a.open_sprint(board_id=1, name="Group 1")
    activation_body = json.loads(a.transport.calls[1][2])
    assert activation_body["state"] == "active"
    assert "startDate" in activation_body
    assert "endDate" in activation_body
    start = _parse_jira_iso(activation_body["startDate"])
    end = _parse_jira_iso(activation_body["endDate"])
    assert end > start


def test_open_sprint_dates_are_iso8601_with_milliseconds_and_z():
    a = admin({
        "POST /rest/agile/1.0/sprint": {"id": 42, "name": "Group 1"},
        "POST /rest/agile/1.0/sprint/42": {"id": 42, "state": "active"},
    })
    a.open_sprint(board_id=1, name="Group 1")
    activation_body = json.loads(a.transport.calls[1][2])
    import re
    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
    assert re.match(pattern, activation_body["startDate"]), activation_body["startDate"]
    assert re.match(pattern, activation_body["endDate"]), activation_body["endDate"]


def test_open_sprint_days_argument_extends_end_date():
    a_default = admin({
        "POST /rest/agile/1.0/sprint": {"id": 42, "name": "Group 1"},
        "POST /rest/agile/1.0/sprint/42": {"id": 42, "state": "active"},
    })
    a_default.open_sprint(board_id=1, name="Group 1")
    default_body = json.loads(a_default.transport.calls[1][2])
    default_start = _parse_jira_iso(default_body["startDate"])
    default_end = _parse_jira_iso(default_body["endDate"])
    assert (default_end - default_start) == timedelta(days=14)

    a_custom = admin({
        "POST /rest/agile/1.0/sprint": {"id": 43, "name": "Group 1"},
        "POST /rest/agile/1.0/sprint/43": {"id": 43, "state": "active"},
    })
    a_custom.open_sprint(board_id=1, name="Group 1", days=1)
    custom_body = json.loads(a_custom.transport.calls[1][2])
    custom_start = _parse_jira_iso(custom_body["startDate"])
    custom_end = _parse_jira_iso(custom_body["endDate"])
    assert (custom_end - custom_start) == timedelta(days=1)
    assert custom_end < default_end


def test_open_sprint_creation_call_carries_no_dates():
    """Pins the two-call split: creating a *future* sprint needs no dates, only
    starting one does. A later refactor that merges the two calls (or leaks
    dates onto the creation call) must fail this test."""
    a = admin({
        "POST /rest/agile/1.0/sprint": {"id": 42, "name": "Group 1"},
        "POST /rest/agile/1.0/sprint/42": {"id": 42, "state": "active"},
    })
    a.open_sprint(board_id=1, name="Group 1")
    creation_body = json.loads(a.transport.calls[0][2])
    assert "startDate" not in creation_body
    assert "endDate" not in creation_body
    assert creation_body == {"name": "Group 1", "originBoardId": 1}


def test_main_sprint_open_honours_days_argument(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    config_path = tmp_path / "jira-config.json"
    jira_bootstrap.write_config(str(config_path), {"boardId": 1})
    monkeypatch.setattr(jira_bootstrap, "CONFIG_PATH", str(config_path))

    captured = {}

    def fake_open_sprint(self, board_id, name, days=14):
        captured["board_id"] = board_id
        captured["name"] = name
        captured["days"] = days
        return {"id": 42, "name": name}

    monkeypatch.setattr(jira_bootstrap.JiraAdmin, "open_sprint", fake_open_sprint)
    code = jira_bootstrap.main(["sprint-open", "--name", "Group 1", "--days", "7"])
    assert code == 0
    assert captured["days"] == 7


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
