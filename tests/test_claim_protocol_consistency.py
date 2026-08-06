"""The claim protocol is stated in five places. They must not drift.

Four are prose (an always-on rule, a skill, two agent system prompts) and the
fifth is generated at runtime by `teammate_idle_workcheck.py`, which injects the
whole recipe into a teammate's context at the exact moment it is about to claim.
That fifth copy is the highest-leverage one and was previously guarded by
nothing: a doc-only fix would ship alongside a hook still teaching the old,
broken protocol.

Four properties matter, and all are load-bearing:

  1. Every copy teaches the `mkdir` lock as the thing that decides ownership.
     Jira cannot arbitrate the race -- `editJiraIssue` has no compare-and-swap,
     and transitions are global so both racers' move to `In Progress` succeeds.

  2. No copy teaches the unreachable tie-break. `editJiraIssue` REPLACES the
     labels array, so a loser's write erases the winner's label and exactly one
     survives -- which means "if two agent-* labels are present" is a state the
     API can never produce, and any rule keyed on it never fires.

  3. Every copy takes the lock BEFORE it touches the board, and prescribes a
     `getJiraIssue` projection that includes `labels` -- an explicit `fields`
     list replaces the defaults, so a projection without it turns the claim's
     label write into a permanent erase of role-*/spec-*/group-*.

  4. Releases are `rm -rf`. The lock dir is never empty, so `rmdir` fails.

Each is asserted on the statement that carries it, not on a token that happens
to be nearby: this file is the only guard on five copies of a protocol, and an
assertion that a word is present survives a copy that says the opposite of it.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKILL = os.path.join(REPO, ".claude", "skills", "jira-workflow", "SKILL.md")
PROTOCOL = os.path.join(REPO, ".claude", "rules", "agent-team-protocol.md")
CODING = os.path.join(REPO, ".claude", "agents", "coding-agent.md")
DEVOPS = os.path.join(REPO, ".claude", "agents", "devops-agent.md")
IDLE_HOOK = os.path.join(REPO, ".claude", "hooks", "teammate_idle_workcheck.py")
CLAIM_GATE_HOOK = os.path.join(REPO, ".claude", "hooks", "claim_gate.py")

CLAIMING_DOCS = [SKILL, PROTOCOL, CODING, DEVOPS]
# Six copies, not five: claim_gate.py prints the recipe too, in the block message
# an agent reads at the exact moment it tried to edit unclaimed work.
ALL_COPIES = CLAIMING_DOCS + [IDLE_HOOK, CLAIM_GATE_HOOK]

# Phrasings of the tie-break that lost-update makes unreachable.
DEAD_TIEBREAK = re.compile(
    r"lowest instance name|more than one `?agent-\*|two `?agent-\* labels are present",
    re.I,
)

# The recipe is FOUR steps and every one of them is load-bearing. Token greps
# ("does the word mkdir appear?") pass against copies that are semantically
# inverted, so match the steps themselves, per line:
#
#   1. bootstrap the parent (`mkdir -p`) -- a bare `mkdir` of the issue directory
#      is ENOENT on a fresh $HOME, and every copy reads a failed `mkdir` as "you
#      lost". Omit this and the whole pool loses every race and the board starves.
#   2. the atomic `mkdir` of the issue directory -- the test-and-set itself.
#   3. `owner` -- so a loser is told who holds the lock instead of inferring it.
#   4. `heartbeat` -- the lead's stale-claim sweep is
#      `find ~/.claude/logs/claims/<projectKey> -name heartbeat -mmin +60`, which
#      matches the heartbeat FILE. A lock without one is invisible to the only
#      thing that recovers a dead owner's issue, so the sprint wedges silently.
#
# Per-line matching keeps a copy free to spell the path as `$CLAIMS` or in full,
# and survives the backslash-escaped quotes a Python string literal adds -- but
# it cannot survive a dropped or reordered step.
# `>\s+`, never `>\s*`. Every copy renders the issue directory as the literal
# `<ISSUE-KEY>`, whose closing angle bracket is immediately followed by `/owner`
# -- so `>\s*\S*owner` matched the LOSING branch's `$(cat ".../owner")` read and
# the owner WRITE went unpinned in five of the six copies. Requiring whitespace
# after the redirect distinguishes the write (`> "$CLAIMS/<ISSUE-KEY>/owner"`)
# from both that read and from `2>/dev/null`.
STEPS = (
    ("bootstrap the claims parent with `mkdir -p`", r"mkdir\s+-p\b"),
    ("take the lock with a bare, atomic `mkdir`", r"mkdir\s+(?!-p\b)\S"),
    ("record the lock's `owner`", r">\s+\S*owner"),
    ("record a `heartbeat` for the lead's sweep", r">\s+\S*heartbeat"),
)


def read(path):
    with open(path) as fh:
        return fh.read()


def prose(path):
    """Body with markdown emphasis stripped, so a phrase match is not defeated
    by someone bolding a word inside it."""
    return read(path).replace("*", "").replace("`", "")


def _mentions_claims(line):
    return "$CLAIMS" in line or "logs/claims/" in line


def _step_lines(path):
    """First line index of each recipe step in one copy; -1 when absent."""
    lines = read(path).splitlines()
    hits = []
    for _label, pattern in STEPS:
        rx = re.compile(pattern)
        hits.append(next(
            (i for i, line in enumerate(lines)
             if _mentions_claims(line) and rx.search(line)), -1))
    return hits


def flat(path):
    """One-line view of a copy: Python adjacent-string-literal seams closed and
    all whitespace collapsed. The same prescription is written three ways --
    wrapped across two markdown lines, inline in a rule, and split across two
    concatenated `"..."` chunks in the hook -- and a regex that only reads one
    of those notations is how the hook copy went unguarded. Markdown emphasis is
    stripped as in `prose`."""
    text = prose(path).replace('\\"', '"')
    text = re.sub(r'"\s*\n\s*"', "", text)
    return re.sub(r"\s+", " ", text)


# A *prescribed* getJiraIssue projection. Anchored to `getJiraIssue(` on purpose:
# the skill also shows `fields=["issuelinks"]` as the destructive counter-example,
# and a bare `fields=[...]` scan would flag that and force the warning to be deleted.
GET_ISSUE_PROJECTION = re.compile(r"getJiraIssue\s*\([^)\]]*?fields\s*=\s*\[([^\]]*)\]")

# The claim recipe's board step, in every notation the copies use:
# `transitionJiraIssue(transition.id = <To Do -> In Progress>)`,
# "`transitionJiraIssue` to **`In Progress`**", "transition to **In Progress**".
# Anchored to the `Claimed by` comment that follows it in all five copies, so
# that prose *about* the transition elsewhere in a file is not mistaken for the
# recipe step whose position is being asserted.
BOARD_STEP = re.compile(
    r"transition(?:JiraIssue)?(?:\.id)?[^.]{0,90}In Progress[^.]{0,90}Claimed by", re.I)

# Every copy has to say which step *decides* ownership, in so many words. The
# five copies put the sentence both ways round -- "ownership is decided by an
# atomic mkdir", "the lock decides" -- so both directions are accepted, but the
# subject of the decision must be the lock.
LOCK_DECIDES = re.compile(
    r"(?:lock|mkdir)[^.]{0,90}(?:decides|only step)"
    r"|(?:ownership|the claim)\s+is[^.]{0,60}(?:lock|mkdir)",
    re.I,
)


def test_every_copy_teaches_the_lock_as_the_claim():
    """Naming `mkdir` is not the same as teaching it. A copy that mentions the
    lock while presenting the transition as what reserves the issue teaches a
    protocol with no mutual exclusion, so the sentence naming the decider is
    itself asserted."""
    for path in ALL_COPIES:
        rel = os.path.relpath(path, REPO)
        body = read(path)
        assert "claims/" in body and "mkdir" in body, (
            "{} must teach the mkdir claim lock -- it is the only step that "
            "actually decides ownership".format(rel)
        )
        assert LOCK_DECIDES.search(flat(path)), (
            "{} names the mkdir lock but never says it is what decides "
            "ownership. Transitions are global and label writes are "
            "last-write-wins, so if the lock is not stated as the decider the "
            "copy is teaching a claim that cannot exclude anyone.".format(rel)
        )


def test_every_copy_teaches_all_four_steps_of_the_claim():
    """A copy that names the lock but drops a step is worse than no copy: it
    reads as authoritative and produces a lock nothing else can see."""
    for path in ALL_COPIES:
        hits = _step_lines(path)
        for (label, _pattern), hit in zip(STEPS, hits):
            assert hit >= 0, "{} never tells the agent to {}".format(
                os.path.relpath(path, REPO), label)


def test_no_copy_shows_the_atomic_mkdir_before_the_parent_bootstrap():
    """The ENOENT trap. `mkdir ~/.claude/logs/claims/<key>/<ISSUE>` fails on a
    fresh $HOME because the parent does not exist -- and every copy tells the
    agent that a failed `mkdir` means it lost the race. So a copy that omits or
    postpones `mkdir -p` makes EVERY agent lose EVERY race, silently."""
    for path in ALL_COPIES:
        bootstrap, lock = _step_lines(path)[:2]
        assert 0 <= bootstrap < lock, (
            "{}: the atomic mkdir appears at line {} but the `mkdir -p` of the "
            "parent is at line {} -- an agent following this in order hits "
            "ENOENT on a fresh $HOME and reads it as a lost race".format(
                os.path.relpath(path, REPO), lock + 1, bootstrap + 1))


def test_no_copy_writes_owner_or_heartbeat_outside_the_won_branch():
    """On a LOST race an unconditional `echo > owner` overwrites the true
    owner's record and re-stamps its heartbeat, destroying the one signal the
    stale-claim sweep reads. The write must sit inside the `if mkdir` branch."""
    for path in ALL_COPIES:
        rel = os.path.relpath(path, REPO)
        bootstrap, lock, owner, heartbeat = _step_lines(path)
        assert lock < owner and lock < heartbeat, (
            "{}: owner/heartbeat are written before the lock is won".format(rel))

        # Containment, not just ordering. Asserting `if mkdir` appears SOMEWHERE
        # passes a copy whose guard block is inert and whose writes sit after the
        # closing `fi` -- which is precisely the unconditional write this test is
        # named for. The writes must fall inside the won branch: after the `if`,
        # and before whatever ends it.
        lines = read(path).splitlines()
        guard = next((i for i, l in enumerate(lines) if re.search(r"if mkdir", l)), -1)
        assert guard != -1, (
            "{} must guard the owner/heartbeat writes with `if mkdir ...`; an "
            "unconditional write corrupts the winner's record on a lost "
            "race".format(rel))
        closer = next(
            (i for i, l in enumerate(lines)
             if i > guard and re.search(r"^\s*(?:else\b|fi\b)|\belse\b|;\s*fi\b", l)),
            len(lines))
        for label, idx in (("owner", owner), ("heartbeat", heartbeat)):
            assert guard < idx < closer, (
                "{}: the {} write (line {}) is outside the `if mkdir` branch "
                "(lines {}..{}). On a LOST race an unconditional write "
                "overwrites the true owner's record and re-stamps its "
                "heartbeat, destroying the one signal the stale-claim sweep "
                "reads.".format(rel, label, idx + 1, guard + 1, closer + 1))


def test_no_copy_teaches_the_unreachable_tiebreak():
    for path in ALL_COPIES:
        match = DEAD_TIEBREAK.search(prose(path))
        assert not match, (
            "{} still teaches the '{}' tie-break. editJiraIssue replaces the "
            "labels array, so two agent-* labels never coexist and this rule "
            "can never fire.".format(os.path.relpath(path, REPO), match.group(0))
        )


def test_every_claiming_copy_requires_the_in_progress_transition():
    """The reported symptom: 8 of 11 worked issues never entered In Progress, so
    the lead's monitor JQL reported in-flight work as unstarted."""
    for path in CLAIMING_DOCS:
        assert "In Progress" in read(path), (
            "{} must require the In Progress transition before any file is "
            "edited".format(os.path.relpath(path, REPO))
        )


