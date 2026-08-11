# W2 visual — design_review_delay sample 0

sid: design_review_delay_w2_s0_78567

## Gate-1 (two-zone manipulable): True
- read-only zone present: True
- read-write zone present: True
- zones independent (ro has no inputs): True
- editable field present: True
- after edit, read-only shows new value: True
- after edit, old value gone from read-only: True

## Gate-2 (single-app single-step rollback): True
- undo app: calendar · entity: E3
- entity reverted byte-identical: True
- non-interference-on-rollback: True
- log emptied: True

## Steps (reproducible)
1. open `1_two_zone_initial.html` — read-only shows design_review_date = old value; cross-check `0_native_initial.html` (native :3013) shows the same.
2. edit design_review_date → 2026-08-22; open `2_two_zone_after_edit.html` — read-only re-syncs to new value; `2_native_after_edit.html` shows the app's real state changed.
3. click undo (calendar); open `3_two_zone_after_undo.html` — read-only reverts; `3_native_after_undo.html` shows the app's real state reverted (compensation via app API).
