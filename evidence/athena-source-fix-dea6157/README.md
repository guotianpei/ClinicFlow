# N-ATH fixes F-ATH-I1/I2 (Claude container, PG16 dev evidence)

Head dea6157c97d32cd731c6ae2e959b115f0667d85b. Full tests/demo_reminders 598 passed (587 + 11 new in test_athena_startup.py) exit 0; ruff demo package + tests exit 0; mypy --strict 7 modules exit 0. Frozen test files unchanged. Regression check: the 11 new tests against old head 3daeff2 → 10 failed, 1 passed (the exact-100 boundary case); some old-head failures are signature errors (run() lacked env/athena_transport), not only behaviour. Password masked.