def test_the_runtime_nudge_agrees_with_the_skill():
    """The hook's injected recipe is a real copy of the protocol, not a summary."""
    hook = read(IDLE_HOOK)
    for token in ("~/.claude/logs/claims/", "In Progress", "issuelinks"):
        assert token in hook, (
            "the idle nudge must carry '{}' -- it is delivered at the moment of "
            "claiming and is the copy an agent is most likely to act on".format(token)
        )


def test_the_explicit_fields_projection_warns_that_it_replaces_defaults():
    """getJiraIssue's `fields` REPLACES the default set. A projection that omits
    `labels` makes the claim's read-modify-write erase role-*/spec-*/group-*.

    Asserted on the contents of the `[...]` bracket, not on the words around it:
    a copy can carry `issuelinks` and the whole warning paragraph while the
    prescription itself says `fields=["issuelinks"]`."""
    for path in (SKILL, PROTOCOL, IDLE_HOOK):
        rel = os.path.relpath(path, REPO)
        text = flat(path)
        projections = GET_ISSUE_PROJECTION.findall(text)
        assert projections, (
            "{} must prescribe an explicit getJiraIssue(fields=[...]) -- that "
            "read is what the claim's label write is built on".format(rel)
        )
        for fields in projections:
            assert '"labels"' in fields, (
                "{} prescribes getJiraIssue(fields=[{}]), which omits `labels`. "
                "The explicit list replaces the defaults, so the claim's label "
                "write would erase role-*/spec-*/group-* permanently.".format(
                    rel, fields)
            )
            assert '"issuelinks"' in fields, (
                "{} prescribes getJiraIssue(fields=[{}]), which omits "
                "`issuelinks` -- the blocker check then reads nothing and every "
                "issue looks unblocked".format(rel, fields)
            )
        assert re.search(r"replaces? the default", text, re.I), (
            "{} prescribes an explicit fields list but never warns that it "
            "replaces the defaults".format(rel)
        )


