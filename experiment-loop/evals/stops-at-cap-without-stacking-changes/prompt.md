---
tags: [experiment-loop]
max_turns: 40
timeout_seconds: 1800
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

keep iterating on my refund agent on kitaru until it stops failing, you know the drill: baseline, then tweak the prompt or swap the model one thing at a time. experiment-loop.json is already in the repo root. only go 3 iterations max this time though, and if it still isn't green twice by then just tell me what you tried and what each attempt scored, don't try to fix two things in one go to squeeze it in
