# Choosing the one change for the next round

The loop's value is attribution: when a round confirms, you want to say which single change did it. That only holds if each round differs from the previous one by exactly one idea, and the idea came from evidence rather than from a prompt that was already known to work.

## Start from round 0's audit and the last round's reds

`next` prints the round-0 classification (consistent fail, flaky, consistent pass) and, after a failed round, which cases were red in which runs. `collect` prints each red case with evaluator explanations and the agent's reply. Read the replies. Ask:

1. What did the agent believe about the case? It usually says so in the reply.
2. Which instruction or tool result did it misread? Is the instruction ambiguous, or merely unstated?
3. Was the case consistently failing or flaky in round 0? A flaky case means the instruction is understood sometimes; make it impossible to misread rather than adding emphasis.
4. Does the hypothesis in the config still fit what the replies show? If not, say so before changing anything; a loop that drifts away from its hypothesis is still allowed, but the report must say which round the hypothesis changed.

Write the diagnosis in one sentence. It becomes `--change` and the round table row.

## Pick the smallest intervention

In order of preference, within `allowed_changes`:

1. **Clarify the misread instruction.** Add or reword one sentence so the specific misreading is impossible. Keep the rest of the prompt byte-identical to the previous round's.
2. **Add one deterministic condition** the agent can evaluate from tool output, when clarification held sometimes but not always.
3. **Change a model parameter** (for example lower temperature) when the round-0 audit shows flakiness on cases the prompt already states plainly. Keep the prompt identical.
4. **Change the model**, from the allow-list, keeping the prompt identical, when the prompt is already plain and the smaller model still misapplies it, or fixing one case keeps breaking another.
5. Larger structural changes (new tools, retrieval, multi-step flows) are outside a one-change loop. Stop and report instead.

## Write the change as a file

For a prompt change, create `prompts/<route>_round<N>.md` by copying the prompt the previous round used and applying the one edit. The diff between consecutive files should read as a single idea. Pass it with `--prompt-file`. Never edit a prompt file an earlier round used.

## Keep it honest

- No case IDs, expected answers, or evaluator vocabulary in a prompt. The green must come from the policy being clearer, not from the prompt knowing the test.
- If an earlier project found the same fix, it is fine to arrive there again from the evidence; note the coincidence in `--change`.
- When a change fixes the target cases but regresses another, record it; the next round targets the regression or moves to the model lever. Do not stack two edits.
- When the cap or budget stops the loop, the report lists every attempt. That is a finding.
