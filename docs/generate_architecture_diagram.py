#!/usr/bin/env python3
"""Generate docs/architecture-diagram.png from source.

The previous PNG in this repo had no generator -- it was a hand-made export
that nobody could regenerate, so it silently rotted out of sync with the v2
(Jira-backed) architecture. This script is that missing source: it draws the
diagram from scratch with pure matplotlib + the standard library, so it can
be regenerated deterministically any time the architecture changes.

Run it from the repo root with the project's own virtualenv, which already
has matplotlib installed for other purposes:

    .venv/bin/python3 docs/generate_architecture_diagram.py

It writes docs/architecture-diagram.png, overwriting the file in place so
the existing README reference (docs/architecture-diagram.png) keeps working.

matplotlib is a BUILD-TIME-ONLY dependency for this one script. It is
intentionally NOT added to requirements-dev.txt -- nothing at runtime or in
the test suite imports it. If your environment doesn't have it:

    python3 -m venv /tmp/diagram-venv
    /tmp/diagram-venv/bin/pip install --no-input matplotlib==3.11.1
    /tmp/diagram-venv/bin/python3 docs/generate_architecture_diagram.py
"""
import os

import matplotlib

matplotlib.use("Agg")  # headless -- no display server required to render

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "architecture-diagram.png")

# -- palette -----------------------------------------------------------------
# Keeps the original diagram's blue / purple / green three-column feel.
BLUE = {"face": "#E3F2FD", "edge": "#1565C0", "text": "#0D47A1"}
BLUE_DARK = {"face": "#BBDEFB", "edge": "#0D47A1", "text": "#0D47A1"}
PURPLE = {"face": "#F3E5F8", "edge": "#6A1B9A", "text": "#4A148C"}
GREEN = {"face": "#E8F5E9", "edge": "#2E7D32", "text": "#1B5E20"}
GREEN_DARK = {"face": "#C8E6C9", "edge": "#1B5E20", "text": "#1B5E20"}
AMBER = {"face": "#FFF3E0", "edge": "#E65100", "text": "#BF360C"}
GRAY = {"face": "#FAFAFA", "edge": "#616161", "text": "#212121"}
RED_NOTE = "#B71C1C"

FIG_W, FIG_H = 20, 13.33  # 3:2 landscape
XLIM, YLIM = 200, 133.33


def new_axes():
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ax.set_xlim(0, XLIM)
    ax.set_ylim(0, YLIM)
    ax.axis("off")
    ax.set_aspect("equal")
    return fig, ax


def box(ax, x, y, w, h, palette, title=None, body=None, dashed=False,
        title_size=12.5, body_size=9.2, lw=1.8, title_color=None, zorder=2,
        title_align="left"):
    """Rounded box with an optional bold title line and a multi-line body."""
    style = "round,pad=0.35,rounding_size=1.6"
    patch = FancyBboxPatch(
        (x, y), w, h, boxstyle=style,
        linewidth=lw, edgecolor=palette["edge"], facecolor=palette["face"],
        linestyle="dashed" if dashed else "solid", zorder=zorder,
    )
    ax.add_patch(patch)
    cursor_y = y + h - 2.6
    if title:
        tx = x + w / 2 if title_align == "center" else x + 2.2
        ha = "center" if title_align == "center" else "left"
        ax.text(tx, cursor_y, title, fontsize=title_size, fontweight="bold",
                 color=title_color or palette["text"], ha=ha, va="top", zorder=zorder + 1)
        cursor_y -= 3.6
    if body:
        ax.text(x + 2.2, cursor_y, body, fontsize=body_size, color="#222222",
                 ha="left", va="top", linespacing=1.55, zorder=zorder + 1)
    return patch


