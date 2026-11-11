---
name: kitaru-loop
description: Run a Kitaru "Loop" — iterate an agent one change per round against a fixed cohort with a defensible, configurable stopping criterion (n trials per round, pass^k consistency, per-case floor, no-regression, confirmation on fresh trials, round cap and budget) — and report what each change did. Use this whenever someone wants to iterate on prompts, models or parameters until a cohort is "green", "consistent", "stable" or "all passing"; wants a baseline consistency audit of an agent; mentions pass^k, pass@k, trials per round, or "keep changing one thing"; or is setting up or explaining a Loop in the Kitaru UI, even if they don't say "loop". Not for a single replay comparison (kitaru-replay-experiment), reviewing traces (kitaru-investigation), or the simpler two-greens recipe (experiment-loop).
---

# Kitaru loop

Iterate an agent against a fixed set of known cases, one change per round, and stop only when the evidence is good enough to say which change did what. This skill is the general, configurable version of the loop: the person running it sets the inputs for their case, the script applies the rule, and the report says exactly what a green can and cannot claim.

It sits after Kitaru's **Define** step. It needs an accepted cohort version and registered evaluators; if those don't exist, hand off to `kitaru-investigation` first.

## Why the rule looks the way it does

An agent's answer varies between runs even when the grader is deterministic. One run cannot tell a case that passes 95% of the time from one that always passes, and "green twice in a row" (two trials) bounds almost nothing. So every round, including the baseline, replays each case **n** times and scores each case with **pass^k**: the estimated probability that all k attempts succeed, computed from its n trials (τ-bench's estimator, `C(c,k)/C(n,k)`). One miss in five gives pass^3 = 0.40, so inconsistency is punished instead of averaged away.

Three more pieces keep a green honest. A **per-case floor** stops one flaky case hiding inside a good average (default "allow one miss", because "every case n of n" rejects a 95%-reliable candidate 99 times in 100 at n=5 on 20 cases). **No regression** is paired per case against round 0, so "already passing and still passing" reads as unchanged, not improvement, and "got worse" is visible even when the average went up. A **confirmation round** re-runs the winner on n fresh trials that were not used to pick it, because a loop that looks at the dice until something passes is partly selected by luck. The full reasoning, with the numbers, is in [references/criteria.md](references/criteria.md); read it before changing a default or when the user asks why.

## The procedure

1. **Round 0, the baseline audit.** The unchanged agent, n trials per case. It classifies each case as consistent pass, flaky, or consistent fail. That classification is what the hypothesis should target; a change aimed at a case that was already consistently green is not testable here.
2. **Each later round changes exactly one thing** (system prompt, model from the allow-list, or model parameters, whichever the config allows), recorded as an experiment override on the same registered agent version.
3. **Screen first.** The round runs once. Any red gate case ends the round; the loop moves to the next change without spending the other n−1 trials.
4. **Trials.** A round that passes the screen gets the remaining trials. Then the gate: per-case floor, cohort mean pass^k ≥ target, no regression against round 0, and any metric thresholds.
5. **Confirmation.** A round that meets the gate is re-run with n fresh trials and must meet the gate again on those alone. Then the loop stops with that round as the winner.
6. **Stops.** Success, a plateau (no round has beaten the best first-run score in `plateau_patience` consecutive rounds), the round cap, or the budget, whichever comes first. `next` also notes oscillation (the failing set alternating between two rounds, a trade-off) and a stuck case (red three rounds running, a hint to change lever). A loop that never confirms a winner is a finding, not a failure.

Only the experiment override changes between rounds. The registered agent version does not, so every run in the Kitaru UI shows the same version chip; the Overrides column is where the change shows. Say this up front.

## Naming

Experiments are `<prefix>-round-0` for the baseline and `<prefix>-round-N` for each change, where `<prefix>` is the loop name slugified (override with `experiment_prefix`). Runs inside an experiment are numbered by the server and carry their phase (screen, trial, confirmation) in the plan file. Keep these names; the script finds state by them.

## Collect the inputs

These mirror the two Loop setup screens. Take them from the conversation, an investigation checkpoint, or the project; ask only for what is missing, and ask for one thing at a time.

