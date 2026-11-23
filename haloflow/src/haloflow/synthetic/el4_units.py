"""Eligibility E4 / OD-EL12 (Rachel 2026-11-22): unit t127, the synthetic contact-history read.
SYNTHETIC ONLY: eligibility install profile only (target 127).

`m05_el_contact_history(p_item_id uuid) RETURNS jsonb` (STABLE, SECURITY DEFINER, owned by the
M05 owner, runtime EXECUTE only): the OD-EL7 contact entries of ONE eligibility item of this
tenant, in history order:
    {"schema_version": 1, "entries": [{"seq", "contact_code", "actor_id", "at"}]}
An unknown item or an item of another kind -> {"schema_version": 1, "error": "ITEM_NOT_FOUND"}.
The standard tenant check applies (cross-tenant isolation is the gateway's tenant-to-schema
binding, as for every M05 read). No write, no grant change: the runtime role still has no
SELECT on `m05_contact_entries` (E3b); this function is the only read path.
Built with the B5 display-unit builder (`display_units._definition` / `_profile`).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.units import UnitDefinitions
from haloflow.m05 import gateways as _gw
from haloflow.synthetic.display_units import _definition, _profile

CONTACT_HISTORY_ID: Final = "t127_m05_el_contact_history"
_EL_KINDS: Final = "('eligibility_inactive', 'eligibility_inconclusive', 'eligibility_manual')"

_BODY: Final = r"""
DECLARE
    v_setting text;
    v_entries jsonb;
BEGIN""" + _gw._TENANT_CHECK + r"""
    IF p_item_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM {schema}.m05_followup_items AS i
         WHERE i.item_id = p_item_id AND i.item_kind IN """ + _EL_KINDS + r""") THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'error', 'ITEM_NOT_FOUND');
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'seq', h.seq, 'contact_code', c.contact_code,
               'actor_id', h.actor_id::text,
               'at', pg_catalog.to_char(pg_catalog.timezone('UTC', h.created_at),
                                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
               ORDER BY h.seq), '[]'::jsonb)
      INTO v_entries
      FROM {schema}.m05_followup_history AS h
      JOIN {schema}.m05_contact_entries AS c ON c.history_id = h.history_id
     WHERE h.item_id = p_item_id AND h.action = 'log_contact';
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'entries', v_entries);
END;
"""

_PARAMS: Final = (("p_item_id", "uuid"),)

EL4_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType({
    CONTACT_HISTORY_ID: _definition("m05_el_contact_history", _PARAMS, "jsonb", "stable",
                                    _BODY)})
EL4_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    CONTACT_HISTORY_ID: _profile("m05_el_contact_history", _PARAMS, "jsonb", "stable")})
