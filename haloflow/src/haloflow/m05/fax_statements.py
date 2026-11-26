"""Incoming fax routing FX-IN, package FX3a: the statement keys of the fax gateways (t139, t142,
t143, t144). SYNTHETIC composition only (`build_synthetic_catalog(..., include_fx3=True)`).

Authorization is per key (Codex e9c224a9):
- RECEIVE_KEY is held only by the fax ingress workload context (CAPABILITY_INGRESS);
- LINES_KEY only by the trusted startup context that builds the line directory
  (CAPABILITY_LINES);
- READ_KEY and OPEN_KEY need the staff read capability. OPEN_KEY appends a constant right-hand
  `jsonb_build_object('action', 'open')` (built, because the catalogue refuses any double quote
  in a statement), so the operation is fixed to open on the SQL side and a caller-supplied
  action cannot override it (jsonb `||` keeps the right operand's value);
- ACT_KEY (move / file / sort) needs m05.followup.apply, held only with the `act` route.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode

CAPABILITY_INGRESS: Final = "m05.fax.ingress"
CAPABILITY_LINES: Final = "m05.fax.lines"
READ_CAPABILITY: Final = "m05.followup.read"
APPLY_CAPABILITY: Final = "m05.followup.apply"

RECEIVE_KEY: Final = "m05.fax.receive"
LINES_KEY: Final = "m05.fax.lines"
READ_KEY: Final = "m05.fax.read"
OPEN_KEY: Final = "m05.fax.open"
ACT_KEY: Final = "m05.fax.act"

FX3_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    RECEIVE_KEY: (StatementMode.WRITE, CAPABILITY_INGRESS,
                  "SELECT m05_fx_receive(%(command)s)"),
    LINES_KEY: (StatementMode.READ, CAPABILITY_LINES,
                "SELECT m05_fx_lines_snapshot(%(command)s)"),
    READ_KEY: (StatementMode.READ, READ_CAPABILITY,
               "SELECT m05_fx_staff_read(%(command)s)"),
    OPEN_KEY: (StatementMode.WRITE, READ_CAPABILITY,
               "SELECT m05_fx_staff_action(%(command)s OPERATOR(pg_catalog.||)"
               " pg_catalog.jsonb_build_object('action', 'open'))"),
    ACT_KEY: (StatementMode.WRITE, APPLY_CAPABILITY,
              "SELECT m05_fx_staff_action(%(command)s)"),
})

# FX-IN FX4 (target 149): the queue read (t145) and the receive / read keys rebound to the v2
# functions (t148 / t149; the unit framework never replaces a function).
QUEUES_KEY: Final = "m05.fax.queues"
FX4_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    QUEUES_KEY: (StatementMode.READ, READ_CAPABILITY, "SELECT m05_fx_staff_queues(%(command)s)"),
})
FX4_REBOUND: Final[StatementDefinitions] = MappingProxyType({
    RECEIVE_KEY: (StatementMode.WRITE, CAPABILITY_INGRESS,
                  "SELECT m05_fx_receive_v2(%(command)s)"),
    READ_KEY: (StatementMode.READ, READ_CAPABILITY, "SELECT m05_fx_staff_read_v2(%(command)s)"),
})
