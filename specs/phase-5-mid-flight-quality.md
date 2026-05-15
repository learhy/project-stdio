# Phase 5: Mid-Flight Worker Quality Management

## Scope and motivation

Phase 4 extended worker execution to remote hosts, Kubernetes clusters, and Docker containers. But the orchestrator-worker relationship remained fire-and-forget: spawn, receive heartbeats, receive final_report, done. There is no way for workers to ask questions mid-execution, and no way for the orchestrator to inspect work quality before the final report arrives.

Phase 5 changes this to an active supervisory relationship. Workers can ask questions rather than silently guessing. The orchestrator runs periodic review passes over in-progress work, inspecting progress, checking calibration divergence, and intervening when a worker appears to be going off-track. Interventions that require human judgment are surfaced to PMs via GitHub, MCP, and CLI.

**What does not change in Phase 5:**

- The state machine, DAG executor, capability manifest schema, approval matrix, artifact protocol. All unchanged.
- The WorkerRunner interface. Runners are not modified; this is a protocol-level change between the orchestrator and already-running workers.
- Bundle submission and approval flows.

**What changes in Phase 5:**

- New worker-initiated RPC methods: `worker.ask_question`, `worker.report_checkpoint`, `worker.describe_progress`, `worker.show_artifact`
- Extended `worker.inject_context` with acknowledgement channel and typed responses
- Question routing engine: LLM-first, escalate to PM if low confidence or rate-limited
- ReviewScheduler background task triggering periodic and anomaly-based reviews
- Structured LLM review calls producing verdicts with five action types
- Intervention tracking across question answers, redirects, and escalations
- GitHub comment formatting for all report types (questions, escalations, checkpoints, final reports)
- Calibration integration tracking intervention accuracy and PM response times
- PM feedback loop on review quality

---

## Bundle 5.1: Bidirectional worker introspection protocol

### Background

Workers currently have no way to ask questions during execution. If a worker encounters ambiguity — an unclear requirement, a missing dependency, a design decision it doesn't have context for — it must either guess (risking wrong output) or fail. Similarly, the orchestrator has no structured way to query a worker's current state beyond heartbeats.

This bundle adds a set of worker-initiated RPC methods for asking questions and reporting progress, and extends `inject_context` to support typed responses with acknowledgement tracking.

### Deliverables

**`worker.ask_question` RPC method.** Worker submits a question to the orchestrator:

- `question_id`: ULID for idempotency
- `question`: the question text
- `context`: relevant background from the worker's current state
- `blocking`: if true, worker pauses current task subtree until answer arrives
- `urgency`: low, medium, or high

Response is an acknowledgement only (`{"status": "received", "question_id": "..."}`). The answer arrives later via `inject_context`.

**`worker.report_checkpoint` RPC method.** Worker signals a major phase transition:

- `checkpoint_id`: ULID
- `phase_completed`: description of what just finished
- `phase_starting`: description of what comes next
- `summary`: 2-3 sentence human-readable summary of work done so far
- `concerns`: list of strings — anything the worker is uncertain about
- `estimated_remaining`: loc, seconds, tokens estimates for remaining work

**`worker.describe_progress` RPC method.** Queryable by orchestrator, not worker-initiated. Returns structured current state snapshot:

- `current_activity`: string
- `completed_steps`: list of strings
- `planned_steps`: list of strings
- `blockers`: list of strings (empty if none)
- `confidence`: high, medium, or low
- `recent_tool_calls`: last 5 tool calls with tool name and summary

**`worker.show_artifact` RPC method.** Queryable by orchestrator. Returns current contents of a file within the worktree:

- Request: `{"path": "relative path within worktree"}`
- Response: `{"content": "...", "size_bytes": int, "last_modified": "ISO timestamp"}`

**Extended `worker.inject_context`.** The existing method gains:

- `injection_id`: ULID for idempotency and ack tracking
- `type`: answer, redirect, feedback, or question_response
- `question_id`: if type is question_response, the question this answers
- Worker acknowledges with `{"injection_id": "...", "acknowledged": true, "worker_response": "..."}`
- If no acknowledgement within 30 seconds, orchestrator logs a warning and continues
- Acknowledgement is recorded in the audit log

**Question routing on the orchestrator side.** When a question arrives:

1. Attempt orchestrator LLM answer first (Bundle 5.2 provides LLM details). The LLM receives the original task spec, bundle proposal_json, the worker's question and context.
2. If LLM confidence is high: answer the worker via inject_context immediately. Log to audit trail. Do NOT post to GitHub.
3. If LLM confidence is low OR question requires human policy judgment: escalate to PM. Post to GitHub as a comment on the bundle issue. Pause the worker if blocking=true.
4. Rate limit: if a worker asks more than 10 questions in a single execution, treat subsequent questions as escalations regardless of LLM confidence. The worker is confused, not just missing context.

