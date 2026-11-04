"""L-1W W4a security mutation controls (plan v1 §3; Codex eb7bbb28: declared mutation
evidence for implementation acceptance).

Three groups, judged by the shared part-A checker (scripts/m02_observation_mutants.py),
exactly as scripts/l1w_w1_mutants.py: the control run must pass completely; a
mutant is KILLED only by a completed run whose every failure is in its PREDECLARED kill
set; the target is restored after every run and its SHA-256 re-checked.

- admission: haloflow.platform.admission against the PostgreSQL adapter rows.
- app: haloflow.platform.app against the unit rows.
- http: haloflow.platform.http against the unit rows.

Kill sets were measured locally on implementation head d484edf before CI (unit groups
on the locked Python 3.12 venv; the admission group on a disposable local PostgreSQL 16
cluster, diagnostic only) and are confirmed or reported by the PG17 CI run; a CI
difference is reported, never edited away silently.

Usage (from haloflow/):
    python3 -I scripts/l1w_w4a_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/l1w-w4a-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ADMISSION = Path("src/haloflow/platform/admission.py")
APP = Path("src/haloflow/platform/app.py")
HTTP = Path("src/haloflow/platform/http.py")
ADMISSION_TESTS = "tests/platform/test_l1w_w4a_admission_postgres.py"
APP_TESTS = "tests/platform/test_l1w_w4a_app_units.py"
TITLE_FROM = "m02-observation-mutants"


def _nodes(module: str, *names: str) -> frozenset[str]:
    return frozenset(f"{module}::{name}" for name in names)


# (id, description, target, old, new, predeclared kills)
SPECS: tuple[tuple[str, str, Path, str, str, frozenset[str]], ...] = (
    ('AM-NOCOMMIT', 'admission COMMIT never sent', ADMISSION,
     '                await conn.commit()\n',
     '                pass\n',
     _nodes(ADMISSION_TESTS,
            'test_acf1_known_rollback_on_admitted_is_unavailable_and_nothing_persists',
            'test_acf2_known_rollback_on_idle_is_unavailable_and_revocation_not_persisted',
            'test_acf3_committed_but_ack_lost_on_admitted_is_unavailable_session_persisted',
            'test_acf4_committed_but_ack_lost_on_idle_is_unavailable_revocation_persisted',
            'test_admit_map_admitted_returns_binding_and_commits_once',
            'test_admit_persist_confirmed_refusal_revocation_is_durable[ABSOLUTE]',
            'test_admit_persist_confirmed_refusal_revocation_is_durable[BINDING_MISMATCH]',
            'test_admit_persist_confirmed_refusal_revocation_is_durable[IDLE]',
            'test_lo01_adapter_logout_commit_unknown_is_unavailable',
            'test_lo01_adapter_logout_revokes_only_that_session',
     )),
    ('AM-INFER', 'failed COMMIT treated as success', ADMISSION,
     '        if failed == "committed":  # the COMMIT returned; only releasing failed\n',
     '        if failed in ("committed", "commit"):\n',
     _nodes(ADMISSION_TESTS,
            'test_acf1_known_rollback_on_admitted_is_unavailable_and_nothing_persists',
            'test_acf2_known_rollback_on_idle_is_unavailable_and_revocation_not_persisted',
            'test_acf3_committed_but_ack_lost_on_admitted_is_unavailable_session_persisted',
            'test_acf4_committed_but_ack_lost_on_idle_is_unavailable_revocation_persisted',
            'test_lo01_adapter_logout_commit_unknown_is_unavailable',
     )),
    ('AM-CODE', 'failed COMMIT reported as plain unavailable', ADMISSION,
     '            raise AdmissionUnavailable(COMMIT_UNKNOWN if failed == "commit" else UNAVAILABLE)\n',  # noqa: E501
     '            raise AdmissionUnavailable(UNAVAILABLE)\n',
     _nodes(ADMISSION_TESTS,
            'test_acf1_known_rollback_on_admitted_is_unavailable_and_nothing_persists',
            'test_acf2_known_rollback_on_idle_is_unavailable_and_revocation_not_persisted',
            'test_acf3_committed_but_ack_lost_on_admitted_is_unavailable_session_persisted',
            'test_acf4_committed_but_ack_lost_on_idle_is_unavailable_revocation_persisted',
            'test_lo01_adapter_logout_commit_unknown_is_unavailable',
     )),
    ('AM-ASSURED', 'assurance taken as always true', ADMISSION,
     '                                           identity.auth_time, identity.assured))\n',
     '                                           identity.auth_time, True))\n',
     _nodes(ADMISSION_TESTS,
            'test_admit_map_every_refusal_outcome',
     )),
    ('AM-REFUSAL', 'committed refusal reported as unavailable', ADMISSION,
     '        if type(outcome) is str and outcome in ADMISSION_REFUSALS:\n            raise AdmissionRefused(outcome)\n        raise AdmissionUnavailable(UNAVAILABLE)\n\n    async def logout',  # noqa: E501
     '        raise AdmissionUnavailable(UNAVAILABLE)\n\n    async def logout',
     _nodes(ADMISSION_TESTS,
            'test_admit_map_every_refusal_outcome',
            'test_admit_persist_confirmed_refusal_revocation_is_durable[ABSOLUTE]',
            'test_admit_persist_confirmed_refusal_revocation_is_durable[BINDING_MISMATCH]',
            'test_admit_persist_confirmed_refusal_revocation_is_durable[IDLE]',
            'test_lo01_adapter_logout_revokes_only_that_session',
     )),
    ('AP-ORDER', 'cleanup in opening order, not reverse', APP,
     '    for close in reversed(closers):\n',
     '    for close in list(closers):\n',
     _nodes(APP_TESTS,
            'test_el2_shutdown_unpublishes_before_closing',
            'test_el2_startup_order_is_pinned',
            'test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[admission]',
            'test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[keyset]',
            'test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[verifier]',
     )),
    ('AP-MASK', 'close failure raised over the original', APP,
     '            failed = True\n',
     '            raise\n',
     _nodes(APP_TESTS,
            'test_el3_cancellation_during_startup_wins_and_cleans_up',
            'test_el3_cleanup_failure_alone_fails_startup',
            'test_el3_close_failure_never_masks_the_original[admission]',
            'test_el3_close_failure_never_masks_the_original[keyset]',
            'test_el3_close_failure_never_masks_the_original[verifier]',
     )),
    ('AP-CANCEL', 'cancellation during cleanup swallowed', APP,
     '    if cancelled:\n        raise asyncio.CancelledError()\n',
     '',
     _nodes(APP_TESTS,
            'test_el3_cancellation_during_cleanup_wins',
     )),
    ('AP-UNPUBLISH', 'shutdown does not unpublish', APP,
     '            app.state.platform = None  # the gate refuses from here on\n',
     '',
     _nodes(APP_TESTS,
            'test_el2_shutdown_unpublishes_before_closing',
     )),
    ('AP-CLEANFAIL', 'cleanup failure alone not reported', APP,
     '            if await _close_all(closers):\n                raise StartupCleanupFailed()\n',
     '            await _close_all(closers)\n',
     _nodes(APP_TESTS,
            'test_el3_cleanup_failure_alone_fails_startup',
     )),
    ('AP-MFA', 'MFA refusal mapped to the generic 401', APP,
     '        key = "REAUTHENTICATION_REQUIRED" if refused.code == "MFA_REQUIRED" else "UNAUTHENTICATED"\n',  # noqa: E501
     '        key = "UNAUTHENTICATED"\n',
     _nodes(APP_TESTS,
            'test_admit_map_http_refusal_status[MFA_REQUIRED-401-REAUTHENTICATION_REQUIRED]',
     )),
    ('AP-UNAVAIL', 'admission unavailability mapped to 401', APP,
     '    except AdmissionUnavailable:\n        raise _Refusal("UNAVAILABLE") from None\n    return session\n',  # noqa: E501
     '    except AdmissionUnavailable:\n        raise _Refusal("UNAUTHENTICATED") from None\n    return session\n',  # noqa: E501
     _nodes(APP_TESTS,
            'test_admit_map_http_unavailable_is_503_and_no_handler[ADMISSION_COMMIT_UNKNOWN]',
            'test_admit_map_http_unavailable_is_503_and_no_handler[ADMISSION_UNAVAILABLE]',
     )),
    ('HT-READY', 'readiness gate removed', HTTP,
     '        if getattr(state, "platform", None) is None:\n            await body("NOT_READY")(scope, receive, send)\n            return\n',  # noqa: E501
     '',
     _nodes(APP_TESTS,
            'test_el2_shutdown_unpublishes_before_closing',
            'test_el2_without_lifespan_every_request_is_503[/anything-GET]',
            'test_el2_without_lifespan_every_request_is_503[/healthz-GET]',
     )),
    ('HT-HEADER', 'tenant header refusal removed', HTTP,
     '        if bytes(name).lower() in TENANT_HEADERS:\n',
     '        if False:\n',
     _nodes(APP_TESTS,
            'test_h03_tenant_input_is_400_before_authentication[header-X-TENANT-GET-/healthz]',
            'test_h03_tenant_input_is_400_before_authentication[header-X-TENANT-POST-/v1/session/logout]',  # noqa: E501
            'test_h03_tenant_input_is_400_before_authentication[header-X-Tenant-Id-GET-/healthz]',
            'test_h03_tenant_input_is_400_before_authentication[header-X-Tenant-Id-POST-/v1/session/logout]',  # noqa: E501
            'test_h03_tenant_input_is_400_before_authentication[header-tenant-GET-/healthz]',
            'test_h03_tenant_input_is_400_before_authentication[header-tenant-POST-/v1/session/logout]',  # noqa: E501
            'test_h03_tenant_input_is_400_before_authentication[header-x-haloflow-tenant-GET-/healthz]',  # noqa: E501
            'test_h03_tenant_input_is_400_before_authentication[header-x-haloflow-tenant-POST-/v1/session/logout]',  # noqa: E501
     )),
    ('HT-QUERY', 'tenant query refusal removed', HTTP,
     '    return any(key.lower() in TENANT_QUERY_KEYS\n',
     '    return False and any(key.lower() in TENANT_QUERY_KEYS\n',
     _nodes(APP_TESTS,
            'test_h03_tenant_input_is_400_before_authentication[query-CLINIC_ID-GET-/healthz]',
            'test_h03_tenant_input_is_400_before_authentication[query-CLINIC_ID-POST-/v1/session/logout]',  # noqa: E501
            'test_h03_tenant_input_is_400_before_authentication[query-clinic-GET-/healthz]',
            'test_h03_tenant_input_is_400_before_authentication[query-clinic-POST-/v1/session/logout]',  # noqa: E501
            'test_h03_tenant_input_is_400_before_authentication[query-tenant-GET-/healthz]',
            'test_h03_tenant_input_is_400_before_authentication[query-tenant-POST-/v1/session/logout]',  # noqa: E501
            'test_h03_tenant_input_is_400_before_authentication[query-tenant_hint-GET-/healthz]',
            'test_h03_tenant_input_is_400_before_authentication[query-tenant_hint-POST-/v1/session/logout]',  # noqa: E501
            'test_h03_tenant_input_is_400_before_authentication[query-tenant_id-GET-/healthz]',
            'test_h03_tenant_input_is_400_before_authentication[query-tenant_id-POST-/v1/session/logout]',  # noqa: E501
     )),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("admission", ADMISSION, ADMISSION_TESTS),
    ("app", APP, APP_TESTS),
    ("http", HTTP, APP_TESTS),
)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m02_observation_mutants", HERE / "m02_observation_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_mutants(checker: Any, target: Path) -> tuple[Any, ...]:
    return tuple(
        checker.Mutant(mid, desc, old, new, kills)
        for mid, desc, spec_target, old, new, kills in SPECS
        if spec_target == target
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = build_mutants(checker, target)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=l1w-w4a-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
