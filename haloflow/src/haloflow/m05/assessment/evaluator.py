"""D1 offline evaluator (architecture v2 decision sequence 1-7; contract pins P2/P4).

Pure: one synthetic appointment revision in, a question-level assessment, obligations,
ranked proposed actions and a reproducible trace out. No queue or adapter receives the
actions; nothing is sent, written or mutated (`effects` is always zero).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any, Final

from .bundle import BundleRejected, validate_bundle
from .canonical import CanonicalError, canonical_bytes, digest, normalize
from .expressions import UNKNOWN, eval_expr
from .zones import ZoneData, ZoneMissing, local_datetime

EVALUATOR_VERSION: Final = "d1.1"
ZERO_EFFECTS: Final = {"sends": 0, "db_writes": 0, "staff_mutations": 0, "ehr_writes": 0}

_INSTANT: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_COVERAGE_TYPES: Final = frozenset({"M", "B", "A", "N", "deductible", "utilization"})
_SERVICE_TYPES: Final = frozenset({"B", "A", "deductible", "utilization"})
_META_TYPES: Final = frozenset({"category", "priority", "policy"})
_ABSENT_REASON: Final = {"N": "participation_absent"}
_STATUS_ACTIONS: Final = frozenset({"WAIT_AUTO", "WAIT_REPLY"})
_RESOLVED: Final = frozenset({"adequate", "justified"})

Json = dict[str, Any]


class _Reject(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _parse_instant(text: Any) -> datetime:
    if not isinstance(text, str) or not _INSTANT.match(text):
        raise _Reject("instant_precision")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _known(value: Any) -> bool:
    return value is not None and not (isinstance(value, Mapping) and "unknown" in value)


def _rejected(code: str, req: Any) -> Json:
    request_digest = digest(req) if isinstance(req, Mapping) else None
    return {
        "status": "rejected", "rejection": {"code": code}, "applicability": "unresolved",
        "configuration_exception": None, "questions": [], "completion": False,
        "obligations": [], "follow_up": "undetermined", "actions": [], "primary": None,
        "primary_state": "blocked", "effects": dict(ZERO_EFFECTS),
        "trace": {"input_digest": request_digest, "evaluator_version": EVALUATOR_VERSION,
                  "criteria": []},
        "replay_record": None,
    }


def evaluate(request: Mapping[str, Any], *, zones: ZoneData) -> Json:
    """Evaluate one canonical request. Never mutates `request`; never performs an effect."""
    try:
        req = normalize(request)
    except CanonicalError as exc:
        return _rejected(exc.code, None)
    try:
        return _Evaluation(req, zones).run()
    except _Reject as exc:
        return _rejected(exc.code, req)


class _Evaluation:
    def __init__(self, req: Json, zones: ZoneData) -> None:
        self.req = req
        self.zones = zones
        self.criteria: list[Json] = []
        self.conflicts: list[Json] = []

    # ------------------------------------------------------------------ rules

    def criterion(self, criterion: str, qtype: str, view: Mapping[str, Any],
                  question_key: str | None) -> Json | None:
        """Evaluate every rule of `criterion` for `qtype`; return the winning outcome.

        Matching rules (True -> `then`, Unknown -> `unknown`) at the most precedent
        (lowest) priority decide. Equal outputs coalesce; different outputs record a
        configuration exception. Rule load order never decides (rules are a canonical set).
        """
        rules = [r for r in self.req["snapshots"]["policy"]["rules"]
                 if r["criterion"] == criterion
                 and ("*" in r["question_types"] or qtype in r["question_types"])]
        rules.sort(key=lambda r: (r["priority"], r["rule_id"]))
        matched: list[tuple[int, str, Json]] = []
        for rule in rules:
            truth = eval_expr(rule["when"], view)
            if truth is UNKNOWN:
                branch, outcome = "unknown", dict(rule["unknown"])
            elif truth is True:
                branch, outcome = "true", dict(rule["then"])
            else:
                branch, outcome = "false", None
            self.criteria.append({
                "criterion": criterion, "question_key": question_key,
                "rule_id": rule["rule_id"], "rule_version": rule["version"],
                "branch": branch,
                "outcome": None if outcome is None else outcome.get("outcome"),
                "reason": None if outcome is None else outcome.get("reason"),
            })
            if outcome is not None:
                matched.append((rule["priority"], rule["rule_id"], outcome))
        if not matched:
            return None
        top = min(p for p, _, _ in matched)
        winners = [(rid, out) for p, rid, out in matched if p == top]
        outputs = {canonical_bytes(out) for _, out in winners}
        if len(outputs) > 1:
            self.conflicts.append({"criterion": criterion,
                                   "rule_ids": sorted(rid for rid, _ in winners)})
            return None
        return winners[0][1]

    # ------------------------------------------------------------------ run

    def run(self) -> Json:
        req = self.req
        ctx = req["context"]
        snapshots = req.get("snapshots")
        if not isinstance(snapshots, Mapping) or "policy" not in snapshots:
            raise _Reject("missing_content")
        self.instant = _parse_instant(req["evaluation_instant"])
        self._check_instants()
        try:
            validate_bundle(snapshots["policy"])
        except BundleRejected as exc:
            raise _Reject(exc.code) from exc
        self._check_record_versions()
        if ctx.get("environment") not in ("simulated", "sandbox"):
            raise _Reject("environment_mismatch")
        reference = snapshots.get("reference", {}).get("tzdata", {})
        if reference.get("release") != self.zones.release or \
                reference.get("digest") != self.zones.digest:
            raise _Reject("tzdata_mismatch")
        try:
            local = local_datetime(self.zones, ctx["clinic_zone"], self.instant)
        except ZoneMissing as exc:
            raise _Reject("zone_unavailable") from exc

        self.coverages = {c["coverage_id"]: c for c in ctx.get("coverages", [])}
        self.routes: Mapping[str, Any] = snapshots.get("routes", {})
        self.permissions = set(snapshots.get("permissions", []))

        applicability = self._applicability()
        questions, templates_matched = self._expand()
        if not templates_matched:
            applicability = "unresolved"
        if applicability == "unresolved" or not questions:
            questions.append(self._policy_question(applicability))
        for q in questions:
            if q["type"] not in _META_TYPES:
                self._assess(q, questions)
        self._dependencies(questions)
        obligations = self._obligations(questions)
        exception = self.conflicts[0] if self.conflicts else None
        actions = [] if exception else self._actions(obligations)

        open_obligations = [o for o in obligations if o["state"] != "closed"]
        if any(o["mode"] == "staff" for o in open_obligations):
            follow_up = "yes"
        elif open_obligations or applicability == "unresolved":
            follow_up = "undetermined"
        else:
            follow_up = "no"
        completion = (exception is None and applicability == "resolved"
                      and all(q["adequacy"] in _RESOLVED for q in questions
                              if q["requiredness"] == "required"))
        if actions:
            primary_state = "action"
        elif exception or applicability == "unresolved" or any(
                o["state"] == "blocked" for o in open_obligations):
            primary_state = "blocked"
        else:
            primary_state = "none"

        input_digest = digest(req)
        bundle_digest = digest(snapshots["policy"])
        tz = {"release": self.zones.release, "digest": self.zones.digest}
        public_questions = [{k: v for k, v in q.items() if not k.startswith("_")}
                            for q in questions]
        public_obligations = [{k: v for k, v in o.items() if not k.startswith("_")}
                              for o in obligations]
        return {
            "status": "assessed", "rejection": None, "applicability": applicability,
            "configuration_exception": exception, "questions": public_questions,
            "completion": completion, "obligations": public_obligations,
            "follow_up": follow_up, "actions": actions,
            "primary": {"code": actions[0]["code"]} if actions else None,
            "primary_state": primary_state, "effects": dict(ZERO_EFFECTS),
            "trace": {
                "evaluator_version": EVALUATOR_VERSION,
                "evaluation_instant": req["evaluation_instant"],
                "evaluation_local_date": local.date().isoformat(),
                "evaluation_local_time": local.strftime("%H:%M:%S"),
                "service_date": ctx.get("service_date"),
                "input_digest": input_digest, "bundle_digest": bundle_digest, "tzdata": tz,
                "context": {"stated_reason": ctx.get("stated_reason"),
                            "booked_category": ctx.get("booked_category")},
                "suppressed": self._suppressed(),
                "criteria": self.criteria,
            },
            "replay_record": {"request": req, "input_digest": input_digest,
                              "bundle_digest": bundle_digest, "tzdata": tz},
        }

    # ------------------------------------------------------------------ validation

    def _check_instants(self) -> None:
        for ev in self.req.get("evidence", []):
            if ev.get("observed_at") is not None:
                _parse_instant(ev["observed_at"])
            if ev.get("retrieved_at") is not None:
                _parse_instant(ev["retrieved_at"])
        for ob in self.req.get("work", {}).get("obligations", []):
            for name in ("due", "review"):
                if ob.get(name) is not None:
                    _parse_instant(ob[name])

    def _check_record_versions(self) -> None:
        seen: dict[tuple[Any, Any], bytes] = {}
        for ev in self.req.get("evidence", []):
            ident = (ev.get("evidence_id"), ev.get("version"))
            body = canonical_bytes(ev)
            if ident in seen and seen[ident] != body:
                raise _Reject("conflicting_record_version")
            seen[ident] = body

    # ------------------------------------------------------------------ questions

    def _applicability(self) -> str:
        ctx = self.req["context"]
        view = {"context.environment": ctx.get("environment"),
                "context.service_date": ctx.get("service_date"),
                "evaluation_instant": self.req["evaluation_instant"]}
        outcome = self.criterion("applicability", "*", view, None)
        if outcome is not None and outcome.get("outcome") == "unresolved":
            return "unresolved"
        return "resolved"

    def _expand(self) -> tuple[list[Json], bool]:
        ctx = self.req["context"]
        policy = self.req["snapshots"]["policy"]
        payers = {c.get("payer") for c in self.coverages.values()}
        services = [s for s in ctx.get("services", []) if s.get("confirmed") is True]
        questions: dict[bytes, Json] = {}
        matched_any = False
        for service in services:
            for template in policy["templates"]:
                if template["category"] != service.get("category"):
                    continue
                if template.get("payer") not in (None, *payers):
                    continue
                matched_any = True
                for qdef in template["questions"]:
                    for scope in self._scopes(qdef["type"], service):
                        q = self._question(qdef, scope, template)
                        key = canonical_bytes({"type": q["type"], "scope": q["scope"]})
                        if key in questions:
                            if q["requiredness"] == "required":
                                questions[key]["requiredness"] = "required"
                            questions[key]["_depends_on"] = sorted(
                                set(questions[key]["_depends_on"]) | set(q["_depends_on"]))
                        else:
                            questions[key] = q
        ordered = [questions[k] for k in sorted(questions)]
        return ordered, matched_any

    def _scopes(self, qtype: str, service: Mapping[str, Any]) -> list[Json]:
        ctx = self.req["context"]
        if qtype not in _COVERAGE_TYPES:
            return [{}]
        out = []
        for cov in self.coverages.values():
            scope: Json = {"coverage_id": cov["coverage_id"], "coverage_version": cov["version"]}
            if qtype in _SERVICE_TYPES:
                scope["service_id"] = service["service_id"]
            if qtype == "N":
                scope["provider"] = ctx.get("provider")
                scope["site"] = ctx.get("site")
            out.append(scope)
        return out

    def _question(self, qdef: Mapping[str, Any], scope: Json,
                  template: Mapping[str, Any]) -> Json:
        ctx = self.req["context"]
        key = digest({"schema": "d1.key.v1", "kind": "question", "tenant": ctx["tenant"],
                      "clinic": ctx["clinic"], "source_system": ctx["source_system"],
                      "appointment_id": ctx["appointment_id"], "type": qdef["type"],
                      "scope": scope})
        q: Json = {"key": key, "type": qdef["type"], "scope": scope,
                   "requiredness": qdef["requiredness"], "applicability": "applicable",
                   "finding": "unknown", "adequacy": "missing", "reason": "answer_absent",
                   "accepted": [], "rejected": [], "_depends_on": list(qdef["depends_on"])}
        if qdef["type"] == "category":
            if template.get("ambiguous") is True:
                q.update(finding="unknown", adequacy="unresolved", reason="ambiguous_category")
            else:
                q.update(finding="confirmed", adequacy="adequate", reason="category_match")
        elif qdef["type"] == "priority":
            priorities = [c.get("priority") for c in self.coverages.values()]
            if all(isinstance(p, int) for p in priorities) and len(set(priorities)) == len(
                    priorities):
                q.update(finding="ordered", adequacy="adequate", reason="order_known")
            else:
                q.update(finding="unknown", adequacy="unresolved", reason="order_unknown")
        return q

    def _policy_question(self, applicability: str) -> Json:
        ctx = self.req["context"]
        key = digest({"schema": "d1.key.v1", "kind": "question", "tenant": ctx["tenant"],
                      "clinic": ctx["clinic"], "source_system": ctx["source_system"],
                      "appointment_id": ctx["appointment_id"], "type": "policy", "scope": {}})
        q: Json = {"key": key, "type": "policy", "scope": {}, "requiredness": "required",
                   "applicability": applicability, "accepted": [], "rejected": [],
                   "_depends_on": []}
        if applicability == "unresolved":
            q.update(finding="unknown", adequacy="unresolved", reason="applicability_unknown")
        else:
            q.update(finding="applicable", adequacy="adequate", reason="template_match")
        return q

    def _suppressed(self) -> list[Json]:
        ctx = self.req["context"]
        confirmed = {s.get("category") for s in ctx.get("services", [])
                     if s.get("confirmed") is True}
        return [{"service_category": s.get("service_category"), "source": s.get("source"),
                 "reason": "unconfirmed_order"}
                for s in ctx.get("suggestions", [])
                if s.get("service_category") not in confirmed]

    # ------------------------------------------------------------------ evidence

    def _assess(self, q: Json, questions: list[Json]) -> None:
        ctx = self.req["context"]
        scope = q["scope"]
        typed = [e for e in self.req.get("evidence", []) if e.get("question_type") == q["type"]]
        candidates = []
        for ev in typed:
            subject = ev.get("subject", {})
            if "coverage_id" in scope and subject.get("coverage_id") != scope["coverage_id"]:
                continue
            ev_service = ev.get("scope", {}).get("service_id")
            if ("service_id" in scope and ev_service is not None
                    and ev_service != scope["service_id"]
                    and any(o["type"] == q["type"]
                            and o["scope"].get("service_id") == ev_service
                            and o["scope"].get("coverage_id") == scope.get("coverage_id")
                            for o in questions)):
                continue  # this record answers another question of the same type
            candidates.append(ev)

        if not candidates:
            if q["requiredness"] == "informational":
                q.update(finding="unknown", adequacy="informational", reason="optional_absent")
            elif typed:
                q.update(finding="unknown", adequacy="missing", reason="no_matching_scope")
            elif "service_id" in scope and ctx.get("appointment_version", 1) > 1:
                q.update(finding="unknown", adequacy="missing", reason="new_service")
            else:
                q.update(finding="unknown", adequacy="missing",
                         reason=_ABSENT_REASON.get(q["type"], "answer_absent"))
            return

        accepted: list[tuple[Json, str]] = []
        rejected: list[Json] = []
        for ev in sorted(candidates, key=lambda e: (e["evidence_id"], e.get("version", 0))):
            reason = self._mismatch(ev, q)
            if reason is None:
                outcome = self.criterion("acceptance", q["type"], self._evidence_view(ev, q),
                                         q["key"])
                if outcome is not None and outcome.get("outcome") == "reject":
                    reason = str(outcome.get("reason", "rejected"))
            if reason is not None:
                entry: Json = {"evidence_id": ev["evidence_id"], "reason": reason}
                if reason == "environment_mismatch":
                    entry["environment"] = ev.get("environment")
                rejected.append(entry)
                continue
            accepted.append((ev, self._freshness(ev, q)))
        q["rejected"] = rejected

        if not accepted:
            q.update(finding="unknown", adequacy="insufficient", reason=rejected[0]["reason"])
            return

        lineages: dict[str, list[tuple[Json, str]]] = {}
        for ev, fresh in accepted:
            lineages.setdefault(ev.get("upstream_txn") or ev["evidence_id"], []).append(
                (ev, fresh))
        reps: list[tuple[Json, str, list[str]]] = []
        for group in lineages.values():
            group.sort(key=lambda pair: pair[0]["evidence_id"])
            freshest = "fresh" if any(f == "fresh" for _, f in group) else (
                "unknown" if any(f == "unknown" for _, f in group) else "stale")
            reps.append((group[0][0], freshest, sorted({str(e.get("origin")) for e, _ in group})))
        reps.sort(key=lambda r: r[0]["evidence_id"])
        q["accepted"] = [r[0]["evidence_id"] for r in reps]
        q["linked_origins"] = sorted({o for r in reps for o in r[2]})

        fresh_kinds = {r[0]["value"]["kind"] for r in reps if r[1] == "fresh"}
        if len(fresh_kinds) > 1:
            q.update(finding="disputed", adequacy="conflicting", reason="opposed_sources")
            return
        best = next((r for r in reps if r[1] == "fresh"), None) or next(
            (r for r in reps if r[1] == "unknown"), None) or reps[0]
        ev, fresh, _ = best
        finding, adequacy, reason = self._classify(ev, q)
        if fresh == "stale" and adequacy == "adequate":
            adequacy, reason = "stale", "window_expired"
        elif fresh == "unknown" and adequacy == "adequate":
            adequacy, reason = "insufficient", "observation_time_unknown"
        q.update(finding=finding, adequacy=adequacy, reason=reason)
        if "amount" in ev:
            q["amount"] = ev["amount"]

    def _mismatch(self, ev: Mapping[str, Any], q: Mapping[str, Any]) -> str | None:
        ctx = self.req["context"]
        scope = q["scope"]
        subject = ev.get("subject", {})
        ev_scope = ev.get("scope", {})
        if subject.get("patient") != ctx.get("patient"):
            return "subject_mismatch"
        if "coverage_version" in scope and subject.get("coverage_version") != scope[
                "coverage_version"]:
            return "coverage_version_mismatch"
        if ev.get("environment") != ctx.get("environment"):
            return "environment_mismatch"
        if ev_scope.get("service_date") not in (None, ctx.get("service_date")):
            return "service_date_mismatch"
        coverage = self.coverages.get(scope.get("coverage_id", ""), {})
        for dim in ("payer", "product"):
            if coverage and ev_scope.get(dim) not in (None, coverage.get(dim)):
                return "product_mismatch"
        if "service_id" in scope and ev_scope.get("service_id") not in (None, scope["service_id"]):
            return "service_mismatch"
        if q["type"] == "N":
            if ev_scope.get("provider") not in (None, ctx.get("provider")):
                return "provider_mismatch"
            if ev_scope.get("site") not in (None, ctx.get("site")):
                return "site_mismatch"
        return None

    def _evidence_view(self, ev: Mapping[str, Any], q: Mapping[str, Any]) -> Json:
        ctx = self.req["context"]
        view: Json = {
            "evidence.environment": ev.get("environment"),
            "evidence.source_class": ev.get("source_class"),
            "evidence.valid_from": ev.get("valid_from"),
            "evidence.valid_to": ev.get("valid_to"),
            "context.environment": ctx.get("environment"),
            "context.service_date": ctx.get("service_date"),
            "question.type": q["type"],
            "question.requiredness": q["requiredness"],
            "evaluation_instant": self.req["evaluation_instant"],
        }
        if ev.get("observed_at") is not None:
            age = self.instant - _parse_instant(ev["observed_at"])
            view["evidence.age_seconds"] = Fraction(
                age.days * 86400 * 10**6 + age.seconds * 10**6 + age.microseconds, 10**6)
        return {k: v for k, v in view.items() if v is not None}

    def _freshness(self, ev: Mapping[str, Any], q: Mapping[str, Any]) -> str:
        outcome = self.criterion("freshness", q["type"], self._evidence_view(ev, q), q["key"])
        if outcome is None:
            return "stale"
        if outcome.get("outcome") == "fresh":
            return "fresh"
        return "unknown"

    def _classify(self, ev: Mapping[str, Any], q: Mapping[str, Any]) -> tuple[str, str, str]:
        value = ev["value"]
        kind = value["kind"]
        ctx = self.req["context"]
        if kind == "active":
            return "active", "adequate", "current_member"
        if kind == "inactive":
            return "inactive", "adequate", "current_negative"
        if kind == "covered":
            ev_scope = ev.get("scope", {})
            explicit = "service_id" in ev_scope and "coverage_id" in ev_scope
            return "covered", "adequate", "scope_match" if explicit else "current_benefit"
        if kind == "excluded":
            return "excluded", "adequate", "explicit_exclusion"
        if kind == "participating":
            return "participating", "adequate", "current_participation"
        if kind == "required":
            return "required", "adequate", "authorization_required"
        if kind == "not_required":
            return "not_required", "adequate", "authorization_not_required"
        if kind == "conditional":
            conditions = value.get("conditions", {})
            for dim in sorted(conditions):
                if not _known(ctx.get(dim)):
                    return "conditional", "insufficient", f"{dim}_unknown"
            if all(ctx.get(dim) == want for dim, want in conditions.items()):
                return "required", "adequate", "authorization_required"
            return "not_required", "adequate", "condition_not_met"
        if kind == "pending":
            return "pending", "insufficient", "pending"
        if kind == "unsupported":
            return "unsupported", "insufficient", "operation_unsupported"
        if kind == "unavailable":
            return "unavailable", "insufficient", f"attempt_{value.get('cause', 'failure')}"
        if kind == "generic_membership":
            return "unknown", "missing", "generic_return"
        return "unknown", "missing", "answer_absent"

    def _dependencies(self, questions: list[Json]) -> None:
        for q in questions:
            for dep_type in q["_depends_on"]:
                for dep in questions:
                    if dep["type"] != dep_type:
                        continue
                    same_coverage = dep["scope"].get("coverage_id") in (
                        None, q["scope"].get("coverage_id"))
                    if same_coverage and dep["finding"] == "inactive" and \
                            dep["adequacy"] == "adequate":
                        q.update(finding="inapplicable", adequacy="justified",
                                 reason="dependency_inactive", applicability="inapplicable",
                                 accepted=[], rejected=[])

    # ------------------------------------------------------------------ obligations

    def _work_match(self, otype: str, qtype: str, scope: Mapping[str, Any]) -> Json | None:
        for ob in self.req.get("work", {}).get("obligations", []):
            if ob.get("type") == otype and ob.get("question_type") == qtype and all(
                    scope.get(k) == v for k, v in ob.get("scope", {}).items()):
                return dict(ob)
        return None

    def _closed_by_disposition(self, scope: Mapping[str, Any]) -> bool:
        for disp in self.req.get("work", {}).get("dispositions", []):
            for closes in disp.get("closes", []):
                if all(scope.get(k) == v for k, v in closes.items()):
                    return True
        return False

    def _obligation(self, otype: str, q: Mapping[str, Any] | None, *, mode: str,
                    state: str, queue: str, action: str, work: Mapping[str, Any] | None,
                    scope: Mapping[str, Any] | None = None, qtype: str | None = None) -> Json:
        ctx = self.req["context"]
        oscope = dict(q["scope"]) if q is not None else dict(scope or {})
        otype_q = q["type"] if q is not None else qtype
        key = digest({"schema": "d1.key.v1", "kind": "obligation", "tenant": ctx["tenant"],
                      "clinic": ctx["clinic"], "source_system": ctx["source_system"],
                      "appointment_id": ctx["appointment_id"], "type": otype,
                      "question_type": otype_q, "scope": oscope})
        ob: Json = {"key": key, "type": otype, "question_type": otype_q, "scope": oscope,
                    "state": state, "mode": mode, "queue": queue,
                    "due": (work or {}).get("due"), "review": (work or {}).get("review"),
                    "_action": action, "_question": q["key"] if q is not None else None}
        if state == "closed":
            ob["basis"] = "disposition" if self._closed_by_disposition(oscope) else "completed"
        return ob

    def _obligation_view(self, q: Mapping[str, Any], work: Mapping[str, Any] | None) -> Json:
        view: Json = {"question.finding": q["finding"], "question.adequacy": q["adequacy"],
                      "question.applicability": q["applicability"],
                      "question.type": q["type"],
                      "evaluation_instant": self.req["evaluation_instant"]}
        route = self.routes.get(q["type"])
        if route is not None:
            view["route"] = route
        if work is not None:
            for name in ("type", "state", "review", "due"):
                if work.get(name) is not None:
                    view[f"obligation.{name}"] = work[name]
        return view

    def _obligations(self, questions: list[Json]) -> list[Json]:
        out: list[Json] = []
        used: set[bytes] = set()

        def take(work: Mapping[str, Any] | None) -> None:
            if work is not None:
                used.add(canonical_bytes(work))

        by_type = {q["type"]: q for q in questions}
        for q in questions:
            if q["requiredness"] != "required":
                continue
            resolved = q["adequacy"] in _RESOLVED
            work = self._work_match("verification", q["type"], q["scope"]) or \
                self._work_match("clarification", q["type"], q["scope"])
            take(work)
            outcome = self.criterion("obligation", q["type"], self._obligation_view(q, work),
                                     q["key"]) or {}
            kind = outcome.get("outcome")
            if resolved:
                if kind in ("disposition", "handoff_auth"):
                    otype = str(kind)
                    existing = self._work_match(otype, q["type"], q["scope"])
                    take(existing)
                    action = "DISPOSE" if otype == "disposition" else "HANDOFF_AUTH"
                    state = "closed" if existing and existing.get("state") == "closed" \
                        else "ready"
                    out.append(self._obligation(otype, q, mode="staff", state=state,
                                                queue=str(outcome.get("queue", "clinic")),
                                                action=action, work=existing))
                if work is not None:
                    out.append(self._obligation(work["type"], q, mode="staff", state="closed",
                                                queue="verification", action="VERIFY",
                                                work=work))
                continue
            if work is not None and work.get("state") == "closed":
                out.append(self._obligation(work["type"], q, mode="staff", state="closed",
                                            queue="verification", action="VERIFY", work=work))
                continue
            ob = self._open_obligation(q, work, kind, by_type)
            if ob is not None:
                out.append(ob)

        for work in self.req.get("work", {}).get("obligations", []):
            if canonical_bytes(work) in used:
                continue
            state = "closed" if work.get("state") == "closed" else (
                "waiting" if work.get("state") == "waiting" else "ready")
            otype = work.get("type", "verification")
            action = {"documentation": "DOCUMENT", "disposition": "DISPOSE",
                      "handoff_auth": "HANDOFF_AUTH"}.get(otype, "VERIFY")
            queue = {"documentation": "chart", "disposition": "clinic",
                     "handoff_auth": "authorization"}.get(otype, "verification")
            out.append(self._obligation(otype, None, mode="staff", state=state, queue=queue,
                                        action=action, work=work, scope=work.get("scope", {}),
                                        qtype=work.get("question_type")))
        unique: dict[str, Json] = {}
        for ob in out:
            unique.setdefault(ob["key"], ob)
        return [unique[k] for k in sorted(unique)]

    def _open_obligation(self, q: Json, work: Mapping[str, Any] | None, kind: Any,
                         by_type: Mapping[str, Json]) -> Json | None:
        qtype = q["type"]
        if qtype in _META_TYPES:
            if qtype == "policy" and kind != "clarification":
                return None
            action = {"priority": "CLARIFY_PRIORITY"}.get(qtype, "CLARIFY_CATEGORY")
            return self._obligation("clarification", q, mode="staff", state="ready",
                                    queue="verification", action=action, work=work)
        if q["finding"] == "conditional" and q["reason"].endswith("_unknown"):
            dim = q["reason"][: -len("_unknown")].upper()
            return self._obligation("clarification", q, mode="staff", state="ready",
                                    queue="verification", action=f"CLARIFY_{dim}", work=work)
        missing = q["adequacy"] == "missing"
        if kind == "review_due":
            if missing:
                q["reason"] = "review_due"
            ob = self._obligation("verification", q, mode="staff", state="ready",
                                  queue="verification", action="REVIEW_REPLY", work=work)
        elif work is not None and work.get("contact") == "awaiting_reply":
            if missing:
                q["reason"] = "awaiting_reply"
            ob = self._obligation("verification", q, mode="staff", state="waiting",
                                  queue="verification", action="WAIT_REPLY", work=work)
        elif work is not None and work.get("recovery") == "pending":
            if missing:
                q["reason"] = "recovery_pending"
            ob = self._obligation("verification", q, mode="system", state="waiting",
                                  queue="system", action="WAIT_AUTO", work=work)
        elif work is not None and work.get("recovery") == "exhausted":
            if missing:
                q["reason"] = "recovery_exhausted"
            ob = self._obligation("verification", q, mode="staff", state="ready",
                                  queue="verification", action="VERIFY", work=work)
        elif q["adequacy"] == "conflicting":
            ob = self._obligation("verification", q, mode="staff", state="ready",
                                  queue="verification", action="RESOLVE_CONFLICT", work=work)
        elif kind == "unresolved":
            ob = self._obligation("verification", q, mode="staff", state="blocked",
                                  queue="verification", action="VERIFY", work=work)
        elif kind is None and self.routes.get(qtype) == "automatic_available":
            ob = self._obligation("verification", q, mode="system", state="ready",
                                  queue="system", action=f"CHECK_{qtype}", work=work)
        else:
            ob = self._obligation("verification", q, mode="staff", state="ready",
                                  queue="verification", action="VERIFY", work=work)
        for dep_type in q["_depends_on"]:
            dep = by_type.get(dep_type)
            if dep is not None and dep["requiredness"] == "required" and \
                    dep["adequacy"] not in _RESOLVED and ob["state"] != "waiting":
                ob["state"] = "blocked"
                ob["_dependency_blocked"] = True
        return ob

    # ------------------------------------------------------------------ actions

    def _actions(self, obligations: list[Json]) -> list[Json]:
        outcome = self.criterion("action_priority", "*", {"obligation.type": "any"}, None)
        order = list((outcome or {}).get("order", []))
        actions: list[Json] = []
        for ob in obligations:
            if ob["state"] == "closed":
                continue
            code = ob["_action"]
            if code not in _STATUS_ACTIONS and code not in self.permissions:
                if ob["state"] == "ready":
                    ob["state"] = "blocked"
                continue
            if ob["state"] == "blocked" and not ob.get("_dependency_blocked"):
                state = "blocked"
            else:
                state = ob["state"]
            actions.append({"code": code, "mode": ob["mode"], "state": state,
                            "queue": ob["queue"], "due": ob["due"], "review": ob["review"],
                            "obligation_key": ob["key"]})

        def rank(a: Json) -> tuple[int, int, int, str, str, str]:
            prominent = 0 if (a["mode"] == "staff" and a["state"] == "ready") else 1
            index = order.index(a["code"]) if a["code"] in order else len(order)
            due = a["due"]
            return (prominent, index, 0 if due else 1, due or "", a["obligation_key"],
                    a["code"])

        actions.sort(key=rank)
        return actions