**Question rate limiting.** `questions_asked` counter on the workers table. Increment on every `worker.ask_question`. When counter exceeds `ops.max_worker_questions_before_escalation` (default 10), all further questions bypass LLM routing and go directly to PM escalation. Audit log entry noting rate limit was hit.

### Schema (v12)

- `worker_questions` table: question_id, worker_id, bundle_id, question, context, blocking, urgency, status (pending/answered/escalated), answer, asked_at, answered_at
- `worker_checkpoints` table: checkpoint_id, worker_id, bundle_id, phase_completed, phase_starting, summary, concerns_json, estimated_remaining_json, created_at
- `workers` table: add questions_asked INT NOT NULL DEFAULT 0

### Acceptance criteria

1. Worker calls `worker.ask_question`, question is stored, routing decision made, answer delivered via inject_context
2. Worker calls `worker.report_checkpoint`, checkpoint stored, GitHub comment posted if major transition
3. Orchestrator calls `worker.describe_progress`, worker returns structured snapshot
4. Orchestrator calls `worker.show_artifact`, file contents returned
5. `inject_context` with injection_id, worker acknowledges, audit log entry created
6. No acknowledgement within 30s, warning logged, orchestrator continues
7. 11th question from a worker bypasses LLM routing, escalates directly to PM

---

## Bundle 5.2: Review trigger and LLM evaluation

### Background

With the introspection protocol in place (Bundle 5.1), the orchestrator can now periodically inspect worker state. This bundle adds a background `ReviewScheduler` that evaluates when to trigger reviews and calls a structured LLM evaluation to determine whether intervention is needed.

### Review triggers

The `ReviewScheduler` runs alongside the existing `Scheduler`. For each IN_PROGRESS bundle, it evaluates:

1. **Time trigger**: every N minutes for any running worker (default 10, configurable via `ops.review_interval_minutes`). Skip if worker has been running less than 5 minutes — too early to have meaningful output.
2. **Anomaly trigger**: when calibration divergence exceeds thresholds:
   - Token usage > 1.5x estimate: trigger immediately
   - Wall time > 1.5x estimate: trigger immediately
   - Worker has not made a checkpoint in 15 minutes on a task estimated >10 minutes: trigger
3. **PM-initiated**: `studio request_review` RPC triggers an immediate check-in on the specified worker. CLI: `studio review-worker <worker-id>`.
4. **Post-checkpoint**: always trigger a lightweight review after each `worker.report_checkpoint` to decide if the next phase looks on-track.

All triggers are recorded in the audit log with the trigger reason.

### Review LLM call

When a review triggers, the orchestrator collects:

- Original task spec and acceptance criteria
- Bundle proposal_json (complexity/risk scores, implementation plan)
- `worker.describe_progress()` output
- Last 3 worker checkpoints (if any)
- Current calibration trajectory (tokens used vs estimated, time elapsed vs estimated)
- Any prior interventions and their outcomes this execution

The orchestrator calls the Ollama Cloud LLM with a structured review prompt. The LLM returns:

```json
{
  "verdict": "on_track | request_clarification | request_artifact | request_redirect | escalate_to_human",
  "confidence": "high | medium | low",
  "rationale": "string — 2-3 sentences explaining the verdict",
  "action": {
    "type": "inject_context | ask_artifact | none | escalate",
    "content": "string — if inject_context: the message to send to the worker",
    "artifact_path": "string — if ask_artifact: path to request",
    "escalation_reason": "string — if escalate: why human judgment is needed"
  }
}
```

**Verdict actions:**

- `on_track`: do nothing. Log the review and result to audit trail.
- `request_clarification` or `request_redirect`: call `worker.inject_context` with the action content. Log the intervention.
- `request_artifact`: call `worker.show_artifact` with the path, then run a second LLM call with the artifact content appended to context. The second call produces a verdict on the artifact specifically.
- `escalate_to_human`: pause the worker, post to GitHub (see Bundle 5.3), surface via MCP.

### ReviewScheduler

New class in `studio/orchestrator/review.py`. Runs as a background asyncio task. Checks all IN_PROGRESS workers every 60 seconds against the trigger conditions. Fires review calls when conditions are met. Does not block the main event loop — review LLM calls are run in `asyncio.to_thread`.

### Settings

```json
"review": {
  "enabled": true,
  "interval_minutes": 10,
  "token_divergence_threshold": 1.5,
  "time_divergence_threshold": 1.5,
  "checkpoint_silence_minutes": 15,
  "model": null
}
```

`model: null` means use the same model as the bundler. Set to a specific model string to use a different model for review calls.

### Acceptance criteria

