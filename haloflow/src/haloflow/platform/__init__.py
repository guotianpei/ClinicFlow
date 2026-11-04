"""The authenticated HaloFlow platform entry point (L-1W).

Requirements v2 + v2r1 + v2r2 (Codex ALIGN 9df98dec), architecture v1 + v2 + addendum 1
(mechanisms ALIGN 3f814376), package v1 + v1r1 + the W2/W3 consolidated investigation.

Tranche W1: the access-token verifier (`verify`) and the key-set client (`keys`).
Tranche W4a: configuration (`config`), per-request admission (`admission`), the
boundary literals and outermost gate (`http`), and the ASGI app with its lifespan
(`app`; routes `/healthz` and `/v1/session/logout` only). Tenant routes are W4b.

Non-claims: no tenant route is served yet (W4b), no identity
provider is chosen (owner RQ-B1: decided before the pilot), and L-1W stays a release
hold until selected-provider validation.
"""
