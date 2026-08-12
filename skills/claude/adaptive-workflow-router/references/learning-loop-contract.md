# Persistent learning-loop contract

Read this file before changing `scripts/learning_loop.py`, its policy, evidence schema, activation transaction, novelty archive, or scheduled-cycle behavior.

## Authority split

The learning ledger coordinates experiments; it is not a second model router.

- `adaptive-model-router/scripts/router_lab.py` exclusively owns model candidate creation, scored imports, qualification, promotion, monitoring, validation, and rollback. The learning ledger may mirror a model state only after reading the matching live router candidate authority.
- `adaptive-workflow-router/scripts/learning_loop.py` owns workflow-policy evidence aggregation and the exact-byte workflow activation journal.
- `workflow_plan.py` owns workflow composition and route requests.
- `workflow_dispatch.py` owns explicit `claude -p` flag assembly, observed execution identity, and the private append-only execution/quality receipt registry. Execution receipts bind completed dispatcher metadata, raw transcripts, input manifests, identity, usage, and duration. Quality receipts bind a retained grade to the exact evaluated execution receipts and either the bundled deterministic grader or a receipt-bound blind independent grader.
- Active routing never consumes a locally qualified model recommendation from the learning ledger.

## Bounded cycle

One scheduled run begins at most one candidate. A cycle is `model`, `workflow`, or `serendipity`; SERENDIPITY also declares whether its single safe mutation belongs to the model or workflow lane. Every cycle binds one normalized task-feature hash, one hypothesis, one causal variable, the canonical live catalog and active-policy paths plus file/semantic/model-scope hashes, exact arm identities, frozen counterpart policy, shared experiment manifest, grader and rubric, evaluation suite, headless runtime binary/label/evidence-source, dynamic run budget, evidence TTL, and candidate ID.

The learner derives the routable curated slug set from the canonical live global-router `catalog.json` and requires that set to equal the curated `adaptive-model-router/assets/claude-catalog.json` authority `router_lab.py` baselines from, that the live catalog was refreshed from those exact curated bytes, that every active-policy tier routes inside the set, and that the declared family and bound hashes match. Claude Code has no versioned model families, so there is no version window; a slug the curated catalog drops is a no-downgrade violation. A caller cannot choose the family or provide a substitute catalog. Model experiments vary only `model.route_assignment` and freeze the workflow policy. Workflow experiments vary one allowlisted workflow decision and freeze the complete route, profile, and router policy. Both arms stay in that derived slug set. T4 is exactly the `fable`/`max` route, read-only, and has no fallback.

## Evidence

Paired arms share one retained input manifest covering prompt, context, tools, permissions, explicit service tier, retry rule, grader, token cap, and wall-time cap. That shared manifest binds every arm-specific dispatcher manifest and a closed `planned_invocations_by_arm` map. Each planned invocation maps one-to-one to a run-budget call ID and freezes arm, role, phase, attempt kind, execution-manifest hash, expected inference count, token cap, and wall-time cap. An observation must contain every planned invocation exactly once; missing, extra, duplicated, misclassified, or over-cap work is inadmissible.

Each invocation names one execution receipt in the dispatcher's fixed private hash-chained registry; that receipt carries the retained runtime metadata and binds the raw `runs/<id>/claude-cli.NNNN.jsonl` stream by path and hash. The learner re-reads the chained receipt and the retained stream bytes and derives requested/observed identity, completion, usage, duration, event count, dispatch-packet and phase-contract hashes from them. Identity is enforced against the curated catalog `model_id` and against the literal `--model`/`--effort` flags in the retained command; a stream whose model usage names anything else is a substitution. There is no separate `execution-metadata.json` and no App Server turn record on this platform, and a receipt declaring anything other than `scored_evidence_status: BLOCKED_MODEL_ENFORCEMENT` is inadmissible. The quality artifact must likewise have a matching quality receipt bound to the exact execution-receipt IDs, grader identity, rubric, case, and arm. Deterministic quality is rerun through the bundled pinned grader; blind quality is bound to an independently routed grader receipt and cannot be self-graded. These receipts are trusted first-party harness provenance, not provider-signed proof. A generic spawn, caller-created file, caller assertion, unregistered grade, or model self-report is inadmissible.