1. Worker running 11 minutes: time trigger fires review
2. Token usage 1.6x estimate: anomaly trigger fires
3. No checkpoint in 16 minutes on long task: silence trigger fires
4. LLM returns on_track: no action, audit log entry
5. LLM returns request_redirect: inject_context called with redirect content
6. LLM returns escalate_to_human: worker paused, GitHub comment posted
7. LLM returns request_artifact: artifact fetched, second LLM call made
8. Worker running 4 minutes: time trigger skipped (too early)

---

## Bundle 5.3: Intervention actions and GitHub surfacing

### Background

Bundles 5.1 and 5.2 produce questions and review verdicts that may require PM intervention. This bundle builds the full intervention pipeline: recording interventions, pausing/resuming workers, surfacing escalations to PMs across all three approval surfaces (GitHub, MCP, CLI), and formatting all report types for GitHub comment posting.

### Intervention types

Extend `inject_context` type field:

- `answer`: response to worker.ask_question
- `redirect`: orchestrator telling worker to change approach
- `feedback`: general feedback mid-execution
- `question_response`: PM-provided answer to an escalated question

Each intervention is recorded in a `worker_interventions` table: intervention_id, worker_id, bundle_id, type, content, triggered_by (review_scheduler, pm_request, question_answer), trigger_reason, worker_acknowledged, created_at.

### PM escalation flow

When the orchestrator escalates to PM (from question routing or review LLM verdict):

1. Pause the worker
2. Post GitHub comment to the bundle's issue (formatted per below)
3. Record escalation in `worker_interventions` with status=pending
4. Surface via MCP: `worker_escalations` resource lists all pending escalations

PM responds via any surface:
- **GitHub slash command**: `/answer:<question_id> <response text>` or `/resume:<worker_id> [context]`
- **MCP tool**: `answer_worker_question(question_id, answer)` or `resume_worker(worker_id, context)`
- **CLI**: `studio answer-question <question-id> "<answer>"` or `studio resume-worker <worker-id> --context "<context>"`

When PM response arrives:
1. Call `worker.inject_context` with the answer and type=question_response
2. Resume the worker (if paused)
3. Update intervention status to answered
4. Post GitHub confirmation comment
5. Record in calibration data: escalation required, PM response time

### GitHub comment formats

**Worker question escalated to PM:**

```markdown
## 🤔 Worker needs guidance — `<worker-id>` on `<node-id>`

**Question** (asked <N> minutes ago):
<question text>

**Context the worker provided:**
<context text>

**Urgency:** <low|medium|high> | **Blocking:** <yes|no>

---
Reply with `/answer:<question-id> your response here` or use Claude Desktop.
```

**Orchestrator review escalation:**

```markdown
## ⚠️ Review escalation — `<worker-id>` on `<node-id>`

The orchestrator's review pass flagged a concern and needs human judgment.

**Concern:**
<rationale from LLM>

**Worker's current activity:**
<describe_progress.current_activity>

**Options:**
- Let it continue: `/resume:<worker-id>`
- Redirect: `/resume:<worker-id> <new direction>`
- Kill and restart: `studio kill <bundle-id>`
```

**Checkpoint report** (posted every 3rd checkpoint or when concerns list is non-empty):

```markdown
## ✅ Worker checkpoint — `<worker-id>`

**Completed:** <phase_completed>
**Starting:** <phase_starting>

<summary>

<if concerns non-empty>
⚠️ **Worker flagged concerns:** <concerns joined with "; ">
```

**Final report** (always posted on bundle completion):

```markdown
## 📋 Bundle complete — `<bundle-id>`

**Result:** ✅ Passed / ❌ Failed
**PR:** <pr link if exists>

### What was done
<worker final_report.summary>

### Acceptance criteria
<for each criterion: ✅ or ❌ criterion text>

### Calibration
| Axis | Estimated | Actual | Divergence |
|------|-----------|--------|------------|
| LOC  | <est>     | <act>  | <pct>%     |
| Time | <est>     | <act>  | <pct>%     |
| Tokens | <est>  | <act>  | <pct>%     |

<if any divergence > 50%>
⚠️ Significant divergence — post-mortem will auto-fire.

[Full audit trail](<mcp link or studio show command>)
```

### New CLI commands

- `studio answer-question <question-id> "<answer>"` — answer a pending worker question
- `studio resume-worker <worker-id> [--context "<context>"]` — resume a paused worker
- `studio pending-escalations` — list all pending PM escalations across all bundles
- `studio review-worker <worker-id>` — trigger an immediate review check-in

### Schema (v13)

- `worker_interventions` table: intervention_id, worker_id, bundle_id, type, content, triggered_by, trigger_reason, worker_acknowledged, created_at
- `worker_questions` table: add escalated_at, answered_at, answered_by (llm or pm), answer columns

### Acceptance criteria

