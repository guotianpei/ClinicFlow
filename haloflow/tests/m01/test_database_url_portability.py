"""CP2-0 D-02 -- PORTABILITY-01. Stage-3 draft, packet v7. UNRUN against the repository.

Supersedes packet v6 (module sha256 32b5af4a...), which superseded v5
(516f0cd7...). Every v5 assertion is kept; the changes are listed in the
packet README.

THE CONTRACT THESE TESTS PIN
----------------------------
Requirements v4 (0e62551c...), owner-ruled, and architecture v3 (a8bd8273...),
owner-approved with Codex stage-2 PASS.

`_database_url_from(params, dbname)` returns a `postgresql://` URL that two
consumers read: psycopg directly, and SQLAlchemy after `alembic/env.py`
rewrites the scheme to `postgresql+psycopg://`. The helper:

* carries every supported, non-empty supplied value except `dbname` (R-3, R-11)
  and never invents one -- no `postgres`, `127.0.0.1` or `5432` (R-2);
* never reads the environment (R-3);
* omits every exact-empty value, for every key (R-6, owner Q-1 and Q-4);
* replaces `params["dbname"]` with the requested name (R-8);
* refuses a key libpq does not recognise, before any URL exists, with a fixed
  message and no exception chain (R-11, owner Q-5; architecture D-3/D-4);
* refuses a non-str value (D-5, owner Q-A) and an empty or non-str requested
  database name (D-7, owner Q-B), each with its own fixed message;
* yields the SAME effective mapping through both consumers (R-9).

WHAT THESE TESTS DELIBERATELY DO NOT DO
---------------------------------------
* No connection is opened. Whether the URL reaches a real PostgreSQL 17 as the
  owner's OS user is CP2-D01, a `D` row, and Rachel's to run.
* No assertion pins the literal URL text (R-4). What is asserted is what each
  consumer RECOVERS.
* Semantically invalid string values (for example `sslmode="nonsense"`) are NOT
  asserted to be refused. No public parse API validates values (architecture
  M11-M13); libpq rejects them only at connect time. That is a recorded
  non-claim, not a gap in this module.
"""

from collections.abc import Callable
from typing import Any

import pytest
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine.url import make_url

_LIBPQ_ENV = (
    "PGUSER",
    "PGPASSWORD",
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGDATABASE",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGPASSFILE",
    "PGSSLMODE",
    "PGOPTIONS",
    "PGAPPNAME",
    "PGCONNECT_TIMEOUT",
)

INVALID_OPTIONS = "invalid connection options"
NON_STR_VALUE = "connection option values must be str"
BAD_DBNAME = "requested database name must be a non-empty str"

BuildUrl = Callable[[dict[str, object], Any], str]

# The full supported key domain, taken from the linked libpq rather than from a
# hand-written list, minus `dbname` (whose replacement is R-8, not R-6). Only
# the KEYWORD names are used; the environment-dependent default VALUES are not.
_LIBPQ_KEYS = sorted(
    keyword
    for keyword in (option.keyword.decode() for option in pq.Conninfo.get_defaults())
    if keyword != "dbname"
)


