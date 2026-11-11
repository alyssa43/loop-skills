# Loop config: `kitaru-loop.json`

`scripts/loop.py init --from FILE` validates and writes this file in the project root; every other command reads it. The cohort, evaluators, tool policy and criteria stay fixed from round 0 through the last round. Write a new config (new name) for a new loop.

```json
{
  "name": "Fix refund escalations",
  "hypothesis": "The prompt approves refunds over $200 instead of escalating. Telling it to escalate should fix the failing cases without breaking the rest.",
  "kitaru": ".venv/bin/kitaru",
  "agent_version": "support-router@1",
  "cohort_version_id": "01a1c71c-abff-7454-8f31-2808a19b9f10",
  "allowed_changes": ["system_prompt", "model", "model_params"],
  "allowed_models": ["gpt-4.1", "gpt-4.1-mini"],
  "tool_policy": {
    "default": {"type": "passthrough"},
    "tools": {
      "lookup_order": {"type": "history", "scope": "baseline", "on_miss": "passthrough"},
      "issue_refund": {"type": "static", "cases": [{"match_mode": "exact", "result": "Refund issued."}], "on_miss": "fail"}
    }
  },
  "evaluators": [
    {"ref": "refund_policy@3", "kind": "binary", "role": "gate", "judge": true, "validated": false},
    {"ref": "address_check@1", "kind": "binary", "role": "gate", "params": {"strict": true}},
    {"ref": "tone@2", "kind": "binary", "role": "report", "judge": true},
    {"source": "session.cost", "kind": "metric", "role": "gate", "threshold": {"op": "<=", "value": 0.012}}
  ],
  "criteria": {
    "trials_per_round": 5,
    "k": 3,
    "screen_first": true,
    "per_case_floor": "allow_one_miss",
    "cohort_target_mean_pass_k": 0.9,
    "no_regression": true,
    "confirmation_round": true,
    "max_rounds": 6,
    "plateau_patience": 2,
    "budget_usd": 5.0
  },
  "evidence_dir": "sessions/kitaru-loop",
  "case_id_field": "case_id",
  "prompt_dir": "prompts"
}
```

## Fields

- `name`: the loop. Slugified into `experiment_prefix` unless you set that explicitly. Experiments become `<prefix>-round-0`, `<prefix>-round-1`, …
- `hypothesis`: free text; stored on each experiment description and in the report.
- `kitaru`: the CLI binary; use the project virtualenv's so the worker runs with the project's adapter.
- `agent_version`: exact `name@N`. The one registered executable the loop runs on; rounds are overrides on it. Usually supplied by the user.
- `cohort_version_id`: the immutable membership version UUID.
- `allowed_changes`: any of `system_prompt`, `model`, `model_params`. `round start` refuses a change kind not listed. The adapter must support the kind: the OpenAI Agents adapter applies all three; an importer-backed version supports none (preflight reports `runtime_capabilities`).
- `allowed_models`: `--model` refuses anything not listed. Empty means the user said any model is fine.
- `tool_policy`: applied to every round. Name every tool the agent can call.
- `evaluators`: each has `kind` `binary` (pass/fail per case → pass^k) or `metric` (number per case → threshold on the round mean), and `role` `gate` (counts toward green) or `report` (shown, not gating). A binary gate needs a `ref` `name@N`; `params` are passed verbatim. A metric can be a `ref` (the evaluation's `score`, else `value`) or the built-in `source: "session.cost"` (the result session's recorded cost). A gating metric needs `threshold: {op, value}` with op one of `<=`, `<`, `>=`, `>`. Mark LLM judges `"judge": true` and, once checked against human verdicts, `"validated": true`.
- `criteria`: see [criteria.md](criteria.md). `budget_usd` null means no budget stop. `plateau_patience` is the number of consecutive rounds (from round 1) allowed without beating the best first-run score so far; null turns the plateau stop off.
- `case_id_field`: the key inside a session's recorded `inputs` that names the case; falls back to the session name.
- `evidence_dir`, `prompt_dir`: where evidence and candidate prompt files go.

## Phases and run numbering

Within a round's experiment, runs are numbered by the server. The plan file records each run's phase:

- round 0: all runs are `trial` (no screen; the misses are the audit).
- round N: run 1 is `screen` when `screen_first` is on; then `trial` until `trials_per_round` selection trials exist; then `confirmation` for another `trials_per_round` runs if the selection gate was met.

`next` derives all of this from the saved plan and comparison files; `round start` refuses to start a phase the state does not call for.

## Server-side notes the script relies on

- `experiment get NAME` resolves by case-sensitive name; that is how rounds stay idempotent. Experiment configuration freezes once it has runs, which the one-change-per-round naming respects.
- `experiment run start --baseline-evaluation-mode if-missing` adopts an existing baseline evaluation only if session, evaluator version and params match.
- `replay list` and `evaluation list` are paged and filtered client-side (by `experiment_run_id`, by `session_id`); the latest evaluation per (session, evaluator) wins.
- The server normalizes stored overrides and policies with explicit nulls; comparisons strip nulls.
- Cost per run is estimated from the cohort sessions' recorded `cost` at preflight; actual spend is summed from result sessions at collect.
