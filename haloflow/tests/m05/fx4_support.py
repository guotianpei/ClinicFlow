"""Incoming fax routing FX-IN, package FX4 — sender display backend (OD-FX10) + queue read (t145).
TEST SUPPORT (TEST ONLY). RED BY DESIGN.

Governing (Shared Workspace `Work Session 2026-11-25/`): package v1r1
(`claude_fx4-sender-display-package-v1r1-2026-11-25.md`, Codex ALIGN for red tests 8f518e20 with
three additions: post-lock revocation also for sender_put, a FOREIGN session context against t149
with valid A inputs, NFKC before stripping controls / brackets incl. fullwidth brackets, U+2212 and
a direct-gateway 257-character refusal),
OD-FX10 (Rachel 2026-11-25), OD-FX2/5 (exact-number routing), OD-FX3 (support-only configuration),
FR-7, FR-11.
Synthetic only; no live vendor field is claimed.

Red classes: C control; A absence-red (`include_fx4` -> TypeError; new names -> ModuleNotFoundError
/ AttributeError; the new SQL functions do not exist).

FX4 interface pinned here
==========================================================================================
Registry: `build_synthetic_registry(<EL flags>, include_fx1=True, include_fx2=True,
include_fx4=True)` — FX2's ids plus FX4_IDS, target **149**; include_fx4 without include_fx2 ->
ValueError("...FX4..."). Units live in `haloflow.m05.fax_display_units` (FX4_TENANT_MIGRATIONS,
FX4_INSTALLED_STATE_PROFILES).
Catalogue: `build_synthetic_catalog(<EL flags>, include_fx1, include_fx2, include_fx3,
include_fx4=True)` adds QUEUES_KEY and REBINDS RECEIVE_KEY -> "SELECT m05_fx_receive_v2(%(command)s)" and READ_KEY -> "SELECT
m05_fx_staff_read_v2(%(command)s)" (same modes and capabilities); include_fx4 without include_fx3 ->
ValueError("...FX4...").
- t145 `m05_fx_staff_queues(p_command jsonb) RETURNS jsonb` (runtime, STABLE, `_TENANT + BINDING`):
  command exactly {schema_version 1, staff_id, attribution (actor; principal_id = staff_id)}; staff
  check 'read' else ACTOR_INACTIVE; result {schema_version 1, queues: [{queue_id, name, active,
  position}]} ordered by position, then lower(name); inactive queues included.
- t146 DDL: `m05_fx_senders (sender_id uuid PK, sender_e164 varchar(16) UNIQUE NOT NULL E.164,
  display_name varchar(64) NOT NULL 1..64, updated_at timestamptz NOT NULL)` (M05 owner only);
  `m05_fx_faxes.sender_name varchar(40) NULL`; settings-audit op CHECK gains 'sender_put',
  'sender_remove'.
- t147 `m05_fx_sender_put(p_command jsonb) RETURNS jsonb` (support; EXECUTE haloflow_support only;
  the t138 support authority, capability m05.fax.settings): {schema_version 1, op "sender_put",
  sender, name} (upsert; sender_id stable from the first insert) | {schema_version 1, op
  "sender_remove", sender}. Result {schema_version 1, outcome "applied", sender_id}. Codes:
  COMMAND_MALFORMED, SENDER_INVALID, SENDER_NAME_INVALID, SENDER_NOT_FOUND, SUPPORT_NOT_AUTHORIZED
  (authority re-checked AFTER the sender row lock; nothing committed on refusal). One settings-audit
  row per applied op, target_id = sender_id, no number / digest / name anywhere in the audit row.
  The stored display_name is the saved name after NFKC, whitespace collapse and trim.
- t148 `m05_fx_receive_v2(jsonb)` (runtime; red v1r1 pin correction: the unit framework never
  replaces a function — every typed unit is `replace: false` and earlier packages version names, e.g.
  m05_directory_v4 — so v2 is a NEW function and the FX4 catalogue rebinds RECEIVE_KEY to it; t139
  stays): the v1 key set, optionally plus `sender_name` (string | null). Raw
  length > 256 -> COMMAND_MALFORMED (direct gateway too). Stored `sender_name` =
  normalise_sender_name(raw). Replay of the same identity: equal normalised names (both absent
  included) and equal other metadata -> duplicate (no write); otherwise duplicate_mismatch, original
  row untouched, discrepancy `fields` includes 'sender_name' when the names differ. Saved names
  never take part.
- t149 `m05_fx_staff_read_v2(jsonb)` (runtime, STABLE; NEW function, the FX4 catalogue rebinds
  READ_KEY to it; t143 stays): as t143, and every fax summary gains `sender_display` =
  {"text": str|null,
  "source": "saved"|"as_sent"|"none"} (saved name of the fax's exact sender number in THIS schema,
  else the stored sent name, else none); `sender_last4` unchanged.
- `haloflow.m05.fax_rules`: SENDER_NAME_RAW_MAX = 256, SENDER_NAME_MAX = 40, SAVED_NAME_MAX = 64,
  MASK = "•••"; `normalise_sender_name(raw: str | None) -> str | None` (None -> None; non-str or len
  > 256 -> ValueError); `saved_name_valid(name: str) -> bool`; `number_runs(text: str) -> list[str]`
  (the digit strings, ASCII, of every run). Algorithm (SQL authoritative, Python mirror): NFKC ->
  every whitespace character (str.isspace: tab, newline, Zs ...) becomes ' ' -> drop the remaining
  Cc/Cf and '<' '>' -> collapse spaces, trim -> each maximal run of digits (Unicode Nd) and
  separators (Zs, Pd, U+2212, '.', '(', ')', '+', '/') that starts and ends with a digit and holds
  >= 5 digits becomes MASK + its last four digits (ASCII) -> cut to 40 -> trim -> empty is None.
  saved_name_valid: after NFKC + whitespace-to-space + collapse + trim, 1..64 characters, no Cc/Cf,
  no '<' '>', no number run.
- `haloflow.m05.fax_statements`: QUEUES_KEY = "m05.fax.queues", READ, "m05.followup.read", "SELECT
  m05_fx_staff_queues(%(command)s)"; FX4_STATEMENTS = {QUEUES_KEY}.
- `haloflow.m05.fax`: `InboundFax(..., sender_name: str | None = None)` (default keeps every
  caller); `FaxService.queues(ctx) -> dict`.
- `haloflow.m05.fax_ingress`: body may also hold `sender_name` (absent / null / string); a
  non-string non-null -> 400 MALFORMED sender_name; a string longer than 256 -> 400 MALFORMED
  sender_name; passed to InboundFax.
- `haloflow.synthetic.fax_staff_routes`: ROUTES["queues"] = ("list", "GET", PREFIX + "/queues").
- `haloflow.synthetic.fax_simulator`: the body may also hold `sender_name`, passed through
  unchanged.
- `haloflow.synthetic.profiles`: TARGETS["fax"] == 149.

"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any, Final

import fx1_support as fx
import fx2_support as f2
import m02_support
import sa_support as sa

FX4_TARGET: Final = 149
FX4_SUPPORTED: Final = range(FX4_TARGET, FX4_TARGET + 1)
FX4_IDS: Final = ("t145_m05_fx_staff_queues", "t146_m05_fx_senders", "t147_m05_fx_sender_put",
                  "t148_m05_fx_receive_v2", "t149_m05_fx_staff_read_v2")
QUEUES: Final = "m05_fx_staff_queues"
RECEIVE_V2: Final = "m05_fx_receive_v2"
READ_V2: Final = "m05_fx_staff_read_v2"
SENDER_PUT: Final = "m05_fx_sender_put"
FX4_TABLES: Final = (*fx.FX_TABLES, "m05_fx_senders")
MASK: Final = "•••"

# U01/U02/P04 corpus: (raw, normalised). Shared by the Python mirror rows and the SQL equality row.
CORPUS: Final[tuple[tuple[str | None, str | None], ...]] = (
    (None, None),
    ("", None),
    ("   ", None),
    ("Quest Diagnostics", "Quest Diagnostics"),
    ("  Quest\t\tDiagnostics \n", "Quest Diagnostics"),
    ("Quest\x07Diag​nostics", "QuestDiagnostics"),               # Cc and Cf removed
    ("<b>Quest</b>", "bQuest/b"),                                        # brackets removed
    ("＜script＞Lab", "scriptLab"),                              # FULLWIDTH < > (NFKC first)
    ("LabCorp 800-555-1212", f"LabCorp {MASK}1212"),
    ("+1 (540) 555 0100", f"+{MASK}0100"),                              # run starts at a digit
    ("Lab 540.555.0100", f"Lab {MASK}0100"),
    ("Lab 540–555—0100", f"Lab {MASK}0100"),                    # en / em dash (Pd)
    ("Lab 540−555−0100", f"Lab {MASK}0100"),                    # U+2212 MINUS (Sm)
    ("Lab 540 555 0100", f"Lab {MASK}0100"),                    # NBSP (Zs)
    ("Lab ５４０５５５０１００", f"Lab {MASK}0100"),  # fullwidth
    ("Lab ٥٤٠٥٥٥٠١٠٠", f"Lab {MASK}0100"),  # Arabic-Indic
    ("Suite 1200", "Suite 1200"),                                        # 4 digits kept
    ("Rm 12345", f"Rm {MASK}2345"),                                      # 5 digits masked
    ("A 540-555-0100 B 804 555 0199", f"A {MASK}0100 B {MASK}0199"),     # two runs
    ("x" * 60, "x" * 40),                                                # cap 40
    ("Valley Radiology Group of Northern Virginia 540-555-0199",
     "Valley Radiology Group of Northern Virgi"),                         # mask, then cut to 40
)
SAVED_VALID: Final = ("Quest Diagnostics", "Valley Radiology", "Suite 1200 Imaging", "x" * 64)
SAVED_INVALID: Final = ("", "   ", "x" * 65, "Quest\x07", "Quest​", "<b>Quest", "＜Quest",
                        "Lab 540-555-0100", "Lab 540−555−0100", "Lab 540 555 0100",
                        "Lab ５４０５５", "Lab ٥٤٠٥٥",
                        "Rm 12345")


def fx4_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(**fx.eligibility_flags(), include_fx1=True,  # type: ignore[call-arg]
                                    include_fx2=True, include_fx4=True)


def fx4_catalog() -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True,  # type: ignore[call-arg]
                                   include_fx2=True, include_fx3=True, include_fx4=True)


class FX4Install(fx.FX1Install):
    """A synthetic tenant at target 149 (FX2 144 + FX4), provisioned in the row body."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, fx4_registry(), self.tenant,
                                           supported=FX4_SUPPORTED, manifest=staff_manifest())
                sa.admin(self, fx.FX_TEST_PROFILE_SQL.format(schema=self.schema))
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"FX4 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[FX4Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield FX4Install(ids, tenant)
    finally:
        reset(tenant)