def test_the_lock_is_taken_before_the_board_is_touched():
    """Order is the design, not a style choice. `mkdir` decides ownership;
    the label and the transition only mirror a decision already made. Because
    transitions are global, both racers' move to `In Progress` succeeds -- so a
    copy that stakes the claim by transitioning first teaches a protocol with no
    mutual exclusion in it at all."""
    for path in ALL_COPIES:
        rel = os.path.relpath(path, REPO)
        text = flat(path)
        lock = re.search(r"logs/claims", text)
        board = BOARD_STEP.search(text)
        assert lock, "{} must name the ~/.claude/logs/claims lock".format(rel)
        assert board, (
            "{} must carry the claim recipe's board step -- transition to In "
            "Progress, then the 'Claimed by <instance>' comment".format(rel)
        )
        assert lock.start() < board.start(), (
            "{} reaches the board step ('{}') before the claims lock. The "
            "transition cannot be the claim -- it is global and succeeds for "
            "both racers.".format(rel, text[board.start():board.start() + 60])
        )


def test_the_lock_release_is_recursive_not_rmdir():
    """Step 2 writes `owner` and `heartbeat` INSIDE the lock directory, so the
    directory is never empty and `rmdir` always fails. A release that silently
    fails leaves the issue held forever: invisible to the claim JQL (wrong
    status) and to the idle check."""
    for path in ALL_COPIES:
        rel = os.path.relpath(path, REPO)
        text = flat(path)
        assert not re.search(r"rmdir\s+[\"'$~]", text), (
            "{} releases a lock with `rmdir`, which fails on a non-empty "
            "directory and silently orphans the issue".format(rel)
        )
        for match in re.finditer(r"rmdir", text):
            window = text[max(0, match.start() - 80):match.end() + 80]
            assert re.search(r"\bnot\b|\bnever\b|\bfails\b|\bcannot\b", window, re.I), (
                "{} mentions `rmdir` at '...{}...' without saying it fails. The "
                "lock dir holds `owner` and `heartbeat`; releases are `rm -rf`."
                .format(rel, window)
            )
    assert re.search(r"rm -rf\s+~/\.claude/logs/claims/", flat(SKILL)), \
        "the skill must show the release as `rm -rf ~/.claude/logs/claims/...`"


