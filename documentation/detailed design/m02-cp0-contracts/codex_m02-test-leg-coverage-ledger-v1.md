# M02 CP0 — test-leg coverage ledger v1

2026-09-09 · ChatGPT/Codex lead · Codex. Review draft; no tests implemented, executed or frozen. Read with test-leg entrypoints/cases v2 and execution-route reconciliation v1. Earlier drafts remain reference history; their obsolete decision-status statements are not current gates.

## NFR traceability

| Requirement | Named specification | Evidence required / current gap |
|---|---|---|
| NFR-001 durability | TC01/07/08/09/19 | success only after commit; definite rollback distinct from unknown commit; signal failure does not reverse commit |
| NFR-002 availability | TC01/07 plus NF02 | provider unavailable/DB unavailable/timeout fixtures; fail closed before required intent; approved service objective still needs owner/workflow target |
| NFR-003 concurrency | TC10/11/15/22 | concurrent duplicate, correction and callback; failover/reconstruction; no lost facts or deadlock regression; unit mocks cannot prove database locks |
| NFR-004 performance | NF04 | measured latency/query cost over operation AND subject history, warm/cold, tenant load and link fanout; n=10/100/1000/10000 are selected stress/planning inputs, not empirical population; targets and representative workload remain open |
| NFR-005 compatibility | TC18 plus NF05 | old/new producer-consumer-engine matrix, retained historical contracts, missing/unsupported snapshot refusal; frozen OI-007 versioning rules |
| NFR-006 observability | TC19 plus NF06 | structural counters for every enumerated success/failure/duplicate/level/status/stale/indeterminate/lag/conflict/correction/unknown/tenant-pressure metric; no synthetic prohibited fields |
| NFR-007 alerting | NF07 | independent sustained failure, incompatible projection, stale/indeterminate backlog, terminal conflict, unknown provider mapping, correction abuse, foreign reference signals reach correct owner; threshold policies remain explicit dependencies |
| NFR-008 replay | TC14 plus NF08 | bounded full-range repeat and interruption restart; sequence allocation/commit order permutation and gaps; no incremental watermark, provider calls or duplicate downstream work; OI-009 review thresholds not invented |
| NFR-009 fairness | NF09 | heavy tenant plus control tenant under timeline/replay/dispatch pressure; bounded pages/sweeps/backpressure and per-tenant observations; objective values need production-like evidence |
| NFR-010 integrity | NF10 | authorized privileged mutation simulation detected against retained evidence; missing-record, changed-record and unexplained-sequence fixtures; rollback-created gap alone produces no tampering claim; separately authorized audit identities |

NF02/04/05/06/07/08/09/10 are draft case-family IDs. None is a passing test. NF04/09 cannot close on row-visit arithmetic alone; NF02/07 cannot close with invented objectives. Distinguish specification completeness from later measured release evidence.

## Earlier installation/security test families retained

Part3 test-contract-v1 B01–B09 (definition split/parser/validated-byte identity/legacy t001), P01–P10 (effective grants, production units, sequence ownership, atomic install and retry), G01–G04 plus part1 C01–C07 (gateway metadata, path, locks and context) remain necessary. Apply approved installation-feasibility-v2, registry-lock-v2, key-scope-v3 and layered-context-v4 refinements rather than obsolete provisional B1/rotation/qualification statements.

Key tests must assert exact MF201/202/203 classification, one/eight valid versions, nine invalid, active-first descending one-based arrays, retained-key failure, nested key-scope call, accepted updated_at mutation residual, registry UPDATE rejection and no gateway mutation statements. No live heartbeat or installed rotation expected in v1. CI assertion/lint/type failures remain three independently evidenced gates.

RC01 uses layered M01 authorization, not forged-setting authentication. RC02/03 use approved lock contracts. RC04 maps to TC11; RC05/17 to key cases; RC06 is superseded by TC07–09/25 under A; RC07 maps TC10; RC08 replacement identity chain remains dependent on exact reviewed acceptance semantics; RC09 maps TC15; RC10 maps TC04; RC11/18 map TC12 plus read-view-v2; RC12–16 retain installation/parser/absent-rotation/CI obligations with current approved refinements.

## Closure state

Closed as source inventory: ten baseline public methods, three proposed workers; frozen observation/correction scope; 13 frozen event families mapped through 21 service branches; FR-001–035 have draft mapped case families. Not closed as implementation inventory: additional proposed completion/evidence/audit/null-event routes and exact registered capability matrix require review. No existing production M02 catalogue exists to certify.

Before the single alignment: resolve route-review findings; settle exact handoff/payload oracles and internal evidence envelope; consolidate superseded prose into one clean test contract; bind all physical signatures/grants/columns to those tests; assemble requirements/design/test-case versions with a final source and approval index. Outstanding objective-setting and later execution evidence must be labelled as such, never as fulfilled by CP0 prose. Source issuance proceeds only under Rachel's separate authorization and QA. No code, database, source or Git changes made.