@pytest.fixture(autouse=True)
def isolated_libpq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test states its own inputs completely.

    Autouse, so isolation cannot be forgotten in one case and silently make that
    case depend on the developer's shell.
    """

    for name in _LIBPQ_ENV:
        monkeypatch.delenv(name, raising=False)


def _psycopg_view(url: str) -> dict[str, str]:
    """What psycopg makes of the URL. Independent oracle, not the code under test."""

    return conninfo_to_dict(url)


def _alembic_view(url: str) -> dict[str, object]:
    """What alembic/env.py's rewritten URL becomes at the SQLAlchemy dialect.

    `create_connect_args` performs the real conversion without opening a
    connection -- the same path `create_engine` takes in env.py:38.
    """

    rewritten = url.replace("postgresql://", "postgresql+psycopg://", 1)
    sa_url = make_url(rewritten)
    return sa_url.get_dialect()().create_connect_args(sa_url)[1]


def _normalized(view: dict[str, Any]) -> dict[str, Any]:
    """R-9 permits DOCUMENTED normalization only, and exactly one is documented:
    `port` may arrive as an int in one view and a str in the other. Every other
    value is compared as-is, with no coercion, so a genuine type or value
    disagreement on any other key still fails.
    """

    normalized = dict(view)
    if isinstance(normalized.get("port"), int):
        normalized["port"] = str(normalized["port"])
    return normalized


def _views(url: str) -> dict[str, dict[str, str]]:
    """Both views, and the R-9 check that they agree, in one place."""

    psycopg_view = _normalized(_psycopg_view(url))
    alembic_view = _normalized(_alembic_view(url))
    assert psycopg_view == alembic_view, (
        "the two consumers recovered different mappings (R-9): "
        f"psycopg keys {sorted(psycopg_view)} vs alembic keys {sorted(alembic_view)}"
    )
    return {"psycopg": psycopg_view, "alembic": alembic_view}


def _assert_refused(build: Callable[[], object], message: str, *secrets: str) -> None:
    """A refusal is a ValueError with a FIXED message and NO exception chain.

    Architecture D-4 and M14: the helper-owned error is raised after the
    `except` block, so neither `__cause__` nor `__context__` can carry the
    library message, which may contain key text or values. Every supplied
    secret is checked against the message and repr as well.
    """

    with pytest.raises(ValueError) as caught:
        build()

    error = caught.value
    assert str(error) == message
    assert error.__cause__ is None, "library exception reachable via __cause__"
    assert error.__context__ is None, "library exception reachable via __context__"
    for secret in secrets:
        assert secret not in str(error)
        assert secret not in repr(error)


# --- CP2-U01: omission, not invention -------------------------------------


def test_cp2_u01_absent_user_and_host_are_omitted_for_libpq(database_url: BuildUrl) -> None:
    """CP2-U01 (v5, unchanged). Nothing supplied -> only the database name."""

    url = database_url({}, "haloflow_test_m01")

    for label, view in _views(url).items():
        assert view.get("dbname") == "haloflow_test_m01"
        assert "user" not in view, f"{label}: invented user {view.get('user')!r}"
        assert "host" not in view, f"{label}: invented host {view.get('host')!r}"


def test_cp2_u01b_supplied_postgres_and_loopback_are_preserved(database_url: BuildUrl) -> None:
    """CP2-U01b (v5 -> both views). The rule forbids INVENTING these values."""

    for view in _views(database_url({"user": "postgres", "host": "127.0.0.1"}, "db")).values():
        assert view["user"] == "postgres"
        assert view["host"] == "127.0.0.1"


# --- CP2-U19/U20: supplied values win; the environment is libpq's ---------


def test_cp2_u20_environment_is_left_for_libpq_not_copied_into_the_url(
    database_url: BuildUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP2-U20 (v5 -> both views). The helper does not read the environment."""

    monkeypatch.setenv("PGUSER", "env_user")
    monkeypatch.setenv("PGHOST", "env.example")

    for view in _views(database_url({}, "db")).values():
        assert "user" not in view
        assert "host" not in view


