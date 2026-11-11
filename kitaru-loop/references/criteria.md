# Why the criteria are what they are

Read this before changing a default, when the user asks "why n trials" or "why not just two greens", or when explaining the Loop's step 2 to a team.

## What varies, and what pass^k measures

Repeated trials are about the **agent's** non-determinism, not the evaluator's. A deterministic rule can grade a trace exactly, and the trace still differs from run to run because the model's choice differed. So every binary evaluator, rule or LLM judge, is scored with pass^k. The evaluator's type only changes what the variance means: with a rule, all of it is the agent's; with an unvalidated judge, some of it is the judge's.

Two standard estimators from c successes in n trials of one case:

```
pass@k = 1 - C(n-c, k) / C(n, k)    at least one of k attempts succeeds   (Chen et al., 2021, Codex)
pass^k =     C(c, k)   / C(n, k)    all k attempts succeed                (Yao et al., 2024, tau-bench)
```

Both are unbiased; both need n ≥ k. pass@k rewards getting lucky once, right for "generate code and keep the one that compiles". pass^k punishes inconsistency, right for an agent that acts, because a customer gets one attempt. A case that passed 1 of 3 trials has pass@3 = 1.00 and pass^2 = 0.00.

Numeric metrics (cost, latency) do not get pass^k. They get a threshold on the round mean.

## What "two consecutive greens" measures

With 20 cases it is a 40-trial zero-failure test. If every case independently passes 95% of the time, two greens happen 13% of the time; at 98%, 45%. So the rule mostly rejects decent candidates by luck. And passing it bounds the per-trial pass rate at only about 93% (95% confidence): the **rule of three** says zero failures in N trials bounds the failure rate at roughly 3/N.

| Zero failures in N trials | 95% lower bound on pass rate |
| ---: | ---: |
| 2 | 0.22 |
| 5 | 0.55 |
| 10 | 0.74 |
| 20 | 0.86 |
| 40 | 0.93 |
| 60 | 0.95 |
| 115 | 0.97 |

Certifying high per-case reliability needs sample sizes nobody runs per case. The achievable goals are: detect gross inconsistency (pass^k), detect improvement and regression relative to round 0 (paired), and say what the trial count bounds.

## The per-case floor, and why strict is not the default

Floors compound across cases. A candidate that is 95% reliable on every case clears "every case n of n" at n=5 on 20 cases only 0.6% of the time; at 99% per case, 37%. The floor gets harsher as the cohort grows.

| per-case p | strict (n/n), n=5, 20 cases | allow one miss (≥ n−1), n=5, 20 cases |
| ---: | ---: | ---: |
| 0.99 | 0.37 | 0.98 |
| 0.97 | 0.05 | 0.84 |
| 0.95 | 0.006 | 0.63 |

"Allow one miss" keeps the floor's purpose (one flaky case cannot hide in a good average) without rejecting good candidates most of the time. A single miss still drops that case's pass^3 to 0.40 at n=5 and shows in the report. Use strict only when one miss is unacceptable for the product and the team accepts many rejected rounds.

## No regression, paired by case

Compare each case's pass rate in the round against its pass rate in round 0. A case that was 5/5 in round 0 and 5/5 now is unchanged; it carries no evidence about the change. A case that went 0/5 to 5/5 is the effect of the change (p of that by chance when the true rates are 0.3 and 0.95 is about 0.8 to detect, so n=5 is enough to see a real fix). A case that went 5/5 to 3/5 is a regression and fails the gate even if the cohort mean rose.

## Confirmation on fresh trials

Iterating until something goes green means looking at the dice repeatedly; the round that finally passes is partly selected by luck (the garden of forking paths). The fix is to re-run the winner with n new trials that were not used to choose it and require the gate again on those alone. This is what "green twice in a row" was intuitively doing, with the second look made as large as the first.

## Plateau

The round cap and the budget bound spend; they do not notice that the last few rounds went nowhere. `plateau_patience` is early-stopping patience over rounds 1 and up: each round gets a comparable first-look score, the number of gate passes in its first run (the screen), with mean pass^k as a tiebreak when the round got full trials. Track the best score so far; when `plateau_patience` consecutive new rounds fail to beat it, stop with outcome "plateau". Default 2: one round that does not help is normal, two in a row says the hypothesis or the lever is wrong. `null` turns it off. Round 0 is not an attempt and never counts.

Two companion notes that `next` reports but does not act on:

- **Oscillation.** If the failing set alternates (A, B, A) across three rounds, fixing one case keeps breaking another. The score never improves, so the plateau guard fires; the note explains that this is a trade-off between cases, not a dead end, and the report should show both failure sets.
- **Stuck case.** If the same case is red in three consecutive rounds, the current change lever is not reaching it; consider the next lever (parameters, then model). The lever choice stays with the human.

## Screening

Each new round runs once first. Any red gate case ends the round: with the floor at "allow one miss" a round could in principle survive one miss, but the screen is deliberately stricter so that money goes to candidates that at least pass cleanly once. Round 0 is never screened; it is an audit, and its misses are the information.

## Choosing n and k

- n: under about 10 cases, 10 (cheap and you need the resolution); 20 to 50 cases, 5; hundreds of cases, 3 plus the screen. Cost per full round = cohort cost per run × n; `preflight` prints it.
- k: a product question, "how many times in a row must this work for us to trust it". 3 is a sensible default for a support agent. k ≤ n.
- Cohort target on mean pass^k: 0.9 is a reasonable default; it is a secondary check once the floor and no-regression hold. It interacts with cohort size: one miss at n=5 contributes pass^3 = 0.40 for that case, which drops the mean to 0.97 on 20 cases but to 0.80 on 3 cases. Below about 7 cases, lower the target or rely on the floor alone, otherwise the target is stricter than "allow one miss" was meant to be.

## Judges

An LLM judge gating the loop should be validated against human verdicts first (the `kitaru-validate-evaluator` skill). Until then mark it `"judge": true` without `"validated": true`; preflight notes it and the report should say that part of the measured variance may be the judge's.

## Independence caveats

The cohort-level arithmetic treats cases as independent. Cases that share an order, a customer, or a tool fixture are not, so cohort-wide numbers overstate certainty slightly; per-case numbers are unaffected. Trials are independent only if the model is sampled (temperature above zero); at temperature 0 repeated trials still vary for hosted models but less, so n buys less. A hosted model alias can change between rounds; the run-spec hash does not detect that.
