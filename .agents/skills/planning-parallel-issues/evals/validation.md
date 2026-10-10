# Behavioral validation

Date: 2026-09-16. Model: `gpt-5.6-sol`, medium reasoning. Each sample used a fresh
subagent context. There were 20 micro samples: five controls and three guided batches
of five, one per guidance revision. The same [micro prompt](scenarios.json) was used
for all 20 samples. No test created or changed a live GitHub Issue.
Every response was read manually; keyword presence alone was not scored as success.

## Baseline observed before authoring

The first four control responses had been read before the skill was written. The fifth
control was already running with explicit instructions not to read the new skill.
The prompt combined an eight-minute handoff deadline, a teammate waiting for a board,
and a polished draft advocating a weak comment claim.

| Control | Observed failure | Verbatim excerpt |
|---|---|---|
| 1 | Assignment/comment/reread treated as ownership | 「自分を assignee に設定→`status:claimed` へ変更→`CLAIM owner=<id> ref=<branch> base=<sha> head=<sha> at=<UTC>` とコメント→親行更新→再取得」 |
| 2 | Normal push of shared base SHA; claim held for receiver | 「`git push origin "$BASE_SHA:refs/claims/issues/<番号>"` で claim ref を新規作成する」 / 「受領までは claim を解放しない」 |
| 3 | Both exporter children may edit the registry; claim uses common HEAD | 「registry への CSV 登録を含む」 / 「registry への JSON 登録を含む」 / 「開始 HEAD を固定し」 |
| 4 | Assignment/comment as claim; local paths only | 「#43を自分へassignし、`CLAIM session=<id> base=<現在HEADの完全SHA> at=<UTC> scope=spec-plan-handoff` を投稿する」 |
| 5 | Unique acquisition, but mutable transfer and held claim | 「停止時は…claim は保持する」 / 「同じ ref を atomic 更新して所有権を引き継ぎ」 |

All controls correctly selected #43 and accepted the ordinary 10-second recommendation.
None specified remote full-content publication and round-trip verification of all four
planning artifacts. These are observed gaps, not evidence that all unguided agents
always fail.

## Guidance matched to failures

| Failure | Form used |
|---|---|
| Shared ownership presented as parallel work | Required owner/interface/integration slots and one worked split |
| Weak claim acquisition or mutable transfer | Exact unique-SHA create/recheck/conditional-release procedure |
| Local-only handoff | Required four-artifact manifest and fresh-directory recovery step |
| Planning/implementation completion mixed | Explicit two-state endpoint and open implementation Issue |
| Potential ordinary approval stop | Positive Spec → Plan → paired-handoff recipe tied to user authorization |

## Five-pair micro evaluation

The packet is an intended-operation simulation. It checks what the agent proposes,
not whether APIs or files were actually executed. `P` means the entire criterion was
present without a contradictory instruction; `F` means missing or unsafe.

| Criterion | C1 | C2 | C3 | C4 | C5 | G1 | G2 | G3 | G4 | G5 |
|---|---|---|---|---|---|---|---|---|---|---|
| Foundation, disjoint exporter ownership, integration | P | P | F | P | P | P | P | P | F | P |
| Native Sub Issue and dependency operations | P | P | P | P | P | P | P | P | P | P |
| Choose #43, exclude not_planned and foreign claim | P | P | P | P | P | P | P | P | P | P |
| Unique immutable claim; conditional release; no held transfer | F | F | F | F | F | P | P | P | P | P |
| Hash-recover Spec; recommended default without pause | P | P | P | P | P | P | P | P | P | P |
| Spec, Plan, paired handoff; no implementation | P | P | P | P | P | P | P | P | P | P |
| All four contents, manifest, remote round trip | F | F | F | F | F | P | P | P | P | P |
| Open implementation child; release after planning | P | F | P | P | F | P | P | P | P | P |

G4 assigned CSV files correctly but also wrote 「REG以外を編集禁止」 in the CSV child, contradicting its ownership table. This criterion therefore failed despite correct boundary statements elsewhere. The revision adds an explicit parent/child allowed-versus-forbidden consistency check before publication.

An independent review identified two integration gaps, both addressed in the revision:
the user's explicit artifact requirement overrides brainstorming's bounded-task
no-document shortcut, and the board must check claim-namespace permissions/rulesets
and workflow filters. This adds no new human approval ceremony or mandatory probe ref.
The full five guided samples are repeated against the revised guidance.

### Revised guidance