def test_cp2_u19_explicit_params_are_materialized_and_beat_the_environment(
    database_url: BuildUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP2-U19 (v5 -> both views). What IS supplied appears; env cannot override."""

    monkeypatch.setenv("PGUSER", "env_user")
    monkeypatch.setenv("PGHOST", "env.example")

    url = database_url({"user": "explicit", "host": "explicit.example"}, "db")

    for view in _views(url).values():
        assert view["user"] == "explicit"
        assert view["host"] == "explicit.example"


# --- CP2-U21: socket host, the measured trap ------------------------------


@pytest.mark.parametrize("socket_dir", ["/tmp", "/var/run/postgresql"])
def test_cp2_u21_socket_host_survives_both_parsers(database_url: BuildUrl, socket_dir: str) -> None:
    """CP2-U21 (v5, unchanged in substance)."""

    for view in _views(database_url({"user": "u", "host": socket_dir}, "db")).values():
        assert view["host"] == socket_dir
        assert view["dbname"] == "db", f"database name corrupted: {view.get('dbname')!r}"


def test_cp2_u21b_socket_host_with_explicit_port(database_url: BuildUrl) -> None:
    """CP2-U21b (v5, unchanged in substance)."""

    for view in _views(database_url({"user": "u", "host": "/tmp", "port": "5433"}, "db")).values():
        assert view["host"] == "/tmp"
        assert view["dbname"] == "db"
        assert view["port"] == "5433"


# --- CP2-U22: reserved characters through both parsers --------------------


@pytest.mark.parametrize(
    "password",
    [
        "p@ss word",
        "sl/ash",
        "co:lon",
        "que?ry#frag",
        "unicöde",
        "100%pct",
        "back\\slash",
        "amp&eq=semi;",
    ],
)
def test_cp2_u22_password_round_trips_through_both_parsers(
    database_url: BuildUrl, password: str
) -> None:
    """CP2-U22 (v5 + one case). `&`, `=` and `;` matter for the all-query form."""

    for view in _views(database_url({"user": "u", "password": password}, "db")).values():
        assert view["password"] == password
        assert view["user"] == "u"
        assert view["dbname"] == "db"


@pytest.mark.parametrize("user", ["plain", "with space", "with@at", "with/slash"])
def test_cp2_u22b_username_round_trips(database_url: BuildUrl, user: str) -> None:
    """CP2-U22b (v5 -> both views)."""

    for view in _views(database_url({"user": user}, "db")).values():
        assert view["user"] == user
        assert view["dbname"] == "db"


@pytest.mark.parametrize("user", ["plain", "with space", "with@at", "with/slash", "with?q"])
def test_cp2_u22c_username_survives_both_parsers(database_url: BuildUrl, user: str) -> None:
    """CP2-U22c (v5, unchanged in substance)."""

    for view in _views(database_url({"user": user}, "db")).values():
        assert view["user"] == user


@pytest.mark.parametrize(
    "dbname",
    ["plain_db", "db with space", "db/slash", "db?query", "d%b", "db&x=y", " "],
)
def test_cp2_u22d_reserved_characters_in_dbname_survive(
    database_url: BuildUrl, dbname: str
) -> None:
    """CP2-U22d (v5 + three cases). Architecture M1 is the regression here.

    A percent-encoded name in the URL PATH is decoded by psycopg but NOT by
    SQLAlchemy, so the two consumers address different databases. `" "` is
    included because D-7 says whitespace is a valid name.
    """

    for view in _views(database_url({"user": "u"}, dbname)).values():
        assert view["dbname"] == dbname


# --- CP2-U23: empty converges with absent (R-6) ---------------------------


def test_cp2_u23_explicit_empty_user_does_not_become_an_invented_default(
    database_url: BuildUrl,
) -> None:
    """CP2-U23 (v5 -> both views, tightened): an empty user is OMITTED."""

    for view in _views(database_url({"user": ""}, "db")).values():
        assert "user" not in view, f"explicit empty user became {view.get('user')!r}"


def test_cp2_u23b_empty_and_omitted_agree_while_pguser_is_set(
    database_url: BuildUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP2-U23b (v5 -> both views). With PGUSER set, empty and omitted still agree."""

    monkeypatch.setenv("PGUSER", "env_user")

    empty = _views(database_url({"user": ""}, "db"))
    omitted = _views(database_url({}, "db"))

    for label in ("psycopg", "alembic"):
        assert empty[label] == omitted[label], f"{label}: empty and omitted diverged"
        assert empty[label].get("user", "") == ""
        assert empty[label].get("user", "") != "env_user", "helper copied PGUSER"
        assert empty[label].get("user", "") not in ("postgres", "root")


def test_cp2_u23c_empty_and_omitted_user_agree_and_neither_is_a_fallback(
    database_url: BuildUrl,
) -> None:
    """CP2-U23c (v5 -> both views)."""

    empty = _views(database_url({"user": ""}, "db"))
    omitted = _views(database_url({}, "db"))

    for label in ("psycopg", "alembic"):
        assert empty[label] == omitted[label]
        for view in (empty[label], omitted[label]):
            assert view.get("user", "") not in ("postgres", "root")


def test_cp2_u23d0_the_key_domain_is_real() -> None:
    """CP2-U23d0 (NEW). Guards U23d against a vacuous parametrization.

    If libpq ever reported no keywords, U23d would collect zero cases and prove
    nothing. Every key exercised elsewhere in this module must be in the domain.
    """

    exercised = {
        "user",
        "password",
        "host",
        "port",
        "service",
        "sslmode",
        "options",
        "connect_timeout",
        "application_name",
        "target_session_attrs",
    }
    assert exercised <= set(_LIBPQ_KEYS), sorted(exercised - set(_LIBPQ_KEYS))
    assert "dbname" not in _LIBPQ_KEYS


@pytest.mark.parametrize("key", _LIBPQ_KEYS)
def test_cp2_u23d_every_exact_empty_value_is_omitted(database_url: BuildUrl, key: str) -> None:
    """CP2-U23d (NEW, R-6 generalized by owner Q-4). Omitted, for EVERY supported key.

    The domain is libpq's own keyword list minus `dbname`, so a key-specific
    empty filter cannot pass by covering only the keys someone thought of.
    """

    for label, view in _views(database_url({key: ""}, "db")).items():
        assert key not in view, f"{label}: empty {key} was emitted as {view.get(key)!r}"
        assert view == {"dbname": "db"}


def test_cp2_u23e_zero_is_not_empty(database_url: BuildUrl) -> None:
    """CP2-U23e (NEW). A zero-looking string is a value, not an absence.

    Scope, measured: for `str` values Python truthiness and `!= ""` agree, so this
    does NOT distinguish the two spellings (mutant M-c in the packet README is
    equivalent). The case it does pin is an implementation that treats "0" as
    unset -- for example by numeric coercion. Non-str values such as int 0 never
    reach empty handling; D-5 refuses them first (CP2-U32).
    """

    for view in _views(database_url({"connect_timeout": "0"}, "db")).values():
        assert view["connect_timeout"] == "0"


# --- CP2-U25: service configuration (R-7) ---------------------------------


def test_cp2_u25_service_is_preserved_without_invented_companions(
    database_url: BuildUrl,
) -> None:
    """CP2-U25 (v5 -> both views)."""

    for view in _views(database_url({"service": "haloflow_local"}, "db")).values():
        assert view.get("service") == "haloflow_local"
        assert "user" not in view
        assert "host" not in view


def test_cp2_u25b_service_survives_the_alembic_view_with_dbname_override(
    database_url: BuildUrl,
) -> None:
    """CP2-U25b (v5, unchanged in substance)."""

    for view in _views(database_url({"service": "haloflow_local"}, "override_db")).values():
        assert view["service"] == "haloflow_local"
        assert view["dbname"] == "override_db"
        for field in ("user", "host", "port"):
            assert field not in view, f"invented {field} beside a service definition"


def test_cp2_u25c_supplied_companions_of_service_are_preserved(database_url: BuildUrl) -> None:
    """CP2-U25c (NEW, R-7 as clarified in requirements v3). Supplied overrides stay."""

    url = database_url({"service": "haloflow_local", "host": "h1", "port": "5433"}, "db")

    for view in _views(url).values():
        assert view == {"service": "haloflow_local", "host": "h1", "port": "5433", "dbname": "db"}


# --- CP2-U28: the requested dbname wins (R-8) -----------------------------


def test_cp2_u28_requested_dbname_wins_over_the_one_in_params(database_url: BuildUrl) -> None:
    """CP2-U28 (v5 -> both views)."""

    source = {"user": "u", "host": "h", "port": "5433", "dbname": "haloflow_test_m01"}

    for view in _views(database_url(source, "haloflow_test_m01_preflight")).values():
        assert view == {
            "user": "u",
            "host": "h",
            "port": "5433",
            "dbname": "haloflow_test_m01_preflight",
        }


def test_cp2_u28b_dbname_override_survives_a_socket_and_a_password(
    database_url: BuildUrl,
) -> None:
    """CP2-U28b (v5, unchanged in substance)."""

    source = {"user": "u", "host": "/tmp", "password": "p@ss", "dbname": "original"}

    for view in _views(database_url(source, "override")).values():
        assert view["dbname"] == "override"
        assert view["host"] == "/tmp"
        assert view["password"] == "p@ss"


# --- CP2-U29: port and password defer exactly like user and host ----------


def test_cp2_u29_absent_port_is_not_materialized_as_5432(
    database_url: BuildUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP2-U29 (v5, unchanged in substance)."""

    monkeypatch.setenv("PGPORT", "5433")

    for label, view in _views(database_url({}, "db")).items():
        assert "port" not in view, f"{label}: invented port {view.get('port')!r}"


def test_cp2_u29b_explicit_port_is_materialized(database_url: BuildUrl) -> None:
    """CP2-U29b (v5 -> both views)."""

    for view in _views(database_url({"port": "5433"}, "db")).values():
        assert view["port"] == "5433"


def test_cp2_u29c_absent_password_is_not_materialized(
    database_url: BuildUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP2-U29c (v5, unchanged in substance)."""

    monkeypatch.setenv("PGPASSWORD", "env_secret")

    for label, view in _views(database_url({"user": "u"}, "db")).items():
        assert "password" not in view, f"{label}: password copied from the environment"


# --- CP2-U30: recognized non-core keys are carried (R-11) -----------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sslmode", "require"),
        ("options", "-c search_path=a,b -c x=y"),
        ("connect_timeout", "10"),
        ("application_name", "halo flow & co=1;?#/%"),
        ("target_session_attrs", "read-write"),
    ],
)
def test_cp2_u30_recognized_keys_are_carried_through_both_parsers(
    database_url: BuildUrl, key: str, value: str
) -> None:
    """CP2-U30 (NEW, R-11 / owner Q-3). Every supported key, reserved characters included."""

    for view in _views(database_url({key: value}, "db")).values():
        assert view == {key: value, "dbname": "db"}