def test_confirm_step_is_fail_open():
    """An absent label with no competitor is an unconfirmed write, not a loss.
    Failing closed there turns a stale read into a self-inflicted orphan."""
    for path in (SKILL, PROTOCOL, CODING, DEVOPS):
        assert re.search(r"unconfirmed|fail open", prose(path), re.I), (
            "{} must say that an absent label with no competing agent-* is "
            "unconfirmed rather than lost".format(os.path.relpath(path, REPO))
        )


def test_reviewers_are_told_not_to_self_claim():
    """There is one role-review card per sprint and it belongs to the
    synthesizer; a reviewer pool that self-claims manufactures the duplicate
    verdict the one-synthesizer rule exists to prevent.

    Headings are stripped first: `## Reviewers Do Not Self-Claim` is a label,
    not an instruction, and it survives a body that says the opposite."""
    body = prose(os.path.join(REPO, ".claude", "agents", "review-agent.md"))
    body = re.sub(r"(?m)^#+ .*$", "", body)
    body = re.sub(r"\s+", " ", body)
    assert re.search(r"do not\s+(?:self-claim|run the role JQL)", body, re.I), (
        ".claude/agents/review-agent.md has no directive telling a reviewer not "
        "to self-claim the sprint's role-review card -- a section heading alone "
        "is not one"
    )