1. Question escalated to PM: worker paused, GitHub comment posted with correct format, MCP resource lists escalation
2. PM answers via `/answer:<qid> <text>`: inject_context called, worker resumed, confirmation posted
3. PM answers via CLI `studio answer-question`: same flow as GitHub
4. PM resumes via `/resume:<wid> <context>`: inject_context with redirect, worker resumed
5. Checkpoint posted to GitHub every 3rd checkpoint or when concerns non-empty
6. Final report posted to GitHub on bundle completion with calibration table
7. All slash command formats parse correctly
8. `studio pending-escalations` lists all open escalations

---

## Bundle 5.4: Calibration integration and report surfacing

### Background

With the full intervention pipeline in place, this bundle closes the loop by integrating review and intervention data into the calibration system and ensuring all report types reach their correct surfaces. It also adds a PM feedback mechanism for tuning review aggressiveness over time.

### Calibration updates

New outcome dimensions tracked per bundle execution:

- `interventions_count`: how many mid-flight interventions fired
- `interventions_correct`: how many the PM confirmed were warranted
- `questions_asked`: total worker questions
- `questions_llm_answered`: how many the LLM answered without PM
- `questions_escalated`: how many reached PM
- `escalation_response_time_seconds`: how long PM took to respond
- `checkpoints_count`: total worker checkpoints

**PM feedback mechanism.** After a bundle completes, if there were interventions, the calibration loop posts a GitHub comment:

```markdown
## 📊 Review quality feedback

This bundle had <N> mid-flight interventions. Were they helpful?

- `/review-good` — interventions were appropriate and helpful
- `/review-noisy` — too many interventions, worker was on track
- `/review-missed` — important issues weren't caught
```

PM feedback is optional. When provided it updates a `review_calibration` table. This data feeds into future review LLM prompt tuning:
- If review_noisy rate is high: raise the confidence threshold required before intervening
- If review_missed rate is high: lower the confidence threshold

### Report surfacing rules

1. **Final reports → GitHub**: on transition to COMPLETE, if bundle has a github_issue_number, post the final report comment
2. **Checkpoint reports → GitHub (selective)**: post if (a) it's the Nth checkpoint where N is every 3rd, OR (b) the checkpoint's concerns list is non-empty. Other checkpoints go to audit log only.
3. **Intervention reports → audit log only**: NOT posted to GitHub. Query via MCP and CLI only.
4. **Review calibration feedback → GitHub**: post after COMPLETE bundles that had interventions

### Calibration report update

`studio calibration-report` output gains new review dimensions:
- Review intervention rate (interventions per bundle)
- LLM answer rate (% of questions answered without PM)
- Average escalation response time
- Review accuracy rate (from PM feedback, if available)

### Acceptance criteria

1. Bundle completes with interventions: calibration records new dimensions
2. Bundle with interventions: feedback comment posted, `/review-good` processing works
3. High noisy rate: confidence threshold increases
4. Bundle completes: final report comment posted with correct format
5. 6 checkpoints: only 2nd and 5th posted (every 3rd) plus any with concerns
6. Intervention audit log entry does NOT trigger GitHub comment

---

## Security notes

**Question content in transit.** Worker questions may contain snippets of proprietary code or internal system details. Questions routed through the LLM are sent to Ollama Cloud; questions escalated to PM are posted as GitHub comments. Both are encrypted in transit (TLS to Ollama Cloud, HTTPS to GitHub). Questions that contain secrets (detected via pattern matching against the SecretStore) are rejected at the RPC boundary before routing.

**Injection content trust.** `inject_context` content from PMs (via GitHub slash commands, MCP, or CLI) is trusted — the PM is an authorized operator. Content from the review LLM is treated as advisory: the worker receives it as guidance, not as an override of its capability grants. The worker still enforces its own capability checks on any actions it takes.

**Rate limit as abuse prevention.** The question rate limit (10 per execution) prevents a compromised or malfunctioning worker from flooding the orchestrator with questions as a DoS vector. After the limit, questions still flow to PM escalation — they are not silently dropped — but the LLM bypass prevents token cost amplification.

---

## Deferred

**Worker-driven code review.** A worker could request a targeted code review of a specific file or diff before committing. This is a natural extension of `worker.show_artifact` + review LLM but adds complexity around diff generation, PR creation awareness, and review scope. Deferred to Phase 6.

**Multi-worker coordination.** If two workers in the same bundle are working on related code (e.g., one on the API, one on the frontend), the orchestrator could detect conflicts or coordinate handoffs. Requires DAG-level awareness of code surface areas. Deferred.

**Historical review accuracy dashboard.** The `review_calibration` table accumulates PM feedback over time. A dashboard or periodic report surfacing accuracy trends would help operators tune thresholds. The data collection mechanism is built in 5.4; the dashboard is deferred.
