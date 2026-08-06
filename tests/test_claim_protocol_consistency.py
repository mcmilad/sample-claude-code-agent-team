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
        rel = os.path.relpath(path, REPO)

        # Scoped to the release BLOCK, never the whole file. Matching the file
        # made every one of these assertions inert: `rm -rf ~/.claude/logs/claims`
        # also appears in the teardown step, and "To Do", "remov" and "agent-"
        # appear throughout. Deleting all three release mechanics left the suite
        # green -- the exact "a word is present" failure this file exists to stop.
        body = prose(path)
        start = body.find("logged release")
        assert start != -1, (
            "{}: no release block found -- it must be introduced by the phrase "
            "'logged release', which is what anchors these assertions".format(rel))
        # The block ends at the prose that follows its fenced commands.
        end = body.find("Without the release", start)
        block = body[start:end if end != -1 else start + 900]

        # Commented-out lines are not instructions. `# never run: rm -rf ...`
        # satisfied a bare search, so a disabled command read as a live one.
        live = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))

        for pattern, why in (
            (r"rm -rf\s+\S*logs/claims/",
             "delete the claim lock, or the key can never be retaken -- mkdir "
             "keeps returning EEXIST"),
            # `prose()` strips '*', so an `agent-\*` alternative here could never
            # match and was dead code. Matched against the stripped form.
            (r"labels\s*=\s*<labels minus|remov\w+\s+.{0,40}agent-",
             "strip the dead agent-* label, or a same-named respawn passes every "
             "ownership check"),
            (r"transitionJiraIssue.{0,60}To Do|back to To Do",
             "transition back to To Do -- the claim JQL only returns that status, "
             "so without it the issue stays unreachable"),
            # Acceptance names FOUR mechanics. The comment is the audit trail:
            # a release with no recorded evidence is indistinguishable from the
            # takeover the liveness rule forbids.
            (r"addCommentToJiraIssue\(.{0,30}Released from",
             "comment the evidence -- an unrecorded release is indistinguishable "
             "from an unauthorised takeover"),
        ):
            assert re.search(pattern, live), (
                "{}: the release must {}".format(rel, why))

        # A guard that accepts the INVERTED instruction is worse than none:
        # "Do NOT transition it back to To Do" satisfied the clause above,
        # because these assertions only check that a step is PRESENT.
        #
        # Line-scoped, and only for lines that actually carry a mechanic. A
        # block-wide search spans the fenced commands and fires on the wholly
        # legitimate "Never take the work over yourself" -- which is an
        # instruction the liveness rule requires, not a negated release step.
        NEGATED = re.compile(r"\b(?:do not|don't|never|must not|no longer)\b", re.I)
        MECHANIC = re.compile(
            r"rm -rf\s+\S*logs/claims/|transitionJiraIssue|back to To Do|"
            r"labels\s*=\s*<labels minus|addCommentToJiraIssue", re.I)
        for line in live.splitlines():
            if MECHANIC.search(line) and NEGATED.search(line):
                raise AssertionError(
                    "{}: a release mechanic is stated in the NEGATIVE -- {!r}. "
                    "The presence assertions above would still pass, so an "
                    "inverted procedure would ship unnoticed.".format(
                        rel, line.strip()[:90]))


# The sweep is stated in TWO copies, not one. SKILL.md:160-167 restates the
# threshold, the posture, and the release gate, and every assertion here used to
# read only the lead -- dropping the skill's copy to 25 minutes left all 310
# tests green while leaving that file self-contradictory ("older than 25 minutes
# (well clear of the ~29-minute legitimate verification pass...)").
SWEEP_DOCS = (LEAD, SKILL)

# The threshold in both notations the copies use: the lead's `find ... -mmin +60`
# and the skill's prose "older than 60 minutes".
SWEEP_THRESHOLD = re.compile(r"-mmin \+(\d+)|older than (\d+) minutes", re.I)

