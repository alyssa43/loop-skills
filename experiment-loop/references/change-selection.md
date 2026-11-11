# Choosing the one change for the next iteration

The loop's value is attribution: when a run goes green, you want to be able to say which single change did it. That only holds if each iteration differs from the previous one by exactly one idea.

## Read the failure first

`loop.py collect` prints each red case with the evaluator's explanation and the agent's reply. Read the replies, not just the pass/fail. Ask:

1. What did the agent believe about the case? (It usually says so in the reply: "it has been exactly 30 days, so it is outside the window".)
2. Which instruction or tool result did it misread, and is the instruction genuinely ambiguous or just unstated?
3. Did it fail the same way in the baseline, or is this new? A case that flips between runs under an unchanged configuration is variance; a change that targets it may still be worth testing, but say that the baseline was not stable there.

Write the diagnosis in one sentence. It goes into `--description` and the version table.

## Pick the smallest intervention that addresses the diagnosis

In order of preference:

1. **Clarify the ambiguous instruction.** Add or reword one sentence in the system prompt so the specific misreading is impossible. Keep the rest of the prompt byte-identical to the previous iteration. Example: an inclusive boundary read as exclusive gets "day 30 counts as eligible; only day 31 or later is too late".
2. **Add one deterministic condition** the agent can evaluate from tool output, when clarification alone did not hold or the agent reasons inconsistently about the same facts. Example: an explicit eligibility predicate over the order record's fields.
3. **Change the model**, from the allowed list, keeping the prompt file identical to the previous iteration. Do this when the prompt already states the rule plainly and the smaller model still misapplies it, or when fixing one case keeps breaking another.
4. Larger structural changes (new tools, retrieval, multi-step flows) are outside a one-change loop. Stop and report instead.

## Write the change as a file

Create `prompts/<route>_iter<N>.md` by copying the prompt the previous iteration used and applying the one edit. The diff between consecutive files should read as a single idea. Name the file in `--prompt-file` and in the description. Do not edit a prompt file that an earlier iteration already used.

## Keep it honest

- No case IDs, expected answers, or evaluator vocabulary in the prompt. The green must come from the policy being clearer, not from the prompt knowing the test.
- If an earlier project found the same fix, it is fine to arrive there again from the evidence; note the coincidence. Do not start from the old fix and work backwards to a justification.
- When a change fixes the target case but regresses another, that is a real result: record it, and let the next iteration target the regression or revert to the model lever. Do not stack two edits to patch both at once.
- When the cap is reached, the report says so and lists every attempt. A loop that never went green twice is a finding, not a failure of the process.
