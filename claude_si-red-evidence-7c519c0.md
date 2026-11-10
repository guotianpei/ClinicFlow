# S-I red tests — intended-red / control evidence at 7c519c014cec853d77c9cfee6e852a21a5a770c0

Captured by Claude, 2026-11-10, local run (not CI). Every count below is computed from the JUnit XML of the runs whose full logs are attached (a testcase is failed iff it has a <failure> or <error> child, tested with `is not None`). Generator: `si_evidence.py` (stdlib only).

## Environment (disclosed)

- Python: Python 3.13.18 (local venv, `pip install -e '.[dev]'`, NOT the lock). **CI uses Python 3.12 with the locked environment; this local run is not the CI environment.** The official intended-red measurement is the PR #85 CI run at this head.
- PostgreSQL: PostgreSQL 17.11 on x86_64-pc-linux-gnu, compiled by gcc (Ubuntu 7.5.0-3ubuntu1~18.04) 7.5.0, 64-bit — the embedded-postgres npm build 17.11.0-beta.19 (owner-approved 2026-11-10), not the CI postgres:17 image.
- Commands run from `haloflow/` with HALOFLOW_TEST_DATABASE_URL=postgresql://postgres:***@localhost:54317/haloflow_test_m01 and HALOFLOW_TEST_DISPOSABLE_CLUSTER=1.

## File hashes (sha256)

- `tests/m05/test_si_units.py` 0867fadb35ab61480353b9e55cea82aa4b8a335351621088c17181ba34df086f
- `tests/m05/si_support.py` 94daa24f94a6d2939403c53bf064a3fdf6681d8c5583ea68e9378ebad029193d
- `tests/m05/test_si_postgres.py` 87e0c3020cdd294e8ceacc255f8b2d1d0982288b919641060ad4c3c33c5057b6
- `tests/m05/test_si_service_postgres.py` bfcdbb2b4f0d9f5d0c46b96d26a5d272a36ce74fccdddcb81a9b6eeec4a18ffe
- `tests/m05/test_si_harness_postgres.py` 1b67c78eb8e1d6e1f90dc41cb92c6f63a6734435dc03ee97b8379fc5844a2cfc

## Command `units`

```
pytest tests/m05/test_si_units.py -rA -p no:cacheprovider --junitxml=units.xml
```
- Exit status: `1` (pytest: 1 = some tests failed)
- Full log: `claude_si-red-units-7c519c0.log`, sha256 5932049517b34fdac1406f933bcb4aa809f293252682f8e8ff5c53cf991a9580
- Summary line from the log: `66 failed in 2.94s`
- Test cases: 66; failed 66 (of which <error> 0); passed 0

### Failure causes, grouped (exact message)

- **32** × `ModuleNotFoundError: No module named 'haloflow.m05.writeback'`
- **34** × `ModuleNotFoundError: No module named 'haloflow.m05.simulated_ehr'`

### Chained root cause (every `ModuleNotFoundError` in each failure's full text)

- **32** × haloflow.m05.writeback
- **34** × haloflow.m05.simulated_ehr

### One representative traceback per group (last lines, unedited)

`tests.m05.test_si_units::test_si_uw01_05_wrapper_returns_every_h2_outcome_unchanged[accepted]`
```
    return future.result()
           ^^^^^^^^^^^^^^^
tests/m05/test_si_units.py:220: in body
    worker = _wb_mod().BaselineWorker(inner, capture)
             ^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

    def _wb_mod() -> Any:
>       import haloflow.m05.writeback as module
E       ModuleNotFoundError: No module named 'haloflow.m05.writeback'

tests/m05/test_si_units.py:134: ModuleNotFoundError
```
`tests.m05.test_si_units::test_si_ul01_delayed_writer_after_the_deadline_changes_nothing`
```
    ehr, permit = _ehr(clock), str(uuid.uuid4())
                  ^^^^^^^^^^^
tests/m05/test_si_units.py:162: in _ehr
    ehr = _ehr_mod().SimulatedEhr(clock=clock)
          ^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

    def _ehr_mod() -> Any:
>       import haloflow.m05.simulated_ehr as module
E       ModuleNotFoundError: No module named 'haloflow.m05.simulated_ehr'

tests/m05/test_si_units.py:128: ModuleNotFoundError
```

