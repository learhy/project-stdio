"""Bundler agent worker: reads an idea, consults memory, produces a full bundle proposal.

Invokes deepseek-v4-pro on Ollama Cloud with a structured system prompt. Communicates
with the orchestrator over JSON-RPC via Unix socket (same pattern as developer worker).
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
_WORKER_ID = os.environ.get("STUDIO_WORKER_ID", "bundler-unknown")
_BUNDLE_ID = os.environ.get("STUDIO_BUNDLE_ID", "unknown")
_NODE_ID = os.environ.get("STUDIO_NODE_ID", "bundler")
_TASK_SPEC_RAW = os.environ.get("STUDIO_TASK_SPEC", "{}")
_HEARTBEAT_INTERVAL = float(os.environ.get("STUDIO_HEARTBEAT_INTERVAL", "30"))
_OLLAMA_BASE_URL = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1")

# ── Bundler system prompt ─────────────────────────────────────────────────────

_BUNDLER_SYSTEM_PROMPT = """You are the Studio bundler agent. Your job is to receive a free-text idea and produce a structured bundle proposal as JSON. You are trusted to plan honestly. The system depends on you not gaming yourself.

## Output format

Respond with a single JSON object matching this schema exactly:

```json
{
  "complexity_score": <int 0-10>,
  "risk_score": <int 0-10>,
  "complexity_factors": {
    "loc": <int 0-10>,
    "components_touched": <int 0-10>,
    "worker_tasks": <int 0-10>,
    "cross_component_coordination": <int 0-10>,
    "new_abstractions": <int 0-10>
  },
  "risk_factors": {
    "security_sensitive_paths": <int 0-10>,
    "data_handling_paths": <int 0-10>,
    "public_interfaces": <int 0-10>,
    "reversibility": <int 1-3>,
    "production_proximity": 0,
    "net_new_dependencies": <int 0-10>
  },
  "estimated_loc": <int>,
  "estimated_duration_seconds": <int>,
  "estimated_worker_count": <int>,
  "estimated_tokens": <int>,
  "target": "<new-repo | existing-repo:<name> | control-plane>",
  "target_repo": "<new-repo:<kebab-case-name> | owner/repo>",
  "target_rationale": "<one-sentence explanation>",
  "concerns": ["<concern 1>", "<concern 2>", ...],
  "requirements_summary": "<paragraph>",
  "rfc_summary": "<paragraph>",
  "implementation_plan": "<paragraph>",
  "task_dag": {
    "nodes": [
      {
        "id": "<string>",
        "kind": "worker | gate | aggregator",
        "spec": {
          "objective": "<required: exact task this worker must complete, with ALL endpoint paths, request/response field names, and expected behavior spelled out in full — the worker must never need to ask follow-up questions>",
          "capability": "<code | file | documentation | review>",
          "description": "<step-by-step implementation details including exact field names, status codes, and data types — every detail the worker needs to complete the task without any clarification questions>",
          "language": "<python | markdown | etc>",
          "dependencies": ["<package names>"]
        }
      }
    ],
    "edges": [
      {
        "from": "<node id>",
        "to": "<node id>",
        "condition": {"kind": "on_success | on_failure | always | on_property", "property": "..."}
      }
    ]
  }
}
```

## Scoring guidance

### Complexity factors (0-10)

- **loc** (lines of code): 0-2 (<100 lines), 3-4 (100-500), 5-6 (500-2000), 7-8 (2000-5000), 9-10 (>5000)
- **components_touched**: number of distinct subdirectories / modules affected
- **worker_tasks**: number of worker nodes in the planned DAG
- **cross_component_coordination**: how many separate components must agree on interfaces
- **new_abstractions**: net-new classes, protocols, or architectural patterns introduced

### Risk factors (0-10)

- **security_sensitive_paths**: does work touch auth, token handling, permission checks, or paths listed in settings.json as security-sensitive?
- **data_handling_paths**: does work read, write, transform, or store user data?
- **public_interfaces**: does work change an externally-visible API, endpoint, or CLI surface?
- **reversibility** (1-3): 1 = trivial rollback (revert commit), 2 = moderate (DB migration with down script), 3 = hard (data format change, API break, multi-service coordination)
- **production_proximity**: always 0 in v1.1 (no production deployment yet)
- **net_new_dependencies**: new packages, services, or external API dependencies

Each factor's contribution must be shown so the reviewer can sanity-check the math.

## Target determination

Set the `target` field using this algorithm:

