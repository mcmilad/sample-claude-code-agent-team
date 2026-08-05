"""The claim primitive itself: exactly one winner, deterministically.

This is the property the whole coordination design rests on, and it is the one
Jira cannot provide. Verified against the live API: `editJiraIssue` accepts only
{cloudId, issueIdOrKey, fields, contentFormat, responseContentFormat} with
additionalProperties:false -- no version, no ETag, no `update` verb -- so a label
write is read-modify-write and a loser silently erases the winner. And every
transition is `isGlobal: true`, so moving an issue to In Progress never fails,
even from In Progress; both racers succeed and neither learns anything.

`mkdir` is the primitive that does work: POSIX requires it to fail if the path
exists, and the check-and-create is atomic. The agent-facing protocol is a shell
snippet rather than Python, so these tests exercise `mkdir` the same way the
agents do -- through the shell -- rather than asserting on os.mkdir semantics
that no agent ever calls.
"""
import concurrent.futures
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The snippet documented in .claude/skills/jira-workflow/SKILL.md, verbatim in
# behaviour: mkdir decides, and the loser is told who holds it.
CLAIM = r"""
set -u
CLAIMS="$1"; KEY="$2"; ME="$3"
mkdir -p "$CLAIMS"
if mkdir "$CLAIMS/$KEY" 2>/dev/null; then
  echo "$ME" > "$CLAIMS/$KEY/owner"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$CLAIMS/$KEY/heartbeat"
  echo "CLAIMED"
else
  echo "LOST -- owned by $(cat "$CLAIMS/$KEY/owner" 2>/dev/null || echo unknown)"
fi
"""


def claim(claims_dir, key, instance):
    proc = subprocess.run(["bash", "-c", CLAIM, "claim", str(claims_dir), key, instance],
                          capture_output=True, text=True)
    return proc.stdout.strip()


def test_exactly_one_of_two_racers_wins(tmp_path):
    claims = tmp_path / "claims" / "AGENT"
    results = [claim(claims, "AGENT-14", "coding-1"),
               claim(claims, "AGENT-14", "coding-2")]
    assert sum(r.startswith("CLAIMED") for r in results) == 1
    assert sum(r.startswith("LOST") for r in results) == 1


def test_a_twelve_way_race_still_produces_one_winner(tmp_path):
    """The lead spawns pools together, so claim attempts are phase-aligned --
    precisely the regime where a backoff-based scheme fails to decorrelate. The
    documented cap is twelve concurrent agents."""
    claims = tmp_path / "claims" / "AGENT"
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(
            lambda i: claim(claims, "AGENT-14", "coding-%d" % i), range(12)))
    winners = [r for r in results if r.startswith("CLAIMED")]
    assert len(winners) == 1, results
    assert len([r for r in results if r.startswith("LOST")]) == 11


def test_the_loser_learns_who_won_rather_than_inferring_it(tmp_path):
    """The failure mode being replaced was a loser that could not tell it had
    lost. The lock names the owner, so the outcome is observed, not deduced."""
    claims = tmp_path / "claims" / "AGENT"
    assert claim(claims, "AGENT-14", "coding-1") == "CLAIMED"
    assert claim(claims, "AGENT-14", "coding-2") == "LOST -- owned by coding-1"


def test_releasing_the_lock_makes_the_issue_claimable_again(tmp_path):
    """Recovery is respawn-and-reclaim; without a working release an orphaned
    issue is unreachable, since it is invisible to both the claim JQL and the
    idle work-check."""
    claims = tmp_path / "claims" / "AGENT"
    assert claim(claims, "AGENT-14", "coding-1") == "CLAIMED"
    subprocess.run(["rm", "-rf", str(claims / "AGENT-14")], check=True)
    assert claim(claims, "AGENT-14", "coding-7") == "CLAIMED"


def test_the_claim_records_a_heartbeat_for_the_stale_sweep(tmp_path):
    claims = tmp_path / "claims" / "AGENT"
    claim(claims, "AGENT-14", "coding-1")
    heartbeat = claims / "AGENT-14" / "heartbeat"
    assert heartbeat.is_file() and heartbeat.read_text().strip()


def test_distinct_issues_do_not_contend(tmp_path):
    claims = tmp_path / "claims" / "AGENT"
    assert claim(claims, "AGENT-14", "coding-1") == "CLAIMED"
    assert claim(claims, "AGENT-15", "coding-2") == "CLAIMED"


def test_the_documented_snippet_matches_what_is_tested():
    """Guards against the skill drifting away from the verified behaviour."""
    with open(os.path.join(REPO, ".claude", "skills", "jira-workflow",
                           "SKILL.md")) as fh:
        skill = fh.read()
    for token in ('mkdir "$CLAIMS/<ISSUE-KEY>" 2>/dev/null',
                  '/owner', '/heartbeat', 'LOST'):
        assert token in skill, "SKILL.md lost the claim snippet token: " + token