def test_cp2_u30b_the_full_caller_shape_round_trips(database_url: BuildUrl) -> None:
    """CP2-U30b (NEW). The only caller's source is `conninfo_to_dict` output.

    Feed a realistic, fully populated conninfo through exactly that function,
    as test_gateway_postgres.py:966 does, and require an exact mapping back.
    """

    source = conninfo_to_dict(
        "host=/tmp port=5433 user='with@at' password='p w=&;' "
        "dbname=haloflow_test_m01 sslmode=disable application_name=hf"
    )

    expected = dict(source, dbname="haloflow_test_m01_preflight")
    for view in _views(database_url(source, "haloflow_test_m01_preflight")).values():
        assert view == expected


# --- CP2-U31: unrecognised keys are refused, with no leak (R-11, Q-5) -----


@pytest.mark.parametrize(
    "params",
    [
        {"bogus": "value-bogus-7f3a"},
        {"bogus": ""},  # refused BEFORE empty omission (architecture D-2)
        {"passwrd": "hunter2-9c1e", "user": "u"},  # a misspelling must not pass silently
        {"hunter2-secret-key": "value-secret-5b2d"},
        {"se\ncret\x00key": "value-ctrl-4e8f"},  # control characters in the key itself
    ],
)
def test_cp2_u31_unrecognised_key_is_refused_without_leaking(
    database_url: BuildUrl, params: dict[str, object]
) -> None:
    """CP2-U31 (NEW). Fixed message, no chain, and no key or value text escapes."""

    # Only distinctive strings are checked: a one-letter value such as "u"
    # would falsely match inside the fixed message itself.
    secrets = [str(key) for key in params if len(str(key)) > 3]
    secrets += [str(v) for v in params.values() if len(str(v)) > 3]
    _assert_refused(lambda: database_url(params, "db"), INVALID_OPTIONS, *secrets)


