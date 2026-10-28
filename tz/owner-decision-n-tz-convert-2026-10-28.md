# Owner decisions — EHR department timezone (2026-10-28)

Decided by Rachel at about 15:25 ET, through the clickable options in the Claude session.

- **OD-TZ1 — Convert and send.** For real clinics, if the EHR says a department is in a different timezone from the clinic's HaloFlow setting (as with the sandbox: Denver vs New York), HaloFlow trusts the EHR's timezone. It sends that department's reminders with times in the department's local time.
  - Claude's recommendation was "Hold and alert staff". Rachel chose "Convert and send".
- **OD-TZ2 — Keep text, no label (recommended option).** The approved Q-B4 patient text stays exactly as it is, with no timezone label. The time shown is the department's local time.

Context:
- PR #46, merged 19:24:14Z (main 36c8c72), fails closed on a mismatch. OD-TZ1 reverses that.
- Implementation follows the N-TZ Step 3 package (claude_n-tz-step3-package-2026-10-28-v1.md), after Codex alignment and tests first.
- Sends stay simulated (OD-A3).
