---
name: experiment-loop
description: Run a bounded Kitaru experiment loop against an accepted cohort — one unchanged baseline run, then one change per iteration (system prompt or model) until a configuration passes every case in two consecutive runs or an iteration cap is hit — and report the version history honestly. Use this whenever the user wants to iterate on an agent until it is "all green", "loop experiments", "keep changing one thing until it passes", run a baseline then candidates, or automate prompt/model iteration on a Kitaru cohort, even if they don't say "loop". Not for a single one-off replay comparison (that is kitaru-replay-experiment) or for reviewing traces (kitaru-investigation).
---

# Experiment loop

Iterate an agent against a fixed set of known cases, one change at a time, until it is green twice in a row, then show exactly what changed and what each change did. The human sets the rule and the budget; this skill runs the loop and keeps the evidence straight.

The loop sits after the Kitaru **Define** step. It needs an accepted cohort version and a registered evaluator. If those do not exist yet, hand off to `kitaru-investigation` first.

## The rule, in one paragraph

Run the recorded configuration once, unchanged, as the **baseline** experiment. If every case is green, run the identical configuration again; two consecutive all-green runs on one configuration end the loop. If any case is red, that configuration is finished: read the failing cases, change exactly one thing aimed at that failure (a system-prompt edit or a model switch from the allowed list), and start a new experiment. Never change two things at once, and never touch the cohort, evaluator, parameters, or tool policy during the loop. Stop at the cap even if nothing is green yet. The cap and the "two greens" count are the user's numbers.

## What stays fixed and what varies

Only the experiment override changes between iterations. The registered agent version does not. In the Kitaru UI every run therefore shows the same version chip (for example `support-desk-demo · v1`), and the Overrides column is where the change shows. Say this up front so nobody expects a `v2` to appear. If the user wants real registered versions per iteration, that is a different workflow: register a version per candidate and point `agent_version` at it, instead of using overrides.

## Naming