# --- CP2-U32: non-str values are refused (D-5, owner Q-A) -----------------


@pytest.mark.parametrize(
    "params",
    [{"port": 5433}, {"password": b"hunter2"}, {"user": None}, {"connect_timeout": 0}],
)
def test_cp2_u32_non_str_value_is_refused_without_coercion(
    database_url: BuildUrl, params: dict[str, object]
) -> None:
    """CP2-U32 (NEW). Its own message; no coercion; no chain; no value text."""

    secrets = [repr(v) for v in params.values() if v not in (None, 0)] + ["hunter2", "5433"]
    _assert_refused(lambda: database_url(params, "db"), NON_STR_VALUE, *secrets)


# --- CP2-U33: the requested database name (D-7, owner Q-B) ----------------


@pytest.mark.parametrize("dbname", ["", None, b"db", 0])
def test_cp2_u33_requested_dbname_must_be_a_non_empty_str(
    database_url: BuildUrl, dbname: object
) -> None:
    """CP2-U33 (NEW). No silent fall-back to libpq's default database."""

    _assert_refused(lambda: database_url({"user": "u"}, dbname), BAD_DBNAME)


# --- CP2-U34: the post-build guard (architecture D-3) ---------------------


def test_cp2_u34_post_build_guard_refuses_a_mismatched_parse(
    database_url: BuildUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP2-U34 (NEW, INTERFACE-COUPLED -- see packet README Q-3).

    The guard can only fire on an internal inconsistency, so it is reached by
    substituting the `conninfo_to_dict` the helper resolves at call time with
    one that returns a different mapping. The helper must then refuse with the
    fixed message and no chain -- and never return the URL it built.
    """

    helper_globals = database_url.__globals__  # type: ignore[attr-defined]
    real = helper_globals["conninfo_to_dict"]

    def tampered(conninfo: str = "", **kwargs: object) -> dict[str, str]:
        parsed = real(conninfo, **kwargs)
        return {**parsed, "user": "tampered"}

    monkeypatch.setitem(helper_globals, "conninfo_to_dict", tampered)

    _assert_refused(
        lambda: database_url({"user": "u", "password": "hunter2"}, "db"),
        INVALID_OPTIONS,
        "hunter2",
        "tampered",
    )
