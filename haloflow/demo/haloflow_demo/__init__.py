"""Reminder demonstrator (two-week plan v3, outcome A): synthetic data only.

Self-contained by design (deviation D-1): nothing here imports the legacy
``haloflow.config`` / ``haloflow.database`` / ``haloflow.ehr`` /
``haloflow.integrations`` / ``haloflow.scheduler`` chain, which requires
``DATABASE_URL`` at import time and builds the legacy engine as a side effect.

Not production code. Authentication and tenant isolation are not provided.
"""
