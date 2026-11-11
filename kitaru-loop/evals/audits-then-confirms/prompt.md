---
tags: [kitaru-loop]
max_turns: 60
timeout_seconds: 3600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

I'm in my support-desk-demo checkout, logged in to Kitaru with .venv/bin/kitaru. Agent support-desk-demo@1, cohort version 01a1c71c-abff-7454-8f31-2808a19b9f10, evaluator support-desk-action-correct@1 (python -m support_desk.kitaru_args answer-key refund prints its params, tool-policy refund prints the tool policy). Set up a loop called "Day-30 refunds" with the hypothesis that the agent reads the 30-day window as exclusive. Use 5 trials per round, k=3, allow one miss, confirmation on, cap 4 rounds, budget $1. Allowed models gpt-4.1-mini and gpt-4.1. Run it without asking me between rounds and tell me what each change did.