Cross-cycle reuse requires the exact experiment-scope hash and exact current catalog, family, runner, route/profile, frozen policy, workflow, shared manifest, grader/rubric, feature, evaluation-suite, configuration, and budget bindings. Expiration occurs at the TTL boundary. Stale observations receive append-only status events and are never deleted.

Model observations additionally reference an exact eligible `router_lab.py` observation ID whose `execution_evidence_mode` is `external-verifier`; the declared `runtime-metadata` default is never scored model evidence, so model lanes stay `BLOCKED_MODEL_ENFORCEMENT` until `router_lab.py configure-attestation` pins a verifier. Ordinary workflow metadata may support workflow learning but remains ineligible for model promotion.

## Resource and quality gates

Every inference appears once with its planned call ID, role, phase, attempt kind, input/output tokens, context replay, retained metadata, transcript, and dispatcher receipt. Total tokens are the sum of measured input and output tokens; replay and reviewer/grader/tool-loop/retry/rework/escalation values are reported as non-additive slices of that total. End-to-end wall time is acceptance minus arm start, not the sum of concurrent calls. The run budget's worst-case aggregate includes every planned call plus explicit reviewer and grader allowances. Missing or unplanned work, a missing allowance, cap breach, missing usage, time, trusted quality, or comparable arm remains ineligible.

Safety, authority, correctness, objective gates, critical-failure absence, and quality noninferiority are hard constraints. Only then are token and time savings computed separately. Both must meet their configured Pareto thresholds; a tie, material regression in either dimension, excessive variance, or uncertainty keeps the incumbent. Holdouts participate only in qualification and never update action statistics.

## Persistence and states

The append-only ledger is self-hashed and hash-chained. Each append uses an atomic whole-ledger replacement with a checksummed recovery journal. Workflow activation snapshots exact incumbent bytes and uses a second journal whose recovery completes either the intended candidate bytes plus state event or the exact rollback bytes plus state event. Every public ledger read or mutation automatically recovers both journals before proceeding; `recover` remains an explicit diagnostic command, not a prerequisite for safe ordinary use. A tampered ledger, registry, journal, snapshot, manifest, live catalog, active policy, or candidate fails closed.

The state graph is:

```text
STAGED -> COLLECTING | BLOCKED_MODEL_ENFORCEMENT | STALE | SUPERSEDED
COLLECTING -> COLLECTING | BLOCKED_MODEL_ENFORCEMENT | QUALIFIED |
              REJECTED | STALE | SUPERSEDED
BLOCKED_MODEL_ENFORCEMENT -> COLLECTING | STALE | SUPERSEDED
QUALIFIED -> PROMOTED | COLLECTING | STALE | SUPERSEDED
PROMOTED -> VALIDATED | ROLLED_BACK
```

`REJECTED`, `VALIDATED`, `ROLLED_BACK`, `STALE`, and `SUPERSEDED` are terminal. Budget exhaustion is inconclusive and preserves the current state.

Prepromotion evidence is frozen once a cycle is `QUALIFIED`; later evidence cannot be appended behind that decision. Workflow promotion reruns evaluation against the current retained evidence and live authority and proceeds only when that fresh result is exactly `QUALIFY` for the same ordinary workflow candidate. A stale state label or earlier qualification event is insufficient promotion authority.

After promotion, canary evidence enters only through `record-canary` and only while the ordinary workflow candidate is `PROMOTED`. `monitor-workflow` accepts no caller verdict, window count, or Boolean payload. It derives complete control/challenger pairs, quality, identity, safety, authority, current bindings, and configured window counts from retained canary observations. An enforcement abort or qualified regression restores the exact candidate-bound snapshot automatically; incomplete evidence keeps `PROMOTED`, and only the configured number of complete non-regressing windows produces `VALIDATED`.

## SERENDIPITY