### Passed

- none

## Command `pg`

```
python -P -m pytest tests/m05/test_si_postgres.py tests/m05/test_si_service_postgres.py tests/m05/test_si_harness_postgres.py -rA -p no:cacheprovider --junitxml=pg.xml
```
- Exit status: `1` (pytest: 1 = some tests failed)
- Full log: `claude_si-red-pg-7c519c0.log`, sha256 5533a2c1dd1ec700cc38e20f479afd11e38724b1b1bc8df17e549693dbb6547d
- Summary line from the log: `101 failed, 1 passed in 9.73s`
- Test cases: 102; failed 101 (of which <error> 0); passed 1

### Failure causes, grouped (exact message)

- **100** × `AssertionError: S-I test installation failed: ModuleNotFoundError`
- **1** × `ModuleNotFoundError: No module named 'haloflow.m05.writeback_statements'`

### Chained root cause (every `ModuleNotFoundError` in each failure's full text)

- **100** × haloflow.m05.writeback_units
- **1** × haloflow.m05.writeback_statements

### One representative traceback per group (last lines, unedited)

`tests.m05.test_si_postgres::test_si_pd01_directory_v3_stores_identity_and_always_bumps_the_version`
```
                    sh2.settings_raw(self, clinic_display_name=sh2.CLINIC)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
>           raise AssertionError(
                f"S-I test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error
E           AssertionError: S-I test installation failed: ModuleNotFoundError

tests/m05/si_support.py:304: AssertionError
```
`tests.m05.test_si_postgres::test_si_pg02_each_rebound_key_has_exactly_one_binding_to_the_si_function`
```
def test_si_pg02_each_rebound_key_has_exactly_one_binding_to_the_si_function() -> None:
        """The real S-I TEST catalogue builds (no duplicate key from any contributing part)
        and each rebound key resolves to its S-I function; nothing else binds those
        functions' predecessors."""
    
>       from haloflow.m05.writeback_statements import SI_REBOUND
E       ModuleNotFoundError: No module named 'haloflow.m05.writeback_statements'

tests/m05/test_si_postgres.py:698: ModuleNotFoundError
```

### Passed

- `tests.m05.test_si_postgres::test_si_pd05_control_h3_v2_directory_unchanged` (CONTROL)

## Traceability (packet v1 requirements -> rows)

| Req | Rows |
|---|---|
| SI-R1 directory v3 + closure | P-D01..D05 (D05 CONTROL), P-G02 |
| SI-R2/R3 capture + ordering | P-C01..C06, U-W01b |
| SI-R4 wrapper | U-W01..W12, P-W10, P-W10b, P-W11 |
| SI-R5 WB2 matrix + precedence | P-M01..M21 (incl. M20 persisted NO/STOP/ambiguous), E02 |
| SI-R6 one write per permit | P-S01..S06, U-S06/S07, U-Q06 |
| SI-R7 apply deadline | U-L01..L08, P-L06, E04 |
| SI-R8 crash / lost ack | P-K01, P-Q01, P-I03b, P-W10b, U-K03/K04, U-H01..H04, P-P01, E05 |
| SI-R9 reconciliation | P-R01..R04, P-Q01, U-Q01..Q05, E04 |
| SI-R10 retry / escalation (owner C5) | U-T01..T04 (model-level), P-Y01 (stored r1_failures / next_at), E03 |
| SI-R11 staff item (Option A) | P-I01..I08 incl. I02b, I03b, I06b, I07b; E02 |
| SI-R12 activation | P-A01..A03 |
| SI-R13 tenant / authorization | P-T01..T04 (service; T02/03 statement spy + transaction count), P-I07b, P-G01 |
| SI-R14 concurrency | P-C06a/b, P-I04, P-M21 |
| SI-R15 restart | P-P01, U-L07[restart], E05 |
| SI-R16 privacy | U-V01, P-V01 |
| SI-R17 not composed / immutable | U-N01, P-G01, P-G02 |
| SI-R18 end to end | E01..E06 |