def arrow(ax, posA, posB, color, rad=0.0, style="-|>", lw=1.7, ls="solid",
          label=None, fontsize=7.6, label_offset=(0, 0), zorder=4):
    patch = FancyArrowPatch(
        posA, posB, connectionstyle="arc3,rad={}".format(rad), arrowstyle=style,
        color=color, lw=lw, linestyle=ls, mutation_scale=15, zorder=zorder,
        shrinkA=2, shrinkB=2,
    )
    ax.add_patch(patch)
    if label:
        mx = (posA[0] + posB[0]) / 2 + label_offset[0]
        my = (posA[1] + posB[1]) / 2 + label_offset[1]
        ax.text(mx, my, label, fontsize=fontsize, color=color, ha="center", va="center",
                 style="italic", zorder=zorder + 1,
                 bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85))


def build():
    fig, ax = new_axes()

    # -- title -----------------------------------------------------------
    ax.text(XLIM / 2, 130.5,
             "Claude Code Agent Team — Architecture (v2: Jira as System of Record)",
             fontsize=19, fontweight="bold", ha="center", va="top", color="#111111")

    # -- Plan -> Build -> Review -> Fix strip -----------------------------
    loop_labels = ["Plan", "Build", "Review", "Fix"]
    loop_x = [8, 56, 104, 152]
    loop_w = 40
    loop_y, loop_h = 108, 11
    for i, (lx, lbl) in enumerate(zip(loop_x, loop_labels)):
        pal = BLUE if lbl in ("Plan",) else PURPLE if lbl == "Build" else GREEN if lbl == "Review" else AMBER
        box(ax, lx, loop_y, loop_w, loop_h, pal, title=None, body=None, lw=1.6)
        ax.text(lx + loop_w / 2, loop_y + loop_h / 2, lbl, fontsize=13.5, fontweight="bold",
                 ha="center", va="center", color=pal["text"])
        if i < 3:
            arrow(ax, (lx + loop_w, loop_y + loop_h / 2), (loop_x[i + 1], loop_y + loop_h / 2),
                  color="#424242", lw=1.8)
    # fix -> build loop-back (review fails -> new fix sprint). Routed as a
    # shallow arc ABOVE the strip, in the clear band below the title, so it
    # never crosses the column boxes underneath.
    arrow(ax, (loop_x[3] + loop_w / 2, loop_y + loop_h), (loop_x[1] + loop_w / 2, loop_y + loop_h),
          color=AMBER["edge"], rad=0.12, ls="dashed", lw=1.6,
          label="review FAILs → fix sprint", label_offset=(0, 6.6))

    col_top, col_bot = 104, 8

    # ======================================================================
    # COLUMN 1 -- Agent Team
    # ======================================================================
    c1_x, c1_w = 4, 68
    box(ax, c1_x, col_bot, c1_w, col_top - col_bot, BLUE, dashed=True, lw=2.0,
        title="Agent Team", title_size=14, title_align="center")

    # Inner boxes stop short of the outer box's right edge, leaving a clear
    # vertical channel (rx) for the lead<->agents arrows so they never run
    # along / through a box border.
    inner_x = c1_x + 4
    inner_w = c1_w - 8 - 8  # -8 outer padding, -8 reserved channel on the right
    rx = c1_x + c1_w - 5.5  # arrow channel, between inner boxes and outer border

    # Parallel Workers (nested dashed)
    pw_x, pw_y, pw_w, pw_h = inner_x, 74, inner_w, 24
    box(ax, pw_x, pw_y, pw_w, pw_h, BLUE, dashed=True, lw=1.5,
        title="Parallel Workers", title_size=10.5, title_align="center", title_color="#1565C0")

    coding_x, coding_w = pw_x + 2, pw_w / 2 - 3
    devops_x, devops_w = pw_x + pw_w / 2 + 1, pw_w / 2 - 3
    inner_y, inner_h = pw_y + 2.2, pw_h - 9.5
    box(ax, coding_x, inner_y, coding_w, inner_h, BLUE_DARK, lw=1.4,
        title="coding-agent", title_size=10.5, title_align="center",
        body="sonnet · high", body_size=8.6)
    box(ax, devops_x, inner_y, devops_w, inner_h, BLUE_DARK, lw=1.4,
        title="devops-agent", title_size=10.5, title_align="center",
        body="sonnet · xhigh", body_size=8.6)

    review_y, review_h = 56, 14
    box(ax, inner_x, review_y, inner_w, review_h, BLUE_DARK, lw=1.6,
        title="review-agent", title_size=11.5, title_align="center",
        body="opus · max", body_size=8.8)

    sa_y, sa_h = 38, 14
    box(ax, inner_x, sa_y, inner_w, sa_h, BLUE, dashed=True, lw=1.4,
        title="sa-agent  (optional)", title_size=11, title_align="center",
        body="opus · high", body_size=8.8)

    lead_y, lead_h = 14, 16
    box(ax, inner_x, lead_y, inner_w, lead_h, BLUE_DARK, lw=2.2,
        title="fullstack-agent  —  Team Lead", title_size=11.5, title_align="center",
        body="opus · xhigh", body_size=8.8)

    # User prompt arrow into the lead
    ax.text(c1_x - 3.2, lead_y + lead_h / 2, "User\nprompt", fontsize=8.6, ha="right",
             va="center", color="#0D47A1", fontweight="bold")
    arrow(ax, (c1_x - 3.2, lead_y + lead_h / 2), (inner_x, lead_y + lead_h / 2),
          color="#0D47A1", lw=1.9)

    # workers -> review-agent (outputs), short straight drop just outside the
    # Parallel Workers box's own left edge
    ox = inner_x + 2
    arrow(ax, (ox, pw_y), (ox, review_y + review_h), color="#1565C0",
          rad=0.0, lw=1.7, label="outputs", label_offset=(-6.8, 0))

    # lead <-> agents (SendMessage), dedicated right-margin channel, entirely
    # clear of every box's border
    ax.text(rx + 1.4, pw_y - 1.4, "SendMessage", fontsize=8, ha="left", va="top",
             color="#1976D2", style="italic", rotation=90)
    arrow(ax, (rx, lead_y + lead_h - 1), (rx, pw_y + 3), color="#1976D2",
          style="<->", rad=0.0, lw=1.5, ls="dashed")
    arrow(ax, (rx, lead_y + lead_h - 1), (rx - 3, review_y + 3), color="#1976D2",
          style="<->", rad=-0.25, lw=1.5, ls="dashed")
    arrow(ax, (rx, lead_y + lead_h - 1), (rx - 3, sa_y + 3), color="#1976D2",
          style="<->", rad=-0.20, lw=1.5, ls="dashed")

    # ======================================================================
    # COLUMN 2 -- Configuration / Skills / Hooks
    # ======================================================================
    c2_x, c2_w = 76, 56

    cfg_y, cfg_h = 86, 18
    box(ax, c2_x, cfg_y, c2_w, cfg_h, PURPLE, lw=1.7,
        title="Configuration (.claude/)", title_size=11.5,
        body="agents/   rules/   skills/   commands/   hooks/\n"
             "settings.json\n"
             "jira-config.json  — generated, gitignored",
        body_size=8.7)

    skr_y, skr_h = 56, 26
    box(ax, c2_x, skr_y, c2_w, skr_h, PURPLE, lw=1.7,
        title="Skills / Rules / Commands", title_size=11.5,
        body="Rules:  security-guidelines, agent-team-protocol,\n"
             "        execution-hygiene\n"
             "Skills: spec-workflow, jira-workflow, documentation,\n"
             "        git-workflow, concurrent-fetch\n"
             "Commands: /brainstorm, /optimize-my-claude",
        body_size=8.5)

    hooks_y, hooks_h = 10, 42
    box(ax, c2_x, hooks_y, c2_w, hooks_h, PURPLE, lw=2.3,
        title="Hooks (fail-open)", title_size=12,
        body="PreToolUse  → createJiraIssue        (format check)\n"
             "PreToolUse  → transitionJiraIssue     (verify gate)\n"
             "PostToolUse → Jira mutations          (mirror journal)\n"
             "TeammateIdle                          (work check)",
        body_size=8.8)
    ax.text(c2_x + c2_w - 2.2, hooks_y + hooks_h - 2.6, "corrected in v2",
             fontsize=8, ha="right", va="top", color=RED_NOTE, fontweight="bold",
             style="italic")
    ax.text(c2_x + 2.2, hooks_y + 8.6,
             "All four → ~/.claude/logs/team-hooks.jsonl\n"
             "mirror journal also → ~/.claude/logs/jira-mirror/<project>.jsonl\n"
             "  (TeammateIdle reads that mirror back)",
             fontsize=8.1, ha="left", va="top", color="#4A148C", style="italic",
             linespacing=1.5)

    # ======================================================================
    # COLUMN 3 -- Jira
    # ======================================================================
    c3_x, c3_w = 138, 58

    jira_y, jira_h = 82, 22
    box(ax, c3_x, jira_y, c3_w, jira_h, GREEN_DARK, lw=2.0,
        title="Jira  —  board model", title_size=12, title_align="center",
        body="Epic = spec        Task = work unit\n"
             "Sprint = parallel group\n"
             "Labels: role-*  agent-*  spec-*  group-*\n\n"
             "To Do → In Progress → In Review → Done",
        body_size=8.6)

    # Nested dashed "Two planes into Jira" box -- same pattern as Parallel
    # Workers on the left, so the section label lives in its own header
    # strip instead of floating in the arrows' path.
    planes_y, planes_h = 50, 27
    box(ax, c3_x, planes_y, c3_w, planes_h, GREEN, dashed=True, lw=1.5,
        title="Two planes into Jira", title_size=10.3, title_align="center",
        title_color="#1B5E20")

    rt_w = c3_w / 2 - 3
    inner_planes_y, inner_planes_h = planes_y + 2, planes_h - 9
    box(ax, c3_x + 2, inner_planes_y, rt_w, inner_planes_h, GREEN, lw=1.4,
        title="Runtime plane", title_size=9.3, title_align="center",
        body="agents → Atlassian MCP\n(OAuth read/write:\njira-work) → Jira",
        body_size=7.7)
    box(ax, c3_x + c3_w / 2 + 1, inner_planes_y, rt_w, inner_planes_h, AMBER, lw=1.4,
        title="Admin plane", title_size=9.3, title_align="center",
        body="jira_bootstrap.py\n(JIRA_API_TOKEN)\n→ Jira REST: project,\nsprint lifecycle, ID\ndiscovery, teardown",
        body_size=7.3)

    # single convergence arrow: both planes feed the same Jira instance
    arrow(ax, (c3_x + c3_w / 2, planes_y + planes_h), (c3_x + c3_w / 2, jira_y),
          color="#2E7D32", lw=1.8)

    art_y, art_h = 10, 36
    box(ax, c3_x, art_y, c3_w, art_h, GREEN, lw=1.7,
        title="Spec artifacts  (.claude/specs/<slug>/)", title_size=10.3,
        body="spec.md   design.md   decisions.md\n"
             "requirements.md   jira-run.json",
        body_size=8.6)
    ax.text(c3_x + 2.2, art_y + art_h - 15.2,
             "tasks.md and review.md are RETIRED\n"
             "backlog + review verdicts live in Jira,\nnot on disk",
             fontsize=8.3, ha="left", va="top", color=RED_NOTE, fontweight="bold",
             linespacing=1.5)

    # ======================================================================
    # Footer
    # ======================================================================
    ax.plot([4, XLIM - 4], [5.6, 5.6], color="#BDBDBD", lw=1)
    ax.text(XLIM / 2, 2.6,
             "plan / build / review / fix   |   Jira is the system of record   |   four fail-open hooks",
             fontsize=10.5, ha="center", va="center", color="#333333", style="italic")

    fig.savefig(OUT_PATH, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("wrote {}".format(OUT_PATH))


if __name__ == "__main__":
    build()
