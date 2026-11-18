# HaloFlow synthetic app — run instructions

Everything in this app is **SIMULATED**: synthetic data, no real patients, no texts sent, no
EHR changed. Every page shows that banner and every JSON response carries `"simulated": true`.

## 1. What it is

- A local, synthetic-only build of the M05 reminder and follow-up flow (Option B, B1–B5):
  the staff item screen, a simulator page, packaged scenarios, and the real M05 schedulers.
- **Loopback only.** The app binds to a loopback address and refuses any other host, origin or
  cross-site request.
- **Not a demo.** Showing it to anyone outside the team stays under the demo hold.
- **No live SMS.** Reminders go to an in-memory FakeSender; the page's Outbox lists them and
  nothing is sent.
- **No real EHR.** The "EHR" is a simulated EHR held in the app's process memory.
- **No real patient data.** Patients, phones (fictional `+1 555 555-01NN` numbers), providers and
  departments are synthetic fixtures.

## 2. Setup

Run it only in an approved synthetic environment:
- Rachel's Mac, against a disposable PostgreSQL 17 cluster whose database name starts with
  `haloflow_test`; or
- the cloud container with its embedded PostgreSQL 17.

The installer reads three environment variables:

| Variable | Meaning |
|---|---|
| `HALOFLOW_SYNTHETIC_ADMIN_DSN` | admin connection to the disposable cluster (database name must start with `haloflow_test`) |
| `HALOFLOW_SYNTHETIC_STATE` | the state directory (created 0700; holds `state.json` and `state.lock`) |
| `HALOFLOW_SYNTHETIC_DISPOSABLE=1` | the environment's statement that the cluster is disposable |

## 3. Start

From `haloflow/`:

```
python -m haloflow.synthetic init --i-attest-disposable-cluster   # once per disposable cluster
python -m haloflow.synthetic status                               # read-only
python -m haloflow.synthetic serve                                # http://127.0.0.1:8765
```

Open `http://127.0.0.1:8765/synthetic/simulator` for the simulator page and
`http://127.0.0.1:8765/synthetic/ui/items` for the staff item screen.

## 4. Load hours and time zone

- Scenarios can be loaded only from 11:00 AM to 4:00 PM Eastern (`America/New_York`). At other
  times the page shows:

  > Scenarios can be loaded between 11:00 AM and 4:00 PM Eastern. Try again then.
- A loaded appointment starts two days later at 6:00 PM Eastern (S4: ten days later), so its
  reminder is due at once. Appointment times on the item screen are shown in Eastern time.
- The clinic's reminder send window ends at 7:00 PM Eastern. A Reminders tick after 7:00 PM
  Eastern does not send; the reminder is recorded as `outside_window`. Load and send on the same
  afternoon.

## 5. Walkthrough

The simulator page has five sections: **Scenarios**, **Run a tick now** (Due windows /
Reminders / Write-back), **Simulate patient reply**, **Simulated EHR**, and
**Outbox (nothing was sent)**. Each scenario can be loaded once; after that it shows "Loaded".

**S1 — confirm (Avery).**
1. Scenarios: **Load** S1.
2. Run a tick now: **Reminders**. The Outbox shows one reminder to S1's phone.
3. Simulate patient reply: S1's phone, text `1`.
4. Run a tick now: **Write-back**, twice. The Simulated EHR shows S1's appointment as
   `positive` (confirmed).

**S2 — reschedule, cancel, unclear, stop (four patients).**
1. Load S2, then a **Reminders** tick (four reminders in the Outbox).
2. Reply from each phone: `2` (reschedule), `3` (cancel), free text such as `maybe later`
   (unclear), and `STOP` (opt-out).
3. The staff item screen shows "Reschedule requested — call patient", "Cancel requested — staff
   to cancel in EHR" and "Reply not understood…" items, each with the patient's first name and
   appointment. STOP opts the number out and creates no item.

**S3 — EHR confirmation check.**
1. Load S3, a **Reminders** tick, then reply `1` from S3's phone.
2. Simulated EHR: set S3's appointment (its id and start are listed in the Simulated EHR
   table) to status `cancelled`, keeping the same start.
3. **Write-back** ticks. Write-back does not write the confirmation; it opens an "EHR
   confirmation needs checking — status changed" item.

**S4 — reply with no appointment linked.**
1. Load S4. Its appointment is ten days out, so a **Reminders** tick sends nothing for it.
2. Reply from S4's phone with any text. The item is "Reply not understood…" and shows
   "No appointment linked".

**No reply.** There is no on-demand "no reply" (owner decision OD-B5-1). A "No reply — call
patient" item appears only when a reminder has had no answer for 24 hours after it was sent, in
real time, and a **Due windows** tick runs after that.

## 6. Restart behaviour

- The database keeps every loaded scenario across a restart; loading it again shows "This
  scenario is already loaded. Reset to load it again."
- The simulated EHR, the Outbox and the EHR failure settings are process memory: a restart
  empties them. For S1 and S3 the page then shows "The simulated EHR for S1 was cleared by a
  restart. Reset to run scenarios again." (or S3).
- Reminders still send after a restart, because their baseline comes from the database.
  Confirmations then cannot be written back: after about 21 minutes of retries each becomes an
  "EHR confirmation needs checking — couldn't reach EHR" item. To run scenarios cleanly again,
  reset.
- A load is all-or-nothing, so the app never leaves a scenario half loaded. If only some of a
  scenario's rows are found (for example rows written outside the app), the page shows
  "Scenario data is incomplete. Reset to run scenarios again."

## 7. Reset

No reset command exists (by design, B2). To start again from nothing:
1. Stop the app (Ctrl-C on `serve`).
2. Drop the disposable database named in `HALOFLOW_SYNTHETIC_ADMIN_DSN` (it starts with
   `haloflow_test`), for example `dropdb -h <host> -p <port> -U <admin> <haloflow_test…>`.
3. Remove the state directory: `rm -rf "$HALOFLOW_SYNTHETIC_STATE"`.
4. Re-create the database, then run `init` again (section 3).

Only do this against the disposable cluster. Never point these variables at a shared or
production database.

## 8. What is simulated, and the run restrictions

- Simulated: patients, phones, appointments, providers and departments (fixtures); the SMS
  provider (FakeSender, Outbox only); the EHR (in-memory, with failure injection on the
  Simulated EHR form); patient replies (typed on the simulator page).
- Real: the M05 database functions, schedulers, write-back rules and the staff screen logic,
  running against the disposable database.
- Run restrictions: approved synthetic environments only (section 2); loopback only; no live
  SMS; no real EHR; no real patient data; not a demo. These holds are not lifted by this app.
