# Track B — BAA & Vendor Selection Status Tracker

*Halo Holdings — HaloFlow / HaloVox Medical*
*Last updated: July 31, 2026*
*Owner: Rachel*

> **Purpose:** Single source of truth for vendor selection and BAA status across Track B (Tier 1–4). Update this file at the end of each working session — status changes, new replies received, new open items. Pick this file up at the start of the next session before resuming vendor/compliance work.

---

## How to read this table

- **Status** — `Locked` (fully confirmed, no action needed) / `Pending` (waiting on external reply) / `Disqualified` (ruled out) / `Backup` (not primary, kept in reserve) / `Not Selected`
- **BAA Cost** — confirmed cost to execute the BAA itself (separate from usage/subscription pricing)
- **Next Action** — what specifically needs to happen to close the item, and who it's waiting on

---

## Tier 2 — Pilot Vendor Stack (SMS, Fax, Eligibility)

| Vendor | Category | BAA Status | BAA Cost | Next Action | Last Updated |
|---|---|---|---|---|---|
| **Notifyre** | SMS + Fax | 🟢 Locked | Free (one BAA covers both) | None — closed | Jul 30 |
| **Stedi** | Insurance eligibility | 🟡 Pending | Not yet confirmed | Waiting on Stedi reply: (1) BAA/onboarding terms, (2) payer connectivity for Anthem/HealthKeepers, UnitedHealthcare, Sentara | Jul 30 |
| Telnyx | SMS (alternate) | 🔴 Disqualified | N/A | None — ruled out, self-contradictory BAA story never resolved | Jul 30 |
| Twilio | SMS (fallback) | ⚪ Not Selected | N/A | None — no longer needed now that Notifyre is locked | Jul 30 |
| SignalWire | SMS+fax+voice (fallback) | ⚪ Not Selected | N/A | None — priced for multi-channel consolidation, not pilot scope | Jul 30 |
| Availity | Eligibility (backup) | ⚪ Backup | Unconfirmed (ISV path unresolved) | Revisit only if a priority payer isn't well-covered via Stedi | Jul 29 |
| pVerify / Office Ally | Eligibility (deprioritized) | ⚪ Not Selected | N/A | None — no confirmed BAA; Office Ally looks portal-only | Jul 29 |

## Tier 3 — LLM Infrastructure

| Vendor | Category | BAA Status | BAA Cost | Next Action | Last Updated |
|---|---|---|---|---|---|
| **Google Vertex AI** | LLM (Claude access) | 🟢 Locked | Free, no usage surcharge (Google Cloud BAA umbrella) | Minor: confirm Vertex Gen AI *product-specific* HIPAA coverage page explicitly lists the generative endpoint (cloud-level BAA already covers it in principle) | Jul 31 |

## Tier 4 — Voice (deferred use, vendor pre-selected)

| Vendor | Category | BAA Status | BAA Cost | Next Action | Last Updated |
|---|---|---|---|---|---|
| **Retell AI** | Voice | 🟢 Locked | Free / no additional fee (self-serve, pay-as-you-go) | None — confirmed against Retell's own docs | Jul 30 |
| Vapi | Voice (rejected for Track B) | 🔴 Disqualified | ~$1,000/mo HIPAA add-on | None — unviable for free pilot, stays on Track A only | Jul 25 |

## Hosting

| Vendor | Category | BAA Status | BAA Cost | Next Action | Last Updated |
|---|---|---|---|---|---|
| **GCP** | Cloud hosting | 🟢 Locked | Free, no usage surcharge | None — confirmed via Google Cloud HIPAA compliance page | Jul 31 |
| AWS | Cloud hosting (alternate) | ⚪ Not Selected | Free (was confirmed via AWS Artifact) | None — GCP chosen for native HL7v2 store + Vertex consolidation | Jul 30 |
| Azure | Cloud hosting (not evaluated) | ⚪ Not Selected | N/A | None — not in approved charter list; would add a 3rd BAA relationship | Jul 29 |

---

## Open Items Requiring Follow-Up

| # | Item | Waiting On | Priority |
|---|---|---|---|
| 1 | Stedi BAA/onboarding + payer connectivity confirmation | Stedi (external reply) | High — last Tier 2 vendor not fully locked |
| 2 | Vertex AI Gen AI product-specific HIPAA page double-check | Self (quick doc check) | Low — cloud-level BAA already covers it |
| 3 | Notifyre OCR / HL7-conversion feature — confirm same BAA scope + timeline | Notifyre (follow-up question) | Low — Q3 feature, not blocking Tier 2 |

---

## Closed / Resolved This Cycle

- ✅ Notifyre BAA confirmed free, covers SMS + fax (Jul 30)
- ✅ Retell AI BAA confirmed free, self-serve, no enterprise minimum (Jul 30)
- ✅ GCP selected for hosting over AWS — native HL7v2 store, consolidates with Vertex under one cloud BAA (Jul 30)
- ✅ GCP BAA cost confirmed free with no HIPAA usage surcharge, direct from Google's HIPAA compliance page (Jul 31)

---

## Overall Tier 2 Vendor Selection Status

**Functionally complete pending one external reply (Stedi).** Do not mark fully closed until Stedi confirms.

*Session log: created Jul 31, 2026. Update the "Last Updated" column and Open Items table each session rather than rewriting the whole doc.*
