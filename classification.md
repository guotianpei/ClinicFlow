# S-J PR #86 — final CI 42928334005 classification (Claude, 2026-11-11)

Classified from the raw job log fetched with authentication at ~7:50 AM ET (12:50Z UTC).
`job.log` sha256 973d078025bc01be76fc225fb2463c937e939c1059d7b8c2036ee419c60b0334 (22,290,780 bytes).

| Item | Result |
|---|---|
| Run / job | 42928334005 / 130220422461 `verify-m01`, event pull_request, head 6cb0915 |
| Window (UTC) | 2026-11-11T00:03:46Z – 04:07:08Z |
| Checkout | log L233 `HEAD is now at 37e76b3 Merge 6cb0915… into e7789af…` |
| Tested tree | 37e76b3^{tree} = **8e2af58** (fetched and checked) = merge-tree(41794a8, 6cb0915) |
| Steps | **54/54 success** (steps.txt), 0 failed, 0 skipped |
| Main suite | **5682 passed** in 800.81 s (L7300), 0 failed |
| Trace | **14 passed** (L7349) |
| Gate reachability selftest | **PASS** (L7438), working tree clean before and after |
| Mutation | **316/316 killed**, every `restored=True`, 0 survived, 18 harnesses (table below) |
| Mutation controls | 60 control notices, 5495 control cases, **0 problems** |
| `sj` harness | **12/12** (8 + 1 + 3), controls 82/25/25 cases, 0 problems; J-M7 notice L8744 |
| Errors / warnings | 0 `##[error]`; 1 warning: Node.js 20 actions deprecation (runner notice, not a test result) |
| Non-zero exit codes | none |

Per-harness kills: m02_observation 14, m05_create 4, m05_resolve 9, b5_startup 12, l1w_w1 28,
l1w_w2 35, l1w_w3a 12, l1w_w3b 21, l1w_w4a 19, l1w_w4b 20, sa_intake 17, sb_due 11,
sc_activation 13, sh_h1 12, sh_h2 16, sh_h3 22, si 39, sj 12 = 316.

Consistency with the S-I final (42869646639): mutation 304 → 316 (+12 = sj); control notices
57 → 60. Local S-J gate figures (5682 tests, sj 12/12) are reproduced by CI.

Main CI 42928732669 on 41794a8: completed/success (not classified step by step here).

Current main is 3f58d59. merge-tree(3f58d59, 6cb0915) = ea1bd4c, which differs from 8e2af58 only in
`documentation/HaloFlow_Module_Delivery_Tracker.md` (PR #87, +17/−1). No code difference.

Claim limits: synthetic only; S-J is not composed into production startup; no holds are lifted. This is
Claude's classification for Codex's independent acceptance review, not an acceptance.
