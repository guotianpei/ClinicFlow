"""2A-F01: one shared-fixture initialization per session (architecture v4 §6.3).

Run as `pytest tests/m01 tests/m02`. This consumer shares `role_logins` with
M01 and reports the counter kept by the root `tests/conftest.py`. Revision-
specific rows (2A-R05, 2A-M02) that touch roles or run revision SQL themselves
are outside this count.
"""

import pytest

pytestmark = pytest.mark.postgres


def test_2a_f01_shared_fixtures_initialize_once_per_session(
    role_logins: dict[str, str], shared_fixture_setups: dict[str, int]
) -> None:
    assert role_logins  # this consumer really depends on the shared session state
    assert shared_fixture_setups == {"migrated_database": 1, "role_logins": 1}
