"""Incoming fax routing FX-IN, package FX2 — test support (TEST ONLY). RED BY DESIGN.

Governing (Shared Workspace `Work Session 2026-11-25/`): requirements v1r1, architecture v1r2,
test cases v1r1 FX2/FX3 revision (`claude_fax-inbound-test-cases-v1r1-fx2-fx3-2026-11-25.md`,
Codex ALIGN 19629e35, incl. "a line referenced by a rule refuses removal/reassignment
atomically until the rule is removed"), OD-FX6 (open / move / mark filed, no assignment), OD-FX8
(filed is final), FX1 at PR #114 (ee490bd). Synthetic only.

Red classes: C control; A absence-red (`include_fx2` -> TypeError, reported as "FX2 test
installation failed"; the provisioner rows fail on ModuleNotFoundError for
`haloflow.synthetic.fax_lines`).

FX2 interface pinned here (red packet v1, section 2). DEVIATION from architecture v1r2 §8.2,
for Codex: the staff gateways need the acting staff member's status and access, which live in the
staff-owned `m01_staff` / `m01_staff_followup_access` (the M05 owner has no read on them), so FX2
adds a staff-owned check unit (the t022 / t137 pattern). FX2 is therefore t141..t144, target 144
(renumbered after FX1 gained t140 m05_fx_tenant_bound, Codex 3e5361ff). Every FX2 runtime
gateway (t142..t144) refuses a session tenant that is not the schema's tenant (22023 'tenant
context invalid', via t140) before any read or write.

Registry: `build_synthetic_registry(<all EL flags>, include_fx1=True, include_fx2=True)`, ids
FX1 + t141..t144, target **144**; `include_fx2` without `include_fx1` -> ValueError.
- `t141_m05_fx_staff_check`  `m05_fx_staff_check(p_staff_id uuid, p_access text) RETURNS boolean`
  (staff owner; EXECUTE to the M05 owner only): the m01_staff row FOR SHARE, status active, and
  an access row equal to p_access ('read' | 'act'). False otherwise; never raises for those.
- `t142_m05_fx_staff_action` `m05_fx_staff_action(jsonb) RETURNS jsonb` (runtime). Command
  {schema_version 1, action in open|move|file|sort, fax_id, staff_id, attribution (actor;
  principal_id = staff_id), expected_version (move/file/sort), queue_id (move/sort)}. Result
  {schema_version 1, outcome "applied", fax_id, state, queue_id, version}. open: history
  `opened`, version unchanged, any state. move/file/sort bump the version by 1.
  Precedence (test cases v1r1 §1): ACTOR_INACTIVE (staff check: 'read' for open, 'act' for
  move/file/sort; before any fax lock) -> FAX_NOT_FOUND -> state guard (sort: ALREADY_SORTED when
  the fax's sorting item is resolved, NOT_NEEDS_SORTING when it never needed sorting; move/file:
  FAX_FILED when filed, NOT_ROUTED when needs_sorting) -> VERSION_CONFLICT -> QUEUE_NOT_FOUND
  (unknown, inactive or another tenant's queue). Lock order fax -> sorting -> queue (FOR SHARE).
- `t143_m05_fx_staff_read` `m05_fx_staff_read(jsonb) RETURNS jsonb` (runtime, STABLE). Command
  {schema_version 1, view in list|get|history, staff_id, attribution, queue_id (list; null =
  the needs-sorting list), fax_id (get/history)}. ACTOR_INACTIVE ('read'), FAX_NOT_FOUND,
  QUEUE_NOT_FOUND. Fax summaries carry fax_id, state, queue_id, reason (code), pages,
  received_at, version, sender_last4 — never the full sender, line, doc_ref or provider id.
- `t144_m05_fx_lines_snapshot` `m05_fx_lines_snapshot(jsonb) RETURNS jsonb` (runtime, STABLE):
  {schema_version 1, lines: [sorted canonical lines]} of this tenant.
Provisioner line step (offline; NOT a unit): `haloflow.synthetic.fax_lines`
  `provision_lines(conninfo: str, ops: Sequence[LineOp]) -> None`, `LineOp(op: "add"|"remove",
  tenant_id: str, line: str)`; one transaction on the installer's admin connection:
  `pg_try_advisory_xact_lock(K_FX_LINES)` (else `LineProvisionRefused("LINE_PROVISION_BUSY")`,
  nothing written); after the lock, every op validated against ALL tenants' `m05_fx_lines`
  (`LINE_DUPLICATE` when the line would be in two tenants or is added twice; `LINE_NOT_FOUND` for
  a remove of an absent line; `LINE_IN_USE` for a remove while a rule references the line
  (Codex FK point); `TENANT_UNKNOWN`), then applied; any refusal rolls back the whole call.
  `LineProvisionRefused.code` carries the code.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any, Final

import fx1_support as fx
import m02_support
import sa_support as sa

FX2_TARGET: Final = 144
FX2_SUPPORTED: Final = range(FX2_TARGET, FX2_TARGET + 1)
FX2_IDS: Final = ("t141_m05_fx_staff_check", "t142_m05_fx_staff_action",
                  "t143_m05_fx_staff_read", "t144_m05_fx_lines_snapshot")
CHECK: Final = "m05_fx_staff_check"
ACTION: Final = "m05_fx_staff_action"
READ: Final = "m05_fx_staff_read"
SNAPSHOT: Final = "m05_fx_lines_snapshot"
STAFF_OWNER: Final = "haloflow_staff_owner"


def fx2_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(**fx.eligibility_flags(), include_fx1=True,
                                    include_fx2=True)  # type: ignore[call-arg]


class FX2Install(fx.FX1Install):
    """A synthetic tenant at target 144 (FX1 140 + FX2), provisioned in the row body."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, fx2_registry(), self.tenant,
                                           supported=FX2_SUPPORTED, manifest=staff_manifest())
                sa.admin(self, fx.FX_TEST_PROFILE_SQL.format(schema=self.schema))
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"FX2 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[FX2Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield FX2Install(ids, tenant)
    finally:
        reset(tenant)


