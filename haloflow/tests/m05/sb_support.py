"""L-1S package S-B (due-window selection) red-test support.

Governing: S-B requirements v1/v1r1, architecture v1, interface pins v1 + the red
package interface delta (Shared Workspace, Work Session 2026-11-06). S-B builds on
S-A: the S-B TEST registry is the S-A TEST registry plus the S-B units (target 45);
the S-B TEST catalogue is the S-A TEST catalogue plus `SB_STATEMENTS`. S-A's units,
profiles and `SA_STATEMENTS` are not modified (SB-S02).

Every import of S-B code is deferred into the row body, so a missing implementation
makes each row FAIL in its own body (class-B red rule), never ERROR at collection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import m02_support
import psycopg
import sa_support as sa
from psycopg import sql
from psycopg.types.json import Jsonb

# --- units, statements, codes (pins P1/P2 + red-package delta D1) ------------------

SB_UNIT_IDS: Final = ("t044_m05_reminder_windows_due", "t045_m05_due_windows")
SB_TARGET: Final = 45
SB_SUPPORTED: Final = range(SB_TARGET, SB_TARGET + 1)
INDEX_NAME: Final = "m05_reminder_windows_due"
SELECTOR: Final = "m05_due_windows"
DUE_KEY: Final = "m05.intake.due_windows"
CLOSE_KEY: Final = "m05.intake.close"
APPEND: Final = sa.APPEND_CAP
CREATE: Final = sa.CREATE_CAP
TIMER_CAPS: Final = frozenset({DUE_KEY, CLOSE_KEY, APPEND, CREATE})
SELECT_CAPS: Final = frozenset({DUE_KEY})
CLOSE_CAPS: Final = frozenset({CLOSE_KEY, APPEND, CREATE})
# Selector refusals (SQLSTATE P0001 message). BOUNDS_INVALID: after >= until.
SELECTOR_CODES: Final = ("COMMAND_MALFORMED", "BOUNDS_INVALID", "WORKLOAD_NOT_AUTHORIZED")
RUN_CODES: Final = ("COMPLETE", "CAP", "SELECTION_FAILED", "RUN_IN_PROGRESS")
PAGE_MAX: Final = 500
CAP_MAX: Final = 10_000


def sb_units() -> Any:
    from haloflow.m05.due_units import SB_TENANT_MIGRATIONS

    return SB_TENANT_MIGRATIONS


def sb_profiles() -> Any:
    from haloflow.m05.due_units import SB_INSTALLED_STATE_PROFILES

    return SB_INSTALLED_STATE_PROFILES


def sb_registry() -> Any:
    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        sa.sa_units(),
        sb_units(),
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES, **sa.sa_profiles(),
                                  **sb_profiles()},
        acl_amendments=APPROVED_ACL_AMENDMENTS,
    )


def sb_statements() -> Any:
    from haloflow.m05.due_statements import SB_STATEMENTS

    return SB_STATEMENTS


def sb_catalog() -> Any:
    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import build_statement_catalog

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, sa.sa_statements(),
                                   sb_statements())


class SBInstall(sa.SAInstall):
    """The S-A install shape, provisioned through the S-B registry (target 45)."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(
                    self.ids, sb_registry(), self.tenant, supported=SB_SUPPORTED,
                    manifest=staff_manifest(),
                )
                if self.profile:
                    sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"S-B test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[SBInstall]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield SBInstall(ids, tenant)
    finally:
        reset(tenant)


# --- selector commands ------------------------------------------------------------


def iso(value: datetime) -> str:
    """The M05 timestamp rule: UTC, microseconds, literal Z."""

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def key(deadline: datetime | str, reminder_id: str) -> dict[str, Any]:
    return {"deadline": deadline if isinstance(deadline, str) else iso(deadline),
            "reminder_id": reminder_id}


def due_cmd(*, after: Any = None, until: Any = None, limit: Any = 100,
            principal: str = sa.W_TIMER, kind: str = "workload") -> dict[str, Any]:
    return {"schema_version": 1, "after": after, "until": until, "limit": limit,
            "attribution": sa.attribution(principal, kind=kind)}


