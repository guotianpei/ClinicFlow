"""L-1S package S-C: the D-S1 activation policy of a composed tenant gateway.

`ENFORCE` makes the gateway probe the tenant's M05 activation record (t046) before the
first activation-protected statement of every transaction; `NOT_ENFORCED` never probes.
The policy is fixed when the gateway is composed: the production builder requires it
and startup maps the trusted `Deployment` to it (PRODUCTION -> ENFORCE). It is never
taken from a request or a context. M05-A1 is unchanged: startup still refuses a
production generation with M05 active, so ENFORCE is exercised only by tests today.
"""

from enum import Enum
from typing import Final

__all__ = ["PROBE_SQL", "ActivationPolicy"]


class ActivationPolicy(Enum):
    ENFORCE = "enforce"
    NOT_ENFORCED = "not_enforced"


# Fixed SQL, run on the gateway's own connection after the search_path is pinned; not a
# catalogue statement, so no capability can name it.
PROBE_SQL: Final = ("SELECT m05_activation_probe(),"
                    " pg_catalog.current_setting('transaction_isolation')")
