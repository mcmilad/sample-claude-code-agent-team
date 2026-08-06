"""The serverless-3tier CI workflow must stay runnable on a tree where
examples/serverless-3tier/ does not exist. `workflow_dispatch` is not
path-filtered, so the workflow can be invoked on exactly such a tree -- and
that directory is absent from this repo right now.

Nothing else in this suite reads .github/. The verification originally
declared for this work was
`python3 -c "import yaml,sys; yaml.safe_load(open(...)); print('yaml ok')"`,
which is blind twice over: it cannot even import yaml under bare python3, and
when it does run it only proves the file is well-formed YAML -- it printed
"yaml ok" just as happily with the workflow-level
`defaults.run.working-directory` that caused the failure it was meant to
catch. These tests assert the properties that actually keep the workflow
green.
"""
import os
import re

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, ".github", "workflows", "serverless-3tier.yml")

# The directory the workflow builds; absent from the tree today.
TEMPLATE_DIR = "examples/serverless-3tier"

# `if: steps.<id>.outputs.present == 'true'`, with or without ${{ }} wrapping.
GUARD_RE = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outputs\.present\s*==\s*'true'")


def load_workflow():
    with open(WORKFLOW) as fh:
        return yaml.safe_load(fh)


def jobs():
    return load_workflow()["jobs"]


def presence_check_ids(job):
    """Ids of the steps that probe the tree and export `present`."""
    return [step["id"] for step in job.get("steps") or []
            if step.get("id") and "present=" in (step.get("run") or "")
            and "GITHUB_OUTPUT" in (step.get("run") or "")]


def template_dependency(step):
    """Why this step breaks when TEMPLATE_DIR is absent, or None if it does not.

    Two shapes matter. A run step with a `working-directory` under the template
    cannot chdir. A step passing `node-version-file` to setup-node resolves it
    from the workspace root, so it fails on the missing .nvmrc regardless of
    any working-directory -- the same holds for any `with` value naming the
    template dir (`cache-dependency-path`, artifact paths).
    """
    wd = step.get("working-directory") or ""
    if wd == TEMPLATE_DIR or wd.startswith(TEMPLATE_DIR + "/"):
        return "working-directory: " + wd
    with_ = step.get("with") or {}
    if "node-version-file" in with_:
        return "with.node-version-file: {}".format(with_["node-version-file"])
    for key, value in with_.items():
        if isinstance(value, str) and TEMPLATE_DIR in value:
            return "with.{}: {}".format(key, value)
    return None


def test_the_workflow_parses_and_declares_both_jobs():
    assert os.path.isfile(WORKFLOW), WORKFLOW + " is missing"
    assert set(jobs()) == {"infra", "web"}


def test_no_workflow_level_defaults():
    """A workflow-level `defaults.run.working-directory` applies to every run
    step in BOTH jobs, including the presence checks that have to run from the
    repo root. A workflow_dispatch on a tree without the template then dies on
    `cannot chdir` before a single guard is ever evaluated.
    """
    assert "defaults" not in load_workflow(), (
        "the workflow must not set a top-level `defaults` block: it would "
        "re-apply working-directory to every run step in both jobs, which is "
        "the regression this file exists to catch")


def test_no_job_level_defaults():
    for name, job in jobs().items():
        assert "defaults" not in job, (
            "job {!r} must not set `defaults`: it would apply "
            "working-directory to that job's presence check too".format(name))


def test_each_job_probes_for_the_template_from_the_repo_root():
    for name, job in jobs().items():
        ids = presence_check_ids(job)
        assert ids, (
            "job {!r} has no presence-check step writing `present=` to "
            "$GITHUB_OUTPUT; nothing can be guarded on it".format(name))
        by_id = {step.get("id"): step for step in job["steps"]}
        for step_id in ids:
            step = by_id[step_id]
            assert not step.get("working-directory"), (
                "presence check {!r} in job {!r} must run from the repo root, "
                "or it cannot report that the template is missing".format(
                    step_id, name))
            assert "if" not in step, (
                "presence check {!r} in job {!r} must run unconditionally"
                .format(step_id, name))


def test_every_template_dependent_step_is_guarded_on_a_presence_check():
    for name, job in jobs().items():
        ids = presence_check_ids(job)
        for step in job["steps"]:
            reason = template_dependency(step)
            if reason is None:
                continue
            label = step.get("name") or step.get("uses") or step.get("run")
            referenced = GUARD_RE.findall(str(step.get("if") or ""))
            assert referenced, (
                "step {!r} in job {!r} needs {} but carries no "
                "`if: steps.<check>.outputs.present == 'true'` guard; it fails "
                "when the template is absent".format(label, name, reason))
            unknown = set(referenced) - set(ids)
            assert not unknown, (
                "step {!r} in job {!r} is guarded on {}, which is not a "
                "presence check in that job".format(
                    label, name, sorted(unknown)))
