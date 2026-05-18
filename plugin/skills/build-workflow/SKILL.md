---
name: build-workflow
description: "Run the three workflow-build sub-skills end-to-end, each in its own isolated Agent context: build-design-workflow (Step 1), build-plan-workflow (Step 2), build-run-plan-flow (Step 3). Collects each sub-skill's returned JSON and returns a combined summary. Use as the workflow-build phase of /process-setup."
---

# build-workflow — orchestrate design → plan → run-plan in separate contexts

You run **locally inside Claude Code**. This skill is a thin orchestrator: it invokes three sub-skills sequentially, each in its **own Agent (separate context)** so their conversations don't pollute each other or this one.

Run the steps in order. After each step, capture the sub-skill's returned JSON (status + label + any extra fields) and carry it into the final return shape.

---

## Step 1 — run `build-design-workflow` in a separate context

Spawn an Agent that invokes the `build-design-workflow` skill. The agent should run the skill to completion and report back its final JSON return shape.

Suggested Agent call:
- `description`: "Run build-design-workflow"
- `subagent_type`: `general-purpose`
- `prompt`: "Invoke the `build-design-workflow` skill end-to-end. Follow every step in its SKILL.md, ask the user any clarifying questions it prompts for via `AskUserQuestion`, and when done return its final fenced JSON block verbatim."

Capture the returned JSON as `design_result`.

---

## Step 2 — run `build-plan-workflow` in a separate context

Spawn a new Agent (fresh context) that invokes the `build-plan-workflow` skill.

Suggested Agent call:
- `description`: "Run build-plan-workflow"
- `subagent_type`: `general-purpose`
- `prompt`: "Invoke the `build-plan-workflow` skill end-to-end. Follow every step in its SKILL.md, ask the user any clarifying questions it prompts for via `AskUserQuestion`, and when done return its final fenced JSON block verbatim."

Capture the returned JSON as `plan_result`.

---

## Step 3 — run `build-run-plan-flow` in a separate context

Spawn a new Agent (fresh context) that invokes the `build-run-plan-flow` skill.

Suggested Agent call:
- `description`: "Run build-run-plan-flow"
- `subagent_type`: `general-purpose`
- `prompt`: "Invoke the `build-run-plan-flow` skill end-to-end. Follow every step in its SKILL.md, ask the user any clarifying questions it prompts for via `AskUserQuestion`, and when done return its final fenced JSON block verbatim."

Capture the returned JSON as `run_plan_result`.

---

## Return shape

End your final reply with a single fenced JSON block combining all three sub-skill results:

```json
{
  "status": "configured",
  "design": { "...design_result..." },
  "plan": { "...plan_result..." },
  "run_plan": { "...run_plan_result..." }
}
```

Each nested object is the verbatim JSON the corresponding sub-skill returned (e.g. `{ "status": "configured", "label": "design-workflow: configured", ... }` or `{ "status": "skipped", "label": "design-workflow: skip" }`).
