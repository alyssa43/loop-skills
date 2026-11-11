---
tags: [experiment-loop]
max_turns: 40
timeout_seconds: 1800
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

I'm in my support-desk-demo checkout and logged in to my Kitaru server with .venv/bin/kitaru. The agent is support-desk-demo@1, the cohort version is 01a1c71c-abff-7454-8f31-2808a19b9f10, and the evaluator is support-desk-action-correct@1 (python -m support_desk.kitaru_args answer-key refund prints its params, and tool-policy refund prints the tool policy). Run my experiment loop: baseline first, then change one thing at a time until something is green twice. Allowed models are gpt-4.1-mini, gpt-4.1 and gpt-5-mini, cap it at 6, and don't ask me between iterations.
