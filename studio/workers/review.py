"""Pre-execution review track worker: adversarial critique, security review, QA verification planning.

Invokes deepseek-v4-pro on Ollama Cloud with a role-specific system prompt. Communicates
with the orchestrator over JSON-RPC via Unix socket (same pattern as bundler worker).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from typing import Any

# ── Configuration from environment ────────────────────────────────────────────

_TOKEN = os.environ.get("STUDIO_WORKER_TOKEN", "")
_WORKER_ID = os.environ.get("STUDIO_WORKER_ID", "review-unknown")
_BUNDLE_ID = os.environ.get("STUDIO_BUNDLE_ID", "unknown")
_NODE_ID = os.environ.get("STUDIO_NODE_ID", "adversarial")
_TASK_SPEC_RAW = os.environ.get("STUDIO_TASK_SPEC", "{}")
_HEARTBEAT_INTERVAL = float(os.environ.get("STUDIO_HEARTBEAT_INTERVAL", "30"))
_OLLAMA_BASE_URL = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1")

# ── System prompts ────────────────────────────────────────────────────────────

_ADVERSARIAL_PROMPT = """You are the Studio adversarial critique agent. Your job is to find weaknesses in bundle proposals before they reach human review. You are a generalist critic: you look for weak reasoning, unaddressed counter-cases, scope creep, hidden complexity, and mismatches between layers of the proposal.

## Input

You will receive a JSON bundle proposal containing:
- An idea (free-text request)
- Requirements summary
- RFC summary
- Implementation plan
- Complexity and risk scores with factor breakdowns
- Bundle concerns
- Task DAG

## Output format

Respond with a single JSON object matching this schema exactly:

```json
{
  "findings": [
    {
      "severity": "low|medium|high",
      "status": "unresolved|resolved|accepted-risk",
      "category": "one of: weak-reasoning, unaddressed-counter-case, scope-creep, hidden-complexity, requirements-rfc-mismatch, rfc-impl-mismatch, missing-concern, underestimated-scoring, other",
      "finding": "<one-sentence description of what you found>",
      "recommendation": "<what should change>",
      "rationale": "<why this matters>"
    }
  ],
  "blocking_issue": <true if any finding is severe enough that the bundle should be re-planned before reaching human review, otherwise false>,
  "blocking_reason": "<if blocking_issue is true, explain why in one sentence>",
  "summary": "<2-3 sentence overall assessment>"
}
```

## Rules

- Severity "low": minor issues, stylistic concerns, optional improvements.
- Severity "medium": meaningful gaps in reasoning or planning. Should be addressed but not necessarily blocking.
- Severity "high": significant flaws that could lead to wrong implementation, missed requirements, or unanticipated complexity. Consider setting blocking_issue=true.
- Set blocking_issue=true ONLY for findings that genuinely make the proposal unfit for human review.
- Be specific. Vague critiques like "this could be better" are not useful.
- Every finding must have a concrete recommendation.
- Leave status as "unresolved" — the bundler or reviewer will resolve.
- At least one finding is expected. If you genuinely find nothing wrong, explain why in the summary.
"""

_SECURITY_PROMPT = """You are the Studio security review agent. Your job is to audit bundle proposals for security vulnerabilities, unsafe patterns, and threat surfaces before they reach human review. You bring a specialist security lens.

## Input

You will receive a JSON bundle proposal containing:
- An idea (free-text request)
- Requirements summary
- RFC summary
- Implementation plan
- Task DAG with capability manifests
- Complexity and risk scores

## Output format

Respond with a single JSON object matching this schema exactly:

```json
{
  "findings": [
    {
      "severity": "info|low|medium|high|critical",
      "status": "unresolved|resolved|accepted-risk",
      "category": "one of: auth, authorization, data-handling, input-validation, dependency-cve, supply-chain, secret-leak, token-handling, fail-open, privilege-escalation, injection, logging-leak, crypto, session, csrf, other",
      "finding": "<one-sentence description of the security issue>",
      "recommendation": "<what should change>",
      "rationale": "<why this is a security concern>"
    }
  ],
  "threat_model": {
    "summary": "<one-paragraph threat model overview>",
    "assets": ["<asset that needs protection>"],
    "threats": ["<specific threat>"],
    "mitigations": ["<how the proposal mitigates or should mitigate>"],
    "open_risks": ["<risks not addressed by the proposal>"]
  },
  "blocking_issue": <true if any finding should block the bundle from reaching human review in its current state, otherwise false>,
  "blocking_reason": "<if blocking_issue is true, explain why in one sentence>",
  "summary": "<2-3 sentence overall security assessment>"
}
```

