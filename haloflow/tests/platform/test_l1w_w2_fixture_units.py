"""FX-01: the fixture-login isolation guard (Codex final disposition pin 4). No database."""

from __future__ import annotations

import l1w_db_support as db
import pytest


@pytest.mark.parametrize(
    ("environ", "database"),
    [
        ({}, "haloflow_test_x"),
        ({db.DISPOSABLE_ENV: "0"}, "haloflow_test_x"),
        ({db.DISPOSABLE_ENV: "true"}, "haloflow_test_x"),
        ({db.DISPOSABLE_ENV: "1"}, "haloflow_dev"),
        ({db.DISPOSABLE_ENV: "1"}, "postgres"),
        ({db.DISPOSABLE_ENV: "1"}, "test_haloflow"),
    ],
    ids=["no-flag", "flag-0", "flag-true", "dev-db", "postgres-db", "prefix-not-leading"],
)
def test_fx01_guard_refuses_outside_a_disposable_test_cluster(
    environ: dict[str, str], database: str
) -> None:
    with pytest.raises(db.FixtureIsolationRefused):
        db.check_fixture_isolation(environ, database)


def test_fx01_guard_admits_flag_and_prefix() -> None:
    db.check_fixture_isolation({db.DISPOSABLE_ENV: "1"}, "haloflow_test_l1w")
