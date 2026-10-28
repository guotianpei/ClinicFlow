# Staff-screen tests r1 — baseline evidence (Claude container, PG16)

Test head: 485309a0c609d7a7c978364952cf978fe7834c46 (branch fix/staff-screen-gaps).
Code under test: unchanged demo package from e1da39c (no implementation commits on the branch).
Run from haloflow/ with DEMO_TEST_ADMIN_URL pointing at a throwaway local PG16 role.
Only change to captured files: the local throwaway role password is masked as `***`.
PG16 is development evidence only; it does not establish PostgreSQL 17 behaviour.

- cmd-targeted.txt / targeted.log: new tests + the two inbound restart/rollover tests → 24 failed, 9 passed, exit 1 (expected pre-implementation).
- cmd-full.txt / full.log: full tests/demo_reminders → 24 failed, 430 passed, exit 1. The 24 failures are exactly the expected ones (23 in test_staff_screen.py + test_writeback_after_restart_fails_closed); 430 = 422 unchanged existing tests + 8 new tests that already pass.
- environment.txt: interpreter, package versions, server version, dates. First targeted attempt at 15:21:50Z hit a stopped PostgreSQL (33 connection errors) and was discarded and rerun.