LEAD = os.path.join(REPO, ".claude", "agents", "fullstack-agent.md")


def test_the_release_path_exists():
    """Without an explicit release, an issue orphaned at In Progress is
    unreachable: it is invisible to the claim JQL and to the idle check.

    A `/releas/i` grep is not a guard -- the word survives every inversion of the
    procedure. The three steps that make an orphan reclaimable are asserted
    individually, because omitting any one of them leaves it stuck: the lock
    keeps the key un-retakeable, the status keeps it out of the claim JQL, and
    the stale label makes a same-named respawn look like the owner.
    """
    for path in (SKILL, LEAD):
        body = prose(path)
        rel = os.path.relpath(path, REPO)
        assert re.search(r"rm -rf\s+~?/?\.?claude/logs/claims|logs/claims/", body), (
            "{}: the release must delete the claim lock, or the key can never "
            "be retaken (mkdir keeps returning EEXIST)".format(rel))
        assert re.search(r"remov\w*|minus|strip\w*", body, re.I) and "agent-" in body, (
            "{}: the release must strip the dead agent-* label".format(rel))
        assert re.search(r"To Do", body), (
            "{}: the release must transition back to To Do -- the claim JQL "
            "only returns that status".format(rel))


def test_the_stale_sweep_is_investigate_only_and_clears_the_incident_threshold():
    """The two highest-value properties of the sweep, and neither was guarded.

    The threshold exists because the incident that produced the liveness rule
    involved a legitimate ~29-minute verification pass; a threshold near that
    length is a coin flip on the exact run the rule was written for. And the
    posture matters more than the number: a stale heartbeat is the same object
    as "no message in N minutes", which the lead's own non-negotiable rule names
    as NOT positive evidence of death. Inverting either left the suite green.
    """
    body = prose(LEAD)

    minutes = [int(m) for m in re.findall(r"-mmin \+(\d+)", body)]
    assert minutes, "the lead must document the stale-claim sweep's find command"
    assert all(m >= 60 for m in minutes), (
        "stale-sweep threshold {} is at or below the ~29-minute legitimate "
        "verification pass that motivated the liveness rule; 60 is the "
        "documented floor".format(minutes))

    assert re.search(r"never evidence of death|reason to look", body, re.I), (
        "fullstack-agent.md must state that a stale heartbeat is a reason to "
        "INVESTIGATE and never evidence of death -- inverting that posture "
        "re-authorises the takeover the liveness rule forbids")
    assert re.search(r"only on positive evidence|positive evidence of death", body, re.I), (
        "the release must be gated on positive evidence, not on the sweep alone")
