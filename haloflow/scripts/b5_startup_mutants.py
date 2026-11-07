"""Focused mutation controls for UC1 part B, B5 (trusted startup).

The SU mutants were predeclared with the B5 package (v2 + v2r1). Semantics are
fixed; each is one contiguous textual change at an exact anchor in the current
source, and a missing anchor refuses the run.

src/haloflow/startup.py:
- SU-NOVALIDATE  the per-tenant ``validate_loaded_generation`` call removed (the
                 load and the comparator are kept);
- SU-PERTENANT   only captured index 0 is enforced: a later tenant's refusal is
                 dropped (the tenant is left out of the validated set). The brief's
                 example (loop over ``captured[:1]``) would also fail B5-S01, whose
                 second VALID tenant would then be refused by the facade's
                 validated-pair check: not exact, so not used;
- SU-KEYS        ``keys=ProductionNoteKeys()`` replaced by a local provider class
                 returning fixed test keys;
- SU-ADMIT       ``ServedTenantGateway`` delegates without the membership check;
- SU-FACADE      the facade calls the backing resolver before the allowed-id check;
- SU-SNAPSHOT    a split snapshot: ``ROLLBACK`` then ``BEGIN ISOLATION LEVEL
                 REPEATABLE READ, READ ONLY`` on the same connection right before
                 the generation read. ``_verify_tenant`` (called directly by
                 B5-S15) receives a connection, not a factory, so a second fresh
                 migrator connection is not reachable there; the session settings
                 (search_path, SET ROLE) survive the rollback;
- SU-CONSTRUCT   the services are constructed after the static checks but before
                 the per-tenant loop (with empty validated sets that are filled in,
                 through the name-mangled slots, after the loop, so that a valid
                 start still serves);
- SU-CHAIN       the generation-refusal mapping raises ``StartupRefused`` inside
                 the ``except RegistryRefused`` blocks (``__context__`` set). The
                 implemented startup RETURNS codes and ``_verify_served_tenant``
                 re-maps every ``StartupRefused`` to its code, so that change alone
                 is an EQUIVALENT mutant at the factory (the factory raises a fresh
                 refusal). The one contiguous span therefore also lets a chained
                 refusal (``__context__`` not None) pass ``_verify_served_tenant``
                 unchanged; only generation refusals are chained, so only they
                 surface (index None, context set).
src/haloflow/m02/registry_validation.py:
- SU-NOACTIVATION the M05-A1 production branch removed (linkage kept);
- SU-LINKAGE     the linkage rule removed: the activation flag is ``all(flags)``
                 with no partial refusal.

The judging rules are exactly the part A checker's (scripts/m02_observation_mutants.py):
the control run of the group's test module must pass completely, a mutant is KILLED
only by a completed run whose every failure is in its PREDECLARED kill set (no
error, no skip, the control's node set), and the target is restored after every run
and its SHA-256 re-checked. This script runs that checker once per group (target,
test module, mutants) and adds no judging logic of its own. A mutant joins a group
only when its declared kill set names nodes of that group's module.

Usage (from haloflow/):
    python3 -I scripts/b5_startup_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/b5-startup-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STARTUP = Path("src/haloflow/startup.py")
REGISTRY = Path("src/haloflow/m02/registry_validation.py")
PG = "tests/m05/test_b5_startup_postgres.py"
UNITS = "tests/m05/test_b5_composition_units.py"
TITLE_FROM = "m02-observation-mutants"

KINDS = ("version-11", "suspended", "function-config-drift", "test-family-active",
         "synthetic-resolver-active", "m05-full-activation", "m05-partial-activation",
         "m05-resolver-row-missing", "m05-family-row-missing", "m05-type-row-missing",
         "test-intake-present")
VALIDATION_KINDS = ("test-family-active", "synthetic-resolver-active", "m05-full-activation",
                    "m05-partial-activation", "test-intake-present")
GENERATION_KINDS = (*VALIDATION_KINDS[:4], "m05-resolver-row-missing",
                    "m05-family-row-missing", "m05-type-row-missing", "test-intake-present")
PARTIAL = ("100", "010", "001", "110", "101", "011")
E_ROWS = ("test_b5_e01_cleanup_failure_without_prior_failure_refuses",
          "test_b5_e02_non_idle_connection_refused_not_adopted",
          "test_b5_e03_factory_failure_is_sanitized",
          "test_b5_e04_cancellation_propagates_and_cleans_up",
          "test_b5_e05_query_time_driver_error_maps_to_generation_unreadable",
          "test_b5_e06_cleanup_failure_after_refusal_keeps_the_original_reason",
          "test_b5_e07_cancellation_during_refusal_cleanup_wins")


def _nodes(module: str, *names: str) -> frozenset[str]:
    return frozenset(f"{module}::{name}" for name in names)


def _s02(*kinds: str, positions: tuple[str, ...] = ("first", "last")) -> frozenset[str]:
    return _nodes(PG, *(f"test_b5_s02_one_bad_tenant_refuses_startup[{k}-{p}]"
                        for k in kinds for p in positions))


# ---------------------------------------------------------------------------
# Anchors (exact text of the current source)
# ---------------------------------------------------------------------------

_VALIDATE = (
    "    try:\n"
    "        registry_validation.validate_loaded_generation("
    "generation, deployment=deployment.value)\n"
    "    except registry_validation.RegistryRefused as refused:\n"
    "        return _registry_code(refused)\n"
    "    except Exception:  # noqa: BLE001\n"
    "        return _UNREADABLE\n"
)
_ENFORCE = (
    "        if isinstance(outcome, str):\n"
    "            raise _refusal(outcome, index)\n"
    "        validated.append(outcome)\n"
)
_KEYS = (
    "        services = ProductionServices(\n"
    "            m05=M05Service(gateway, foundation=foundation, keys=ProductionNoteKeys()),\n"
)
_ADMIT = (
    "        if not _admitted(context, self.__validated):\n"
    "            raise TenantNotServed()\n"
    "        return await self.__gateway.with_tenant_transaction("
    "context, callback, options=options)\n"
)
_FACADE_CHECK = (
    "        if type(tenant_hint) is not str or tenant_hint not in self.__allowed_ids:\n"
    "            raise TenantNotServed()\n"
)
_FACADE_CALL = (
    "        context = await self.__resolver.resolve(\n"
    "            principal=principal,\n"
    "            tenant_hint=tenant_hint,\n"
    "            purpose=purpose,\n"
    "            capabilities=capabilities,\n"
    "            source=source,\n"
    "            execution_id=execution_id,\n"
    "            correlation_id=correlation_id,\n"
    "            correlation_source=correlation_source,\n"
    "            request_id=request_id,\n"
    "        )\n"
)
_GENERATION_READ = (
    "    # (3) The generation, in the same transaction, then validated.\n"
    "    try:\n"
    "        generation = await registry_validation.load_generation_in(connection, schema_key)\n"
)
_LOOP_AND_CONSTRUCT_START = (
    "    # 3. Every tenant, in order; the first refusal aborts. Each startup connection\n"
)
_CONSTRUCT = (
    "    # 4. Construction, only now.\n"
    "    pairs = frozenset((tenant_id, schema_key) for tenant_id, schema_key, _ in validated)\n"
    "    services: ProductionServices | None = None\n"
    "    try:\n"
    "        policy = (ActivationPolicy.ENFORCE if deployment is Deployment.PRODUCTION\n"
    "                  else ActivationPolicy.NOT_ENFORCED)\n"
    "        runtime = composition.build_production_tenant_runtime(\n"
    "            dependencies, activation_policy=policy)\n"
    "        gateway = ServedTenantGateway(runtime.gateway, validated=pairs)\n"
)
_CONSTRUCT_END = (
    "    if services is None:\n"
    "        raise _refusal(_COMPOSITION_INVALID, None)\n"
    "    return services\n"
)
_GEN_MAP = (
    "    except registry_validation.RegistryRefused as refused:\n"
    "        return _registry_code(refused)\n"
)
_SERVED_MAP = (
    "            except StartupRefused as refused:\n"
    "                code = refused.reason_code\n"
)
_A1 = (
    '    if deployment == "production" and m05_active is True:\n'
    '        raise RegistryRefused("M05_ACTIVATION_NOT_AUTHORIZED")\n'
)
_LINKAGE = (
    "    if not (family and contract and resolver) or len(flags) != 1:\n"
    '        raise RegistryRefused("ROUTE_NOT_INSTALLED")\n'
    "    return flags.pop()\n"
)

_FIXED_KEYS = (
    "        class _FixedTestKeys:\n"
    "            def current_hmac(self) -> tuple[int, bytes]:\n"
    '                return 1, b"\\x01" * 32\n'
    "\n"
    "            def hmac_key(self, version: int) -> bytes:\n"
    '                return b"\\x01" * 32\n'
    "\n"
    "            def current_encryption(self) -> tuple[int, bytes]:\n"
    '                return 1, b"\\x02" * 32\n'
    "\n"
    "            def encryption_key(self, version: int) -> bytes:\n"
    '                return b"\\x02" * 32\n'
    "\n"
    "        services = ProductionServices(\n"
    "            m05=M05Service(gateway, foundation=foundation, keys=_FixedTestKeys()),\n"
)


def _exactly_once(text: str, anchor: str, mutant_id: str) -> int:
    count = text.count(anchor)
    if count != 1:
        raise SystemExit(f"{mutant_id}: anchor occurs {count} times")
    return text.index(anchor)


def _span(text: str, first: str, last: str, mutant_id: str) -> str:
    """The contiguous text from `first` through `last` (each exactly once, in order)."""

    start = _exactly_once(text, first, mutant_id)
    end = _exactly_once(text, last, mutant_id) + len(last)
    if end <= start:
        raise SystemExit(f"{mutant_id}: anchors out of order")
    return text[start:end]


def _facade(text: str) -> tuple[str, str]:
    old = _FACADE_CHECK + _FACADE_CALL
    _exactly_once(text, old, "SU-FACADE")
    return old, _FACADE_CALL + _FACADE_CHECK


def _snapshot(text: str) -> tuple[str, str]:
    _exactly_once(text, _GENERATION_READ, "SU-SNAPSHOT")
    return _GENERATION_READ, (
        "    # (3) The generation, in the same transaction, then validated.\n"
        "    try:\n"
        '        await connection.execute("ROLLBACK")\n'
        '        await connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")\n'
        "        generation = await registry_validation.load_generation_in("
        "connection, schema_key)\n"
    )


def _construct(text: str) -> tuple[str, str]:
    """Steps 3 and 4 swapped in one span; the loop text itself is kept verbatim."""

    old = _span(text, _LOOP_AND_CONSTRUCT_START, _CONSTRUCT_END, "SU-CONSTRUCT")
    split = old.index(_CONSTRUCT)
    loop, construction = old[:split], old[split:]
    pairs_line = (
        "    pairs = frozenset((tenant_id, schema_key) for tenant_id, schema_key, _ in validated)\n"
    )
    early = construction.replace(pairs_line, "", 1).replace(
        "validated=pairs)\n", "validated=frozenset())\n", 1
    ).replace(
        "                runtime.resolver, allowed_ids=frozenset(captured), validated=pairs\n",
        "                runtime.resolver, allowed_ids=frozenset(captured),"
        " validated=frozenset()\n",
        1,
    ).replace("    return services\n", "", 1)
    if early.count("validated=pairs") or "frozenset()" not in early:
        raise SystemExit("SU-CONSTRUCT: construction text changed")
    new = (
        early.replace("    # 4. Construction, only now.\n",
                      "    # 4. Construction (SU-CONSTRUCT: before the per-tenant checks).\n", 1)
        + "\n"
        + loop.rstrip("\n") + "\n"
        + pairs_line
        + "    services.m05._gateway._ServedTenantGateway__validated = pairs\n"
        + "    services.staff._gateway._ServedTenantGateway__validated = pairs\n"
        + "    services.resolver._ServedTenantResolver__validated = pairs\n"
        + "    return services\n"
    )
    return old, new


def _chain(text: str) -> tuple[str, str]:
    old = _span(text, _GENERATION_READ, _SERVED_MAP, "SU-CHAIN")
    if old.count(_GEN_MAP) != 2:
        raise SystemExit(f"SU-CHAIN: generation mapping occurs {old.count(_GEN_MAP)} times")
    new = old.replace(
        _GEN_MAP,
        "    except registry_validation.RegistryRefused as refused:\n"
        "        raise StartupRefused(_registry_code(refused))  # noqa: B904\n",
    ).replace(
        _SERVED_MAP,
        "            except StartupRefused as refused:\n"
        "                if refused.__context__ is not None:\n"
        "                    raise\n"
        "                code = refused.reason_code\n",
    )
    return old, new


Change = tuple[tuple[str, str], ...] | Callable[[str], tuple[str, str]]

# (id, description, target, change, kills across modules)
SPECS: tuple[tuple[str, str, Path, Change, frozenset[str]], ...] = (
    ("SU-NOVALIDATE", "per-tenant validate_loaded_generation removed", STARTUP,
     ((_VALIDATE, "    _ = generation  # SU-NOVALIDATE\n"),),
     _s02(*VALIDATION_KINDS)),
    ("SU-NOACTIVATION", "M05-A1 production branch removed (linkage kept)", REGISTRY,
     ((_A1, ""),),
     _nodes(UNITS, "test_b5_v01_full_activation_in_production_refused",
            "test_b5_v05_full_with_test_family_in_production_is_m05_a1_first",
            "test_b5_v06_full_with_synthetic_resolver_in_production_is_m05_a1_first")
     | _s02("m05-full-activation")),
    ("SU-LINKAGE", "linkage rule removed (flag = all(flags))", REGISTRY,
     ((_LINKAGE, "    return all(flags)\n"),),
     _nodes(UNITS, *(f"test_b5_v03_partial_activation_is_route_not_installed[{d}-{f}]"
                     for d in ("production", "non_production") for f in PARTIAL),
            "test_b5_v04_partial_with_test_family_in_production_is_linkage_first")
     | _s02("m05-partial-activation")),
    ("SU-PERTENANT", "only captured index 0 enforced", STARTUP,
     ((_ENFORCE,
       "        if isinstance(outcome, str):\n"
       "            if index == 0:\n"
       "                raise _refusal(outcome, index)\n"
       "            continue\n"
       "        validated.append(outcome)\n"),),
     _s02(*KINDS, positions=("last",))),
    ("SU-KEYS", "fixed test key provider wired", STARTUP, ((_KEYS, _FIXED_KEYS),),
     _nodes(UNITS, "test_b5_k03_startup_wires_only_the_fail_closed_key_provider")),
    ("SU-ADMIT", "gateway delegates without the membership check", STARTUP,
     ((_ADMIT,
       "        return await self.__gateway.with_tenant_transaction(context, callback, "
       "options=options)\n"),),
     _nodes(PG, "test_b5_a01_independent_context_for_unserved_tenant_refused_on_every_service")),
    ("SU-FACADE", "backing resolver called before the allowed-id check", STARTUP, _facade,
     _nodes(PG, "test_b5_a02_facade_refuses_unserved_and_unknown_before_resolver_io",
            "test_b5_s04_served_set_is_captured_once")),
    ("SU-SNAPSHOT", "split snapshot before the generation read", STARTUP, _snapshot,
     _nodes(PG, "test_b5_s15_commit_after_tenant_row_read_is_invisible[generation-row]")),
    ("SU-CONSTRUCT", "services constructed before the per-tenant loop", STARTUP, _construct,
     _s02(*KINDS) | _nodes(PG, *E_ROWS)),
    ("SU-CHAIN", "generation refusal raised inside the except block", STARTUP, _chain,
     _s02(*GENERATION_KINDS)
     | _nodes(PG, "test_b5_e05_query_time_driver_error_maps_to_generation_unreadable")),
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


def build_mutants(text: str, mutant_class: Any, target: Path,
                  only: str | None = None) -> tuple[Any, ...]:
    """The group's mutants: one contiguous (old, new) each, `old` exactly once in
    `text`, the kill set restricted to the module `only` (mutants with no kill in
    that module are not in the group)."""

    mutants = []
    for mutant_id, description, spec_target, change, kills in SPECS:
        if spec_target != target:
            continue
        group_kills = frozenset(k for k in kills if only is None or k.startswith(f"{only}::"))
        if not group_kills:
            continue
        if callable(change):
            old, new = change(text)
        else:
            ((old, new),) = change
        _exactly_once(text, old, mutant_id)
        if old == new:
            raise SystemExit(f"{mutant_id}: the mutation changes nothing")
        mutants.append(mutant_class(mutant_id, description, old, new, group_kills))
    return tuple(mutants)


GROUPS = (
    ("startup-postgres", STARTUP, PG),
    ("startup-units", STARTUP, UNITS),
    ("registry-postgres", REGISTRY, PG),
    ("registry-units", REGISTRY, UNITS),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--group", action="append", choices=[g[0] for g in GROUPS],
                        help="run only these groups (default: all; CI runs all)")
    args = parser.parse_args(argv)
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        if args.group and label not in args.group:
            continue
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        text = target.read_text(encoding="utf-8")
        checker.MUTANTS = build_mutants(text, checker.Mutant, target, only=tests)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=b5-startup-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
