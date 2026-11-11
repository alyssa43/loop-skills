# loop-skills

Two Claude Code skills for iterating an agent against a fixed Kitaru cohort, one change at a time, until it is reliably green.

| Skill | Use it when | Stop rule |
| --- | --- | --- |
| [`kitaru-loop`](kitaru-loop/SKILL.md) | **Default for new loops.** You want a criterion you can defend: several trials per round and a consistency check. | Configurable: n trials per round, pass^k, a per-case floor, no regression against round 0, confirmation on fresh trials, a round cap and a budget. See [criteria.md](kitaru-loop/references/criteria.md). |
| [`experiment-loop`](experiment-loop/SKILL.md) | You want the simple recipe: one baseline run, then one change per iteration. | One configuration passes every case in two consecutive runs, or the iteration cap is hit. |

Both skills apply each change as an experiment override on the same registered agent version, so the Kitaru UI keeps showing `v1` and the change appears in the Overrides column.

## Install

Claude Code loads skills from `~/.claude/skills/` (for all your projects) or from `<project>/.claude/skills/` (for one project). Clone this repo and symlink the skills so a `git pull` keeps them up to date:

```bash
git clone https://github.com/alyssa43/loop-skills.git ~/loop-skills
```

```bash
mkdir -p ~/.claude/skills && ln -s ~/loop-skills/kitaru-loop ~/loop-skills/experiment-loop ~/.claude/skills/
```

Then ask Claude Code to run a loop, or call one by name, such as `/kitaru-loop`.

## Requirements

- The `kitaru` CLI, logged in to your server. Point the config at the project's virtualenv binary, for example `.venv/bin/kitaru`.
- Kitaru's Define step finished: an accepted cohort version and at least one registered evaluator. If you don't have these yet, start with the `kitaru-investigation` skill.
- Python 3. `scripts/loop.py` uses only the standard library.

## What's inside each skill

- `SKILL.md`: the instructions Claude follows.
- `scripts/loop.py`: the runner. It creates the experiments, applies the stop rule and writes the evidence.
- `references/`: the config schema with a worked example, plus guidance on which change to try next.
- `evals/`: test cases for the skill itself.
- `agents/openai.yaml`: display metadata.

The worked examples use the support-desk-demo agent, cohort and evaluator IDs. Replace them with your own.

## Where the output goes

`kitaru-loop` reads `kitaru-loop.json` in the project root and writes evidence to `sessions/kitaru-loop/` by default. Most Kitaru projects git-ignore `sessions/`, so the evidence stays on the machine that ran the loop unless you share it some other way.