## Hard rules

- **Critical findings**: auth bypass, secret leak, crypto broken, privilege escalation, injection with data loss. Always set status="unresolved" for critical findings.
- **High findings**: missing auth on sensitive endpoint, unsafe deserialization, logging PII/secrets, fail-open on security check. These disable auto-ship.
- **Medium findings**: missing input validation, outdated dependency with known CVE, overly broad file permissions, missing rate limiting.
- **Threat model is required** when the bundle touches auth, data handling, external surfaces, secrets, billing, or PII. Otherwise set threat_model to null.
- **Bundles touching auth, billing, secrets, or PII**: always produce at least one finding documenting the sensitivity.
- Every external input is hostile. Every dependency is suspect. Every log message is public.
- Be specific and actionable. Generic "check for SQL injection" is not a finding.
"""

_QA_PROMPT = """You are the Studio QA / verification planning agent. Your job is to produce a Verification Plan for a bundle proposal before it reaches human review. You plan how to verify the bundle's work — you do NOT test code (it may not exist yet).

## Input

You will receive a JSON bundle proposal containing:
- An idea (free-text request)
- Requirements summary
- RFC summary
- Implementation plan
- Task DAG
- Complexity and risk scores

## Output format

Respond with a single JSON object matching this schema exactly:

```json
{
  "verification_plan": {
    "acceptance_criteria": ["<observable, testable condition tied to requirements>"],
    "test_surface": {
      "unit": "<what to unit-test, coverage target %>",
      "integration": "<integration test scope>",
      "end_to_end": "<e2e test scenario or 'none'>",
      "load": "<load test scenario or 'none'>",
      "manual_smoke": "<manual smoke checklist items>"
    },
    "pre_merge_gates": ["<specific gate: CI pass, coverage >= X%, security findings resolved, etc.>"],
    "post_ship_verification": {
      "metrics": ["<specific metric to monitor>"],
      "time_window_hours": <int, how long to monitor>,
      "expected_ranges": {"<metric>": "<expected range>"}
    },
    "rollback_plan": {
      "machine_executable": <true if rollback can be fully automated, false if manual steps needed>,
      "auto_rollback_eligible": <true if rollback is safe to trigger automatically on verification failure, false otherwise>,
      "steps": ["<step to execute rollback>"],
      "recovery_time_estimate_seconds": <int, estimated time to complete rollback>
    }
  },
  "findings": [
    {
      "severity": "low|medium|high",
      "status": "unresolved",
      "category": "one of: untestable-requirement, missing-acceptance-criteria, rollback-gap, coverage-gap, observability-gap, timeline-risk, dependency-risk, other",
      "finding": "<one-sentence description>",
      "recommendation": "<what should change>",
      "rationale": "<why this matters for verification>"
    }
  ],
  "blocking_issue": <true if the bundle lacks a viable verification approach or rollback plan, otherwise false>,
  "blocking_reason": "<if blocking_issue is true, explain why in one sentence>",
  "summary": "<2-3 sentence overall QA assessment>"
}
```

## Hard rules

