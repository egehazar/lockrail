# Lockrail eval report

Generated: 2026-05-17T14:18:04+00:00

## Headline numbers

- **Unsafe writes**: 23.6% → 0.0% (33 → 0 of 140 scenarios).
- **Duplicate prevention**: 95.0% of 200 replays prevented (10 distinct executions).
- **Task completion (dedicated set)**: 61.0% → 81.0% (+20.0pp; 20 recoveries via evidence-retry; 100 scenarios).
- **Task completion (standard set)**: 76.4% → 85.7% (+9.3pp; 13 recoveries via evidence-retry; 140 scenarios).

## Unsafe writes

Total scenarios: 140. Of these, 33 are flagged `expected_safe=False`.

| Category | Total | Baseline unsafe | Treatment unsafe |
|----------|-------|-----------------|-------------------|
| crm | 50 | 8 | 0 |
| email | 30 | 5 | 0 |
| refund | 60 | 20 | 0 |

## Duplicate prevention

- 10 replay scenarios fired 200 total submissions.
- Without IdempotencyGate: 200 distinct executions (every submission ran).
- With IdempotencyGate: 10 distinct executions, 190 prevented (95.0%).

## Task completion — dedicated set

100-scenario set designed specifically for the completion metric: 61 trivial / 20 evidence-recoverable / 19 contract-unfixable. This is the bullet the resume's 61% → 81% number refers to.

Total scenarios: 100. Baseline completed: 61; treatment completed: 81 (20 via evidence-deny + retry).

| Category | Total | Baseline completed | Treatment completed |
|----------|-------|--------------------|----------------------|
| crm | 32 | 20 | 26 |
| email | 27 | 16 | 22 |
| refund | 41 | 25 | 33 |

## Task completion — standard set

140-scenario set shared with the unsafe-writes metric. The completion delta here is the gate's incidental contribution on a safety-focused distribution, not the headline.

Total scenarios: 140. Baseline completed: 107; treatment completed: 120 (13 via evidence-deny + retry).

| Category | Total | Baseline completed | Treatment completed |
|----------|-------|--------------------|----------------------|
| crm | 50 | 42 | 46 |
| email | 30 | 25 | 30 |
| refund | 60 | 40 | 44 |

## Methodology

See `evals/README.md` for the full methodology — what baseline + treatment runtimes look like for each metric, what counts as success, and the honest framing of what these numbers do and don't prove.