def select(install: SBInstall, command: Any) -> list[tuple[str, str]]:
    """t045 through a runtime connection; (deadline iso, reminder_id) pairs in order."""

    result = sa.call(install, SELECTOR, command)
    assert set(result) == {"schema_version", "keys"}, result
    assert result["schema_version"] == 1
    out = []
    for item in result["keys"]:
        assert set(item) == {"deadline", "reminder_id"}, item
        out.append((item["deadline"], item["reminder_id"]))
    return out


def refused(install: SBInstall, command: Any, code: str) -> None:
    sa.refused(install, SELECTOR, command, code)


# --- windows ----------------------------------------------------------------------


def due_window(install: SBInstall, *, hours_past: float = 25.0) -> tuple[str, str, str]:
    """A new patient + one dispatched window whose deadline is `hours_past - 24` hours
    in the past (deadline = sent_at + 24 h; shift_window moves both). Returns
    (patient, phone, reminder)."""

    patient, phone, (ref,) = sa.fresh_patient(install)
    reminder = sa.new_id()
    sa.dispatch(install, reminder, ref)
    sa.shift_window(install, reminder, -hours_past)
    return patient, phone, reminder


def set_deadline(install: SBInstall, reminder: str, deadline: datetime) -> None:
    """FIXTURE: pin a window's deadline exactly (sent_at = deadline - 24 h)."""

    sa.admin(install, sa.q(install, "UPDATE {s}.m05_reminder_windows"
                                    " SET sent_at = %s - interval '24 hours', deadline = %s"
                                    " WHERE reminder_id = %s"), (deadline, deadline, reminder))


def awaiting_due(install: SBInstall) -> list[tuple[datetime, str]]:
    rows = sa.admin(install, sa.q(install, "SELECT deadline, reminder_id::text"
                                           " FROM {s}.m05_reminder_windows"
                                           " WHERE state = 'awaiting' AND deadline <= now()"
                                           " ORDER BY deadline, reminder_id"))
    return [(r[0], r[1]) for r in rows]


def close_all_awaiting(install: SBInstall) -> None:
    """FIXTURE: retire every awaiting window in the module tenant (rows start clean)."""

    sa.admin(install, sa.q(install, "UPDATE {s}.m05_reminder_windows SET state = 'replied'"
                                    " WHERE state = 'awaiting'"))


def states(install: SBInstall, reminders: list[str]) -> dict[str, str]:
    rows = sa.admin(install, sa.q(install, "SELECT reminder_id::text, state"
                                           " FROM {s}.m05_reminder_windows"
                                           " WHERE reminder_id = ANY(%s::uuid[])"), (reminders,))
    return {r[0]: r[1] for r in rows}


def no_reply_events(install: SBInstall, reminder: str) -> int:
    rows = sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.patient_events"
                                           " WHERE event_type = %s AND correlation_id = %s"),
                    (sa.T_NO_REPLY, reminder))
    return int(rows[0][0])


def no_reply_items(install: SBInstall, patient: str) -> int:
    return sum(1 for item in sa.items_for(install, patient) if item["item_kind"] == "no_reply")


def predicate_holds(install: SBInstall, predicate: str, deadline: datetime | None,
                    cutoff: datetime) -> bool | None:
    """Evaluate the EXACT t045 comparison text (due_units.DUE_PREDICATE) at a fixed
    cutoff, substituting only its two operands (red-package delta D2: source-level tie;
    no caller-controlled cutoff reaches the production selector)."""

    assert predicate.count("w.deadline") == 1 and predicate.count("v_cutoff") == 1, predicate
    text = (predicate.replace("w.deadline", "%(deadline)s::timestamptz")
            .replace("v_cutoff", "%(cutoff)s::timestamptz"))
    with psycopg.connect(install.ids.admin, autocommit=True) as conn:
        row = conn.execute(sql.SQL("SELECT " + text),  # type: ignore[arg-type]
                           {"deadline": deadline, "cutoff": cutoff}).fetchone()
    assert row is not None
    value: bool | None = row[0]
    return value


def timely(install: SBInstall, received: datetime | None, deadline: datetime | None) -> Any:
    rows = sa.admin(install, sa.q(install, "SELECT {s}.intake_reply_is_timely(%s, %s)"),
                    (received, deadline))
    return rows[0][0]


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


MICRO: Final = timedelta(microseconds=1)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)