- Baseline experiment: `<agent-name>-baseline`. It runs once. The user has been explicit that the baseline is not "iteration 1".
- Candidates: `<agent-name>-iteration-1`, `-iteration-2`, … numbered from the first change.
- Run numbers inside an experiment come from the server (Run #1, Run #2).

`scripts/loop.py` enforces these names, so do not create experiments by hand with other names mid-loop.

## Inputs to collect before starting

Gather these from the conversation, an existing investigation checkpoint, or the project, and ask only for what is missing. The agent version is normally something the user supplies.

| Input | Where it usually comes from |
| --- | --- |
| Agent version reference (`name@N`) | the user, or the version attached to the cohort's sessions |
| Cohort version ID | the Define step; `kitaru cohort version get NAME@N` |
| Evaluator reference(s) `name@N` and parameters | the registered evaluator and its answer key; many projects have a helper that prints the params JSON |
| Tool policy JSON | the project's replay policy; confirm it covers every tool the agent can call |
| Allowed models | the user; do not invent a list. If they think one exists, look before saying it doesn't |
| Cap on iterations and the greens-in-a-row count | the user; defaults 6 and 2 |
| Prompt files directory | where candidate prompts are written, so each change is a reviewable file |

Then write them down once with `loop.py init`, which produces `experiment-loop.json` in the project root. Everything else reads that file. See [references/config.md](references/config.md) for the schema and a worked example.

## Approvals and cost

Each run is a fresh replay of every cohort case: paid model calls plus worker time. State the per-run case count and a cost guess from the recorded sessions, then ask for **one** approval that covers the whole loop up to the cap. Many users explicitly do not want to be asked between iterations; respect that once they have said it. Ask separately only if a tool policy would hit a live external system, or if a candidate wants a model outside the allowed list.

## Running the loop

Use `scripts/loop.py` for every step so the evidence layout, idempotency keys, and verification are uniform. Run it with the project's Kitaru CLI (set `kitaru` in the config to the virtualenv binary when the project has one).

1. **Preflight** (read-only): `loop.py preflight`. It checks authentication, live workers, the agent version's run spec and `runtime_capabilities`, the cohort's session count, and that each evaluator's agent scope matches. It records a hash of the public run spec. Fix problems before spending anything.
2. **Worker**: if `live_worker_count` is 0, `loop.py worker start` launches one from the project root in the background (concurrency 10, claiming the agent version and evaluator tasks). The task subprocess inherits the project's environment, so provider credentials must be reachable there. Stop it with `loop.py worker stop` when the loop ends.
3. **Baseline**: `loop.py start --experiment baseline --description "Unchanged recorded configuration."` creates `<agent>-baseline` with no override, re-verifies the stored tool policy and run-spec hash, and starts run 1.
4. **Watch and collect**: `loop.py watch RUN_ID` blocks until the run settles (run it in the background for long runs). Then `loop.py collect RUN_ID --experiment baseline` pairs every replay with its evaluations and prints the red cases with their evaluator explanations and the agent's reply. That printout is the raw material for the next change.
5. **Decide**: `loop.py next` applies the rule to the saved evidence and says what comes next: `repeat`, `new_iteration` (with the red case IDs), `stop_success`, `stop_cap`, or `collect` if something was left unfinished. Follow it; do not re-derive the rule by hand.
6. **Change one thing**: for `new_iteration`, read [references/change-selection.md](references/change-selection.md), look at the failing replies, and write the change. For a prompt change, write a new file such as `prompts/<route>_iter<N>.md` derived from the previous iteration's prompt (so the diff is one idea), and pass `--prompt-file`. For a model change, pass `--model` with a name from the allowed list and keep the prompt file the same as the previous iteration. Give `--description` one sentence naming the change and the failure it targets; it becomes the version table row.
7. **Repeat** from step 4 until `next` says stop.
8. **Report**: `loop.py report` writes `results.md` under the evidence directory: the version table, fixed conditions, per-run IDs and costs, a post-loop drift check on the run spec, and the limits. Stop the worker.

If a run fails or stalls, collect what settled, report pending/failed/canceled counts separately from red cases, and do not count a run with execution failures as green or red. Fix the cause and start a new run of the same experiment.

## Deriving a change honestly

The user is testing whether iterating one change at a time converges, so the change must come from the evidence of the run that just failed, not from a known-good prompt lying around in the repo. Read the failing replies. Name the specific misreading (an inclusive boundary treated as exclusive, authorization confused with eligibility, a status word misunderstood). Prefer, in this order: clarify the ambiguous instruction; add one deterministic condition the agent can check from tool output; switch the model. If an earlier project already found a fix, it is fine to converge on the same idea, but write it from the failure in front of you and say that it coincides.

A prompt change should not mention case IDs, expected answers, or anything that only exists in the evaluator; that would be leakage, and the green would mean nothing.

## Reporting

Lead with the outcome in one sentence (which experiment stopped the loop, or that the cap was hit), then the version table from `results.md`. Give raw counts with denominators, name every regression, and keep execution failures separate from evaluator reds. Always include:

- what the evaluator does not measure (reply quality is the usual gap) with one concrete example from the winning runs;
- that these are adaptive results on the curated cohort, not a reliability estimate;
- that the agent version stayed fixed and the changes live in the experiment overrides;
- where the evidence is, and which new prompt files are uncommitted.

Do not say "ship". The user owns the deployment decision.

## Evidence layout

```
<evidence_dir>/
├── experiment-loop.json copy is NOT here; the config lives in the project root
├── preflight.json
├── worker.log, worker.pid
├── <agent>-baseline/
│   ├── experiment.json
│   ├── run1_plan.json          configuration, override, run-spec hash
│   ├── run1_comparison.json    paired per-case results, replies, IDs, cost
│   ├── run1_jobs.json, run1_run.json
├── <agent>-iteration-1/ …
└── results.md
```

Everything is per project under `sessions/experiment-loop` by default, which most Kitaru projects already git-ignore. Say so when the user plans to share the repo: the summary tables in the final message and `results.md` are what teammates can see.