# --- staff (FIXTURE: privileged rows; the staff admin gateway is covered by its own suite) ---


def seed_staff(install: sa.SAInstall, *, access: tuple[str, ...] = ("read", "act"),
               active: bool = True) -> str:
    staff_id = str(uuid.uuid4())
    sa.admin(install, sa.q(install, "INSERT INTO {s}.m01_staff (staff_id, status,"
                                    " status_changed_at, display_name) VALUES (%s, %s,"
                                    " clock_timestamp(), 'Fax Staff')"),
             (staff_id, "active" if active else "inactive"))
    for value in access:
        sa.admin(install, sa.q(install, "INSERT INTO {s}.m01_staff_followup_access (staff_id,"
                                        " access, granted_at) VALUES (%s, %s, clock_timestamp())"),
                 (staff_id, value))
    return staff_id


def deactivate_staff(install: sa.SAInstall, staff_id: str) -> None:
    sa.admin(install, sa.q(install, "UPDATE {s}.m01_staff SET status = 'inactive',"
                                    " status_changed_at = clock_timestamp() WHERE staff_id = %s"),
             (staff_id,))


# --- commands -------------------------------------------------------------------------------


def action_cmd(action: str, fax_id: str, staff_id: str, **fields: Any) -> dict[str, Any]:
    return {"schema_version": 1, "action": action, "fax_id": fax_id, "staff_id": staff_id,
            **fields, "attribution": sa.attribution(staff_id, kind="actor")}


def read_cmd(view: str, staff_id: str, **fields: Any) -> dict[str, Any]:
    return {"schema_version": 1, "view": view, "staff_id": staff_id, **fields,
            "attribution": sa.attribution(staff_id, kind="actor")}


def act(install: sa.SAInstall, action: str, fax_id: str, staff_id: str, **fields: Any
        ) -> dict[str, Any]:
    return sa.call(install, ACTION, action_cmd(action, fax_id, staff_id, **fields))


def read(install: sa.SAInstall, view: str, staff_id: str, **fields: Any) -> dict[str, Any]:
    return sa.call(install, READ, read_cmd(view, staff_id, **fields))


def version(install: sa.SAInstall, fax_id: str) -> int:
    return int(fx.fax(install, fax_id)["version"])


def routed_fax(install: sa.SAInstall, queue: str = "Imaging") -> str:
    """A fax routed by an L2 rule to `queue` (rule seeded once per queue)."""
    sender = "+1804555" + f"{uuid.uuid4().int % 10000:04d}"
    fx.seed_rule(install, sender=sender, line=None, queue=queue)
    result = fx.receive(install, fx.fax_cmd(sender=sender))
    assert result["outcome"] == "routed", result
    return str(result["fax_id"])


def sorting_fax(install: sa.SAInstall) -> str:
    result = fx.receive(install, fx.fax_cmd(sender=None))
    assert result["outcome"] == "needs_sorting", result
    return str(result["fax_id"])
