"""Verifier: canonical_state (snapshot/compare), non_interference (hard check),
round_trip_checks (3 checks + AOHP-weighted score). cross_app + reconciliation = W2/W4 stubs.

Honesty invariant: reads ONLY hidden canonical sandbox state; never self-judges."""