| Input | Where it comes from |
| --- | --- |
| Loop name and hypothesis | the user; the hypothesis names the failure and the suspected cause |
| Agent version `name@N` | the user, or the version on the cohort's sessions |
| Cohort version ID | the Define step |
| Allowed change kinds and model allow-list | the user; do not invent a model list |
| Tool policy | the project's replay policy; must cover every tool |
| Evaluators with kind (binary or metric) and role (gate or report), params, thresholds | the registered evaluators; `session.cost` is a built-in metric source |
| Criteria: n, k, floor, cohort target, no-regression, confirmation, max rounds, plateau patience, budget | the user; defaults n=5, k=3, allow one miss, 0.90, on, on, 6, 2, none |

Then `loop.py init --from FILE` writes `kitaru-loop.json`. Schema and a worked example: [references/config.md](references/config.md). Suggest n from cohort size: under about 10 cases use 10; 20 to 50 use 5; hundreds use 3 plus the screen.

## Approvals and cost

`preflight` reports the estimated cost per run (sum of the cohort's recorded session costs) and per full round. State the worst case (max rounds × n trials, plus confirmation) and ask for one approval covering the whole loop; if the user set a budget, that is the ceiling and the script stops before exceeding it. Do not ask between rounds unless the user wants that. Ask separately if the tool policy can reach a live external system, or if a round would use a model outside the allow-list (the script refuses it anyway).

## Running it

Use `scripts/loop.py` for every step. Point `kitaru` in the config at the project's virtualenv binary.

1. `loop.py preflight` (read-only): auth, workers, run-spec capabilities and hash, cohort size, evaluator scopes, cost estimates. Fix problems before spending.
2. `loop.py worker start` if no worker is live; `worker stop` at the end.
3. `loop.py next` says what the rule requires: `start_round_0`, `start_round N`, `continue_round` (with phase), `collect`, `stop_success`, `stop_plateau`, `stop_cap`, or `stop_budget`, plus any oscillation or stuck-case notes. Follow it; do not re-derive the rule by hand.
4. `loop.py round start --round N ...` creates the experiment idempotently, verifies the stored override and tool policy and the run-spec hash, picks the phase from saved state, and starts one run. Round 0 takes no change flag; later rounds take exactly one of `--prompt-file`, `--model`, `--params`, plus `--change` (one sentence: the change and the failure it targets).
5. `loop.py watch RUN_ID`, then `loop.py collect RUN_ID`. Collect prints each red or unscored case with evaluator explanations and the reply; that is the material for the next change.
6. Back to `next`. When it says stop, `loop.py report` writes `results.md`.

A run with execution failures (worker errors, canceled replays) is neither green nor red; its unscored cases stay out of the denominator and the round needs another run. Say so in the report.

## Deriving the next change

Read [references/change-selection.md](references/change-selection.md). The change must come from the replies that failed in the round just finished, never from a known-good prompt elsewhere in the repo. Prefer clarifying the misread instruction, then adding one deterministic condition the agent can check from tool output, then switching the model. Write a prompt change as a new file derived from the previous round's prompt so the diff is one idea. Never put case IDs, expected answers or evaluator vocabulary in a prompt.

## Reporting

Lead with the outcome in one sentence: which round confirmed, or that the cap or budget stopped the loop. Then the round table from `results.md`, the round-0 audit (consistent, flaky, failing), and for the winner the per-case matrix of round 0 versus selection versus confirmation trials. Always include:

- what the gate can claim, with the rule-of-three bound on total trials, and that unchanged cases carry no evidence about the change;
- what the evaluators do not measure, with one concrete example from the winning replies;
- that these are adaptive results on the curated cohort, not a reliability estimate;
- that the agent version stayed fixed and the changes live in experiment overrides;
- where the evidence is, and which prompt files are new and uncommitted.

Do not say "ship". The user owns deployment.

## Evidence layout

```
<evidence_dir>/
├── preflight.json, worker.log, worker.pid
├── <prefix>-round-0/   experiment.json, runK_plan.json, runK_comparison.json, runK_jobs.json, runK_run.json
├── <prefix>-round-1/ …
└── results.md
```

The config `kitaru-loop.json` lives in the project root. The evidence directory defaults to `sessions/kitaru-loop`, which most Kitaru projects git-ignore; tell the user when they plan to share the repo.
