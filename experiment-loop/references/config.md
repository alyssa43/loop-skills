# Loop config: `experiment-loop.json`

`scripts/loop.py init` writes this file in the project root; every other command reads it. Edit it by hand when a value changes, but not mid-loop: the cohort, evaluators, parameters and tool policy are supposed to stay fixed from the baseline through the last iteration.

```json
{
  "kitaru": ".venv/bin/kitaru",
  "agent_version": "support-desk-demo@1",
  "cohort_version_id": "01a1c71c-abff-7454-8f31-2808a19b9f10",
  "evaluators": [
    {"ref": "support-desk-action-correct@1", "params": {"expected": {"refund-01": "issue_refund", "refund-04": "none"}}}
  ],
  "tool_policy": {
    "default": {"type": "passthrough"},
    "tools": {
      "lookup_order": {"type": "history", "scope": "baseline", "on_miss": "passthrough"},
      "issue_refund": {"type": "static", "cases": [{"match_mode": "exact", "result": "Refund issued."}], "on_miss": "fail"}
    }
  },
  "allowed_models": ["gpt-4.1-mini", "gpt-4.1", "gpt-5-mini"],
  "cap": 6,
  "required_consecutive_greens": 2,
  "evidence_dir": "sessions/experiment-loop",
  "case_id_field": "case_id",
  "prompt_dir": "prompts",
  "worker_concurrency": 10
}
```

Field notes:

- `kitaru`: the CLI binary. Use the project virtualenv's binary so the worker runs with the project's installed adapter.
- `agent_version`: exact `name@N`. This is the one registered executable the whole loop runs on; candidates are overrides on it. The user normally supplies it.
- `cohort_version_id`: the immutable membership version UUID (not the cohort UUID).
- `evaluators`: each entry needs `ref` (`name@N`). `params` is optional and is passed verbatim as that evaluator's parameters; omit it for evaluators that take none. A case is green only if every listed evaluator passed on its replay result.
- `tool_policy`: the replay tool policy, applied to every experiment in the loop. Include every tool the agent can call. `default` passthrough means an unnamed tool runs live.
- `allowed_models`: `start --model` refuses anything not listed. Leave empty only if the user has said any model is fine.
- `cap`, `required_consecutive_greens`: the user's stopping numbers.
- `case_id_field`: the key inside a session's recorded `inputs` that names the case. Falls back to the session name, then the session ID.
- `prompt_dir`: where candidate prompt files are written. Informational; `start --prompt-file` takes an explicit path.

Example `init` call for the project above:

```sh
python ~/.claude/skills/experiment-loop/scripts/loop.py init \
  --kitaru .venv/bin/kitaru \
  --agent-version support-desk-demo@1 \
  --cohort-version 01a1c71c-abff-7454-8f31-2808a19b9f10 \
  --evaluator support-desk-action-correct@1 --evaluator-params-file /tmp/answer-key.json \
  --tool-policy-file /tmp/tool-policy.json \
  --allowed-model gpt-4.1-mini --allowed-model gpt-4.1 --allowed-model gpt-5-mini \
  --cap 6 --greens 2
```

Projects often already have a helper that prints the params and tool policy as JSON (in support-desk-demo it is `python -m support_desk.kitaru_args answer-key refund` and `... tool-policy refund`); redirect those into the two files rather than retyping them.

## Server-side terms the script relies on

- `experiment get NAME` resolves by case-sensitive name, which is how `start` finds an existing experiment and stays idempotent. Experiment configuration freezes once it has runs; a changed candidate needs a new experiment, which the naming rule gives you.
- `experiment run start … --baseline-evaluation-mode if-missing` adopts an existing baseline evaluation only when session, evaluator version and parameters match; otherwise it schedules a fresh one (deterministic evaluators cost nothing).
- `replay list` and `evaluation list` are paged and filtered client-side by the script (by `experiment_run_id` and `session_id`). The latest evaluation per (session, evaluator) wins.
- The server normalizes stored overrides and tool policies by adding explicit nulls; the script compares after stripping nulls.
