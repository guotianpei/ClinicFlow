"""Eligibility E5 mutation controls, group ``el5`` (M-E5-1..M-E5-16). SYNTHETIC ONLY.

Targets and killing rows predeclared in the E5 test cases v1r1 "Mutation controls" table
(Codex ALIGN 9e6974f2; architecture v1r1 A9 + A11):

M-E5-1  select manual routing by class + NULL verification (timer event documented)  P01c P01f
M-E5-2  a manual entry worded as a payer check                                         P02b
M-E5-3  the latest case answer instead of the event's own                              P02a P01e
M-E5-4  store uniqueness skipped (a second valid permit overwrites / adds an entry)    P03q
M-E5-5  re-append after an unknown outcome without close + read                         P03i P03j
M-E5-6  the doc item closed by case closure                                            P04b
M-E5-7  the tenant / appointment check skipped at the store                       P05b P05c P05e
M-E5-8  manual closure without close_permit                                            P04g
M-E5-9  reconciliation read budget unbounded                                           P03k
M-E5-10 permit binding taken from the op (request) instead of the event row            P05a
M-E5-11 a new permit while the previous one is unreconciled (open, unclosed)           P03o P03m
M-E5-12 the confirmation route appends                                                 P04c
M-E5-13 a fourth append attempt allowed                                                P03e
M-E5-14 append without a successful current-epoch mark-sent                            P03l
M-E5-15 settle accepts an entry under a permit the op did not issue                    P05f
M-E5-16 the reconciliation read counter is not durable (reset on re-claim)             P03k

Every mutant that edits SQL composed into the eligibility registry (rules selection, t129..t134
bodies) is RE-PINNED: the harness re-measures the ELIGIBILITY installed-state profile on the
mutated source before its rows run (b3_mutants.py `repin_profile`), so the kill is the row's,
never the seal's. Judged by the declared-rows rule of scripts/b3_mutants.py (as E4): the union
of the declared rows runs once on the clean source and must pass completely; a mutant is
KILLED when at least one of its declared rows fails or errors; every target is restored
byte-identically. The machinery is b3_mutants.py's, loaded unchanged with this mutant list.

Usage (from haloflow/):
    python3 -I scripts/el5_mutants.py --pytest "$VENV/bin/pytest" \\
        --evidence "$RUNNER_TEMP/verify-evidence/el5-mutation" [--mutant M-E5-1 ...] [--check]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Final

HERE = Path(__file__).resolve().parent
M05: Final = Path("src/haloflow/m05")
RULES, UNITS, DOC, CHART = (M05 / "eligibility_doc_rules.py", M05 / "eligibility_doc_units.py",
                            M05 / "eligibility_doc.py", M05 / "simulated_chart.py")
EP: Final = "tests/synthetic/test_el5_postgres.py::"


def _b3() -> Any:
    spec = importlib.util.spec_from_file_location("_b3_mutants", HERE / "b3_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _p(*names: str) -> tuple[str, ...]:
    return tuple(f"{EP}test_el5_{n}" for n in names)


def mutants(b3: Any) -> tuple[Any, ...]:
    m = b3.Mutant
    el = {"repin": True, "repin_profile": "eligibility"}
    return (
        m("M-E5-1", "manual routing selected by class + NULL verification (any reason)", RULES,
          "        f\" OR (e.verification_id IS NULL AND e.class = '{MANUAL_CLASS}'\"\n"
          "        f\" AND e.reason IN ({_in(tuple(DOC_MANUAL_REASONS))})))\")\n",
          "        f\" OR (e.verification_id IS NULL AND e.class = '{MANUAL_CLASS}'))\")\n",
          _p("p01c_real_timer_event_not_documented",
             "p01f_unlisted_manual_reason_fixture"), **el),
        m("M-E5-2", "a manual entry is worded as a payer check", RULES,
          "        return f\"{MANUAL_PREFIX}{REASON_LABELS[reason]}{MANUAL_SUFFIX}\"\n",
          "        return f\"{PAYER_PREFIX}{REASON_LABELS[reason]}\"\n",
          _p("p02b_manual_wording")),
        m("M-E5-3", "the entry uses the appointment's latest answer, not the event's own",
          UNITS,
          "         WHERE a.verification_id = v_e.verification_id AND a.state = 'answered'\n"
          "           AND a.answer IS NOT NULL\n"
          "         ORDER BY a.n DESC\n",
          "         WHERE a.verification_id IN (SELECT v.verification_id\n"
          "                 FROM {schema}.m05_el_verifications AS v\n"
          "                WHERE v.appointment_ref = (SELECT e2.appointment_ref\n"
          "                        FROM {schema}.m05_el_case_events AS e2\n"
          "                       WHERE e2.event_id = v_e.event_id))\n"
          "           AND a.state = 'answered' AND a.answer IS NOT NULL\n"
          "         ORDER BY a.finished_at DESC\n",
          _p("p02a_entry_uses_that_events_answer", "p01e_two_versions_two_entries"),
          also=((UNITS,
                 "          \" 'class', v_e.class, 'reason', v_e.reason, 'coverage_version',\"\n",
                 "          \" 'class', COALESCE(v_answer OPERATOR(pg_catalog.->>) 'class',"
                 " v_e.class), 'reason', COALESCE(v_answer OPERATOR(pg_catalog.->>) 'reason',"
                 " v_e.reason), 'coverage_version',\"\n"),), **el),
        m("M-E5-4", "store uniqueness skipped: a second valid permit overwrites the entry",
          CHART,
          "                        if entry is None:\n",
          "                        if True:\n",
          _p("p03q_destination_duplicate"),
          also=((CHART,
                 "                                \" (%s, %s, %s, %s, %s,"
                 " pg_catalog.clock_timestamp())\",\n",
                 "                                \" (%s, %s, %s, %s, %s,"
                 " pg_catalog.clock_timestamp()) ON CONFLICT (tenant, event_id) DO UPDATE"
                 " SET body = EXCLUDED.body, permit_id = EXCLUDED.permit_id\",\n"),)),
        m("M-E5-5", "an unknown append outcome is retried as a non-write (no close + read)",
          DOC,
          "        except Exception:  # noqa: BLE001 - timeout / lost acknowledgement: unknown\n"
          "            return await self._settle(context, op, \"unknown\")\n",
          "        except Exception:  # noqa: BLE001 - timeout / lost acknowledgement: unknown\n"
          "            return await self._settle(context, op, \"refused\", permit_closed=True)\n",
          _p("p03i_committed_ack_lost", "p03j_release_between_read_and_close")),
        m("M-E5-6", "the doc item is closed when its eligibility case closes", UNITS,
          "    GET DIAGNOSTICS v_n = ROW_COUNT;\n",
          "    UPDATE {schema}.m05_followup_items AS i SET state = 'resolved',\n"
          "           outcome_code = 'manual_documentation', version = i.version + 1\n"
          "     WHERE i.item_kind = 'eligibility_doc_check' AND i.state <> 'resolved'\n"
          "       AND EXISTS (SELECT 1 FROM {schema}.m05_el_case_events AS e\n"
          "                     JOIN {schema}.m05_el_cases AS c\n"
          "                       ON c.appointment_ref = e.appointment_ref\n"
          "                    WHERE e.event_id = i.source_event_id AND c.state = 'closed');\n"
          "    GET DIAGNOSTICS v_n = ROW_COUNT;\n",
          _p("p04b_item_survives_case_closure"), **el),
        m("M-E5-7", "the store skips its tenant and appointment checks", CHART,
          "                if entry is not None and entry[0] != str(appointment_ref):\n"
          "                    return REFUSED_MISMATCH\n",
          "                if False:\n"
          "                    return REFUSED_MISMATCH\n",
          _p("p05b_mismatched_first_write", "p05c_same_event_two_tenants",
             "p05e_permit_through_other_tenant_port"),
          also=((CHART,
                 "                elif row[0] != self._tenant:\n"
                 "                    outcome = TENANT_MISMATCH\n",
                 "                elif False:\n"
                 "                    outcome = TENANT_MISMATCH\n"),)),
        m("M-E5-8", "manual closure reads without closing the permit", DOC,
          "        closed = await self._close_quietly(port, op.get(\"permit_id\"))\n"
          "        if closed not in (None, sc.ALREADY_USED):\n"
          "            raise DocFailed(\"CLOSE_UNKNOWN\")\n",
          "        closed = None\n"
          "        if closed not in (None, sc.ALREADY_USED):\n"
          "            raise DocFailed(\"CLOSE_UNKNOWN\")\n",
          _p("p04fg_manual_vs_delayed_append")),
        m("M-E5-9", "the reconciliation read budget is unbounded", UNITS,
          "    ELSIF v_result = 'read_unavailable' THEN\n"
          "        IF v_op.reads >= \"\"\" + str(len(READ_DELAYS_S)) + r\"\"\" THEN\n",
          "    ELSIF v_result = 'read_unavailable' THEN\n"
          "        IF false THEN\n",
          _p("p03k_held_uncommitted_reads_unavailable"),
          also=((UNITS,
                 "    ELSIF v_result = 'charge_read' THEN\n"
                 "        IF v_op.reads >= \"\"\" + str(len(READ_DELAYS_S)) + r\"\"\" THEN\n",
                 "    ELSIF v_result = 'charge_read' THEN\n"
                 "        IF false THEN\n"),), **el),
        m("M-E5-10", "the permit binding is taken from the op, not the event row", UNITS,
          "    SELECT e.appointment_ref INTO v_ref FROM {schema}.m05_el_case_events AS e\n"
          "     WHERE e.event_id = v_op.event_id;\n\n"
          "    IF v_op.state = 'manual_pending' THEN\n",
          "    v_ref := v_op.appointment_ref;\n\n"
          "    IF v_op.state = 'manual_pending' THEN\n",
          _p("p05a_binding_from_event_row"), **el),
        m("M-E5-11", "a new permit while the previous one is still open (never closed)", DOC,
          "            closed = await self._close_quietly(port, permit) is None\n",
          "            closed = True\n",
          _p("p03o_already_used_is_not_a_nonwrite", "p03m_exhaustion_before_new_permit")),
        m("M-E5-12", "the confirmation route appends", DOC,
          "    async def _confirm(self, context: Any, port: sc.TenantChart, op: Mapping[str,"
          " Any]) -> str:\n        try:\n",
          "    async def _confirm(self, context: Any, port: sc.TenantChart, op: Mapping[str,"
          " Any]) -> str:\n        try:\n"
          "            await self._port(port.append(str(op.get(\"permit_id\")),"
          " {\"text\": \"x\"}))\n"
          "        except Exception:  # noqa: BLE001\n            pass\n        try:\n",
          _p("p04c_late_commit_confirmed_once")),
        m("M-E5-13", "a fourth append attempt is allowed", UNITS,
          "        IF v_op.attempts >= \"\"\" + str(APPEND_ATTEMPTS) + r\"\"\" THEN\n"
          "            v_failure := 'attempts_exhausted';\n",
          "        IF false THEN\n"
          "            v_failure := 'attempts_exhausted';\n",
          _p("p03e_three_attempts_then_needs_check"),
          also=((UNITS,
                 "    IF v_op.attempts >= \"\"\" + str(APPEND_ATTEMPTS) + r\"\"\" THEN\n"
                 "        UPDATE {schema}.m05_el_doc_ops SET state = 'needs_check',"
                 " lease_until = NULL,\n",
                 "    IF false THEN\n"
                 "        UPDATE {schema}.m05_el_doc_ops SET state = 'needs_check',"
                 " lease_until = NULL,\n"),), **el),
        m("M-E5-14", "the append runs without a successful current-epoch mark-sent", DOC,
          "        except DocFailed as failed:\n"
          "            if failed.code == \"EPOCH_STALE\":\n"
          "                return \"stale\"                       # no current-epoch marker:"
          " no append\n"
          "            raise\n",
          "        except DocFailed:\n"
          "            pass\n",
          _p("p03l_stale_epoch_cannot_mark_sent")),
        m("M-E5-15", "settle accepts an entry under a permit the op did not issue", UNITS,
          "          OR NOT ((v_entry OPERATOR(pg_catalog.->>) 'permit_id')::uuid\n"
          "                  = ANY (v_op.issued_permits)) THEN\n",
          "          OR false THEN\n",
          _p("p05f_foreign_permit_never_settles"), **el),
        m("M-E5-16", "the reconciliation read counter is reset on re-claim (not durable)",
          UNITS,
          "    ELSIF v_op.state = 'reconciling' THEN\n"
          "        UPDATE {schema}.m05_el_doc_ops SET epoch = epoch + 1, lease_until = \"\"\""
          " + _LEASE + r\"\"\",\n",
          "    ELSIF v_op.state = 'reconciling' THEN\n"
          "        UPDATE {schema}.m05_el_doc_ops SET epoch = epoch + 1, reads = 0,"
          " lease_until = \"\"\" + _LEASE + r\"\"\",\n",
          _p("p03k_held_uncommitted_reads_unavailable"), **el),
    )


def main(argv: list[str] | None = None) -> int:
    b3 = _b3()
    b3.MUTANTS = mutants(b3)
    return int(b3.main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
