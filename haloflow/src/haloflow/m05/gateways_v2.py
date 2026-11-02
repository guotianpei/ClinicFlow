"""L-1W W3a M05 v2 action gateways t030 / t031 (contract v1 §4, mechanism note §4,
Codex 215519Z; red gate ACCEPT at 6884cb6).

`m05_prepare_action_v2` / `m05_apply_action_v2` are the v1 template (t015/t016)
with exactly four changes, each applied by an exact-count replacement so the v1
bodies are untouched:

1. Membership standing comes from `m01_staff_member_standing` (t028, FOR SHARE) for
   the enumeration set {actor} plus the command assignee for assign/reassign, in
   ascending staff_id order, BEFORE the item lock (global order: membership ->
   item). `m01_staff_assignable` is no longer called.
2. The actor's standing check (step 4b) reads that standing; it is still decided
   at step 4b, so refusal precedence is unchanged.
3. `supervise` = the actor's supervisor role row (through t028) AND the caller's
   flag: the flag can only narrow.
4. Target eligibility (step 6e) reads the assignee's standing taken in (1).

Owner and execution role `haloflow_m05_owner`; EXECUTE to `haloflow_runtime` only.
W3a keeps v1 callable (W3b closes it).
"""

import re
from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.units import UnitDefinitions
from haloflow.m05.gateways import (
    _ACTION_TEMPLATE,
    _REFUSE_APPLY,
    _REFUSE_PREPARE,
    _REPLAY_APPLY,
    _REPLAY_PREPARE,
    _RESOLVE_APPLY,
    _RESOLVE_CHALLENGE_PREPARE,
    _RESOLVE_PAYLOAD_APPLY,
    _RESOLVE_SHAPE_APPLY,
    _RESOLVE_SHAPE_PREPARE,
    _TAIL_APPLY,
    _TAIL_PREPARE,
    _definition,
    _profile,
)

_V1_SUPERVISE: Final = "(v_perm OPERATOR(pg_catalog.->) 'supervise') <> 'true'::jsonb"

# (old, new, exact occurrence count in the v1 template)
_V2_EDITS: Final[tuple[tuple[str, str, int], ...]] = (
    (
        "    v_constraint text;\n",
        "    v_constraint text;\n"
        "    v_actor_standing jsonb;\n"
        "    v_target_standing jsonb;\n"
        "    v_supervise boolean;\n",
        1,
    ),
    (
        "    -- 3. The item lock;",
        "    -- 2b. W3a: membership standing through t028 (FOR SHARE), ascending staff_id,\n"
        "    -- before the item lock. Enumeration set: the actor, plus the command\n"
        "    -- assignee for assign/reassign (a target equal to the actor is taken once).\n"
        "    IF v_assignee IS NOT NULL AND v_assignee < v_actor THEN\n"
        "        v_target_standing := {schema}.m01_staff_member_standing(v_assignee);\n"
        "        v_actor_standing := {schema}.m01_staff_member_standing(v_actor);\n"
        "    ELSE\n"
        "        v_actor_standing := {schema}.m01_staff_member_standing(v_actor);\n"
        "        IF v_assignee IS NOT NULL AND v_assignee <> v_actor THEN\n"
        "            v_target_standing := {schema}.m01_staff_member_standing(v_assignee);\n"
        "        END IF;\n"
        "    END IF;\n"
        "    -- supervise = the supervisor role row AND the caller's flag (narrows only).\n"
        "    v_supervise := COALESCE((v_actor_standing OPERATOR(pg_catalog.->>)\n"
        "            'supervisor')::boolean, false)\n"
        "        AND (v_perm OPERATOR(pg_catalog.->) 'supervise') = 'true'::jsonb;\n"
        "\n"
        "    -- 3. The item lock;",
        1,
    ),
    (
        "    IF NOT {schema}.m01_staff_assignable(v_actor) THEN",
        "    IF NOT COALESCE((v_actor_standing OPERATOR(pg_catalog.->>)\n"
        "            'assignable')::boolean, false) THEN",
        1,
    ),
    (_V1_SUPERVISE, "NOT v_supervise", 4),
    (
        "       AND NOT {schema}.m01_staff_assignable(v_target) THEN",
        "       AND NOT COALESCE((v_target_standing OPERATOR(pg_catalog.->>)\n"
        "            'assignable')::boolean, false) THEN",
        1,
    ),
)


def _v2_template() -> str:
    template = _ACTION_TEMPLATE
    for old, new, count in _V2_EDITS:
        if template.count(old) != count:
            raise AssertionError(f"v1 template edit point moved: {old!r}")
        template = template.replace(old, new)
    return template


def _action_body_v2(mode: str) -> str:
    apply = mode == "apply"
    refuse = _REFUSE_APPLY if apply else _REFUSE_PREPARE
    body = _v2_template().replace("@@TAIL@@", _TAIL_APPLY if apply else _TAIL_PREPARE)
    body = body.replace("@@RESOLVE_APPLY@@", _RESOLVE_APPLY)
    body = body.replace("@@RESOLVE_SHAPE@@",
                        _RESOLVE_SHAPE_APPLY if apply else _RESOLVE_SHAPE_PREPARE)
    body = body.replace("@@RESOLVE_PAYLOAD@@", _RESOLVE_PAYLOAD_APPLY if apply else "")
    body = body.replace("@@RESOLVE_CHALLENGE@@", "" if apply else _RESOLVE_CHALLENGE_PREPARE)
    body = body.replace("@@REPLAY@@", _REPLAY_APPLY if apply else _REPLAY_PREPARE)
    body = re.sub(r"@@REFUSE:([A-Z_]+)@@", lambda m: refuse.format(code=m.group(1)), body)
    if "@@" in body or not body.isascii() or "m01_staff_assignable" in body:
        raise AssertionError("v2 body rendering incomplete")
    return body


_PREPARE_V2_BODY: Final = _action_body_v2("prepare")
_APPLY_V2_BODY: Final = _action_body_v2("apply")
_PREPARE_V2_BODY_SHA256: Final = "e1cc76b143ca817104d1c77d31919b9294d06601b690ba53a1a32118290965d1"
_APPLY_V2_BODY_SHA256: Final = "8a8b7aab78da7b970180b94bb94c5f00fdbdf72a3c5114147d912bc1b91e682c"

_FUNCTIONS: Final = (
    ("t030_m05_prepare_action_v2", "m05_prepare_action_v2", _PREPARE_V2_BODY,
     _PREPARE_V2_BODY_SHA256),
    ("t031_m05_apply_action_v2", "m05_apply_action_v2", _APPLY_V2_BODY,
     _APPLY_V2_BODY_SHA256),
)
_PARAMS: Final = (("p_command", "jsonb"),)

M05_V2_UNITS: Final[UnitDefinitions] = MappingProxyType(
    {mid: _definition(name, _PARAMS, "volatile", body, sha)
     for mid, name, body, sha in _FUNCTIONS}
)
M05_V2_PROFILES: Final = MappingProxyType(
    {mid: _profile(name, _PARAMS, "volatile") for mid, name, _body, _sha in _FUNCTIONS}
)