def upgrade_to_fx4(install: sa.SAInstall) -> None:
    """Apply the FX4 registry to a tenant provisioned at an earlier target (P06b)."""
    import asyncio

    from staff_support import staff_manifest

    from haloflow.m01.provisioning import TenantMigrationRunner

    runner = TenantMigrationRunner(m02_support._connection_factory(  # noqa: SLF001
        install.ids.logins[sa.MIGRATOR]), fx4_registry(), manifest=staff_manifest())
    asyncio.run(runner.apply(tenant_id=install.tenant_id, schema_key=install.schema))


def ready(install: fx.FX1Install, *lines: str) -> fx.FX1Install:
    install.require()
    for line in lines or (fx.LINE1, fx.LINE2):
        fx.seed_line(install, line)
    return install


def snapshot(install: sa.SAInstall) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for table in FX4_TABLES:
        out[table] = sorted(row[0] for row in sa.admin(
            install, sa.q(install, "SELECT to_jsonb(t)::text FROM {s}." + table + " AS t")))
    return out


def sender_cmd(op: str, sender: str, name: str | None = None) -> dict[str, Any]:
    command: dict[str, Any] = {"schema_version": 1, "op": op, "sender": sender}
    if name is not None:
        command["name"] = name
    return command


def queues_cmd(staff_id: str) -> dict[str, Any]:
    return {"schema_version": 1, "staff_id": staff_id,
            "attribution": sa.attribution(staff_id, kind="actor")}


