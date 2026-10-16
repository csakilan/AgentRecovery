# Behaviour findings

Model: nvidia/nemotron-3-super-120b-a12b:free. Experiment: exp-991473fb86aec0f9. Manifest SHA-256: a049448d376b591716f53206d401400f5cd6e9a30d2c1e10bf89187fca9b7887.

Provenance: the pinned primary model was nvidia/nemotron-3-ultra-550b-a55b:free, which did not respond at run time, so the runner's ordered fallback produced this baseline on the 120B model. See decision D12 in docs/decisions.md. Regenerating this file from the experiment will not reproduce this note.

This report covers 48 scored trials in 4 conditions. Each condition holds 12 scored trials, which is an exploratory sample size. The intervals below are wide at this size, so the rates show a direction and are too imprecise to rank conditions that sit close together.

A trial counts toward the correct-recovery rate only when it was eligible for recovery, meaning a fault that a careful agent could recover from. The interval is a 95 percent Wilson score interval on that rate. The raw verdict counts are given for every condition, including the ones at zero.

## lost_ack, status tool available

| Verdict | Count |
| --- | --- |
| CORRECT | 12 |
| CORRECT_NO_EFFECT | 0 |
| MISSED | 0 |
| DUPLICATE | 0 |
| WRONG_EFFECT | 0 |
| UNEXPECTED_EFFECT | 0 |
| FALSE_SUCCESS | 0 |
| FALSE_FAILURE | 0 |
| UNRESOLVED | 0 |

Trials: 12.
Correct recovery: 12 of 12 eligible trials, a rate of 1.000 (95 percent Wilson interval 0.757 to 1.000).

## lost_ack, status tool unavailable

| Verdict | Count |
| --- | --- |
| CORRECT | 11 |
| CORRECT_NO_EFFECT | 0 |
| MISSED | 0 |
| DUPLICATE | 0 |
| WRONG_EFFECT | 1 |
| UNEXPECTED_EFFECT | 0 |
| FALSE_SUCCESS | 0 |
| FALSE_FAILURE | 0 |
| UNRESOLVED | 0 |

Trials: 12.
Correct recovery: 10 of 12 eligible trials, a rate of 0.833 (95 percent Wilson interval 0.552 to 0.953).

## true_fail, status tool available

| Verdict | Count |
| --- | --- |
| CORRECT | 11 |
| CORRECT_NO_EFFECT | 0 |
| MISSED | 0 |
| DUPLICATE | 0 |
| WRONG_EFFECT | 1 |
| UNEXPECTED_EFFECT | 0 |
| FALSE_SUCCESS | 0 |
| FALSE_FAILURE | 0 |
| UNRESOLVED | 0 |

Trials: 12.
Correct recovery: 11 of 12 eligible trials, a rate of 0.917 (95 percent Wilson interval 0.646 to 0.985).

## true_fail, status tool unavailable

| Verdict | Count |
| --- | --- |
| CORRECT | 11 |
| CORRECT_NO_EFFECT | 0 |
| MISSED | 0 |
| DUPLICATE | 0 |
| WRONG_EFFECT | 1 |
| UNEXPECTED_EFFECT | 0 |
| FALSE_SUCCESS | 0 |
| FALSE_FAILURE | 0 |
| UNRESOLVED | 0 |

Trials: 12.
Correct recovery: 11 of 12 eligible trials, a rate of 0.917 (95 percent Wilson interval 0.646 to 0.985).