The learning policy reserves its configured fraction of scheduled capacity, capped at one half, for safe novelty search. Allocation is derived only from that policy share and cycle history; an optional fraction field is an equality assertion and is rejected if it differs from policy, so it confers no allocation authority. The allocation contract is bound to the current ledger head and cannot be reused. A SERENDIPITY hypothesis must explicitly say that immediate improvement is not predicted, mutate one safe allowlisted model or workflow variable, retain a behavioral descriptor, set a finite minimum-quality floor, and precommit unrelated replay task families.

The novelty archive is append-only and descriptor-indexed. It reports, without collapsing them, minimum-quality status, novelty distance, learning progress, and unrelated-family transfer value. Safe, authority-preserving, objective-passing quality or efficiency failures may be retained as informative failures with zero promotion credit. Unsafe, incorrect, unauthorized, wrong-family, or untrusted results remain in the experiment ledger but cannot enter the novelty archive. Novelty, surprise, or transfer never relaxes current-family, T4, identity, authority, safety, correctness, quality, sample, holdout, promotion, canary, or rollback gates.

Archived actions are periodically replayed at the configured interval only through an exact recorded held-out challenger observation for the same archived action and candidate semantics. The task family must differ from the source and belong to the archive's precommitted replay families. Replay never updates production action statistics or promotion state.

An ordinary later cycle may declare an ordered archive-ID lineage only at `begin`. Every archive must already exist; its event sequence, event hash, and action hash are frozen into the experiment scope. Retrospective stepping-stone credit requires that later cycle to reach `VALIDATED` after its begin event and a retained causal manifest binding the archive event, target begin event, validated state event, action, lineage, and target experiment scope. Credit is append-only diagnostic attribution and changes no promotion state.

## Sparse escalation and budgets

Run T0 checks first. T1/T2 finish routine work. T3 independently reviews only material ambiguity, conflict, objective failure, or elevated risk. T4 `fable`/`max` adjudicates terminal strategy, critical cross-system conflict, disputed promotion, and harness architecture in read-only mode. Review packets contain only recommendation, alternatives, objective results, disagreements, failure signals, evidence paths/hashes, and at most 12,000 bytes of bounded excerpts. One remediation pass is allowed; a second failure is surfaced.

Every invocation is preceded by a self-hashed run-budget manifest bound to the shared task manifest. It lists calls and arms, per-call requirement-derived caps, expected inference counts, context-growth allowance, reviewer/grader allowance, worst-case aggregate tokens, wall time, and rationale. There is no universal token ceiling. A fixed cap also requires retained measured-safe evidence.

## Commands

```bash
python3 scripts/learning_loop.py doctor
python3 scripts/learning_loop.py recover
python3 scripts/learning_loop.py status
python3 scripts/learning_loop.py capacity
python3 scripts/learning_loop.py begin --json CYCLE.json
python3 scripts/learning_loop.py record CYCLE_ID ARTIFACT_PATH ARTIFACT_SHA256
python3 scripts/learning_loop.py evaluate CYCLE_ID
python3 scripts/learning_loop.py advance CYCLE_ID
python3 scripts/learning_loop.py sync-model CYCLE_ID
python3 scripts/learning_loop.py promote-workflow CYCLE_ID
python3 scripts/learning_loop.py record-canary CYCLE_ID ARTIFACT_PATH ARTIFACT_SHA256
python3 scripts/learning_loop.py monitor-workflow CYCLE_ID
python3 scripts/learning_loop.py archive-serendipity CYCLE_ID
python3 scripts/learning_loop.py replay-archive ARCHIVE_ID --json RESULT.json
python3 scripts/learning_loop.py credit-stepping-stone ARCHIVE_ID VALIDATED_CYCLE CAUSAL_PATH CAUSAL_SHA256
python3 scripts/learning_loop.py sweep-stale --json CURRENT_BINDINGS.json
python3 scripts/learning_loop.py no-call
python3 scripts/learning_loop.py simulate
```

The simulation must prove canonical family binding, trusted receipt consumption, exact planned-call accounting, incomplete cross-cycle accumulation, quality-safe Pareto qualification above a non-universal aggregate budget, fresh exact workflow promotion, evidence-derived canary regression, and byte-identical rollback.
