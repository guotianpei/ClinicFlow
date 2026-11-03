"""The authenticated HaloFlow platform entry point (L-1W).

Requirements v2 + v2r1 + v2r2 (Codex ALIGN 9df98dec), architecture v1 + v2 + addendum 1
(mechanisms ALIGN 3f814376), package v1 + v1r1 + the W2/W3 consolidated investigation.

Tranche W1 (this package so far): the access-token verifier (`verify`) and the
key-set client (`keys`). Unit tier only; no database, no HTTP routes yet.

Non-claims: nothing here is wired into an application entry point yet, no identity
provider is chosen (owner RQ-B1: decided before the pilot), and L-1W stays a release
hold until selected-provider validation.
"""
