"""U-layer start-up refusals: T03 (R-2), T09 (R-6), T12 (R-8), T16 (R-11), R-9.

Every refusal must happen before any listener or connection exists. These tests
call pure validation functions; T01 separately proves construction opens no
socket.
"""

from __future__ import annotations

import pytest
from haloflow_demo.settings import (
    DemoSettings,
    StartupRefused,
    build_server_configs,
    mask_phone,
    normalize_us_phone,
    validate_demo_database_url,
    validate_startup,
)
from haloflow_demo.web import NullStaffService, build_callback_app, build_staff_app

GOOD_DB = "postgresql://demo@127.0.0.1:5433/haloflow_reminders_demo"
LEGACY_DB = "postgresql+asyncpg://legacy@127.0.0.1:5433/haloflow_dev"


def _settings(**env: str) -> DemoSettings:
    base = {"DEMO_DATABASE_URL": GOOD_DB, "DATABASE_URL": LEGACY_DB}
    base.update(env)
    return DemoSettings.from_env(base)


def _reason(exc: pytest.ExceptionInfo[StartupRefused]) -> str:
    return exc.value.reason


# ── defaults ──────────────────────────────────────────────────────────────────


def test_defaults_are_safe() -> None:
    s = _settings()
    assert s.sms_mode == "stub"
    assert s.callback_mode == "simulated"
    assert s.ehr_write == "simulated"
    assert s.dispatcher is False
    assert s.sms_daily_cap == 10
    assert s.clinic_tz == "America/New_York"
    assert (s.staff_host, s.staff_port) == ("127.0.0.1", 8080)
    assert (s.callback_host, s.callback_port) == ("127.0.0.1", 8081)
    validate_startup(s)  # does not raise


@pytest.mark.parametrize("mode", ["", "REAL ", "notifyre", "Real"])
def test_only_exact_real_selects_real_sms(mode: str) -> None:
    assert _settings(DEMO_SMS_MODE=mode).sms_mode == "stub"


# ── T03 (R-2) ─────────────────────────────────────────────────────────────────


def _apps() -> tuple[object, object]:
    return build_staff_app(NullStaffService(), csrf_secret=b"k" * 32), build_callback_app()


def test_t03_workers_2_refused() -> None:
    staff, cb = _apps()
    with pytest.raises(StartupRefused) as exc:
        build_server_configs(_settings(), staff, cb, workers=2)
    assert _reason(exc) == "workers"


def test_t03_reload_refused() -> None:
    staff, cb = _apps()
    with pytest.raises(StartupRefused) as exc:
        build_server_configs(_settings(), staff, cb, reload=True)
    assert _reason(exc) == "reload"


@pytest.mark.parametrize(
    "bind", ["0.0.0.0:8080", "192.168.1.5:8080", "[::]:8080", "example.com:8080"]
)
def test_t03_staff_bind_non_loopback_refused(bind: str) -> None:
    with pytest.raises(StartupRefused) as exc:
        validate_startup(_settings(DEMO_STAFF_BIND=bind))
    assert _reason(exc) == "staff_bind_not_loopback"


def test_t03_valid_configs_are_single_worker_no_reload_loopback() -> None:
    staff, cb = _apps()
    s = _settings()
    staff_cfg, cb_cfg = build_server_configs(s, staff, cb)
    for cfg in (staff_cfg, cb_cfg):
        assert cfg.workers == 1
        assert cfg.reload is False
    assert (staff_cfg.host, staff_cfg.port) == ("127.0.0.1", 8080)
    assert (cb_cfg.host, cb_cfg.port) == ("127.0.0.1", 8081)


# ── T09 (R-6) ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("secret", ["", "changeme", "  ", " changeme "])
def test_t09_real_callback_weak_secret_refused(secret: str) -> None:
    with pytest.raises(StartupRefused) as exc:
        validate_startup(_settings(DEMO_CALLBACK_MODE="real", DEMO_CALLBACK_SECRET=secret))
    assert _reason(exc) == "callback_secret"


def test_t09_simulated_callback_does_not_need_secret() -> None:
    validate_startup(_settings(DEMO_CALLBACK_SECRET=""))


def test_t09_settings_repr_never_shows_secret_or_number() -> None:
    s = _settings(DEMO_CALLBACK_SECRET="s3cr3t-value-xyz", DEMO_SMS_TEST_NUMBER="+15550100199")
    text = repr(s) + str(s)
    assert "s3cr3t-value-xyz" not in text
    assert "5550100199" not in text


