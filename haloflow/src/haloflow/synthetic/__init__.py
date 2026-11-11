"""HaloFlow internal SYNTHETIC application (Option B). Synthetic data only.

Owner basis: Option B build approved 2026-11-11, for the build only. Deployment, external
demos, real data, live messaging, real EHR actions and release of any operational hold are
NOT authorized by it.

Fence: nothing reachable from production (`haloflow.platform`, `haloflow.startup`,
`haloflow.composition`, `haloflow.main`) may import this package. That is enforced by
tests/synthetic (B1-C1 static transitive closure, B1-C1r runtime). Production startup
refuses a synthetic (target-94) tenant (B1-C3).
"""