# Polarity bound to its subject, and matched over `flat`, not `prose`.
#
# `never evidence of death|reason to look` was an alternation in which the second
# half survives every inversion: rewriting the lead to "a reason to look, and firm
# evidence of death" kept the suite green. Inversion is the likelier drift, too --
# an editor rewriting the clause, not deleting the sentence -- and it reads as
# authorization for exactly the takeover this section forbids.
#
# `flat` is required rather than cosmetic: SKILL.md wraps this sentence across two
# lines, so `prose` (which strips emphasis but not newlines) cannot see it at all,
# and the same reflow applied to the lead turned the old assertion red on a change
# that altered no words.
SWEEP_POSTURE = re.compile(
    r"stale heartbeat[^.]{0,120}?\bnever\s+(?:positive\s+)?evidence of death", re.I)

# The release gate. NOT load-bearing alone: this phrase survives a doc that
# redefines the sweep hit AS positive evidence ("...and the sweep hit IS that
# evidence"). What rejects that inversion is the posture above plus the takeover
# prohibition below, so all three are asserted, not just this one.
SWEEP_GATE = re.compile(r"only on positive evidence of death", re.I)

# The operative prohibition behind G3, and nothing asserted it: flipping
# `Never take the work over yourself` to `Take the work over yourself` left all
# 310 tests green. Lead-only -- the skill hands the release to the lead, so only
# the lead is told not to do the work itself.
SWEEP_NO_TAKEOVER = re.compile(r"never take the work over yourself", re.I)


def test_the_stale_sweep_is_investigate_only_and_clears_the_incident_threshold():
    """Four properties of the sweep, across both copies that state them.

    The threshold exists because the incident that produced the liveness rule
    involved a legitimate ~29-minute verification pass; a threshold near that
    length is a coin flip on the exact run the rule was written for. The posture
    matters more than the number: a stale heartbeat is the same object as "no
    message in N minutes", which the lead's own non-negotiable rule names as NOT
    positive evidence of death.

    Every one of these inversions left the suite green before this test: the
    posture flipped to "firm evidence of death", the takeover prohibition flipped
    to "Take the work over yourself", and the skill's threshold dropped to 25
    minutes. Presence checks accept the negation of the thing they name, so each
    assertion below binds the polarity rather than the keyword.
    """
    # Positive control on the loop itself, before it runs. Emptying SWEEP_DOCS
    # makes every assertion below evaporate with the test still reporting PASS
    # and the suite count UNCHANGED -- no skip marker, no summary line, nothing
    # to notice. Measured: it survived the first mutation pass of this very fix.
    assert LEAD in SWEEP_DOCS and SKILL in SWEEP_DOCS, (
        "SWEEP_DOCS must carry both copies that state the sweep. A copy dropped "
        "from it is a copy no longer checked, and nothing else here would fail.")

    for path in SWEEP_DOCS:
        rel = os.path.relpath(path, REPO)
        body = flat(path)

        # Positive control on the detector itself: a doc that states no
        # threshold at all must fail here rather than pass an empty `all()`.
        minutes = [int(a or b) for a, b in SWEEP_THRESHOLD.findall(body)]
        assert minutes, (
            "{} states no stale-sweep threshold -- it must carry either "
            "`-mmin +<n>` or `older than <n> minutes`. An absent threshold "
            "makes the `all()` below vacuously true.".format(rel))
        assert all(m >= 60 for m in minutes), (
            "{}: stale-sweep threshold {} is at or below the ~29-minute "
            "legitimate verification pass that motivated the liveness rule; "
            "60 is the documented floor".format(rel, minutes))

        assert SWEEP_POSTURE.search(body), (
            "{} must bind the negation to its subject -- 'a stale heartbeat "
            "is ... never evidence of death'. A loose presence check passes "
            "the INVERTED sentence, which re-authorises the takeover the "
            "liveness rule forbids.".format(rel))
        assert SWEEP_GATE.search(body), (
            "{}: the release must be gated on positive evidence of death, "
            "never on the sweep hit alone".format(rel))

    assert SWEEP_NO_TAKEOVER.search(flat(LEAD)), (
        "fullstack-agent.md must carry 'Never take the work over yourself'. It "
        "is the operative prohibition behind G3 -- the release procedure exists "
        "to hand the issue to a fresh instance, and without the prohibition the "
        "same section reads as authorization to do the work yourself.")
