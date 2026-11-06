"""L-1S package S-C: unit t046 `m05_activation_probe()` (D-S1 read authority).

Governing: S-C requirements v1, architecture v1 A4, red gate PASS at c610550 (Codex
20261106T214654Z). The runtime may learn ONLY whether follow-up is active for the tenant:
`{"schema_version": 1, "active": bool}`, from the latest activation record (the t043
rule, `ORDER BY r.created_at DESC, r.record_id DESC LIMIT 1`); no record -> inactive.
t042/t043 stay owner-only; the runtime gains no write and no other read.

NOT composed into production: installs only through the S-C TEST registry
(tests/m05/sc_support.py, target 46); the production target stays 36.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m05.intake_units import _definition, _profile

T046_MIGRATION_ID: Final = "t046_m05_activation_probe"
PROBE_NAME: Final = "m05_activation_probe"

_PROBE_BODY: Final = r"""
DECLARE
    v_change text;
BEGIN
    SELECT r.change INTO v_change FROM {schema}.m05_activation_records AS r
     ORDER BY r.created_at DESC, r.record_id DESC LIMIT 1;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1,
        'active', COALESCE(v_change OPERATOR(pg_catalog.=) 'grant', false));
END;
"""

SC_TENANT_MIGRATIONS: Final = MappingProxyType({
    T046_MIGRATION_ID: _definition(PROBE_NAME, (), "stable", _PROBE_BODY, True),
})
SC_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    T046_MIGRATION_ID: _profile(PROBE_NAME, (), "stable", True),
})
