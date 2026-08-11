# W4 JVM-moment visual — 20260807_191356

## Gate-1 (substrate-invariance): True
- interface stable (both show release_date=2026-08-18): True
- semantics consistent (dependent deadlines sync in both): True
- trajectory differs (different operator): True
  - Stack A op: move_event (calendar.E1.date via move_event)
  - Stack B op: reschedule_appointment (outlook_cal.A1.scheduled_for via reschedule_appointment)
- Stack A dep-tracking: 1.0 | Stack B: 1.0

## Steps
1. open `stack_a_calendar.html` — release_date moved to 8/18 via calendar.move_event.
2. open `stack_b_outlook_cal.html` — SAME task variable + value, but via outlook_cal.reschedule_appointment (different substrate, same semantics).
