# Lockrail eval report

Generated: 2026-05-17T13:59:30+00:00

## Headline numbers

- **Unsafe writes**: 23.6% → 0.0% (33 → 0 of 140 scenarios).
- **Duplicate prevention**: 95.0% of 200 replays prevented (10 distinct executions).
- **Task completion**: 76.4% → 85.7% (+9.3pp; 13 recoveries via evidence-retry).

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

## Task completion

Total scenarios: 140. Baseline completed: 107; treatment completed: 120 (13 via evidence-deny + retry).

| Category | Total | Baseline completed | Treatment completed |
|----------|-------|--------------------|----------------------|
| crm | 50 | 42 | 46 |
| email | 30 | 25 | 30 |
| refund | 60 | 40 | 44 |

## Methodology

See `evals/README.md` for the full methodology — what baseline + treatment runtimes look like for each metric, what counts as success, and the honest framing of what these numbers do and don't prove.