1. Classify the work:
   - Does the proposal create a new deployable unit (its own deploy step, port, data store, or user-facing surface)? → candidate: "new-repo"
   - Does the proposal explicitly modify files in an existing repo from the registry? → candidate: "existing-repo:<name>"
   - Are all changes internal to the control plane (specs/, settings.json, .github/, orchestrator/, prompts/, templates/)? → candidate: "control-plane"

2. If the submitter provided a `target_hint`, check coherence:
   - If hint matches the classification, use it.
   - If hint conflicts, override and explain in target_rationale.
   - If hint is "existing-repo:<name>" but the repo doesn't exist in the registry, raise a concern.

3. If the target cannot be determined, set target_rationale to "AmbiguousTargetError: cannot determine target automatically" and list candidates in concerns.

## Concerns section — REQUIRED NON-EMPTY

You MUST populate the concerns list with at least one entry. Possible concerns: what could go wrong, what assumptions are you making, what information is missing, what would you want a second opinion on, what calibration signals are relevant.

**"No concerns" on a bundle with risk_score >= 6 is forbidden** — it is a calibration signal that something is off, not confirmation the bundle is safe. If risk >= 6 and you genuinely have no concerns, write: "Risk score is high (>=6) but no specific concerns identified — this is itself a calibration signal."

## Artifact classification

You MUST include two additional top-level fields in your JSON output:

- `artifact_type`: one of `executable_app`, `library`, `infrastructure`, `documentation`, `data_schema`, `test_suite`, `mixed`
- `verification_strategy`: an object with a `type` field matching the artifact_type, plus type-specific fields

## target_repo — REQUIRED

You MUST include a `target_repo` field in every proposal. Use `new-repo:<name>` for new projects (use a short kebab-case name based on the project idea) or `owner/repo` for modifications to existing repos. Never omit this field.

### Per-type verification strategy examples

**executable_app** (Flask, FastAPI, CLI, scripts):
```json
{
  "type": "executable_app",
  "startup_command": "flask run --port 5000",
  "health_check": "GET http://localhost:5000/",
  "smoke_tests": [
    {"method": "GET", "path": "/should-i-have-this-meeting?title=Test", "expected_status": 200},
    {"method": "POST", "path": "/prioritize", "body": {"tasks": ["a", "b"]}, "expected_status": 200}
  ],
  "teardown_command": null
}
```

**library** (Python packages, npm packages):
```json
{
  "type": "library",
  "test_command": "pytest"
}
```

**infrastructure** (Dockerfile, Helm, Terraform):
```json
{
  "type": "infrastructure",
  "validate_command": "helm lint ."
}
```

**documentation** (README, API docs, specs):
```json
{
  "type": "documentation",
  "review": "llm"
}
```

**data_schema** (SQL migrations, JSON schemas, OpenAPI specs):
```json
{
  "type": "data_schema",
  "validate_command": "python -m jsonschema schema.json"
}
```

**mixed**: include a `sub_strategies` array with one entry per sub-type detected.

Classification guidelines:
- Flask/FastAPI/CLI/script → executable_app
- Package with tests → library
- Dockerfile/Helm/Terraform → infrastructure
- Documentation-only → documentation
- Schema/spec-only → data_schema
- Tests-only → test_suite
- Multiple types → mixed (describe each sub-type in sub_strategies)

## Multi-service applications

If the idea describes multiple services (e.g. "a Flask API and a React frontend", "3 microservices", "a backend and dashboard"), you MUST:
1. Set artifact_type to "mixed"
2. Create separate top-level DAG nodes for each service (e.g. build-situation-room, build-metric-massager, build-war-room-dashboard)
3. Each service node should be executable_app type with its own verification_strategy
4. Create a final integration-setup node that depends on ALL service nodes and writes the docker-compose.yml and top-level README
5. Do NOT collapse multiple services into a single library — each service must be independently deployable

Signs that an idea is a multi-service app:
- Mentions multiple ports (port 5001, port 5002, port 3000)
- Mentions Docker Compose or docker-compose.yml
- Describes a "frontend" AND "backend" or "API"
- Describes 2+ independent web services with different responsibilities
- Uses words like "microservice", "service mesh", "multi-service"

## Worker spec requirements — CRITICAL

Every worker node spec MUST be self-contained. The worker runs in an isolated environment with no access to the original idea or other nodes' specs. It cannot ask questions (the question tool is disabled in headless mode). If the worker lacks any detail it needs, it will fail silently.

