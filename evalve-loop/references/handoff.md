# Evalve MVP handoff

## Form fields

| Field | Contract |
| --- | --- |
| Loop name | Display name. The runner uses its lowercase slug as the experiment prefix. |
| Cohort | Exact cohort version UUID. The cohort identifies the agent. |
| Evaluators | One or more exact binary evaluator references in `name@version` form. |
| Allowed changes | At least one of `system_prompt` and `model`. |
| Allowed models | Selected model IDs when model changes are enabled. The form's model picker supplies IDs. Provider credentials stay in the project environment. |
| Replays to succeed | A positive integer. This is the number of consecutive full-cohort greens required and the minimum number of fully scored baseline replays. |
| Maximum attempts | A positive integer at least as large as replays to succeed. Counts all submitted full-cohort runs across the baseline, candidates, and retries. |
| Replay budget | A positive USD amount. |

## Generated prompt

Interpolate the actual form selections and pluralize “replay” as appropriate. The cohort size in the prose comes from the selected cohort version. Include the model list when model changes are enabled.

```text
Use the evalve-loop skill in this project to run “{loopName}”.

Replay the selected cohort on the unchanged agent for lap 0. Complete
{requiredConsecutiveGreens} fully scored baseline replays even if one is red.
Stop if the baseline earns {requiredConsecutiveGreens} consecutive greens.
Otherwise, use the failures to choose one allowed change per candidate lap.
A replay is green when all {cohortSize} sessions pass every selected evaluator.
A candidate wins with {requiredConsecutiveGreens} consecutive green replays
and ends on its first red. Diagnose incomplete replays and retry that lap.

Cohort version  {cohortLabel} ({cohortVersionId})
Evaluators  {exactEvaluatorReferences}
Allowed changes  {enabledChanges}
Allowed models  {selectedModelIds}
Maximum replay attempts  {maxAttempts}, including baseline and retries
Replay budget  ${budgetUsd} USD

Resolve the agent version, evaluator parameters, and replay tool policy
from this project or its saved configuration. Ask me about missing or
ambiguous settings. Check the replay policy and estimated cost in
preflight, then run the loop within the limits above. Report the lap
results, stopping reason, and winning change if there is one.
```

## Runner config

Save the resolved settings as `evalve-loop.json` in the agent project. No prior experiment or run ID is required.

```json
{
  "name": "Fix refund escalations",
  "cohort_version_id": "01a1c71c-abff-7454-8f31-2808a19b9f10",
  "evaluators": [
    {"ref": "refund_policy@3", "params": {"limit": 200}},
    {"ref": "address_check@2"}
  ],
  "allowed_changes": ["system_prompt", "model"],
  "allowed_models": ["example-model"],
  "required_consecutive_greens": 2,
  "max_attempts": 10,
  "budget_usd": "5.00",
  "tool_policy": {
    "default": {"type": "history", "scope": "baseline", "on_miss": "fail"},
    "tools": {}
  },
  "kitaru": ".venv/bin/kitaru"
}
```

Use the project's actual replay tool policy. Optional fields include:

- `agent_version` selects an exact `name@version` or `agent-id@version` of the cohort's agent when the cohort cannot supply a single runnable recorded version.
- Each evaluator accepts `params` and a `connection_id` if its project configuration specifies one.
- `baseline_prompt_file` is a project-relative path to the inherited prompt. It lets the report show a prompt diff. `baseline_model` can supply the inherited model ID when it lives outside the agent version's `OPENAI_MODEL` setting. Resolve it before launching a model candidate. If needed after preflight, pass `start --baseline-model MODEL` with the candidate.
- `baseline_estimate_usd` supplies the estimated agent replay cost of one full-cohort run. Otherwise preflight uses the sum of recorded costs when all members have costs from the selected version. A new model needs its own estimate with `start --estimate-usd USD`.
- `kitaru` defaults to `kitaru` on PATH. `evidence_dir` defaults to `sessions/evalve-loop`. Each loop has its own slug subdirectory.

Before a submission the runner adds the next run's estimate to accounted spend and checks it against the cap. When Kitaru omits a result-session cost, the runner accounts for at least that attempt's estimate and shows both totals. Kitaru's replay cost can be estimated and excludes evaluator/provider charges outside its session cost records. The check runs between submissions. An in-flight run can cost more than its estimate.

## Reading results in Evalve

Experiments use `<slug>-round-N`. The description starts with `Round N: <change>` followed by a line starting with `Evalve loop: ` and one JSON object. This identifies the MVP workflow. Group experiments by the object's `loop_id` and sort by `lap`. Read settings from lap 0.

- Every lap has `schema_version: 1`, `loop_id`, and `lap`.
- Lap 0 also has `name`, `agent_version_id`, `cohort_version_id`, `evaluators`, `allowed_changes`, `allowed_models`, `required_consecutive_greens`, `max_attempts`, and `budget_usd`.

The runner writes an `Evalve result: ` JSON line on lap 0 after collecting a run and when generating the report. This is the authoritative snapshot of the runner's decision. It has `schema_version`, `loop_id`, `status`, nullable `winner_lap` and `baseline_model`, total `attempts`, `recorded_cost_usd` and `accounted_cost_usd` as decimal strings, nullable `reason`, and `laps`. Status values are `running`, `stop_success`, `stop_attempt_cap`, and `stop_budget`. Each lap includes `lap`, `change`, `experiment_id`, `attempts`, `green`, `red`, `incomplete`, `green_streak`, and `runs`. Each run includes `run_id`, `outcome`, `passed`, `failed`, and `unscored`. The outcome is `green`, `red`, or `incomplete`. Read live progress from Kitaru's experiment-run statuses. If the budget blocks the very first submission, the report is local because there is no experiment to publish to.

Use the winning experiment's `override` to display the proposed change. Local evidence includes `results.json` with the same result object, `results.md` with the report and available diff, and `lap-N-run-K.json` with case outcomes, replay and result-session IDs, evaluator explanations, and replies. Link to experiments and replays using their IDs.