# ── T12 (R-8) and P-ID (equivalent URLs) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", "demo_db_missing"),
        ("postgresql://u@x.neon.tech/haloflow_demo", "demo_db_not_local"),
        ("postgresql://u@10.0.0.5:5432/haloflow_demo", "demo_db_not_local"),
        ("postgresql://u@127.0.0.1:5433/haloflow_dev", "demo_db_name"),
        ("postgresql://u@127.0.0.1:5433/haloflow_demo_x", "demo_db_name"),
        ("mysql://u@127.0.0.1/haloflow_demo", "demo_db_scheme"),
    ],
)
def test_t12_demo_db_guard_refuses(url: str, reason: str) -> None:
    with pytest.raises(StartupRefused) as exc:
        validate_demo_database_url(_settings(DEMO_DATABASE_URL=url))
    assert _reason(exc) == reason


@pytest.mark.parametrize(
    "legacy",
    [
        "postgresql+asyncpg://other@127.0.0.1:5433/haloflow_reminders_demo",
        "postgresql://x@localhost:5433/haloflow_reminders_demo",
        "postgresql://x:pw@[::1]:5433/haloflow_reminders_demo?sslmode=disable",
    ],
)
def test_t12_equal_or_equivalent_to_database_url_refused(legacy: str) -> None:
    with pytest.raises(StartupRefused) as exc:
        validate_demo_database_url(_settings(DATABASE_URL=legacy))
    assert _reason(exc) == "demo_db_equals_legacy"


def test_t12_default_port_equivalence() -> None:
    s = _settings(
        DEMO_DATABASE_URL="postgresql://u@localhost/haloflow_reminders_demo",
        DATABASE_URL="postgresql+asyncpg://v@127.0.0.1:5432/haloflow_reminders_demo",
    )
    with pytest.raises(StartupRefused) as exc:
        validate_demo_database_url(s)
    assert _reason(exc) == "demo_db_equals_legacy"


def test_t12_equal_to_test_database_url_refused() -> None:
    s = _settings(HALOFLOW_TEST_DATABASE_URL="postgresql://t@127.0.0.1:5433/haloflow_reminders_demo")
    with pytest.raises(StartupRefused) as exc:
        validate_demo_database_url(s)
    assert _reason(exc) == "demo_db_equals_test"


def test_t12_unix_socket_and_loopback_accepted() -> None:
    for url in (
        GOOD_DB,
        "postgresql://u@localhost/haloflow_reminders_demo",
        "postgresql:///haloflow_reminders_demo?host=/tmp",
        "postgresql+asyncpg://u@[::1]:5433/haloflow_reminders_demo",
    ):
        target = validate_demo_database_url(_settings(DEMO_DATABASE_URL=url))
        assert target.dbname == "haloflow_reminders_demo"


def test_t12_query_host_overrides_cannot_smuggle_remote_host() -> None:
    url = "postgresql://u@127.0.0.1/haloflow_reminders_demo?host=db.example.com"
    with pytest.raises(StartupRefused) as exc:
        validate_demo_database_url(_settings(DEMO_DATABASE_URL=url))
    assert _reason(exc) == "demo_db_not_local"


# ── T16 (R-11) ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cap", ["0", "11", "-1", "ten", "1.5"])
def test_t16_cap_out_of_bounds_refused(cap: str) -> None:
    with pytest.raises(StartupRefused) as exc:
        validate_startup(_settings(DEMO_SMS_DAILY_CAP=cap))
    assert _reason(exc) == "sms_daily_cap"


@pytest.mark.parametrize("cap", ["1", "10"])
def test_t16_cap_bounds_accepted(cap: str) -> None:
    validate_startup(_settings(DEMO_SMS_DAILY_CAP=cap))


# ── R-9 phone normalisation and masking ───────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+15550100199", "+15550100199"),
        ("(555) 010-0199", "+15550100199"),
        ("1-555-010-0199", "+15550100199"),
        ("", None),
        ("abc", None),
        ("+445550100199", None),
        ("555010019", None),
    ],
)
def test_r9_normalize(raw: str, expected: str | None) -> None:
    assert normalize_us_phone(raw) == expected


def test_r9_mask_keeps_only_last_two_digits() -> None:
    assert mask_phone("+15550100199") == "+1********99"


def test_r9_invalid_test_number_is_none_not_error() -> None:
    assert _settings(DEMO_SMS_TEST_NUMBER="abc").sms_test_number is None
    assert _settings().sms_test_number is None


def test_clinic_tz_invalid_refused() -> None:
    with pytest.raises(StartupRefused) as exc:
        validate_startup(_settings(CLINIC_TZ="Mars/Olympus"))
    assert _reason(exc) == "clinic_tz"
