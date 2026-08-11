# W3 visual — design_review_delay sample 1

## Gate-1 (cross-app rollback saga): True
- apps touched: 2
- saga reverted: 3/3
- rollback fidelity: 1.0
- non-interference-on-rollback: True
- cross-app consistency (post-dispatch): 1.0

## Gate-2 (reconciliation): True
- conflict detected: True
- n conflicts: 1
- both values shown (no silent overwrite): True
- merge options present: True
- amber rendered: True
- agent not blocked: True

## Steps (reproducible)
1. open `1_after_dispatch.html` — multi-app edit applied (calendar+taskboard changed).
2. open `2_after_undo.html` — saga undo → ALL touched apps reverted byte-identical.
3. open `3_reconcile_before_external.html` — user's projection Y cached.
4. open `4_reconcile_after_external.html` — external change → AMBER conflict shows both Y + X + merge options (no silent overwrite, agent not blocked).