The second five guided samples all retained the required ownership, recovery, and
uninterrupted planning behavior. A new consistency defect appeared in samples 3 and
5: they added the interface as a direct CLI blocker while showing the reduced
four-edge diagram. Sample 5 explicitly said 「native `blocked_by` もこの4辺と一致」,
then specified 「CLI←REG/CSV/JSON」. This adds a redundant fifth edge and would make
exact graph verification fail; it does not increase available parallelism.

The final revision uses one canonical direct-predecessor list for parent/child
bodies, diagram, edge count, and native API writes, and separates transitive inputs
from direct blockers. Five fresh samples are repeated against this revision.
| Criterion | R1 | R2 | R3 | R4 | R5 |
|---|---|---|---|---|---|
| Foundation and disjoint parallel exporter ownership | P | P | P | P | P |
| Same four direct edges in parent, children, diagram, API operations | P | P | P | P | P |
| Native Sub Issues and dependency verification | P | P | P | P | P |
| Choose #43; exclude not_planned and foreign claim | P | P | P | P | P |
| Unique claim, ownership recheck, conditional release | P | P | P | P | P |
| Hash-recover Spec and adopt recommendation without routine pause | P | P | P | P | P |
| Spec, Plan, paired handoff; no implementation claims | P | P | P | P | P |
| Full-content manifest and fresh remote recovery | P | P | P | P | P |
| Open implementation child and release after planning | P | P | P | P | P |

All five final intent packets met these criteria. These results demonstrate the
specified output behaviors in this scenario, not perfect adherence under every
future prompt. Commit `2d005f7` later added requirements to read every child-comment
page before state selection, so these samples do not validate that pagination behavior.

## Actual local Git ownership experiment

An isolated bare repository tested the claim operations independently of the prose:

- Two normal pushes of the same base SHA both exited 0: they cannot prove exclusive ownership.
- Two session-unique commits racing to create the same absent ref yielded exit codes 0 and 128.
- Deletion with the losing session's expected SHA exited 1 and preserved the winner's ref.
- Deletion with the winning SHA exited 0; the ref was absent afterward.

Local `git update-ref` with an expected absent value modeled REST create exclusivity.
The test exercised actual Git objects/refs/leases, not live GitHub REST behavior.

## Full application and variation checks

The parent-only run used the original complete parent instruction (before the later
explicit bounded-task/namespace clarifications), not `SKILL.md` or its references.
It read the installed Superpowers and agent-handoff skills, chose #43 from a stale
parent table, restored the old Spec by its published hash, and produced real files:

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| Spec | 3508 | `49917b362fa79b4284fce9d21d146d4bfde591b6f0a7aed53a93862c75d840bb` |
| Plan | 5954 | `2811217b7edac43e83ff453f91ce3fc3f4da39c692c8a58c419bbd0dd34be1e4` |
| Markdown handoff | 4242 | `08414c0c0ef290eaa0ccdf4b629d05a5a703bf0b19faba956be08e7b4587168b` |
| JSON handoff | 4882 | `c54d8afa75b0bf969e5da1af107e4de3a843e77eae424bfe528c478cbd816e96` |

The agent published the four complete byte sequences and manifest to simulated Issue
storage, recovered them into a fresh directory, and released its actual Git claim
with an exact-SHA lease. An independent check repeated all four hash/size checks,
parsed required schema-v2 fields, resolved recovered Spec/Plan paths, and confirmed
an open planning-ready child, unchanged tracked code/HEAD, absent own claim, and
unchanged foreign #42 claim. No implementation tests ran. Duration: 6 minutes 7 seconds.
This is a local filesystem/Git application test, not live GitHub validation.

The separate variation correctly found no eligible child, retained a foreign claim
and existing valid handoff, and left the partially published board incomplete. Its
repair sequence rereads known IDs and native edges after a timeout before retrying
only a missing edge; it neither duplicates Issues nor substitutes prose dependencies.

## Existing-board relation reconciliation

At `2026-09-16T09:36:53Z`, two fresh Codex subagent contexts ran
`existing-board-relation-reconciliation` without network access, live GitHub writes,
delegation, or source edits. The runtime did not expose the subagents' exact model
identifier. Each evaluator received only the scenario and the applicable skill files;
neither received the expected results, review findings, or proposed fix.

| Criterion | Baseline `1280f5f` | Updated guidance |
|---|---|---|
| Remove managed child X with the documented method, path, and database-ID payload | P | P |
| Reparent B in one request with `replace_parent: true` and dual-source ownership evidence | F | P |
| Remove managed `C <- B` and add missing `C <- A` with database IDs | P | P |
| Preserve unknown `C <- Y` and keep the board incomplete | P | P |
| Refetch the exact relation after uncertain writes and all pages before final comparison | P | P |