- **No bundle reaches human review without a Verification Plan.** If you cannot produce one, set blocking_issue=true and explain why.
- **Acceptance criteria must be observable and testable.** "The code is correct" is not a criterion. "GET /health returns 200 with {status: ok}" is.
- **Rollback plan is required.** If the bundle truly cannot be rolled back (irreversible migration, etc.), document that explicitly and set machine_executable=false, auto_rollback_eligible=false.
- **Bundles without a viable rollback**: document in findings with severity=high. The system will auto-bump Reversibility to 3.
- At least 3 acceptance criteria. At least 2 pre-merge gates.
"""

# ── LLM helpers ────────────────────────────────────────────────────────────────


def _extract_json(text: str) -> dict:
    import json as _json
    # Strategy 1: ```json ... ``` fence
    if "```json" in text:
        try:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return _json.loads(text[start:end].strip())
        except (ValueError, _json.JSONDecodeError):
            pass
    # Strategy 2: ``` fence without language tag
    if "```" in text:
        try:
            start = text.index("```") + 3
            end = text.index("```", start)
            return _json.loads(text[start:end].strip())
        except (ValueError, _json.JSONDecodeError):
            pass
    # Strategy 3: plain JSON
    try:
        return _json.loads(text.strip())
    except _json.JSONDecodeError:
        pass
    # Strategy 4: find first { ... } pair
    try:
        brace_start = text.index("{")
        depth = 0
        for i, ch in enumerate(text[brace_start:], brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return _json.loads(text[brace_start : i + 1])
    except (ValueError, _json.JSONDecodeError):
        pass

    return {"parse_error": True, "raw_text": text[:1000]}


def _call_llm(system_prompt: str, user_message: str) -> dict:
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "model": "deepseek-v4-pro:cloud",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message[:64000]},
        ],
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{_OLLAMA_BASE_URL}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        # OpenAI-compatible format: content is at choices[0].message.content
        # Fall back to native Ollama format: message.content
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = body.get("message", {}).get("content", "")
        return _extract_json(content)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# ── RPC client ─────────────────────────────────────────────────────────────────

from .client import RpcClient, get_orchestrator_addr_display


# ── Memory helpers ─────────────────────────────────────────────────────────────


def _read_file(path: str) -> str | None:
    try:
        p = os.path.join("/work", path)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()[:32000]
    except Exception:
        return None


def _build_proposal_context(task_spec: dict) -> str:
    """Build the user message: the full bundle proposal as context."""
    parts: list[str] = []
    parts.append("## Bundle Proposal for Review\n")

    idea = task_spec.get("idea", task_spec.get("objective", ""))
    if idea:
        parts.append(f"### Idea\n{idea}\n")

    reqs = task_spec.get("requirements_summary", "")
    if reqs:
        parts.append(f"### Requirements Summary\n{reqs}\n")

    rfc = task_spec.get("rfc_summary", "")
    if rfc:
        parts.append(f"### RFC Summary\n{rfc}\n")

    impl = task_spec.get("implementation_plan", "")
    if impl:
        parts.append(f"### Implementation Plan\n{impl}\n")

    concerns = task_spec.get("concerns", [])
    if concerns:
        parts.append("### Bundle Concerns\n")
        for c in concerns:
            parts.append(f"- {c}")
        parts.append("")

    parts.append("### Scoring\n")
    parts.append(f"- Complexity: {task_spec.get('complexity_score', 0)}/10")
    parts.append(f"- Risk: {task_spec.get('risk_score', 0)}/10")

    complexity_factors = task_spec.get("complexity_factors", {})
    if complexity_factors:
        parts.append(f"- Complexity factors: {json.dumps(complexity_factors)}")

    risk_factors = task_spec.get("risk_factors", {})
    if risk_factors:
        parts.append(f"- Risk factors: {json.dumps(risk_factors)}")

    target = task_spec.get("target", "control-plane")
    parts.append(f"\n### Target\n{target}")
    rationale = task_spec.get("target_rationale", "")
    if rationale:
        parts.append(f"Rationale: {rationale}")

    dag = task_spec.get("task_dag", {})
    if dag:
        parts.append(f"\n### Task DAG\n```json\n{json.dumps(dag, indent=2)}\n```")

    return "\n".join(parts)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _load_task_spec() -> dict:
    try:
        return json.loads(_TASK_SPEC_RAW)
    except json.JSONDecodeError:
        return {}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Review Worker ──────────────────────────────────────────────────────────────


class ReviewWorker:
    """Review track agent: reads a bundle proposal, critiques it, produces findings."""

    def __init__(self) -> None:
        self.rpc = RpcClient()
        self.task_spec = _load_task_spec()
        self.role: str = self.task_spec.get("role", _NODE_ID)
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def run(self) -> int:
        if not _TOKEN:
            self._log("ERROR: STUDIO_WORKER_TOKEN not set — cannot authenticate")
            return 1

        try:
            await self.rpc.connect()
        except Exception as exc:
            addr_display = get_orchestrator_addr_display()
            self._log(f"ERROR: cannot connect to orchestrator at {addr_display}: {exc}")
            return 1

        auth_resp = await self.rpc.call("auth", {"token": _TOKEN})
        if "error" in auth_resp:
            self._log(f"ERROR: auth failed: {auth_resp['error'].get('message', auth_resp['error'])}")
            await self.rpc.close()
            return 1

        bound = auth_resp.get("result", {}).get("bound", False)
        if not bound:
            self._log("ERROR: auth rejected — token not bound to any worker")
            await self.rpc.close()
            return 1

        self._log(f"Authenticated as {self.role} reviewer {_WORKER_ID} (bundle {_BUNDLE_ID})")
        self._running = True

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            outcome = await self._execute_task()
        except Exception as exc:
            outcome = {"outcome": "failure", "errors": [str(exc)], "summary": f"Review worker crashed: {exc}"}
        finally:
            self._running = False
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass

        final_params: dict[str, Any] = {
            "outcome": outcome.get("outcome", "failure"),
            "summary": outcome.get("summary", ""),
            "errors": outcome.get("errors", []),
        }

        if outcome.get("blocking_issue"):
            final_params["blocking_issue"] = True
            final_params["blocking_reason"] = outcome.get("blocking_reason", "")

        findings = outcome.get("findings")
        if findings is not None:
            final_params["findings"] = findings

        if outcome.get("threat_model"):
            final_params["threat_model"] = outcome["threat_model"]

        if outcome.get("verification_plan"):
            final_params["verification_plan"] = outcome["verification_plan"]

        await self.rpc.call("worker.final_report", final_params)
        self._log(f"Final report sent: {outcome.get('outcome', 'failure')}")

        await self.rpc.close()
        return 0 if outcome.get("outcome") == "success" else 1

    # ── Heartbeat ──────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        phase = "thinking"
        while self._running:
            try:
                await self.rpc.notify("worker.heartbeat", {"phase": phase})
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
            except Exception:
                await asyncio.sleep(5)

    # ── Task execution ─────────────────────────────────────────────────────

    async def _execute_task(self) -> dict[str, Any]:
        if self.role == "adversarial":
            prompt = _ADVERSARIAL_PROMPT
        elif self.role == "security":
            prompt = _SECURITY_PROMPT
        elif self.role == "qa":
            prompt = _QA_PROMPT
        else:
            return {
                "outcome": "failure",
                "summary": f"Unknown review role: {self.role}",
                "errors": [f"Unknown role: {self.role}"],
            }

        proposal_context = _build_proposal_context(self.task_spec)

        await self.rpc.notify("worker.progress_report", {
            "stage": f"{self.role}-review",
            "percent": 50,
            "message": f"Running {self.role} review...",
        })

        result = _call_llm(prompt, proposal_context)

        if "error" in result:
            await self.rpc.notify("worker.progress_report", {
                "stage": f"{self.role}-llm-error",
                "percent": 100,
                "message": f"LLM call failed: {result.get('error', 'unknown')}",
            })
            return {
                "outcome": "failure",
                "summary": f"LLM API call failed: {result.get('error', 'unknown')}",
                "errors": [result.get("error", "LLM call failed")],
            }

        if result.get("parse_error"):
            await self.rpc.notify("worker.progress_report", {
                "stage": f"{self.role}-parse-failed",
                "percent": 100,
                "message": "Failed to parse LLM response as JSON",
            })
            return {
                "outcome": "failure",
                "summary": f"Failed to parse LLM response for {self.role} review",
                "errors": ["LLM response could not be parsed as JSON"],
            }

        findings_raw = result.get("findings", [])
        findings = []
        for f in findings_raw:
            if isinstance(f, dict):
                findings.append({
                    "severity": f.get("severity", "low"),
                    "status": f.get("status", "unresolved"),
                    "category": f.get("category", "other"),
                    "finding": f.get("finding", ""),
                    "recommendation": f.get("recommendation", ""),
                    "rationale": f.get("rationale", ""),
                })

        outcome: dict[str, Any] = {
            "outcome": "success",
            "summary": result.get("summary", f"{self.role} review complete"),
            "findings": findings,
        }

        if result.get("blocking_issue"):
            outcome["blocking_issue"] = True
            outcome["blocking_reason"] = result.get("blocking_reason", "")

        if self.role == "security":
            tm = result.get("threat_model")
            if tm and isinstance(tm, dict):
                outcome["threat_model"] = tm

        if self.role == "qa":
            vp = result.get("verification_plan")
            if vp and isinstance(vp, dict):
                outcome["verification_plan"] = vp

        return outcome

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _log(msg: str) -> None:
        ts = _now_iso()
        print(f"[{ts}] review-worker: {msg}", file=sys.stderr, flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    worker = ReviewWorker()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    exit_code = 1
    try:
        exit_code = loop.run_until_complete(worker.run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[{_now_iso()}] review-worker: FATAL: {exc}", file=sys.stderr, flush=True)
    finally:
        loop.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
