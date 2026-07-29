# Optimize Claude Code Configuration

Audits and optimizes the entire `~/.claude` configuration — settings, agents, rules, skills, plugins, and MCP servers — for the current Claude model and latest Claude Code features.

## Input

Focus area (optional): $ARGUMENTS

If a focus area is provided (e.g., "settings", "agents", "rules", "plugins", "mcp"), only audit and optimize that area. Otherwise, run the full audit.

## Standing Constraints

These hold on **every** run, regardless of focus area:

- **The model lineup is Opus 5 / Sonnet 5 / Haiku 4.5.** The `opus` / `sonnet` / `haiku` aliases resolve to **Opus 5** (`claude-opus-5`), **Sonnet 5** (`claude-sonnet-5`), and **Haiku 4.5** (`claude-haiku-4-5`, full ID `claude-haiku-4-5-20251001`). Rank them by **tier, not by version number**: Opus 5 > Sonnet 5 > Haiku 4.5. Opus and Sonnet are both 5-series; Haiku's `4.5` is the current fast tier, not a superseded one.
- **Never recommend Claude Fable 5 (`claude-fable-5`).** Anthropic prices it above Opus tier ($10/$50 per MTok vs Opus 5's $5/$25) — do **not** surface it as a main-loop upgrade, a per-agent `model:` value, a settings key, a roster entry, or a "feature to consider later." The same exclusion covers `claude-mythos-5` (Project Glasswing) and `claude-mythos-preview`. If a docs page or the research agent proposes Fable 5, drop it silently rather than reporting it as a finding.
- **Do not "reconcile" per-agent `effort:` tiers in either direction.** The per-role spread is deliberate: depth is allocated by **blast radius and irreversibility**, not by pool size or uniformity. A role sitting at `high` while a peer sits at `max` is the intended state, not drift to be levelled up or a budget cut to be levelled down — see the settings.json checks below.

## Process

### Phase 1: Audit Current State

1. **Inventory all configuration files** — read every file under `~/.claude/` that matters. Issue all reads in a single parallel batch (Opus 5 handles wide tool fan-out efficiently and has a 1M-token context window; sequential reads still waste turns):
   - `~/CLAUDE.md`
   - `~/.claude/settings.json` and `~/.claude/settings.local.json`
   - `~/.claude/agents/*.md` (all agent definitions)
   - `~/.claude/rules/*.md` (all rules files)
   - `~/.claude/skills/*/SKILL.md` (all custom skills)
   - `~/.claude/commands/*.md` (all custom commands)
   - `~/.claude/plugins/installed_plugins.json` and `~/.claude/plugins/blocklist.json`
   - MCP server config from `~/.claude.json` (extract `mcpServers` and project-level `mcpServers`)

2. **Record the current state** — note file sizes, line counts, and key settings for before/after comparison.

### Phase 2: Research Current Best Practices

3. **Research latest Claude Code features** — use the `claude-code-guide` agent to research. Anchor the research on the **active main-loop model — Opus 5 (`claude-opus-5`)** — 1M-token context window, 128K max output, full `low`…`max` effort ladder. The current lineup is **Opus 5** (flagship / main loop), **Sonnet 5** (`claude-sonnet-5` — the mid subagent tier), and **Haiku 4.5** (`claude-haiku-4-5` — the fast tier), reached via the `opus` / `sonnet` / `haiku` aliases. **Rank by tier, never by version number: Opus 5 > Sonnet 5 > Haiku 4.5.** Opus 5 and Sonnet 5 now share the Claude 5 family, so the old "Sonnet 5 vs Opus 4.8" version-number trap is gone — but the tier rule still binds: never promote Sonnet 5 into the main loop over Opus 5, and don't read Haiku 4.5's `4.5` as stale. **Exclude Fable 5 from all research and recommendations** (see Standing Constraints). Flag any recommendation that assumes a superseded model — e.g. Opus 4.8-era framing where omitting `thinking` meant *no* thinking (on Opus 5, thinking is **on by default**), or manual extended-thinking `budget_tokens`, which Opus 5 rejects with a 400 in favor of adaptive thinking plus the `effort` knob.

   **Verify claims against the CLI binary, not just docs.** Research agents confidently mis-report env-var behavior. The shipped CLI is a compiled binary (`~/.local/share/claude/versions/<version>` — Mach-O on macOS), so there is no JS bundle to grep. Dump it once and search the dump:

   ```bash
   strings -n 6 ~/.local/share/claude/versions/<version> > /tmp/cc_probe.txt
   grep -c 'ANTHROPIC_DEFAULT_OPUS_MODEL' /tmp/cc_probe.txt   # presence check
   ```

   For context around a hit, use a small Python script with `str.find` and a fixed window. Do **not** use wide-window regex (`grep -oE '.{0,220}FOO.{0,220}'`) — on a ~400k-line dump that exceeds ugrep's complexity limits and times out. The binary's env-var registry is the ground truth for whether a variable is read at all.

   **Token-accounting is source-agnostic:** wherever this command reasons about "cost," the real constraint is **token-budget headroom**, independent of how those tokens are consumed — billed per-token via Amazon Bedrock / Google Vertex / the Anthropic API, or counted against an Anthropic subscription plan's rate limits. Published per-token prices ($/MTok) are useful only as *relative weights* between models (Opus > Sonnet > Haiku). Determine the active backend before reasoning about cost: check `settings.json` for `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX`, the model-ID style (`us.anthropic.claude-*` ⇒ Bedrock inference profile; `claude-*@<date>` ⇒ Vertex; bare `claude-*` ⇒ Anthropic API / plan), and caching env vars (`ENABLE_PROMPT_CACHING_1H_BEDROCK` ⇒ Bedrock). Cover:
   - Opus 5 capabilities and recommended settings (prompt caching — the minimum cacheable prefix drops to **512 tokens** on Opus 5, down from 1024 on Opus 4.8, so prompts previously written off as uncacheable now cache; effort ladder through `max`; adaptive thinking on by default; parallel tool calls; 1M-context headroom)
   - New or changed environment variables
   - New hook events, skill features, or agent team capabilities
   - Deprecated settings or features (including legacy model IDs now superseded: Opus `claude-opus-4-8` / `claude-opus-4-7` / `claude-opus-4-6` / `claude-opus-4-5` — the current Opus is Opus 5, `claude-opus-5`; and Sonnet `claude-sonnet-4-6` / `claude-sonnet-4-5` — the current Sonnet is Sonnet 5, `claude-sonnet-5`. **Haiku 4.5 is not superseded** — it is still the current fast tier, so don't flag it as legacy)
   - Token optimization best practices specific to Opus-tier models (caching discipline conserves token-budget headroom on every backend, even with 1M context). Note that Opus 5 draws on a **separate rate-limit bucket** from the combined Opus 4.x pool — a lineup move does not inherit the old bucket's headroom
   - New plugins or MCP servers worth adopting

4. **Compare current config against best practices** — identify:
   - Deprecated env vars still in use, and **env vars that were never recognized at all** (a plausible-looking key that the binary does not read is a silent no-op, not a safe fallback)
   - Suboptimal defaults (effort level, model assignments, caching)
   - Redundant content across files (rules duplicating CLAUDE.md, agents duplicating protocol)
   - Stale permissions in `settings.local.json`
   - Broken references — a documented skill/command/agent that the installed plugin does not actually ship (verify against the plugin cache, don't trust the docs)
   - Missing new features that would benefit the user's workflow
   - Context bloat (oversized rules, too many unconditional rules, MCP tool overhead)

### Phase 3: Plan Changes

5. **Present findings to user** — organize into categories:

   ```
   ## Findings

   ### High Impact (cost/performance)
   - <finding> — <recommendation>

   ### Medium Impact (features/quality)
   - <finding> — <recommendation>

   ### Low Impact (cleanup)
   - <finding> — <recommendation>

   ### No Changes Needed
   - <area> — already optimal because <reason>
   ```

6. **Get user approval** — ask the user which findings to act on before making changes. Do NOT make changes without confirmation.

### Phase 4: Implement

7. **Apply approved changes** — for each approved change:
   - Read the file first
   - Make the minimal edit needed
   - Verify the edit is correct

8. **Validate cross-references** — after all edits:
   - Verify every path referenced in CLAUDE.md exists
   - Verify agent model frontmatter matches the CLAUDE.md agent roster
   - Verify rules files referenced by agents and skills exist
   - Verify hook script paths in `settings.json` resolve
   - Verify no broken cross-references between files
   - Verify no file mentions Fable 5 / Mythos as an option (Standing Constraints)
   - Beware of tilde expansion in your own validation loops — an unexpanded `~/` produces false "missing file" reports

### Phase 5: Report

9. **Summarize changes** — present a before/after table:

   ```
   ## Changes Applied

   | File | Change | Reason |
   |------|--------|--------|
   | ... | ... | ... |

   ## Features to Consider Later
   - <feature> — <when it would be useful>
   ```

   Report any item you could not verify as **explicitly unverified** rather than silently omitting it or presenting an inference as a finding.

10. **Update memory** — save a memory entry recording what was changed and why, so future runs of this command can track drift over time. Record verified-clean areas too, so the next run doesn't "fix" something that is already correct.

## Key Checks by Area

### settings.json
- Deprecated env vars (`ANTHROPIC_SMALL_FAST_MODEL`, etc.)
- **`ANTHROPIC_DEFAULT_MODEL` is not a recognized variable** — verified absent from the CLI 2.1.220 env-var registry (which does contain `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, and `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`). If present it is a silent no-op, **not** a generic fallback beneath the role-specific vars. The main-loop override is `ANTHROPIC_MODEL`; the `model:` key plus `ANTHROPIC_DEFAULT_<TIER>_MODEL` is the supported path. Flag it as dead weight rather than assuming belt-and-braces
- Prompt caching enabled (no `DISABLE_PROMPT_CACHING=1` unless justified) — caching ROI is highest at Opus usage weight; on Bedrock confirm `ENABLE_PROMPT_CACHING_1H_BEDROCK` (first-party uses `ENABLE_PROMPT_CACHING_1H`). Opus 5 lowers the minimum cacheable prefix to **512 tokens** (Opus 4.8 required 1024), so short system prompts, rules, and skill preambles that never cached before now can — re-check anything previously dismissed as too small to cache
- Effort level strategy. The valid ladder is `low` < `medium` < `high` < `xhigh` < `max`; Opus 5 honors the effort knob strictly, and unlike earlier Opus releases its `low`/`medium` tiers are unusually strong — a step *down* is a legitimate cost lever on routine routes, but only where explicitly asked for. **This setup is tuned capability-first, and allocates depth by blast radius and irreversibility rather than uniformly**: read the actual values from each agent's frontmatter rather than trusting a list here (at last audit: `review` `max`; `fullstack` and `devops` `xhigh`; `coding` and `sa` `high`). The rationale, so an audit does not "fix" it: `review` holds sole PASS/FAIL authority and both error directions are expensive; `devops` runs live cloud credentials where mistakes are billable and hard to reverse, and its documented bug classes are invisible to static gates; `coding` executes fully-specified tasks behind an adversarial review gate, where extra deliberation mostly buys scope drift — a real defect vector in a shared tree; `sa` is bounded by MCP grounding, not deliberation, since no amount of thinking makes a remembered pricing figure correct. Treat these as the **intended state**: do NOT flag them as over-budget, step them down to `medium`, nor level the lower tiers up for consistency — only revisit if explicitly asked. The global main-loop default is separate from these per-agent tiers
- Subagent model strategy (per-agent frontmatter vs global override). With Opus 5 as the main-loop model, route focused/narrow work to Sonnet 5 or Haiku 4.5 subagents rather than inheriting Opus 5 — the win (lower token weight + latency) holds on every backend. Sonnet 5 is a meaningful capability step up from the Sonnet 4.6 these agents were originally tuned against, which *raises* the bar for reserving `opus`. This is a **model** choice, separate from effort: the Sonnet teammates deliberately run at elevated effort per the capability-first preference, so do not "correct" their effort downward. Flag any teammate still pinned to `opus` whose task doesn't actually need Opus-tier reasoning — but note that `review` and `sa` are **deliberate** `opus` pins (adversarial gate authority; recall-and-severity judgment over a six-pillar model), not leaks
- `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` **does** exist in the CLI env-var registry, but its semantics and default are undocumented. Do not set it speculatively; do not report it as nonexistent either
- MCP tool search threshold (`ENABLE_TOOL_SEARCH` / `MAX_MCP_OUTPUT_TOKENS`) — with the current server footprint (the AWS plugin suite — deploy-on-aws / aws-serverless / databases-on-aws / aws-amplify / aws-core / aws-agents / aws-data-analytics — plus context7), tool-search deferral is effectively mandatory to keep the system prompt compact. The CLI validates the value as `auto:N where N is a number`, so `auto:5` parses; N's exact semantics are undocumented, so don't tune the number blind
- Model IDs are current for the active backend — main loop on Opus 5 (Bedrock inference profile `us.anthropic.claude-opus-5`; Vertex `claude-opus-5@<date>`; Anthropic API `claude-opus-5`). Agent frontmatter uses the `opus` / `sonnet` / `haiku` aliases, resolving to **Opus 5**, **Sonnet 5** (`claude-sonnet-5`), and **Haiku 4.5** (`claude-haiku-4-5-20251001`). Verify no legacy IDs linger as the main loop in settings, agent frontmatter, or CLAUDE.md — superseded Opus `claude-opus-4-8` / `-4-7` / `-4-6` / `-4-5` and superseded Sonnet `claude-sonnet-4-6` / `-4-5`. Opus and Sonnet are both 5-series now, so the two aliases move together; Haiku stays on 4.5 and is **not** a legacy ID. Remember the backend wrapper: a bare `claude-opus-5` / `claude-sonnet-5` on Bedrock/Vertex still needs the backend-appropriate form, and Haiku on Bedrock carries the versioned suffix (`us.anthropic.claude-haiku-4-5-20251001-v1:0`). Confirm any Bedrock profile is `ACTIVE` in the configured region with `aws bedrock list-inference-profiles --no-cli-pager`
- **No Fable 5 / Mythos model IDs anywhere** — `claude-fable-5`, `claude-mythos-5`, and `claude-mythos-preview` must not appear in `settings.json`, `settings.local.json`, agent frontmatter, CLAUDE.md, rules, skills, or commands. Flag any occurrence for removal
- **Inference backend matches the account.** Confirm `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` (or their absence) is consistent with the model-ID style in use — a mismatch silently routes to the wrong or unavailable backend
- **`/fast` is first-party-API only.** It speeds up Opus output without downgrading to a smaller model, and works on Opus 5/4.8/4.7 — but the CLI gates it on backend *identity*, not model: the check is `!== "firstParty"`, yielding status `not_first_party` and the message *"Fast mode is only available when using the Anthropic API directly."* On a Bedrock, Vertex, or Foundry route it will refuse, and no model choice or flag works around it (the underlying beta `fast-mode-2026-02-01` / `speed: "fast"` is not offered on those platforms). Flag any doc claiming `/fast` works on Bedrock, and any doc restricting it to older Opus versions. Other refusal states worth knowing: `disabled_by_env`, `model_not_allowed`, `sdk_opt_in_required`, `extra_usage_disabled`
- **Never edit `settings.json` casually.** The `hooks` key has silently vanished from this file on prior writes. After any change, re-read the file and confirm `hooks`, `enabledPlugins`, and `extraKnownMarketplaces` all survived

### Agents
- Model assignments appropriate for the Opus-5 era:
  - `opus` (Opus 5) — team lead, cross-file reasoning, review-agent, sa-agent, code-architect roles; benefits most from the 1M context on large-repo work. Runs elevated effort (`review` `max`, `fullstack` `xhigh`, `sa` `high`) per the capability-first preference
  - `sonnet` (Sonnet 5, `claude-sonnet-5`) — `[coding]` and `[devops]` teammates with well-scoped tasks, Explore agent; runs elevated effort (`devops` `xhigh`, `coding` `high`), not stepped down. The Claude 5-family Sonnet is a capability step up from the 4.6 these roles were tuned against — it comfortably absorbs work that used to lean toward Opus, so prefer it over an `opus` pin for well-scoped subagent tasks
  - `haiku` (4.5) — narrow lookups, mechanical transforms, status-line helpers, quick classifiers
  - Any `opus` pin on a teammate that does focused single-file work is likely a token-budget leak — call it out
  - No agent may be pinned to Fable 5 or Mythos (Standing Constraints)
- Frontmatter sets `effort:` deliberately per the capability-first preference — allocated by blast radius, not uniformly (`max` on `review`, `xhigh` on `fullstack`/`devops`, `high` on `coding`/`sa`). **Each agent's frontmatter is the source of truth** if any doc drifts from it; fix the doc, not the frontmatter
- No duplicated content that belongs in `agent-team-protocol.md`
- Frontmatter uses current features (model, effort, isolation, memory)
- Cross-references to rules and specs are valid
- Teammates that dispatch many parallel tool calls benefit from Opus 5's wide tool fan-out and large context; keep their prompts from over-sequencing work
- **Delegation appetite changed direction on Opus 5.** Where Opus 4.8 *under*-reached for subagents and needed prompting to delegate, Opus 5 reaches for them readily. Since `fullstack-agent` spawns pools of up to 6 `coding`, 2 `devops`, and 4 `review` instances, check the lead's prompt for leftover "delegate more" encouragement written for the 4.8 era, and confirm the "size to the parallel width of the task graph / don't spawn idle workers" guidance is still doing the capping work
- **Opus 5 self-verifies without being told.** Prompt-level *"double-check your work"* / *"re-verify before responding"* scaffolding is now redundant and can cause over-verification — flag such phrasing for removal. This does **not** apply to the machine-enforced verification gate (the `TaskCompleted` hook + sentinel attestation in `agent-team-protocol.md`): that is deliberate infrastructure that exists because the hook cannot see the transcript. Never recommend weakening or removing it
- **Opus 5 can expand task scope and narrate self-corrections at length.** If agent output shows unrequested extra work or verbose "actually I was wrong earlier" passages, the fix is a short scope-discipline / corrections instruction in the agent prompt, not a model or effort change

### Rules
- No content duplicated between rules files and CLAUDE.md
- No content duplicated between rules files and agent definitions
- Large rules consider whether content could move to on-demand skills
- Rules that only apply to specific file types use `paths` frontmatter

### Skills
- Skills use SKILL.md format (not legacy commands format)
- Skills reference current MCP servers and plugins
- Agent integration sections reference current agent roster
- Any skill that names a model uses the current lineup (Opus 5 / Sonnet 5 / Haiku 4.5) and does not offer Fable 5

### Plugins
- No deprecated plugins enabled
- **Every documented plugin capability actually exists.** Plugins ship skills, agents, and commands — these are *not* interchangeable, and docs drift. Verify against the plugin cache before trusting a reference:
  ```bash
  find ~/.claude/plugins -path '*<plugin>*' \( -name 'SKILL.md' -o -path '*/agents/*' -o -path '*/commands/*' \) | grep -v node_modules
  ```
  A capability documented as a skill that ships only as an agent cannot be invoked with the `Skill` tool — it needs the `Agent` tool, and the wrong instruction fails at use time
- Enabled set matches the installed set (no entries for uninstalled plugins, no installed-but-disabled surprises)
- Plugin count reasonable (each adds context overhead)

### MCP Servers
- No deprecated servers (e.g., `awslabs.aws-diagram-mcp-server` has been superseded by the deploy-on-aws diagram skill)
- Server config uses latest package versions (`@latest`)
- `FASTMCP_LOG_LEVEL=ERROR` set to reduce noise
- Deferred tool search threshold appropriate for server count — with the AWS plugin suite + context7 footprint, the eager-load tool set should be kept small and everything else routed through `ToolSearch`
- `MAX_MCP_OUTPUT_TOKENS` tuned so a single tool call cannot swamp the context window — Opus 5's 1M context is large, but an oversized tool result still wastes tokens and pollutes the cache

### Permissions (settings.local.json)
- No stale one-off permission entries
- No narrow entries subsumed by a broader allow already present (e.g. `Bash(claude --version)` under a blanket `Bash`) — but check the tool prefix first; `Skill(foo)` is not redundant unless a blanket `Skill` exists
- Permissions are minimal and intentional

### Housekeeping
- `logs/team-hooks.jsonl` has no built-in rotation and grows unbounded. When it gets large, **archive rather than delete** — it is an audit log (`gzip -c logs/team-hooks.jsonl > logs/team-hooks.<date>.jsonl.gz && : > logs/team-hooks.jsonl` preserves the inode so running hooks keep appending)
- Orphaned verification sentinels under `logs/verified/<team>/` accumulate from teams that no longer exist. Cross-check team names against live `teams/` and `tasks/` state, inspect the contents, then remove only the dead ones
- Vestigial empty config files (e.g. an `agents/.mcp.json` containing just `{"mcpServers": {}}`) can go