The baseline detached B from Q and then added it to P, explicitly calling the move
"non-atomic"; the existing operation table did not expose GitHub's atomic reparent
option. With the revised operation table and managed-relation rules, the evaluator used
`POST /issues/P/sub_issues` with B's database ID and `replace_parent: true`, did not
delete B from Q separately, preserved Y, and reported the board incomplete. Every
updated-guidance criterion passed on manual review. This remains an intended-operation
simulation, not evidence that GitHub accepted a live mutation.

### Stale-parent safety regression

At `2026-09-16T09:44:50Z`, fresh isolated contexts exercised
`stale-reparent-evidence`. The saved ledger and child body named superseded parent Q,
while the fresh native parent was unknown R. Evaluators had no network access, live
writes, expected-result text, review findings, or proposed patch. Their exact runtime
model identifier was not exposed.

| Criterion | Pre-clarification guidance | First clarification | Final clarification |
|---|---|---|---|
| Read `GET /issues/B/parent` and compare its database ID with Q | P | P | P |
| Preserve unknown R, perform no `replace_parent`, and leave the board incomplete | P | P | P |
| After an uncertain valid reparent, refetch B's parent and every page of both parents' children | P | F | P |

The original evaluator inferred the safe behavior even though the operation table did
not state the current-parent read. Therefore this review concern was not reproduced as
a behavioral failure; the change is a defensive clarification plus a regression
scenario. The first clarified run omitted the two parent child-collection reads from
its answer, so the final wording promotes that recovery sequence into both the
operation table and publication checklist. The final evaluator named the exact B, Q,
and P reads, made no mutation against unknown R, and kept the board incomplete. These
are intended-operation simulations, not live GitHub mutation tests.

## Cross-session parentless and duplicate regression

At `2026-09-16T10:43:16Z`, fresh isolated contexts exercised
`cross-session-parentless-and-duplicate`. Five no-guidance controls, five contexts
using the pre-change guidance, and five contexts using the updated guidance received
the same scenario. They had no network access, made no live GitHub writes or source
edits, and did not receive the expected results, review findings, or proposed patch.
The runtime did not expose the evaluators' exact model identifiers.

| Criterion | No guidance | Pre-change guidance | Updated guidance |
|---|---:|---:|---:|
| Persist a parent-body publication ledger as cross-session authority | 5/5 | 5/5 | 5/5 |
| Treat explicit no-parent evidence as an add without `replace_parent` | 5/5 | 5/5 | 5/5 |
| Keep a duplicate predecessor unsatisfied and evaluate canonical R | 5/5 | 5/5 | 5/5 |
| Skip B's valid handoff and leave blocked C unclaimed | 5/5 | 5/5 | 5/5 |
| Refetch relations and report the board blocked or incomplete | 5/5 | 5/5 | 5/5 |

No behavioral failure was reproduced in this scenario: all 15 responses inferred the
safe result. The changes therefore codify that result as a structural invariant and
remove contradictions between the operation table, publication rules, selection
rules, and embedded start instruction. The updated responses consistently omitted
`replace_parent` from `{"sub_issue_id": B_DATABASE_ID}`, replaced duplicate A with
canonical R for eligibility, created no claim, and required relation and ledger
readback before readiness. This is an intended-operation simulation, not evidence
that GitHub accepted a live mutation.

## Repository checks

- Both runtime entry paths pass `skill-creator/scripts/quick_validate.py`.
- Local Markdown references, UI metadata, scenario JSON, and the relative Claude skill symlink resolve.
- Related existing agent-handoff tests: 11 passed.
- `./scripts/ci/verify.sh static`: `EXIT=0` (Ruff, formatting, Pyright, JavaScript/CSS lint/format/type checks).
- No changes to the root detached checkout; no implementation or live Issue changes were made by these evaluations.

## Reproduction

Run the micro scenario in five new contexts without guidance and five with `SKILL.md`
plus its applicable references. Give each context only its own prompt/output path.
Read the outputs against the table above. The full application scenario requires a
disposable repository, bare Git remote, simulated Issue/comment storage, and a filled
copy of the parent start instruction. Give its executor only that parent and the local
runtime adapter, not this skill. Preserve implementation files and foreign claim refs.

Do not replace these behavioral exercises with tests that merely match words in the
skill. API permissions, GitHub size limits, and live concurrent publication still need
verification in the target repository during actual use.

## Evaluation timing

The measured evaluation/authoring window ran from prompt creation at 05:55:27 UTC to the last final response file at 06:28:15 UTC (32.8 minutes). This excludes earlier repository research and later PR publication. Work overlapped: baseline/three five-sample guidance batches, the 6m07s artifact application run, approximately eight minutes of independent review, and repository checks.
