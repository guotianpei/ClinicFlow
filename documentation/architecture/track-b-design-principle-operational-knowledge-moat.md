# Halo Holdings — Track B
## Design Principle: Operational Knowledge as Moat (Forward-Compatible Data Logging)

*Status: Adopted design principle — not a build item*
*Last updated: August 8, 2026*
*Origin: Strategic discussion on defensibility given falling AI-coding costs; cross-checked against ChatGPT analysis*

---

## The principle

Features and code are not the moat — a competent competitor with AI coding tools can reproduce visible functionality (eligibility checks, reminders, fax routing, voice) in weeks. What compounds and can't be quickly copied is:

> **The accumulated operational knowledge of how to successfully complete healthcare administrative work for a specific clinic, EMR, and payer mix — captured as structured data, not just as tacit staff/founder knowledge.**

Examples of what this looks like in practice:
- This payer returns ambiguous eligibility responses under these conditions, and here's the fallback that worked
- This EMR field silently fails to update under this condition
- This exception type resolves this way, this percentage of the time, with this intervention

This is a **long-term positioning principle**, not an instruction to build a knowledge-layer architecture now.

---

## Why this matters now, even at single-clinic scale

We are not building the knowledge layer today. But every Tier 2 workflow we ship (eligibility, reminders, fax routing, care-gap outreach) will generate exception and outcome data regardless. The only design decision needed **right now** is:

**Don't let that data get thrown away by the schema.**

Capturing *why* something failed and *how* it resolved, rather than only *whether* it succeeded, costs very little at design time and preserves the option to build on it later — once there's more than one clinic to generalize across.

---

## What to do now (applies to the in-progress ERD / schema)

When designing the tenant schema tables that already exist in scope — `error_queue`, `reconciliation_cases`, `work_queue`, `batch_recipient_operations`, and the eligibility/fax workflow tables — include fields for:

- **Failure/exception classification** (not just a status enum — a reason code or category)
- **Resolution path taken** (what fixed it, or what the fallback was)
- **Outcome** (did the resolution work, did it require manual override)

This is additive to the already-signed-off Requirements v2.4 / ADR v4 schema intent — it does not change the architecture, tenant isolation model, or registry-insert-before-send pattern (ADR-002). It's a field-level discipline to apply during ERD design, not a new module.

---

## What NOT to build yet

Explicitly deferred, pending multi-clinic scale and a separate compliance review:

- A cross-clinic "Operational Intelligence Layer" with first-class objects like `PayerProfile`, `EMRCapability`, `ExceptionPattern`, `AutomationConfidence`, etc.
- A cross-tenant "Payer Operational Graph" or shared knowledge base
- Any aggregation of outcome data across clinics/tenants

**Reason:** this is currently a single-pilot, single-EMR (CGM eMDs) engagement. A knowledge layer only becomes a real asset once there's more than one clinic's data to generalize across — building it now is premature architecture, not moat-building.

---

## The compliance gate this triggers later (not now, but flagged early)

Cross-clinic aggregation of exception/outcome data — even de-identified — is a **compliance-architecture decision, not just an engineering one**. Before any cross-tenant knowledge layer is built:

- Confirm what can legally be pooled across tenants under the applicable BAAs
- Determine de-identification requirements for any shared/aggregate data
- Route through the same Tier 1 discipline (requirements → architecture → sign-off) as any other PHI-adjacent design decision

This gate should be revisited when a second clinic is being onboarded, not before.

---

## Summary

| Now (Tier 2, single pilot) | Later (multi-clinic) |
|---|---|
| Log failure reason + resolution path in existing tenant schema tables | Build a structured cross-clinic knowledge layer |
| No new architecture, no new compliance review needed | Requires its own compliance/BAA review before build |
| Cost: near-zero, folds into current ERD work | Cost: meaningful — new module, new data governance |

**Bottom line:** preserve the exhaust data now; don't build the refinery until there's enough flow to justify it.