**The objective and description together must include:**
- Every endpoint path and method (e.g. POST /should-i-have-this-meeting)
- Every request field with its exact name and type (e.g. "attendee_count": int, not "attendees": int)
- Every response field with its exact name (e.g. returns {"answer": "yes", "reason": "..."}, not returns "a decision")
- All required status codes and error conditions
- Any specific behavior requirements (use random.choice, validate input types, etc.)

**Example of a GOOD worker spec:**
```
objective: "Write app.py containing a Flask app with these exact endpoints:
1. POST /should-i-have-this-meeting — accepts JSON {\"title\": \"...\", \"attendee_count\": N}, returns {\"answer\": \"yes\", \"reason\": \"...\"} or {\"answer\": \"no\", \"reason\": \"...\"}
2. POST /estimate-multiplier — accepts {\"task_description\": \"...\", \"original_estimate_hours\": N}, returns {\"multiplier\": 1.5, \"revised_hours\": ..., \"explanation\": \"...\"}
3. GET /standup-excuse — returns {\"excuse\": \"...\"}
4. POST /prioritize — accepts {\"tasks\": [...], \"criteria\": \"...\"}, returns {\"prioritized\": [...]}
Include proper error handling: return 400 JSON {\"error\": \"...\"} for missing parameters."
description: "Use flask. For /should-i-have-this-meeting, use random.choice on a list of yes/no with matching reasons. For /estimate-multiplier, pick a random multiplier between 1.5-3.0 and compute revised_hours. For /standup-excuse, pick from a hardcoded list of 5-10 funny tech excuses. For /prioritize, sort the tasks array based on relevance to criteria string. Validate all input types."
```

**Example of a BAD worker spec (will cause worker failure):**
```
objective: "Write app.py with the four required endpoints"
description: "Implement the endpoints as described in the requirements"
```

## Memory context

If memory files are provided below (prior decisions, killed ideas, calibration data), factor them into your analysis. Reference specific past bundles when relevant. If calibration data shows systematic under-estimation of complexity, adjust upward.
"""


def _now() -> int:
    return int(time.time())


def _load_task_spec() -> dict[str, Any]:
    try:
        return json.loads(_TASK_SPEC_RAW)
    except (json.JSONDecodeError, TypeError):
        return {}


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

from .client import RpcClient, get_orchestrator_addr_display


# ── Memory readers ────────────────────────────────────────────────────────────


def _read_file(path: str) -> str | None:
    try:
        p = os.path.join("/work", path)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()[:32000]  # cap per file
    except Exception:
        return None


def _list_memory_files(subdir: str) -> list[str]:
    """List files in a memory subdirectory, newest first."""
    try:
        p = os.path.join("/work", "memory", subdir)
        if not os.path.isdir(p):
            return []
        files = sorted(
            [f for f in os.listdir(p) if os.path.isfile(os.path.join(p, f))],
            key=lambda f: os.path.getmtime(os.path.join(p, f)),
            reverse=True,
        )
        return files[:10]
    except Exception:
        return []


def _build_memory_context() -> str:
    """Gather memory file contents for the bundler prompt."""
    parts: list[str] = []

    agents_md = _read_file("AGENTS.md")
    if agents_md:
        parts.append("## AGENTS.md\n\n" + agents_md)

    # Recent decisions
    decision_files = _list_memory_files("decisions")
    if decision_files:
        parts.append("## Recent decisions (memory/decisions/)\n")
        for fname in decision_files[:5]:
            content = _read_file(f"memory/decisions/{fname}")
            if content:
                parts.append(f"### {fname}\n{content[:4000]}")

    # Killed ideas
    killed_files = _list_memory_files("killed")
    if killed_files:
        parts.append("## Prior killed ideas (memory/killed/)\n")
        for fname in killed_files[:5]:
            content = _read_file(f"memory/killed/{fname}")
            if content:
                parts.append(f"### {fname}\n{content[:4000]}")

    # Calibration data
    cal_files = _list_memory_files("calibration")
    if cal_files:
        parts.append("## Calibration data (memory/calibration/)\n")
        for fname in cal_files[:3]:
            content = _read_file(f"memory/calibration/{fname}")
            if content:
                parts.append(f"### {fname}\n{content[:4000]}")

    return "\n\n".join(parts) if parts else "(no memory context available)"


# ── LLM call ─────────────────────────────────────────────────────────────────


def _call_llm(system_prompt: str, user_message: str) -> dict:
    """POST to Ollama Cloud chat completions. Returns parsed JSON response.

    Falls back to a minimal stub response if the API is unreachable (for testing).
    """
    import urllib.request

    body = json.dumps({
        "model": "deepseek-v4-pro:cloud",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "options": {"thinking_mode": "high"},
    }).encode()

    api_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
    url = f"{_OLLAMA_BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc), "fallback": True}

    # OpenAI-compatible format: content is at choices[0].message.content
    # Fall back to native Ollama format: message.content
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = raw.get("message", {}).get("content", "")
    return _extract_json(content)


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of LLM response text, handling markdown fences."""
    # Try parsing the whole text first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try content inside ```json fences
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        try:
            return json.loads(text[start:end].strip())
        except (json.JSONDecodeError, TypeError):
            pass

    # Try content inside ``` fences
    if "```" in text:
        start = text.index("```") + 3
        # Skip optional language tag
        nl = text.index("\n", start) if "\n" in text[start:] else start
        end = text.index("```", nl + 3)
        try:
            return json.loads(text[nl:end].strip())
        except (json.JSONDecodeError, TypeError):
            pass

    # Find first { ... } pair
    try:
        brace_start = text.index("{")
        depth = 0
        for i, ch in enumerate(text[brace_start:], brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[brace_start : i + 1])
    except (ValueError, json.JSONDecodeError):
        pass

    return {"parse_error": True, "raw_text": text[:1000]}