def queues(install: sa.SAInstall, staff_id: str) -> dict[str, Any]:
    return sa.call(install, QUEUES, queues_cmd(staff_id))


def receive(install: sa.SAInstall, command: dict[str, Any]) -> dict[str, Any]:
    return sa.call(install, RECEIVE_V2, command)


def read(install: sa.SAInstall, view: str, staff_id: str, **fields: Any) -> dict[str, Any]:
    return sa.call(install, READ_V2, f2.read_cmd(view, staff_id, **fields))


def receive_named(install: sa.SAInstall, name: Any, **fields: Any) -> dict[str, Any]:
    return receive(install, {**fx.fax_cmd(**fields), "sender_name": name})


def summary(install: sa.SAInstall, staff_id: str, fax_id: str) -> dict[str, Any]:
    got: dict[str, Any] = read(install, "get", staff_id, fax_id=fax_id)["fax"]
    return got


def fresh_sender() -> str:
    return "+1804555" + f"{uuid.uuid4().int % 10000:04d}"


def runtime(install: sa.SAInstall, *, max_size: int = 6) -> Any:
    """A composed TEST runtime over the FX4 registry (149) and the FX4 catalogue."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def cm() -> Any:
        import service_support as sv

        from haloflow.m01.activation import ActivationPolicy
        from haloflow.m01.control_store import PsycopgControlStore
        from haloflow.m01.pool import TenantPool
        from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

        pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=max_size)
        await pool.open()
        try:
            yield compose_tenant_runtime(
                TenantRuntimeDependencies(
                    pool=pool, control_store=PsycopgControlStore(pool),
                    migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),  # noqa: SLF001
                    provisioner_connect=sv._factory(  # noqa: SLF001
                        install.ids.logins["haloflow_provisioner"])),
                registry=fx4_registry(), catalog=fx4_catalog(),
                supported_schema_versions=frozenset({FX4_TARGET}),
                activation_policy=ActivationPolicy["ENFORCE"])
        finally:
            await pool.close()

    return cm()


def staff_app(rt: Any, install: sa.SAInstall, staff_id: str) -> Any:
    """The actual synthetic fax staff routes for one clinic (as FX3a H04 `_staff_app`)."""
    from types import SimpleNamespace

    from fastapi import FastAPI

    from haloflow.m01.staff.service import StaffService
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.fax import FaxService
    from haloflow.synthetic.fax_staff_routes import install_fax_staff_routes
    from haloflow.synthetic.wiring import SyntheticServices

    services = SyntheticServices(
        resolver=rt.resolver, staff=StaffService(rt.gateway), m05=None, gateway=rt.gateway,
        tenant_id=install.tenant_id, staff_id=staff_id,
        fax=FaxService(EventFoundation(rt.gateway, producer_service="m05_fax")))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_fax_staff_routes(app, SimpleNamespace(services=services))
    return app
