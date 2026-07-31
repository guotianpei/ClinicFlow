# Track B — EMR Integration Backlog

*Halo Holdings — HaloFlow / HaloVox Medical*
*Last updated: July 31, 2026*
*Owner: Rachel*

> **Purpose:** Tracks EMR/EHR systems beyond the pilot clinic's CGM eMDs — future integration targets once the platform expands past the single pilot. Not required for pilot. Pick this file up alongside `track-b-vendor-baa-status.md` at the start of a session touching EMR/integration scope.

---

## Pilot EMR — Resolved

| EMR | Status | Notes |
|---|---|---|
| **CGM eMDs** | 🟢 Confirmed, Tier 1 in progress | Pilot clinic's actual EMR (corrected from initial Epic assumption — confirmed via version 10.1.3202.6541). Customer (clinic) controls app authorization directly — no larger health-system governance layer, unlike Epic. Annual API fee: **$1,200/yr** (1–5 providers tier) — needs to be added to infrastructure cost model; billed to the clinic under their CGM license. **Developer Account registration is NOT self-serve** — requires contacting a CGM account executive, and CGM verifies the clinic has already purchased the FHIR API connector before registration completes. Real dependency: clinic must confirm the connector is purchased/active and provide an introduction to the *clinic's* account executive (not Halo's — Halo has no standing CGM relationship) before registration can start. |
| CGM eMDs — additional access paths (researched, not primary) | Informational | **No classic proprietary REST API** beyond FHIR — unlike athenahealth/eClinicalWorks, CGM has standardized on FHIR as its only general-purpose API surface. **eMDs Data Gateway** exists as a second, older path — CCD (document-level) access only, gated via "Solution Series Sales" (API@emds.com) + clinic-supplied tokens, better suited to document/referral-letter workflows (Tier 3) than Tier 2 transactional needs. **HL7 v2 support unconfirmed publicly** for CGM eMDs itself (confirmed only for the separate CGM LabDAQ lab product) — likely exists for lab-result intake but not something Halo's automation layer needs to touch directly. **Certified Partner Integration Program** (Phreesia, AutoRemind, Intrado, CGM CONNECTION) confirmed real via Phreesia's own press release, but **no public application path found** — appears relationship/sales-driven, not self-serve. Realistic access is likely only through the clinic's existing CGM account-exec relationship, not cold outreach. Treat as a longer-term aspiration, not a near-term parallel option; **FHIR remains the pragmatic Tier 2 target** — sufficient for read + status-writeback use cases (reminders, confirmation status), likely insufficient for full scheduling ops (create/reschedule), which is where certified-partner-tier access would actually matter (Tier 3). |

---

## Immediate Action — Prototyping, Decoupled from Clinic Dependency

**Decision (Jul 31, 2026):** Rachel needs early prototyping to start now, independent of whether the current pilot clinic (CGM eMDs) stays in — if it falls through, a replacement clinic will be found, but engineering shouldn't wait on that outcome.

**athenahealth is the immediate prototyping target**, not just the #1 backlog priority:
- Self-serve developer registration at developer.athenahealth.com — sandbox credentials issued immediately, no approval gate, no clinic relationship required
- Sandbox uses synthetic test data — **no BAA needed to start**, since no real PHI is touched until production
- 800+ documented REST/FHIR endpoints — broader surface than CGM eMDs' FHIR-only path
- Zero cost gate before production (Marketplace program has commercial terms, but sandbox/dev access itself is free)

**Plan:** build Tier 2 logic (reminders/confirmation writeback, eligibility-status ingestion, fax-routing metadata handling) against athenahealth's sandbox now, using the existing Track A/B adapter-pattern architecture so the core logic is EMR-agnostic. Whichever real clinic and EMR eventually goes to production (CGM eMDs with this clinic, or athenahealth/eCW/other with a replacement), the prototype adapts to specifics rather than starting from zero. This also derisks the CGM eMDs dependency chain (clinic → account exec → connector purchase confirmation), which continues in parallel without blocking development.

---

## Backlog — Future Integration Targets

*Not needed for pilot. Ordered by technical integration readiness, not market share — re-rank as real research or a specific prospect clinic changes the picture.*

| Priority | EMR | Ambulatory Market Share | Integration Readiness | Notes |
|---|---|---|---|---|
| 1 | **athenahealth** | ~7% | Strong — FHIR R4 + proprietary REST API, Marketplace program, OAuth 2.0/SMART on FHIR, well documented | **Active prototyping target as of Jul 31, 2026** — see decision above. 5 Best in KLAS awards Feb 2026 incl. Overall Independent Physician Practice Suite Vendor. AI partnerships (Abridge, Microsoft Dragon Copilot) targeting the 1–50 provider segment — your market. Self-serve sandbox, no clinic relationship or BAA needed to start. |
| 2 | **eClinicalWorks** | ~12–13.9% (largest cloud ambulatory install base) | Strong — open API via "EHR Vault" + FHIR R4 | Strong primary care / FQHC fit. |
| 3 | **DrChrono** | Smaller, iPad-first niche | Strong — first cloud EHR to go FHIR-enabled, public Patient API | Common in small specialty/iPad-based practices. |
| 4 | **Tebra (formerly Kareo)** | Notable independent-practice presence | Weaker — limited data export tooling flagged in current reviews | Already a named Tier 2/3 competitor — integration vs. displacement is a live tension worth revisiting. |
| 5 | **Elation Health** | Not independently confirmed; strong reputation in target segment | **Unknown — not yet researched** | Best in KLAS, Small Practice Ambulatory EHR/PM (1–10 physicians), 2 years running (2025, 2026) — best clinical/market fit for independent primary care specifically, your actual target segment. Ranked last only because technical integration depth is unverified, not because the market case is weak. Anthropic's Claude is already embedded in Elation's platform (Clinical Insights feature) — worth noting for future vendor-trust conversations. **Needs a dedicated research pass before re-ranking.** |

### Lower-tier / not yet researched
CharmHealth, OptiMantra, Practice Fusion — repeatedly cited as low-cost/no-contract options solo practices land on. No confirmed developer-API research yet. Bucket for "research if a real prospect clinic runs one" rather than proactive build targets.

---

## Deferred, Not Backlog-Equivalent

| EMR | Status | Notes |
|---|---|---|
| **Epic** | Confirmed backlog — not required for pilot, but expected to be needed long-term given market presence, especially in larger/hospital-affiliated practices. | Requires two-layer approval: open.epic developer registration + separate governance sign-off from the hosting health system (App Orchard-style). Health system governance is typically the pacing bottleneck, not Epic corporate. Community Connect/hosted-instance question applies if a future target clinic isn't self-hosted. Research already done; stays valid whenever this becomes active. |

---

## Open Items

| # | Item | Status |
|---|---|---|
| 1 | CGM eMDs Developer Account registration | Blocked on clinic — need confirmation the FHIR API connector is purchased/active on their license, and an introduction to their CGM account executive |
| 2 | CGM eMDs $1,200/yr API fee — confirm this rides on clinic's existing CGM bill as part of pilot's third-party infrastructure cost coverage | Not yet raised with clinic |
| 3 | **athenahealth developer sandbox registration** | Not started — no external dependency, can begin immediately |
| 4 | eClinicalWorks — effectively needs a sponsoring clinic already on eCW to be the "straightforward" path; revisit if a prospect clinic surfaces on eCW | Deprioritized until a real eCW-running prospect exists |
| 3 | Elation Health developer/API program — dedicated research pass | Not started |
| 4 | Market-share figures for DrChrono, Tebra, CharmHealth, OptiMantra, Practice Fusion | Not yet researched — current entries are qualitative only |

---

*Session log: created Jul 31, 2026, alongside `track-b-vendor-baa-status.md`. Update priority order and Open Items as research lands — don't rewrite the whole doc each pass.*