def _has_multi_service_signals(idea: str) -> bool:
    """Detect whether an idea string describes a multi-service application."""
    lower = idea.lower()
    if any(kw in lower for kw in ("docker-compose", "docker compose",
                                    "multiple services", "microservice",
                                    "micro-service", "multi-service")):
        return True
    web_backend = any(kw in lower for kw in ("flask", "fastapi", "express",
                                               "django", "rails"))
    web_frontend = any(kw in lower for kw in ("react", "vue", "angular",
                                                "next.js", "svelte", "dashboard"))
    if web_backend and web_frontend:
        return True
    # Multiple port mentions
    import re
    ports = re.findall(r"port\s*(\d+)", lower)
    if len(set(ports)) >= 2:
        return True
    return False


# ── Bundler Worker ─────────────────────────────────────────────────────────────


class BundlerWorker:
    """Bundler agent: reads an idea, consults memory, produces a full bundle proposal."""

    def __init__(self) -> None:
        self.rpc = RpcClient()
        self.task_spec = _load_task_spec()
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

        self._log(f"Authenticated as bundler {_WORKER_ID} (bundle {_BUNDLE_ID})")
        self._running = True

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            idea = self.task_spec.get("idea", self.task_spec.get("objective", "plan a software change"))
            outcome = await self._execute_task(idea)
        except Exception as exc:
            outcome = {"outcome": "failure", "errors": [str(exc)], "summary": f"Bundler crashed: {exc}"}
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

        proposal = outcome.get("proposal")
        if proposal is not None:
            final_params["proposal"] = proposal

        await self.rpc.call("worker.final_report", final_params)
        self._log(f"Final report sent: {outcome.get('outcome', 'failure')}")

        await self.rpc.close()
        return 0 if outcome.get("outcome") == "success" else 1

    # ── Heartbeat ──────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        phase = "thinking"
        while self._running:
            try:
                await self.rpc.call("worker.heartbeat", {
                    "phase": phase,
                    "progress": "",
                    "current_step": None,
                    "estimated_completion_seconds": None,
                })
            except Exception:
                pass
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    # ── Task execution ─────────────────────────────────────────────────────

    async def _execute_task(self, idea: str) -> dict:
        self._log(f"Planning idea: {idea[:200]}")
        await self.rpc.notify("worker.progress_report", {
            "stage": "reading-memory",
            "percent": 5,
            "message": "Reading memory and AGENTS.md...",
        })

        memory_ctx = _build_memory_context()

        await self.rpc.notify("worker.progress_report", {
            "stage": "calling-llm",
            "percent": 20,
            "message": "Calling deepseek-v4-pro to plan bundle...",
        })

        # Pass artifact type hint from orchestrator if available
        hint = self.task_spec.get("artifact_type_hint", "")
        hint_line = (
            f"\nIMPORTANT: The orchestrator detected artifact_type_hint={hint}. "
            f"Strongly prefer this type unless you have a clear reason to override.\n"
            if hint else ""
        )

        user_message = f"""## Idea to plan

{idea}
{hint_line}
## Memory context

{memory_ctx}

Produce the bundle proposal JSON now. Include all required fields: complexity and risk scores with factor breakdowns, target determination with rationale, a non-empty concerns section, requirements summary, RFC summary, implementation plan, and a task DAG with capability manifests for each worker node.
"""

        result = await asyncio.to_thread(_call_llm, _BUNDLER_SYSTEM_PROMPT, user_message)

        if "error" in result:
            await self.rpc.notify("worker.progress_report", {
                "stage": "llm-error",
                "percent": 100,
                "message": f"LLM call failed: {result.get('error', 'unknown')}",
            })
            return {
                "outcome": "failure",
                "summary": f"LLM API call failed: {result.get('error', 'unknown')}",
                "errors": [result.get("error", "LLM call failed")],
            }

        if result.get("parse_error") or result.get("fallback"):
            await self.rpc.notify("worker.progress_report", {
                "stage": "parse-failed",
                "percent": 100,
                "message": "Failed to parse LLM response as JSON",
            })
            return {
                "outcome": "failure",
                "summary": "Failed to parse LLM response as structured JSON proposal",
                "errors": ["LLM response could not be parsed as JSON"],
            }

        def _build_proposal(r: dict) -> dict:
            target = r.get("target", "control-plane")
            target_repo = r.get("target_repo", "")
            if not target_repo:
                if target.startswith("existing-repo:"):
                    target_repo = target.split(":", 1)[1]
                elif target == "new-repo":
                    import re
                    slug = re.sub(r'[^a-z0-9]+', '-', idea.lower()).strip('-')[:40]
                    target_repo = f"new-repo:{slug}"
            return {
                "complexity_score": r.get("complexity_score", 0),
                "risk_score": r.get("risk_score", 0),
                "complexity_factors": r.get("complexity_factors", {}),
                "risk_factors": r.get("risk_factors", {}),
                "estimated_loc": r.get("estimated_loc", 0),
                "estimated_duration_seconds": r.get("estimated_duration_seconds", 0),
                "estimated_worker_count": r.get("estimated_worker_count", 0),
                "estimated_tokens": r.get("estimated_tokens", 0),
                "target": target,
                "target_repo": target_repo,
                "target_rationale": r.get("target_rationale", ""),
                "concerns": r.get("concerns", ["(bundler did not populate concerns)"]),
                "requirements_summary": r.get("requirements_summary", ""),
                "rfc_summary": r.get("rfc_summary", ""),
                "implementation_plan": r.get("implementation_plan", ""),
                "task_dag": r.get("task_dag", {"nodes": [], "edges": []}),
                "artifact_type": r.get("artifact_type", "mixed"),
                "verification_strategy": r.get("verification_strategy", None),
            }

        proposal = _build_proposal(result)

        # ── Validation: multi-service idea but wrong artifact_type ─────────
        has_multi = _has_multi_service_signals(idea)
        artifact_type = proposal.get("artifact_type", "")
        if has_multi and artifact_type not in ("mixed",):
            self._log(
                f"Warning: idea has multi-service signals but artifact_type={artifact_type}. "
                f"Re-prompting bundler to use MIXED with separate service nodes."
            )
            correction = (
                f"\nYour plan has artifact_type={artifact_type} but the idea describes what "
                f"appears to be a multi-service application. Please reconsider and use "
                f"artifact_type=mixed with separate DAG nodes for each service, plus an "
                f"integration-setup node for docker-compose.yml.\n"
            )
            user_message += correction
            result2 = await asyncio.to_thread(_call_llm, _BUNDLER_SYSTEM_PROMPT, user_message)
            if not result2.get("error") and not result2.get("parse_error") and not result2.get("fallback"):
                proposal = _build_proposal(result2)
                self._log(
                    f"Re-prompted proposal: artifact_type={proposal.get('artifact_type')}"
                )
        # ──────────────────────────────────────────────────────────────────

        await self.rpc.notify("worker.progress_report", {
            "stage": "complete",
            "percent": 100,
            "message": f"Bundle planned: C={proposal['complexity_score']} R={proposal['risk_score']}, "
                       f"target={proposal['target']}, {len(proposal['task_dag'].get('nodes', []))} DAG nodes",
        })

        self._log(
            f"Proposal complete: C={proposal['complexity_score']} R={proposal['risk_score']} "
            f"target={proposal['target']} concerns={len(proposal['concerns'])}"
        )

        return {
            "outcome": "success",
            "summary": f"Bundle planned: C={proposal['complexity_score']} R={proposal['risk_score']}",
            "proposal": proposal,
            "errors": [],
        }

    # ── Internal logging ───────────────────────────────────────────────────

    @staticmethod
    def _log(msg: str) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    worker = BundlerWorker()
    exit_code = asyncio.run(worker.run())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
